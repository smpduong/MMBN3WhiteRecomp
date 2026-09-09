#!/usr/bin/env python3
"""Windowed replay + assist-script run with full session record (criteria C/D).

One invocation = one genuinely windowed run (--window is explicit; a missing
window is a failure, not a silent headless fallback). Guest input comes from
an actual frame-indexed trace (GBARECOMP_INPUT_REPLAY); host save/load/rewind
come from GBARECOMP_ASSIST_SCRIPT through the same host-control queue as
window keys (the debugger path bypasses that queue, so this is the route
under test). No TCP is used.

Isolation: per-run ROM copy (slot states land beside the ROM), disposable
save copy (source hashed before/after, never written), per-run heal cache
(empty / seeded copy / seeded from a previous run's cache), cwd=run dir,
retained stdout/stderr, monotonic timeout, child-PID-only teardown. Forced
termination FAILS the run.

Analysis retained in result.json: presented frames vs requested, audio-device
init line, assist/state-control log lines in order, frame-phase CSV row count
vs presented frames (the limit-frame dump fix), present-interval distribution
(row sums = present-to-present wall time; telemetry claim checked), hitch
correlation with compile_us, audio underrun/overflow/fill extrema, preload
and heal counters, guest frame ID span.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TRACE_HEADER = "# gbarecomp-keyinput-v1\n# frame,keyinput_active_low\n"


def build_trace(path):
    # Net-area walk: UP held throughout (movediag-proven), with same-frame
    # duplicate pairs (last-wins rule) spread across the run. Effective input
    # never changes, so duplicate handling is observable in logs without
    # perturbing the route.
    events = [(0, 0x03BF)]
    for f in (10000, 20000, 30000, 40000, 50000, 60000):
        events.append((f, 0x03BF))
        events.append((f, 0x03BF))
    with open(path, "w") as f:
        f.write(TRACE_HEADER)
        for frame, keys in events:
            f.write(f"{frame},0x{keys:04X}\n")
    return events


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_rev(d):
    try:
        out = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def parse_intervals(csv_path):
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    intervals, hitched, compile_us = [], 0, []
    frames = []
    for r in rows:
        try:
            vals = [int(r[c]) for c in
                    ("guest_us", "render_us", "present_us", "audio_us",
                     "pump_us", "pacer_us")]
        except (KeyError, ValueError):
            continue
        total = sum(vals)
        cu = int(r.get("compile_us", 0))
        intervals.append(total / 1000.0)
        compile_us.append(cu)
        frames.append(int(r["frame"]))
        if total / 1000.0 > 25.0:
            hitched += 1
    return rows, intervals, hitched, compile_us, frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--cache-mode", choices=["empty", "seed", "seed-from"],
                    default="seed")
    ap.add_argument("--seed-from", type=Path, default=None)
    ap.add_argument("--load-state", type=Path, required=True)
    ap.add_argument("--save-src", type=Path, required=True)
    ap.add_argument("--assist", default="")
    ap.add_argument("--warm-load", action="store_true",
                    help="preload the heal cache (default: bypassed). "
                         "D runs pass --warm-load (the empty-vs-populated "
                         "contrast is the measurement); C runs leave it off "
                         "so the shutdown join is not wedged by the preload "
                         "backlog (sampled in gate1-B7).")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (args.out or (ROOT / "build" / "winreplay" / f"{args.label}-{stamp}")).resolve()
    args.save_src = args.save_src.resolve()
    args.load_state = args.load_state.resolve()
    if args.seed_from is not None:
        args.seed_from = args.seed_from.resolve()
    out.mkdir(parents=True, exist_ok=False)
    res = {"label": args.label, "ok": False}
    t0 = time.monotonic()

    def note(msg):
        print(msg, flush=True)
        with open(out / "driver.log", "a") as f:
            f.write(msg + "\n")

    src_before = sha256_file(args.save_src)
    test_sav = out / "test.sav"
    shutil.copyfile(args.save_src, test_sav)
    rom = out / "mmbn3_white_usa.gba"
    shutil.copyfile(ROOT / "roms" / "mmbn3_white_usa.gba", rom)
    rom_ok = (hashlib.sha1(rom.read_bytes()).hexdigest()
              == "ff45038ae6d01cde4eae25a02dcb8bed29e07a6f")
    assert rom_ok, "run ROM copy hash mismatch"
    state = out / "load.state"
    shutil.copyfile(args.load_state, state)
    trace_path = out / "input-replay.csv"
    events = build_trace(trace_path)

    cache_dir = out / "heal-cache"
    if args.cache_mode == "empty":
        cache_dir.mkdir()
        cache_note = "empty (cold: on-demand compilation expected)"
    elif args.cache_mode == "seed-from":
        shutil.copytree(args.seed_from, cache_dir)
        cache_note = f"seeded from {args.seed_from}"
    else:
        shutil.copytree(ROOT / "recomp_cache", cache_dir)
        cache_note = "seeded from shared recomp_cache (write-isolated)"
    note(f"cache: {cache_note}")

    frame_phase = out / "frame-phase.csv"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GBARECOMP_")}
    overrides = {
        "GBARECOMP_HEAL_CACHE": str(cache_dir),
        "GBARECOMP_HEAL_WARM_LOAD": "1" if args.warm_load else "0",
        "GBARECOMP_INPUT_REPLAY": str(trace_path),
        "GBARECOMP_FRAME_PHASE": str(frame_phase),
        "GBARECOMP_EVENT_PROBE": "1",
        "GBARECOMP_AUDIO_PROBE": "1",
    }
    if args.assist:
        overrides["GBARECOMP_ASSIST_SCRIPT"] = args.assist
    env.update(overrides)
    exe = ROOT / "build" / "MMBN3WhiteRecomp"
    cmd = [str(exe), "game.toml", "--window", "--frames", str(args.frames),
           "--rom", str(rom), "--bios",
           str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin"),
           "--save", str(test_sav), "--load-state", str(state)]
    session = {
        "argv": cmd, "cwd": str(out), "environment_overrides": overrides,
        "frames_requested": args.frames, "assist_script": args.assist,
        "trace_events": len(events),
        "trace_sha256": sha256_file(trace_path),
        "load_state_sha256": sha256_file(state),
        "source_save": str(args.save_src),
        "source_sha256_before": src_before,
        "rom_sha1": hashlib.sha1(rom.read_bytes()).hexdigest(),
        "config_sha256": sha256_file(ROOT / "game.toml"),
        "exe_sha256": sha256_file(exe),
        "game_rev": git_rev(ROOT),
        "engine_rev": git_rev(ROOT.parent / "gbarecomp"),
        "timeout_seconds": args.timeout,
    }
    (out / "session.json").write_text(json.dumps(session, indent=1))
    note(f"launch: {' '.join(cmd)}")

    proc, forced, timed_out = None, False, False
    with open(out / "stdout.log", "wb") as so, open(out / "stderr.log", "wb") as se:
        proc = subprocess.Popen(cmd, cwd=out, env=env,
                                stdin=subprocess.DEVNULL, stdout=so, stderr=se)
        (out / "child_pid.txt").write_text(str(proc.pid))
        try:
            proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if proc.poll() is None:
            if not timed_out:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            forced = True
    res["exit_code"] = proc.returncode
    res["forced_termination"] = forced
    res["timed_out"] = timed_out
    res["wall_seconds"] = round(time.monotonic() - t0, 1)

    text = (out / "stdout.log").read_text(errors="replace") + "\n" + \
           (out / "stderr.log").read_text(errors="replace")
    m = re.search(r"frames_presented=(\d+)", text)
    res["frames_presented"] = int(m.group(1)) if m else 0
    m = re.search(r"ppu_frames=(\d+)", text)
    res["ppu_frames"] = int(m.group(1)) if m else 0
    audio = re.search(r"host_window: audio=(\S+) driver=(\S+) device=(.*?) "
                      r"want=(\S+) cushion=(\S+) preroll=(\S+) got=(\S+)", text)
    res["audio_device"] = audio.groups() if audio else None
    res["assist_events"] = re.findall(r"assist_script_event pump=(\d+) "
                                      r"action=(\S+)", text)
    res["savestate_lines"] = re.findall(
        r"savestate_(saved|loaded) slot=(\d+).*", text)
    res["rewind_lines"] = re.findall(r"rewind_loaded frame=(\d+)", text)
    res["rewind_not_ready"] = text.count("rewind history is not ready yet")
    res["preload"] = re.findall(r"cache preload queued entries=(\d+)", text)
    res["healed"] = re.findall(r"healed_native=(\d+)", text)
    res["misses_line"] = re.findall(r"dispatch_misses=(\d+)", text)
    res["interp_line"] = re.findall(r"interpreted_insns=(\d+)", text)

    if frame_phase.is_file():
        rows, intervals, hitched, compile_us, frames = \
            parse_intervals(frame_phase)
        res["phase_rows"] = len(rows)
        res["phase_frames_span"] = [frames[0], frames[-1]] if frames else []
        res["phase_frames_monotonic"] = all(
            b > a for a, b in zip(frames, frames[1:])) if frames else False
        if intervals:
            q = statistics.quantiles(intervals, n=100)
            res["interval_ms"] = {
                "n": len(intervals),
                "median": round(statistics.median(intervals), 3),
                "p95": round(q[94], 3), "p99": round(q[98], 3),
                "max": round(max(intervals), 3)}
            res["hitch_frac_over_25ms"] = round(hitched / len(intervals), 4)
            both = sum(1 for iv, cu in zip(intervals, compile_us)
                       if iv > 25.0 and cu > 0)
            res["hitch_compile_overlap"] = {
                "hitched_with_compile_us": both, "hitched_total": hitched,
                "frames_with_compile_us": sum(1 for cu in compile_us if cu > 0)}
        else:
            res["interval_ms"] = None
    else:
        res["phase_rows"] = 0
        res["interval_ms"] = None

    under = [int(x) for x in re.findall(r"bridge_underrun=(\d+)", text)]
    over = [int(x) for x in re.findall(r"overflow_drops=(\d+)", text)]
    fills = [float(x) for x in re.findall(r"fill_ms=([\d.]+)", text)]
    res["audio_counters"] = {
        "underrun_last": under[-1] if under else None,
        "underrun_first": under[0] if under else None,
        "overflow_last": over[-1] if over else None,
        "overflow_first": over[0] if over else None,
        "overflow_grew": (over[-1] > over[0]) if len(over) >= 2 else None,
        "fill_ms_max": max(fills) if fills else None,
        "fill_ms_median": (round(statistics.median(fills), 2)
                           if fills else None)}
    res["input_apply_frames"] = len(re.findall(r"input_apply_ns=", text))

    res["source_sha256_after"] = sha256_file(args.save_src)
    res["source_untouched"] = (res["source_sha256_after"] == src_before)
    res["test_save_final"] = sha256_file(test_sav)
    res["slot_states"] = sorted(p.name for p in out.glob("mmbn3_white_usa.state*"))

    checks = {
        "windowed": res["frames_presented"] > 0,
        "completed_frames": res["frames_presented"] >= args.frames,
        "audio_device_init": res["audio_device"] is not None,
        "phase_rows_match": res["phase_rows"] == res["frames_presented"],
        "clean_exit": res["exit_code"] == 0 and not forced and not timed_out,
        "source_untouched": res["source_untouched"],
    }
    if args.assist:
        kinds = [a for _, a in res["assist_events"]]
        checks["assist_save_executed"] = "save1" in kinds
        checks["assist_load_executed"] = "load1" in kinds
        checks["assist_rewind_executed"] = "rewind" in kinds
        checks["slot_save_logged"] = any(
            k == "saved" for k, _ in res["savestate_lines"])
        checks["slot_load_logged"] = any(
            k == "loaded" for k, _ in res["savestate_lines"])
        checks["rewind_logged"] = len(res["rewind_lines"]) > 0
    res["checks"] = checks
    res["ok"] = all(checks.values())
    (out / "result.json").write_text(json.dumps(res, indent=1))
    note(f"exit={res['exit_code']} presented={res['frames_presented']} "
         f"audio={res['audio_device'] is not None} "
         f"phase_rows={res['phase_rows']} ok={res['ok']}")
    print(("PASS" if res["ok"] else "FAIL") + f": {out}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
