#!/usr/bin/env python3
"""Matched-window no-action baseline for the host-action edge sweep.

Question this answers: in runs with NO fast-forward / pause / resume, do any
windows of the SAME duration as the scripted action intervals show what the
action intervals show -- the host ring drained to <= `--empty-ms` (2.0 ms,
the sweep's own empty threshold), bridge counter steps, and a producer gap
covering the window?

Method (offline and READ-ONLY -- this tool never writes to a run dir, a
sweep JSON or any capture; it only reads `events_path` files named in a
sweep report and writes the report you ask it to write):

  * For every EDGES run in a sweep report, recompute the action intervals
    (fast-on -> fast-off, pause -> resume) with the sweep's own
    `interval_metrics`, so fill/steps use exactly the reviewed semantics.
  * For every CONTROL run (and any `control2_runs` branch), recompute the
    settle boundary from the raw capture (first producer push + `--settle-s`,
    cross-checked against the report's stored `settle_ns`) and slide windows
    of each paired duration over the post-settle P/C/M stream.
  * A control window's duration is taken from the PAIRED edges run of the
    same `repeat` index, so a 99.5-100.3 ms fast interval is compared against
    99.5-100.3 ms control windows, not against a nominal 100 ms.
  * The RUN is the statistical unit: per run the report carries the worst
    matched-window values, and the batch summary counts runs, never windows.
    Overlapping windows are scan positions, not independent samples.

Declared window rules (fixed here, not tuned per run):

  * anchors are P/C records at or after the settle boundary. A window is the
    half-open span [anchor, anchor+D) and is scanned only when the capture
    contains a record at/after anchor+D (a window clipped by the end of the
    capture is skipped).
  * `fill_min_ms` is the minimum `fill_ms` over every record inside the span
    (the anchor record included, the record at anchor+D excluded).
  * a counter step counts when its record index j satisfies
    anchor_idx < j < close_idx, where close_idx is the first record at/after
    anchor+D -- the same strictly-inside-the-boundaries rule the sweep
    applies between two action markers.
  * producer gaps are intervals between consecutive P records. A window's
    `gap_overlap_ms` is the largest overlap of such a gap with the window
    (bounded by D); `max_gap_ms` is the largest full gap overlapping it.
  * steps/records before the settle boundary are excluded from the scan and
    never windowed.

Rejection rules -- incomplete, malformed, too-short or rate-incompatible
captures are REFUSED, not silently dropped and never turned green:

  * the capture must exist, parse, pass `verify_timeline`, and have a
    producer push; the run record must report `exit == 0`, not
    `timed_out`/`truncated` and a parsed `capture`.
  * the post-settle span must be at least twice the longest matched duration
    and every duration must offer at least `--min-windows` windows.
  * all runs must carry the same positive integer `source_rate`, equal to
    `--expect-rate` when that is nonzero (65536 here).
  * a batch whose runs fail any check is REFUSED -- the separation result
    cannot be reported from it.

Separation: a control run "matches" when some scanned window of either
duration drains to <= `--empty-ms`. Matched windows are always reported with
their position (ns relative to the first producer push) and values. Batch
results: OK (all runs scanned, no matched window), NOT SEPARATED (all runs
scanned, >= 1 matched window), REFUSED (any run unusable), INSUFFICIENT (no
scannable pair). Overall exit code is 0 only for OK.

The comparison is CONSERVATIVE, not exactly time-matched: control windows
are scanned anywhere after the settle boundary, so they are far more
numerous than the two scripted intervals and the worst window is the whole
run's best effort to reproduce the action. No pump-to-push arithmetic or
route-time locking is used; a time-locked pseudo-edge would need its own
defensible alignment and is not claimed here.

Exit codes: 0 = separated with every run scanned; 1 = not separated,
refused or insufficient; 2 = missing input files.
"""
import argparse
import bisect
import json
from pathlib import Path

import host_audio_queue_check as q
import host_action_ring_sweep as s

ROOT = q.ROOT
TOOL_RULE_VERSION = '2026-09-26-matched-window-v1'
COUNTERS = q.COUNTERS
EDGE_LABELS = s.EDGE_LABELS
EMPTY_MS = 2.0
MIN_WINDOWS = 10


# ── producer timeline ────────────────────────────────────────────────────

