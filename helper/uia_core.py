"""uia_core.py - the UI Automation observe/act engine for the win-rdp helper.

This is the "senses and hands" that replace pixel guessing. It reads the Windows
UI Automation (UIA) tree of a target window and hands back a list of NAMED
elements with exact screen rectangles; the model picks one by id, and this code
actuates it exactly (invoke / set value / keys / launch / last-resort click).

No network here on purpose. `poke.py` drives this by hand for Phase 0; the
dial-out helper (Phase 2) imports the same Session/observe/act and serves it
over a socket. Keeping the UIA logic network-free means a Phase 0 failure can
only be UIA, never the transport.

UIA REQUIRES a logged-in, unlocked, interactive desktop. It cannot run as a
session-0 service, and a locked or disconnected RDP session makes the tree go
blank. That is a property of Windows, not of this code.

Verified against uiautomation 2.0.29 (comtypes 1.4.16).
"""
from __future__ import annotations

import base64
import ctypes
import io
import os
import subprocess
from ctypes import wintypes

import uiautomation as auto

# uiautomation sets per-monitor DPI awareness on import; be explicit and first
# so BoundingRectangle values are physical pixels that match a screen grab.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# Enumerating an existing tree does not search, but keep any incidental search
# snappy so a slow or missing control cannot stall a whole observe().
try:
    auto.SetGlobalSearchTimeout(2)
except Exception:
    pass


# Control types the model can actually act on. This is the default filter for
# observe(); poke's --all bypasses it to dump every control when hunting for a
# UIA blind spot.
INTERACTIVE = {
    "ButtonControl", "CheckBoxControl", "RadioButtonControl", "ComboBoxControl",
    "EditControl", "DocumentControl", "HyperlinkControl", "ListItemControl",
    "MenuItemControl", "TabItemControl", "TreeItemControl", "SplitButtonControl",
}

MAX_ELEMENTS = 300     # a dialog has a handful; a full app window a few dozen
MAX_DEPTH = 30


class TargetError(Exception):
    """No usable target window (bad title/handle, or nothing in foreground)."""


# ---------------------------------------------------------------------------
# small ctypes helpers (no pywin32 dependency in the helper)
# ---------------------------------------------------------------------------
# Declare signatures so a 64-bit process HANDLE is not truncated to int32 by
# ctypes' default c_int return type (which would silently blank out app names).
_k32 = ctypes.windll.kernel32
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _proc_name(pid: int) -> str:
    """Best-effort image name (e.g. 'notepad.exe') for a process id."""
    if not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        _k32.CloseHandle(h)
    return ""


def _attr(ctrl, name, default=None):
    """Read a uiautomation property or call a method, swallowing COM errors.

    uiautomation exposes some things as properties (Name, IsEnabled) and some as
    methods (GetChildren). Any of them can raise a COMError if the control died
    between enumeration and read, so never let that abort an observe()."""
    try:
        v = getattr(ctrl, name)
        return v() if callable(v) else v
    except Exception:
        return default


def _rect(ctrl):
    """Return [x, y, w, h] in screen pixels, or None if degenerate/offscreen."""
    r = _attr(ctrl, "BoundingRectangle")
    if r is None:
        return None
    try:
        w, h = r.right - r.left, r.bottom - r.top
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return [int(r.left), int(r.top), int(w), int(h)]


def _value(ctrl) -> str:
    """A human-readable current value: edit text, or checkbox/radio state."""
    try:
        vp = ctrl.GetValuePattern()
        if vp is not None:
            v = vp.Value
            if v:
                return str(v)
    except Exception:
        pass
    try:
        tp = ctrl.GetTogglePattern()
        if tp is not None:
            return {0: "off", 1: "on", 2: "indeterminate"}.get(tp.ToggleState, "")
    except Exception:
        pass
    return ""


def _short_type(control_type_name: str) -> str:
    """'ButtonControl' -> 'Button'. Shorter tokens, less noise for the model."""
    return control_type_name[:-7] if control_type_name.endswith("Control") else control_type_name


# ---------------------------------------------------------------------------
# window selection
# ---------------------------------------------------------------------------
def _top_windows():
    """Top-level windows, in the z-order uiautomation returns them."""
    root = auto.GetRootControl()
    return _attr(root, "GetChildren", []) or []


def list_windows() -> list[dict]:
    """Every real top-level window: title, class, pid, app, rect, foreground.

    This is `poke windows` - it lets you see that the target dialog IS in the
    tree and grab its title/handle to address it precisely."""
    fg = auto.GetForegroundControl()
    fg_hwnd = _attr(fg, "NativeWindowHandle", 0) if fg else 0
    out = []
    for w in _top_windows():
        rect = _rect(w)
        if rect is None:
            continue
        hwnd = _attr(w, "NativeWindowHandle", 0)
        if not hwnd:
            continue
        pid = _attr(w, "ProcessId", 0)
        out.append({
            "hwnd": int(hwnd),
            "title": _attr(w, "Name", "") or "",
            "class": _attr(w, "ClassName", "") or "",
            "type": _short_type(_attr(w, "ControlTypeName", "") or ""),
            "pid": int(pid) if pid else 0,
            "app": _proc_name(pid),
            "rect": rect,
            "foreground": bool(hwnd == fg_hwnd),
        })
    return out


