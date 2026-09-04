#!/usr/bin/env python3
"""Boot to title: screenshots + DISPCNT checks, taps only when display on."""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASED = 0x3FF
START = 0x3FF & ~0x008
A = 0x3FF & ~0x001


class Client:
    def __init__(self, port: int) -> None:
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 2.0)
                self.sock.settimeout(20.0)
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
    ap.add_argument("--port", type=int, default=19894)
    ap.add_argument("--rounds", type=int, default=14)
    ap.add_argument("--gap", type=float, default=45.0)
    args = ap.parse_args()
    proc = subprocess.Popen(
        [str(ROOT / "build" / "MMBN3WhiteRecomp"), "--tcp", str(args.port),
         "--rom", str(ROOT / "roms" / "mmbn3_white_usa.gba"),
         "--bios", str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin")],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    outdir = ROOT / "build" / "boot_title"
    outdir.mkdir(exist_ok=True)
    try:
        client = Client(args.port)
        try:
            for i in range(args.rounds):
                time.sleep(args.gap)
                disp = client.call(cmd="read_io", addr=0x04000000, len=2)
                r = client.call(cmd="screenshot")
                with open(outdir / f"bt_{i:02d}.ppm", "wb") as f:
                    f.write(f"P6\n{r['w']} {r['h']}\n255\n".encode()
                            + bytes.fromhex(r["data"]))
                print(f"[{i}] dispcnt={disp.get('data')} shot=bt_{i:02d}.ppm",
                      flush=True)
                # tap Start+A every round once display shows layers
                client.call(cmd="set_keyinput", value=START)
                time.sleep(0.3)
                client.call(cmd="set_keyinput", value=RELEASED)
                time.sleep(0.3)
                client.call(cmd="set_keyinput", value=A)
                time.sleep(0.3)
                client.call(cmd="set_keyinput", value=RELEASED)
        finally:
            client.close()
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
