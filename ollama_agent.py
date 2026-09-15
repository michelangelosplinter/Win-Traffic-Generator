"""ollama_agent.py - drive the win-rdp MCP tools with a small LOCAL model on Ollama.

Ollama is not an MCP client, so this file is the host: it speaks MCP to
`server.py` on one side and Ollama's /api/chat on the other, and runs the
agent loop in between.

Everything here exists because the model is small and the machine is slow:

  * Screenshots are DOWNSCALED before the model sees them (vision cost scales
    with pixels, and on a CPU that dominates the step time), but the grid drawn
    on top is labelled in TRUE screen coordinates - so the model reads a number
    off the picture and uses it directly. Neither side does arithmetic.
  * Only the MOST RECENT screenshot is kept in the conversation. A small model
    given six 1280x800 images runs out of context and slows to a crawl, and the
    older pictures are actively misleading anyway.
  * The model does not use native tool-calling. It emits ONE flat JSON object,
    constrained by a JSON schema. Small models follow a flat schema far more
    reliably than a tool-call template, and this works on any model.

Usage
-----
    python ollama_agent.py --host 10.0.0.5 --goal "Open Notepad and type hello"
    python ollama_agent.py --selftest          # check MCP wiring, no model needed
    python ollama_agent.py --list-models       # what Ollama has locally
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ModuleNotFoundError:      # mcp 2.x ships httpx2, the successor package
    import httpx2 as httpx
from PIL import Image, ImageDraw
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

HERE = Path(__file__).resolve().parent
PYTHON = str(HERE / ".venv" / "Scripts" / "python.exe")
SERVER = str(HERE / "server.py")
OLLAMA = "http://127.0.0.1:11434"
JOB_FILE = HERE / "job.json"     # overridden by --job; see run()

# --------------------------------------------------------------------------
# The action protocol. Flat on purpose: one object, one action, no nesting.
# --------------------------------------------------------------------------
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "screen": {"type": "string",
                   "description": "what is actually visible on the screen right now"},
        "target": {"type": "string",
                   "description": "the exact on-screen thing being acted on, e.g. 'File menu'"},
        "action": {
            "type": "string",
            "enum": ["connect", "screenshot", "click", "double_click", "right_click",
                     "type", "type_secret", "press", "open_app", "wait",
                     "done", "fail"],
        },
        "secret": {"type": "string",
                   "description": "name of a saved login field, e.g. outlook.password"},
        "x": {"type": "integer", "description": "grid x, from the numbers on the image"},
        "y": {"type": "integer", "description": "grid y, from the numbers on the image"},
        "text": {"type": "string", "description": "text for the type action, else empty"},
        "keys": {"type": "string", "description": "key for the press action, else empty"},
        "app": {"type": "string", "description": "app name for open_app, else empty"},
        "seconds": {"type": "integer", "description": "seconds for wait, else 0"},
        "why": {"type": "string", "description": "one short sentence"},
    },
    # EVERY field is required. Constrained decoding only guarantees the fields
    # marked required, and a 3B model simply omits the optional ones - measured:
    # three out of three click actions arrived with no x/y at all. Forcing the
    # full object costs ~20 extra tokens (~1.4s) and makes clicks well-formed.
    "required": ["screen", "target", "action", "x", "y", "text", "keys", "app",
                 "seconds", "secret", "why"],
}

SYSTEM_PROMPT = """You control a Windows computer by looking at screenshots and clicking.

Every screenshot has a GRID drawn on it with numbers along the top and down the
left side. Those numbers ARE the coordinates. To click something, read the
nearest grid numbers and use them. Do not calculate anything.

Reply with ONE JSON object and nothing else. One action per reply.

ALWAYS fill in EVERY field. When a field does not apply, use 0 for numbers and
"" for text. Never leave a field out.

"screen" = what you can SEE right now. Not what you want to do.
"target" = the thing on screen you are acting on, e.g. "File menu", "Best match".

