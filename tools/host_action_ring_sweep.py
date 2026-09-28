#!/usr/bin/env python3
"""Repeatable sweep of host-action edges (fast-forward / pause / resume).

Question this answers: what does the host audio ring do at fast-forward and
pause/resume edges, and do the same steps appear in paired no-action controls?

Mechanism (all read-only; no engine behavior change): the capture records
`kind='M'` action markers in the same mutex-serialized steady-clock timeline
as the producer pushes (`P`) and device pulls (`C`). This tool runs, from the
same banked fixture and trace:

  edges    GBARECOMP_ASSIST_SCRIPT="20:save1;120:fast_on;140:fast_off;"
                                  "170:pause;190:resume;210:save2"
           -> markers fast-on / fast-off / pause / resume, one each, in order.
  control  GBARECOMP_ASSIST_SCRIPT="20:save1"  (no host actions)

For every edges run it measures, per interval between consecutive action
markers (fast-on->fast-off, pause->resume, and the recovery interval after
each closing edge):

  fill_at_edge_ms   ring fill recorded AT the edge marker (exact boundary).
  fill_min/max/mean ring fill over the P/C/M records inside the interval.
  empty             whether fill reached <= 2 ms inside the interval.
  steps             bridge counter steps (stretch / underrun / overflow)
                    inside the interval, read from the MERGED event stream
                    with their own steady-clock ns.
  recovery_ms       time from the closing edge until fill is back >= target
                    (40 ms) again, or None if the capture ends first.

The CONTROL run supplies the baseline this must be read against: its
post-prime fill band and every counter step it takes anywhere. A step in an
edges run is reported as associated with a control-clean batch when the
controls are step-free; this is not proof of an exclusive or causal effect.
If a control steps, the sweep reports the steps and refuses the association
(exit 1).

SETTLE WINDOW. Sometimes -- not always -- a run takes a burst of
stretch-concealment steps ~0.12-0.20 s after the first push: capture startup
/ the initial state load stalling the producer while the device drains the
ring. It happens with NO host action, so it is never attributable: observed
in 1/10 runs of the first settled batch (an edges run) and, at 160 frames, in
4/5 no-action controls. Such steps are excluded from attribution in BOTH
branches: steps before `--settle-s` (default 1.0 s after the first producer
push) are reported as `startup_steps` (context) and the actions are scripted
well after it. If a CONTROL run still moves a counter after the settle
window, attribution is refused rather than forced.

FRESH-EVIDENCE OPTIONS (defaults unchanged for retained artifacts):
--run-root writes the per-run capture dirs under another ignored build
path, --heal-cache DIR runs every branch against one persistent warm heal
cache instead of a fresh per-run one (a cold cache makes each run compile and
first-load ~90 new shard paths, which parks the event pump - see
AUDIO_REVIEW §5o-§5q - so the two protocols do not measure the same base
rate), and --control-script-2 adds a second no-action branch per repeat
(e.g. the edges branch's save checkpoints without fast-forward / pause /
resume). That branch is reported as `control2_runs`, its post-settle
counter steps count toward the same control-clean gate, and
`tools/host_action_baseline.py` scans it as part of the matched-window
no-action baseline.

Statistics over --repeat runs are reported as distributions (min / median /
max, plus the absolute `spread` and `rel_spread`). `consistent` is strict --
every repeat must round to the same 3-decimal value -- so a tight but not
bit-identical metric is flagged variable; read `spread` before treating a
metric as unsettled; present variable metrics as ranges rather than invariants.

Context (by design, not a defect): while fast-forward is active the runtime
does not push guest audio into the ring at all (`push_audio_samples` is
skipped when `fast_forward_active`), and while paused no guest frames run, so
in both cases the ring only drains. This tool measures the host-ring
consequence; it says nothing about the guest-side audio that fast-forward
discards.

Exit codes: 0 = measured with at least two repeats and clean controls; 1 =
measured but not attributable, or a run failed validation; 2 = missing
fixture. Metric variability is reported as a distribution and does not by
itself fail the sweep. Private assets are
used via disposable copies in an ignored run dir; the source save is
hash-verified and never written.
"""
import argparse
import json
import statistics
import tempfile
from pathlib import Path

