# win-rdp helper — Phase 0

The reliable path for driving the VMs. Instead of asking a small vision model to
guess a pixel, a helper running **inside** each VM's interactive session reads
the Windows **UI Automation (UIA)** tree and hands the model a list of named
elements with exact rectangles. The model picks one by id; the helper clicks it
exactly. The guessing step is deleted.

Phase 0 proves the one thing the whole approach rests on: **can UIA see and
dismiss a real Windows dialog** (the Windows Activation nag) on a VM. If yes, the
0/20 pixel result is behind us. If UIA is blind to something, we learn it here,
cheaply, before building anything else.

There is **no network, no model, and no controller** in Phase 0 — just `poke.exe`
run by hand on one VM. That makes a result unambiguous.

## Files

| File | What it is |
|---|---|
| `uia_core.py` | The UIA engine: `observe()` (enumerate elements) + `act()` (invoke/set_value/keys/launch/click_xy). No network. |
| `poke.py` | The Phase 0 CLI that drives `uia_core` by hand. Frozen to `poke.exe`. |
| `test_uia.py` | A local smoke test — run on the controller to confirm UIA works on this machine before building the exe. |
| `build_helper.ps1` | Freezes `poke.py` → `dist\poke.exe` (handles the comtypes freezing trap). |
| `requirements-helper.txt` | Build-venv dependencies. |

## 1. One-time setup (on the controller / build box)

Use a **stable Python 3.10–3.13**. Not 3.14 — `uiautomation`, `comtypes` and
`pillow` ship no wheels for a pre-release, exactly the trap the pixel path hit.
On this box `py -3` resolves to a 3.14 alpha, so pin 3.11 explicitly:

```powershell
cd C:\Users\zeerv\win-rdp-mcp\helper
py -3.11 -m venv .venv-helper
.\.venv-helper\Scripts\python.exe -m pip install -r requirements-helper.txt
```

(The venv already exists from setup; this is here for a clean rebuild.)

## 2. Prove the plumbing locally (no VM needed)

```powershell
.\.venv-helper\Scripts\python.exe test_uia.py
```

Expect `RESULT: PASS`. It briefly opens and closes Notepad. If this fails, the
exe will too — fix it here first.

## 3. Build the exe

```powershell
powershell -ExecutionPolicy Bypass -File .\build_helper.ps1
```

Output is `dist\poke.exe` (~15–25 MB, self-contained). The script pre-generates
the comtypes UIAutomationCore wrapper and bundles it — without that step a frozen
UIA app dies on its first call.

Smoke-test the exe locally before copying it anywhere:

```powershell
.\dist\poke.exe windows
.\dist\poke.exe launch notepad
.\dist\poke.exe observe --title Notepad
```

## 4. Phase 0 acceptance — on ONE VM

RDP into one VM (e.g. `54.81.98.104`, Administrator) and **stay connected and
unlocked** — UIA goes blind on a locked or disconnected session. Copy
`dist\poke.exe` to the VM (e.g. `C:\poke.exe`). Make the Windows Activation nag
appear if it isn't already (it usually is on an unactivated box). Then, in a
Command Prompt or PowerShell on the VM:

```
1.  poke.exe windows
        -> find the activation dialog in the list; note its hwnd, e.g. 197612

2.  poke.exe observe --hwnd 197612
        -> lists its buttons with ids + rects, e.g.
           [  0] Button (980,540 120x30) "Ask me later"  enabled
           [  1] Hyperlink ...            "Change product key"

3.  poke.exe invoke 0 --hwnd 197612
        -> OK: invoke id=0   ... and the dialog closes

4.  poke.exe windows
        -> confirm the activation dialog is gone
```

If the dialog isn't easy to address by hwnd, `poke.exe observe --title "Activat"`
works too (any substring of its title). If it has no title, use `poke.exe
windows` to get the hwnd.

### What to paste back to me

The full console output of steps 1–4 (especially step 2's element list and step
3's OK/FAILED line). That single result validates the whole approach. If UIA
returns **no** elements for the dialog, run `poke.exe observe --hwnd <H> --all`
so we can see whether it's a blind spot or just a filter gap — that's exactly the
kind of thing Phase 0 exists to surface.

## Phase 1 & 2 — the controller (`uia_agent.py`)

Phase 0 is by-hand `poke.exe`. Phases 1–2 add the model and the fleet:

- **`helper.py` → `helper.exe`** — the in-VM agent. Same UIA engine as `poke`,
  but it **dials out** to the controller over a token-gated socket and serves
  `observe`/`act`, reconnecting if the link drops.
- **`../uia_agent.py`** — the controller. It listens for helpers, and for each
  `(host, goal)` runs an Ollama loop where every turn hands the model the element
  list and it replies with one action (`invoke` an id, `set_value`, `keys`,
  `launch`, `set_secret`, or `click_xy` as a last-resort pixel fallback). It
  drives every connected VM **in parallel**. It depends only on `httpx`, so it
  runs on a lean controller with no mstsc/pixel stack. `click_xy` stays as the
  in-protocol last-resort fallback for anything with no addressable element.

### Build the helper exe

```powershell
powershell -ExecutionPolicy Bypass -File .\build_helper.ps1 -Script helper.py
# -> dist\helper.exe
```

### Verify the controller locally (no VM, no Ollama)

From the repo root, with the main `.venv` (it has `httpx`):

```powershell
.\.venv\Scripts\python.exe uia_agent.py --selftest    # fake helper + scripted model
.\.venv\Scripts\python.exe uia_agent.py --localtest    # real helper subprocess + Notepad
```

Both should print `PASS`. `--localtest` proves the real helper serves live UIA
over the socket and the loop drives a goal to `done`.

### Run it against the VMs

1. Start the controller (Ollama must be serving the model on the controller):
   ```powershell
   .\.venv\Scripts\python.exe uia_agent.py --job job.json --token <SHARED_TOKEN>
   ```
   It listens on `0.0.0.0:8765` and waits for helpers.
2. On each VM, dial in with a label that matches a `host` in the job:
   ```powershell
   helper.exe --connect <CONTROLLER>:8765 --token <SHARED_TOKEN> --host-label vm-1
   ```
3. See `../job.uia.example.json` for the element-mode job format (the `host`
   field is the **label**, not an address; `username`/`password` feed
   `set_secret` for in-app sign-in).

The controller and VMs reaching each other is a network question, not a code
one: the plan is a **VPC LAN** (controller in the same VPC, helpers dial its
private IP — no NAT, no VPN). See **NETWORK.md** for the one security-group rule,
plus a mesh-VPN fallback if the controller lives outside AWS.

The one thing not yet exercised end to end is the **real model** picking actions;
that needs a VM with a connected helper and Ollama, which is the live test.

## Security (say it plainly)

`poke.exe` / the helper is a **remote-control agent that runs as the logged-in
user**. Phase 0 is local-only (no network), so the exposure is just "a tool that
can click things is on the box". From Phase 2 it will dial out to the controller,
gated by a shared token from the first commit. Until it's hardened (mTLS, a
controller allowlist, a signed binary) this is **lab-only** — only run it on VMs
you own and intend to automate.
