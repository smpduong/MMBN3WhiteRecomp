#!/usr/bin/env python3
"""Replay a bounded input trace with isolated saves and audio-buffer diagnostics.

This measures production timing and internal buffering, NOT button-to-speaker
latency. The old event probe observed host input, not verified guest input.
Inferring its frame numbers is useful for reproductions, not an exact recording.

Example: python3 tools/audio_replay_check.py --label buffer-fix --frames 3600
Use --replay PATH for a real GBARECOMP_INPUT_RECORD CSV instead of inference.
Headless checks exercise execution/coverage only: no audio device is opened.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SOURCE_RATE = 65536.0
AUDIO_RE = re.compile(r"audio_push_ns=(\d+) count=(\d+)")
INPUT_RE = re.compile(r"input_ns=(\d+) keyinput=0x([0-9a-fA-F]+)")
TRACE_RE = re.compile(r"(\d+),0x([0-9a-fA-F]+)")
LIMITATIONS = [
    "Queue occupancy excludes hardware/device buffering; it is not sound latency.",
    "Audio chunks contain background music as well as effects; amplitude does not identify a button sound.",
    "Instrumented runs can change scheduling and should be compared with equivalent instrumentation.",
    "A copied personal save may differ from the initial save used by the original capture.",
]


def file_hash(path, algorithm="sha256"):
    if not path.is_file():
        return None
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def read_phases(path):
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "frame" not in reader.fieldnames:
            raise ValueError(f"missing frame CSV header: {path}")
        rows = [{key: int(value) for key, value in row.items()}
                for row in reader]
    if any(b["frame"] <= a["frame"] for a, b in zip(rows, rows[1:])):
        raise ValueError(f"frame CSV is not strictly increasing: {path}")
    return rows


def read_trace(path):
    events = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = TRACE_RE.fullmatch(line)
        if not match:
            raise ValueError(f"invalid input CSV line {line_number}: {path}")
        frame, keys = int(match[1]), int(match[2], 16)
        if keys > 0x3FF or (events and frame < events[-1][0]):
            raise ValueError(f"invalid input state/order at line {line_number}: {path}")
        events.append((frame, keys))
    if not events:
        raise ValueError(f"input CSV has no events: {path}")
    return events


def infer_trace(capture):
    event_path = capture / "events.log"
    phase_path = capture / "frame-phase.csv"
    rows = read_phases(phase_path)
    lines = event_path.read_text(encoding="utf-8", errors="replace").splitlines()
    push_count = sum(bool(AUDIO_RE.search(line)) for line in lines)
    if not rows or push_count != len(rows):
        raise ValueError(f"cannot infer trace: {push_count} audio pushes != "
                         f"{len(rows)} frame rows (both must be nonempty)")
    events = []
    push_index = -1
    previous_time = -1
    observed = []
    for line_number, line in enumerate(lines, 1):
        push = AUDIO_RE.search(line)
        if push:
            push_index += 1
        event = INPUT_RE.search(line)
        if event:
            timestamp, keys = int(event[1]), int(event[2], 16)
            if timestamp < previous_time or keys > 0x3FF:
                raise ValueError("capture input timestamps/states are invalid")
            previous_time = timestamp
            frame = rows[push_index]["frame"] if push_index >= 0 else 0
            events.append((frame, keys))
            observed.append({"log_line": line_number, "input_ns": timestamp,
                             "inferred_frame": frame, "keyinput": f"0x{keys:03X}"})
    if not events:
        raise ValueError("capture has no input events")
    provenance = {
        "kind": "inferred_from_host_event_probe", "capture": str(capture),
        "events_sha256": file_hash(event_path),
        "frame_phase_sha256": file_hash(phase_path),
        "audio_push_count": push_count, "phase_row_count": len(rows),
        "mapping": "nth audio_push maps to nth frame row; following input maps to that frame; pre-push input maps to frame 0",
        "limitation": "No original GBARECOMP_INPUT_RECORD exists in this capture. Host-observed keys may have been filtered before guest delivery; this is an inferred reproduction, not an exact input recording.",
        "observed_inputs": observed,
    }
    return events, provenance


def bounded_trace(events, frame_limit):
    # --frames counts presented frames for windowed runs, whereas the CSV uses
    # PPU frame numbers. Release before this lower bound even when PPU gaps occur.
    release_frame = max(0, frame_limit - 1)
    bounded = [(0, 0x3FF)]
    for frame, keys in events:
        if frame > release_frame:
            continue
        if frame == bounded[-1][0]:
            bounded[-1] = (frame, keys)
        elif keys != bounded[-1][1]:
            bounded.append((frame, keys))
    if bounded[-1][0] == release_frame:
        bounded[-1] = (release_frame, 0x3FF)
    else:
        bounded.append((release_frame, 0x3FF))
    return bounded


def describe(values):
    return ({"count": len(values), "min": min(values),
             "median": statistics.median(values), "max": max(values)}
            if values else {"count": 0, "min": None, "median": None, "max": None})


def numeric_fields(line):
    result = {}
    for key, raw in re.findall(r"([A-Za-z_][A-Za-z_0-9]*)=([^\s]+)", line):
        match = re.match(r"[+-]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?|\.\d+)", raw)
        if match:
            value = match[0]
            if value.lstrip("+-").lower().startswith("0x"):
                result[key] = int(value, 16)
            else:
                result[key] = float(value) if "." in value else int(value)
    return result


def analyze_logs(directory):
    event_path = directory / "events.log"
    text = event_path.read_text(encoding="utf-8", errors="replace") if event_path.exists() else ""
    stdout_path = directory / "stdout.log"
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    rows = read_phases(directory / "frame-phase.csv")
    pushes, queue, pulls = [], [], []
    observations_dropped = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        push = AUDIO_RE.search(line)
        if push:
            pushes.append({"ns": int(push[1]), "count": int(push[2]), "log_line": line_number})
        if "[event-probe] audio_pull_ns=" in line:
            pulls.append({"log_line": line_number, **numeric_fields(line)})
        if "[event-probe] audio_pull_observations_dropped=" in line:
            observations_dropped = max(observations_dropped,
                                       numeric_fields(line).get("audio_pull_observations_dropped", 0))
        if "[gba-audio-probe]" in line and "fill_ms=" in line:
            fields = numeric_fields(line)
            queue.append({"log_line": line_number,
                          "wall_seconds_from_first_push": ((pushes[-1]["ns"] - pushes[0]["ns"]) / 1e9) if pushes else None,
                          "reported_source_seconds": fields.get("audio"),
                          "fill_ms": fields.get("fill_ms"), "fields": fields})
    aligned = bool(pushes) and len(pushes) == len(rows)
    large_chunks = [{**push, "frame": rows[i]["frame"] if aligned else None,
                     "wall_seconds_from_first_push": (push["ns"] - pushes[0]["ns"]) / 1e9}
                    for i, push in enumerate(pushes) if push["count"] > 1200]
    gaps = []
    for i, (before, after) in enumerate(zip(rows, rows[1:]), 1):
        if after["frame"] - before["frame"] > 1:
            gaps.append({"previous_frame": before["frame"], "frame": after["frame"],
                         "skipped_frames": after["frame"] - before["frame"] - 1,
                         "guest_us": after.get("guest_us"),
                         "wall_seconds_from_first_push": (pushes[i]["ns"] - pushes[0]["ns"]) / 1e9 if aligned else None})
    wall = (pushes[-1]["ns"] - pushes[0]["ns"]) / 1e9 if len(pushes) > 1 else None
    samples = sum(push["count"] for push in pushes)
    coverage_lines = [line for line in (stdout + "\n" + text).splitlines()
                      if "self_heal_coverage=" in line]
    coverage = None
    if coverage_lines:
        line = coverage_lines[-1]
        coverage = {"raw": line, "fields": numeric_fields(line),
                    "status": re.search(r"self_heal_coverage=(\S+)", line)[1]}
    final_lines = [line for line in stdout.splitlines() if line.startswith("final_pc=")]
    # Callback records are drained by the producer, so their log-line order is
    # later than their actual timestamps. Compare their monotonic timestamps.
    pulls.sort(key=lambda pull: pull["audio_pull_ns"])
    pull_gaps = [(b["audio_pull_ns"] - a["audio_pull_ns"]) / 1e6
                 for a, b in zip(pulls, pulls[1:])]
    callback = {
        "observation_count": len(pulls), "observations_dropped": observations_dropped,
        "gap_ms": describe(pull_gaps),
        "fill_before_ms": describe([pull["fill_before_ms"] for pull in pulls if "fill_before_ms" in pull]),
        "fill_after_ms": describe([pull["fill_after_ms"] for pull in pulls if "fill_after_ms" in pull]),
        "frames_per_callback": describe([pull["frames"] for pull in pulls if "frames" in pull]),
        "gaps_over_25ms": [{"previous_ns": before["audio_pull_ns"], "ns": after["audio_pull_ns"], "gap_ms": gap}
                           for before, after, gap in zip(pulls, pulls[1:], pull_gaps) if gap > 25],
        "first": pulls[0] if pulls else None, "last": pulls[-1] if pulls else None,
        "limitation": "Callbacks observe application-side consumption, not physical speaker output. Missing observations prevent exact callback-gap accounting.",
    }
    return {
        "audio_metrics_available": bool(pushes and queue),
        "audio_push_count": len(pushes), "phase_rows": len(rows),
        "audio_phase_alignment_verified": aligned,
        "source_rate_assumption_hz": SOURCE_RATE,
        "total_pushed_samples": samples, "total_pushed_audio_seconds": samples / SOURCE_RATE,
        "first_to_last_push_wall_seconds": wall,
        "producer_hz_excluding_first_chunk": (samples - pushes[0]["count"]) / wall if wall else None,
        "push_gap_ms": describe([(b["ns"] - a["ns"]) / 1e6 for a, b in zip(pushes, pushes[1:])]),
        "chunks_over_1200": large_chunks, "frame_gaps": gaps,
        "queue_fill_ms": describe([row["fill_ms"] for row in queue if row["fill_ms"] is not None]),
        "queue_timeline": queue, "coverage": coverage,
        "callback": callback,
        "final_runtime_fields": numeric_fields(final_lines[-1]) if final_lines else {},
        "phase_us": {key: describe([row[key] for row in rows])
                     for key in rows[0] if key != "frame"} if rows else {},
    }


def git_state(path):
    result = {"path": str(path)}
    for key, args in (("head", ["rev-parse", "HEAD"]),
                      ("status", ["status", "--short"]),
                      ("diff_sha256", ["diff", "--no-ext-diff", "HEAD", "--"])):
        proc = subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, timeout=10, check=False)
        if proc.returncode == 0:
            result[key] = (hashlib.sha256(proc.stdout).hexdigest() if key == "diff_sha256"
                           else proc.stdout.decode("utf-8", errors="replace").strip())
        else:
            result[key] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capture", type=Path, default=ROOT / "build/runs/live-audio-siii6r9e")
    parser.add_argument("--replay", type=Path, help="existing frame-indexed input CSV; bypass capture conversion")
    parser.add_argument("--exe", type=Path, default=ROOT / "build/MMBN3WhiteRecomp")
    parser.add_argument("--frames", type=int, default=3600)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--label", default="audio-replay")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--snapshot", action="store_true", help="capture the final PPU frame (windowed only)")
    args = parser.parse_args()
    if args.frames < 1 or args.timeout <= 0:
        parser.error("--frames and --timeout must be positive")
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", args.label).strip("-")[:64] or "audio-replay"
    exe = args.exe.resolve()
    source_save = ROOT / "saves/mmbn3_white_usa.sav"
    config = ROOT / "game.toml"
    rom = ROOT / "roms/mmbn3_white_usa.gba"
    bios = ROOT.parent / "gbarecomp/bios/gba_bios.bin"
    for path in (exe, source_save, config, rom, bios):
        if not path.is_file():
            parser.error(f"required file not found: {path}")
    try:
        if args.replay:
            replay_source = args.replay.resolve()
            events = read_trace(replay_source)
            provenance = {"kind": "supplied_input_csv", "path": str(replay_source),
                          "sha256": file_hash(replay_source),
                          "limitation": "Supplied CSV origin/initial state must be checked separately."}
        else:
            events, provenance = infer_trace(args.capture.resolve())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    bounded = bounded_trace(events, args.frames)
    runs = ROOT / "build/runs"
    runs.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=label + "-", dir=runs))
    save = out / "test.sav"
    replay = out / "input-replay.csv"
    before = file_hash(source_save)
    shutil.copyfile(source_save, save)
    if file_hash(save) != before or file_hash(source_save) != before:
        raise RuntimeError("personal save changed during copying; run was not launched")
    provenance.update({"original_event_count": len(events), "bounded_event_count": len(bounded),
                       "requested_presented_frames": args.frames,
                       "release_ppu_frame": args.frames - 1,
                       "clipping": "Input after the requested frame budget is omitted; final state is forced released. Repeated states and same-frame intermediate states are collapsed."})
    write_json(out / "replay-provenance.json", provenance)
    with replay.open("w", encoding="utf-8") as stream:
        stream.write("# gbarecomp-keyinput-v1\n# frame,keyinput_active_low\n")
        stream.write("# See replay-provenance.json: inferred traces are NOT exact input recordings.\n")
        for frame, keys in bounded:
            stream.write(f"{frame},0x{keys:04X}\n")
    overrides = {
        "GBARECOMP_HEAL_WARM_LOAD": "0", "GBARECOMP_HEAL_CACHE": str(out / "cache"),
        "GBARECOMP_EVENT_PROBE": "1", "GBARECOMP_AUDIO_PROBE": "1",
        "GBARECOMP_FRAME_PHASE": str(out / "frame-phase.csv"),
        "GBARECOMP_INPUT_REPLAY": str(replay),
        "GBARECOMP_PROTECTED_FRAME_PROBE": "1",
    }
    if args.snapshot and not args.headless:
        (out / "screenshots").mkdir()
        overrides.update({"GBARECOMP_FRAMEDUMP_DIR": str(out / "screenshots"),
                          "GBARECOMP_FRAMEDUMP_START": str(args.frames),
                          "GBARECOMP_FRAMEDUMP_COUNT": "1"})
    if args.strict:
        overrides["GBARECOMP_STRICT_STATIC"] = "1"
    env = {key: value for key, value in os.environ.items() if not key.startswith("GBARECOMP_")}
    env.update(overrides)
    cmd = [str(exe), str(config), "--no-window" if args.headless else "--window",
           "--frames", str(args.frames), "--rom", str(rom), "--bios", str(bios), "--save", str(save)]
    session = {
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "argv": cmd, "cwd": str(out), "environment_overrides": overrides,
        "stripped_inherited_environment_names": sorted(key for key in os.environ if key.startswith("GBARECOMP_")),
        "inherited_sdl_environment": {key: os.environ[key] for key in ("SDL_AUDIODRIVER", "SDL_VIDEODRIVER", "SDL_RENDER_DRIVER") if key in os.environ},
        "timeout_seconds": args.timeout, "strict": args.strict, "headless": args.headless,
        "source_save": str(source_save), "source_save_sha256_before": before,
        "test_save_sha256_initial": file_hash(save), "executable_sha256": file_hash(exe),
        "config_sha256": file_hash(config), "rom_sha1": file_hash(rom, "sha1"),
        "bios_sha1": file_hash(bios, "sha1"), "replay_sha256": file_hash(replay),
        "game_repository": git_state(ROOT), "engine_repository": git_state(ROOT.parent / "gbarecomp"),
        "limitations": LIMITATIONS,
    }
    write_json(out / "session.json", session)
    result = {"ok": False, "run_directory": str(out), "timed_out": False,
              "cleanup_actions": [], "limitations": LIMITATIONS}
    proc = None
    started = time.monotonic()
    print(out, flush=True)
    try:
        with (out / "stdout.log").open("wb") as stdout, (out / "events.log").open("wb") as stderr:
            proc = subprocess.Popen(cmd, cwd=out, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            session["pid"] = proc.pid
            write_json(out / "session.json", session)
            try:
                proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
    except (OSError, KeyboardInterrupt) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if proc is not None and proc.poll() is None:
            # Only the exact process created above; never pkill or process-group signals.
            proc.terminate()
            result["cleanup_actions"].append({"action": "terminate", "pid": proc.pid})
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                result["cleanup_actions"].append({"action": "kill", "pid": proc.pid})
                proc.wait(timeout=5)
        result["exit_code"] = proc.returncode if proc else None
        result["elapsed_wall_seconds"] = time.monotonic() - started
        result["source_save_sha256_before"] = before
        result["source_save_sha256_after"] = file_hash(source_save)
        result["personal_save_unchanged"] = before == result["source_save_sha256_after"]
        result["test_save_sha256_final"] = file_hash(save)
        try:
            result["metrics"] = analyze_logs(out)
            final = result["metrics"]["final_runtime_fields"]
            actual_frames = final.get("ppu_frames" if args.headless else "frames_presented", 0)
            result["completed_requested_frames"] = actual_frames >= args.frames
            coverage = result["metrics"]["coverage"]
            result["strict_coverage_passed"] = (coverage is not None and coverage["status"] == "FULLY_STATIC"
                and all(coverage["fields"].get(key) == 0 for key in ("dispatch_misses", "interpreted_insns", "healed_native"))) if args.strict else None
            result["ok"] = (result["exit_code"] == 0 and not result["timed_out"]
                            and result["personal_save_unchanged"] and result["completed_requested_frames"]
                            and (not args.strict or result["strict_coverage_passed"]))
        except (OSError, ValueError) as exc:
            result["analysis_error"] = str(exc)
        write_json(out / "result.json", result)
    print(json.dumps({key: result[key] for key in ("ok", "exit_code", "timed_out", "personal_save_unchanged")}, indent=2))
    print("Full metrics: " + str(out / "result.json"))
    print("This reports buffering and execution, not exact button-to-speaker latency.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