import host_audio_queue_check as q

ROOT = q.ROOT
TOOL_RULE_VERSION = '2026-09-26-host-action-sweep-v2'

EDGE_LABELS = ('fast-on', 'fast-off', 'pause', 'resume')
COUNTERS = q.COUNTERS
REQUIRED_AGGREGATES = (
    'fill_at_fast_on_ms', 'fill_at_fast_off_ms',
    'fill_at_pause_ms', 'fill_at_resume_ms',
    'fast_interval_fill_min_ms', 'pause_interval_fill_min_ms',
    'recovery_after_fast_off_s', 'recovery_after_resume_s',
)


def interval_metrics(events, start_idx, end_idx, target_ms, settle_ns):
    """Fill/counter facts strictly inside (start_idx, end_idx).

    Counter steps before `settle_ns` are startup concealment and are not
    counted here (see the module docstring).
    """
    span = events[start_idx:end_idx + 1]
    records = [r for r in span[1:-1] if r['kind'] in ('P', 'C', 'M')]
    fills = [r['fill_ms'] for r in records]
    steps = {c: [] for c in COUNTERS}
    for c in COUNTERS:
        for (idx, before, after) in q.counter_steps(events, c):
            if start_idx < idx < end_idx and events[idx]['ns'] >= settle_ns:
                steps[c].append([idx, before, after])
    out = {
        'duration_s': round((events[end_idx]['ns'] - events[start_idx]['ns'])
                            / 1e9, 4),
        'records': len(records),
        'fill_min_ms': min(fills) if fills else None,
        'fill_max_ms': max(fills) if fills else None,
        'fill_mean_ms': round(sum(fills) / len(fills), 3) if fills else None,
        'fill_empty': bool(fills) and min(fills) <= 2.0,
        'steps': {c: v for c, v in steps.items() if v},
        'step_counts': {c: len(v) for c, v in steps.items()},
    }
    # Recovery: first record after the closing edge back at the target fill.
    recovery = None
    for r in events[end_idx + 1:]:
        if r['fill_ms'] >= target_ms:
            recovery = round((r['ns'] - events[end_idx]['ns']) / 1e9, 4)
            break
    out['recovery_s'] = recovery
    return out


def analyse_edges(res, target_ms, settle_ns):
    """Per-edge analysis for one edges run. No pump arithmetic."""
    events = res['events']
    tl = q.verify_timeline(events)
    a = {'timeline': tl, 'problem': None}
    if tl['problems']:
        a['problem'] = 'capture timeline inconsistent: ' + '; '.join(
            tl['problems'])
        return a, None
    indices = {}
    for lab in EDGE_LABELS:
        hits = q.marker_indices(events, lab)
        if len(hits) != 1:
            a['problem'] = f'expected exactly one {lab!r} marker, found {len(hits)}'
            return a, None
        indices[lab] = hits[0]
    order = [lab for lab in sorted(indices, key=lambda k: indices[k])]
    if order != list(EDGE_LABELS):
        a['problem'] = f'edge markers out of order: {order}'
        return a, None
    if settle_ns >= events[indices['fast-on']]['ns']:
        a['problem'] = 'settle window reaches the first host-action marker'
        return a, None
    a['edges'] = {lab: q.ring_depth_at(events, indices[lab], res['source_rate'])
                  for lab in EDGE_LABELS}
    a['fast_interval'] = interval_metrics(events, indices['fast-on'],
                                          indices['fast-off'], target_ms,
                                          settle_ns)
    a['pause_interval'] = interval_metrics(events, indices['pause'],
                                           indices['resume'], target_ms,
                                           settle_ns)
    a['post_fast_recovery'] = a['fast_interval']['recovery_s']
    a['post_pause_recovery'] = a['pause_interval']['recovery_s']
    a['all_steps'] = {c: q.counter_steps(events, c) for c in COUNTERS}
    a['startup_steps'] = {c: [s for s in a['all_steps'][c]
                              if events[s[0]]['ns'] < settle_ns]
                          for c in COUNTERS}
    a['startup_steps_total'] = sum(len(v) for v in a['startup_steps'].values())
    return a, events