Actions:
  connect                      ONLY if a tool says the session is gone
  screenshot                   look again
  open_app   + app             open the Start menu and search for an app
  click      + x,y             left click
  double_click + x,y           open a desktop icon
  right_click + x,y            context menu
  type       + text            type into whatever was last clicked
  type_secret + secret       type a saved password or username you cannot see
  press      + keys            enter, esc, tab, down, ctrl+a
  wait       + seconds         an app is still starting
  done                         the goal is finished
  fail                         you are stuck

There is deliberately no worked example here to copy. Your reply is forced into
the correct shape automatically, so an example would only give you words to
repeat. Every value must come from the picture in front of you.

Before each action, LOOK at the newest picture and write what is actually on it
in "screen". If the picture shows a window that was not there before, say so.
Repeating your previous "screen" text is the most common way to fail this task.

For a click, x and y must come from the yellow grid labels on that picture. If
you did not read them off the picture, they are wrong.

The taskbar is along the very bottom of the screen, near the largest y label.
The Start button is at the bottom-LEFT corner.

RULES
1. FIRST, CHECK WHETHER YOU ARE ALREADY DONE. Look at the newest picture. If it
   already shows the GOAL achieved, reply with action "done" straight away.
   Never repeat an action that has already worked - if you asked for text to be
   typed and you can now see that text on the screen, you are DONE.
2. Look at the newest screenshot before every action. Describe it in "screen".
3. Click a text box BEFORE typing into it.
4. To open an app: open_app, then look, then click the highlighted top result,
   then wait 10, then look again. Apps take 5-30 seconds to appear.
5. If the screen looks the same as before, use wait - do NOT repeat the click.
   Clicking twice opens the app twice.
6. To fill in a login, click the box first, then use type_secret, for example
   {"action":"type_secret","secret":"login.password"}. "login" is the account
   for the computer you are working on. You never see the value and must never
   try to guess or retype it. If no saved login is listed for you, use fail.
7. Never use the Windows key. It does not work.
8. Only plain English letters, numbers and symbols can be typed.
9. Only sign in where the GOAL told you to, using a saved login. Never accept
   terms, buy anything, change settings, install, or delete. Never type a saved
   login into a web page or app the GOAL did not name - if the screen asks for
   one anywhere else, use fail and say where.
"""


# --------------------------------------------------------------------------
# Screenshot preparation
# --------------------------------------------------------------------------
def prepare_image(png: bytes, scale: float, grid: int) -> tuple[str, int, int]:
    """Downscale the screenshot and draw a coordinate grid labelled in TRUE
    screen pixels. Returns (base64 jpeg, true_width, true_height).

    The model reads a label off the image and passes that number straight back
    as x/y, so the labels must stay in the server's coordinate space even though
    the image itself is smaller."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    true_w, true_h = img.size
    if scale != 1.0:
        img = img.resize((max(1, int(true_w * scale)), max(1, int(true_h * scale))),
                         Image.LANCZOS)
    sx, sy = img.width / true_w, img.height / true_h
    draw = ImageDraw.Draw(img, "RGBA")

    for tx in range(0, true_w, grid):
        px = int(tx * sx)
        draw.line([(px, 0), (px, img.height)], fill=(255, 0, 0, 70), width=1)
    for ty in range(0, true_h, grid):
        py = int(ty * sy)
        draw.line([(0, py), (img.width, py)], fill=(255, 0, 0, 70), width=1)

    # Label EVERY gridline. Labelling every other one left a 200px unlabelled
    # gap, and measurably both models missed targets that fell inside it: the
    # taskbar Start button at y=780 sits between the 600 label and the bottom
    # edge, and 3b/7b guessed y=128 and y=700 for it.
    for tx in range(0, true_w, grid):
        px = min(int(tx * sx), img.width - 26)
        draw.rectangle([px, 0, px + 25, 11], fill=(0, 0, 0, 200))
        draw.text((px + 2, 1), str(tx), fill=(255, 255, 0))
    for ty in range(0, true_h, grid):
        py = min(int(ty * sy), img.height - 12)
        draw.rectangle([0, py, 25, py + 11], fill=(0, 0, 0, 200))
        draw.text((2, py + 1), str(ty), fill=(255, 255, 0))
    # Mark the bottom edge too, since the taskbar lives there and the last
    # gridline label is a whole grid step above it. Anchored bottom-RIGHT: the
    # bottom-LEFT is the Start button, and a label there would cover it.
    by = img.height - 12
    draw.rectangle([img.width - 34, by, img.width, img.height], fill=(0, 0, 0, 200))
    draw.text((img.width - 32, by + 1), str(true_h), fill=(255, 255, 0))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode(), true_w, true_h


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
async def ollama_chat(client: httpx.AsyncClient, model: str, messages: list,
                      num_ctx: int) -> str:
    r = await client.post(
        f"{OLLAMA}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "format": ACTION_SCHEMA,
            "options": {"temperature": 0, "num_ctx": num_ctx},
        },
        timeout=600.0,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


