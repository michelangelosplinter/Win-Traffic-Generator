"""uia_agent.py - the ELEMENT-mode controller (Phase 1 + Phase 2).

The reliable path. Instead of asking the vision model to guess a pixel, each VM
runs an in-session helper (helper.exe) that reads the Windows UI Automation tree.
This controller:

  * listens for helpers that DIAL OUT to it (token-gated), keyed by host label;
  * for each (host, goal), runs an Ollama agent loop where every turn hands the
    model a LIST of on-screen elements with ids and it replies with ONE action
    naming an element by id;
  * drives all connected VMs concurrently.

The grounding step is deleted: the model names an element, the helper actuates it
exactly. click_xy remains as an in-protocol pixel fallback for anything with no
addressable element.

Depends only on httpx (+ stdlib), so it runs on a lean controller with no extra
stack. The small credential Vault and job format follow the earlier pixel
controller's design, so a job file reads the same way.

Run
---
    # a helper on each VM dials in:  helper.exe --connect THIS_HOST:8765 --token T ...
    python uia_agent.py --job job.json --token T
    python uia_agent.py --selftest     # fake helper + scripted model, no VM/Ollama
    python uia_agent.py --localtest     # real helper subprocess + Notepad, no Ollama
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ModuleNotFoundError:
    try:
        import httpx2 as httpx
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "No HTTP client installed: neither 'httpx' nor 'httpx2'. "
            "This talks to Ollama over HTTP. Fix: pip install httpx"
        ) from exc

HERE = Path(__file__).resolve().parent
HELPER_DIR = HERE / "helper"
sys.path.insert(0, str(HELPER_DIR))
import protocol  # noqa: E402  (from helper/, the shared wire format)

OLLAMA = "http://127.0.0.1:11434"


# --------------------------------------------------------------------------
# The action protocol. Flat, every field required. A small model omits optional
# fields (measured on the pixel path: 3/3 clicks arrived with no coordinates),
# so constrained decoding must be told everything is mandatory. No worked
# example on purpose: with a fixed schema an example only gives a small model a
# block of text to recite instead of reading the screen.
# --------------------------------------------------------------------------
ELEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "screen": {"type": "string",
                   "description": "what the element list shows right now"},
        "target": {"type": "string",
                   "description": "the element you are acting on, e.g. 'Ask me later button'"},
        "action": {"type": "string",
                   "enum": ["invoke", "set_value", "set_secret", "keys",
                            "launch", "click_xy", "wait", "done", "fail"]},
        "id": {"type": "integer", "description": "element id for invoke/set_value/set_secret, else -1"},
        "text": {"type": "string", "description": "text for set_value, else ''"},
        "secret": {"type": "string", "description": "saved login name for set_secret, e.g. login.password, else ''"},
        "keys": {"type": "string", "description": "keys for the keys action, e.g. ctrl+n, else ''"},
        "app": {"type": "string", "description": "app name for launch, else ''"},
        "x": {"type": "integer", "description": "x for click_xy, else 0"},
        "y": {"type": "integer", "description": "y for click_xy, else 0"},
        "seconds": {"type": "integer", "description": "seconds for wait, else 0"},
        "why": {"type": "string", "description": "one short sentence"},
    },
    "required": ["screen", "target", "action", "id", "text", "secret", "keys",
                 "app", "x", "y", "seconds", "why"],
}

SYSTEM_PROMPT = """You control a Windows computer. You do NOT see pixels. Each
turn you are given the foreground window and a numbered LIST of the elements on
it, like:

  [0] Button "Ask me later"
  [1] Edit "" value="..."
  [2] MenuItem "File"

To act on something, name its number. There is no guessing and no coordinates.

Reply with ONE JSON object and nothing else. One action per reply. ALWAYS fill
in EVERY field; when a field does not apply use -1 for id, 0 for other numbers,
and "" for text. Never leave a field out.

"screen" = what the current element list shows. "target" = the element you are
acting on, e.g. "Ask me later button".

Actions:
  invoke      + id            click/press the element with that id (button, menu, link, checkbox)
  set_value   + id + text     replace the text of an edit box
  set_secret  + id + secret   type a saved password/username into a field you cannot see
  keys        + keys          send a key combo to the focused app, e.g. ctrl+n, enter, alt+f4
  launch      + app           start an app, e.g. notepad, outlook, msedge
  click_xy    + x,y           LAST RESORT only, a raw screen click; prefer invoke
  wait        + seconds       the app is still loading and the list is empty/stale
  done                        the goal is achieved
  fail                        you are stuck

