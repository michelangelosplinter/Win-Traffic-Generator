# win-rdp — UIA fleet automation

Drive a fleet of Windows VMs from a single local vision model, reliably, by
reading the **Windows UI Automation (UIA) tree** instead of guessing pixels.

Each VM runs a small in-session helper that enumerates the on-screen elements
(buttons, edits, menus) with their exact rectangles. The controller hands that
list to the model; the model picks an element **by id**; the helper actuates it
**natively** (a UIA `Invoke`, not a synthetic click at a guessed coordinate).
The controller drives every VM in parallel.

## Why this exists

An earlier version screenshotted each VM and asked the model for pixel
coordinates. Measured against 4 domain VMs it scored **0/20**: the 7B model reads
each screen correctly (it names every dialog) but cannot convert "click that
button" into the right pixel. Spatial grounding is the bottleneck, and it lives
in the model's weights, so no amount of resolution or extra screenshots fixes it.

The fix is to remove the guess. UIA already knows where every control is, so the
model only has to choose from a named list. That is the whole design.

## Architecture

```
 CONTROLLER  (one box; in the VMs' network)          each TARGET VM  (unlocked desktop)
 +----------------------------------+                +----------------------------------+
 |  Ollama :11434  qwen2.5vl:7b     |                |  helper.exe                      |
 |      ^ element list   | one action                |    +- helper.py   (socket+token) |
 |      | (as text)      v (JSON)   |                 |    +- uia_core.py (Session)      |
 |  +----------------------------+  |  observe / act  |         observe()     act()      |
 |  | uia_agent.py               |  |<==============> |           |            |         |
 |  |  Hub: token, N parallel    |  |   protocol.py   |           v            v         |
 |  |  element loop + Vault      |  |  JSON / socket  |    UIA tree of the foreground    |
 |  +----------------------------+  |  (helper dials  |    app (buttons, edits, menus)   |
 |    reads job.json                |       OUT)      |    -> native invoke / click      |
 +----------------------------------+                +----------------------------------+

 Phase 0 (by hand, no controller/model):  poke.exe = poke.py + uia_core.py, run ON one VM.
```

Two design rules make this safe to run across a fleet:

- **Helpers dial OUT** to the controller, so a VM needs no inbound firewall rule.
- **A shared token gates the channel** from the first frame and on every request.

## Runtime flow

1. Start the controller: `uia_agent.py` loads `job.json`, opens the token-gated
   hub on `0.0.0.0:8765`, connects to Ollama, and waits for helpers.
2. Each VM runs `helper.exe --connect CONTROLLER:8765 --token T --host-label vm-1`.
   The helper sends a token-bearing `hello`; the hub registers it under its label.
3. **Observe** — the controller asks the helper for the foreground window's
   elements: `{id, type, name, rect, enabled, value}`.
4. **Think** — the controller renders that as a numbered list, adds it to the
   chat history, and calls Ollama with a schema-constrained format. The model
   replies with one action, e.g. `{"action":"invoke","id":5}`.
5. **Act** — the controller forwards the action; the helper runs the matching UIA
   pattern on the cached control (Invoke / Toggle / Select / Expand, or a click).
6. **Loop** — the controller re-observes and repeats until the model says
   `done`/`fail`, gets stuck, or hits the step limit.
7. Every connected VM runs steps 3–6 concurrently.

The model only ever sees element names and ids — never pixels, and never
credential values (`set_secret` substitutes a saved password at the last moment).

## Files

### Controller (runs on the controller box)

