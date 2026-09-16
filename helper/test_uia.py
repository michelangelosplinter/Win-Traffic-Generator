"""test_uia.py - a self-contained smoke test for uia_core.

Run it wherever there is an unlocked interactive desktop and Python + the helper
deps (i.e. the controller). It proves the UIA library works on THIS machine
before you ever build the exe or touch a VM. It is not the Phase 0 acceptance
test (that is the Windows Activation dialog on a real VM via poke.exe) - it is a
cheap "is the plumbing sound" check.

  .venv-helper\\Scripts\\python.exe test_uia.py

Exit code 0 = all hard checks passed.
"""
from __future__ import annotations

import sys
import time

import uia_core

PASS, FAIL = "PASS", "FAIL"
_hard_failures = []


def check(label, ok, detail=""):
    mark = PASS if ok else FAIL
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        _hard_failures.append(label)
    return ok


def warn(label, ok, detail=""):
    print(f"  [{'ok ' if ok else 'warn'}] {label}" + (f"  ({detail})" if detail else ""))


def test_key_translation():
    print("key translation (pure, no COM):")
    cases = {
        "ctrl+n": "{Ctrl}n",
        "ctrl+shift+s": "{Ctrl}{Shift}s",
        "alt+f4": "{Alt}{F4}",
        "enter": "{Enter}",
        "esc": "{Esc}",
        "ctrl+a": "{Ctrl}a",
        "f5": "{F5}",
        "tab": "{Tab}",
    }
    for combo, expected in cases.items():
        got = uia_core._translate_keys(combo)
        check(f"{combo!r} -> {expected!r}", got == expected, f"got {got!r}")


def test_list_windows():
    print("list_windows():")
    wins = uia_core.list_windows()
    check("returns at least one top-level window", len(wins) > 0, f"{len(wins)} windows")
    fg = [w for w in wins if w["foreground"]]
    warn("exactly one foreground window", len(fg) == 1, f"{len(fg)} marked foreground")
    return wins


def test_observe_and_act():
    print("launch + observe + act (Notepad):")
    try:
        uia_core._do_launch("notepad")
    except Exception as e:
        check("launch notepad", False, str(e))
        return
    check("launch notepad", True)

    sess = None
    for _ in range(16):  # up to ~8s
        time.sleep(0.5)
        try:
            sess = uia_core.resolve_target(foreground=False, title="Notepad")
            break
        except uia_core.TargetError:
            continue
    if not check("find the Notepad window by title", sess is not None):
        return

    data = sess.observe()
    els = data["elements"]
    check("observe returns interactive elements", len(els) > 0, f"{len(els)} elements")
    print(f"      foreground: {data['foreground']}")
    for e in els[:12]:
        print(f"      [{e['id']:>3}] {e['type']:<11} \"{e['name']}\" "
              f"{tuple(e['rect'])} {'enabled' if e['enabled'] else 'DISABLED'}")

    # Soft: type into the edit surface and read it back. Win11 Notepad is a
    # RichEdit and may not honour ValuePattern.SetValue; that is fine to warn on.
    edit = next((e for e in els if e["type"] in ("Edit", "Document")), None)
    if edit:
        sess.act({"action": "set_value", "id": edit["id"], "text": "PHASE0 OK"})
        time.sleep(0.4)
        after = sess.observe()["elements"]
        val = next((e["value"] for e in after if e["id"] == edit["id"]), "")
        warn("set_value landed in the edit surface", "PHASE0" in (val or ""),
             f"value now {val!r}")
    else:
        warn("found an edit surface to type into", False, "none in the tree")

    # Clean up: close via keys (Alt+F4). Best-effort; a leftover Notepad is harmless.
    try:
        sess.control.SetFocus()
        import uiautomation as auto
        auto.SendKeys("{Alt}{F4}", waitTime=0)
        time.sleep(0.4)
        auto.SendKeys("{Right}{Enter}", waitTime=0)  # "Don't save" if prompted
    except Exception:
        pass


def main() -> int:
    print("=" * 60)
    print("uia_core smoke test")
    print("=" * 60)
    test_key_translation()
    print()
    test_list_windows()
    print()
    test_observe_and_act()
    print()
    print("=" * 60)
    if _hard_failures:
        print(f"RESULT: FAIL - {len(_hard_failures)} hard check(s) failed:")
        for f in _hard_failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS - UIA plumbing is sound on this machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