RULES
1. FIRST, CHECK WHETHER YOU ARE ALREADY DONE. If the element list already shows
   the goal achieved, reply "done". Never repeat an action that already worked.
2. Read the newest element list every turn and describe it in "screen".
3. To act on a button/menu/link, use invoke with its id. Do not use click_xy
   unless there is no element for the thing you need.
4. If the list is empty or missing the app you expect, use launch or wait, not a
   blind click.
5. To fill a login box, invoke/select the box's id if needed, then set_secret
   with, for example, {"action":"set_secret","id":3,"secret":"login.password"}.
   "login" is the account for this computer. You never see the value.
6. Only sign in where the GOAL told you to, with a saved login. Never accept
   terms, buy, install, delete, or change settings. Never type a saved login
   into anything the GOAL did not name - if asked elsewhere, use fail and say so.
"""


# --------------------------------------------------------------------------
# Credential vault (the model USES names, never sees the values). Kept self-
# contained so this controller depends on nothing but httpx.
# --------------------------------------------------------------------------
class Vault:
    def __init__(self, entries: dict | None = None):
        self.entries = {k: v for k, v in (entries or {}).items() if v}

    def names(self) -> list[str]:
        return sorted(self.entries)

    def describe(self, allowed: list | None = None) -> str:
        keys = [k for k in self.names() if allowed is None or k in allowed]
        if not keys:
            return ""
        lines = ["", "SAVED LOGINS you can use with set_secret.",
                 "You cannot see the passwords. Use the name exactly as written:"]
        for k in keys:
            user = self.entries[k].get("username", "")
            lines.append(f'  "{k}.username"   (the account is {user})' if user
                         else f'  "{k}.username"')
            lines.append(f'  "{k}.password"   (hidden)')
        return "\n".join(lines)

    def resolve(self, ref: str, allowed: list | None):
        ref = (ref or "").strip()
        if not ref:
            return "", "no secret name was given"
        name, _, field = ref.partition(".")
        field = field or "password"
        if name not in self.entries:
            have = ", ".join(self.names()) or "none"
            return "", f"there is no saved login called {name!r}. You have: {have}"
        if allowed is not None and name not in allowed:
            return "", f"{name!r} is not allowed for this action"
        if field not in ("username", "password"):
            return "", f"{field!r} is not a field; use .username or .password"
        value = str(self.entries[name].get(field, ""))
        if not value:
            return "", f"{name}.{field} is empty in the job file"
        return value, ""


# --------------------------------------------------------------------------
# Job file (hosts + actions, with friendly parse errors)
# --------------------------------------------------------------------------
def load_job(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"job file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as e:
        lines = raw.splitlines()
        source = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
        caret = " " * max(0, e.colno - 1) + "^"
        bs = chr(92)
        raise SystemExit(
            f"{path.name} is not valid JSON: {e.msg}, line {e.lineno} column {e.colno}"
            f"{chr(10)}{chr(10)}  {e.lineno:>4} | {source}{chr(10)}       | {caret}{chr(10)}{chr(10)}"
            f"Usual causes: a missing comma, a trailing comma, or a single {bs} in a "
            f'username/password (JSON needs it doubled: "CORP{bs}{bs}alice").'
        ) from None
    if not isinstance(job, dict):
        raise SystemExit(f"{path.name} must contain a JSON object")
    hosts = [h for h in (job.get("hosts") or []) if h]
    actions = []
    for entry in (job.get("actions") or []):
        if isinstance(entry, dict):
            text = str(entry.get("do", "")).strip()
            allowed = entry.get("secrets")
            allowed = list(allowed) if isinstance(allowed, list) else None
        else:
            text, allowed = str(entry).strip(), None
        if text:
            actions.append({"do": text, "secrets": allowed})
    hostnames = []
    for e in hosts:
        name = str(e.get("host", "")).strip() if isinstance(e, dict) else str(e).strip()
        if name:
            hostnames.append(name)
    if not hostnames:
        raise SystemExit(f"{path}: no 'hosts' listed")
    if not actions:
        raise SystemExit(f"{path}: no 'actions' listed")
    return {"hosts": hosts, "hostnames": hostnames, "actions": actions,
            "model": job.get("model"), "max_steps": job.get("max_steps")}


def vault_for_host(job: dict, host: str) -> Vault:
    for e in job.get("hosts") or []:
        if isinstance(e, dict) and e.get("host") == host and e.get("username"):
            return Vault({"login": {"username": e["username"],
                                    "password": e.get("password", "")}})
    return Vault()


# --------------------------------------------------------------------------
# Rendering an observation for the model
# --------------------------------------------------------------------------
def format_observation(data: dict) -> str:
    if not data.get("ok", True) and not data.get("elements"):
        return f"(could not read the screen: {data.get('error', 'unknown error')})"
    fg = data.get("foreground") or {}
    out = [f'Foreground: "{fg.get("title", "")}" [{fg.get("control_type", "")}] '
           f'app={fg.get("app", "") or "?"}']
    els = data.get("elements") or []
    if not els:
        out.append('Elements: none interactive. The app may still be loading - '
                   'use action "wait", or "launch" an app.')
        return "\n".join(out)
    out.append(f"Elements ({len(els)}):")
    for e in els:
        flags = []
        if not e.get("enabled", True):
            flags.append("DISABLED")
        if e.get("default"):
            flags.append("focused")
        v = e.get("value") or ""
        if v:
            flags.append(f'value="{v[:30]}"')
        tail = ("  " + " ".join(flags)) if flags else ""
        out.append(f'  [{e.get("id")}] {e.get("type", "")} "{e.get("name", "")}"{tail}')
    return "\n".join(out)


def to_helper_action(act: dict):
    """Map a model action onto a helper action, or (None, reason)."""
    a = act.get("action")
    if a == "invoke":
        i = act.get("id")
        if not isinstance(i, int) or i < 0:
            return None, "That invoke had no valid element id."
        return {"action": "invoke", "id": i}, None
    if a == "set_value":
        i = act.get("id")
        if not isinstance(i, int) or i < 0:
            return None, "That set_value had no valid element id."
        return {"action": "set_value", "id": i, "text": str(act.get("text", ""))}, None
    if a == "keys":
        if not act.get("keys"):
            return None, "That keys action had no keys."
        return {"action": "keys", "keys": str(act.get("keys"))}, None
    if a == "launch":
        if not act.get("app"):
            return None, "That launch had no app."
        return {"action": "launch", "app": str(act.get("app"))}, None
    if a == "click_xy":
        return {"action": "click_xy", "x": int(act.get("x", 0)), "y": int(act.get("y", 0))}, None
    return None, f"Unknown action {a!r}."


def prune_observations(messages: list) -> None:
    """Keep only the newest element dump; a small model loses the thread when the
    context fills with stale screens. Earlier observations keep their first line
    (the Result/Waited note) and drop the element list."""
    seen = False
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content", "")
        if "Foreground:" not in c and "Elements" not in c:
            continue
        if seen:
            head = c.split("\n\n", 1)[0]
            m["content"] = head + "\n\n[earlier screen omitted]"
        else:
            seen = True


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
async def ollama_chat(http, model: str, messages: list, num_ctx: int) -> str:
    r = await http.post(
        f"{OLLAMA}/api/chat",
        json={"model": model, "messages": messages, "stream": False,
              "format": ELEMENT_SCHEMA,
              "options": {"temperature": 0, "num_ctx": num_ctx}},
        timeout=600.0,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


# --------------------------------------------------------------------------
# Helper connection + hub (asyncio; N helpers concurrently)
# --------------------------------------------------------------------------
class HelperConn:
    def __init__(self, reader, writer, label, token):
        self.reader = reader
        self.writer = writer
        self.label = label
        self.token = token
        self.done = asyncio.Event()
        self._lock = asyncio.Lock()

    async def request(self, obj: dict) -> dict:
        obj = dict(obj)
        obj["token"] = self.token
        async with self._lock:
            self.writer.write(protocol.encode(obj))
            await self.writer.drain()
            line = await self.reader.readline()
        if not line:
            raise ConnectionError(f"helper {self.label!r} disconnected")
        return protocol.decode(line)

    async def observe(self, **kw) -> dict:
        req = {"op": "observe", "window": "foreground"}
        req.update(kw)
        return await self.request(req)

    async def act(self, a: dict) -> dict:
        return await self.request({"op": "act", "a": a})

    async def ping(self) -> dict:
        return await self.request({"op": "ping"})


class Hub:
    def __init__(self, token: str):
        self.token = token
        self.helpers: dict[str, HelperConn] = {}
        self._events: dict[str, asyncio.Event] = {}

    def _event(self, label: str) -> asyncio.Event:
        return self._events.setdefault(label, asyncio.Event())

    async def handle_conn(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=20)
        except (asyncio.TimeoutError, Exception):
            writer.close()
            return
        if not line:
            writer.close()
            return
        try:
            hello = protocol.decode(line)
        except Exception:
            writer.close()
            return
        if hello.get("type") != "hello" or hello.get("token") != self.token:
            print(f"[hub] REJECTED {peer[0]}: bad hello or token")
            try:
                writer.write(protocol.encode({"ok": False, "error": "unauthorized"}))
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return
        label = hello.get("host") or f"{peer[0]}:{peer[1]}"
        conn = HelperConn(reader, writer, label, self.token)
        self.helpers[label] = conn
        self._event(label).set()
        print(f"[hub] helper '{label}' connected from {peer[0]} (v{hello.get('version')})")
        try:
            await conn.done.wait()
        finally:
            try:
                writer.close()
            except Exception:
                pass
            if self.helpers.get(label) is conn:
                del self.helpers[label]
            self._events.pop(label, None)
            print(f"[hub] helper '{label}' released")

    async def wait_for(self, label: str, timeout: float):
        if label in self.helpers:
            return self.helpers[label]
        ev = self._event(label)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self.helpers.get(label)


# --------------------------------------------------------------------------
# The agent loop for one goal on one connected helper
# --------------------------------------------------------------------------
async def drive_goal(conn: HelperConn, args, host: str, goal: str,
                     vault: Vault, allowed, chat_fn) -> dict:
    def record(outcome, steps, detail="", transcript=None):
        return {"host": host, "goal": goal, "outcome": outcome, "steps": steps,
                "detail": detail, "transcript": transcript or []}

    try:
        data = await conn.observe()
    except Exception as e:
        return record("helper-error", 0, str(e)[:200])

    system = SYSTEM_PROMPT + (vault.describe(allowed) if vault else "")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"HOST: {host}\nGOAL: {goal}\n\n"
         f"Here is the screen now. Give your first action.\n\n{format_observation(data)}"},
    ]
    transcript, recent = [], []
    outcome, detail, used = "step-limit", "", 0

    for step in range(1, args.max_steps + 1):
        used = step
        try:
            raw = await chat_fn(messages, data)
        except Exception as e:
            return record("ollama-error", step, str(e)[:200], transcript)
        try:
            act = json.loads(raw)
        except json.JSONDecodeError:
            messages.append({"role": "user",
                             "content": "That was not valid JSON. Reply with ONE JSON object."})
            continue

        a = act.get("action")
        shown = {}
        _id = act.get("id")
        if isinstance(_id, int) and _id >= 0:
            shown["id"] = _id
        for k in ("text", "keys", "app", "secret"):
            if act.get(k):
                shown[k] = act[k]
        for k in ("x", "y", "seconds"):
            if act.get(k):
                shown[k] = act[k]
        print(f"[{host}][{step}] {a} {shown}  screen={str(act.get('screen',''))[:70]!r}")
        messages.append({"role": "assistant", "content": raw})
        transcript.append({"step": step, "action": act})

        if a in ("done", "fail"):
            outcome, detail = a, str(act.get("why", ""))
            print(f"[{host}] model reported {a}: {detail[:120]}")
            break

        key = (a, act.get("id"), act.get("text"), act.get("keys"), act.get("app"))
        recent.append(key)
        same = 1
        while same < len(recent) and recent[-1 - same] == key:
            same += 1
        if same >= 4:
            outcome, detail = "stuck", f"repeated {a} {same}x"
            print(f"[{host}] aborting: same action {same}x")
            break
        if same == 3:
            messages.append({"role": "user", "content":
                "STOP. You have sent that same action three times. Do something "
                "different, or use action \"fail\"."})
            continue

        if a == "wait":
            secs = min(int(act.get("seconds", 5) or 5), 30)
            await asyncio.sleep(secs)
            try:
                data = await conn.observe()
            except Exception as e:
                return record("helper-error", step, str(e)[:200], transcript)
            messages.append({"role": "user",
                             "content": f"Waited {secs}s.\n\n{format_observation(data)}"})
            continue

        secret_ref = ""
        if a == "set_secret":
            if vault is None or not vault.names():
                messages.append({"role": "user",
                                 "content": "No saved logins are configured. Use fail."})
                continue
            value, err = vault.resolve(act.get("secret", ""), allowed)
            if err:
                messages.append({"role": "user",
                                 "content": f"That saved login could not be used: {err}."})
                continue
            i = act.get("id")
            if not isinstance(i, int) or i < 0:
                messages.append({"role": "user",
                                 "content": "set_secret needs the id of the field to type into."})
                continue
            secret_ref = str(act.get("secret", "")).strip()
            print(f"      [audit] typing saved {secret_ref} on {host}")
            helper_action = {"action": "set_value", "id": i, "text": value}
        else:
            helper_action, err = to_helper_action(act)
            if err:
                messages.append({"role": "user", "content": f"{err} Use one of the listed actions."})
                continue

        try:
            res = await conn.act(helper_action)
        except Exception as e:
            return record("helper-error", step, str(e)[:200], transcript)

        if secret_ref:
            result_text = ("Typed the saved login (value hidden)." if res.get("ok")
                           else f"Could not type the saved login: {str(res.get('error',''))[:80]}")
        else:
            result_text = "ok" if res.get("ok") else f"error: {str(res.get('error',''))[:140]}"
        transcript[-1]["result"] = result_text

        try:
            data = await conn.observe()
        except Exception as e:
            return record("helper-error", step, str(e)[:200], transcript)
        prune_observations(messages)
        messages.append({"role": "user",
                         "content": f"Result: {result_text}\n\n{format_observation(data)}"})
    else:
        print(f"[{host}] hit the {args.max_steps}-step limit")

    return record(outcome, used, detail, transcript)


async def drive_host(hub: Hub, args, host: str, actions: list, job: dict, chat_fn) -> list:
    conn = await hub.wait_for(host, args.connect_timeout)
    if conn is None:
        print(f"[hub] no helper for '{host}' after {args.connect_timeout}s. "
              f"Check that its helper.exe dialed in with --host-label {host!r}.")
        return [{"host": host, "goal": a["do"], "outcome": "no-helper", "steps": 0,
                 "detail": "helper never dialed in", "transcript": []} for a in actions]
    results = []
    try:
        for a in actions:
            vault = vault_for_host(job, host)
            print(f"\n----- {host}: {a['do']} -----")
            if vault.names():
                print(f"      (may type: {'no login' if a.get('secrets') == [] else 'the host login'})")
            results.append(await drive_goal(conn, args, host, a["do"], vault,
                                            a.get("secrets"), chat_fn))
    finally:
        conn.done.set()
    return results


def print_summary(results: list) -> int:
    print("\n================ SUMMARY ================")
    ok = 0
    for r in results:
        mark = "OK  " if r["outcome"] == "done" else "FAIL"
        if r["outcome"] == "done":
            ok += 1
        print(f"  [{mark}] {r['host']:<18} {r['goal'][:44]:<44} {r['outcome']} ({r['steps']} steps)")
        if r["detail"] and r["outcome"] != "done":
            print(f"         {r['detail'][:110]}")
    print(f"  {ok}/{len(results)} completed")
    return ok


# --------------------------------------------------------------------------
# Normal run
# --------------------------------------------------------------------------
async def run(args) -> int:
    job = load_job(Path(args.job))
    if job.get("model"):
        args.model = job["model"]
    if job.get("max_steps"):
        args.max_steps = int(job["max_steps"])
    hosts = job["hostnames"]

    hub = Hub(args.token)
    server = await asyncio.start_server(hub.handle_conn, args.bind, args.port)
    where = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"[hub] listening on {where} (token-gated)")
    print(f"[job] {len(hosts)} host(s) x {len(job['actions'])} action(s), model {args.model}")
    print(f"[hub] waiting up to {args.connect_timeout}s for helpers to dial in; "
          f"each VM runs:\n      helper.exe --connect THIS_HOST:{args.port} "
          f"--token <token> --host-label <one of: {', '.join(hosts)}>")

    async with httpx.AsyncClient() as http:
        async def chat_fn(messages, data):
            return await ollama_chat(http, args.model, messages, args.num_ctx)
        tasks = [drive_host(hub, args, host, job["actions"], job, chat_fn) for host in hosts]
        nested = await asyncio.gather(*tasks)

    server.close()
    await server.wait_closed()
    results = [r for sub in nested for r in sub]
    ok = print_summary(results)
    if args.transcript:
        Path(args.transcript).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"[saved] {args.transcript}")
    return 0 if ok == len(results) else 1


# --------------------------------------------------------------------------
# Self-tests (no VM, no Ollama)
# --------------------------------------------------------------------------
def _mk(action, id=-1, text="", secret="", keys="", app="", x=0, y=0, seconds=0,
        why="", target="", screen=""):
    return json.dumps({"screen": screen, "target": target, "action": action,
                       "id": id, "text": text, "secret": secret, "keys": keys,
                       "app": app, "x": x, "y": y, "seconds": seconds, "why": why})


class _Args:
    max_steps = 8
    num_ctx = 8192
    model = "test"
    connect_timeout = 10


async def _fake_helper(host, port, token, label, script):
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(protocol.encode({"type": "hello", "token": token, "host": label,
                                  "version": "fake"}))
    await writer.drain()
    state = {}
    while True:
        line = await reader.readline()
        if not line:
            break
        req = protocol.decode(line)
        writer.write(protocol.encode(script(req, state)))
        await writer.drain()


def _activation_script(req, state):
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "pong": True}
    if op == "observe":
        if not state.get("invoked"):
            return {"ok": True,
                    "foreground": {"title": "Windows Activation", "control_type": "Window",
                                   "app": "SystemSettings.exe", "hwnd": 1},
                    "elements": [
                        {"id": 0, "type": "Button", "name": "Ask me later", "rect": [900, 500, 120, 30],
                         "enabled": True, "default": False, "value": ""},
                        {"id": 1, "type": "Button", "name": "Change product key", "rect": [900, 540, 160, 30],
                         "enabled": True, "default": False, "value": ""}]}
        return {"ok": True,
                "foreground": {"title": "Desktop", "control_type": "Pane",
                               "app": "explorer.exe", "hwnd": 2}, "elements": []}
    if op == "act":
        a = req.get("a") or {}
        if a.get("action") == "invoke" and a.get("id") == 0:
            state["invoked"] = True
        return {"ok": True}
    return {"ok": False, "error": "unknown op"}


async def _bad_token_rejected(host, port) -> bool:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(protocol.encode({"type": "hello", "token": "WRONG", "host": "x",
                                  "version": "fake"}))
    await writer.drain()
    line = await reader.readline()
    writer.close()
    if not line:
        return True  # closed without a frame
    try:
        return protocol.decode(line).get("error") == "unauthorized"
    except Exception:
        return False


async def _selftest() -> int:
    print("uia_agent self-test (fake helper + scripted model)\n")
    token = "T"
    hub = Hub(token)
    server = await asyncio.start_server(hub.handle_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fails = []

    helper_task = asyncio.create_task(
        _fake_helper("127.0.0.1", port, token, "testvm", _activation_script))

    steps = iter([
        _mk("invoke", id=0, target="Ask me later", screen="Windows Activation dialog is open"),
        _mk("done", why="the activation dialog is gone", screen="Desktop, no dialog"),
    ])

    async def chat(messages, data):
        return next(steps)

    conn = await hub.wait_for("testvm", timeout=5)
    if conn is None:
        print("  [FAIL] fake helper connected"); fails.append("connect")
    else:
        print("  [PASS] fake helper connected")
        res = await drive_goal(conn, _Args(), "testvm",
                               "Dismiss the Windows Activation dialog", Vault(), None, chat)
        conn.done.set()
        ok = res["outcome"] == "done"
        print(f"  [{'PASS' if ok else 'FAIL'}] loop drove goal to done (outcome={res['outcome']}, steps={res['steps']})")
        if not ok:
            fails.append("drive")

    bad = await _bad_token_rejected("127.0.0.1", port)
    print(f"  [{'PASS' if bad else 'FAIL'}] wrong-token helper rejected")
    if not bad:
        fails.append("token")

    # format_observation sanity
    txt = format_observation(_activation_script({"op": "observe"}, {}))
    fmt_ok = "Ask me later" in txt and "[0]" in txt
    print(f"  [{'PASS' if fmt_ok else 'FAIL'}] observation renders element ids for the model")
    if not fmt_ok:
        fails.append("format")

    helper_task.cancel()
    server.close()
    await server.wait_closed()
    print()
    print("SELF-TEST PASS" if not fails else f"SELF-TEST FAIL: {', '.join(fails)}")
    return 0 if not fails else 1


async def _localtest(args) -> int:
    """Real helper.py subprocess + real Notepad + scripted model. No Ollama."""
    import subprocess
    print("uia_agent local integration test (real helper subprocess + Notepad)\n")
    token = "T"
    hub = Hub(token)
    server = await asyncio.start_server(hub.handle_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    subprocess.Popen(["notepad.exe"])
    await asyncio.sleep(2.0)

    py = str(HELPER_DIR / ".venv-helper" / "Scripts" / "python.exe")
    if not Path(py).exists():
        py = sys.executable
    proc = subprocess.Popen([py, str(HELPER_DIR / "helper.py"),
                             "--connect", f"127.0.0.1:{port}",
                             "--token", token, "--host-label", "localvm"],
                            cwd=str(HELPER_DIR))
    fails = []
    conn = await hub.wait_for("localvm", timeout=20)
    if conn is None:
        print("  [FAIL] real helper dialed in"); fails.append("connect")
    else:
        print("  [PASS] real helper dialed in")
        picked = {"id": None}

        async def chat(messages, data):
            els = (data or {}).get("elements") or []
            if picked["id"] is None:
                f = next((e for e in els if e.get("name") == "File"
                          and e.get("type") in ("MenuItem", "Button")), None)
                if f is None:
                    return _mk("wait", seconds=1, screen="waiting for Notepad to show File menu")
                picked["id"] = f["id"]
                return _mk("invoke", id=f["id"], target="File menu",
                           screen="Notepad is open, File menu present")
            if not picked.get("closed"):
                picked["closed"] = True
                return _mk("keys", keys="esc", screen="File menu opened; closing it")
            return _mk("done", why="opened and closed the File menu", screen="menu closed")

        res = await drive_goal(conn, _Args(), "localvm",
                               "Open the File menu in Notepad", Vault(), None, chat)
        conn.done.set()
        saw_file = picked["id"] is not None
        ok = res["outcome"] == "done" and saw_file
        print(f"  [{'PASS' if saw_file else 'FAIL'}] observe (over the socket) listed Notepad's File menu")
        print(f"  [{'PASS' if ok else 'FAIL'}] invoke+keys over the socket drove goal to done "
              f"(outcome={res['outcome']}, steps={res['steps']})")
        if not ok:
            fails.append("drive")

    try:
        proc.terminate()
    except Exception:
        pass
    server.close()
    await server.wait_closed()
    print("\n(Notepad was left open; close it if you like.)")
    print("LOCALTEST PASS" if not fails else f"LOCALTEST FAIL: {', '.join(fails)}")
    return 0 if not fails else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="element-mode UIA controller (Phase 1/2)")
    p.add_argument("--job", default="", help="job.json (hosts + actions)")
    p.add_argument("--token", default=os.environ.get("WIN_RDP_HELPER_TOKEN", ""),
                   help="shared auth token helpers must present (or WIN_RDP_HELPER_TOKEN)")
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on for helpers")
    p.add_argument("--port", type=int, default=protocol.DEFAULT_PORT)
    p.add_argument("--model", default="qwen2.5vl:7b")
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--connect-timeout", type=float, default=120.0,
                   help="seconds to wait for a host's helper to dial in")
    p.add_argument("--transcript", default="", help="write a JSON transcript here")
    p.add_argument("--selftest", action="store_true", help="fake helper + scripted model")
    p.add_argument("--localtest", action="store_true", help="real helper subprocess + Notepad")
    args = p.parse_args(argv)

    if args.selftest:
        return asyncio.run(_selftest())
    if args.localtest:
        return asyncio.run(_localtest(args))
    if not args.job:
        p.error("--job is required (or use --selftest / --localtest)")
    if not args.token:
        p.error("--token is required (the channel is token-gated from day one)")
    load_job(Path(args.job))  # validate before entering async
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[interrupted] stopping")
        return 130


if __name__ == "__main__":
    sys.exit(main())
