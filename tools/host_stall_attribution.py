#!/usr/bin/env python3
"""Attribute post-settle producer stalls to a host-loop code path.

Question this answers: when the producer (audio pushes) stops for tens of
milliseconds in a run with no host action, WHICH code path was the loop
inside while the wall clock advanced? The engine (gbarecomp runtime.cpp,
2026-09-26) emits read-only phase markers into the SAME mutex-serialized
steady-clock stream as the P/C records whenever a host-audio capture is
active:

  fh-enter/fh-exit          frame-present hook (whole per-frame cycle)
  render-enter/-exit        view sync + framebuffer copy/render
  present-enter/-exit       HostWindow::present
  audiopush-enter/-exit     audio drain/mix + producer push
  pumpfn-enter/-exit        pump_host_input (input + assist script)
  rewindcall-enter/-exit    call into capture_rewind_point
  rewind-fire               cadence gate passed; capture is running
  premem-save/postmem-save  in-memory snapshot serialization
  rewind-store              history push/trim after serialization
  hostpump-enter/-exit      HostWindow::pump (SDL window/event pump)
  pumppoll-enter/-exit      SDL_PollEvent, inside HostWindow::pump (the
                            SDL/platform event pump proper; repeated once per
                            dequeued event plus a final empty poll)
  pumphandle-enter/-exit    handling of one dequeued event inside
                            HostWindow::pump (balanced even on the loop's
                            `continue` paths)
  svcev-enter/-exit         SDL_PumpEvents via HostWindow::service_events
                            (the mid-frame host-service hook)
  pace-enter/-exit          FramePacer::wait_for_next_frame

Method (offline, read-only; never writes to a run dir or JSON):

  * find consecutive-push gaps >= `--min-gap-ms` (default 40; normal gaps
    are <= ~21 ms and the known stalls 86-101 ms);
  * classify each gap by the fast-on/off and pause/resume edges INSIDE it
    (fast-forward and pause mute pushes BY DESIGN, and the edges are applied
    in the same pump -- after the frame's push -- so they land within the
    gap) and by the settle boundary: `no-action`, `fast-forward`, `paused`
    or `startup`;
  * for `no-action` gaps, take every adjacent pair of phase markers and
    rank them by their overlap with the gap; the top pair names the code
    path the loop was inside (`path`), with the enclosing marker chain
    (`within`) taken from a validated enter/exit nesting stack;
  * attribution is accepted only when the top pair covers at least
    `--min-attribution-ratio` (default 0.5) of the gap and at least
    `--min-attribution-ms` (default 20). Otherwise the gap is reported
    UNATTRIBUTED with the largest observed marker interval, never dropped.

Rejection rules: a capture without any phase markers (legacy), an
inconsistent timeline, or an unbalanced enter/exit sequence is REFUSED --
attribution is never guessed. Marker gaps whose endpoints are missing
(report boundary) are reported as `unterminated`.

Exit codes: 0 = every unexplained gap attributed; 1 = a run was refused or
some gap stayed unattributed; 2 = missing input.

`--gaps-only` deliberately skips the phase-marker requirement: it finds and
classifies producer gaps only (no attribution). That is how pre-phase-hook
captures can be scanned with the same detector for like-for-like stall-rate
comparisons against post-instrumentation batches.

`evwatch:<type hex>` records (captures taken with GBARECOMP_EVENT_WATCH=1)
and `canary-push-enter/-exit` records (GBARECOMP_EVENT_CANARY=<ms>, written by
the probe's helper thread while it pushes one private user event) are
recognized as known probe markers: they validate the stream but are excluded
from the nesting checks and from the adjacent-interval ranking. The canary
records come from another thread and can interleave with the main-thread
brackets, so they are never treated as phase intervals; attribution semantics
are unchanged whether or not either probe was on. The probe analyses read
those records directly from the events CSV.
"""
import argparse
import json
from pathlib import Path
import re

import host_audio_queue_check as q

ROOT = q.ROOT
TOOL_RULE_VERSION = '2026-09-27-stall-attribution-v6'
EVENT_WATCH_LABEL = re.compile(r'evwatch:[0-9a-f]{8}\Z')
CANARY_LABELS = frozenset(('canary-push-enter', 'canary-push-exit',
                           'canary-push-queued', 'canary-push-rejected'))

