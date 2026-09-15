"""win-rdp-mcp: an MCP server that opens a Remote Desktop session and performs
generic desktop actions inside it.

It is driven by ollama_agent.py, which speaks MCP to this server on one side and
to a local Ollama model on the other. You do not start this file yourself - the
agent spawns it as a child process over stdio.

Design for a small model:
  * Few, generic tools.
  * Every action is expressed in screenshot-pixel coordinates.
  * Every tool returns a short plain-English status line first.
  * Credentials are read from the job file on disk, never passed as
    arguments, so the model only ever handles a hostname.

Run directly only to debug:  python server.py
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import functools
from pathlib import Path

import win32gui
from mcp.server.mcpserver import MCPServer, Image

import rdp
import desktop

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

srv = MCPServer("win-rdp")

# host -> {"pid": int, "hwnd": int, "width": int, "height": int}
SESSIONS: dict[str, dict] = {}


def _resolve(host: str) -> dict:
    """Return the live session record for host, re-finding the window if the
    cached handle went stale. Raises if there is no usable session."""
    sess = SESSIONS.get(host)
    if not sess:
        raise RuntimeError(
            f"No session for {host!r}. Call rdp_connect first.")
    hwnd = sess.get("hwnd")
    # Re-find if the handle is dead OR now belongs to a different host: a
    # handle can be recycled, and acting on the wrong desktop is far worse
    # than failing.
    if (not hwnd or not win32gui.IsWindow(hwnd)
            or not rdp.window_host_matches(hwnd, host)):
        hwnd = rdp.find_window(host, sess.get("pid"))
        if not hwnd:
            raise RuntimeError(
                f"The RDP window for {host!r} is gone. Reconnect with rdp_connect.")
        sess["hwnd"] = hwnd
    return sess


def _shot(host: str) -> Image:
    sess = _resolve(host)
    scale = float(CONFIG.get("screenshot_scale", 1.0)) or 1.0
    png, w, h = desktop.capture(sess["hwnd"], scale=scale)
    # width/height describe the coordinate space the MODEL sees. When the
    # screenshot is downscaled they are smaller than the real client area, so
    # the scale is kept to map the model's pixels back to real ones.
    sess["width"], sess["height"], sess["scale"] = w, h, scale
    return Image(data=png, format="png")


def guard(fn):
    """Return a friendly one-line message instead of letting an exception become
    an opaque 'Error executing tool' plus a stderr traceback. Keeps the driving
    model informed and able to recover (e.g. 'reconnect with rdp_connect')."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - deliberately broad for a tool boundary
            return f"ERROR: {e}"
    return wrapper


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------
@srv.tool(
    description="Open a Remote Desktop session to a host and wait until its "
    "desktop is visible. Provide only the host (name or IP); credentials are "
    "read from the job file by the server itself. Returns a status line and a "
    "screenshot of the session. Example: rdp_connect(host='10.0.0.5').")
@guard
def rdp_connect(host: str, width: int = 0, height: int = 0):
    width = width or CONFIG["default_width"]
    height = height or CONFIG["default_height"]
    if not rdp.credentials_for(host):
        return (f"FAILED: no login configured for {host!r}. Add it to the "
                f"'hosts' block of {rdp.job_path().name}.")
    # Idempotent: if a live window for this host already exists, reuse it instead
    # of opening a second mstsc.
    # find_window only returns a window whose title carries this host, so a
    # live session to a DIFFERENT machine is never adopted here.
    existing = rdp.find_window(host)
    if existing and win32gui.IsWindow(existing) and rdp.window_host_matches(existing, host):
        SESSIONS[host] = {"pid": SESSIONS.get(host, {}).get("pid"), "hwnd": existing,
                          "width": width, "height": height}
        return [f"Already connected to {host}; reusing the existing session.",
                _shot(host)]
    pid = rdp.launch(host, width, height)
    hwnd = rdp.wait_for_window(host, pid, CONFIG["connect_timeout_seconds"])
    if not hwnd:
        return (f"FAILED: launched mstsc for {host!r} but its window did not "
                f"appear within {CONFIG['connect_timeout_seconds']}s. The host may "
                f"be unreachable or the credentials may be wrong.")
    SESSIONS[host] = {"pid": pid, "hwnd": hwnd, "width": width, "height": height}
    time.sleep(1.0)
    return [f"CONNECTED to {host} ({width}x{height}). Desktop screenshot below.",
            _shot(host)]


@srv.tool(
    description="List the Remote Desktop sessions this server has opened and "
    "whether each window is still alive.")
