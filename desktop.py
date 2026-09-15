"""Low-level desktop interaction with a specific top-level window (the mstsc
RDP client). Everything is expressed in *screenshot pixel* coordinates so the
model only ever reasons about "where I see it in the last screenshot".
"""
from __future__ import annotations

import io
import time
import ctypes
from ctypes import wintypes

import win32api
import win32con
import win32gui
import win32process
import mss
from PIL import Image as PILImage

# Make the process DPI aware so GetClientRect/ClientToScreen and the mss grab
# all agree on physical pixels. Must run before any window geometry is read.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Window focus / geometry
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32
_HWND_TOP = 0
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040
_SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001

# Disable the foreground-lock timeout process-wide. Without this, Windows lets
# only the process that most recently received user input call
# SetForegroundWindow; our background automation process would be denied and the
# RDP window could never be reliably focused for keystroke injection.
try:
    _user32.SystemParametersInfoW(_SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                  ctypes.c_void_p(0), 0)
except Exception:
    pass


# The first keystroke sent immediately after the RDP window takes focus is
# occasionally swallowed (mstsc is still wiring up its input capture), so every
# key path pauses this long once focus is confirmed.
FOCUS_SETTLE = 0.15


def _tap_alt() -> None:
    """A momentary Alt press. Windows treats this as user input activity, which
    lifts the foreground lock and lets a background process call
    SetForegroundWindow successfully."""
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)


def _try_foreground(hwnd: int) -> None:
    try:
        _user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:
        pass
    fg = win32gui.GetForegroundWindow()
    cur_thread = win32api.GetCurrentThreadId()
    fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    tgt_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    attached = []
    try:
        if fg_thread and fg_thread != cur_thread:
            if _user32.AttachThreadInput(cur_thread, fg_thread, True):
                attached.append(fg_thread)
        if tgt_thread not in (cur_thread, fg_thread):
            if _user32.AttachThreadInput(cur_thread, tgt_thread, True):
                attached.append(tgt_thread)
        _user32.BringWindowToTop(hwnd)
        _user32.SetWindowPos(hwnd, _HWND_TOP, 0, 0, 0, 0,
                             _SWP_NOSIZE | _SWP_NOMOVE | _SWP_SHOWWINDOW)
        _user32.SetForegroundWindow(hwnd)
        _user32.SetActiveWindow(hwnd)
    except Exception:
        pass
    finally:
        for th in attached:
            try:
                _user32.AttachThreadInput(cur_thread, th, False)
            except Exception:
                pass


def _click_titlebar(hwnd: int) -> None:
    """Left-click the window's title bar. A mouse click is foreground-
    independent (it activates whatever window is under the cursor) and, crucially,
    dismisses any LOCAL Start/Search popup that is holding the foreground and
    that SetForegroundWindow cannot displace. We click the caption, not the
    client area, so the remote session content is untouched."""
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    # Caption band sits between the window top and the client-area top. Click
    # left-of-center to avoid the system menu icon and the min/max/close buttons.
    cx = l + min(160, (r - l) // 3)
    cy = t + 14
    win32api.SetCursorPos((cx, cy))
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, cx, cy, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, cx, cy, 0, 0)
    time.sleep(0.1)


