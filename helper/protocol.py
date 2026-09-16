"""protocol.py - the wire format shared by the in-VM helper and the controller.

One newline-terminated JSON object per message, in both directions. Kept in one
file so the two ends cannot drift. Deliberately tiny and transport-agnostic:
the helper uses blocking sockets, the controller uses asyncio streams, and both
just need encode()/decode() plus the frame vocabulary below.

Frames
------
helper -> controller, once on connect:
    {"type":"hello","token":T,"host":LABEL,"version":V}

controller -> helper, per request (token repeated for defence in depth):
    {"op":"ping","token":T}
    {"op":"observe","token":T,"window":"foreground"|null,"title":str|null,
     "hwnd":int|null,"screenshot":bool,"all":bool}
    {"op":"act","token":T,"a":{...one action...}}

helper -> controller, per response:
    observe -> {"ok":true,"foreground":{...},"elements":[...],"screenshot_b64"?:...}
    act     -> {"ok":true} | {"ok":false,"error":str}
    ping    -> {"ok":true,"pong":true,"host":LABEL,"version":V}
"""
from __future__ import annotations

import json

PROTOCOL_VERSION = "1"
DEFAULT_PORT = 8765


def encode(obj: dict) -> bytes:
    """One frame as bytes, ready to write to a socket/stream."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line) -> dict:
    """Parse one frame (bytes or str). Raises ValueError on bad JSON."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8")
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("frame is not a JSON object")
    return obj
