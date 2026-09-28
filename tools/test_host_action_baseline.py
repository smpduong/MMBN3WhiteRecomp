#!/usr/bin/env python3
"""Unit tests for host_action_baseline (no game run required).

Covers the rules the matched-window no-action comparison stands on:

  * window boundaries: a counter step on the anchor record OR on the record at
    anchor+D is excluded from that window; a step strictly inside is counted;
    fill_min follows the same half-open span rule;
  * settle exclusion: windows never start before the settle boundary and
    pre-settle steps/fills are not attributed;
  * rejection: malformed timelines, push-free captures, captures with no or
    too few windows, short post-settle spans, wrong/absent rates, missing
    event files and failed run records are refused -- never silently dropped
    and never turned green;
  * run-level aggregation: counts are per run, and a matched control window is
    reported with its position;
  * pairing: matched windows take their duration from the paired edges run;
  * separation verdicts: OK / NOT SEPARATED / REFUSED / INSUFFICIENT.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import host_action_baseline as b  # noqa: E402
import host_action_ring_sweep as s  # noqa: E402

MS = 1_000_000
NS0 = 1_000_000_000
HEADER = ('kind,ns,offset,frames,fill_ms,stretch_frames,underrun_frames,'
          'overflow_frames,label')


def ev(kind, ns, fill=30.0, counters=None, offset=0, frames=0, label=''):
    row = {'kind': kind, 'ns': ns, 'offset': offset, 'frames': frames,
           'fill_ms': fill, 'label': label}
    row.update(counters or {c: 0 for c in b.COUNTERS})
    return row


def stream(n_ms, fills=None, steps=(), markers=(), skip_push_ms=()):
    """1 ms grid: P at +0.0 ms, C at +0.5 ms, optional markers 1 us early.

    fills : dict {ms: fill} or callable(ms, kind) -> fill (default 30.0).
    steps : (ms, kind, counter, delta); cumulative from that record onward.
    markers: (ms, kind, label) inserted just before that record; the marker
             carries the counter values BEFORE that record's own delta.
    """
    fill_of = fills if callable(fills) else \
        (lambda ms, kind: (fills or {}).get(ms, 30.0))
    skips = set(skip_push_ms)
    deltas = {}
    for (ms, kind, counter, delta) in steps:
        d = deltas.setdefault((ms, kind), {})
        d[counter] = d.get(counter, 0) + delta
    counters = {c: 0 for c in b.COUNTERS}
    src = out = 0
    rows = []
    for ms in range(n_ms):
        for kind, frac in (('P', 0.0), ('C', 0.5)):
            if kind == 'P' and ms in skips:
                continue
            t = NS0 + ms * MS + int(frac * MS)
            fill = fill_of(ms, kind)
            for (m, k, label) in markers:
                if m == ms and k == kind:
                    rows.append(ev('M', t - 1000, fill, dict(counters),
                                   label=label))
            for c, d in deltas.get((ms, kind), {}).items():
                counters[c] += d
            if kind == 'P':
                rows.append(ev('P', t, fill, dict(counters), offset=src,
                               frames=64))
                src += 64
            else:
                rows.append(ev('C', t, fill, dict(counters), offset=out,
                               frames=512))
                out += 512
    return rows


def ridx(events, kind, ms):
    t = NS0 + ms * MS + (500_000 if kind == 'C' else 0)
    for i, r in enumerate(events):
        if r['kind'] == kind and r['ns'] == t:
            return i
    raise AssertionError(f'no {kind} record at ms {ms}')


def write_csv(path, events):
    with Path(path).open('w') as fh:
        fh.write(HEADER + '\n')
        for r in events:
            fh.write(f"{r['kind']},{r['ns']},{r['offset']},{r['frames']},"
                     f"{r['fill_ms']:.6f},{r['stretch_frames']},"
                     f"{r['underrun_frames']},{r['overflow_frames']},"
                     f"{r['label']}\n")


def run_record(tmp, tag, repeat, events, rate=65536, path=None, **kw):
    p = Path(path) if path else tmp / f'{tag}-r{repeat}.csv'
    if path is None:
        write_csv(p, events)
    rec = {'tag': tag, 'repeat': repeat, 'source_rate': rate, 'exit': 0,
           'timed_out': False, 'truncated': False,
           'capture': {'source_frames': 1}, 'events_path': str(p),
           'dir': str(tmp), 'analysis': {}, 'baseline': {}}
    rec.update(kw)
    return rec


def sweep_json(tmp, name, edges, controls, controls2=()):
    payload = {'tool_rule_version': '2026-09-26-host-action-sweep-v2',
               'verdict': 'SWEEP OK', 'edges_runs': edges,
               'control_runs': controls}
    if controls2:
        payload['control2_runs'] = list(controls2)
    p = tmp / name
    p.write_text(json.dumps(payload))
    return p


def edge_markers(fast=(200, 230), pause=(260, 310)):
    return [(fast[0], 'P', 'fast-on'), (fast[1], 'P', 'fast-off'),
            (pause[0], 'P', 'pause'), (pause[1], 'P', 'resume')]


class ScanWindowTest(unittest.TestCase):
    def test_step_on_anchor_record_is_excluded_from_that_window(self):
        evs = stream(12, steps=[(5, 'P', 'stretch_frames', 512)])
        _summ, detail = b.scan_windows(evs, NS0, 2 * MS, keep_detail=True)
        by = {w['anchor_idx']: w for w in detail}
        self.assertEqual(by[ridx(evs, 'P', 5)]['steps_total'], 0)
        self.assertEqual(by[ridx(evs, 'P', 4)]['steps_total'], 1)
        # a window that STARTS after the step cannot see it either
        self.assertEqual(by[ridx(evs, 'C', 5)]['steps_total'], 0)
        self.assertEqual(by[ridx(evs, 'C', 4)]['steps_total'], 1)

    def test_step_on_the_closing_record_is_excluded(self):
        evs = stream(12, steps=[(7, 'P', 'stretch_frames', 512)])
        _summ, detail = b.scan_windows(evs, NS0, 2 * MS, keep_detail=True)
        by = {w['anchor_idx']: w for w in detail}
        # window [P5, P7) excludes the step landing exactly at P7 ...
        self.assertEqual(by[ridx(evs, 'P', 5)]['steps_total'], 0)
        # ... but [P6, P8) contains it.
        self.assertEqual(by[ridx(evs, 'P', 6)]['steps_total'], 1)

    def test_fill_min_uses_the_half_open_span(self):
        # P8 (fill 1.0) sits exactly on the closing boundary of [P6, P8):
        # the window must not see it, while [P7, P9) does.
        fills = {6: 5.0, 8: 1.0}
        evs = stream(12, fills=fills)
        _summ, detail = b.scan_windows(evs, NS0, 2 * MS, keep_detail=True)
        by = {w['anchor_idx']: w for w in detail}
        self.assertEqual(by[ridx(evs, 'P', 6)]['fill_min_ms'], 5.0)
        self.assertEqual(by[ridx(evs, 'P', 7)]['fill_min_ms'], 1.0)
        self.assertEqual(by[ridx(evs, 'P', 9)]['fill_min_ms'], 30.0)

    def test_pre_settle_fills_and_steps_are_not_scanned(self):
        fills = {1: 1.0, 2: 1.0, 3: 1.0}
        evs = stream(20, fills=fills,
                     steps=[(2, 'P', 'stretch_frames', 512)])
        settle = NS0 + 5 * MS
        summ, detail = b.scan_windows(evs, settle, 2 * MS, keep_detail=True)
        self.assertEqual(summ['fill_min_ms'], 30.0)
        self.assertEqual(summ['post_settle_steps_total'], 0)
        self.assertTrue(detail)
        self.assertTrue(all(w['anchor_ns'] >= settle for w in detail))

    def test_no_fitting_window_is_a_problem_not_a_silent_zero(self):
        summ, _ = b.scan_windows(stream(20), NS0, 100 * MS)
        self.assertEqual(summ['windows'], 0)
        self.assertIn('no post-settle window', summ['problem'])

    def test_too_few_windows_is_flagged(self):
        summ, _ = b.scan_windows(stream(8), NS0, 5 * MS, min_windows=10)
        self.assertLess(summ['windows'], 10)
        self.assertIn('minimum 10', summ['problem'])

    def test_step_at_the_last_record_is_reported_outside_windows(self):
        evs = stream(10, steps=[(9, 'C', 'stretch_frames', 512)])
        summ, _ = b.scan_windows(evs, NS0, 2 * MS)
        self.assertEqual(summ['post_settle_steps_total'], 1)
        self.assertEqual(summ['steps_outside_windows'], 1)

    def test_gap_overlap_is_bounded_by_the_window(self):
        evs = stream(60, skip_push_ms=range(20, 50))
        summ, _ = b.scan_windows(evs, NS0, 10 * MS)
        self.assertLessEqual(summ['gap_overlap_ms'], 10.0)
        self.assertAlmostEqual(summ['gap_overlap_ms'], 10.0, places=3)
        self.assertAlmostEqual(summ['max_gap_ms'], 31.0, places=3)

    def test_empty_threshold_decides_matching(self):
        fills = lambda ms, kind: 1.5 if 5 <= ms <= 8 else 30.0  # noqa: E731
        evs = stream(12, fills=fills)
        lo, _ = b.scan_windows(evs, NS0, 2 * MS, empty_ms=1.0)
        hi, _ = b.scan_windows(evs, NS0, 2 * MS, empty_ms=2.0)
        self.assertFalse(lo['matched'])
        self.assertTrue(hi['matched'])


class ProducerGapTest(unittest.TestCase):
    def test_consecutive_gaps_and_overlap(self):
        evs = stream(12, skip_push_ms=(4, 5, 6))
        gaps = b.producer_gaps(evs)
        self.assertEqual([g['ns'] for g in gaps],
                         [MS] * 3 + [4 * MS] + [MS] * 4)
        overlap, gap = b.gap_overlap(gaps, NS0 + 3 * MS, NS0 + 5 * MS)
        self.assertEqual(overlap, 2 * MS)
        self.assertEqual(gap['start_ns'], NS0 + 3 * MS)

    def test_gap_overlap_zero_when_disjoint(self):
        evs = stream(12)
        overlap, gap = b.gap_overlap(b.producer_gaps(evs), NS0 + 100 * MS,
                                     NS0 + 200 * MS)
        self.assertEqual((overlap, gap), (0, None))


class ControlAnalysisTest(unittest.TestCase):
    def test_malformed_timeline_is_refused(self):
        evs = stream(10)
        evs[ridx(evs, 'P', 5)]['offset'] += 1
        a = b.analyse_control_events(evs, 0.001, {'fast': 2 * MS})
        self.assertIn('control capture timeline inconsistent', a['problem'])

    def test_push_free_capture_is_refused(self):
        evs = stream(10, skip_push_ms=range(10))
        a = b.analyse_control_events(evs, 0.001, {'fast': 2 * MS})
        self.assertEqual(a['problem'], 'control capture has no producer pushes')

    def test_short_post_settle_span_is_refused(self):
        a = b.analyse_control_events(stream(10), 0.005, {'fast': 8 * MS})
        self.assertTrue(any('post-settle span' in p for p in a['problems']))

    def test_no_windows_and_too_few_windows_are_refused(self):
        a = b.analyse_control_events(stream(20), 0.001, {'fast': 100 * MS})
        self.assertTrue(any('no post-settle window' in p
                            for p in a['problems']))
        a = b.analyse_control_events(stream(8), 0.001, {'fast': 5 * MS},
                                     min_windows=10)
        self.assertTrue(any('minimum 10' in p for p in a['problems']))

    def test_stall_is_found_with_position_and_steps(self):
        evs = stream(500,
                     fills=lambda ms, kind: 1.0 if 305 <= ms <= 345 else 30.0,
                     steps=[(320, 'C', 'stretch_frames', 512)],
                     skip_push_ms=range(300, 341))
        a = b.analyse_control_events(evs, 0.001, {'fast': 30 * MS})
        w = a['windows']['fast']
        self.assertTrue(w['matched'])
        self.assertEqual(w['fill_min_ms'], 1.0)
        self.assertGreaterEqual(w['max_steps_in_window'], 1)
        at = (w['fill_min_at_ns'] - NS0) / 1e6
        self.assertGreaterEqual(at, 300.0)
        self.assertLessEqual(at, 345.0)


class ActionIntervalTest(unittest.TestCase):
    def edges(self):
        return stream(500, markers=edge_markers())

    def test_intervals_match_the_sweep_semantics(self):
        evs = self.edges()
        a = b.action_intervals(evs, settle_s=0.001)
        self.assertIsNone(a['problem'])
        settle = NS0 + int(0.001 * 1e9)
        m = s.interval_metrics(evs, q_idx(evs, 'fast-on'),
                               q_idx(evs, 'fast-off'), 40.0, settle)
        got = a['intervals']['fast']
        self.assertEqual(got['fill_min_ms'], m['fill_min_ms'])
        self.assertEqual(got['step_counts'], m['step_counts'])
        self.assertEqual(got['steps_total'], sum(m['step_counts'].values()))
        self.assertAlmostEqual(got['duration_ns'], 30 * MS, delta=2000)

    def test_marker_order_and_settle_guards_are_enforced(self):
        evs = stream(500, markers=[(260, 'P', 'fast-on'),
                                   (230, 'P', 'fast-off'),
                                   (290, 'P', 'pause'),
                                   (310, 'P', 'resume')])
        a = b.action_intervals(evs, settle_s=0.001)
        self.assertIn('out of order', a['problem'])
        evs = self.edges()
        a = b.action_intervals(evs, settle_s=0.5)   # settle reaches fast-on
        self.assertIn('settle window reaches', a['problem'])


def q_idx(events, label):
    from host_audio_queue_check import marker_indices
    hits = marker_indices(events, label)
    return hits[0]


class ComparePairTest(unittest.TestCase):
    def edges_analysis(self, markers=None):
        return b.action_intervals(
            stream(500, markers=markers or edge_markers()), settle_s=0.001)

    def test_matched_window_is_reported_with_action_side_by_side(self):
        ea = self.edges_analysis()
        durs = {k: int(v['duration_ns']) for k, v in ea['intervals'].items()}
        cev = stream(500,
                     fills=lambda ms, kind: 1.0 if 305 <= ms <= 345 else 30.0,
                     steps=[(320, 'C', 'stretch_frames', 512)],
                     skip_push_ms=range(300, 341))
        ca = b.analyse_control_events(cev, 0.001, durs)
        pair = b.compare_pair(0, ea, ca)
        self.assertIsNone(pair['problem'])
        fast = pair['intervals']['fast']
        self.assertTrue(fast['matched'])
        self.assertEqual(fast['control_duration_ms'], 30.0)
        self.assertEqual(fast['action_duration_ms'], 30.0)
        self.assertEqual(fast['control_fill_min_ms'], 1.0)
        self.assertGreaterEqual(fast['control_max_steps_in_window'], 1)
        self.assertLessEqual(fast['control_gap_overlap_ms'],
                             fast['control_duration_ms'])
        self.assertEqual(fast['control_steps_outside_windows'], 0)
        at = fast['control_fill_min_at_s']
        self.assertGreaterEqual(at, 0.299)
        self.assertLessEqual(at, 0.346)

    def test_clean_control_is_separated(self):
        ea = self.edges_analysis()
        durs = {k: int(v['duration_ns']) for k, v in ea['intervals'].items()}
        ca = b.analyse_control_events(stream(500), 0.001, durs)
        pair = b.compare_pair(1, ea, ca)
        self.assertFalse(pair['intervals']['fast']['matched'])
        self.assertFalse(pair['intervals']['pause']['matched'])
        self.assertEqual(pair['intervals']['fast']['control_fill_min_ms'],
                         30.0)

    def test_paired_durations_come_from_the_edges_run(self):
        ea = self.edges_analysis(markers=edge_markers(fast=(200, 250),
                                                      pause=(300, 400)))
        durs = {k: int(v['duration_ns']) for k, v in ea['intervals'].items()}
        ca = b.analyse_control_events(stream(600), 0.001, durs)
        pair = b.compare_pair(0, ea, ca)
        self.assertEqual(pair['intervals']['fast']['control_duration_ms'],
                         50.0)
        self.assertEqual(pair['intervals']['pause']['control_duration_ms'],
                         100.0)


class BatchEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def clean_batch(self, name, controls2=()):
        edges = [run_record(self.tmp, 'edges', r,
                            stream(500, markers=edge_markers()))
                 for r in range(2)]
        controls = [run_record(self.tmp, 'control', r, stream(500))
                    for r in range(2)]
        return sweep_json(self.tmp, name, edges, controls, controls2)

    def analyse(self, path):
        return b.analyse_batch(path, settle_s=0.001, expect_rate=65536)

    def test_clean_batch_is_ok_and_counts_runs_not_windows(self):
        path = self.clean_batch('clean.json')
        batch = self.analyse(path)
        self.assertEqual(batch['problems'], [])
        self.assertEqual(len(batch['pairs']), 2)
        self.assertEqual(batch['runs_scanned'], 2)
        for pair in batch['pairs']:
            for m in pair['intervals'].values():
                self.assertFalse(m['matched'])
                self.assertGreater(m['control_windows'], 10)
        verdict, problems, _notes, rc = b.decide([batch])
        self.assertEqual((verdict, problems, rc), ('MATCHED-WINDOW OK', [], 0))

    def test_stalled_control_makes_the_batch_not_separated(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()))]
        stalled = stream(500,
                         fills=lambda ms, kind: 1.0 if 305 <= ms <= 345
                         else 30.0,
                         steps=[(320, 'C', 'stretch_frames', 512)],
                         skip_push_ms=range(300, 341))
        controls = [run_record(self.tmp, 'control', 0, stalled)]
        path = sweep_json(self.tmp, 'stalled.json', edges, controls)
        batch = self.analyse(path)
        self.assertEqual(batch['problems'], [])
        verdict, problems, _notes, rc = b.decide([batch])
        self.assertEqual((verdict, rc), ('MATCHED-WINDOW NOT SEPARATED', 1))
        self.assertTrue(any('control window drained' in p for p in problems))

    def test_control2_runs_are_part_of_the_no_action_baseline(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()))]
        clean = [run_record(self.tmp, 'control', 0, stream(500))]
        stalled = stream(500,
                         fills=lambda ms, kind: 1.0 if 305 <= ms <= 345
                         else 30.0,
                         skip_push_ms=range(300, 341))
        control2 = [run_record(self.tmp, 'control2', 0, stalled)]
        path = sweep_json(self.tmp, 'c2.json', edges, clean, control2)
        batch = self.analyse(path)
        self.assertEqual(batch['problems'], [])
        self.assertEqual(len(batch['pairs']), 2)
        verdict, _problems, _notes, _rc = b.decide([batch])
        self.assertEqual(verdict, 'MATCHED-WINDOW NOT SEPARATED')

    def test_wrong_rate_and_inconsistent_rates_are_refused(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()))]
        bad = [run_record(self.tmp, 'control', 0, stream(500), rate=44100)]
        path = sweep_json(self.tmp, 'rate.json', edges, bad)
        batch = self.analyse(path)
        self.assertTrue(any('!= expected 65536' in p for p in batch['problems']))
        verdict, _problems, _notes, rc = b.decide([batch])
        self.assertEqual((verdict, rc), ('MATCHED-WINDOW REFUSED', 1))

        bad2 = [run_record(self.tmp, 'control', 0, stream(500), rate=None)]
        path = sweep_json(self.tmp, 'noattr.json', edges, bad2)
        batch = self.analyse(path)
        self.assertTrue(any('source_rate' in p for p in batch['problems']))

    def test_failed_missing_or_truncated_runs_are_refused(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()))]
        for kw in ({'exit': 1}, {'timed_out': True}, {'truncated': True},
                   {'capture': None},
                   {'events_path': str(self.tmp / 'does-not-exist.csv')}):
            run = run_record(self.tmp, 'control', 0, stream(500), **kw)
            path = sweep_json(self.tmp, 'bad.json', edges, [run])
            batch = self.analyse(path)
            self.assertTrue(batch['problems'], f'{kw} not refused')
            verdict, _problems, _notes, rc = b.decide([batch])
            self.assertEqual((verdict, rc), ('MATCHED-WINDOW REFUSED', 1))

    def test_short_control_capture_is_refused_not_greened(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()))]
        controls = [run_record(self.tmp, 'control', 0, stream(60))]
        path = sweep_json(self.tmp, 'short.json', edges, controls)
        batch = self.analyse(path)
        self.assertTrue(batch['problems'])
        verdict, _problems, _notes, rc = b.decide([batch])
        self.assertEqual((verdict, rc), ('MATCHED-WINDOW REFUSED', 1))

    def test_unpaired_repeat_is_refused(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()))]
        controls = [run_record(self.tmp, 'control', 1, stream(500))]
        path = sweep_json(self.tmp, 'unpaired.json', edges, controls)
        batch = self.analyse(path)
        self.assertTrue(any('no usable paired edges run' in p
                            for p in batch['problems']))

    def test_stored_settle_mismatch_is_a_note_not_a_refusal(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()))]
        controls = [run_record(self.tmp, 'control', 0, stream(500),
                               baseline={'settle_ns': NS0 + 12345})]
        path = sweep_json(self.tmp, 'note.json', edges, controls)
        batch = self.analyse(path)
        self.assertEqual(batch['problems'], [])
        self.assertTrue(any('settle_ns' in n for n in batch['notes']))

    def test_recomputed_interval_mismatch_with_stored_analysis_is_noted(self):
        edges = [run_record(self.tmp, 'edges', 0,
                            stream(500, markers=edge_markers()),
                            analysis={'fast_interval':
                                      {'fill_min_ms': 999.0,
                                       'step_counts': {}}})]
        controls = [run_record(self.tmp, 'control', 0, stream(500))]
        path = sweep_json(self.tmp, 'recompute.json', edges, controls)
        batch = self.analyse(path)
        self.assertTrue(any('recomputed fill_min' in n for n in batch['notes']))


class DecideTest(unittest.TestCase):
    def test_rule_version(self):
        self.assertEqual(b.TOOL_RULE_VERSION,
                         '2026-09-26-matched-window-v1')

    def test_no_batches_is_insufficient(self):
        verdict, _problems, _notes, rc = b.decide([])
        self.assertEqual((verdict, rc), ('MATCHED-WINDOW INSUFFICIENT', 1))

    def test_any_batch_problem_refuses_and_never_greens(self):
        good = {'path': 'good.json', 'problems': [], 'notes': [],
                'pairs': [{'repeat': 0, 'source': 'control',
                           'intervals': {'fast': {'matched': False}}}]}
        bad = {'path': 'bad.json', 'problems': ['r0: capture missing'],
               'notes': [], 'pairs': []}
        verdict, problems, _notes, rc = b.decide([good, bad])
        self.assertEqual((verdict, rc), ('MATCHED-WINDOW REFUSED', 1))
        self.assertTrue(any('bad.json' in p for p in problems))

    def test_a_matched_control_window_refuses_separation(self):
        b1 = {'path': 'b.json', 'problems': [], 'notes': [],
              'pairs': [{'repeat': 0, 'source': 'control2',
                         'intervals': {'fast': {'matched': True,
                                                'control_fill_min_ms': 1.0,
                                                'control_fill_min_at_s': 3.28,
                                                'action_fill_min_ms': 0.24}}}]}
        verdict, problems, _notes, rc = b.decide([b1])
        self.assertEqual((verdict, rc), ('MATCHED-WINDOW NOT SEPARATED', 1))
        self.assertTrue(any('control window drained to 1.0 ms' in p
                            for p in problems))


if __name__ == '__main__':
    unittest.main(verbosity=2)