PHASE_PAIRS = ('fh', 'render', 'present', 'audiopush', 'pumpfn',
               'rewindcall', 'hostpump', 'pace',
               'pumppoll', 'pumphandle', 'svcev')
SINGLE_MARKERS = (
    'premem-save', 'postmem-save', 'premem-load', 'postmem-load',
    'rewind-fire', 'rewind-store', 'rewind-fail', 'rewind-trigger',
    'presave', 'postsave', 'preload', 'postload',
    'fast-on', 'fast-off', 'pause', 'resume',
)
PATH_MAP = {
    ('rewindcall-enter', 'rewind-fire'):
        'rewind capture: service entry (capacity/frame gate)',
    ('rewind-fire', 'rewind-fail'):
        'rewind capture: in-memory save failed',
    ('rewind-fire', 'premem-save'):
        'rewind capture: entry before serialization',
    ('premem-save', 'postmem-save'):
        'rewind capture: in-memory snapshot serialization',
    ('postmem-save', 'rewind-store'):
        'rewind capture: history push/trim (rewind-store)',
    ('rewind-store', 'rewindcall-exit'):
        'rewind capture: exit path',
    ('rewindcall-enter', 'rewind-store'):
        'rewind capture (unsplit interval)',
    ('hostpump-enter', 'hostpump-exit'):
        'SDL window/event pump (HostWindow::pump)',
    ('pumppoll-enter', 'pumppoll-exit'):
        'SDL event poll inside HostWindow::pump (SDL_PollEvent -> platform '
        'event pump)',
    ('pumphandle-enter', 'pumphandle-exit'):
        'host event handling inside HostWindow::pump (hotkeys, touch, '
        'runtime-UI/ImGui dispatch, input read)',
    ('svcev-enter', 'svcev-exit'):
        'SDL_PumpEvents mid-frame (HostWindow::service_events)',
    ('present-enter', 'present-exit'):
        'SDL present (HostWindow::present)',
    ('pace-enter', 'pace-exit'):
        'frame pacer wait (FramePacer::wait_for_next_frame)',
    ('render-enter', 'render-exit'):
        'view sync + framebuffer copy/render',
    ('audiopush-enter', 'audiopush-exit'):
        'audio drain/mix + producer push',
    ('fh-exit', 'fh-enter'):
        'guest execution between frame hooks',
    ('fh-exit', 'render-enter'):
        'guest execution + loop (hook exit -> next render)',
    ('pace-exit', 'render-enter'):
        'guest execution (outer loop: pacer -> next render)',
    ('fh-enter', 'render-enter'):
        'frame-hook entry before render',
    ('present-exit', 'audiopush-enter'):
        'present return -> audio block',
    ('audiopush-exit', 'pumpfn-enter'):
        'between audio push and input pump',
    ('pumpfn-enter', 'rewindcall-enter'):
        'input pump entry before rewind service',
    ('rewindcall-exit', 'hostpump-enter'):
        'input pump between rewind service and SDL pump',
    ('hostpump-exit', 'pumpfn-exit'):
        'input pump after SDL pump (assist/input handling)',
    ('pumpfn-exit', 'pace-enter'):
        'input pump -> pacer',
    ('pace-exit', 'fh-exit'):
        'pacer -> hook exit (phase bookkeeping)',
    ('pace-exit', 'fh-enter'):
        'between frames: loop/guest execution to next frame hook '
        '(no host service inside this span)',
    ('pace-exit', 'render-enter'):
        'between frames: loop/guest execution to next outer-loop '
        'presentation (no host service inside this span)',
    ('fh-enter', 'fh-exit'):
        'frame-present hook (unsplit interval)',
    ('pumpfn-enter', 'pumpfn-exit'):
        'input pump (unsplit interval)',
}



def marker_records(events):
    return [(i, r) for i, r in enumerate(events) if r['kind'] == 'M']


def is_probe_label(label):
    """True for probe records that are never main-thread phase brackets.

    Event-watch deliveries and canary push brackets are written by the probe
    (the canary pair from a helper thread), so they can appear inside any
    open bracket and must not participate in nesting validation or in the
    adjacent-interval ranking.
    """
    # Do not let a damaged probe label bypass unknown-marker refusal merely
    # because its prefix resembles a valid watch or canary record.
    return EVENT_WATCH_LABEL.fullmatch(label) is not None or label in CANARY_LABELS


