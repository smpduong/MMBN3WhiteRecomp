#!/usr/bin/env python3
"""Drive title->menu->new-game with periodic button taps via TCP keyinput.

KEYINPUT is active-low; 0x3FF = nothing pressed.
Usage: python3 tools/play_inputs.py [--wait 35] [--rounds 150] [--port 19894]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASED = 0x3FF
TAPS = [
    0x3FF & ~0x008,  # Start (title dismiss)
    0x3FF & ~0x001,  # A (confirm)
    0x3FF & ~0x040,  # Down (menu)
    0x3FF & ~0x001,  # A
    0x3FF & ~0x010,  # Right
    0x3FF & ~0x020,  # Left
    0x3FF & ~0x080,  # Up
    0x3FF & ~0x002,  # B (cancel/back)
]


class Client:
    def __init__(self, port: int) -> None:
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 2.0)
                self.sock.settimeout(15.0)
                self.buf = b""
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("no TCP")

    def call(self, **req):
        self.sock.sendall(json.dumps(req).encode() + b"\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line.decode())

    def close(self):
        try:
            self.call(cmd="quit")
        except (OSError, RuntimeError):
            pass
        self.sock.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=35.0)
    ap.add_argument("--rounds", type=int, default=150)
    ap.add_argument("--port", type=int, default=19894)
    args = ap.parse_args()
    proc = subprocess.Popen(
        [str(ROOT / "build" / "MMBN3WhiteRecomp"), "--tcp", str(args.port),
         "--rom", str(ROOT / "roms" / "mmbn3_white_usa.gba"),
         "--bios", str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin")],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        client = Client(args.port)
        try:
            time.sleep(args.wait)
            for i in range(args.rounds):
                client.call(cmd="set_keyinput", value=TAPS[i % len(TAPS)])
                time.sleep(0.25)
                client.call(cmd="set_keyinput", value=RELEASED)
                time.sleep(0.75)
        finally:
            client.close()
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    print("input run done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