def producer_gaps(events):
    """Consecutive-push gaps: [{start_ns, end_ns, ns, start_idx, end_idx}]."""
    pushes = [(i, r['ns']) for i, r in enumerate(events) if r['kind'] == 'P']
    if len(pushes) < 2:
        return []
    return [{'start_idx': pushes[k][0], 'end_idx': pushes[k + 1][0],
             'start_ns': pushes[k][1], 'end_ns': pushes[k + 1][1],
             'ns': pushes[k + 1][1] - pushes[k][1]}
            for k in range(len(pushes) - 1)]


def gap_overlap(gaps, t0, t1):
    """Largest overlap of any gap with [t0, t1): (overlap_ns, gap|None)."""
    best_ns, best_gap = 0, None
    for g in gaps:
        lo, hi = max(g['start_ns'], t0), min(g['end_ns'], t1)
        if hi > lo and (hi - lo) > best_ns:
            best_ns, best_gap = hi - lo, g
    return best_ns, best_gap


# ── window scan ──────────────────────────────────────────────────────────

def scan_windows(events, settle_ns, duration_ns, empty_ms=EMPTY_MS,
                 min_windows=MIN_WINDOWS, keep_detail=False):
    """Slide [anchor, anchor+D) windows over the post-settle stream.

    Returns a summary dict for the RUN (worst window per metric), never a
    per-window verdict. `keep_detail` adds the per-window list for tests.
    """
    ns = [r['ns'] for r in events]
    fills = [r['fill_ms'] for r in events]
    steps = {c: q.counter_steps(events, c) for c in COUNTERS}
    gaps = producer_gaps(events)
    anchors = [i for i, r in enumerate(events)
               if r['kind'] in ('P', 'C') and r['ns'] >= settle_ns]
    detail = []
    for a in anchors:
        t0, t1 = ns[a], ns[a] + duration_ns
        c = bisect.bisect_left(ns, t1, a + 1)
        if c >= len(events):
            continue                     # capture ends before the window does
        fill_min = min(fills[a:c])
        fill_idx = min(range(a, c), key=lambda k: fills[k])
        counts = {c2: sum(1 for (j, _, _) in steps[c2] if a < j < c)
                  for c2 in COUNTERS}
        overlap, gap = gap_overlap(gaps, t0, t1)
        detail.append({
            'anchor_idx': a, 'anchor_ns': t0, 'close_idx': c,
            'fill_min_ms': fill_min,
            'fill_min_idx': fill_idx,
            'step_counts': counts,
            'steps_total': sum(counts.values()),
            'gap_overlap_ms': round(overlap / 1e6, 6),
            'gap_ns': gap['ns'] if gap else None,
        })
    out = {'duration_ms': round(duration_ns / 1e6, 4), 'windows': len(detail),
           'problem': None}
    if not detail:
        out['problem'] = (f'no post-settle window of {out["duration_ms"]} ms '
                          f'fits in the capture')
        return out, detail
    if len(detail) < min_windows:
        out['problem'] = (f'only {len(detail)} post-settle windows of '
                          f'{out["duration_ms"]} ms (minimum {min_windows})')
    deepest = min(detail, key=lambda w: w['fill_min_ms'])
    most = max(detail, key=lambda w: (w['steps_total'], w['fill_min_ms']))
    widest = max(detail, key=lambda w: w['gap_overlap_ms'])
    # Step coverage: every post-settle step must be inside SOME window, or
    # it is reported as uncovered rather than dropped.
    total_steps = sum(len([1 for (i, _, _) in steps[c] if ns[i] >= settle_ns])
                      for c in COUNTERS)
    covered = set()
    for w in detail:
        for c2 in COUNTERS:
            for (j, _, _) in steps[c2]:
                if w['anchor_idx'] < j < w['close_idx']:
                    covered.add((c2, j))
    out.update({
        'fill_min_ms': deepest['fill_min_ms'],
        'fill_min_at_ns': ns[deepest['fill_min_idx']],
        'fill_min_at_idx': deepest['fill_min_idx'],
        'deepest_window_anchor_ns': deepest['anchor_ns'],
        'matched': deepest['fill_min_ms'] <= empty_ms,
        'empty_ms': empty_ms,
        'max_steps_in_window': most['steps_total'],
        'max_step_counts': most['step_counts'],
        'max_steps_at_ns': most['anchor_ns'],
        'windows_with_steps': sum(1 for w in detail if w['steps_total']),
        'gap_overlap_ms': widest['gap_overlap_ms'],
        'gap_overlap_at_ns': widest['anchor_ns'],
        'max_gap_ms': round(max((w['gap_ns'] for w in detail
                                 if w['gap_ns']), default=0) / 1e6, 6),
        'post_settle_steps_total': total_steps,
        'steps_outside_windows': total_steps - len(covered),
    })
    return out, (detail if keep_detail else [])