def verify_markers(events):
    """Validate the enter/exit nesting; returns (problems, have_phase).

    Legacy captures (only action markers, no phase brackets) report
    have_phase=False so the caller can refuse them instead of guessing.
    """
    problems = []
    stack = []
    have_phase = False
    for i, r in marker_records(events):
        lab = r['label']
        if lab in SINGLE_MARKERS or is_probe_label(lab):
            continue
        if lab.endswith('-enter') and lab[:-6] in PHASE_PAIRS:
            have_phase = True
            stack.append((lab[:-6], i))
            continue
        if lab.endswith('-exit') and lab[:-5] in PHASE_PAIRS:
            have_phase = True
            want = lab[:-5]
            if not stack or stack[-1][0] != want:
                problems.append(f'unbalanced {lab} at index {i}')
            else:
                stack.pop()
            continue
        problems.append(f'unknown marker {lab!r} at index {i}')
    if stack:
        problems.append(f'unterminated {stack[-1][0]}-enter')
    return problems, have_phase


def enclosing_before(events, idx):
    """Validated enter/exit chain open BEFORE the marker at idx."""
    stack = []
    for i, r in marker_records(events):
        if i >= idx:
            break
        lab = r['label']
        if is_probe_label(lab):
            continue
        if lab.endswith('-enter') and lab[:-6] in PHASE_PAIRS:
            stack.append(lab)
        elif lab.endswith('-exit') and lab[:-5] in PHASE_PAIRS and stack:
            stack.pop()
    return stack


def producer_gaps(events):
    pushes = [r['ns'] for r in events if r['kind'] == 'P']
    return [(pushes[k], pushes[k + 1]) for k in range(len(pushes) - 1)]


def state_edges_within(events, start_ns, end_ns, lookback_ns=1_000_000):
    """Fast-on/off and pause/resume edges inside the gap (1 ms lookback).

    The edge is applied in the input pump of the same frame whose push was
    the last one before the mute, so it lands just after `start_ns`; the 1 ms
    lookback only guards against ordering jitter, not against real edges
    before the gap.
    """
    edges = []
    for i, r in marker_records(events):
        if r['ns'] > end_ns:
            break
        if r['ns'] >= start_ns - lookback_ns \
                and r['label'] in ('fast-on', 'fast-off', 'pause', 'resume'):
            edges.append(r['label'])
    return edges


def attribute_gap(events, markers, start_ns, end_ns):
    """Rank adjacent marker intervals by overlap with the gap."""
    best = None
    for k in range(len(markers) - 1):
        (a_idx, a), (_b_idx, b2) = markers[k], markers[k + 1]
        lo, hi = max(a['ns'], start_ns), min(b2['ns'], end_ns)
        overlap = max(0, hi - lo)
        duration = b2['ns'] - a['ns']
        key = (overlap, duration)
        if best is None or key > best[0]:
            best = (key, a_idx, a, b2, overlap)
    if best is None:
        return None
    _key, a_idx, a, b2, overlap = best
    within = enclosing_before(events, a_idx)
    if a['label'].endswith('-exit') and within:
        # The `from` marker closes its own region: the gap lies outside it.
        within = within[:-1]
    return {
        'from': a['label'], 'to': b2['label'],
        'interval_ms': round((b2['ns'] - a['ns']) / 1e6, 4),
        'overlap_ms': round(overlap / 1e6, 4),
        'within': within,
        'path': PATH_MAP.get((a['label'], b2['label']),
                             f'unmapped marker gap ({a["label"]} -> '
                             f'{b2["label"]})'),
    }