def prune_images(messages: list) -> None:
    """Strip images from every message but the last one that has any.

    Without this the context grows by a full image per step: the model slows
    down, then truncates, then starts answering about a stale screen."""
    seen_latest = False
    for m in reversed(messages):
        if m.get("images"):
            if seen_latest:
                m.pop("images", None)
                m["content"] = m.get("content", "") + " [older screenshot removed]"
            else:
                seen_latest = True


# --------------------------------------------------------------------------
# MCP plumbing
# --------------------------------------------------------------------------
def split_result(result) -> tuple[str, bytes | None]:
    """Return (text, newest png bytes) from an MCP tool result."""
    text, png = [], None
    for block in (getattr(result, "content", None) or []):
        kind = getattr(block, "type", "")
        if kind == "text":
            text.append(block.text)
        elif kind == "image":
            png = base64.b64decode(block.data)
    return " ".join(text).strip(), png


def action_to_call(act: dict, host: str, has_wait: bool):
    """Map one model action onto an MCP tool call, or None to stop."""
    a = act.get("action")
    if a in ("done", "fail"):
        return None
    if a == "connect":
        return "rdp_connect", {"host": host}
    if a == "screenshot":
        return "remote_screenshot", {"host": host}
    if a == "open_app":
        return "remote_open_app", {"host": host, "name": act.get("app", "")}
    if a in ("click", "double_click", "right_click"):
        args = {"host": host, "x": int(act.get("x", 0)), "y": int(act.get("y", 0))}
        if a == "double_click":
            args["double"] = True
        if a == "right_click":
            args["button"] = "right"
        return "remote_click", args
    if a == "type":
        return "remote_type", {"host": host, "text": act.get("text", "")}
    if a == "press":
        return "remote_press", {"host": host, "keys": act.get("keys", "")}
    if a == "wait":
        secs = int(act.get("seconds", 5))
        if has_wait:
            return "remote_wait", {"host": host, "seconds": secs}
        time.sleep(min(secs, 30))          # older build: sleep here, then look
        return "remote_screenshot", {"host": host}
    raise ValueError(f"unknown action {a!r}")

