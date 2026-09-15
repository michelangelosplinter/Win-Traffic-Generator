"""RDP session management: store credentials in Windows Credential Manager,
generate a per-host .rdp profile, launch mstsc, and locate its window.

Credentials are read from the job file on disk, never from a tool argument, so
the driving LLM never receives a password - every MCP tool takes a hostname and
nothing else.

The job file is named by WIN_RDP_JOB (set by ollama_agent.py when it spawns this
server), falling back to job.json beside this file.
"""
from __future__ import annotations

import os
import json
import time
import subprocess
from pathlib import Path

import win32gui
import win32con
import win32process

BASE = Path(__file__).resolve().parent
SESSION_DIR = BASE / "sessions"
SESSION_DIR.mkdir(exist_ok=True)

MSTSC_CLASS = "TscShellContainerClass"  # top-level window class of the RDP client


def job_path() -> Path:
    """The job file this server should read logins from."""
    override = os.environ.get("WIN_RDP_JOB")
    return Path(override) if override else BASE / "job.json"


def _load_logins() -> dict:
    """host -> {username, password}, taken from the job file's `hosts` block.

    One login per endpoint: the same credential opens the session and is what
    gets typed into applications inside it. Read fresh on every call so editing
    the job file does not require a restart.
    """
    p = job_path()
    if not p.exists():
        return {}
    try:
        job = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    logins = {}
    for entry in job.get("hosts") or []:
        if isinstance(entry, dict) and entry.get("host") and entry.get("username"):
            logins[entry["host"]] = {"username": entry["username"],
                                     "password": entry.get("password", "")}
    return logins


def credentials_for(host: str) -> dict | None:
    return _load_logins().get(host)


def store_credentials(host: str) -> bool:
    """Push the host's credentials into Windows Credential Manager under
    TERMSRV/<host> so mstsc authenticates without a prompt. Returns False if no
    credentials are configured for the host."""
    creds = credentials_for(host)
    if not creds:
        return False
    # Passed as an argument vector (no shell) so special characters are safe and
    # the password is never rendered through a shell command line.
    subprocess.run(
        ["cmdkey", f"/generic:TERMSRV/{host}",
         f"/user:{creds['username']}", f"/pass:{creds['password']}"],
        check=True, capture_output=True, text=True,
    )
    return True


def clear_credentials(host: str) -> None:
    subprocess.run(["cmdkey", f"/delete:TERMSRV/{host}"],
                   capture_output=True, text=True)


def write_rdp_profile(host: str, width: int, height: int) -> Path:
    creds = credentials_for(host) or {}
    username = creds.get("username", "")
    lines = [
        f"full address:s:{host}",
        f"username:s:{username}",
        "screen mode id:i:1",          # 1 = windowed (no auto-hide connection bar)
        f"desktopwidth:i:{width}",
        f"desktopheight:i:{height}",
        "smart sizing:i:0",            # 1:1 pixels, no scaling
        "dynamic resolution:i:0",
        "authentication level:i:0",    # connect without the identity-warning prompt
        "prompt for credentials:i:0",
        "keyboardhook:i:2",            # always forward Windows-key combos to the session
        "redirectclipboard:i:0",       # no local-resource redirection -> no unsigned-.rdp warning
        "redirectprinters:i:0",
        "redirectdrives:i:0",
        "redirectcomports:i:0",
        "redirectsmartcards:i:0",
        "audiomode:i:2",               # do not play remote audio locally
        "autoreconnection enabled:i:1",
    ]
    path = SESSION_DIR / f"{host.replace(':', '_')}.rdp"
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path


def window_host_matches(hwnd: int, host: str) -> bool:
    """True if this mstsc window is actually connected to `host`.

    mstsc titles its window '<host> - Remote Desktop Connection' (possibly with
    a prefix), so the host string appears verbatim. Used to make sure a window
    is never handed back for the wrong machine."""
    try:
        return host.lower() in win32gui.GetWindowText(hwnd).lower()
    except Exception:
        return False