# ── per-run analysis ─────────────────────────────────────────────────────

def action_intervals(events, target_ms=40.0, settle_s=1.0):
    """Recompute the two action intervals with the sweep's own semantics."""
    out = {'problem': None, 'timeline': q.verify_timeline(events),
           'intervals': {}}
    tl = out['timeline']
    if tl['problems']:
        out['problem'] = 'capture timeline inconsistent: ' + '; '.join(
            tl['problems'])
        return out
    indices = {}
    for lab in EDGE_LABELS:
        hits = q.marker_indices(events, lab)
        if len(hits) != 1:
            out['problem'] = (f'expected exactly one {lab!r} marker, '
                              f'found {len(hits)}')
            return out
        indices[lab] = hits[0]
    order = sorted(indices, key=lambda k: indices[k])
    if order != list(EDGE_LABELS):
        out['problem'] = f'edge markers out of order: {order}'
        return out
    first_push = next((i for i, r in enumerate(events) if r['kind'] == 'P'),
                      None)
    if first_push is None:
        out['problem'] = 'edges capture has no producer pushes'
        return out
    settle_ns = events[first_push]['ns'] + int(settle_s * 1e9)
    if settle_ns >= events[indices['fast-on']]['ns']:
        out['problem'] = 'settle window reaches the first host-action marker'
        return out
    out['settle_ns'] = settle_ns
    gaps = producer_gaps(events)
    for key, (a, b) in (('fast', ('fast-on', 'fast-off')),
                        ('pause', ('pause', 'resume'))):
        ia, ib = indices[a], indices[b]
        m = s.interval_metrics(events, ia, ib, target_ms, settle_ns)
        t0, t1 = events[ia]['ns'], events[ib]['ns']
        overlap, gap = gap_overlap(gaps, t0, t1)
        full = [g['ns'] for g in gaps if g['end_ns'] > t0 and g['start_ns'] < t1]
        m['steps_total'] = sum(m['step_counts'].values())
        m['gap_overlap_ms'] = round(overlap / 1e6, 6)
        m['max_full_gap_ms'] = round(max(full, default=0) / 1e6, 6)
        m['duration_ns'] = t1 - t0
        out['intervals'][key] = m
    return out


def analyse_control_events(events, settle_s, durations_ns, empty_ms=EMPTY_MS,
                           min_windows=MIN_WINDOWS, keep_detail=False):
    """Scan one control capture with windows of the paired durations."""
    out = {'problem': None, 'problems': [], 'timeline':
           q.verify_timeline(events), 'windows': {}}
    tl = out['timeline']
    if tl['problems']:
        out['problem'] = 'control capture timeline inconsistent: ' + \
            '; '.join(tl['problems'])
        return out
    first_push = next((i for i, r in enumerate(events) if r['kind'] == 'P'),
                      None)
    if first_push is None:
        out['problem'] = 'control capture has no producer pushes'
        return out
    t0 = events[first_push]['ns']
    settle_ns = t0 + int(settle_s * 1e9)
    out['settle_ns'] = settle_ns
    out['first_push_ns'] = t0
    out['post_settle_span_s'] = round((events[-1]['ns'] - settle_ns) / 1e9, 4)
    post = [r for r in events if r['kind'] in ('P', 'C') and r['ns'] >= settle_ns]
    if not post:
        out['problem'] = 'control has no post-settle records'
        return out
    out['fill_min_ms'] = min(r['fill_ms'] for r in post)
    max_d = max(durations_ns.values())
    if (events[-1]['ns'] - settle_ns) < 2 * max_d:
        out['problems'].append(
            f'post-settle span {out["post_settle_span_s"]}s is shorter than '
            f'twice the longest matched window ({max_d / 1e6:g} ms)')
    for key, d in durations_ns.items():
        w, detail = scan_windows(events, settle_ns, d, empty_ms=empty_ms,
                                 min_windows=min_windows,
                                 keep_detail=keep_detail)
        out['windows'][key] = w
        if w.get('problem'):
            out['problems'].append(f'{key}: {w["problem"]}')
        if keep_detail:
            out.setdefault('detail', {})[key] = detail
    return out


