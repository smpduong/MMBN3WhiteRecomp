#!/usr/bin/env python3
"""Windowed replay + assist-script run with full session record.

One invocation = one genuinely windowed run (--window is explicit; a missing
window is a failure, not a silent headless fallback). Guest input comes from
an actual frame-indexed trace (GBARECOMP_INPUT_REPLAY); host pause/resume,
save/load and rewind come from GBARECOMP_ASSIST_SCRIPT through the same
host-control queue as window keys (the debugger path bypasses that queue, so
this is the route under test). No TCP is used (the --tcp runtime path never
reaches the window loop, so TCP cannot observe these runs; observation is via
in-run logs: per-frame input CSV, host_pause/savestate/rewind lines, and the
frame-phase CSV).

Isolation: per-run ROM copy (slot states land beside the ROM), disposable
save copy (source hashed before/after, never written), per-run heal cache
(empty / seeded copy / seeded from a previous run's cache), cwd=run dir,
retained stdout/stderr, monotonic timeout, child-PID-only teardown.

Exit semantics (corrected after gate1-D-warm): signal_terminated is True
whenever the exit code is negative (killed by ANY party, harness or
external); harness_killed records only harness-initiated TERM/KILL.
Banner frame counts come from the exit banner and may be MISSING after a
forced end — observed_phase_rows (frame-phase CSV rows) is reported
separately and never conflated with the banner. Windowed-ness is proven by
device/init evidence (audio device line, present-in-place line), never
inferred from a missing banner.

Analysis retained in result.json: banner vs observed presented frames,
audio-device init line, assist/state-control log lines in order with
frame+pc+mem asserts, per-frame KEYINPUT asserts against an independent
expectation, host pause freeze/resume asserts, frame-cycle duration stats
(row sums, accurately labeled — NOT scanout) plus host
presentation-boundary intervals from present_ns, hitch attribution rows
(guest_us + interp_delta + worker_delta_us), audio underrun/overflow
first/last, preload and heal counters, guest frame ID span and restore
epochs.
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

RELEASED = 0x03FF
UP = 0x03BF
DOWN = 0x037F
LEFT = 0x03DF
RIGHT = 0x03EF
A = 0x03FE

PHASE_REQUIRED_COLUMNS = ("frame", "guest_us", "render_us", "present_us",
                          "audio_us", "pump_us", "pacer_us", "compile_us",
                          "present_ns", "interp_delta", "worker_delta_us")


def build_trace_steady(path):
    # Net-area walk: UP held throughout (movediag-proven). Steady route for
    # pacing comparison; input asserts still run (constant expectation).
    events = [(0, UP)]
    with open(path, "w") as f:
        f.write(TRACE_HEADER)
        for frame, keys in events:
            f.write(f"{frame},0x{keys:04X}\n")
    return events


def build_trace_changing(path, base):
    # Deterministic control trace relative to the fixture frame `base`:
    # changing directions, releases, and same-frame conflicting pairs
    # (last-wins) INSIDE the exercised guest range [base, base+599].
    # Load/rewind restores land mid-span by construction (spans are 40
    # frames; any restore crosses several boundaries).
    spans = [UP, DOWN, LEFT, RIGHT, RELEASED]
    events = [(base, RELEASED)]
    f = base + 20
    i = 0
    while f < base + 600:
        events.append((f, spans[i % len(spans)]))
        i += 1
        f += 40
    # Same-frame conflicts (must resolve to the LAST listed):
    events.append((base + 200, UP))
    events.append((base + 200, A))
    events.append((base + 400, LEFT))
    events.append((base + 400, RELEASED))
    events.sort(key=lambda e: e[0])  # stable: conflicts keep listed order
    with open(path, "w") as f:
        f.write(TRACE_HEADER)
        for frame, keys in events:
            f.write(f"{frame},0x{keys:04X}\n")
    return events


def expected_keyinput(events, frame):
    # Independent last-wins lookup (mirrors input_replay.h semantics in
    # separately written code; the engine behavior itself is unit-tested in
    # input_replay_tests including negative controls).
    keys = RELEASED
    for f, k in events:
        if f > frame:
            break
        keys = k
    return keys


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


def cycle_ms(row):
    return sum(int(row[c]) for c in
               ("guest_us", "render_us", "present_us", "audio_us",
                "pump_us", "pacer_us")) / 1000.0


def stats(xs):
    q = statistics.quantiles(xs, n=100) if len(xs) >= 2 else [xs[0]] * 100
    return {"n": len(xs), "median": round(statistics.median(xs), 3),
            "p95": round(q[94], 3), "p99": round(q[98], 3),
            "max": round(max(xs), 3)}


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
    ap.add_argument("--trace-mode", choices=["steady", "changing"],
                    default="steady")
    ap.add_argument("--trace-base", type=int, default=25739,
                    help="fixture guest frame the changing trace is built "
                         "on; verified against the first phase row")
    ap.add_argument("--warm-load", action="store_true",
                    help="preload the heal cache. D-warm seeds from the cold "
                         "run's resulting cache (matched pair); the large "
                         "shared cache is a separate stress case.")
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
    res = {"label": args.label, "ok": False,
           "trace_mode": args.trace_mode, "trace_base": args.trace_base}
    t0 = time.monotonic()

    def note(msg):
        print(msg, flush=True)
        with open(out / "driver.log", "a") as f:
            f.write(msg + "\n")

    def check(name, passed, expected, observed):
        res.setdefault("checks", {})[name] = {
            "pass": bool(passed), "expected": expected, "observed": observed}
        note(f"check {name}: {'PASS' if passed else 'FAIL'} "
             f"(expected {expected}; observed {observed})")
        return bool(passed)

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
    if args.trace_mode == "changing":
        events = build_trace_changing(trace_path, args.trace_base)
    else:
        events = build_trace_steady(trace_path)

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
    res["cache_note"] = cache_note

    frame_phase = out / "frame-phase.csv"
    apply_log = out / "input-apply.csv"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GBARECOMP_")}
    overrides = {
        "GBARECOMP_HEAL_CACHE": str(cache_dir),
        "GBARECOMP_HEAL_WARM_LOAD": "1" if args.warm_load else "0",
        "GBARECOMP_INPUT_REPLAY": str(trace_path),
        "GBARECOMP_INPUT_APPLY_LOG": str(apply_log),
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

    proc, harness_killed, timed_out = None, False, False
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
            harness_killed = True
    res["exit_code"] = proc.returncode
    res["signal_terminated"] = proc.returncode is not None and proc.returncode < 0
    res["harness_killed"] = harness_killed
    res["forced_termination"] = harness_killed  # legacy name: harness-initiated only
    res["timed_out"] = timed_out
    res["wall_seconds"] = round(time.monotonic() - t0, 1)

    text = (out / "stdout.log").read_text(errors="replace") + "\n" + \
           (out / "stderr.log").read_text(errors="replace")
    m = re.search(r"frames_presented=(\d+)", text)
    res["banner_presented"] = int(m.group(1)) if m else None
    res["banner_present"] = m is not None
    m = re.search(r"ppu_frames=(\d+)", text)
    res["ppu_frames"] = int(m.group(1)) if m else None
    audio = re.search(r"host_window: audio=(\S+) driver=(\S+) device=(.*?) "
                      r"want=(\S+) cushion=(\S+) preroll=(\S+) got=(\S+)", text)
    res["audio_device"] = audio.groups() if audio else None
    res["pip_line"] = "present-in-place ON" in text
    res["assist_events"] = re.findall(r"assist_script_event pump=(\d+) "
                                      r"action=(\S+)", text)
    res["pause_lines"] = re.findall(
        r"host_pause state=(paused|resumed) frame=(\d+) vblank=(\d+) "
        r"pc=(0x[0-9A-Fa-f]+)", text)
    res["save_lines"] = re.findall(
        r"savestate_saved slot=(\d+) path=\"([^\"]+)\" pc=(0x[0-9A-Fa-f]+) "
        r"frame=(\d+) mem=([0-9a-f]+)", text)
    res["load_lines"] = re.findall(
        r"savestate_loaded slot=(\d+) path=\"([^\"]+)\" pc=(0x[0-9A-Fa-f]+) "
        r"frame=(\d+) mem=([0-9a-f]+)", text)
    # Legacy lines without mem (old binaries): kept for parsing old logs only.
    res["rewind_lines"] = re.findall(
        r"rewind_loaded frame=(\d+) pc=(0x[0-9A-Fa-f]+) mem=([0-9a-f]+)", text)
    res["rewind_not_ready"] = text.count("rewind history is not ready yet")
    res["preload"] = re.findall(r"cache preload queued entries=(\d+)", text)
    res["dropped_preloads"] = re.findall(
        r"shutdown dropped (\d+) pending preload", text)
    res["healed"] = re.findall(r"healed_native=(\d+)", text)
    res["misses_line"] = re.findall(r"dispatch_misses=(\d+)", text)
    res["interp_line"] = re.findall(r"interpreted_insns=(\d+)", text)

    # ---- frame-phase CSV: strict validation (missing fields must not pass)
    res["phase_rows"] = 0
    rows, cycles, bounds, hitches = [], [], [], []
    restore_epochs = 0
    if frame_phase.is_file():
        with open(frame_phase) as f:
            reader = csv.DictReader(f)
            missing = [c for c in PHASE_REQUIRED_COLUMNS
                       if c not in (reader.fieldnames or [])]
            if missing:
                res["phase_missing_columns"] = missing
            else:
                rows = list(reader)
    else:
        res["phase_missing_columns"] = list(PHASE_REQUIRED_COLUMNS)
    if rows:
        try:
            frames = [int(r["frame"]) for r in rows]
            cycles = [cycle_ms(r) for r in rows]
            pns = [int(r["present_ns"]) for r in rows]
            assert all(p >= 0 for p in pns)
            nz = [(i, p) for i, p in enumerate(pns) if p > 0]
            assert all(b >= a for (_, a), (_, b) in zip(nz, nz[1:])), \
                "present_ns not monotonic"
            bounds = [(b - a) / 1e6 for (_, a), (_, b) in zip(nz, nz[1:])]
            restore_epochs = sum(1 for a, b in zip(frames, frames[1:])
                                 if b < a)
            for i, c in enumerate(cycles):
                if c > 25.0:
                    r = rows[i]
                    hitches.append({
                        "frame": frames[i],
                        "cycle_ms": round(c, 3),
                        "guest_us": int(r["guest_us"]),
                        "present_us": int(r["present_us"]),
                        "audio_us": int(r["audio_us"]),
                        "pump_us": int(r["pump_us"]),
                        "interp_delta": int(r["interp_delta"]),
                        "worker_delta_us": int(r["worker_delta_us"])})
            res["phase_rows"] = len(rows)
            res["phase_frames_span"] = [frames[0], frames[-1]]
            res["phase_first_frame"] = frames[0]
            res["restore_epochs"] = restore_epochs
            res["cycle_ms"] = stats(cycles)
            res["present_boundary_ms"] = stats(bounds) if bounds else None
            res["hitch_frac_over_25ms"] = round(len(hitches) / len(rows), 4)
            res["hitches"] = hitches
        except (KeyError, ValueError, AssertionError) as e:
            res["phase_analysis_error"] = f"{type(e).__name__}: {e}"
            rows = []
    check("phase_columns_complete", "phase_missing_columns" not in res,
          f"columns {list(PHASE_REQUIRED_COLUMNS)}",
          f"missing={res.get('phase_missing_columns')}")
    check("phase_nonempty", len(rows) > 0, ">=1 phase rows",
          f"rows={len(rows)}")
    if rows:
        check("trace_base_matches_first_frame", frames[0] == args.trace_base,
              f"first phase frame == trace base {args.trace_base}",
              f"first={frames[0]}")

    # ---- per-frame KEYINPUT asserts against the independent expectation
    apply_rows = []
    if apply_log.is_file():
        with open(apply_log) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                mf = re.fullmatch(r"(\d+),(0x[0-9A-Fa-f]+)", line)
                if not mf:
                    res.setdefault("apply_parse_errors", []).append(line[:60])
                    continue
                apply_rows.append((int(mf.group(1)), int(mf.group(2), 16)))
    res["apply_rows"] = len(apply_rows)
    mismatches = []
    for fr, keys in apply_rows:
        exp = expected_keyinput(events, fr)
        if keys != exp:
            mismatches.append({"frame": fr, "observed": hex(keys),
                               "expected": hex(exp)})
            if len(mismatches) >= 10:
                break
    res["apply_mismatches"] = mismatches
    check("input_apply_log_nonempty", len(apply_rows) > 0,
          ">=1 applied-input rows", f"rows={len(apply_rows)}")
    check("keyinput_matches_trace", len(mismatches) == 0,
          "every applied KEYINPUT == independent last-wins expectation",
          f"mismatches={len(mismatches)}" +
          (f" e.g. {mismatches[0]}" if mismatches else ""))
    if apply_rows:
        jumps = [(a, b) for (a, _), (b, _) in
                 zip(apply_rows, apply_rows[1:]) if b < a]
        res["apply_backward_jumps"] = len(jumps)
        check("restore_crosses_checked", True, "informational",
              f"{len(jumps)} backward frame jumps in apply log; all rows "
              f"asserted above, including post-restore rows")

    # ---- host-control asserts (order, values, outcomes)
    if args.assist:
        script_actions = [a for _, a in res["assist_events"]]
        want = [tok.split(":")[1] for tok in args.assist.split(";") if tok]
        check("assist_order", script_actions == want,
              f"script order {want}", f"observed {script_actions}")
        n_pause = want.count("pause")
        if n_pause:
            states = [s for s, _, _, _ in res["pause_lines"]]
            check("pause_resume_logged", states == ["paused", "resumed"] * n_pause,
                  f"{['paused', 'resumed'] * n_pause}", f"observed {states}")
            frozen_ok, frozen_detail = True, []
            for i in range(0, len(res["pause_lines"]), 2):
                if i + 1 >= len(res["pause_lines"]):
                    frozen_ok = False
                    frozen_detail.append("unpaired pause")
                    break
                pf = res["pause_lines"][i][1]
                rf = res["pause_lines"][i + 1][1]
                frozen_detail.append(f"pause@{pf}->resume@{rf}")
                if pf != rf:
                    frozen_ok = False
            check("pause_freezes_guest_frame", frozen_ok,
                  "each resume frame == its pause frame",
                  "; ".join(frozen_detail) if frozen_detail else "no pairs")
            adv_ok = True
            frs = [int(x[1]) for x in res["pause_lines"]]
            for i in range(1, len(frs), 2):
                if i + 1 < len(frs) and not (frs[i + 1] > frs[i]):
                    adv_ok = False
            check("resume_advances", adv_ok,
                  "next pause frame > resume frame",
                  f"pause-line frames={frs}")
        if "save1" in want and "load1" in want:
            sok = len(res["save_lines"]) > 0 and len(res["load_lines"]) > 0
            check("slot_save_load_logged", sok, "save+load lines",
                  f"save={res['save_lines'][:1]} load={res['load_lines'][:1]}")
            if sok:
                s, l = res["save_lines"][0], res["load_lines"][0]
                check("restore_identity",
                      s[3] == l[3] and s[2] == l[2] and s[4] == l[4],
                      "save frame==load frame, pc==pc, mem==mem",
                      f"save(f={s[3]} pc={s[2]} mem={s[4][:12]}) "
                      f"load(f={l[3]} pc={l[2]} mem={l[4][:12]})")
        if "rewind" in want:
            check("rewind_logged", len(res["rewind_lines"]) > 0,
                  ">=1 rewind_loaded line", f"lines={res['rewind_lines'][:2]}")
            check("rewind_ready", res["rewind_not_ready"] == 0,
                  "no 'history not ready'", f"count={res['rewind_not_ready']}")

    # ---- audio (buffering continuity only; never latency claims)
    under = [int(x) for x in re.findall(r"bridge_underrun=(\d+)", text)]
    over = [int(x) for x in re.findall(r"overflow_drops=(\d+)", text)]
    fills = [float(x) for x in re.findall(r"fill_ms=([\d.]+)", text)]
    pushes = re.findall(r"audio_push_ns=(\d+)", text)
    res["audio_counters"] = {
        "underrun_first": under[0] if under else None,
        "underrun_last": under[-1] if under else None,
        "underrun_grew": (under[-1] > under[0]) if len(under) >= 2 else None,
        "overflow_first": over[0] if over else None,
        "overflow_last": over[-1] if over else None,
        "overflow_grew": (over[-1] > over[0]) if len(over) >= 2 else None,
        "fill_ms_max": max(fills) if fills else None,
        "fill_ms_median": (round(statistics.median(fills), 2)
                           if fills else None),
        "push_events": len(pushes)}
    res["input_apply_probe_events"] = len(
        re.findall(r"input_apply_ns=", text))

    res["source_sha256_after"] = sha256_file(args.save_src)
    res["source_untouched"] = (res["source_sha256_after"] == src_before)
    res["test_save_final"] = sha256_file(test_sav)
    res["slot_states"] = sorted(p.name for p in out.glob("mmbn3_white_usa.state*"))

    # Windowed-ness from device/init evidence — NEVER inferred from a missing
    # banner. Banner counts and phase rows are reported separately.
    check("windowed", res["audio_device"] is not None and res["pip_line"],
          "audio device line + present-in-place line",
          f"audio={res['audio_device'] is not None} pip={res['pip_line']}")
    if res["banner_present"]:
        check("completed_frames", res["banner_presented"] >= args.frames,
              f"banner presented >= {args.frames}",
              f"banner={res['banner_presented']}")
        check("phase_rows_match_banner", res["phase_rows"] == res["banner_presented"],
              "phase rows == banner presented",
              f"rows={res['phase_rows']} banner={res['banner_presented']}")
    else:
        check("completed_frames", False,
              f"banner presented >= {args.frames} (banner required)",
              "banner MISSING — observed phase rows kept as partial data, "
              f"not a pass (rows={res['phase_rows']})")
    check("clean_exit",
          res["exit_code"] == 0 and not res["signal_terminated"]
          and not timed_out,
          "exit 0, no signal, no timeout",
          f"exit={res['exit_code']} signal={res['signal_terminated']} "
          f"harness_killed={harness_killed} timeout={timed_out}")
    check("source_untouched", res["source_untouched"],
          "source save unchanged", f"before={src_before[:16]}")
    res["ok"] = all(c["pass"] for c in res["checks"].values())
    (out / "result.json").write_text(json.dumps(res, indent=1))
    note(f"exit={res['exit_code']} banner={res['banner_presented']} "
         f"phase_rows={res['phase_rows']} ok={res['ok']}")
    print(("PASS" if res["ok"] else "FAIL") + f": {out}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
