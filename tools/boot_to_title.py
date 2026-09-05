#!/usr/bin/env python3
"""Boot to title, then advance to the New Game menu — with gated inputs.

Gating (inputs are sent only when ALL hold):
- DISPCNT has display layers enabled and forced-blank is clear, AND
- the frame hash is stable across --stable polls (settled screen, not a
  transition). Layers-on alone does not prove title readiness.
After sending Start, the script waits for the frame to CHANGE (left the
title) before claiming the menu checkpoint; screenshots pin each stage.
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


def dispcnt_layers_on(client: Client) -> tuple[bool, str]:
    raw = client.read_io(0x04000000, 2)
    v = int.from_bytes(bytes.fromhex(raw), "little")
    forced_blank = bool(v & 0x0080)
    layers = bool(v & 0x1F00)  # BG0-3 + OBJ bits
    return (layers and not forced_blank, f"{v:#06x}")


def wait_display_ready(client: Client, proc: subprocess.Popen,
                       deadline_s: float, stable: int) -> dict:
    t0 = time.time()
    last: str | None = None
    run = 0
    while time.time() - t0 < deadline_s:
        check_alive(proc, "wait_display_ready")
        ok, reg = dispcnt_layers_on(client)
        h = client.screenshot_hash()
        if ok and h == last:
            run += 1
            if run >= stable:
                return {"dispcnt": reg, "stable_hash": h}
        else:
            run = 0
        last = h
        time.sleep(2.0)
    raise ProbeError(f"display never settled-ready within {deadline_s}s")


def tap(client: Client, keys: int):
    client.set_keys(keys)
    time.sleep(0.4)
    client.set_keys(RELEASED)
    time.sleep(0.4)


def wait_changed(client: Client, proc: subprocess.Popen, before: str,
                 deadline_s: float) -> str:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        check_alive(proc, "wait_changed")
        h = client.screenshot_hash()
        if h != before:
            return h
        time.sleep(1.0)
    raise ProbeError(f"frame unchanged {deadline_s}s after input "
                     f"(still {before[:12]}); input may not have registered")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=19894)
    ap.add_argument("--advance-timeout", type=float, default=180.0)
    ap.add_argument("--ready-timeout", type=float, default=600.0)
    ap.add_argument("--stable", type=int, default=3)
    ap.add_argument("--menu-timeout", type=float, default=60.0)
    args = ap.parse_args()
    rundir = new_run_dir(ROOT / "build" / "runs", "boot-title")
    proc = launch(ROOT / "build" / "MMBN3WhiteRecomp",
                  ROOT / "roms" / "mmbn3_white_usa.gba",
                  ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin",
                  args.port, rundir, ROOT)
    evidence: dict = {"inputs": []}
    client: Client | None = None
    try:
        client = Client(args.port, proc)
        client.resume()
        ev = wait_advancing(client, proc, args.advance_timeout)
        evidence["advancing_hash"] = ev["hash"][:16]
        ready = wait_display_ready(client, proc, args.ready_timeout,
                                   args.stable)
        evidence["title_ready"] = ready
        h0 = client.save_screenshot(rundir / "title.ppm")
        evidence["title_shot"] = {"file": "title.ppm", "sha256": h0}
        t = time.time()
        tap(client, START)
        evidence["inputs"].append({"at": round(t, 1), "keys": "START"})
        h1 = wait_changed(client, proc, h0, args.menu_timeout)
        client.save_screenshot(rundir / "menu.ppm")
        evidence["menu_shot"] = {"file": "menu.ppm", "sha256": h1}
        print(f"OK title={h0[:12]} menu={h1[:12]}", flush=True)
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
