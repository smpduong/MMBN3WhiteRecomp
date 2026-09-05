#!/usr/bin/env python3
"""KEYINPUT round-trip: set values via TCP, read back the register."""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _probe_common import ROOT, RELEASED, ProbeError, Client

EXE = ROOT / "build" / "MMBN3WhiteRecomp"
ROM = ROOT / "roms" / "mmbn3_white_usa.gba"
BIOS = ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin"

# (name, KEYINPUT value to drive)
CASES = [
    ("released", 0x3FF),
    ("A", 0x3FF & ~0x001),
    ("B", 0x3FF & ~0x002),
    ("Select", 0x3FF & ~0x004),
    ("Start", 0x3FF & ~0x008),
    ("Right", 0x3FF & ~0x010),
    ("Left", 0x3FF & ~0x020),
    ("Up", 0x3FF & ~0x040),
    ("Down", 0x3FF & ~0x080),
    ("R", 0x3FF & ~0x100),
    ("L", 0x3FF & ~0x200),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=19897)
    args = ap.parse_args()
    proc = subprocess.Popen(
        [str(EXE), "--tcp", str(args.port),
         "--rom", str(ROM), "--bios", str(BIOS)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []
    try:
        client = Client(args.port, proc)
        try:
            client.resume()
            for name, want in CASES:
                client.set_keys(want)
                time.sleep(0.2)
                raw = bytes.fromhex(client.read_io(0x04000130, 2))
                got = int.from_bytes(raw, "little")
                # KEYINPUT has only 10 bits; upper bits read high.
                ok = (got & 0x3FF) == want
                print(f"{name:9s} want={want:#05x} got={got:#06x} "
                      f"{'ok' if ok else 'MISMATCH'}", flush=True)
                if not ok:
                    failures.append(name)
        finally:
            client.close()
    except ProbeError as e:
        print(f"FAILED: {e}", flush=True)
        return 2
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if failures:
        print(f"FAILED keys: {failures}", flush=True)
        return 1
    print("OK: all 11 KEYINPUT identities round-trip", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
