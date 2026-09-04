#!/usr/bin/env python3
"""Snapshot TCP debug rings after N seconds: native vs interp comparison."""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import subprocess
import time


ROOT = pathlib.Path(__file__).resolve().parent.parent


class Client:
    def __init__(self, port: int) -> None:
        deadline = time.time() + 20.0
        last_error: OSError | None = None
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 2.0)
                self.sock.settimeout(15.0)
                self.buf = b""
                return
            except OSError as error:
                last_error = error
                time.sleep(0.2)
        raise RuntimeError(f"no TCP on {port}: {last_error}")

    def call(self, **request: object):
        self.sock.sendall(json.dumps(request).encode() + b"\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise RuntimeError("socket closed")
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
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--port", type=int, default=19890)
    ap.add_argument("--interp", action="store_true")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()

    env = dict(__import__("os").environ)
    if args.interp:
        env["GBARECOMP_FORCE_INTERP"] = "1"

    proc = subprocess.Popen(
        [str(ROOT / "build" / "MMBN3WhiteRecomp"), "--tcp", str(args.port),
         "--rom", str(ROOT / "roms" / "mmbn3_white_usa.gba"),
         "--bios", str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin")],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    snap: dict = {"mode": "interp" if args.interp else "native"}
    try:
        client = Client(args.port)
        try:
            time.sleep(args.seconds)
            for cmd in ("misses", "counters", "registers", "irq_cap",
                        "state_hash", "runtime_trace"):
                try:
                    snap[cmd] = client.call(cmd=cmd)
                except (OSError, RuntimeError) as e:
                    snap[cmd] = {"ok": False, "error": str(e)}
        finally:
            client.close()
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    args.out.write_text(json.dumps(snap, indent=1)[:200000])
    print(f"wrote {args.out} "
          f"misses={str(snap.get('misses'))[:100]} "
          f"hash={str(snap.get('state_hash'))[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