def control_baseline(res, settle_ns):
    """Post-settle baseline: fill band and counter steps after `settle_ns`.

    Pre-settle steps are reported separately as startup concealment; they can
    occur in either branch and are never associated with the host action.
    """
    events = res['events']
    tl = q.verify_timeline(events)
    first_push = next((i for i, r in enumerate(events) if r['kind'] == 'P'),
                      None)
    b = {'timeline': tl, 'problem': None}
    if tl['problems']:
        b['problem'] = 'control capture timeline inconsistent: ' + '; '.join(
            tl['problems'])
        return b
    if first_push is None:
        b['problem'] = 'control capture has no producer pushes'
        return b
    rows = [r for r in events[first_push:] if r['kind'] in ('P', 'C')]
    fills = [r['fill_ms'] for r in rows]
    b['fill_min_ms'] = min(fills)
    b['fill_max_ms'] = max(fills)
    b['fill_mean_ms'] = round(sum(fills) / len(fills), 3)
    all_steps = {c: q.counter_steps(events[first_push:], c)
                 for c in COUNTERS}
    b['all_step_counts'] = {c: len(v) for c, v in all_steps.items()}
    b['steps'] = {c: [s for s in v if events[first_push + s[0]]['ns']
                      >= settle_ns]
                  for c, v in all_steps.items()}
    b['step_counts'] = {c: len(v) for c, v in b['steps'].items()}
    b['steps_total'] = sum(b['step_counts'].values())
    b['startup_steps'] = {c: b['all_step_counts'][c] - b['step_counts'][c]
                          for c in COUNTERS}
    b['startup_steps_total'] = sum(b['startup_steps'].values())
    return b