def compare_pair(repeat, edges_a, control_a, source='control'):
    """Per-run comparison for one repeat: action interval vs paired windows."""
    pair = {'repeat': repeat, 'source': source, 'problem': None,
            'intervals': {}}
    if edges_a.get('problem'):
        pair['problem'] = f'edges run unusable: {edges_a["problem"]}'
        return pair
    if control_a.get('problem') or control_a.get('problems'):
        pair['problem'] = ('control run unusable: '
                           + (control_a.get('problem')
                              or '; '.join(control_a['problems'])))
        return pair
    for key in ('fast', 'pause'):
        act = edges_a['intervals'].get(key)
        win = (control_a.get('windows') or {}).get(key)
        if not act or act.get('problem'):
            pair['problem'] = f'{key}: action interval not measurable'
            return pair
        if not win or win.get('problem'):
            pair['problem'] = (f'{key}: matched windows unavailable: '
                               f'{(win or {}).get("problem")}')
            return pair
        if act['duration_s'] * 1e9 < 20e6 or act['duration_s'] > 2.0:
            pair['problem'] = (f'{key}: implausible action interval '
                               f'{act["duration_s"]}s')
            return pair
        pair['intervals'][key] = {
            'action_duration_ms': round(act['duration_s'] * 1000, 4),
            'action_fill_min_ms': act['fill_min_ms'],
            'action_steps_total': act['steps_total'],
            'action_step_counts': act['step_counts'],
            'action_gap_overlap_ms': act['gap_overlap_ms'],
            'control_duration_ms': win['duration_ms'],
            'control_windows': win['windows'],
            'control_fill_min_ms': win['fill_min_ms'],
            'control_fill_min_at_s': round(
                (win['fill_min_at_ns'] - control_a['first_push_ns']) / 1e9,
                4),                       # relative to first push
            'control_max_steps_in_window': win['max_steps_in_window'],
            'control_max_step_counts': win['max_step_counts'],
            'control_windows_with_steps': win['windows_with_steps'],
            'control_gap_overlap_ms': win['gap_overlap_ms'],
            'control_max_gap_ms': win['max_gap_ms'],
            'control_steps_outside_windows': win['steps_outside_windows'],
            'matched': win['matched'],
        }
    return pair


# ── batch assembly (offline; reads only events_path files) ───────────────

def _run_problems(tag, run):
    bad = []
    if run.get('exit') != 0:
        bad.append(f'{tag} r{run.get("repeat")}: exit {run.get("exit")}')
    if run.get('timed_out'):
        bad.append(f'{tag} r{run.get("repeat")}: timed out')
    if run.get('truncated'):
        bad.append(f'{tag} r{run.get("repeat")}: truncated')
    if not run.get('capture'):
        bad.append(f'{tag} r{run.get("repeat")}: capture missing')
    if not run.get('events_path') or not Path(run['events_path']).exists():
        bad.append(f'{tag} r{run.get("repeat")}: events file missing')
    return bad


