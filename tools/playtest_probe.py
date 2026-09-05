#!/usr/bin/env python3
"""Playtest probe: resume, verify advancement, screenshot + tap rounds.

Each run lands in build/runs/playtest-<timestamp>/ with stdout.log,
stderr.log, command.txt, exit_code.txt and run.json.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _probe_common import (ROOT, RELEASED, ProbeError, Client, new_run_dir,
                           launch, check_alive, wait_advancing, cleanup)

START = 0x3FF & ~0x008
A = 0x3FF & ~0x001


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=19894)
    ap.add_argument("--shots", type=int, default=8)
    ap.add_argument("--gap", type=float, default=30.0)
    ap.add_argument("--advance-timeout", type=float, default=120.0)
    args = ap.parse_args()
    rundir = new_run_dir(ROOT / "build" / "runs", "playtest")
    proc = launch(ROOT / "build" / "MMBN3WhiteRecomp",
                  ROOT / "roms" / "mmbn3_white_usa.gba",
                  ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin",
                  args.port, rundir, ROOT)
    evidence: dict = {"shots": []}
    client: Client | None = None
    try:
        client = Client(args.port, proc)
        client.resume()  # leaves RS_PAUSED; response confirmed inside
        ev = wait_advancing(client, proc, args.advance_timeout)
        evidence["advancing"] = {k: (v if k != "misses" else v)
                                 for k, v in ev.items()}
        for i in range(args.shots):
            deadline = time.time() + args.gap
            while time.time() < deadline:
                check_alive(proc, f"gap {i}")
                time.sleep(2.0)
            h = client.save_screenshot(rundir / f"shot_{i:02d}.ppm")
            for keys in (START, RELEASED, A, RELEASED):
                client.set_keys(keys)
                time.sleep(0.3)
            evidence["shots"].append({"file": f"shot_{i:02d}.ppm",
                                     "sha256": h})
            print(f"shot {i} {h[:12]}", flush=True)
    except ProbeError as e:
        evidence["failure"] = str(e)
        print(f"FAILED: {e}", flush=True)
        cleanup(client, proc, rundir)
        with open(rundir / "run.json", "w") as f:
            json.dump(evidence, f, indent=1)
        return 1
    cleanup(client, proc, rundir)
    with open(rundir / "run.json", "w") as f:
        json.dump(evidence, f, indent=1)
    print(f"OK: {rundir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
