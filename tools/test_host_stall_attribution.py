#!/usr/bin/env python3
"""Unit tests for host_stall_attribution (no game run required).

Covers the rules stall attribution stands on:

  * a stall is attributed to the adjacent marker pair with the largest
    overlap -- not the longest interval and not the enclosing bracket;
  * the enclosing chain (`within`) comes from a validated enter/exit stack;
  * fast-forward and pause gaps are marked expected (pushes are muted by
    design) and pre-settle gaps are `startup`; only unexplained gaps count
    toward attribution;
  * unattributed gaps are reported with the largest marker interval instead
    of being dropped, and an under-threshold interval yields UNATTRIBUTED;
  * legacy captures without phase markers, malformed timelines and
    unbalanced/unknown marker streams are refused rather than guessed;
  * verdicts: OK / PARTIAL / REFUSED / INSUFFICIENT.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import host_stall_attribution as a  # noqa: E402

MS = 1_000_000
NS0 = 1_000_000_000


def rec(kind, ns, label='', frames=0, offset=0, fill=30.0):
    return {'kind': kind, 'ns': ns, 'offset': offset, 'frames': frames,
            'fill_ms': fill, 'stretch_frames': 0, 'underrun_frames': 0,
            'overflow_frames': 0, 'label': label}


def build(spec):
    """spec: list of (ms, kind, label). Sorted; P/C offsets made contiguous."""
    out = []
    for (t, kind, label) in sorted(spec):
        frames = 64 if kind == 'P' else (512 if kind == 'C' else 0)
        out.append(rec(kind, NS0 + int(t * MS), label=label, frames=frames))
    src = dst = 0
    for r in out:
        if r['kind'] == 'P':
            r['offset'] = src
            src += r['frames']
        elif r['kind'] == 'C':
            r['offset'] = dst
            dst += r['frames']
    return out


def frame_cycle(t0_ms, present_ms=0.0, hook=True):
    """A normal frame cycle of markers starting at t0_ms.

    Offsets are strictly increasing (no ties: ties would sort by label and
    fabricate an unbalanced stream); present_ms > 0 injects the stall
    inside the present bracket. Returns the spec (without P/C records).
    """
    pd = max(present_ms, 0.01)
    spec = []
    if hook:
        spec.append((t0_ms, 'M', 'fh-enter'))
    spec += [
        (t0_ms + 0.10, 'M', 'render-enter'),
        (t0_ms + 0.20, 'M', 'render-exit'),
        (t0_ms + 0.30, 'M', 'present-enter'),
        (t0_ms + 0.30 + pd, 'M', 'present-exit'),
        (t0_ms + 0.40 + pd, 'M', 'audiopush-enter'),
        (t0_ms + 0.50 + pd, 'M', 'audiopush-exit'),
        (t0_ms + 0.60 + pd, 'M', 'pumpfn-enter'),
        (t0_ms + 0.70 + pd, 'M', 'rewindcall-enter'),
        (t0_ms + 0.71 + pd, 'M', 'rewindcall-exit'),
        (t0_ms + 0.72 + pd, 'M', 'hostpump-enter'),
        (t0_ms + 0.80 + pd, 'M', 'hostpump-exit'),
        (t0_ms + 0.81 + pd, 'M', 'pumpfn-exit'),
        (t0_ms + 0.90 + pd, 'M', 'pace-enter'),
        (t0_ms + 1.00 + pd, 'M', 'pace-exit'),
    ]
    if hook:
        spec.append((t0_ms + 1.10 + pd, 'M', 'fh-exit'))
    return spec


class AttributionTest(unittest.TestCase):
    def analyse(self, spec, **kw):
        kw.setdefault('settle_s', 0.0)
        return a.analyse_run(build(spec), **kw)

    def test_stall_inside_present_is_attributed_to_sdl_present(self):
        spec = [(0, 'P', '')] + frame_cycle(0.5, present_ms=100.0) + \
            [(103.0, 'P', '')]
        r = self.analyse(spec)
        self.assertIsNone(r['problem'])
        self.assertEqual(len(r['unexplained']), 1)
        g = r['unexplained'][0]
        self.assertEqual(g['context'], 'no-action')
        self.assertIn('SDL present', g['attribution'])
        self.assertEqual(g['marker_gap']['from'], 'present-enter')
        self.assertEqual(g['marker_gap']['to'], 'present-exit')
        self.assertEqual(g['marker_gap']['within'], ['fh-enter'])

    def test_stall_inside_rewind_serialization_with_enclosing_chain(self):
        spec = [(0, 'P', ''),
                (0.1, 'M', 'pumpfn-enter'),
                (0.2, 'M', 'rewindcall-enter'),
                (0.3, 'M', 'rewind-fire'),
                (5.0, 'M', 'premem-save'),
                (105.0, 'M', 'postmem-save'),
                (105.5, 'M', 'rewind-store'),
                (106.0, 'M', 'rewindcall-exit'),
                (106.2, 'M', 'hostpump-enter'),
                (106.4, 'M', 'hostpump-exit'),
                (106.5, 'M', 'pumpfn-exit'),
                (107.0, 'P', '')]
        r = self.analyse(spec)
        g = r['unexplained'][0]
        self.assertIn('snapshot serialization', g['attribution'])
        self.assertEqual(g['marker_gap']['from'], 'premem-save')
        self.assertEqual(g['marker_gap']['to'], 'postmem-save')
        self.assertEqual(g['marker_gap']['within'],
                         ['pumpfn-enter', 'rewindcall-enter'])

    def test_stall_inside_sdl_window_pump(self):
        spec = [(0, 'P', ''),
                (0.1, 'M', 'pumpfn-enter'),
                (0.2, 'M', 'rewindcall-enter'),
                (0.21, 'M', 'rewindcall-exit'),
                (0.3, 'M', 'hostpump-enter'),
                (100.3, 'M', 'hostpump-exit'),
                (100.5, 'M', 'pumpfn-exit'),
                (101.0, 'P', '')]
        r = self.analyse(spec)
        g = r['unexplained'][0]
        self.assertIn('SDL window/event pump', g['attribution'])
        self.assertEqual(g['marker_gap']['within'], ['pumpfn-enter'])

    def test_stall_inside_sdl_poll_inside_window_pump(self):
        spec = [(0, 'P', ''),
                (0.1, 'M', 'pumpfn-enter'),
                (0.2, 'M', 'rewindcall-enter'),
                (0.21, 'M', 'rewindcall-exit'),
                (0.3, 'M', 'hostpump-enter'),
                (0.35, 'M', 'pumppoll-enter'),
                (0.36, 'M', 'pumppoll-exit'),
                (0.37, 'M', 'pumphandle-enter'),
                (0.38, 'M', 'pumphandle-exit'),
                (0.4, 'M', 'pumppoll-enter'),
                (90.4, 'M', 'pumppoll-exit'),
                (90.5, 'M', 'hostpump-exit'),
                (90.7, 'M', 'pumpfn-exit'),
                (91.0, 'P', '')]
        r = self.analyse(spec)
        g = r['unexplained'][0]
        self.assertIn('SDL event poll inside HostWindow::pump',
                      g['attribution'])
        self.assertEqual(g['marker_gap']['from'], 'pumppoll-enter')
        self.assertEqual(g['marker_gap']['to'], 'pumppoll-exit')
        self.assertEqual(g['marker_gap']['within'],
                         ['pumpfn-enter', 'hostpump-enter'])

    def test_stall_inside_pump_event_handling_not_the_poll(self):
        spec = [(0, 'P', ''),
                (0.3, 'M', 'hostpump-enter'),
                (0.35, 'M', 'pumppoll-enter'),
                (0.36, 'M', 'pumppoll-exit'),
                (0.37, 'M', 'pumphandle-enter'),
                (90.37, 'M', 'pumphandle-exit'),
                (90.4, 'M', 'pumppoll-enter'),
                (90.41, 'M', 'pumppoll-exit'),
                (90.5, 'M', 'hostpump-exit'),
                (91.0, 'P', '')]
        r = self.analyse(spec)
        g = r['unexplained'][0]
        self.assertIn('host event handling inside HostWindow::pump',
                      g['attribution'])
        self.assertEqual(g['marker_gap']['from'], 'pumphandle-enter')
        self.assertEqual(g['marker_gap']['to'], 'pumphandle-exit')
        self.assertEqual(g['marker_gap']['within'], ['hostpump-enter'])

    def test_gaps_only_scans_legacy_capture_without_refusing(self):
        spec = [(0, 'P', '')] + frame_cycle(0.5, present_ms=100.0) + \
            [(103.0, 'P', '')]
        r = a.analyse_run(build(spec), settle_s=0.0, gaps_only=True)
        self.assertIsNone(r['problem'])
        self.assertEqual(len(r['unexplained']), 1)
        g = r['unexplained'][0]
        self.assertEqual(g['context'], 'no-action')
        self.assertIn('gaps-only scan', g['attribution'])
        self.assertIsNone(g['marker_gap'])
        self.assertEqual(r['attributed'], [])
        self.assertEqual(r['unattributed'], [g])
        self.assertEqual(a.decide([r], gaps_only=True)[::4],
                         ('GAP-SCAN OK', 0))

    def test_gap_crossing_settle_boundary_is_startup(self):
        # The previous end-based rule called this a no-action stall merely
        # because the next push landed just beyond the startup cutoff.
        spec = [(float(t), 'P', '') for t in range(0, 961, 20)] + \
            [(962.5, 'P', ''), (1013.12, 'P', '')]
        r = a.analyse_run(build(spec), settle_s=1.0, gaps_only=True)
        self.assertEqual(len(r['gaps']), 1)
        self.assertEqual(r['gaps'][0]['context'], 'startup')
        self.assertFalse(r['gaps'][0]['post_settle'])
        self.assertEqual(r['unexplained'], [])

    def test_gaps_only_still_classifies_expected_contexts(self):
        spec = [(0, 'P', ''), (0.05, 'M', 'fast-on')] + \
            frame_cycle(0.5, present_ms=100.0) + [(102.5, 'P', '')]
        r = a.analyse_run(build(spec), settle_s=0.0, gaps_only=True)
        self.assertEqual(len(r['gaps']), 1)
        self.assertEqual(r['gaps'][0]['context'], 'fast-forward')
        self.assertTrue(r['gaps'][0]['expected'])
        self.assertEqual(r['unexplained'], [])

    def test_stall_inside_mid_frame_service_events(self):
        spec = [(0, 'P', ''),
                (0.5, 'M', 'svcev-enter'),
                (90.5, 'M', 'svcev-exit'),
                (91.0, 'P', '')]
        r = self.analyse(spec)
        g = r['unexplained'][0]
        self.assertIn('SDL_PumpEvents mid-frame', g['attribution'])
        self.assertEqual(g['marker_gap']['within'], [])

    def test_evwatch_records_validate_but_do_not_fragment_attribution(self):
        # GBARECOMP_EVENT_WATCH=1 adds evwatch:<type hex> records per
        # delivered event. They must be known (no refusal) but excluded from
        # the adjacent-interval ranking, so the top interval stays the
        # enclosing svcev bracket.
        spec = [(0, 'P', ''),
                (0.5, 'M', 'svcev-enter'),
                (30.0, 'M', 'evwatch:00000400'),
                (60.0, 'M', 'evwatch:00000300'),
                (90.5, 'M', 'svcev-exit'),
                (91.0, 'P', '')]
        r = self.analyse(spec)
        self.assertIsNone(r['problem'])
        g = r['unexplained'][0]
        self.assertIn('SDL_PumpEvents mid-frame', g['attribution'])
        self.assertEqual(g['marker_gap']['from'], 'svcev-enter')
        self.assertEqual(g['marker_gap']['to'], 'svcev-exit')
        self.assertEqual(g['marker_gap']['interval_ms'], 90.0)

    def test_evwatch_does_not_hide_an_unknown_marker(self):
        spec = [(0, 'P', ''),
                (0.5, 'M', 'svcev-enter'),
                (10.0, 'M', 'evwatch:00000200'),
                (20.0, 'M', 'bogus-label'),
                (90.5, 'M', 'svcev-exit'),
                (91.0, 'P', '')]
        r = self.analyse(spec)
        self.assertIn('unknown marker', r['problem'])

    def test_malformed_probe_labels_are_not_accepted_as_known(self):
        for bad_label in ('evwatch:not-hex', 'evwatch:0000800',
                          'evwatch:00008000x', 'canary-push-open',
                          'canary-push-exit-extra'):
            with self.subTest(label=bad_label):
                spec = [(0, 'P', ''),
                        (0.5, 'M', 'svcev-enter'),
                        (30.0, 'M', bad_label),
                        (90.5, 'M', 'svcev-exit'),
                        (91.0, 'P', '')]
                r = self.analyse(spec)
                self.assertIn('unknown marker', r['problem'])

    def test_canary_push_records_validate_but_never_fragment_attribution(self):
        # GBARECOMP_EVENT_CANARY writes canary-push-enter/-exit from a helper
        # thread, so the pair can interleave with (and even span past) a
        # main-thread bracket. It must validate as known and stay out of the
        # interval ranking: the top interval remains the enclosing svcev
        # bracket, exactly as without the probe.
        spec = [(0, 'P', ''),
                (0.5, 'M', 'svcev-enter'),
                (30.0, 'M', 'canary-push-enter'),
                (30.1, 'M', 'canary-push-queued'),
                (30.2, 'M', 'canary-push-exit'),
                (40.0, 'M', 'canary-push-enter'),
                (89.9, 'M', 'canary-push-rejected'),
                (90.0, 'M', 'canary-push-exit'),
                (90.5, 'M', 'svcev-exit'),
                (91.0, 'P', '')]
        r = self.analyse(spec)
        self.assertIsNone(r['problem'])
        g = r['unexplained'][0]
        self.assertIn('SDL_PumpEvents mid-frame', g['attribution'])
        self.assertEqual(g['marker_gap']['from'], 'svcev-enter')
        self.assertEqual(g['marker_gap']['to'], 'svcev-exit')
        self.assertEqual(g['marker_gap']['interval_ms'], 90.0)

    def test_canary_does_not_hide_an_unknown_marker(self):
        spec = [(0, 'P', ''),
                (0.5, 'M', 'svcev-enter'),
                (10.0, 'M', 'canary-push-enter'),
                (10.1, 'M', 'canary-push-exit'),
                (20.0, 'M', 'bogus-label'),
                (90.5, 'M', 'svcev-exit'),
                (91.0, 'P', '')]
        r = self.analyse(spec)
        self.assertIn('unknown marker', r['problem'])

    def test_gap_opening_on_an_exit_marker_drops_the_closed_region(self):
        spec = [(0, 'P', '')] + frame_cycle(0.5) + \
            [(101.5, 'P', '')] + frame_cycle(102.0) + [(104.0, 'P', '')]
        r = self.analyse(spec)
        g = r['unexplained'][0]
        self.assertEqual(g['marker_gap']['from'], 'fh-exit')
        self.assertEqual(g['marker_gap']['to'], 'fh-enter')
        self.assertEqual(g['marker_gap']['within'], [])

    def test_stall_between_frame_hooks_is_guest_execution(self):
        spec = [(0, 'P', '')] + frame_cycle(0.5) + [(2.0, 'P', '')] + \
            frame_cycle(102.0) + [(104.0, 'P', '')]
        r = self.analyse(spec)
        gaps = r['unexplained']
        self.assertEqual(len(gaps), 1)
        self.assertIn('guest execution between frame hooks',
                      gaps[0]['attribution'])
        self.assertEqual(gaps[0]['marker_gap']['from'], 'fh-exit')
        self.assertEqual(gaps[0]['marker_gap']['to'], 'fh-enter')

    def test_nested_intervals_rank_by_overlap_and_report_chain(self):
        # pace encloses present; the innermost pair has the largest overlap.
        spec = [(0, 'P', ''),
                (0.1, 'M', 'pace-enter'),
                (10.0, 'M', 'present-enter'),
                (43.0, 'M', 'present-exit'),
                (44.0, 'M', 'pace-exit'),
                (45.0, 'P', '')]
        r = self.analyse(spec)
        g = r['unexplained'][0]
        self.assertEqual(g['marker_gap']['from'], 'present-enter')
        self.assertEqual(g['marker_gap']['to'], 'present-exit')
        self.assertEqual(g['marker_gap']['within'], ['pace-enter'])
        self.assertIn('SDL present', g['attribution'])

    def test_expected_contexts_are_classified(self):
        # The assist edge is applied in the same pump, AFTER the frame's
        # push, so it lands inside the producer gap (as in real captures).
        ff = [(0, 'P', ''), (0.05, 'M', 'fast-on')] + \
            frame_cycle(0.5, present_ms=100.0) + [(102.5, 'P', '')]
        r = self.analyse(ff)
        self.assertEqual(r['unexplained'], [])
        self.assertEqual(r['gaps'][0]['context'], 'fast-forward')
        self.assertTrue(r['gaps'][0]['expected'])
        paused = [(0, 'P', ''), (0.05, 'M', 'pause')] + \
            frame_cycle(0.5, present_ms=100.0) + [(102.5, 'P', '')]
        r = self.analyse(paused)
        self.assertEqual(r['gaps'][0]['context'], 'paused')
        startup = [(0, 'P', '')] + frame_cycle(0.5, present_ms=60.0) + \
            [(102.0, 'P', '')]
        r = a.analyse_run(build(startup), settle_s=1.0)
        self.assertEqual(r['gaps'][0]['context'], 'startup')
        self.assertEqual(r['unexplained'], [])

    def test_stale_state_edge_does_not_mask_a_real_stall(self):
        # fast-off happened before the gap: nothing muted pushes inside it.
        spec = [(0, 'M', 'fast-off'), (0.1, 'P', '')] + \
            frame_cycle(0.5, present_ms=100.0) + [(102.5, 'P', '')]
        r = self.analyse(spec)
        self.assertEqual(r['gaps'][0]['context'], 'no-action')
        self.assertEqual(len(r['unexplained']), 1)

    def test_unattributed_when_no_marker_interval_is_large(self):
        spec = [(0, 'P', ''), (0.5, 'M', 'fh-enter')]
        for t in range(10, 100, 10):
            spec.append((float(t), 'M', 'rewind-store'))
        spec += [(100.5, 'M', 'fh-exit'), (105.0, 'P', '')]
        r = self.analyse(spec)
        self.assertEqual(len(r['unattributed']), 1)
        self.assertIn('UNATTRIBUTED', r['unattributed'][0]['attribution'])
        self.assertLessEqual(
            r['unattributed'][0]['marker_gap']['interval_ms'], 10.5)

    def test_threshold_filters_small_gaps(self):
        spec = [(0, 'P', '')] + frame_cycle(0.5, present_ms=30.0) + \
            [(32.0, 'P', '')]
        r = self.analyse(spec)
        self.assertEqual(r['gaps'], [])
        r = self.analyse(spec, min_gap_ms=20.0)
        self.assertEqual(len(r['gaps']), 1)

    def test_pull_cadence_inside_the_gap_is_reported(self):
        spec = [(0, 'P', ''),
                (0.5, 'M', 'present-enter'),
                (0.6, 'M', 'present-exit'),
                (10.0, 'C', ''),
                (20.0, 'C', ''),
                (30.0, 'C', ''),
                (46.0, 'P', '')]
        r = self.analyse(spec)
        p = r['unexplained'][0]['pulls']
        self.assertEqual(p['n'], 3)
        self.assertAlmostEqual(p['median_interval_ms'], 10.0)
        self.assertAlmostEqual(p['max_interval_ms'], 10.0)


class RejectionTest(unittest.TestCase):
    def test_legacy_capture_without_phase_markers_is_refused(self):
        spec = [(0, 'P', ''), (1.0, 'M', 'premem-save'),
                (1.1, 'M', 'postmem-save'), (100.0, 'P', '')]
        r = a.analyse_run(build(spec), settle_s=0.0)
        self.assertIn('no phase markers', r['problem'])

    def test_malformed_timeline_is_refused(self):
        evs = build([(0, 'P', '')] + frame_cycle(0.5) + [(101.0, 'P', '')])
        for r in evs:
            if r['kind'] == 'P' and r['offset'] == 64:
                r['offset'] = 65
        r = a.analyse_run(evs, settle_s=0.0)
        self.assertIn('timeline inconsistent', r['problem'])

    def test_unbalanced_and_unknown_markers_are_refused(self):
        spec = [(0, 'P', ''), (0.5, 'M', 'present-enter'),
                (100.5, 'M', 'present-exit'), (101.0, 'P', ''),
                (101.5, 'M', 'present-enter'), (102.0, 'P', '')]
        r = a.analyse_run(build(spec), settle_s=0.0)
        self.assertIn('unterminated present-enter', r['problem'])
        spec = [(0, 'P', ''), (0.5, 'M', 'bogus-label'),
                (0.6, 'M', 'present-enter'), (0.7, 'M', 'present-exit'),
                (101.0, 'P', '')]
        r = a.analyse_run(build(spec), settle_s=0.0)
        self.assertIn('unknown marker', r['problem'])

    def test_decide_refuses_when_any_run_is_unusable(self):
        good = a.analyse_run(
            build([(0, 'P', '')] + frame_cycle(0.5, present_ms=100.0) +
                  [(103.0, 'P', '')]), settle_s=0.0)
        bad = {'problem': 'no phase markers in capture (legacy)',
               'unexplained': [], 'unattributed': []}
        verdict, refused, _u, _a, rc = a.decide([good, bad])
        self.assertEqual((verdict, rc), ('STALL-ATTR REFUSED', 1))
        self.assertEqual(len(refused), 1)


class VerdictTest(unittest.TestCase):
    def run_with_gap(self, present_ms=100.0):
        return a.analyse_run(
            build([(0, 'P', '')] + frame_cycle(0.5, present_ms=present_ms) +
                  [(103.0, 'P', '')]), settle_s=0.0)

    def test_ok_when_every_unexplained_gap_is_attributed(self):
        verdict, _r, unexplained, unattributed, rc = a.decide(
            [self.run_with_gap()])
        self.assertEqual((verdict, rc), ('STALL-ATTR OK', 0))
        self.assertEqual((unexplained, unattributed), (1, 0))

    def test_partial_when_a_gap_stays_unattributed(self):
        spec = [(0, 'P', ''), (0.5, 'M', 'fh-enter')]
        for t in range(10, 100, 10):
            spec.append((float(t), 'M', 'rewind-store'))
        spec += [(100.5, 'M', 'fh-exit'), (105.0, 'P', '')]
        run = a.analyse_run(build(spec), settle_s=0.0)
        verdict, _r, unexplained, unattributed, rc = a.decide([run])
        self.assertEqual((verdict, rc), ('STALL-ATTR PARTIAL', 1))
        self.assertEqual((unexplained, unattributed), (1, 1))

    def test_no_gaps_is_still_ok_and_empty_is_insufficient(self):
        spec = []
        for k in range(5):
            spec += [(k * 20.0, 'P', '')] + frame_cycle(k * 20.0 + 0.5)
        quiet = a.analyse_run(build(spec), settle_s=0.0)
        verdict, _r, unexplained, unattributed, rc = a.decide([quiet])
        self.assertEqual((verdict, rc), ('STALL-ATTR OK', 0))
        self.assertEqual((unexplained, unattributed), (0, 0))
        verdict, _r, _u, _a, rc = a.decide([])
        self.assertEqual((verdict, rc), ('STALL-ATTR INSUFFICIENT', 1))

    def test_rule_version(self):
        self.assertEqual(a.TOOL_RULE_VERSION,
                         '2026-09-27-stall-attribution-v6')


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_run_sources_accepts_sweep_json_and_run_dirs(self):
        evs = build([(0, 'P', '')] + frame_cycle(0.5, present_ms=100.0) +
                    [(103.0, 'P', '')])
        csv = self.tmp / 'cap-events.csv'
        with csv.open('w') as fh:
            fh.write('kind,ns,offset,frames,fill_ms,stretch_frames,'
                     'underrun_frames,overflow_frames,label\n')
            for r in evs:
                fh.write(f"{r['kind']},{r['ns']},{r['offset']},{r['frames']},"
                         f"{r['fill_ms']:.6f},{r['stretch_frames']},"
                         f"{r['underrun_frames']},{r['overflow_frames']},"
                         f"{r['label']}\n")
        sweep = self.tmp / 'sweep.json'
        sweep.write_text(json.dumps({
            'edges_runs': [], 'control_runs': [
                {'tag': 'control', 'repeat': 0, 'events_path': str(csv)}]}))
        sources, problems = a.run_sources([sweep], [str(self.tmp)])
        self.assertEqual(problems, [])
        self.assertEqual(len(sources), 2)
        self.assertTrue(sources[0][0].startswith('sweep.json:'))
        self.assertTrue(all(s[1] for s in sources))

    def test_missing_sweep_is_a_problem(self):
        _sources, problems = a.run_sources(
            [self.tmp / 'nope.json'], [str(self.tmp / 'nodir')])
        self.assertTrue(any('missing sweep report' in p for p in problems))


if __name__ == '__main__':
    unittest.main(verbosity=2)