def analyse_batch(path, settle_s=1.0, target_ms=40.0, empty_ms=EMPTY_MS,
                  min_windows=MIN_WINDOWS, expect_rate=65536):
    """Read one sweep JSON + its raw captures; never write to either."""
    raw = json.loads(Path(path).read_text())
    b = {'path': str(path), 'file_rule_version': raw.get('tool_rule_version'),
         'file_verdict': raw.get('verdict'), 'problems': [], 'notes': [],
         'pairs': [], 'runs_scanned': 0}
    edges = raw.get('edges_runs') or []
    controls = list(raw.get('control_runs') or [])
    controls2 = list(raw.get('control2_runs') or [])
    if not edges:
        b['problems'].append('batch has no edges runs')
    if not controls and not controls2:
        b['problems'].append('batch has no control runs')
    all_runs = ([('edges', r) for r in edges]
                + [('control', r) for r in controls]
                + [('control2', r) for r in controls2])
    for tag, run in all_runs:
        b['problems'] += _run_problems(tag, run)
    good_rates = {r.get('source_rate') for _, r in all_runs
                  if isinstance(r.get('source_rate'), int)
                  and r.get('source_rate') > 0}
    if len(good_rates) > 1:
        b['problems'].append(f'inconsistent source rates {sorted(good_rates)}')
    if expect_rate and good_rates and good_rates != {expect_rate}:
        b['problems'].append(
            f'source rate {sorted(good_rates)} != expected {expect_rate}')
    if any(not isinstance(r.get('source_rate'), int)
           or r.get('source_rate') <= 0 for _, r in all_runs):
        b['problems'].append('missing/invalid source_rate on one or more runs')

    edge_analysis = {}
    for run in edges:
        ev = q.read_events(Path(run['events_path']))
        a = action_intervals(ev, target_ms, settle_s)
        edge_analysis[run.get('repeat')] = (a, ev)
        if a.get('problem'):
            b['problems'].append(f'edges r{run.get("repeat")}: {a["problem"]}')
            continue
        stored = (run.get('analysis') or {})
        for key in ('fast_interval', 'pause_interval'):
            st = stored.get(key) or {}
            if not st or st.get('problem'):
                continue
            new = a['intervals']['fast' if key.startswith('fast')
                                 else 'pause']
            if (st.get('fill_min_ms') is not None
                    and abs(st['fill_min_ms'] - new['fill_min_ms']) > 1e-6):
                b['notes'].append(
                    f'edges r{run.get("repeat")} {key}: recomputed fill_min '
                    f'{new["fill_min_ms"]} != stored {st["fill_min_ms"]}')
            if (st.get('step_counts') is not None
                    and st['step_counts'] != new['step_counts']):
                b['notes'].append(
                    f'edges r{run.get("repeat")} {key}: recomputed step_counts'
                    f' {new["step_counts"]} != stored {st["step_counts"]}')

    for tag, run in [('control', r) for r in controls] + \
                    [('control2', r) for r in controls2]:
        rep = run.get('repeat')
        pair_edge = edge_analysis.get(rep)
        if pair_edge is None or pair_edge[0].get('problem'):
            b['problems'].append(
                f'{tag} r{rep}: no usable paired edges run')
            continue
        durations = {k: int(v['duration_ns'])
                     for k, v in pair_edge[0]['intervals'].items()}
        ev = q.read_events(Path(run['events_path']))
        ca = analyse_control_events(ev, settle_s, durations,
                                    empty_ms=empty_ms,
                                    min_windows=min_windows)
        b['runs_scanned'] += 1
        if ca.get('problem'):
            b['problems'].append(f'{tag} r{rep}: {ca["problem"]}')
            continue
        if ca.get('problems'):
            b['problems'] += [f'{tag} r{rep}: {p}' for p in ca['problems']]
        stored = run.get('baseline') or {}
        if stored.get('settle_ns') is not None:
            if stored['settle_ns'] != ca['settle_ns']:
                b['notes'].append(
                    f'{tag} r{rep}: recomputed settle_ns {ca["settle_ns"]} != '
                    f'stored {stored["settle_ns"]}')
        elif run.get('settle_ns') is not None \
                and run['settle_ns'] != ca['settle_ns']:
            b['notes'].append(
                f'{tag} r{rep}: recomputed settle_ns {ca["settle_ns"]} != '
                f'stored {run["settle_ns"]}')
        stored_steps = (stored.get('steps') or {})
        if stored_steps:
            stored_total = sum(len(v) for v in stored_steps.values())
            new_total = sum(w.get('post_settle_steps_total', 0)
                            for w in ca.get('windows', {}).values())
            # count distinct step records across durations, not the sum
            if stored_total and new_total == 0:
                b['notes'].append(
                    f'{tag} r{rep}: stored post-settle steps {stored_total} '
                    f'but no window step found')
        pair = compare_pair(rep, pair_edge[0], ca, source=tag)
        if pair.get('problem'):
            b['problems'].append(f'{tag} r{rep}: {pair["problem"]}')
        else:
            b['pairs'].append(pair)
    return b