# --------------------------------------------------------------------------
# Credential vault
# --------------------------------------------------------------------------
class Vault:
    """Named logins the model may USE but never SEE.

    The model emits a reference - "outlook.password" - and this class swaps in
    the real value on the way to the keyboard. The value never enters the
    conversation, the transcript, or the console, so it cannot be read back out
    of the model, and a screen full of hostile text cannot talk the model into
    reciting it.

    What this does NOT protect against: the model typing the right password into
    the wrong box. Scope each action with "secrets" in the job file to limit the
    blast radius, and see the warning in INSTALL.md.
    """

    def __init__(self, entries: dict | None = None):
        self.entries = entries or {}

    def names(self) -> list[str]:
        return sorted(self.entries)

    def describe(self, allowed: list[str] | None = None) -> str:
        """The text the model sees: which logins exist, never their passwords."""
        keys = [k for k in self.names() if allowed is None or k in allowed]
        if not keys:
            return ""
        lines = ["", "SAVED LOGINS you can use with type_secret.",
                 "You cannot see the passwords. Use the name exactly as written:"]
        for k in keys:
            user = self.entries[k].get("username", "")
            lines.append(f'  "{k}.username"   (the account is {user})' if user
                         else f'  "{k}.username"')
            lines.append(f'  "{k}.password"   (hidden)')
        return "\n".join(lines)

    def resolve(self, ref: str, allowed: list[str] | None) -> tuple[str, str]:
        """Return (value, error). Never logs or returns the value on error."""
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


def redact_secret_result(text: str, secret_ref: str) -> tuple[str, bool]:
    """Turn a remote_type echo into something safe to hand back to the model.

    Returns (safe_text, typed_ok). Two things must both hold:
      * the value must not survive - remote_type echoes a preview of what it
        typed, and on failure it also names the exact characters it could not
        send, which for a password ARE part of the password;
      * the failure must still be visible, or the model would believe a login
        succeeded when the field actually holds a mangled string.
    """
    if text.startswith("PARTIALLY TYPED"):
        return (f"WARNING: the saved {secret_ref} could NOT be typed in full. It "
                f"contains characters that do not exist on this host's keyboard "
                f"layout, so the field is now wrong. Do not retry and do not guess "
                f"it: clear the field and use fail.", False)
    return f"Typed the saved {secret_ref} (value hidden).", True


# --------------------------------------------------------------------------
# Job files: which hosts to drive, and what to do on each
# --------------------------------------------------------------------------
def load_job(path: Path) -> dict:
    """Read a job file. See job.example.json for the shape."""
    job = json.loads(path.read_text(encoding="utf-8"))
    hosts = [h for h in (job.get("hosts") or []) if h]

    # An action is either a plain string, or {"do": ..., "secrets": [...]} to
    # limit which saved logins that step may use (least privilege).
    actions = []
    for raw in (job.get("actions") or []):
        if isinstance(raw, dict):
            text = str(raw.get("do", "")).strip()
            allowed = raw.get("secrets")
            allowed = list(allowed) if isinstance(allowed, list) else None
        else:
            text, allowed = str(raw).strip(), None
        if text:
            actions.append({"do": text, "secrets": allowed})

    if not hosts:
        raise SystemExit(f"{path}: no 'hosts' listed")
    if not actions:
        raise SystemExit(f"{path}: no 'actions' listed")
    if job.get("credentials"):
        print(f"[job] note: '{path.name}' has a 'credentials' block, which is no "
              f"longer used. Each host's own login is what gets typed inside its "
              f"session. Move those values into the matching host entry.")
    return {"hosts": hosts, "actions": actions,
            "model": job.get("model"), "max_steps": job.get("max_steps")}


def plan_hosts(hosts: list) -> list[str]:
    """Host names in job-file order, warning about any with no usable login.

    Nothing is written anywhere: the server reads the same job file, so the
    login exists in exactly one place.
    """
    names, missing = [], []
    for entry in hosts:
        if isinstance(entry, dict):
            name = str(entry.get("host", "")).strip()
            if name and not entry.get("username"):
                missing.append(name)
        else:
            name = str(entry).strip()
            if name:
                missing.append(name)
        if name:
            names.append(name)
    if missing:
        print(f"[job] warning: no username/password for {', '.join(missing)} - "
              f"connecting to those will fail")
    return names