def aggregate(rows, key):
    """Distribution summary of a numeric metric over repeats."""
    vals = [r['analysis'].get(key) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {'n': 0}
    med = statistics.median(vals)
    spread = max(vals) - min(vals)
    return {'n': len(vals), 'min': min(vals), 'median': med, 'max': max(vals),
            'spread': spread, 'rel_spread': (spread / med) if med else None,
            'consistent': len(set(round(v, 3) for v in vals)) == 1}


def decide(report):
    """Single verdict gate; QUEUE-style: never OK without control-clean data."""
    bad, notes = [], []
    if not report.get('source_save_unchanged'):
        bad.append('source_save_unchanged')
    edges = report['edges_runs']
    controls = report['control_runs']
    # `control2_runs` is an optional second no-action branch (same fixture and
    # save checkpoints as the edges branch, still no fast/pause/resume). Any
    # control2 step is a no-action step, so it counts toward the same gate.
    controls2 = report.get('control2_runs') or []
    if not edges or not controls:
        bad.append('missing-edges-or-control-runs')
        return 'SWEEP FAIL', bad, notes, 1
    if len(edges) < 2 or len(controls) < 2 or len(edges) != len(controls):
        bad.append('need at least two paired edge/control repeats')
    for b in edges + controls + controls2:
        tag = f"{b['tag']}/r{b['repeat']}"
        if b['exit'] != 0 or b['timed_out'] or b['truncated'] \
                or not b['capture']:
            bad.append(f'{tag}: run did not complete cleanly')
        if b.get('analysis', {}).get('problem'):
            bad.append(f'{tag}: {b["analysis"]["problem"]}')
        if b.get('baseline') and b['baseline'].get('problem'):
            bad.append(f'{tag}: {b["baseline"]["problem"]}')
    ok_controls = [c for c in controls + controls2
                   if not c['baseline'].get('problem')]
    if not ok_controls:
        bad.append('no-usable-control')
        return 'SWEEP FAIL', bad, notes, 1
    control_steps = sum(c['baseline']['steps_total'] for c in ok_controls)
    notes.append(f'control counter steps over {len(ok_controls)} runs: '
                 f'{control_steps}')
    if control_steps:
        bad.append('control-not-clean: the no-action runs themselves moved a '
                   'bridge counter, so edge-interval steps cannot be '
                   'attributed to the host actions')
    # Edge-interval steps are reported either way; attribution needs control.
    interval_steps = 0
    for b in edges:
        a = b['analysis']
        for key in ('fast_interval', 'pause_interval'):
            interval_steps += sum((a.get(key) or {}).get('step_counts', {})
                                  .values())
    report['edge_interval_step_total'] = interval_steps
    # Distribution support: a metric claimed as an effect must repeat.
    for name in REQUIRED_AGGREGATES:
        if name not in report['aggregates']:
            bad.append(f'{name}: required aggregate missing')
    for name, agg in report['aggregates'].items():
        if agg.get('n') != len(edges):
            bad.append(f'{name}: missing metric in one or more edge repeats')
        if agg.get('n') and not agg.get('consistent'):
            notes.append(f'{name}: variable across repeats '
                         f'(min={agg.get("min")} max={agg.get("max")})')
    report['attribution'] = ('control-clean association, not causal proof'
                             if not bad else 'refused (control or run invalid)')
    if not bad:
        return 'SWEEP OK', bad, notes, 0
    return 'SWEEP FAIL', bad, notes, 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--exe', type=Path, default=ROOT / 'build/MMBN3WhiteRecomp')
    ap.add_argument('--state', type=Path,
                    default=ROOT / 'build/winreplay/gate2-battleI-action/'
                                 'rom.state1')
    ap.add_argument('--trace', type=Path,
                    default=ROOT / 'build/winreplay/gate2-battleJ-action2/'
                                 'trace.csv')
    ap.add_argument('--frames', type=int, default=240)
    ap.add_argument('--repeat', type=int, default=5,
                    help='repeats per branch; only the distribution is '
                         'reported, so a single run cannot carry a claim')
    ap.add_argument('--run-root', type=Path,
                    default=ROOT / 'build/host-action-sweep',
                    help='directory for the per-run capture dirs (default: '
                         'build/host-action-sweep); point fresh evidence at '
                         'a new ignored build path instead of reusing one')
    ap.add_argument('--env', dest='extra_env', action='append', default=[],
                    metavar='KEY=VALUE',
                    help='diagnostic toggles for every run: '
                         'GBARECOMP_PHASE_MARKERS=0/1, '
                         'GBARECOMP_EVENT_WATCH=0/1, '
                         'GBARECOMP_EVENT_CANARY=<0-1000 ms>, '
                         'GBARECOMP_SDL_COST=<0-1000 ms>, '
                         'GBARECOMP_NO_GAMEPAD=0/1 or '
                         'GBARECOMP_LOAD_TRACE=0/1, '
                         'GBARECOMP_HEAL_PREWARM_MAP=0/1 or '
                         'GBARECOMP_HEAL_OUT_OF_PROCESS_LOAD=0/1 '
                         '(repeatable; last '
                         'value wins). Other overrides are rejected so the fixture '
                         'and branch settings stay fixed. Parent '
                         'GBARECOMP_* variables are dropped; the toggles are '
                         'recorded as env_overrides in the report')
    ap.add_argument('--control-script-2', default=None,
                    help='optional second no-action branch run per repeat and '
                         'reported as control2_runs (e.g. the edges branch\'s '
                         'save checkpoints without fast/pause/resume); its '
                         'counter steps count toward the control-clean gate')
    ap.add_argument('--target-ms', type=float, default=40.0,
                    help='servo target / recovery threshold (matches the '
                         'engine cushion)')
    ap.add_argument('--settle-s', type=float, default=1.0,
                    help='ignore counter steps in the first N seconds after '
                         'the first producer push (shared startup/priming '
                         'concealment; reported separately, never attributed)')
    ap.add_argument('--timeout', type=float, default=300)
    ap.add_argument('--heal-cache', type=Path, default=None,
                    help='persistent heal-cache directory shared by every '
                         'run (warm-cache arm). Default: a fresh cache per '
                         'run, under which each run compiles and first-loads '
                         '~90 new shard paths - the engine-side dyld loaders '
                         'lock hold measured in AUDIO_REVIEW §5o-§5q')
    ap.add_argument('--json', type=Path, default=None)
    args = ap.parse_args()

    if not args.state.exists() or not args.trace.exists():
        print(f'SKIP: missing fixture {args.state} / {args.trace}')
        return 2

    # Actions are scripted WELL after the settle window so the startup
    # concealment burst (~0.12-0.20 s after the first push) can never fall
    # inside an edge interval.
    edges_script = ('20:save1;120:fast_on;140:fast_off;'
                    '170:pause;190:resume;210:save2')
    control_script = '20:save1'
    source = ROOT / 'saves/mmbn3_white_usa.sav'
    before = q.sha(source)
    out_root = (args.run_root if args.run_root.is_absolute()
                else ROOT / args.run_root)
    out_root.mkdir(parents=True, exist_ok=True)

    edges_runs, control_runs, control2_runs = [], [], []
    branches = [('edges', edges_script, edges_runs),
                ('control', control_script, control_runs)]
    if args.control_script_2:
        branches.append(('control2', args.control_script_2, control2_runs))
    for rep in range(max(1, args.repeat)):
        for tag, script, bucket in branches:
            d = Path(tempfile.mkdtemp(prefix=f'{tag}-r{rep}-', dir=out_root))
            res = q.run_branch(tag, script, 'off', args.frames, d, args)
            res['repeat'] = rep
            res['source_rate'] = 65536
            bucket.append(res)
            events = res.pop('events')
            first_push = next((r['ns'] for r in events if r['kind'] == 'P'),
                              None)
            settle_ns = ((first_push + int(args.settle_s * 1e9))
                         if first_push is not None else 0)
            if tag == 'edges':
                res['settle_ns'] = settle_ns
                res['analysis'], _ = analyse_edges(
                    {'events': events, 'source_rate': 65536},
                    args.target_ms, settle_ns)
                a = res['analysis']
            else:
                res['analysis'] = {}
                a = None
            if tag == 'edges':
                fmin = (a.get('fast_interval') or {}).get('fill_min_ms')
                pmin = (a.get('pause_interval') or {}).get('fill_min_ms')
                print(f"[edges r{rep}] "
                      f"fill@on={(a.get('edges', {}).get('fast-on') or {}).get('fill_ms')} "
                      f"fill@off={(a.get('edges', {}).get('fast-off') or {}).get('fill_ms')} "
                      f"fast_min={fmin} fast_steps="
                      f"{(a.get('fast_interval') or {}).get('step_counts')} "
                      f"fill@pause={(a.get('edges', {}).get('pause') or {}).get('fill_ms')} "
                      f"fill@resume={(a.get('edges', {}).get('resume') or {}).get('fill_ms')} "
                      f"pause_min={pmin} pause_steps="
                      f"{(a.get('pause_interval') or {}).get('step_counts')} "
                      f"recover_fast={(a.get('post_fast_recovery'))}s "
                      f"recover_pause={(a.get('post_pause_recovery'))}s "
                      f"startup={a.get('startup_steps_total')} "
                      f"{res['seconds']}s"
                      + (f" ERR={a.get('problem')}" if a.get('problem') else ''),
                      flush=True)
            else:
                res['settle_ns'] = settle_ns
                res['baseline'] = control_baseline({'events': events},
                                                   settle_ns)
                b = res['baseline']
                print(f"[{tag} r{rep}] fill={b.get('fill_min_ms')}-"
                      f"{b.get('fill_max_ms')}ms "
                      f"post_settle_steps={b.get('step_counts')} "
                      f"startup_steps={b.get('startup_steps')} "
                      f"{res['seconds']}s", flush=True)

    # Flatten the per-edge numbers for the aggregate helper, then summarise
    # every metric as a distribution over repeats.
    for b in edges_runs:
        a = b['analysis']
        e = a.get('edges') or {}
        a['edges_fast_on'] = (e.get('fast-on') or {}).get('fill_ms')
        a['edges_fast_off'] = (e.get('fast-off') or {}).get('fill_ms')
        a['edges_pause'] = (e.get('pause') or {}).get('fill_ms')
        a['edges_resume'] = (e.get('resume') or {}).get('fill_ms')
        a['fast_interval_fill_min'] = (a.get('fast_interval')
                                       or {}).get('fill_min_ms')
        a['pause_interval_fill_min'] = (a.get('pause_interval')
                                        or {}).get('fill_min_ms')
    aggregates = {name: aggregate(edges_runs, key) for name, key in {
        'fill_at_fast_on_ms': 'edges_fast_on',
        'fill_at_fast_off_ms': 'edges_fast_off',
        'fill_at_pause_ms': 'edges_pause',
        'fill_at_resume_ms': 'edges_resume',
        'fast_interval_fill_min_ms': 'fast_interval_fill_min',
        'pause_interval_fill_min_ms': 'pause_interval_fill_min',
        'recovery_after_fast_off_s': 'post_fast_recovery',
        'recovery_after_resume_s': 'post_pause_recovery',
    }.items()}

    src_ok = q.sha(source) == before
    scripts = {'edges': edges_script, 'control': control_script}
    report = {
        'tool_rule_version': TOOL_RULE_VERSION,
        'edges_runs': edges_runs, 'control_runs': control_runs,
        'aggregates': aggregates, 'source_save_unchanged': src_ok,
        'run_root': str(out_root),
        'binary_sha256': q.sha(args.exe),
        'frames': args.frames, 'repeat': args.repeat,
        'target_ms': args.target_ms,
        'scripts': scripts,
        'env_overrides': list(args.extra_env),
        'heal_cache': str(args.heal_cache) if args.heal_cache else None,
    }
    if control2_runs:
        scripts['control2'] = args.control_script_2
        report['control2_runs'] = control2_runs
    verdict, bad, notes, rc = decide(report)
    report['verdict'] = verdict
    report['failures'] = bad
    report['notes'] = notes
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + '\n')

    print(f'\nsource save unchanged: {src_ok}')
    print(f'binary sha256: {report["binary_sha256"]}')
    print('control baseline:')
    for c in control_runs + control2_runs:
        b = c['baseline']
        print(f"  {c['tag']} r{c['repeat']}: fill {b.get('fill_min_ms')}-"
              f"{b.get('fill_max_ms')}ms (mean {b.get('fill_mean_ms')}), "
              f"steps={b.get('step_counts')}")
    print('edge metric distributions (min/median/max over repeats):')
    for name, agg in aggregates.items():
        if agg.get('n'):
            print(f"  {name}: {agg['min']} / {agg['median']} / {agg['max']} "
                  f"(n={agg['n']}, spread={agg['spread']:g}, "
                  f"consistent={agg['consistent']})")
    print(f"edge-interval counter steps measured: "
          f"{report.get('edge_interval_step_total')}")
    for n in notes:
        print(f'note: {n}')
    print(verdict + (f' {bad}' if bad else ''))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