def decide(batches):
    """Verdict over batches: never OK while any run is unusable."""
    problems, notes, matched = [], [], []
    if not batches:
        return 'MATCHED-WINDOW INSUFFICIENT', ['no-batches'], notes, 1
    for b in batches:
        b['problems'] = list(dict.fromkeys(b.get('problems', [])))
        problems += [f'{Path(b["path"]).name}: {p}' for p in b['problems']]
        notes += [f'{Path(b["path"]).name}: {n}' for n in b.get('notes', [])]
        for p in b['pairs']:
            for key, m in p['intervals'].items():
                if m.get('matched'):
                    matched.append({'batch': Path(b['path']).name,
                                    'source': p['source'],
                                    'repeat': p['repeat'], 'interval': key,
                                    'control_fill_min_ms': m['control_fill_min_ms'],
                                    'control_fill_min_at_s': m['control_fill_min_at_s'],
                                    'action_fill_min_ms': m['action_fill_min_ms']})
    if problems:
        return 'MATCHED-WINDOW REFUSED', problems, notes, 1
    pairs = [p for b in batches for p in b['pairs']]
    if not pairs:
        return 'MATCHED-WINDOW INSUFFICIENT', ['no-scannable-pairs'], notes, 1
    if matched:
        problems = [f'{m["source"]} r{m["repeat"]} {m["interval"]}: control '
                    f'window drained to {m["control_fill_min_ms"]} ms at '
                    f'+{m["control_fill_min_at_s"]}s (action '
                    f'{m["action_fill_min_ms"]} ms)'
                    for m in matched]
        return 'MATCHED-WINDOW NOT SEPARATED', problems, notes, 1
    return 'MATCHED-WINDOW OK', problems, notes, 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sweep', type=Path, action='append', required=True,
                    help='sweep report JSON to reanalyze read-only (repeat '
                         'the flag for several batches)')
    ap.add_argument('--settle-s', type=float, default=1.0)
    ap.add_argument('--target-ms', type=float, default=40.0)
    ap.add_argument('--empty-ms', type=float, default=EMPTY_MS)
    ap.add_argument('--min-windows', type=int, default=MIN_WINDOWS)
    ap.add_argument('--expect-rate', type=int, default=65536,
                    help='required source_rate for every run; 0 disables')
    ap.add_argument('--json', type=Path, default=None)
    args = ap.parse_args()

    missing = [str(p) for p in args.sweep if not p.exists()]
    if missing:
        print('SKIP: missing sweep report(s) ' + ', '.join(missing))
        return 2

    batches = [analyse_batch(p, settle_s=args.settle_s,
                             target_ms=args.target_ms,
                             empty_ms=args.empty_ms,
                             min_windows=args.min_windows,
                             expect_rate=args.expect_rate)
               for p in args.sweep]
    verdict, problems, notes, rc = decide(batches)
    report = {'tool_rule_version': TOOL_RULE_VERSION,
              'settle_s': args.settle_s, 'target_ms': args.target_ms,
              'empty_ms': args.empty_ms, 'min_windows': args.min_windows,
              'expect_rate': args.expect_rate,
              'batches': batches, 'verdict': verdict,
              'problems': problems, 'notes': notes}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + '\n')

    for b in batches:
        print(f'== {Path(b["path"]).name} (file rule '
              f'{b["file_rule_version"]}, file verdict {b["file_verdict"]})')
        print(f'   runs scanned: {b["runs_scanned"]}, pairs: '
              f'{len(b["pairs"])}')
        for p in b['pairs']:
            for key, m in p['intervals'].items():
                print(f'   {p["source"]} r{p["repeat"]} {key}: '
                      f'action {m["action_fill_min_ms"]} ms / '
                      f'{m["action_steps_total"]} steps / gap '
                      f'{m["action_gap_overlap_ms"]} ms over '
                      f'{m["action_duration_ms"]} ms vs control worst of '
                      f'{m["control_windows"]} windows: '
                      f'{m["control_fill_min_ms"]} ms / '
                      f'{m["control_max_steps_in_window"]} steps / gap '
                      f'{m["control_gap_overlap_ms"]} ms '
                      f'{"MATCHED" if m["matched"] else "separated"}')
        for p in b['problems']:
            print(f'   problem: {p}')
        for n in b['notes']:
            print(f'   note: {n}')
    for p in problems:
        print(f'problem: {p}')
    for n in notes:
        print(f'note: {n}')
    print(verdict)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