@guard
def rdp_status():
    if not SESSIONS:
        return "No sessions open."
    rows = []
    for host, sess in SESSIONS.items():
        hwnd = sess.get("hwnd")
        if not hwnd or not win32gui.IsWindow(hwnd):
            state = "window closed"
        elif not rdp.window_host_matches(hwnd, host):
            state = "STALE - its window now belongs to another host"
        else:
            state = "alive"
        rows.append(f"{host}: {state} "
                    f"({sess['width']}x{sess['height']})")
    return "\n".join(rows)


@srv.tool(
    description="Close the Remote Desktop session for a host.")
@guard
def rdp_disconnect(host: str):
    sess = SESSIONS.get(host)
    if not sess:
        return f"No session for {host!r}."
    hwnd = sess.get("hwnd")
    if hwnd and win32gui.IsWindow(hwnd):
        rdp.close_window(hwnd)
    SESSIONS.pop(host, None)
    return f"DISCONNECTED {host}."


# ---------------------------------------------------------------------------
# Interaction tools (all coordinates are pixels in the latest screenshot)
# ---------------------------------------------------------------------------
def _clamp(sess: dict, x: int, y: int) -> tuple[int, int]:
    w = sess.get("width") or CONFIG["default_width"]
    h = sess.get("height") or CONFIG["default_height"]
    return max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1))


def _to_client(sess: dict, x: int, y: int) -> tuple[int, int]:
    """Convert a coordinate in the last SCREENSHOT into a real client-area
    pixel. These differ whenever screenshot_scale != 1.0 (downscaled shots are
    much cheaper for a small vision model). Without this the tool contract
    'coordinates are pixels in the latest screenshot' silently breaks and every
    click lands short of its target."""
    scale = float(sess.get("scale", 1.0)) or 1.0
    if scale == 1.0:
        return x, y
    return int(round(x / scale)), int(round(y / scale))


def _start_button(sess: dict) -> tuple[int, int]:
    """Real client pixel of the remote taskbar Start button (bottom-left).

    Derived from the screenshot height then mapped back through the scale, so a
    downscaled screenshot does not move the Start button."""
    h = sess.get("height") or CONFIG["default_height"]
    return _to_client(sess, 18, h - 20)


@srv.tool(
    description="Take a fresh screenshot of the host's Remote Desktop session. "
    "Use this to see the current state before deciding where to click. "
    "Coordinates for click/move refer to pixels in this image, with (0,0) at "
    "the top-left.")
@guard
def remote_screenshot(host: str):
    return [f"Screenshot of {host}:", _shot(host)]


@srv.tool(
    description="Click inside the host's Remote Desktop at pixel (x, y) from the "
    "latest screenshot. button is 'left' or 'right'. Set double=true for a "
    "double-click (e.g. to open a desktop icon). Returns a screenshot taken right "
    "after the click so you can see the result. If the click starts an app or "
    "opens a window, set wait_seconds (5-15) so the screenshot shows the result "
    "instead of the unchanged screen -- otherwise you may think it failed and "
    "click twice.")
@guard
def remote_click(host: str, x: int, y: int, button: str = "left", double: bool = False,
                 wait_seconds: float = 0.0):
    sess = _resolve(host)
    cx, cy = _clamp(sess, x, y)
    rx, ry = _to_client(sess, cx, cy)
    desktop.click(sess["hwnd"], rx, ry, button=button, double=double)
    time.sleep(max(0.6, min(float(wait_seconds), 60.0)))
    kind = "double-" if double else ""
    return [f"{kind}{button}-clicked ({cx},{cy}) on {host}.", _shot(host)]


@srv.tool(
    description="Move the mouse to pixel (x, y) from the latest screenshot "
    "without clicking (useful for hovering to reveal menus/tooltips).")
@guard
def remote_move(host: str, x: int, y: int):
    sess = _resolve(host)
    cx, cy = _clamp(sess, x, y)
    rx, ry = _to_client(sess, cx, cy)
    desktop.move(sess["hwnd"], rx, ry)
    time.sleep(0.3)
    return [f"Moved to ({cx},{cy}) on {host}.", _shot(host)]


@srv.tool(
    description="Type text into the host's Remote Desktop, into whatever field "
    "currently has focus. Always click the target field first so it has the "
    "cursor. Characters are sent as real keystrokes. Returns a screenshot after "
    "typing so you can confirm the text landed.")
