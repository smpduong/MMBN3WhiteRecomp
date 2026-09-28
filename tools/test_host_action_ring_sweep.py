#!/usr/bin/env python3
"""Unit tests for host_action_ring_sweep (no game run required).

Covers the rules that decide whether an action-edge sweep supports a claim:

  * interval_metrics counts only counter steps strictly INSIDE the marker
    pair, on the MERGED P/C/M stream (a step on a pull record must be seen),
    and excludes anything before the settle window from attribution;
  * recovery is the first record after the closing edge back at the target
    fill -- boundary records never count -- and None when the capture ends
    first;
  * analyse_edges refuses a capture with a missing / duplicate / out-of-order
    edge marker or an inconsistent timeline instead of guessing alignment,
    and reports pre-settle steps as `startup_steps` context;
  * control_baseline separates post-settle steps (the baseline attribution is
    read against) from startup steps, and reports a push-free control as
    unusable;
  * decide refuses attribution when ANY control moved a counter: the sweep
    still reports the edge steps but never calls them action effects;
  * aggregate reports a distribution (min/median/max/spread) and calls a
    metric consistent only when every repeat rounds to the same value.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import host_action_ring_sweep as s  # noqa: E402

MS = 1_000_000
NS0 = 1_000_000_000


def counts(**kw):
    """Full step-count dict: every counter is always reported (zeros too)."""
    d = {c: 0 for c in s.COUNTERS}
    d.update(kw)
    return d


def ev(kind, ns, offset=0, frames=0, fill_ms=0.0, label='',
       stretch_frames=0, underrun_frames=0, overflow_frames=0):
    return {'kind': kind, 'ns': ns, 'offset': offset, 'frames': frames,
            'fill_ms': fill_ms, 'stretch_frames': stretch_frames,
            'underrun_frames': underrun_frames,
            'overflow_frames': overflow_frames, 'label': label}


def capture(n_ms, fills=None, labels=None, steps=(), push=64, pull=64):
    """Structurally valid P/C stream on a 1 ms grid (C at +0.5 ms).

    fills  : per-record fills in stream order (P,C,P,C,...).
    labels : {record_index: label}; the marker is inserted 1 us before that
             record with the record's fill, so record_index counts records
             only (not markers).
    steps  : (ms, kind, counter, delta); the delta applies to that record and
             everything after it, so counter_steps() sees one step there.
    """
    if fills is None:
        fills = [50.0] * (2 * n_ms)
    deltas = {}
    for (ms, kind, counter, delta) in steps:
        d = deltas.setdefault((ms, kind), {})
        d[counter] = d.get(counter, 0) + delta
    events, src, out, n_rec = [], 0, 0, 0
    counters = {c: 0 for c in s.COUNTERS}
    for ms in range(n_ms):
        for kind in ('P', 'C'):
            t = NS0 + ms * MS + (500_000 if kind == 'C' else 0)
            fill = fills[n_rec]
            if labels and n_rec in labels:
                # The marker precedes this P/C record, so it sees the counters
                # before any delta applied by that future record.
                events.append(ev('M', t - 1_000, fill_ms=fill,
                                 label=labels[n_rec], **counters))
            for c, d in deltas.get((ms, kind), {}).items():
                counters[c] += d
            if kind == 'P':
                events.append(ev('P', t, offset=src, frames=push,
                                 fill_ms=fill, **counters))
                src += push
            else:
                events.append(ev('C', t, offset=out, frames=pull,
                                 fill_ms=fill, **counters))
                out += pull
            n_rec += 1
    return events


def ridx(events, kind, ms):
    """Index of the P/C record on the 1 ms grid (markers shift positions)."""
    t = NS0 + ms * MS + (500_000 if kind == 'C' else 0)
    for i, r in enumerate(events):
        if r['kind'] == kind and r['ns'] == t:
            return i
    raise AssertionError(f'no {kind} record at ms {ms}')


def run_row(tag='edges', repeat=0, exit_code=0, analysis=None, capture=True,
            timed_out=False, truncated=False, baseline=None):
    return {'tag': tag, 'repeat': repeat, 'exit': exit_code,
            'timed_out': timed_out, 'truncated': truncated,
            'capture': {'source_frames': 1} if capture else None,
            'analysis': analysis if analysis is not None else {},
            'baseline': baseline}


def make_report(edges, controls, aggregates=None, save_ok=True):
    if aggregates is None:
        aggregates = {name: {'n': len(edges), 'consistent': True}
                      for name in s.REQUIRED_AGGREGATES}
    return {'edges_runs': edges, 'control_runs': controls,
            'aggregates': aggregates,
            'source_save_unchanged': save_ok}


class IntervalMetricsTest(unittest.TestCase):
    def test_only_records_between_the_boundaries_are_summarised(self):
        # boundaries are P ms0 = 900 and C ms3 = 900 ms: they must be excluded
        evs = capture(4, fills=[900.0, 50.0, 44.0, 20.0, 3.0, 0.5, 9.0, 900.0])
        i0, i1 = ridx(evs, 'P', 0), ridx(evs, 'C', 3)
        m = s.interval_metrics(evs, i0, i1, 40.0, settle_ns=0)
        self.assertEqual(m['records'], 6)
        self.assertEqual(m['fill_max_ms'], 50.0)
        self.assertEqual(m['fill_min_ms'], 0.5)
        self.assertAlmostEqual(m['fill_mean_ms'],
                               (50 + 44 + 20 + 3 + 0.5 + 9) / 6, 3)
        self.assertTrue(m['fill_empty'])
        self.assertAlmostEqual(m['duration_s'], 0.0035, 6)

    def test_step_on_a_pull_record_inside_the_pair_is_counted(self):
        # The merged-stream rule: the counters move on the pull side, so a
        # push-only scan would miss this step (the old defect class).
        evs = capture(4, steps=[(2, 'C', 'stretch_frames', 512)])
        i0, i1 = ridx(evs, 'P', 0), ridx(evs, 'C', 3)
        m = s.interval_metrics(evs, i0, i1, 40.0, settle_ns=0)
        self.assertEqual(m['step_counts'], counts(stretch_frames=1))
        self.assertEqual(m['steps']['stretch_frames'],
                         [[ridx(evs, 'C', 2), 0, 512]])

    def test_steps_on_the_boundary_or_outside_are_not_attributed(self):
        evs = capture(6, steps=[(0, 'P', 'stretch_frames', 512),   # boundary
                                (6, 'P', 'underrun_frames', 512),  # after
                                (2, 'P', 'overflow_frames', 512)])  # inside
        i0, i1 = ridx(evs, 'P', 0), ridx(evs, 'C', 3)
        m = s.interval_metrics(evs, i0, i1, 40.0, settle_ns=0)
        self.assertEqual(m['step_counts'], counts(overflow_frames=1))

    def test_pre_settle_steps_are_context_not_interval_steps(self):
        evs = capture(6, steps=[(1, 'P', 'stretch_frames', 512)])
        i0, i1 = ridx(evs, 'P', 0), ridx(evs, 'C', 3)
        m = s.interval_metrics(evs, i0, i1, 40.0, NS0 + 2 * MS)
        self.assertEqual(m['step_counts'], counts())
        # the step is not lost: it is still on the stream, one step of 512
        self.assertEqual(s.q.counter_steps(evs, 'stretch_frames'), [(2, 0, 512)])
        # at/after the settle boundary the same step is attributable
        m2 = s.interval_metrics(evs, i0, i1, 40.0, NS0 + MS)
        self.assertEqual(m2['step_counts'], counts(stretch_frames=1))

    def test_recovery_is_first_record_at_target_after_the_edge(self):
        # closing edge at C ms2; below target until P ms4 (41 ms)
        evs = capture(6, fills=[50.0, 50.0, 50.0, 50.0, 10.0, 10.0,
                                20.0, 20.0, 41.0, 41.0, 45.0, 45.0])
        i0, i1 = ridx(evs, 'P', 0), ridx(evs, 'C', 2)
        m = s.interval_metrics(evs, i0, i1, 40.0, settle_ns=0)
        self.assertAlmostEqual(m['recovery_s'], 0.0015, 6)  # 2.5 -> 4.0 ms

    def test_recovery_is_none_when_the_capture_ends_first(self):
        evs = capture(4, fills=[50.0, 50.0, 10.0, 10.0, 20.0, 20.0,
                                30.0, 30.0])
        i0, i1 = ridx(evs, 'P', 0), ridx(evs, 'C', 1)
        m = s.interval_metrics(evs, i0, i1, 40.0, settle_ns=0)
        self.assertIsNone(m['recovery_s'])


class AnalyseEdgesTest(unittest.TestCase):
    FILLS = [50.0, 49.0, 46.0, 44.0, 30.0, 1.0, 0.5, 0.5,
             30.0, 25.0, 20.0, 15.0, 0.3, 0.3, 50.0, 50.0]
    LABELS = {0: 'fast-on', 6: 'fast-off', 8: 'pause', 12: 'resume'}

    def edges(self, **kw):
        fills = kw.pop('fills', self.FILLS)
        labels = kw.pop('labels', self.LABELS)
        return capture(8, fills=fills, labels=labels, **kw)

    def test_happy_path(self):
        evs = self.edges(steps=[(2, 'P', 'underrun_frames', 512)])
        a, out = s.analyse_edges({'events': evs, 'source_rate': 65536},
                                 40.0, 0)
        self.assertIsNone(a['problem'])
        self.assertIs(out, evs)
        self.assertEqual(a['edges']['fast-on']['fill_ms'], 50.0)
        self.assertEqual(a['edges']['fast-off']['fill_ms'], 0.5)
        self.assertEqual(a['edges']['pause']['fill_ms'], 30.0)
        self.assertEqual(a['edges']['resume']['fill_ms'], 0.3)
        fi = a['fast_interval']
        self.assertEqual(fi['records'], 6)
        self.assertEqual(fi['fill_min_ms'], 1.0)
        self.assertEqual(fi['fill_max_ms'], 50.0)
        self.assertTrue(fi['fill_empty'])
        self.assertEqual(fi['step_counts'], counts(underrun_frames=1))
        self.assertAlmostEqual(fi['duration_s'], 0.003, 6)
        pi = a['pause_interval']
        self.assertEqual(pi['fill_min_ms'], 15.0)
        self.assertEqual(pi['step_counts'], counts())
        self.assertEqual(a['startup_steps_total'], 0)
        self.assertAlmostEqual(a['post_fast_recovery'], 0.004, 6)
        self.assertAlmostEqual(a['post_pause_recovery'], 0.001, 6)

    def test_missing_marker_is_refused(self):
        labels = dict(self.LABELS)
        del labels[12]
        a, out = s.analyse_edges({'events': self.edges(labels=labels),
                                  'source_rate': 65536}, 40.0, 0)
        self.assertIsNone(out)
        self.assertIn("expected exactly one 'resume' marker, found 0",
                      a['problem'])

    def test_duplicate_marker_is_refused(self):
        labels = dict(self.LABELS)
        labels[12] = 'resume'
        labels[13] = 'resume'
        a, _ = s.analyse_edges({'events': self.edges(labels=labels),
                                'source_rate': 65536}, 40.0, 0)
        self.assertIsNone(_)
        self.assertIn("expected exactly one 'resume' marker, found 2",
                      a['problem'])

    def test_out_of_order_markers_are_refused(self):
        labels = {0: 'fast-on', 6: 'fast-off', 12: 'pause', 8: 'resume'}
        a, _ = s.analyse_edges({'events': self.edges(labels=labels),
                                'source_rate': 65536}, 40.0, 0)
        self.assertIsNone(_)
        self.assertIn('edge markers out of order', a['problem'])

    def test_settle_window_must_end_before_first_action(self):
        evs = self.edges(steps=[(2, 'C', 'stretch_frames', 512)])
        first_action = evs[s.q.marker_indices(evs, 'fast-on')[0]]['ns']
        a, out = s.analyse_edges({'events': evs, 'source_rate': 65536},
                                 40.0, first_action)
        self.assertIsNone(out)
        self.assertIn('settle window reaches', a['problem'])

    def test_inconsistent_timeline_is_refused(self):
        evs = self.edges()
        for r in evs:                      # break producer offset contiguity
            if r['kind'] == 'P' and r['offset'] == 64:
                r['offset'] = 65
        a, _ = s.analyse_edges({'events': evs, 'source_rate': 65536}, 40.0, 0)
        self.assertIsNone(_)
        self.assertIn('capture timeline inconsistent', a['problem'])


class ControlBaselineTest(unittest.TestCase):
    def test_clean_control_reports_band_and_zero_steps(self):
        evs = capture(5, fills=[16.0, 16.0, 40.0, 50.0, 45.0, 44.0,
                                30.0, 39.0, 41.0, 43.0])
        b = s.control_baseline({'events': evs}, NS0 + MS)
        self.assertIsNone(b['problem'])
        self.assertEqual(b['fill_min_ms'], 16.0)
        self.assertEqual(b['fill_max_ms'], 50.0)
        self.assertEqual(b['steps_total'], 0)
        self.assertEqual(b['startup_steps_total'], 0)

    def test_pre_settle_steps_are_context_not_baseline(self):
        evs = capture(5, steps=[(0, 'P', 'stretch_frames', 512)])
        b = s.control_baseline({'events': evs}, NS0 + MS)
        self.assertEqual(b['step_counts'], counts())
        self.assertEqual(b['startup_steps']['stretch_frames'], 1)
        self.assertEqual(b['steps_total'], 0)

    def test_post_settle_step_makes_the_control_noisy(self):
        evs = capture(5, steps=[(0, 'P', 'stretch_frames', 512),
                                (3, 'C', 'stretch_frames', 512)])
        b = s.control_baseline({'events': evs}, NS0 + 2 * MS)
        self.assertEqual(b['step_counts'], counts(stretch_frames=1))
        self.assertEqual(b['steps_total'], 1)

    def test_push_free_control_is_unusable(self):
        b = s.control_baseline({'events': [ev('C', NS0, frames=64,
                                              offset=0)]}, 0)
        self.assertEqual(b['problem'], 'control capture has no producer pushes')

    def test_malformed_control_timeline_is_unusable(self):
        evs = capture(5)
        evs[ridx(evs, 'P', 2)]['offset'] += 1
        b = s.control_baseline({'events': evs}, NS0 + MS)
        self.assertIn('control capture timeline inconsistent', b['problem'])

    def test_marker_before_step_sees_previous_counter(self):
        evs = capture(3, labels={2: 'fast-on'},
                      steps=[(1, 'P', 'stretch_frames', 512)])
        marker = evs[s.q.marker_indices(evs, 'fast-on')[0]]
        self.assertEqual(marker['stretch_frames'], 0)
        self.assertEqual(evs[ridx(evs, 'P', 1)]['stretch_frames'], 512)


class DecideTest(unittest.TestCase):
    def test_rule_version_distinguishes_retained_reports(self):
        self.assertEqual(s.TOOL_RULE_VERSION,
                         '2026-09-26-host-action-sweep-v2')

    def edges_analysis(self, steps=0):
        evs = AnalyseEdgesTest().edges()
        a, _ = s.analyse_edges({'events': evs, 'source_rate': 65536}, 40.0, 0)
        a['fast_interval']['step_counts'] = ({'stretch_frames': steps}
                                             if steps else {})
        return a

    def clean_edges(self, n=2):
        return [run_row('edges', i, analysis=self.edges_analysis())
                for i in range(n)]

    def clean_controls(self, n=2, steps=0):
        return [run_row('control', i, analysis={},
                        baseline={'problem': None, 'steps_total': steps,
                                  'step_counts': {'stretch_frames': steps},
                                  'startup_steps_total': 0})
                for i in range(n)]

    def test_clean_control_attributes(self):
        r = make_report(self.clean_edges(), self.clean_controls())
        v, bad, notes, rc = s.decide(r)
        self.assertEqual((v, bad, rc), ('SWEEP OK', [], 0))
        self.assertEqual(r['attribution'],
                         'control-clean association, not causal proof')
        self.assertEqual(r['edge_interval_step_total'], 0)
        self.assertIn('control counter steps over 2 runs: 0', notes)

    def test_noisy_control_refuses_attribution(self):
        r = make_report(self.clean_edges(), self.clean_controls(steps=1))
        v, bad, _notes, rc = s.decide(r)
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertEqual(r['attribution'], 'refused (control or run invalid)')
        self.assertTrue(any('control-not-clean' in b for b in bad))

    def test_edge_interval_steps_are_summed_across_runs(self):
        r = make_report(self.clean_edges(2), self.clean_controls())
        r['edges_runs'][0]['analysis'] = self.edges_analysis(steps=3)
        r['edges_runs'][1]['analysis'] = self.edges_analysis(steps=4)
        s.decide(r)
        self.assertEqual(r['edge_interval_step_total'], 7)

    def test_incomplete_edges_run_fails(self):
        edges = self.clean_edges()
        edges[0]['exit'] = 1
        r = make_report(edges, self.clean_controls())
        v, bad, _notes, rc = s.decide(r)
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertTrue(any('edges/r0' in b for b in bad))

    def test_truncated_edges_run_fails(self):
        edges = self.clean_edges()
        edges[0]['truncated'] = True
        v, bad, _notes, rc = s.decide(make_report(edges, self.clean_controls()))
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertTrue(any('did not complete cleanly' in b for b in bad))

    def test_edges_analysis_problem_fails(self):
        edges = self.clean_edges()
        edges[0]['analysis'] = {'problem': "expected exactly one 'resume'"}
        v, bad, _notes, rc = s.decide(make_report(edges, self.clean_controls()))
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertIn("edges/r0: expected exactly one 'resume'", bad)

    def test_missing_run_branch_fails(self):
        v, bad, _notes, rc = s.decide(make_report([], self.clean_controls()))
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertIn('missing-edges-or-control-runs', bad)

    def test_unusable_control_fails(self):
        controls = [run_row('control', 0, analysis={},
                            baseline={'problem': 'no pushes'})]
        v, bad, _notes, rc = s.decide(make_report(self.clean_edges(), controls))
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertIn('no-usable-control', bad)

    def test_control2_steps_refuse_attribution_too(self):
        controls2 = [run_row('control2', 0, analysis={},
                             baseline={'problem': None, 'steps_total': 2,
                                       'step_counts': {'stretch_frames': 2},
                                       'startup_steps_total': 0})]
        r = make_report(self.clean_edges(), self.clean_controls())
        r['control2_runs'] = controls2
        v, bad, _notes, rc = s.decide(r)
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertTrue(any('control-not-clean' in b for b in bad))

    def test_control2_presence_alone_does_not_break_the_pair_check(self):
        controls2 = [run_row('control2', i, analysis={},
                             baseline={'problem': None, 'steps_total': 0,
                                       'step_counts': {},
                                       'startup_steps_total': 0})
                     for i in range(2)]
        r = make_report(self.clean_edges(), self.clean_controls())
        r['control2_runs'] = controls2
        v, bad, _notes, rc = s.decide(r)
        self.assertEqual((v, bad, rc), ('SWEEP OK', [], 0))

    def test_single_repeat_cannot_report_ok(self):
        r = make_report(self.clean_edges(1), self.clean_controls(1))
        v, bad, _notes, rc = s.decide(r)
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertTrue(any('two paired' in b for b in bad))

    def test_missing_aggregate_sample_fails(self):
        aggs = {name: {'n': 2, 'consistent': True}
                for name in s.REQUIRED_AGGREGATES}
        aggs['fill_at_fast_on_ms']['n'] = 1
        r = make_report(self.clean_edges(), self.clean_controls(), aggs)
        v, bad, _notes, rc = s.decide(r)
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertTrue(any('missing metric' in b for b in bad))

    def test_changed_source_save_fails(self):
        r = make_report(self.clean_edges(), self.clean_controls(), save_ok=False)
        v, bad, _notes, rc = s.decide(r)
        self.assertEqual((v, rc), ('SWEEP FAIL', 1))
        self.assertIn('source_save_unchanged', bad)

    def test_variable_aggregate_is_noted_but_not_fatal(self):
        aggs = {name: {'n': 2, 'consistent': True}
                for name in s.REQUIRED_AGGREGATES}
        aggs['fill_at_fast_on_ms'] = {
            'n': 2, 'min': 42.0, 'median': 44.0,
            'max': 49.0, 'spread': 7.0,
            'rel_spread': 0.159, 'consistent': False}
        r = make_report(self.clean_edges(), self.clean_controls(),
                        aggregates=aggs)
        v, bad, notes, rc = s.decide(r)
        self.assertEqual((v, bad, rc), ('SWEEP OK', [], 0))
        self.assertTrue(any('fill_at_fast_on_ms: variable' in n
                            for n in notes))


class AggregateTest(unittest.TestCase):
    def rows(self, *vals):
        return [{'analysis': {'k': v}} for v in vals]

    def test_all_none_yields_n_zero(self):
        self.assertEqual(s.aggregate(self.rows(None, None), 'k'), {'n': 0})

    def test_distribution_includes_spread(self):
        a = s.aggregate(self.rows(1.0, 2.0, 3.0), 'k')
        self.assertEqual((a['n'], a['min'], a['median'], a['max']), (3, 1.0, 2.0,
                                                                    3.0))
        self.assertEqual(a['spread'], 2.0)
        self.assertEqual(a['rel_spread'], 1.0)
        self.assertFalse(a['consistent'])

    def test_none_values_do_not_count_as_repeats(self):
        a = s.aggregate(self.rows(4.0, None, 6.0), 'k')
        self.assertEqual(a['n'], 2)

    def test_consistency_is_strict_rounding_so_read_the_spread(self):
        fast = s.aggregate(self.rows(0.0335, 0.0336, 0.0337, 0.0339, 0.0341),
                           'k')
        self.assertTrue(fast['consistent'])
        resume = s.aggregate(self.rows(0.0364, 0.0365, 0.0367), 'k')
        self.assertFalse(resume['consistent'])
        self.assertLess(resume['spread'], 0.001)

    def test_zero_median_has_no_relative_spread(self):
        a = s.aggregate(self.rows(0.0, 0.0), 'k')
        self.assertIsNone(a['rel_spread'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
