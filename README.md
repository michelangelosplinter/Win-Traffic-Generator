# win-rdp-mcp

Drive a remote Windows desktop from a **local model** over nothing but RDP
(port 3389). The model sees screenshots and acts in pixels. **Nothing is
installed on the remote host.**

Everything runs on your own hardware — the model, the agent loop and the RDP
client. Only RDP leaves the machine.

- [Why it works this way](#why-it-works-this-way)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [Configuration](#configuration)
- [Running it](#running-it)
- [Tool reference](#tool-reference)
- [Credentials](#credentials)
- [Writing actions a small model can follow](#writing-actions-a-small-model-can-follow)
- [What we measured](#what-we-measured)
- [Bugs found and fixed](#bugs-found-and-fixed)
- [Troubleshooting](#troubleshooting)
- [Limitations and known issues](#limitations-and-known-issues)
- [Repo layout](#repo-layout)
- [Security](#security)

---

## Why it works this way

The host this was built against had **only port 3389 open** — WinRM, SSH and
every custom agent port were firewalled. That rules out the usual "install an
agent on the target and talk to it over a socket" approach entirely.

So the server drives the RDP **viewer window** on your own machine: it launches
Windows' built-in `mstsc.exe`, screenshots that window to see, and injects
synthetic mouse and keyboard input to act. The remote host only ever sees a
normal RDP logon.

**The consequence to understand:** because input goes through the local viewer
window, that window must hold the local foreground while the server works — your
real mouse pointer moves. The machine running this should be left alone during a
run.

Ollama is **not** an MCP client, so `ollama_agent.py` is the host that bridges
the two. It is also where the scaffolding a small model needs lives: a
coordinate grid drawn onto each screenshot, screenshot pruning,
schema-constrained actions, a stuck-loop guard, and the credential vault.

## Architecture

```
                        ┌──────────────────────────┐
                        │  Ollama  :11434          │
                        │  qwen2.5vl:7b            │
                        └────▲────────────┬────────┘
                   screenshot│            │one JSON action
                             │            ▼
  ┌────────────┐      ┌──────┴─────────────────────┐
  │ job.json   │─────▶│  ollama_agent.py           │
  │ hosts +    │      │  agent loop · coord grid   │
  │ actions    │      │  vault · stuck-loop guard  │
  └────────────┘      │  spawns server.py itself   │
                      └──────────────┬─────────────┘
                                     │
             ─────────── MCP · stdio · JSON-RPC ───────────
                                     │
                      ┌──────────────▼─────────────┐      ┌──────────────┐
                      │  server.py                 │◀─────│   job.json   │
                      │  10 tools · clamps coords  │ reads│  the logins  │
                      └──────────────┬─────────────┘      └──────────────┘
                                     │
                      ┌──────────────▼─────────────┐
                      │  rdp.py + desktop.py       │
                      │  cmdkey → Credential Mgr   │
                      │  .rdp profile, launch mstsc│
                      │  mss grab · win32 input    │
                      └──────────────┬─────────────┘
                        clicks, keys │ ▲ screenshot
                      ┌──────────────▼─┴───────────┐
                      │  mstsc.exe window          │
                      │  must hold local foreground│
                      └──────────────┬─────────────┘
  ═══════ YOUR MACHINE — everything above ═══════════════════════════
                                     │  RDP · TCP 3389
                      ┌──────────────▼─────────────┐
                      │  Remote Windows host       │
                      │  normal logon, no install  │
                      └────────────────────────────┘
```

`ollama_agent.py` is the only entry point. It starts `server.py` itself; you
never launch that separately.

**The loop.** Every tool returns a fresh screenshot, so the model always works
from the current screen rather than from memory:

```
screenshot ──▶ model picks ONE action ──▶ tool executes ──▶ new screenshot
                                                             └─ repeat
```

Coordinates are always pixels in the most recent screenshot, origin top-left.
That is the whole contract — and it is why a downscaled screenshot has to be
mapped back before a click is injected.

## Prerequisites

**On the machine that runs this**

| Requirement | Detail |
|---|---|
| Windows | 10, 11 or Server. Required — the server drives `mstsc.exe`. |
| Python | 3.10 or newer. |
| `mstsc.exe` | Built into Windows. Nothing to install. |
| Ollama | [ollama.com](https://ollama.com) — `winget install --id Ollama.Ollama -e` |
| A vision model | `ollama pull qwen2.5vl:7b` — about 6 GB. |
| RAM | ~8 GB free for the 7B alongside Windows. |
| Disk | ~250 MB for the virtualenv, plus the model. |
| GPU | Strongly recommended. On CPU expect **~75 s per step**. |
| Exclusive use | The machine cannot be used for anything else while a job runs. |

**On the remote host**

| Requirement | Detail |
|---|---|
| Remote Desktop | Enabled, TCP 3389 reachable from your machine. |
| An account | Any account permitted to log on over RDP. |
| Software | **Nothing.** No agent, no extra port, no install. |

## Install

```powershell
git clone <your-repo-url> win-rdp-mcp
cd win-rdp-mcp
powershell -ExecutionPolicy Bypass -File .\install.ps1 -PullModel
```

The installer verifies Windows and `mstsc.exe`, finds a Python 3.10+, builds
`.venv`, installs dependencies, runs the pywin32 post-install step, seeds
`job.json` from the example, checks that Ollama is running
with the model present, and verifies the whole chain end to end.

`-PullModel` downloads the model if it is missing. Drop it if you already have
one, or want to choose your own.

Then edit `job.json` and run it.

<details>
<summary>Manual setup instead</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy job.example.json job.json
.\.venv\Scripts\python.exe ollama_agent.py --selftest
```
</details>

## Configuration

| File | Holds | In git? |
|---|---|---|
| `config.json` | Session defaults — resolution, connect timeout, screenshot scale. | yes |
| `job.json` | Everything else: the hosts, their logins, and the action list. | **never** |

`job.json` is gitignored and ships with a committed `job.example.json` to copy
from. **There is no separate credential store** — the agent and the server both
read the logins straight out of the job file, so a password lives in exactly one
place. The agent tells the server which file to read through the `WIN_RDP_JOB`
environment variable when it spawns it; credentials never cross the MCP link.

### config.json

```json
{
  "default_width": 1280,
  "default_height": 800,
  "connect_timeout_seconds": 40,
  "allow_remote_run": false,
  "screenshot_scale": 1.0
}
```

`screenshot_scale` below `1.0` makes each screenshot cheaper for a vision model.
Clicks are mapped back to real pixels automatically, so the tool contract holds
at any scale.

### job.json — the unit of work

Two blocks: the machines, and the work.

```json
{
  "model": "qwen2.5vl:7b",
  "max_steps": 12,

  "hosts": [
    { "host": "10.0.0.5", "username": "CORP\\alice", "password": "…" },
    { "host": "10.0.0.6", "username": "CORP\\bob",   "password": "…" }
  ],

  "actions": [
    { "do": "Open Notepad and type HELLO", "secrets": [] },
    { "do": "Open Outlook. If it asks for a password, use the saved login. Stop when the inbox is on screen.",
      "secrets": ["login"] }
  ]
}
```

**One login per endpoint.** The same credential opens the RDP session *and*
signs in to applications inside it.

Every action runs on every host, in order: host 1 action 1, host 1 action 2,
then host 2. Each action gets its own fresh step budget and its own pass/fail
line in the summary.

An action may be a plain string — which lets it use the host's login if it needs
to — or an object with `secrets` naming what it may use. Inside an action the
login is always called `login`, meaning *the account for the machine being
driven right now*, so the same action text works unchanged across endpoints with
different credentials.

## Running it

```powershell
.\.venv\Scripts\python.exe ollama_agent.py --job job.json
```

One-off, without a job file:

```powershell
.\.venv\Scripts\python.exe ollama_agent.py --host 10.0.0.5 --goal "Open Notepad and type hello"
```

Check the wiring without loading a model:

```powershell
.\.venv\Scripts\python.exe ollama_agent.py --selftest
```

### Flags that matter

| Flag | Default | Why change it |
|---|---|---|
| `--model` | `qwen2.5vl:7b` | Bigger aims better, runs slower. The 3B cannot aim at all. |
| `--scale` | `0.75` | How much each screenshot is shrunk before the model sees it. |
| `--grid` | `50` | Coordinate-grid spacing in true pixels. Finer costs nothing. |
| `--max-steps` | `20` | Per action, so a confused model cannot loop forever. |
| `--num-ctx` | `8192` | Raise only if you also raise `--max-steps`. |
| `--transcript` | – | Write every step and result to JSON for debugging. |

### Reading the output

```
[mcp] connecting to 10.0.0.5 ...
[1] ( 78.1s) type {'text': 'HELLO'}
      screen: The Notepad window is open with a blank document...
      -> remote_type [0.9s] Typed 'HELLO' on 10.0.0.5.
[2] ( 75.7s) done
      screen: The Notepad window is open with the word 'HELLO' typed...
[end] model reported done

================ SUMMARY ================
  [OK  ] 10.0.0.5   Open Notepad and type HELLO   done (2 steps)
  1/1 completed
```

The `screen:` line is the model describing what it can actually see. **If it
repeats verbatim step after step, the model has stopped looking** — that is the
single most useful signal in the log.

## Tool reference

Ten tools, deliberately few and generic. Every coordinate is a pixel in the
latest screenshot; every call returns a short status line and a fresh
screenshot.

| Tool | Arguments | What it does |
|---|---|---|
| `rdp_connect` | host, width?, height? | Open or reuse a session. The login comes from the job file. |
| `rdp_status` | — | List sessions and whether each window is still alive. |
| `rdp_disconnect` | host | Close the local viewer. The remote session stays logged on. |
| `remote_screenshot` | host | Look at the current screen. |
| `remote_click` | host, x, y, button?, double?, wait_seconds? | Click. `wait_seconds` delays the screenshot so a launching app is visible in it. |
| `remote_move` | host, x, y | Hover without clicking, to reveal menus and tooltips. |
| `remote_type` | host, text | Type into whatever has focus. Click the field first. |
| `remote_press` | host, keys | `enter`, `esc`, `tab`, `ctrl+a`, `alt+f4`… |
| `remote_open_app` | host, name | Open Start and search. Returns results — then click the one you want. |
| `remote_wait` | host, seconds? | Let time pass, then look again. |

**Two hard limits.** The **Windows key is refused** — mstsc does not forward it
in windowed mode, so it would open your *local* Start menu; use
`remote_open_app`. And **only characters on the host keyboard layout can be
typed** — accented letters, CJK and symbols like `€` have no scan code and never
arrive. `remote_type` returns `PARTIALLY TYPED …` naming exactly what went
missing rather than reporting a success that quietly lost text.

## Credentials

**One login per endpoint**, used two different ways.

**Use 1 — opening the RDP session.** The server reads the login from the job
file and pushes it into the Windows Credential Manager with `cmdkey`, so `mstsc`
authenticates silently. The model never sees it and has no way to ask for it —
that is why every tool takes a hostname and nothing else.

**Use 2 — signing in to an app inside the session.** Outlook or a web portal
needs someone to physically type into a box. The model decides *when and where*;
it never learns *what*:

```
job.json ──▶ ollama_agent.py ──▶ MCP remote_type(text="<real value>")
                   │                        │
                   │                        ▼
                   │              server.py ─▶ scan-code keystrokes
                   │                        ─▶ mstsc window
                   │                        ─▶ the app's password box
                   │
                   └──▶ model only ever sees "login.password"
                        and "Typed the saved login.password (value hidden)"
```

What the model emits, after clicking the box:

```json
{"action": "type_secret", "secret": "login.password", "target": "password field"}
```

What the model is told:

```
SAVED LOGINS you can use with type_secret.
You cannot see the passwords. Use the name exactly as written:
  "login.username"   (the account is CORP\alice)
  "login.password"   (hidden)
```

`remote_type` normally echoes a preview of what it typed. For a secret that echo
is replaced, so the value never reaches the console, the transcript, or the
model's context. **The failure case is redacted too** — when a password contains
characters the host layout cannot type, the error would otherwise name those
exact characters.

**Only the host you are standing on.** The set of typeable logins is rebuilt for
each host as the run moves between machines, and contains exactly one entry:
that host's own. While driving `10.0.0.5`, `10.0.0.6`'s password is not refused
— it is *not present*. Typing one endpoint's credentials on another is
structurally impossible rather than merely disallowed.

**What this does not protect against.** The vault stops a password *leaking*. It
cannot stop a small model being talked into typing it into a convincing fake
sign-in page. Mitigations: scope every action with `secrets`, use
`"secrets": []` wherever no login is needed, and watch the audit line printed on
every use — `[audit] typing saved login.password on 10.0.0.5`.

## Writing actions a small model can follow

Action text is the main lever you have over reliability. Write it the way you
would brief someone on their first day: one concrete outcome, naming what they
should see when it is done.

| Instead of | Write | Because |
|---|---|---|
| "Check the mail" | "Open Outlook and stop when the inbox is on screen" | It needs a visible finish line to recognise. |
| "Do the usual checks" | "Open Event Viewer and report the newest error in the System log" | It has no memory of what is usual. |
| "Update the file" | "In the open Notepad window, type DONE at the end of the first line" | Naming the window removes a guess. |
| "Log in and check mail" | Two separate actions | Each action gets its own step budget and pass/fail. |

**Name the finish.** The single highest-value habit: end every action with what
the screen should show when it has worked. A model that cannot tell it has
finished will keep going — measured, it retyped the same word until the
stuck-loop guard stopped it.

## What we measured

Every design choice below was forced by an observed failure, not chosen on
taste. Numbers are from an Intel UHD 770 machine with no discrete GPU — Ollama
reports `100% CPU` — and 15.7 GB RAM.

### Can the model point at the right pixel?

Three targets with known positions on a real screenshot, scored automatically.
This is the question that decides whether any of it works.

| Target (ground truth) | `qwen2.5vl:3b` | `qwen2.5vl:7b` |
|---|---|---|
| File menu (49, 66) | (100, 30) miss | **(55, 60) hit** |
| Notepad text area | (640, 400) | **(100, 100) hit** |
| Start button (23, 780) | (1280, 800) miss | (20, 700) miss → **hit at `--grid 50`** |
| **Score** | **0 / 3** | **2–3 / 3** |
| Latency per call | ~47 s | ~75 s |

**The 3B does not do visual grounding — do not use it for clicking.** Its
answers are structural guesses that *look* plausible: `(640, 400)` is exactly
the centre of a 1280×800 screen, `(1280, 800)` is exactly the screen size quoted
in its prompt, and in an earlier run it answered `(400, 250)` —
character-for-character the coordinates from the worked example in the system
prompt.

That last one **scored as a hit**, because the example coordinate happened to
land inside the large text area. A benchmark without ground truth would have
recorded a pass.

### Where the time goes

| `--scale` | Image sent | Prompt tokens | Wall |
|---|---|---|---|
| 1.00 | 1280×800 | 1895 | 45.1 s |
| 0.75 | 960×600 | 1627 | 36.6 s |
| 0.50 | 640×400 | 1633 | 39.6 s |
| 0.35 | 448×280 | 1633 | 35.6 s |

Roughly **95% of each step is encoding the image**, not thinking — generation
itself is about 1.5 s. Qwen2.5-VL normalises images to a nearly fixed token
budget, so shrinking the screenshot buys far less than you would expect: cost
floors near 1630 tokens. **0.75 is the sweet spot** — the best fidelity
available at the cheapest price. Going to 1.0 costs 25% more for nothing.

**A finer grid costs nothing.** `--grid 50` and `--grid 100` both measured
**1699 prompt tokens** and ~73 s, because the grid is drawn into the image
rather than added as text. At grid 50 the 7B hits the Start button; at grid 100
it misses. 50 is the default.

### Five things the host does for the model

| Observed failure | Fix now in place |
|---|---|
| The 3B omitted `x`/`y` on **3 of 3** clicks. | Every schema field is required — constrained decoding only guarantees required fields. Clicks with no coordinates are refused, never sent as `(0,0)`. |
| The 7B never called `connect`, then re-sent `open_app` into the same error for **all 14 steps**. | The host connects before the model gets a turn. Step 1 begins with a real screenshot. |
| The same action repeated 14× into the same error. | Stuck-loop guard: interrupt at three identical actions, abort at four. |
| The model recited the prompt's worked example instead of looking, reporting "The Windows desktop is visible" while Notepad was open in front of it. | **The worked example was deleted.** |
| It typed HELLO, saw HELLO, and kept typing. | Rule 1 is now "check whether you are already done". |

**The transferable lesson:** with schema-constrained output, a worked example is
**not free**. The grammar already guarantees the reply's shape, so the example
adds nothing to format and simply hands a small model a block of text to recite.
Deleting it is what turned the `screen` field from a parroted constant into a
real reading that tracked the screen changing.

### A verified run

```
[1] (78.1s) type  text='HELLO'
      screen: The Notepad window is open with a blank document...
      -> remote_type  Typed 'HELLO'
[2] (75.7s) done
      screen: The Notepad window is open with the word 'HELLO' typed...
[end] model reported done
```

Two steps, correct termination, and the screen afterwards held exactly one
`HELLO`. Step 2 is the interesting one: the model saw its own change and
stopped.

### Choosing a model

The scarce combination is **vision + reliable instruction-following at a size
that fits in RAM**. For a 16 GB CPU-only box:

- `qwen2.5vl:7b` (6 GB) — **the recommended default.** Genuinely grounds;
  ~75 s per step.
- `qwen2.5vl:3b` (3.2 GB) — ~1.6× faster and cannot aim (see above). Only worth
  it if your task never needs a precise click.
- `mistral-small3.2` (~15 GB) — vision *and* native tools, but will not fit
  alongside Windows in 16 GB.

Qwen2.5-VL is the recommended family because it is explicitly trained for GUI
grounding — pointing at on-screen elements — which is exactly this workload.

If you add a model, re-check its aim before trusting it. A model that produces
well-formed JSON is not the same as a model that can see.

## Bugs found and fixed

Recorded because each was silent — the system reported success while doing the
wrong thing.

| Bug | Symptom | Fix |
|---|---|---|
| **Cross-host session confusion** | Any mstsc window matched any host. With one session open, connecting to a second host returned the *first* host's desktop — text addressed to one machine was typed into another, while `rdp_status` called both alive. | A window must match the pid or carry the host in its title. No match, no window. |
| **Unicode silently dropped** | `café münchen € 中文` arrived as `cafe mnchen` — every non-ASCII character gone, spaces kept — reported as success. | `PARTIALLY TYPED` names exactly which characters went missing. |
| **Shift state dropped** | `press('A')` typed `a`; `press('!')` typed `1`. A lone `+` pressed nothing. | Each token's implicit modifiers are resolved, and all tokens resolve before any key is pressed so an unknown key cannot leave a modifier stuck down. |
| **Downscaling broke every click** | `screenshot_scale` recorded the *scaled* size as the coordinate space but never scaled back. At 0.5 every click landed at half its intended position. | Coordinates map through the scale on the way out. |
| **No way to wait** | A fixed 0.6 s pause after each click meant app launches were invisible in the returned screenshot — the cue that makes a model click again and open a second copy. | Added `remote_wait` and `wait_seconds`. |

## Troubleshooting

| You see | It means | Do this |
|---|---|---|
| `No session for 'X'. Call rdp_connect first.` | No session for that exact host string. | Check the host string matches the job file character for character. |
| `The RDP window for 'X' is gone.` | mstsc was closed or killed. | Reconnect; the remote session is still logged on. |
| `FAILED: no login configured for 'X'` | Host missing from the job file. | Add it to the `hosts` block. |
| `window did not appear within 40s` | Unreachable *or* wrong credentials — indistinguishable from outside. | `Test-NetConnection X -Port 3389`, then re-check the login. |
| `REJECTED: the Windows key is not supported` | Working as designed. | Use `remote_open_app`. |
| `PARTIALLY TYPED …` | Characters not on the host keyboard layout. | Use ASCII. For a password, change it — the field now holds a mangled value. |
| `could not focus the RDP window` | Something else holds the local foreground. | Stop using the machine while jobs run. Close any local Start menu. |
| Connection refused on `:11434` | Ollama is not running. | Launch Ollama from the Start menu and retry. |
| Clicks land in the wrong place | Usually the model, not the server. | Confirm you are on the 7B, then try `--grid 25`. |
| The same `screen:` text every step | The model has stopped looking at the image. | Check nothing example-like crept into the prompt for it to recite. |
| A `server.py` process survives Ctrl-C | Known issue — the MCP child outlives an interrupted parent. | Stop it by hand; it holds no session. |

## Limitations and known issues

| Limitation | Detail |
|---|---|
| The machine is dedicated | The mstsc window must hold the local foreground, so your mouse moves and the machine cannot be used meanwhile. |
| ASCII only | No accented, CJK or symbol characters can be typed over this path — including in passwords. |
| Pixel aiming is the weak point | Small vision models are far better at "which window is this" than at picking one 20-pixel-tall menu row. Large targets work; dense menus are unreliable. |
| Slow on CPU | ~75 s per step with the 7B. Background automation, not something to watch. |
| Auth failure looks like unreachable | Both surface as the same 40-second timeout. |
| Start button position is assumed | `remote_open_app` clicks the bottom-left. A centred Windows 11 taskbar would need adjusting. |
| **Orphaned server process** | Interrupting a run leaves the MCP child `server.py` running. It holds no session, but stop it by hand. |
| Unactivated Office blocks sign-in | Outlook on an unlicensed host opens onto a trial/licence wall. The model is instructed never to accept terms, so it will correctly stop and report. Sign in once by hand first. |

**The upgrade that would change everything.** Pixel aiming is the ceiling on
reliability. An agent running *inside* the session, letting the model click
elements by name instead of coordinates, would remove that ceiling entirely — at
the cost of one extra open port on the remote host, which is exactly the
constraint that produced this design.

## Repo layout

```
ollama_agent.py       the entry point: MCP↔Ollama host, agent loop, grid, vault
server.py             MCP server — the 10 tool definitions
rdp.py                sessions: cmdkey, .rdp profile, window lookup, prompts
desktop.py            screenshots, synthetic input, focus handling

config.json           defaults (resolution, timeout, scale)
job.example.json      template → job.json          gitignored

install.ps1           one-command setup
requirements.txt      direct dependencies
README.md             this file
```

`sessions/` and `__pycache__/` appear at runtime and are gitignored.

## Security

- The model handles **hostnames only**; credentials stay server-side, and a
  login typed into an app is substituted at the keyboard, never shown.
- `job.json` holds plaintext passwords and is gitignored. Confirm it was never
  committed before publishing this repo.
- The vault stops a password *leaking*. It cannot stop a small model being
  talked into typing it into the wrong box — scope actions, and watch the
  `[audit]` line printed on every use.
- This drives *your* RDP session with *your* credentials. Only configure hosts
  you own and intend to automate.