def _console_hwnd() -> int:
    try:
        c = auto.GetConsoleWindow()
        return int(_attr(c, "NativeWindowHandle", 0) or 0) if c else 0
    except Exception:
        return 0


def resolve_target(foreground: bool = True, title: str | None = None,
                   hwnd: int | None = None) -> "Session":
    """Pick the window to drive.

    hwnd     - exact native handle (most robust; observe prints one to reuse).
    title    - first top-level window whose title contains this substring.
    else     - the foreground window. If that is our own console (because you
               launched this from a terminal), fall back to the next real
               window, since the interesting thing is almost never the console.
               In the real dial-out helper the trigger arrives over a socket, so
               foreground is genuinely the app being driven.
    """
    if hwnd:
        c = auto.ControlFromHandle(int(hwnd))
        if not c:
            raise TargetError(f"no window with handle {hwnd}")
        return Session(c, int(hwnd))

    if title:
        needle = title.lower()
        for w in _top_windows():
            name = (_attr(w, "Name", "") or "").lower()
            if needle in name and _rect(w) is not None:
                return Session(w, int(_attr(w, "NativeWindowHandle", 0) or 0))
        raise TargetError(f"no top-level window title contains {title!r}")

    fg = auto.GetForegroundControl()
    if fg is None:
        raise TargetError("no foreground window")
    fg_hwnd = int(_attr(fg, "NativeWindowHandle", 0) or 0)
    if fg_hwnd and fg_hwnd == _console_hwnd():
        for w in _top_windows():
            h = int(_attr(w, "NativeWindowHandle", 0) or 0)
            if h and h != fg_hwnd and _rect(w) is not None and (_attr(w, "Name", "") or ""):
                return Session(w, h)
    return Session(fg, fg_hwnd)


# ---------------------------------------------------------------------------
# actuation
# ---------------------------------------------------------------------------
def _do_invoke(ctrl) -> None:
    """Perform the control's primary action, trying the richest pattern first
    and falling back to a real click on its center as the last resort."""
    for getter, call in (
        ("GetInvokePattern", lambda p: p.Invoke()),
        ("GetTogglePattern", lambda p: p.Toggle()),
        ("GetSelectionItemPattern", lambda p: p.Select()),
        ("GetExpandCollapsePattern", lambda p: p.Expand()),
    ):
        fn = getattr(ctrl, getter, None)
        if not fn:
            continue
        try:
            p = fn()
        except Exception:
            p = None
        if p is not None:
            try:
                call(p)
                return
            except Exception:
                pass
    # Fallback: uiautomation click at the control's center (screen pixels).
    ctrl.Click(simulateMove=False, waitTime=0)


def _do_set_value(ctrl, text: str) -> None:
    try:
        vp = ctrl.GetValuePattern()
        if vp is not None:
            vp.SetValue(text)
            return
    except Exception:
        pass
    # Fallback: focus and type. Escape SendKeys' special characters.
    try:
        ctrl.SetFocus()
    except Exception:
        pass
    auto.SendKeys("{Ctrl}a{Delete}", waitTime=0)
    auto.SendKeys(_escape_sendkeys(text), waitTime=0)


def _escape_sendkeys(text: str) -> str:
    """SendKeys reads {..} (..) + ^ % ~ as syntax. Escape them for literal text."""
    out = []
    for ch in text:
        if ch in "{}()+^%~":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


_MODS = {"ctrl": "{Ctrl}", "control": "{Ctrl}", "alt": "{Alt}",
         "shift": "{Shift}", "win": "{Win}", "windows": "{Win}"}
_NAMED = {
    "enter": "{Enter}", "return": "{Enter}", "tab": "{Tab}", "esc": "{Esc}",
    "escape": "{Esc}", "backspace": "{Back}", "back": "{Back}", "space": "{Space}",
    "delete": "{Delete}", "del": "{Delete}", "up": "{Up}", "down": "{Down}",
    "left": "{Left}", "right": "{Right}", "home": "{Home}", "end": "{End}",
    "pageup": "{PageUp}", "pagedown": "{PageDown}",
    **{f"f{i}": "{F%d}" % i for i in range(1, 13)},
}


def _translate_keys(combo: str) -> str:
    """'ctrl+n' -> '{Ctrl}n', 'ctrl+shift+s' -> '{Ctrl}{Shift}s', 'enter' -> '{Enter}'.

    SendKeys applies each {Mod} to the following key, so a chain of modifiers
    before one key chords them (verified in uiautomation's SendKeys docstring)."""
    raw = combo.strip()
    if not raw:
        raise ValueError("no keys given")
    if len(raw) == 1:
        return _escape_sendkeys(raw)
    parts = [p for p in raw.replace(" ", "").split("+") if p]
    if len(parts) == 1:
        p = parts[0].lower()
        return _NAMED.get(p, _escape_sendkeys(parts[0]))
    *mods, key = parts
    prefix = "".join(_MODS.get(m.lower(), "") for m in mods)
    if not prefix:
        raise ValueError(f"unrecognized modifier(s) in {combo!r}")
    if key.lower() in _NAMED:
        tail = _NAMED[key.lower()]
    elif len(key) == 1:
        tail = key
    else:
        raise ValueError(f"unrecognized key {key!r} in {combo!r}")
    return prefix + tail


