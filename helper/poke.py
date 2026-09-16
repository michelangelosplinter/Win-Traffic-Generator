"""poke.py - a tiny CLI to drive uia_core by hand. This is the Phase 0 tool.

It runs ON a target VM (frozen to poke.exe, since the VMs have no Python) and
lets you exercise observe/act without any network, model, or controller - so a
result is unambiguous: either UIA can see and click the element, or it cannot.

Phase 0 recipe (prove UIA on the Windows Activation dialog):
  1. poke.exe windows                       # find the dialog, note its hwnd
  2. poke.exe observe --hwnd <H>            # list its buttons with ids + rects
  3. poke.exe invoke <id> --hwnd <H>       # click "Ask me later" -> it closes
  4. poke.exe windows                       # confirm the dialog is gone

Target selection (observe/invoke/set-value/keys):
  --hwnd <N>     exact window handle (most robust; observe prints one to reuse)
  --title <str>  first top-level window whose title contains this substring
  (default)      the foreground window (skips this tool's own console)
"""
from __future__ import annotations

import argparse
import json
import sys

import uia_core


def _fmt_rect(r):
    return f"({r[0]},{r[1]} {r[2]}x{r[3]})"


def cmd_windows(args) -> int:
    wins = uia_core.list_windows()
    print(f"top-level windows ({len(wins)}):")
    for w in wins:
        fg = "  <-- FOREGROUND" if w["foreground"] else ""
        title = w["title"] or "(no title)"
        print(f'  hwnd={w["hwnd"]:<9} pid={w["pid"]:<6} {w["app"] or "?":<16} '
              f'[{w["type"]}] {_fmt_rect(w["rect"])}  "{title}"{fg}')
    if not wins:
        print("  (none - is the desktop locked? UIA needs an unlocked session)")
    return 0


def _target(args) -> uia_core.Session:
    return uia_core.resolve_target(
        foreground=not (args.title or args.hwnd),
        title=args.title,
        hwnd=args.hwnd,
    )


def cmd_observe(args) -> int:
    sess = _target(args)
    data = sess.observe(screenshot=bool(args.shot), include_all=bool(args.all))
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    fg = data["foreground"]
    print(f'foreground: "{fg["title"]}"  [{fg["control_type"]}]  '
          f'app={fg["app"] or "?"}  hwnd={fg["hwnd"]}')
    els = data["elements"]
    print(f"elements ({len(els)}){'  [ALL controls]' if args.all else ''}:")
    for e in els:
        flags = []
        flags.append("enabled" if e["enabled"] else "DISABLED")
        if e["default"]:
            flags.append("*focus")
        if e["value"]:
            flags.append(f'val={e["value"]!r}')
        print(f'  [{e["id"]:>3}] {e["type"]:<12} {_fmt_rect(e["rect"]):<18} '
              f'"{e["name"]}"  {" ".join(flags)}')
    if args.shot and data.get("screenshot_b64"):
        import base64
        with open(args.shot, "wb") as fh:
            fh.write(base64.b64decode(data["screenshot_b64"]))
        print(f"[shot] wrote {args.shot}")
    if not els:
        print("  (no interactive elements - try --all to dump every control)")
    else:
        h = data["foreground"]["hwnd"]
        print(f"\nhint: poke invoke <id> --hwnd {h}")
    return 0


def _act(args, action_dict, focus_first=False) -> int:
    sess = _target(args)
    sess.observe()  # populate the id->control map for this window
    if focus_first:
        try:
            sess.control.SetFocus()
        except Exception:
            pass
    res = sess.act(action_dict)
    if res.get("ok"):
        print(f"OK: {action_dict['action']} {_compact(action_dict)}")
        return 0
    print(f"FAILED: {res.get('error', 'unknown error')}")
    return 1


def _compact(d):
    return " ".join(f"{k}={v!r}" for k, v in d.items() if k != "action")


def cmd_invoke(args) -> int:
    return _act(args, {"action": "invoke", "id": args.id})


def cmd_set_value(args) -> int:
    return _act(args, {"action": "set_value", "id": args.id, "text": args.text})


def cmd_keys(args) -> int:
    return _act(args, {"action": "keys", "keys": args.keys}, focus_first=True)


def cmd_launch(args) -> int:
    sess_action = {"action": "launch", "app": args.app}
    # launch needs no target window; act() on a throwaway session is fine
    res = uia_core.Session(None, 0).act(sess_action)
    print("OK: launch " + repr(args.app) if res.get("ok")
          else f"FAILED: {res.get('error')}")
    return 0 if res.get("ok") else 1


def cmd_click_xy(args) -> int:
    res = uia_core.Session(None, 0).act(
        {"action": "click_xy", "x": args.x, "y": args.y})
    print(f"OK: click_xy ({args.x},{args.y})" if res.get("ok")
          else f"FAILED: {res.get('error')}")
    return 0 if res.get("ok") else 1


def _add_target_opts(p):
    p.add_argument("--title", default=None, help="target window by title substring")
    p.add_argument("--hwnd", type=int, default=None, help="target window by handle")
    p.add_argument("--foreground", action="store_true",
                   help="target the foreground window (the default)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="poke", description="hand-drive the win-rdp UIA helper (Phase 0)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("windows", help="list all top-level windows")
    sp.set_defaults(func=cmd_windows)

    sp = sub.add_parser("observe", help="list a window's interactive elements")
    _add_target_opts(sp)
    sp.add_argument("--all", action="store_true", help="dump EVERY control, not just interactive")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument("--shot", default=None, metavar="FILE", help="also save a PNG screenshot")
    sp.set_defaults(func=cmd_observe)

    sp = sub.add_parser("invoke", help="click/invoke an element by id")
    sp.add_argument("id", type=int)
    _add_target_opts(sp)
    sp.set_defaults(func=cmd_invoke)

    sp = sub.add_parser("set-value", help="set an element's text by id")
    sp.add_argument("id", type=int)
    sp.add_argument("text")
    _add_target_opts(sp)
    sp.set_defaults(func=cmd_set_value)

    sp = sub.add_parser("keys", help="send a key combo, e.g. ctrl+n or enter")
    sp.add_argument("keys")
    _add_target_opts(sp)
    sp.set_defaults(func=cmd_keys)

    sp = sub.add_parser("launch", help="launch an app, e.g. notepad")
    sp.add_argument("app")
    sp.set_defaults(func=cmd_launch)

    sp = sub.add_parser("click-xy", help="last-resort raw click at screen x y")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.set_defaults(func=cmd_click_xy)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except uia_core.TargetError as e:
        print(f"FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
