#!/usr/bin/env python3
"""Context analyzer for the SDL event-watch / canary probes.

Times are monotonic-clock ns values; the reference (t=0) is the run's first
producer push, which is what the sweep uses for settle_ns (first push + 1.0 s).
Everything is reported in milliseconds on that clock.

For every capture in a sweep JSON:
  * pair main-thread phase brackets (svcev/pumppoll/pumphandle/hostpump/...)
  * pair the probe's helper-thread canary brackets (canary-push-enter/-exit)
  * attribute each evwatch:* callback to the innermost main-thread bracket
  * list stall-scale brackets with watch offsets, the enclosing producer
    (P) gap, the startup-window flag, and the canary activity inside them
    (how many pushes overlapped, how long each took, when each ended relative
    to the bracket end, and how many canary watch callbacks landed inside)
  * report explicit queued/rejected results in captures that have them;
    older captures have watch callbacks but no SDL_PushEvent result records
  * report watch burstiness (inter-arrival gaps)

Usage:
  python3 tools/probe_context.py SWEEP.json [--min-ms 30] [--run TAG REPEAT]
"""
import argparse
import collections
import csv
import json
import os
from pathlib import Path
import re
import sys

FAMILIES = [
    "svcev", "pumphandle", "pumppoll", "hostpump", "pumpfn", "rewindcall",
    "pace", "fh", "render", "present", "audiopush",
]
CANARY = "canary-push"
CANARY_OUTCOMES = ("canary-push-queued", "canary-push-rejected")


def read_capture(path):
    """[(kind, ns, label)] in file order."""
    rows = []
    with open(path, newline="") as fh:
        for rec in csv.DictReader(fh):
            rows.append((rec["kind"], int(rec["ns"]), rec["label"]))
    return rows


def pair_all(rows, families):
    """family -> intervals; reject malformed capture brackets."""
    open_stack = {}
    out = collections.defaultdict(list)
    for _kind, ns, label in rows:
        for fam in families:
            if label == fam + "-enter":
                if fam in open_stack:
                    raise ValueError(f"nested {fam}-enter")
                open_stack[fam] = ns
            elif label == fam + "-exit":
                if fam not in open_stack:
                    raise ValueError(f"unmatched {fam}-exit")
                out[fam].append((open_stack.pop(fam), ns))
    if open_stack:
        raise ValueError(f"unterminated {sorted(open_stack)}")
    return out


def canary_watch_label(run, canary):
    """Read the registered user-event type, not a presumed 0x8000."""
    if not canary:
        return None
    log = Path(run["dir"]) / "stderr.log"
    match = re.search(r"event canary active: type=0x([0-9a-fA-F]+)",
                      log.read_text())
    if match is None:
        raise ValueError(f"canary brackets but no event type in {log}")
    return f"evwatch:{int(match.group(1), 16):08x}"


def count_canary_watch_callbacks(evwatch, canary, watch_label):
    if watch_label is None:
        return 0
    return sum(1 for ns, label in evwatch
               if label == watch_label and
               any(a <= ns <= b for a, b in canary))


def canary_outcomes(rows, canary):
    """Return (timestamp, result) for each push; [] for legacy captures.

    New captures write one result marker before every canary-push-exit.
    A partial result stream is refused rather than treated as success.
    """
    outcomes = [(ns, label) for kind, ns, label in rows
                if kind == "M" and label in CANARY_OUTCOMES]
    if not outcomes:
        return []
    if len(outcomes) != len(canary):
        raise ValueError("canary result count does not match push brackets")
    for start, end in canary:
        if sum(start <= ns <= end for ns, _ in outcomes) != 1:
            raise ValueError("canary bracket has missing or duplicate result")
    return outcomes


def is_startup_gap(prev_push_ns, settle_ns):
    """Use the detector's gap-start rule; None means no producer gap yet."""
    return None if prev_push_ns is None else prev_push_ns < settle_ns


def innermost(brackets, ns):
    """Narrowest main-thread bracket containing ns (ties by FAMILIES order)."""
    best = None
    for fam in FAMILIES:
        for (a, b) in brackets.get(fam, []):
            if a <= ns <= b:
                span = b - a
                if best is None or span < best[0]:
                    best = (span, fam)
                break
    return None if best is None else best[1]