| File | Purpose |
|---|---|
| `uia_agent.py` | The element-mode controller. An asyncio **hub** accepts N helpers dialing in (token-gated, keyed by `--host-label`), runs the **Ollama loop** where the model picks an element by id, actuates via the helper, and drives all VMs in **parallel**. Contains the credential **Vault**, the `job.json` loader, and the `--selftest`/`--localtest` self-checks. Depends only on `httpx`. |
| `job.uia.example.json` | Template job file. Copy to `job.json`. Lists `hosts` (each `host` is a helper's `--host-label`, plus the in-session login for `set_secret`) and `actions` (the goals to run on every host, in order). |

### Helper (built into an exe, runs inside each VM)

| File | Purpose |
|---|---|
| `helper/uia_core.py` | The UIA engine, the "senses and hands." `Session.observe()` walks the UIA tree and returns named elements with ids and rects; `Session.act()` performs `invoke`/`set_value`/`keys`/`launch`/`click_xy`; `resolve_target()` picks a window by foreground/title/hwnd; `list_windows()` enumerates top-level windows. No network — a failure here is pure UIA. |
| `helper/poke.py` | The **Phase 0** by-hand CLI. Calls `uia_core` directly (`windows`/`observe`/`invoke`/`set-value`/`keys`/`launch`/`click-xy`). No model, no socket, so a result is unambiguous. Frozen to `poke.exe`. |
| `helper/helper.py` | The **Phase 2** in-VM agent. Wraps `uia_core.Session` and serves `observe`/`act` over the token-gated socket, dialing OUT to the controller and reconnecting on drop. Frozen to `helper.exe`. |
| `helper/protocol.py` | The wire format shared by `helper.py` and `uia_agent.py`: one newline-terminated JSON object per message. Kept in one file so the two ends cannot drift. (The controller imports this from `helper/`.) |

### Build, test, and docs

| File | Purpose |
|---|---|
| `helper/build_helper.ps1` | Freezes `poke.py` or `helper.py` into one self-contained `.exe` with PyInstaller. Pre-generates the comtypes `UIAutomationCore` wrapper first, or the frozen exe dies on its first UIA call. `-Script helper.py` builds `helper.exe`; the default builds `poke.exe`. |
| `helper/test_uia.py` | Controller-side smoke test of `uia_core` (key translation, window listing, and a launch→observe→act on Notepad). Proves the UIA stack works on a machine before you build the exe. |
| `helper/requirements-helper.txt` | Build/runtime dependencies for the helper: `uiautomation`, `comtypes`, `pillow`, `pyinstaller`. |
| `helper/README-helper.md` | Detailed docs for the helper subsystem: the Phase 0 recipe and full Phase 1/2 usage. |
| `helper/NETWORK.md` | How to put the controller and the VMs on one network (a VPC LAN as the primary plan, with a mesh-VPN fallback), including the one security-group rule. |
| `helper/dist/poke.exe`, `helper/dist/helper.exe` | The built binaries you copy to a VM (the VMs have no Python). Rebuild them with `build_helper.ps1`. |
| `.gitignore` | Keeps venvs, build output, and any `job.json` (which holds plaintext credentials) out of version control. |

### Environments (kept, regenerable)

| Path | Purpose |
|---|---|
| `.venv/` | The controller's virtualenv. Runs `uia_agent.py` (it only needs `httpx`). |
| `helper/.venv-helper/` | The build virtualenv (stable Python 3.11) used by `build_helper.ps1` to freeze the exes. Must not be a pre-release Python — `uiautomation`/`comtypes`/`pillow` have no wheels for one. |

## Quick start

**Build the helper exe** (on the controller; the VMs have no Python):

```powershell
cd helper
powershell -ExecutionPolicy Bypass -File .\build_helper.ps1 -Script helper.py
# -> helper\dist\helper.exe   (.\build_helper.ps1 with no -Script builds poke.exe)
```

**Verify the controller locally** (no VM, no Ollama needed):

```powershell
.\.venv\Scripts\python.exe uia_agent.py --selftest     # fake helper + scripted model
.\.venv\Scripts\python.exe uia_agent.py --localtest    # real helper subprocess + Notepad
```

**Run it against the VMs:**

```powershell
# 1) controller (Ollama must be serving the model here)
.\.venv\Scripts\python.exe uia_agent.py --job job.json --token <SHARED_TOKEN>

# 2) on each VM, with a label matching a host in job.json
helper.exe --connect <CONTROLLER>:8765 --token <SHARED_TOKEN> --host-label vm-1
```

See `helper/NETWORK.md` for making `<CONTROLLER>` reachable from the VMs.

## Phase status

| Phase | What | State |
|---|---|---|
| 0 | Prove UIA can see and dismiss a real dialog, by hand with `poke.exe`. | Built; run on a VM to confirm the activation dialog. |
| 1 | The model drives one VM by element id (`uia_agent.py`). | Built; verified locally by `--selftest` / `--localtest`. |
| 2 | Dial-out helper + token + all VMs in parallel. | Built; verified locally. Frozen `helper.exe` serves live UIA over the socket. |

The one thing not yet exercised end to end is the **real model** choosing actions
over this loop, which needs a VM with a connected helper plus Ollama — the live test.

## Security

The helper is a remote-control agent that runs **as the logged-in user**. The
shared token is the only gate today. UIA also requires a logged-in, **unlocked,
interactive** desktop — it cannot run as a session-0 service, and a locked or
disconnected RDP session makes the tree go blank. Treat this as **lab-only**
until it is hardened (mTLS, a controller allowlist, a signed binary). Only run it
on VMs you own and intend to automate.
