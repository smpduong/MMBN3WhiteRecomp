#!/usr/bin/env python3
"""Screenshot helper: save current frame as PPM via TCP screenshot."""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=19894)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    sock = socket.create_connection(("127.0.0.1", args.port), 5.0)
    sock.sendall(b'{"cmd":"screenshot"}\n')
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(1 << 20)
        if not chunk:
            raise RuntimeError("closed")
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    resp = json.loads(line.decode())
    assert resp.get("ok"), resp
    w, h = resp["w"], resp["h"]
    rgb = bytes.fromhex(resp["data"])
    assert len(rgb) == w * h * 3, (len(rgb), w, h)
    with open(args.out, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode() + rgb)
    print(f"saved {args.out} {w}x{h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