def describe(values):
    if not values:
        return "n=0"
    s = sorted(values)
    n = len(s)
    return ("n=%d min %.4f p50 %.4f p95 %.4f max %.4f"
            % (n, s[0], s[n // 2], s[int(n * 0.95)], s[-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sweeps", nargs="+")
    ap.add_argument("--min-ms", type=float, default=30.0)
    ap.add_argument("--run", nargs=2, metavar=("TAG", "REPEAT"))
    args = ap.parse_args()

    watch_total = 0
    canary_watch_total = 0
    queued_total = 0
    rejected_total = 0
    inter_arrivals = []
    bracket_stats = collections.defaultdict(list)
    contain_counts = collections.Counter()
    offsets_from_end = collections.defaultdict(list)
    canary_durations = []
    canary_blocked = []
    stall_rows = []
    for sp in args.sweeps:
        sweep = json.load(open(sp))
        for key in ("edges_runs", "control_runs", "control2_runs"):
            for run in sweep.get(key, []):
                tag, rep = run["tag"], run["repeat"]
                if args.run and (tag, str(rep)) != (args.run[0], args.run[1]):
                    continue
                rows = read_capture(run["events_path"])
                pushes = [ns for (k, ns, _l) in rows if k == "P"]
                if not pushes:
                    continue
                t0 = pushes[0]
                brackets = pair_all(rows, FAMILIES + [CANARY])
                for fam in FAMILIES:
                    for (a, b) in brackets.get(fam, []):
                        bracket_stats[fam].append((b - a) / 1e6)
                canary = brackets.get(CANARY, [])
                watch_label = canary_watch_label(run, canary)
                outcomes = canary_outcomes(rows, canary)
                queued_total += sum(label == "canary-push-queued"
                                    for _ns, label in outcomes)
                rejected_total += sum(label == "canary-push-rejected"
                                      for _ns, label in outcomes)
                for (a, b) in canary:
                    d = (b - a) / 1e6
                    canary_durations.append(d)
                    if d > 5.0:
                        canary_blocked.append(
                            (d, os.path.basename(sp), tag, rep, (a - t0) / 1e6))
                evwatch = [(ns, l) for (k, ns, l) in rows
                           if k == "M" and l.startswith("evwatch:")]
                watch_total += len(evwatch)
                # A matching watch callback is not proof of queue admission
                # on SDL2-compat; only the new queued result marker is.
                canary_watch_total += count_canary_watch_callbacks(
                    evwatch, canary, watch_label)
                times = [ns for ns, _l in evwatch]
                inter_arrivals.extend((b - a) / 1e6
                                      for a, b in zip(times, times[1:]))
                for ns, _l in evwatch:
                    fam = innermost(brackets, ns)
                    contain_counts[fam or "<none>"] += 1
                    if fam:
                        for (a, b) in brackets[fam]:
                            if a <= ns <= b:
                                offsets_from_end[fam].append((b - ns) / 1e6)
                                break
                for fam in ("svcev", "pumppoll", "pumphandle", "hostpump"):
                    for (a, b) in brackets.get(fam, []):
                        dur = (b - a) / 1e6
                        if dur < args.min_ms:
                            continue
                        inside = [(ns - a) / 1e6 for ns, _l in evwatch
                                  if a <= ns <= b]
                        types = sorted(set(
                            l for ns, l in evwatch if a <= ns <= b))
                        prev_p = max([p for p in pushes if p <= a], default=None)
                        next_p = min([p for p in pushes if p >= b], default=None)
                        # Canary pushes attempted while this call was running.
                        c_inside = [(aa, bb) for (aa, bb) in canary
                                    if aa <= b and bb >= a]
                        stall_rows.append(
                            dict(sweep=os.path.basename(sp), tag=tag, repeat=rep,
                                 fam=fam, t=(a - t0) / 1e6, dur=dur,
                                 startup=is_startup_gap(
                                     prev_p, run.get("settle_ns", t0 + 1_000_000_000)),
                                 prev_p=None if prev_p is None else (a - prev_p) / 1e6,
                                 next_p=None if next_p is None else (next_p - b) / 1e6,
                                 gap=(None if prev_p is None or next_p is None
                                      else (next_p - prev_p) / 1e6),
                                 n_in=len(inside),
                                 first_off=None if not inside else inside[0],
                                 last_from_end=None if not inside else (
                                     (b - max(ns for ns, _l in evwatch
                                              if a <= ns <= b)) / 1e6),
                                 c_n=len(c_inside),
                                 c_durs=[(bb - aa) / 1e6 for (aa, bb) in c_inside],
                                 c_span=[(bb - b) / 1e6 for (aa, bb) in c_inside],
                                 c_events=count_canary_watch_callbacks(
                                     evwatch, c_inside, watch_label),
                                 c_queued=sum(a <= ns <= b and
                                              label == "canary-push-queued"
                                              for ns, label in outcomes),
                                 c_rejected=sum(a <= ns <= b and
                                                label == "canary-push-rejected"
                                                for ns, label in outcomes),
                                 types=types)
                        )

    print("== event-watch callbacks (not necessarily queue admission) ==")
    print("total evwatch records:", watch_total,
          "(matching canary watch records: %d)" % canary_watch_total)
    print("explicit canary results: queued=%d rejected=%d"
          % (queued_total, rejected_total))
    print("containment by innermost main-thread family:", dict(contain_counts))
    for fam, offs in sorted(offsets_from_end.items()):
        print("  offset from %s-exit (ms): %s" % (fam, describe(offs)))
    print()
    print("== bracket durations (paired, ms) ==")
    for fam in FAMILIES:
        d = sorted(bracket_stats.get(fam, []))
        if not d:
            continue
        n = len(d)
        print("  %-11s n=%-7d p50 %9.4f p99 %9.4f max %9.4f"
              % (fam, n, d[n // 2], d[int(n * 0.99)], d[-1]))
    print()
    print("== canary-push brackets (helper-thread SDL_PushEvent) ==")
    print("  durations (ms): %s" % describe(canary_durations))
    print("  pushes longer than 5 ms: %d" % len(canary_blocked))
    for (d, sw, tag, rep, t) in sorted(canary_blocked, reverse=True)[:10]:
        print("    %8.3f ms  %s %s r%s  t=%9.1f" % (d, sw, tag, rep, t))
    print()
    print("== stall-scale brackets (>= %.1f ms) ==" % args.min_ms)
    stall_rows.sort(key=lambda r: -r["dur"])
    for r in stall_rows:
        print("  %s %s r%s %-9s t=%9.1f dur=%8.3f startup=%-5s prev_p=%s "
              "next_p=%s P_gap=%s"
              % (r["sweep"], r["tag"], r["repeat"], r["fam"], r["t"], r["dur"],
                 r["startup"],
                 "-" if r["prev_p"] is None else "%.1f" % r["prev_p"],
                 "-" if r["next_p"] is None else "%.1f" % r["next_p"],
                 "-" if r["gap"] is None else "%.1f" % r["gap"]))
        print("      watch records in bracket: n_in=%d first_off=%s last_from_end=%s"
              % (r["n_in"],
                 "-" if r["first_off"] is None else "%.4f" % r["first_off"],
                 "-" if r["last_from_end"] is None else "%.4f" % r["last_from_end"]))
        print("      canary pushes overlapping: %s"
              % (describe(r["c_durs"]) if r["c_durs"] else "n=0"))
        if r["c_durs"]:
            print("        push end vs bracket end (ms, +=after): %s"
                  % " ".join("%+.3f" % v for v in r["c_span"]))
            print("        matching canary watch callbacks inside: %d" % r["c_events"])
            print("        explicit queue results inside: queued=%d rejected=%d"
                  % (r["c_queued"], r["c_rejected"]))
        print("      types: %s" % ",".join(r["types"]))
    if inter_arrivals:
        s = sorted(inter_arrivals)
        n = len(s)
        tiny = sum(1 for g in s if g <= 0.01)
        print()
        print("== event-watch inter-arrival gaps (ms) ==")
        print("  n=%d p50 %.3f p90 %.3f p99 %.3f max %.3f  <=0.01ms: %d (%.1f%%)"
              % (n, s[n // 2], s[int(n * 0.9)], s[int(n * 0.99)], s[-1],
                 tiny, 100.0 * tiny / n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