def find_window(host: str, pid: int | None = None) -> int | None:
    """Locate the mstsc top-level window for THIS host. Prefer matching the
    launched process id; otherwise require the host to appear in the title.

    A window is never returned on class alone: with a session open to another
    machine that would hand back the wrong desktop, and every subsequent click
    and keystroke would land on it."""
    matches: list[tuple[int, int]] = []  # (hwnd, score)

    def enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            cls = win32gui.GetClassName(hwnd)
        except Exception:
            return
        if cls != MSTSC_CLASS:
            return
        wpid = win32process.GetWindowThreadProcessId(hwnd)[1]
        score = 0
        if pid and wpid == pid:
            score += 10
        if window_host_matches(hwnd, host):
            score += 5
        if score:                       # no positive evidence -> not our window
            matches.append((hwnd, score))

    win32gui.EnumWindows(enum, None)
    if not matches:
        return None
    matches.sort(key=lambda m: m[1], reverse=True)
    return matches[0][0]


BM_CLICK = 0x00F5
BM_SETCHECK = 0x00F1
BST_CHECKED = 1
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E


def _child_controls(dlg: int) -> list[tuple[int, str, str]]:
    """Return (hwnd, class, text) for each child control of a dialog."""
    out = []

    def enum(h, _):
        cls = win32gui.GetClassName(h)
        text = win32gui.GetWindowText(h)
        out.append((h, cls, text))

    win32gui.EnumChildWindows(dlg, enum, None)
    return out


def dismiss_prompts(pid: int) -> bool:
    """Auto-dismiss the mstsc warning dialogs that block an unattended connect
    (the 'publisher can't be identified' / identity-verification prompts) for
    this session's process. Ticks any 'don't ask again' checkbox and clicks the
    affirmative button. Returns True if a dialog was acted on."""
    acted = False
    targets = []

    def enum(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        wpid = win32process.GetWindowThreadProcessId(h)[1]
        if wpid != pid:
            return
        cls = win32gui.GetClassName(h)
        if cls == "#32770" or "POPUP" in cls.upper():
            targets.append(h)

    win32gui.EnumWindows(enum, None)
    for dlg in targets:
        for h, cls, text in _child_controls(dlg):
            low = text.lower().replace("&", "")
            if cls == "Button" and ("don't ask" in low or "dont ask" in low
                                     or "do not ask" in low):
                win32gui.SendMessage(h, BM_SETCHECK, BST_CHECKED, 0)
        for h, cls, text in _child_controls(dlg):
            low = text.lower().replace("&", "")
            if cls == "Button" and low in ("connect", "yes", "ok", "continue"):
                win32gui.SendMessage(h, BM_CLICK, 0, 0)
                acted = True
                break
    return acted


def launch(host: str, width: int, height: int) -> int:
    """Store credentials, write the profile, start mstsc. Returns the pid."""
    store_credentials(host)
    profile = write_rdp_profile(host, width, height)
    proc = subprocess.Popen(["mstsc.exe", str(profile)])
    return proc.pid


def wait_for_window(host: str, pid: int, timeout: float) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_window(host, pid)
        if hwnd:
            # Give the session a moment to paint the desktop after the window
            # first appears (login screen -> desktop transition).
            time.sleep(2.0)
            return hwnd
        # No desktop window yet: clear any warning dialog that is blocking it.
        dismiss_prompts(pid)
        time.sleep(0.5)
    return None


def close_window(hwnd: int) -> None:
    """Terminate the mstsc process that owns this window. Using WM_CLOSE instead
    makes mstsc pop a 'your session will be disconnected' confirmation dialog
    that blocks a clean, unattended disconnect, so we kill the process. The
    remote session stays logged on and can be reconnected."""
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True)
    except Exception:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def kill_all_mstsc() -> None:
    """Force-close every mstsc client window (cleanup helper)."""
    subprocess.run(["taskkill", "/IM", "mstsc.exe", "/F"],
                   capture_output=True, text=True)