def focus(hwnd: int, retries: int = 4) -> bool:
    """Bring the RDP window to the foreground robustly. Returns True if it is
    foreground at the end; never raises.

    The reliable primitive is a title-bar CLICK, not SetForegroundWindow: a
    background process is often denied SetForegroundWindow, and a local
    Start/Search popup (a protected shell CoreWindow) cannot be displaced by it
    at all -- but a click activates the window and closes that popup.
    """
    if win32gui.GetForegroundWindow() == hwnd and not win32gui.IsIconic(hwnd):
        return True
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass

    for attempt in range(retries):
        _click_titlebar(hwnd)
        _try_foreground(hwnd)     # nudge via the API too, harmless once clicked
        time.sleep(0.15)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def ensure_focus(hwnd: int, timeout: float = 6.0) -> bool:
    """Block until the window is confirmed foreground, or timeout. Keystrokes
    must never be sent until this returns True, or they leak to the wrong
    window (locally or into the wrong remote app)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if focus(hwnd):
            return True
        time.sleep(0.2)
    return False


def client_region(hwnd: int) -> dict:
    """Screen-coordinate rectangle of the window's client area (the remote
    desktop surface, excluding any window border)."""
    l, t, r, b = win32gui.GetClientRect(hwnd)
    sx, sy = win32gui.ClientToScreen(hwnd, (l, t))
    return {"left": sx, "top": sy, "width": r - l, "height": b - t}


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------
def capture(hwnd: int, scale: float = 1.0) -> tuple[bytes, int, int]:
    """Return (png_bytes, width, height) of the window's client area.

    width/height describe the coordinate space the model should use for clicks.
    """
    focus(hwnd)
    region = client_region(hwnd)
    if region["width"] <= 0 or region["height"] <= 0:
        raise RuntimeError("window has no visible client area (minimized?)")
    with mss.mss() as sct:
        raw = sct.grab(region)
        img = PILImage.frombytes("RGB", raw.size, raw.rgb)
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), img.width, img.height


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------
def _to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    region = client_region(hwnd)
    return region["left"] + int(x), region["top"] + int(y)


def move(hwnd: int, x: int, y: int) -> None:
    ensure_focus(hwnd)
    sx, sy = _to_screen(hwnd, x, y)
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.05)


def click(hwnd: int, x: int, y: int, button: str = "left", double: bool = False) -> None:
    ensure_focus(hwnd)
    sx, sy = _to_screen(hwnd, x, y)
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.05)
    if button == "right":
        down, up = win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP
    else:
        down, up = win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP
    clicks = 2 if double else 1
    for _ in range(clicks):
        win32api.mouse_event(down, sx, sy, 0, 0)
        win32api.mouse_event(up, sx, sy, 0, 0)
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------
# SendInput structures for Unicode typing.
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def _send_unicode(ch: str) -> None:
    code = ord(ch)
    events = []
    for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
        ki = _KEYBDINPUT(0, code, flags, 0, None)
        events.append(_INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki)))
    n = len(events)
    arr = (_INPUT * n)(*events)
    ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(_INPUT))


def _type_char_scancode(ch: str) -> bool:
    """Type one character using scan-code-backed key events, which mstsc
    forwards to the remote session (unlike synthetic Unicode input). Returns
    False if the character is not typable on the current keyboard layout."""
    res = win32api.VkKeyScan(ch)
    if res == -1:
        return False
    vk = res & 0xFF
    shift_state = (res >> 8) & 0xFF
    mods = []
    if shift_state & 1:
        mods.append(win32con.VK_SHIFT)
    if shift_state & 2:
        mods.append(win32con.VK_CONTROL)
    if shift_state & 4:
        mods.append(win32con.VK_MENU)
    for m in mods:
        win32api.keybd_event(m, 0, 0, 0)
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    for m in reversed(mods):
        win32api.keybd_event(m, 0, win32con.KEYEVENTF_KEYUP, 0)
    return True


def _set_clipboard(text: str, retries: int = 8) -> bool:
    import win32clipboard
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.1)
    return False


def type_scancode(hwnd: int, text: str, per_char_delay: float = 0.055) -> list[str]:
    """Type character-by-character via scan-code key events. The per-char delay
    must exceed the RDP round-trip or scan codes interleave and the remote
    receives scrambled text. Use for short/simple fields or when paste is
    unavailable.

    Returns the characters that have no scan code on the current keyboard
    layout (accented letters, CJK, symbols such as the euro sign). Those are
    retried as synthetic Unicode input, but mstsc does not forward that to the
    session, so they usually do NOT arrive. They are returned rather than
    swallowed, so the caller can report the loss instead of claiming a success
    that quietly dropped text."""
    if not ensure_focus(hwnd):
        raise RuntimeError("could not focus the RDP window before typing")
    time.sleep(FOCUS_SETTLE)
    undeliverable: list[str] = []
    for ch in text:
        if ch == "\n":
            press(hwnd, "enter")
        elif not _type_char_scancode(ch):
            undeliverable.append(ch)
            _send_unicode(ch)      # last resort; rarely crosses the RDP link
        time.sleep(per_char_delay)
    return undeliverable


def type_text(hwnd: int, text: str, method: str = "type") -> list[str]:
    """Enter text into the focused remote field. Returns the characters that
    could not be delivered as real keystrokes (an empty list on a clean run).

    Default method 'type' sends real scan-code key events, which this RDP path
    forwards reliably. 'paste' sets the local clipboard and sends Ctrl+V -- only
    works when the host permits clipboard redirection (many locked-down hosts,
    including this test server, do not), so it is not the default."""
    if not ensure_focus(hwnd):
        raise RuntimeError("could not focus the RDP window before typing")
    if method == "paste" and _set_clipboard(text):
        time.sleep(0.25)
        press(hwnd, "ctrl+v")
        time.sleep(0.2)
        return []
    return type_scancode(hwnd, text)


# Virtual-key map for named keys used in combos.
_VK = {
    "ctrl": win32con.VK_CONTROL, "control": win32con.VK_CONTROL,
    "alt": win32con.VK_MENU, "shift": win32con.VK_SHIFT,
    "win": win32con.VK_LWIN, "windows": win32con.VK_LWIN, "super": win32con.VK_LWIN,
    "enter": win32con.VK_RETURN, "return": win32con.VK_RETURN,
    "tab": win32con.VK_TAB, "esc": win32con.VK_ESCAPE, "escape": win32con.VK_ESCAPE,
    "space": win32con.VK_SPACE, "backspace": win32con.VK_BACK,
    "delete": win32con.VK_DELETE, "del": win32con.VK_DELETE,
    "home": win32con.VK_HOME, "end": win32con.VK_END,
    "up": win32con.VK_UP, "down": win32con.VK_DOWN,
    "left": win32con.VK_LEFT, "right": win32con.VK_RIGHT,
    "pageup": win32con.VK_PRIOR, "pagedown": win32con.VK_NEXT,
    "f1": win32con.VK_F1, "f2": win32con.VK_F2, "f3": win32con.VK_F3,
    "f4": win32con.VK_F4, "f5": win32con.VK_F5, "f6": win32con.VK_F6,
    "f7": win32con.VK_F7, "f8": win32con.VK_F8, "f9": win32con.VK_F9,
    "f10": win32con.VK_F10, "f11": win32con.VK_F11, "f12": win32con.VK_F12,
}


def _vk_mods_for(token: str) -> tuple[int, list[int]]:
    """Return (virtual-key, implicit modifiers) for one token of a combo.

    A single printable character carries its own shift state: on a US layout
    'A' is Shift+a and '!' is Shift+1. Dropping that modifier is not a no-op --
    the remote receives 'a' and '1', silently typing the wrong text."""
    raw = token.strip()
    low = raw.lower()
    if low in ("win", "windows", "super", "lwin", "rwin", "meta", "cmd"):
        raise ValueError(
            "the Windows key is not supported over RDP here (it triggers the "
            "LOCAL Start menu); open apps via a desktop icon or Ctrl+Esc + click")
    if low in _VK:
        return _VK[low], []
    if len(raw) == 1:
        res = win32api.VkKeyScan(raw)
        if res == -1:
            raise ValueError(
                f"{raw!r} cannot be typed on the current keyboard layout")
        state = (res >> 8) & 0xFF
        mods = []
        if state & 1:
            mods.append(win32con.VK_SHIFT)
        if state & 2:
            mods.append(win32con.VK_CONTROL)
        if state & 4:
            mods.append(win32con.VK_MENU)
        return res & 0xFF, mods
    raise ValueError(f"unknown key: {token!r}")


# Keys that must carry the extended-key flag or the remote session ignores them
# (the Windows keys are the important case for RDP; nav keys are extended too).
_EXTENDED_VKS = {
    win32con.VK_LWIN, win32con.VK_RWIN,
    win32con.VK_UP, win32con.VK_DOWN, win32con.VK_LEFT, win32con.VK_RIGHT,
    win32con.VK_HOME, win32con.VK_END, win32con.VK_PRIOR, win32con.VK_NEXT,
    win32con.VK_INSERT, win32con.VK_DELETE,
}


def _key_down(vk: int) -> None:
    flags = win32con.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
    win32api.keybd_event(vk, 0, flags, 0)


def _key_up(vk: int) -> None:
    flags = win32con.KEYEVENTF_KEYUP
    if vk in _EXTENDED_VKS:
        flags |= win32con.KEYEVENTF_EXTENDEDKEY
    win32api.keybd_event(vk, 0, flags, 0)


def press(hwnd: int, combo: str) -> None:
    """Press a key combination such as 'enter', 'ctrl+c', 'ctrl+shift+n'.

    The Windows key is intentionally unsupported: on this setup mstsc does not
    forward it, so it would trigger the LOCAL machine's Start menu instead.
    """
    if not ensure_focus(hwnd):
        raise RuntimeError("could not focus the RDP window before pressing keys")
    time.sleep(FOCUS_SETTLE)
    raw = combo.strip()
    # A one-character combo is the key itself, so '+' stays pressable.
    parts = [raw] if len(raw) == 1 else [
        p for p in raw.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError(f"no key to press in {combo!r}")
    # Resolve every token before pressing anything, so an unknown key cannot
    # leave a modifier stuck down inside the remote session.
    resolved = [_vk_mods_for(p) for p in parts]
    explicit = [vk for vk, _ in resolved]
    implicit = [m for _, mods in resolved for m in mods if m not in explicit]
    vks = implicit + explicit
    for vk in vks:                                  # press down in order
        _key_down(vk)
        time.sleep(0.03)
    for vk in reversed(vks):                        # release in reverse
        _key_up(vk)
        time.sleep(0.03)
