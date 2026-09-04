#!/usr/bin/env python3
"""Dump EWRAM tail (IRQ flag area) after N seconds, native vs interp."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


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
            chunk = self.sock.recv(1 << 20)
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
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--port", type=int, default=19892)
    ap.add_argument("--interp", action="store_true")
    args = ap.parse_args()
    env = dict(os.environ)
    if args.interp:
        env["GBARECOMP_FORCE_INTERP"] = "1"
    proc = subprocess.Popen(
        [str(ROOT / "build" / "MMBN3WhiteRecomp"), "--tcp", str(args.port),
         "--rom", str(ROOT / "roms" / "mmbn3_white_usa.gba"),
         "--bios", str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin")],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        client = Client(args.port)
        try:
            time.sleep(args.seconds)
            # IWRAM top: polled flags live at mirrors 0x03FFFFF8 (->0x7FF8)
            # and 0x03FFFCFF (->0x7CFF). Dump 0x7C00..0x8000.
            r = client.call(cmd="read_iwram", addr=0x7C00, len=1024)
            assert r.get("ok"), r
            blob = bytes.fromhex(r["data"])
        finally:
            client.close()
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    tag = "interp" if args.interp else "native"
    out = ROOT / "build" / f"iwram_top_{tag}.bin"
    out.write_bytes(blob)
    nz = sum(1 for b in blob if b)
    print(f"{tag}: {len(blob)} bytes nonzero={nz}")
    for off in (0x7FF8 - 0x7C00, 0x7CFF - 0x7C00, 0x7FFC - 0x7C00):
        w = int.from_bytes(blob[off:off + 4], "little")
        print(f"  [iwram+{off + 0x7C00:#06x}] = {w:#010x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
