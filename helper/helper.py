"""helper.py - the in-VM dial-out agent (Phase 2). Frozen to helper.exe.

It runs inside a VM's interactive desktop and serves observe/act (from uia_core)
to the controller over a token-gated socket. By default it DIALS OUT to the
controller, so the VM needs no inbound firewall rule; it also reconnects if the
link drops. A --listen mode is offered for local testing.

    helper.exe --connect CONTROLLER:PORT --token SECRET --host-label vm-1
    helper.exe --listen 127.0.0.1:8765 --token SECRET        # local test

UIA REQUIRES a logged-in, unlocked, interactive desktop. It cannot run as a
session-0 service, and a locked/disconnected RDP session makes the tree blank.

SECURITY: this is a remote-control agent running as the logged-in user. The
shared token is the only gate today. Lab use only until hardened (mTLS,
controller allowlist, signed binary).
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

import protocol

VERSION = protocol.PROTOCOL_VERSION


class HelperState:
    def __init__(self, token: str, label: str):
        self.token = token
        self.label = label or socket.gethostname()
        self.session = None  # current uia_core.Session, set by observe


def handle_request(req: dict, st: HelperState) -> dict:
    if req.get("token") != st.token:
        return {"ok": False, "error": "unauthorized"}
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "pong": True, "host": st.label, "version": VERSION}

    # uia_core is imported here (not at top) so a bad UIA stack surfaces per
    # request instead of stopping the helper from ever connecting.
    import uia_core

    if op == "observe":
        st.session = uia_core.resolve_target(
            foreground=not (req.get("title") or req.get("hwnd")),
            title=req.get("title"),
            hwnd=req.get("hwnd"),
        )
        data = st.session.observe(screenshot=bool(req.get("screenshot", False)),
                                  include_all=bool(req.get("all", False)))
        return {"ok": True, **data}

    if op == "act":
        a = req.get("a") or {}
        if not isinstance(a, dict):
            return {"ok": False, "error": "'a' must be an action object"}
        # launch/keys/click_xy need no target window; give them a bare session.
        if st.session is None:
            st.session = uia_core.Session(None, 0)
        return st.session.act(a)

    return {"ok": False, "error": f"unknown op {op!r}"}


def serve_conn(rfile, wfile, st: HelperState, hello: dict | None = None) -> None:
    """Serve framed requests on one connection until it closes."""
    if hello is not None:
        wfile.write(protocol.encode(hello))
        wfile.flush()
    for line in rfile:
        if not line.strip():
            continue
        try:
            req = protocol.decode(line)
        except Exception as e:
            resp = {"ok": False, "error": f"bad json: {e}"}
        else:
            try:
                resp = handle_request(req, st)
            except Exception as e:
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        wfile.write(protocol.encode(resp))
        wfile.flush()


def run_connect(host: str, port: int, st: HelperState, backoff: float = 5.0) -> int:
    print(f"[helper] {st.label} dialing controller {host}:{port}")
    while True:
        conn = None
        try:
            conn = socket.create_connection((host, port), timeout=15)
            rfile, wfile = conn.makefile("rb"), conn.makefile("wb")
            hello = {"type": "hello", "token": st.token, "host": st.label,
                     "version": VERSION}
            print(f"[helper] connected; serving as '{st.label}'")
            serve_conn(rfile, wfile, st, hello=hello)
            print("[helper] controller closed the connection")
        except KeyboardInterrupt:
            print("\n[helper] stopped")
            return 0
        except Exception as e:
            print(f"[helper] connection failed: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        print(f"[helper] reconnecting in {backoff:g}s")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            print("\n[helper] stopped")
            return 0


def run_listen(host: str, port: int, st: HelperState) -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    print(f"[helper] {st.label} listening on {host}:{port}")
    try:
        while True:
            conn, addr = srv.accept()
            print(f"[helper] connection from {addr[0]}:{addr[1]}")
            try:
                serve_conn(conn.makefile("rb"), conn.makefile("wb"), st)
            except Exception as e:
                print(f"[helper] connection error: {e}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[helper] stopped")
        return 0


def _hostport(s: str, default_port: int = protocol.DEFAULT_PORT):
    s = s.strip()
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        return host, int(rest.lstrip(":")) if rest.lstrip(":") else default_port
    if ":" in s:
        host, _, p = s.rpartition(":")
        return host, int(p) if p else default_port
    return s, default_port


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="helper",
                                description="in-VM UIA dial-out agent")
    p.add_argument("--version", action="version", version=f"helper {VERSION}")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--connect", metavar="HOST:PORT", help="dial out to the controller")
    g.add_argument("--listen", metavar="HOST:PORT", help="listen locally (testing)")
    p.add_argument("--token", default=os.environ.get("WIN_RDP_HELPER_TOKEN", ""),
                   help="shared auth token (or WIN_RDP_HELPER_TOKEN)")
    p.add_argument("--host-label", default="", help="name reported to the controller")
    args = p.parse_args(argv)
    if not args.token:
        raise SystemExit("helper needs a --token (or set WIN_RDP_HELPER_TOKEN); "
                         "the channel is token-gated from day one.")
    st = HelperState(token=args.token, label=args.host_label)
    if args.connect:
        host, port = _hostport(args.connect)
        return run_connect(host, port, st)
    host, port = _hostport(args.listen)
    return run_listen(host, port, st)


if __name__ == "__main__":
    sys.exit(main())