_LAUNCH_ALIASES = {
    "notepad": "notepad.exe", "calc": "calc.exe", "calculator": "calc.exe",
    "cmd": "cmd.exe", "explorer": "explorer.exe", "mspaint": "mspaint.exe",
    "paint": "mspaint.exe", "wordpad": "write.exe", "write": "write.exe",
    "outlook": "outlook.exe", "word": "winword.exe", "excel": "excel.exe",
    "powerpoint": "powerpnt.exe", "edge": "msedge.exe", "chrome": "chrome.exe",
}


def _do_launch(app: str) -> None:
    exe = _LAUNCH_ALIASES.get(app.strip().lower(), app.strip())
    try:
        subprocess.Popen(exe, shell=False)
        return
    except FileNotFoundError:
        pass
    # Office / Store apps are not on PATH; let the shell resolve them.
    os.startfile(exe)  # noqa: S606 - launching a named app is the whole point


# ---------------------------------------------------------------------------
# the observe/act session
# ---------------------------------------------------------------------------
class Session:
    """A chosen target window plus the id->control map from its last observe().

    The helper keeps one Session alive across many observe/act calls, so ids
    stay valid without re-walking. poke, being one-shot per process, builds a
    Session, observe()s to populate the map, then act()s - all in one run."""

    def __init__(self, control, hwnd: int):
        self.control = control
        self.hwnd = int(hwnd or 0)
        self._map: dict[int, object] = {}

    def foreground_info(self) -> dict:
        pid = _attr(self.control, "ProcessId", 0)
        return {
            "title": _attr(self.control, "Name", "") or "",
            "control_type": _short_type(_attr(self.control, "ControlTypeName", "") or ""),
            "app": _proc_name(pid),
            "hwnd": self.hwnd,
        }

    def observe(self, screenshot: bool = False, include_all: bool = False) -> dict:
        """Enumerate the target's interactive elements in deterministic
        pre-order, assigning integer ids 0..N. Rebuilds the id->control map."""
        self._map.clear()
        elements = []
        idx = 0
        for c, _depth in auto.WalkControl(self.control, includeTop=False,
                                          maxDepth=MAX_DEPTH):
            ctn = _attr(c, "ControlTypeName", "") or ""
            if not include_all and ctn not in INTERACTIVE:
                continue
            if _attr(c, "IsOffscreen", False):
                continue
            rect = _rect(c)
            if rect is None:
                continue
            name = _attr(c, "Name", "") or _attr(c, "AutomationId", "") or ""
            elements.append({
                "id": idx,
                "type": _short_type(ctn),
                "name": name,
                "rect": rect,
                "enabled": bool(_attr(c, "IsEnabled", True)),
                "default": bool(_attr(c, "HasKeyboardFocus", False)),
                "value": _value(c),
            })
            self._map[idx] = c
            idx += 1
            if idx >= MAX_ELEMENTS:
                break
        out = {"foreground": self.foreground_info(), "elements": elements}
        if screenshot:
            out["screenshot_b64"] = self._screenshot()
        return out

    def _screenshot(self) -> str:
        try:
            from PIL import ImageGrab
            r = _attr(self.control, "BoundingRectangle")
            box = (r.left, r.top, r.right, r.bottom) if r else None
            img = ImageGrab.grab(bbox=box, all_screens=True)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            return ""

    def _resolve(self, elem_id):
        if elem_id not in self._map:
            raise TargetError(
                f"no element id {elem_id} in the last observe "
                f"(ids are 0..{len(self._map) - 1 if self._map else -1}); observe again")
        return self._map[elem_id]

    def act(self, a: dict) -> dict:
        """Perform one action. Returns {'ok': bool, 'error'?: str}."""
        action = (a.get("action") or "").strip()
        try:
            if action == "invoke":
                _do_invoke(self._resolve(int(a["id"])))
            elif action == "set_value":
                _do_set_value(self._resolve(int(a["id"])), str(a.get("text", "")))
            elif action == "keys":
                auto.SendKeys(_translate_keys(str(a.get("keys", ""))), waitTime=0)
            elif action == "launch":
                _do_launch(str(a.get("app", "")))
            elif action == "click_xy":
                auto.Click(int(a["x"]), int(a["y"]), waitTime=0)
            else:
                return {"ok": False, "error": f"unknown action {action!r}"}
        except TargetError as e:
            return {"ok": False, "error": str(e)}
        except KeyError as e:
            return {"ok": False, "error": f"missing field {e} for action {action!r}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True}