def host_login(host: str) -> dict | None:
    """The login for one host, read fresh from the job file."""
    if not JOB_FILE.exists():
        return None
    try:
        job = json.loads(JOB_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    for entry in job.get("hosts") or []:
        if isinstance(entry, dict) and entry.get("host") == host and entry.get("username"):
            return {"username": entry["username"], "password": entry.get("password", "")}
    return None


def vault_for(host: str) -> "Vault":
    """The logins typeable while working on `host` - only that host's own.

    Building this per host is what makes cross-host credential use impossible:
    while driving 10.0.0.5 there is simply no entry holding 10.0.0.6's password.
    """
    login = host_login(host)
    return Vault({"login": login}) if login else Vault()


# --------------------------------------------------------------------------
# Driving one goal on one already-connected host
# --------------------------------------------------------------------------
async def drive_goal(mcp, http, args, host: str, goal: str, has_wait: bool,
                     vault: "Vault | None" = None,
                     allowed: list | None = None) -> dict:
    """Run the agent loop until the model reports done/fail, gets stuck, or runs
    out of steps. Returns a result record."""
    text, png = split_result(await mcp.call_tool("remote_screenshot", {"host": host}))
    if png is None:
        return {"host": host, "goal": goal, "outcome": "no-screenshot",
                "steps": 0, "detail": text, "transcript": []}

    b64, tw, th = prepare_image(png, args.scale, args.grid)
    # The vault description lists WHICH logins exist, never their values.
    system = SYSTEM_PROMPT + (vault.describe(allowed) if vault else "")
    messages = [
        {"role": "system", "content": system},
        {"role": "user",
         "content": (f"Host: {host}\nGOAL: {goal}\n\n"
                     f"You are already connected. Here is the screen now. "
                     f"The grid numbers are the real coordinates "
                     f"(screen is {tw}x{th}). Give your first action."),
         "images": [b64]},
    ]
    transcript, recent = [], []
    outcome, detail, used = "step-limit", "", 0

    for step in range(1, args.max_steps + 1):
        used = step
        prune_images(messages)
        t0 = time.time()
        try:
            raw = await ollama_chat(http, args.model, messages, args.num_ctx)
        except Exception as e:
            return {"host": host, "goal": goal, "outcome": "ollama-error",
                    "steps": step, "detail": str(e)[:200], "transcript": transcript}
        think = time.time() - t0

        try:
            act = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[{step}] model returned non-JSON: {raw[:160]}")
            messages.append({"role": "user",
                             "content": "That was not valid JSON. Reply with ONE JSON object."})
            continue

        a = act.get("action")
        shown = {k: v for k, v in act.items()
                 if k in ("x", "y", "text", "keys", "app", "seconds") and v not in ("", 0)}
        print(f"[{step}] ({think:5.1f}s) {a} {shown}")
        print(f"      screen: {str(act.get('screen', ''))[:100]}")
        transcript.append({"step": step, "think_s": round(think, 1), "action": act})
        messages.append({"role": "assistant", "content": raw})

        if a in ("done", "fail"):
            outcome, detail = a, str(act.get("why", ""))
            print(f"[end] model reported {a}: {detail[:160]}")
            break

        # Stuck-loop guard. A small model that hits an error re-sends the
        # identical action forever rather than changing course - measured: 14
        # identical open_app calls in a row. Escalate, then abort.
        key = (a, act.get("x"), act.get("y"), act.get("text"),
               act.get("keys"), act.get("app"))
        recent.append(key)
        same = 1
        while same < len(recent) and recent[-1 - same] == key:
            same += 1
        if same >= 4:
            outcome = "stuck"
            detail = f"repeated {a} {same}x"
            print(f"[end] aborting: same action {same}x, no progress")
            break
        if same == 3:
            print("      !! same action 3x - telling the model to change course")
            messages.append({"role": "user", "content":
                "STOP. You have now sent that exact action three times and it is "
                "not working. Do something different: take a screenshot and look "
                "at what is actually on the screen, or use the fail action."})
            continue

        # A click with no usable coordinates is the most common small-model
        # failure. Never turn it into a click at (0,0).
        if a in ("click", "double_click", "right_click"):
            x, y = act.get("x"), act.get("y")
            if not isinstance(x, int) or not isinstance(y, int) or (x == 0 and y == 0):
                tgt = act.get("target") or "the thing you want to click"
                print("      !! click had no coordinates - asking again")
                messages.append({"role": "user", "content":
                    f"That click had no usable coordinates. Look at the yellow "
                    f"numbers on the grid and reply again with x and y set to the "
                    f"position of {tgt}."})
                continue

        # A saved login is resolved HERE, at the last possible moment. The value
        # goes straight to the keyboard and is never printed, stored, or sent
        # back to the model.
        secret_ref = ""
        if a == "type_secret":
            if vault is None:
                messages.append({"role": "user", "content":
                    "There are no saved logins configured. Use fail."})
                continue
            value, err = vault.resolve(act.get("secret", ""), allowed)
            if err:
                print(f"      !! secret refused: {err}")
                messages.append({"role": "user",
                                 "content": f"That saved login could not be used: {err}."})
                continue
            secret_ref = str(act.get("secret", "")).strip()
            print(f"      [audit] typing saved {secret_ref} on {host}")
            tool, targs = "remote_type", {"host": host, "text": value}
        else:
            try:
                tool, targs = action_to_call(act, host, has_wait)
            except ValueError as e:
                messages.append({"role": "user",
                                 "content": f"{e}. Use one of the listed actions."})
                continue

        t1 = time.time()
        result = await mcp.call_tool(tool, targs)
        text, png = split_result(result)
        if secret_ref:
            text, typed_ok = redact_secret_result(text, secret_ref)
            if not typed_ok:
                print(f"      !! {secret_ref} is not typeable on this host's layout")
        print(f"      -> {tool} [{time.time() - t1:.1f}s] {text[:130]}")
        transcript[-1]["result"] = text

        msg = {"role": "user", "content": f"Result: {text}\nHere is the new screen."}
        if png:
            b64, tw, th = prepare_image(png, args.scale, args.grid)
            msg["images"] = [b64]
            msg["content"] += (f" The grid numbers are the real coordinates "
                               f"(screen is {tw}x{th}).")
        messages.append(msg)
    else:
        print(f"[end] hit the {args.max_steps}-step limit")

    return {"host": host, "goal": goal, "outcome": outcome, "steps": used,
            "detail": detail, "transcript": transcript}


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
async def run(args) -> int:
    # The server is a separate process and the MCP tools carry only a hostname,
    # so it has to read the logins itself. Point it at the same job file rather
    # than keeping a second copy of the credentials on disk.
    global JOB_FILE
    JOB_FILE = (Path(args.job) if args.job else HERE / "job.json").resolve()
    env = dict(os.environ)
    env["WIN_RDP_JOB"] = str(JOB_FILE)
    # So the server can exit on its own if we are killed without cleanup.
    env["WIN_RDP_PARENT_PID"] = str(os.getpid())
    params = StdioServerParameters(command=PYTHON, args=[SERVER], cwd=str(HERE), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = [t.name for t in (await mcp.list_tools()).tools]
            has_wait = "remote_wait" in tools
            print(f"[mcp] {len(tools)} tools: {', '.join(tools)}")
            if not has_wait:
                print("[mcp] note: this build has no remote_wait; waiting locally instead")

            if args.selftest:
                text, _ = split_result(await mcp.call_tool("rdp_status", {}))
                print(f"[selftest] rdp_status -> {text}")
                print("[selftest] MCP wiring OK")
                return 0

            # Build the plan: (host, goal) pairs, in order.
            if args.job:
                job = load_job(Path(args.job))
                hosts = plan_hosts(job["hosts"])
                if job.get("model"):
                    args.model = job["model"]
                if job.get("max_steps"):
                    args.max_steps = int(job["max_steps"])
                plan = [(h, a) for h in hosts for a in job["actions"]]
                print(f"[job] {len(hosts)} host(s) x {len(job['actions'])} action(s) "
                      f"= {len(plan)} run(s), model {args.model}")
            else:
                plan = [(args.host, {"do": args.goal, "secrets": None})]

            results = []
            async with httpx.AsyncClient() as http:
                connected = None
                for host, action in plan:
                    goal, allowed = action["do"], action.get("secrets")
                    # Connect UP FRONT, once per host. Session bootstrap is pure
                    # overhead for a small model and it measurably cannot do it:
                    # told to start with a connect action, qwen2.5vl:7b went
                    # straight to open_app and then re-sent that same failing
                    # action for every remaining step.
                    if host != connected:
                        print(f"\n===== connecting to {host} =====")
                        text, png = split_result(
                            await mcp.call_tool("rdp_connect", {"host": host}))
                        print(f"[mcp] {text}")
                        if png is None:
                            print(f"[mcp] cannot reach {host} - skipping its actions")
                            results.append({"host": host, "goal": goal,
                                            "outcome": "connect-failed", "steps": 0,
                                            "detail": text[:160], "transcript": []})
                            continue
                        connected = host

                    # Only this host's own login is reachable from this action.
                    vault = vault_for(host)
                    print(f"\n----- {host}: {goal} -----")
                    if vault.names():
                        may = "no login" if allowed == [] else f"the {host} login"
                        print(f"      (may type: {may})")
                    results.append(await drive_goal(mcp, http, args, host, goal,
                                                    has_wait, vault, allowed))

    # ---- summary -------------------------------------------------------
    print("\n================ SUMMARY ================")
    ok = 0
    for r in results:
        mark = "OK  " if r["outcome"] == "done" else "FAIL"
        if r["outcome"] == "done":
            ok += 1
        print(f"  [{mark}] {r['host']:<16} {r['goal'][:46]:<46} "
              f"{r['outcome']} ({r['steps']} steps)")
        if r["detail"] and r["outcome"] != "done":
            print(f"         {r['detail'][:110]}")
    print(f"  {ok}/{len(results)} completed")

    if args.transcript:
        Path(args.transcript).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"[saved] {args.transcript}")
    return 0 if ok == len(results) else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Drive win-rdp with a local Ollama model",
        epilog="Use --job for several hosts and/or several actions; "
               "--host/--goal is the one-off form.")
    p.add_argument("--job", default="",
                   help="JSON file listing hosts (with credentials) and actions "
                        "to run on each; see job.example.json")
    p.add_argument("--host", default="", help="remote host to control")
    p.add_argument("--goal", default="", help="what the model should accomplish")
    p.add_argument("--model", default="qwen2.5vl:7b",
                   help="Ollama model tag; 3b cannot ground reliably, see README.md")
    p.add_argument("--scale", type=float, default=0.75,
                   help="downscale factor for screenshots sent to the model")
    p.add_argument("--grid", type=int, default=50,
                   help="grid spacing in true pixels; 50 measurably beats 100 "
                        "at identical token cost")
    p.add_argument("--max-steps", type=int, default=20,
                   help="per action, so a confused model cannot loop forever")
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--transcript", default="", help="write a JSON transcript here")
    p.add_argument("--selftest", action="store_true", help="check MCP wiring only")
    p.add_argument("--list-models", action="store_true")
    args = p.parse_args()

    if args.list_models:
        print(httpx.get(f"{OLLAMA}/api/tags", timeout=10).text)
        return 0
    if not args.selftest and not args.job:
        if not args.host:
            p.error("--host is required (or use --job, or --selftest)")
        if not args.goal:
            p.error("--goal is required (or use --job, or --selftest)")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        # Let the context managers unwind so the server is torn down cleanly;
        # its watchdog is the backstop if we are killed outright instead.
        print()
        print("[interrupted] stopping")
        return 130


if __name__ == "__main__":
    sys.exit(main())