@guard
def remote_type(host: str, text: str):
    sess = _resolve(host)
    dropped = desktop.type_text(sess["hwnd"], text, method="type")
    time.sleep(0.4)
    preview = text if len(text) <= 40 else text[:40] + "..."
    if dropped:
        missing = "".join(sorted(set(dropped)))
        line = (f"PARTIALLY TYPED {preview!r} on {host}: {len(dropped)} character(s) "
                f"are NOT on the host keyboard layout and are missing from the "
                f"text: {missing!r}. Check the screenshot before continuing.")
    else:
        line = f"Typed {preview!r} on {host}."
    return [line, _shot(host)]


@srv.tool(
    description="Press a key or key combination in the host's Remote Desktop. "
    "Examples: 'enter', 'esc', 'tab', 'ctrl+c', 'ctrl+v', 'ctrl+a', 'alt+f4', "
    "'down', 'f5'. The Windows key is NOT supported (it would trigger the local "
    "machine's Start menu, not the remote one); open apps with remote_open_app "
    "instead. Returns a screenshot afterward.")
@guard
def remote_press(host: str, keys: str):
    sess = _resolve(host)
    try:
        desktop.press(sess["hwnd"], keys)
    except ValueError as e:
        return f"REJECTED: {e}"
    time.sleep(0.5)
    return [f"Pressed '{keys}' on {host}.", _shot(host)]


@srv.tool(
    description="Search the host's Start menu for an application by name (e.g. "
    "'chrome', 'excel', 'notepad', 'outlook'). Clicks the Start button and types "
    "the name, then returns a screenshot of the search results. This does NOT "
    "launch the app by itself: look at the screenshot and call remote_click on "
    "the app entry you want, which is usually the highlighted top 'Best match'. "
    "This two-step approach works across Windows versions whose Start layouts "
    "differ.")
@guard
def remote_open_app(host: str, name: str):
    sess = _resolve(host)
    hwnd = sess["hwnd"]
    desktop.press(hwnd, "esc")
    time.sleep(0.4)
    sx, sy = _start_button(sess)
    desktop.click(hwnd, sx, sy)        # open remote Start (Win key is not forwarded)
    time.sleep(1.4)
    desktop.type_scancode(hwnd, name, per_char_delay=0.07)  # real keys trigger search
    time.sleep(1.8)
    return [f"Searched Start for '{name}' on {host}. Click the app you want "
            f"(usually the highlighted top result) with remote_click to launch it.",
            _shot(host)]


@srv.tool(
    description="Wait for a number of seconds and then return a fresh screenshot. "
    "Use this when the host is still busy -- an app is starting, a page is "
    "loading, a progress bar is running -- and the last screenshot did not show "
    "the result yet. Waiting and looking again is always better than repeating "
    "a click, which can open a second copy of the app.")
@guard
def remote_wait(host: str, seconds: float = 5.0):
    _resolve(host)
    seconds = max(0.0, min(float(seconds), 60.0))
    time.sleep(seconds)
    return [f"Waited {seconds:g}s on {host}.", _shot(host)]


def _exit_when_parent_dies() -> None:
    """Leave as soon as the process that spawned us does.

    This server exists only to serve one parent over a stdio pipe, so it has no
    reason to outlive it. The MCP client does tear the child down on a clean
    exit - it even puts it in a Windows job object with KILL_ON_JOB_CLOSE - but
    none of that runs when the parent is hard-killed (taskkill /F, a stopped
    task, a debugger detach). Measured: two server.py processes survived an
    interrupted run and had to be stopped by hand.

    So the child takes responsibility for itself. ollama_agent.py passes its own
    pid in WIN_RDP_PARENT_PID; we hold a handle to that exact process and block
    on it. A handle pins the process object rather than the number, so a recycled
    pid cannot make us exit early. Without the variable - running this file by
    hand to debug - no watchdog is started.
    """
    raw = os.environ.get("WIN_RDP_PARENT_PID", "").strip()
    if not raw.isdigit():
        return
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(raw))
    if not handle:
        return          # already gone, or not permitted; the pipe is the fallback

    def wait() -> None:
        ctypes.windll.kernel32.WaitForSingleObject(handle, INFINITE)
        # The parent is gone; nothing can be mid-request, so leave immediately
        # rather than unwinding a server whose transport no longer exists.
        os._exit(0)

    threading.Thread(target=wait, daemon=True, name="parent-watchdog").start()


if __name__ == "__main__":
    # stdio only. ollama_agent.py spawns this as a child and speaks MCP over the
    # pipe; there is no network listener and nothing to expose.
    _exit_when_parent_dies()
    srv.run(transport="stdio")