def analyse_run(events, settle_s=1.0, min_gap_ms=40.0,
                min_attribution_ms=20.0, min_attribution_ratio=0.5,
                gaps_only=False):
    """gaps_only: scan/classify producer gaps without phase markers.

    Legacy captures (recorded before the phase hooks existed) still carry the
    action markers, so their gaps can be found and classified by context
    (startup / fast-forward / paused / no-action) even though nothing can be
    attributed to a code path. Used for like-for-like stall-rate comparisons
    between pre- and post-instrumentation binaries with one detector.
    """
    out = {'problem': None, 'problems': [], 'gaps': [],
           'gaps_only': gaps_only}
    tl = q.verify_timeline(events)
    out['timeline'] = tl
    if tl['problems']:
        out['problem'] = 'capture timeline inconsistent: ' + '; '.join(
            tl['problems'])
        return out
    if not gaps_only:
        problems, have_phase = verify_markers(events)
        if not have_phase:
            out['problem'] = ('no phase markers in capture (legacy): stall '
                              'attribution unavailable')
            return out
        if problems:
            out['problem'] = 'phase marker stream malformed: ' + '; '.join(
                problems[:3])
            return out
    first_push = next((r['ns'] for r in events if r['kind'] == 'P'), None)
    if first_push is None:
        out['problem'] = 'capture has no producer pushes'
        return out
    settle_ns = first_push + int(settle_s * 1e9)
    out['settle_ns'] = settle_ns
    # Probe records validate the stream but must not fragment the
    # adjacent-interval ranking (v4; unchanged for captures without them).
    markers = [] if gaps_only else [
        (i, r) for i, r in marker_records(events)
        if not is_probe_label(r['label'])]
    for (start_ns, end_ns) in producer_gaps(events):
        gap_ms = (end_ns - start_ns) / 1e6
        if gap_ms < min_gap_ms:
            continue
        edges = state_edges_within(events, start_ns, end_ns)
        # A gap that begins before the cutoff is a startup/boundary gap,
        # even if its next push arrives afterward.  Classifying by the end
        # falsely promotes a startup delay into a post-settle stall.
        if start_ns < settle_ns:
            context, expected = 'startup', True
        elif 'fast-on' in edges:
            context, expected = 'fast-forward', True
        elif 'pause' in edges:
            context, expected = 'paused', True
        else:
            context, expected = 'no-action', False
        pulls = [r['ns'] for r in events
                 if r['kind'] == 'C' and start_ns <= r['ns'] <= end_ns]
        intervals = sorted((pulls[k + 1] - pulls[k]) / 1e6
                           for k in range(len(pulls) - 1))
        rec = {
            'gap_ms': round(gap_ms, 4),
            'start_s': round((start_ns - first_push) / 1e9, 4),
            'end_s': round((end_ns - first_push) / 1e9, 4),
            'post_settle': start_ns >= settle_ns,
            'context': context,
            'expected': expected,
            'pulls': {
                'n': len(pulls),
                'median_interval_ms': (round(intervals[len(intervals) // 2],
                                             4) if intervals else None),
                'min_interval_ms': (round(intervals[0], 4)
                                    if intervals else None),
                'max_interval_ms': (round(intervals[-1], 4)
                                    if intervals else None),
            },
        }
        attr = (attribute_gap(events, markers, start_ns, end_ns)
                if markers else None)
        rec['marker_gap'] = attr
        if context == 'startup':
            rec['attribution'] = ('excluded (pre-settle startup window; not '
                                  'attributed either way)')
        elif expected:
            rec['attribution'] = 'expected (pushes muted by ' + context + ')'
        elif gaps_only:
            rec['attribution'] = ('UNATTRIBUTED (gaps-only scan; phase '
                                  'markers not used)')
        elif attr is None:
            rec['attribution'] = 'UNATTRIBUTED (no marker interval overlaps)'
        elif (attr['overlap_ms'] >= min_attribution_ms
                and attr['overlap_ms'] >= min_attribution_ratio * gap_ms):
            rec['attribution'] = attr['path']
        else:
            rec['attribution'] = ('UNATTRIBUTED (largest marker interval '
                                  f'{attr["interval_ms"]} ms: {attr["from"]}'
                                  f' -> {attr["to"]})')
        out['gaps'].append(rec)
    out['unexplained'] = [g for g in out['gaps'] if not g['expected']]
    out['attributed'] = [g for g in out['unexplained']
                         if not g['attribution'].startswith('UNATTRIBUTED')]
    out['unattributed'] = [g for g in out['unexplained']
                           if g['attribution'].startswith('UNATTRIBUTED')]
    return out


def run_sources(sweeps, run_dirs):
    """(label, events_path) pairs from sweep JSONs and/or run dirs."""
    sources, problems = [], []
    for sp in sweeps:
        if not sp.exists():
            problems.append(f'missing sweep report {sp}')
            continue
        d = json.loads(sp.read_text())
        bucket = [(f"{sp.name}:{b.get('tag', '?')} r{b.get('repeat')}",
                   b.get('events_path')) for key in
                  ('edges_runs', 'control_runs', 'control2_runs')
                  for b in (d.get(key) or [])]
        sources += bucket
    for rd in run_dirs:
        p = Path(rd)
        csv = p if p.suffix == '.csv' else p / 'cap-events.csv'
        sources.append((str(p), str(csv)))
    return sources, problems


def decide(runs, gaps_only=False):
    refused = [r for r in runs if r.get('problem')]
    unexplained = sum(len(r.get('unexplained') or []) for r in runs)
    unattributed = sum(len(r.get('unattributed') or []) for r in runs)
    prefix = 'GAP-SCAN' if gaps_only else 'STALL-ATTR'
    if refused:
        return f'{prefix} REFUSED', refused, unexplained, unattributed, 1
    if not runs:
        return f'{prefix} INSUFFICIENT', runs, 0, 0, 1
    if gaps_only:
        return 'GAP-SCAN OK', [], unexplained, unattributed, 0
    if unattributed:
        return 'STALL-ATTR PARTIAL', [], unexplained, unattributed, 1
    return 'STALL-ATTR OK', [], unexplained, unattributed, 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sweep', type=Path, action='append', default=[],
                    help='sweep report JSON whose runs to analyse '
                         '(repeatable)')
    ap.add_argument('--run-dir', action='append', default=[],
                    help='run directory (cap-events.csv) or CSV path '
                         '(repeatable)')
    ap.add_argument('--settle-s', type=float, default=1.0)
    ap.add_argument('--min-gap-ms', type=float, default=40.0)
    ap.add_argument('--min-attribution-ms', type=float, default=20.0)
    ap.add_argument('--min-attribution-ratio', type=float, default=0.5)
    ap.add_argument('--gaps-only', action='store_true',
                    help='scan and classify producer gaps without requiring '
                         'or using phase markers (legacy captures; no '
                         'attribution, used for pre/post-instrumentation '
                         'stall-rate comparisons)')
    ap.add_argument('--json', type=Path, default=None)
    args = ap.parse_args()

    if not args.sweep and not args.run_dir:
        print('SKIP: nothing to analyse (--sweep / --run-dir)')
        return 2
    sources, problems = run_sources(args.sweep, args.run_dir)
    for p in problems:
        print(f'problem: {p}')
    if problems and not sources:
        return 2

    runs = []
    for label, events_path in sources:
        path = Path(events_path) if events_path else None
        if not path or not path.exists():
            runs.append({'label': label, 'events_path': str(events_path),
                         'problem': 'capture events file missing'})
            continue
        events = q.read_events(path)
        r = analyse_run(events, settle_s=args.settle_s,
                        min_gap_ms=args.min_gap_ms,
                        min_attribution_ms=args.min_attribution_ms,
                        min_attribution_ratio=args.min_attribution_ratio,
                        gaps_only=args.gaps_only)
        r['label'] = label
        r['events_path'] = str(path)
        runs.append(r)

    verdict, refused, unexplained, unattributed, rc = decide(
        runs, gaps_only=args.gaps_only)
    report = {'tool_rule_version': TOOL_RULE_VERSION,
              'settle_s': args.settle_s, 'min_gap_ms': args.min_gap_ms,
              'gaps_only': args.gaps_only,
              'runs': runs, 'verdict': verdict,
              'unexplained_gaps': unexplained,
              'unattributed_gaps': unattributed}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + '\n')

    for r in runs:
        if r.get('problem'):
            print(f'{r["label"]}: PROBLEM {r["problem"]}')
            continue
        print(f'{r["label"]}: gaps >= {args.min_gap_ms:g} ms: '
              f'{len(r["gaps"])} '
              f'({len(r["unexplained"])} unexplained)')
        for g in r['gaps']:
            mg = g['marker_gap']
            within = (' within ' + '>'.join(mg['within'])) if mg and \
                mg['within'] else ''
            detail = (f"marker gap {mg['from']} -> {mg['to']} "
                      f"{mg['interval_ms']} ms ({mg['overlap_ms']} overlap)"
                      f"{within}" if mg else 'no overlapping marker pair')
            print(f"   +{g['start_s']:8.4f}s {g['gap_ms']:8.2f} ms "
                  f"[{g['context']}] {g['attribution']}")
            print(f"            {detail}; pulls={g['pulls']['n']} "
                  f"median={g['pulls']['median_interval_ms']} ms")
    print(f'unexplained gaps: {unexplained}, unattributed: {unattributed}')
    if args.gaps_only:
        print(f'gaps-only scan: captured gaps classified, none attributed '
              f'(/ {len(runs)} runs)')
    print(verdict)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
