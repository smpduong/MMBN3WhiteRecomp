#!/usr/bin/env python3
"""Unit tests for host_audio_queue_check (no game run required).

Covers the rules that decide whether the host-queue evidence supports a
claim, so a regression cannot reintroduce the earlier false-green:

  * alignment comes ONLY from in-engine action markers -- a capture with no
    marker is rejected, never re-aligned by assist pump number (the defect
    that made the old tool index P records by pump);
  * timeline / FIFO / push-count sanity;
  * counter steps are found on the MERGED event stream (not just producer
    pushes) and attributed by steady_clock time window;
  * a noisy no-restore control refuses attribution (QUEUE FAIL, never OK);
  * valid exposure with no decisive post-bridge identification yields
    QUEUE UNRESOLVED, not QUEUE OK;
  * the post-bridge tail correlation identifies a delayed copy and refuses a
    weak/ambiguous one.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import host_audio_queue_check as q  # noqa: E402


def ev(kind, ns, offset=0, frames=0, fill_ms=0.0, label='',
       stretch_frames=0, underrun_frames=0, overflow_frames=0):
    return {'kind': kind, 'ns': ns, 'offset': offset, 'frames': frames,
            'fill_ms': fill_ms, 'stretch_frames': stretch_frames,
            'underrun_frames': underrun_frames,
            'overflow_frames': overflow_frames, 'label': label}


def synth_capture(with_marker=True, marker_fill=40.0, step_in_window=False,
                  step_kind='stretch_frames'):
    """A small but structurally valid capture around a load boundary.

    Producer pushes 1 ms of ns apart (16 frames each); pulls every other ms;
    the marker sits at 41 ms. A step close to the marker lands on a PULL
    record (the case the old push-only scan could not see); a step far from
    it lands 359 ms later and must stay unattributed.
    """
    ms = 1_000_000
    ns0 = 1_000_000_000
    events = []
    src_off = out_off = 0
    for i in range(40):
        t = ns0 + i * ms
        events.append(ev('P', t, offset=src_off, frames=16,
                         fill_ms=marker_fill + (i - 40) * 0.1))
        src_off += 16
        if i % 2 == 0:
            events.append(ev('C', t + 400_000, offset=out_off, frames=16,
                             fill_ms=marker_fill))
            out_off += 16
    if with_marker:
        events.append(ev('M', ns0 + 41 * ms, fill_ms=marker_fill,
                         label='preload'))
    if step_in_window:
        events.append(ev('C', ns0 + 41 * ms + 200_000, offset=out_off,
                         frames=16, fill_ms=marker_fill,
                         **{step_kind: 111}))
        out_off += 16
    events.append(ev('P', ns0 + 42 * ms, offset=src_off, frames=16,
                     fill_ms=marker_fill))
    src_off += 16
    events.append(ev('C', ns0 + 43 * ms, offset=out_off, frames=16,
                     fill_ms=marker_fill))
    out_off += 16
    if not step_in_window:
        # A counter step far from the boundary (session priming / shutdown).
        events.append(ev('C', ns0 + 400 * ms, offset=out_off, frames=16,
                         fill_ms=marker_fill, **{step_kind: 5}))
        out_off += 16
    events.sort(key=lambda r: r['ns'])
    return events


def branch_from(events, kind='restore', presented=42):
    res = {'events': events}
    analysis, _ = q.analyse_branch(res, kind, 'preload', 150.0, presented,
                                   65536)
    return analysis


class TestParsingAndTimeline(unittest.TestCase):
    def _write(self, header, rows):
        import tempfile
        td = tempfile.mkdtemp()
        p = Path(td) / 'e.csv'
        p.write_text(header + '\n' + '\n'.join(rows) + '\n')
        return p

    def test_new_format_with_label_parses(self):
        p = self._write(
            'kind,ns,offset,frames,fill_ms,stretch_frames,underrun_frames,'
            'overflow_frames,label',
            ['M,1000,0,0,40.000000,0,0,0,preload'])
        rows = q.read_events(p)
        self.assertEqual(rows[0]['kind'], 'M')
        self.assertEqual(rows[0]['label'], 'preload')
        self.assertEqual(rows[0]['fill_ms'], 40.0)

    def test_legacy_format_without_label_parses_with_empty_label(self):
        p = self._write(
            'kind,ns,offset,frames,fill_ms,stretch_frames,underrun_frames,'
            'overflow_frames',
            ['P,1000,0,16,40.000000,0,0,0'])
        rows = q.read_events(p)
        self.assertEqual(rows[0]['label'], '')

    def test_timeline_offsets_must_be_contiguous(self):
        events = synth_capture()
        self.assertEqual(q.verify_timeline(events)['problems'], [])
        broken = [dict(r) for r in events]
        for r in broken:
            if r['kind'] == 'P' and r['offset'] == 32:
                r['offset'] = 48
                break
        problems = q.verify_timeline(broken)['problems']
        self.assertTrue(any('producer offset' in p for p in problems), problems)


class TestMarkerAlignment(unittest.TestCase):
    def test_marker_locates_the_boundary_without_pump_arithmetic(self):
        a = branch_from(synth_capture(marker_fill=41.5))
        self.assertIsNone(a['problem'])
        self.assertEqual(a['boundary']['fill_ms'], 41.5)
        self.assertEqual(a['boundary']['frames'],
                         int(round(41.5 * 65536 / 1000.0)))
        self.assertEqual(a['marker_hits'], 1)

    def test_missing_marker_is_rejected_not_reindexed_by_pump(self):
        a = branch_from(synth_capture(with_marker=False))
        self.assertIsNotNone(a['problem'])
        self.assertIn('no', a['problem'])
        self.assertIn('marker', a['problem'])
        self.assertIn('pump-index fallback refused', a['problem'])
        self.assertNotIn('boundary', a)

    def test_last_marker_is_the_control_boundary(self):
        events = synth_capture(with_marker=False)
        # Two presaves: the control boundary is the LAST one (the save one
        # pump before where the restore would fire), even when a later 'M'
        # of another label exists in the stream.
        events.append(ev('M', 1_000_000_000 + 10_000_000, fill_ms=30.0,
                         label='presave'))
        events.append(ev('M', 1_000_000_000 + 50_000_000, fill_ms=30.0,
                         label='presave'))
        events.sort(key=lambda r: r['ns'])
        last_presave = max(i for i, r in enumerate(events)
                           if r['label'] == 'presave')
        self.assertEqual(q.find_marker(events, 'presave', -1), last_presave)
        self.assertEqual(len(q.marker_indices(events, 'presave')), 2)

    def test_host_action_markers_do_not_confuse_boundary_selection(self):
        # pause/resume/fast-forward/rewind-trigger markers share the stream
        # with save/load markers; the boundary must still be the LAST marker
        # with the requested label, not whichever marker is nearest.
        events = synth_capture(marker_fill=42.0)
        for lab, offset in (('fast-on', -30_000_000),
                            ('fast-off', -20_000_000),
                            ('pause', -16_000_000),
                            ('resume', -12_000_000),
                            ('rewind-trigger', 1_000_000)):
            events.append(ev('M', 1_000_000_000 + 41_000_000 + offset,
                             fill_ms=1.0, label=lab))
        events.sort(key=lambda r: r['ns'])
        a = branch_from(events)
        self.assertIsNone(a['problem'])
        self.assertEqual(a['boundary']['fill_ms'], 42.0)
        self.assertEqual(a['marker_hits'], 1)
        labels = a['timeline']['marker_labels']
        for want in ('fast-on', 'fast-off', 'pause', 'resume',
                     'rewind-trigger', 'preload'):
            self.assertIn(want, labels)

    def test_memory_path_marker_labels_are_distinct(self):
        events = synth_capture(with_marker=False)
        events.append(ev('M', 1_000_000_000 + 41_000_000, fill_ms=40.0,
                         label='premem-load'))
        events.sort(key=lambda r: r['ns'])
        a, _ = q.analyse_branch({'events': events}, 'restore', 'premem-load',
                                150.0, 42, 65536)
        self.assertIsNone(a['problem'])
        self.assertEqual(a['boundary']['label'], 'premem-load')


class TestBufferStateCheck(unittest.TestCase):
    """The marker's fill must be consistent with the captured counts.

    Output frames pulled beyond the bridge's read cursor (`src_before - fill`)
    are priming/conceal silence and are normal; they are reported, not failed.
    """

    def _capture(self, lead_in=0, fill_delta_ms=0.0):
        ms = 1_000_000
        ns0 = 1_000_000_000
        events = []
        src = out = 0
        if lead_in:
            events.append(ev('C', ns0 - 1_000_000, offset=0, frames=lead_in))
            out += lead_in
        for i in range(10):
            events.append(ev('P', ns0 + i * ms, offset=src, frames=16,
                             fill_ms=0))
            src += 16
            if i % 2 == 0:
                events.append(ev('C', ns0 + i * ms + 400_000, offset=out,
                                 frames=16, fill_ms=0))
                out += 16
        fill = src - (out - lead_in)
        events.append(ev('M', ns0 + 11 * ms,
                         fill_ms=fill * 1000.0 / 65536.0 + fill_delta_ms,
                         label='preload'))
        events.sort(key=lambda r: r['ns'])
        return events

    def test_priming_silence_does_not_fail_the_check(self):
        a = branch_from(self._capture(lead_in=32))
        self.assertIsNone(a['problem'])
        self.assertTrue(a['buffer_state_check'])
        self.assertEqual(a['silence_or_priming_output_frames'], 32)
        self.assertEqual(a['cursor_at_boundary'], 80)
        self.assertEqual(q.lead_in_frames(self._capture(lead_in=32)), 32)

    def test_check_holds_without_priming_silence(self):
        a = branch_from(self._capture(lead_in=0))
        self.assertTrue(a['buffer_state_check'])
        self.assertEqual(a['silence_or_priming_output_frames'], 0)

    def test_fill_larger_than_pushed_source_is_rejected(self):
        # 100 ms of extra fill in a 160-frame capture puts the read cursor at
        # a source index the run never reached: the marker cannot describe
        # this stream, so the branch must fail.
        a = branch_from(self._capture(lead_in=32, fill_delta_ms=100.0))
        self.assertFalse(a['buffer_state_check'])
        self.assertLess(a['cursor_at_boundary'], 0)


class TestActionEvidence(unittest.TestCase):
    """Marker presence alone is not enough: the scripted action must have run
    on the expected path (assist events + rewind/file-load log line)."""

    def test_assist_events_must_match_the_script(self):
        res = {'events': synth_capture(),
               'events_observed': [(5, 'save1'), (299, 'save2')]}
        a, _ = q.analyse_branch(res, 'restore', 'preload', 150.0, 42, 65536,
                                expected_events=[(5, 'save1'),
                                                 (299, 'save2'),
                                                 (300, 'load1'),
                                                 (315, 'save3')])
        self.assertIsNotNone(a['problem'])
        self.assertIn('observed assist events', a['problem'])

    def test_path_specific_action_line_is_required(self):
        res = {'events': synth_capture(),
               'events_observed': [(5, 'save1'), (299, 'save2'),
                                   (300, 'load1'), (315, 'save3')],
               'file_loads': []}
        a, _ = q.analyse_branch(
            res, 'restore', 'preload', 150.0, 42, 65536,
            expected_events=[(5, 'save1'), (299, 'save2'),
                             (300, 'load1'), (315, 'save3')],
            action_seen=lambda r: bool(r['file_loads']))
        self.assertIsNotNone(a['problem'])
        self.assertIn('expected restore path', a['problem'])


class TestCounterAttribution(unittest.TestCase):
    def test_step_on_a_pull_record_is_found(self):
        # The old tool scanned only the P timeline, so a servo step applied on
        # the pull side between two pushes was invisible.
        a = branch_from(synth_capture(step_in_window=True))
        hits = {c: v for c, v in a['counter_steps_in_window'].items() if v}
        self.assertIn('stretch_frames', hits)

    def test_step_far_outside_the_window_is_not_attributed(self):
        a = branch_from(synth_capture(step_in_window=False))
        hits = {c: v for c, v in a['counter_steps_in_window'].items() if v}
        self.assertEqual(hits, {})
        self.assertTrue(a['action_window_clean'])

    def test_window_is_time_based_not_index_based(self):
        events = synth_capture()
        # The step record is INDEX-adjacent to the marker but 2 s later in
        # steady-clock time: a time window must reject it.
        out_off = sum(r['frames'] for r in events if r['kind'] == 'C')
        events.append(ev('C', max(r['ns'] for r in events) + 2_000_000_000,
                         frames=16, offset=out_off, fill_ms=40.0,
                         stretch_frames=99))
        events.sort(key=lambda r: r['ns'])
        a = branch_from(events)
        self.assertIsNone(a.get('problem'))
        self.assertEqual(
            {c: v for c, v in a['counter_steps_in_window'].items() if v}, {})


class TestDecide(unittest.TestCase):
    def _branch(self, tag, kind, problem=None, clean=True, tail=None,
                exit_=0):
        a = {'action_kind': kind, 'problem': problem,
             'action_window_clean': clean, 'push_count_check': True,
             'buffer_state_check': True, 'counter_steps_in_window': {}}
        b = {'tag': tag, 'repeat': 0, 'analysis': a, 'exit': exit_,
             'timed_out': False, 'truncated': False,
             'capture': {'events': 1}}
        if tail is not None:
            b['output_tail'] = tail
        return b

    def _report(self, branches):
        return {'branches': branches, 'source_save_unchanged': True}

    def test_clean_run_with_established_tail_is_ok(self):
        r = self._report([
            self._branch('control', 'control'),
            self._branch('load-off', 'restore',
                         tail={'status': 'established'}),
        ])
        verdict, bad, notes, rc = q.decide(r, 150.0)
        self.assertEqual((verdict, bad, rc), ('QUEUE OK', [], 0))

    def test_noisy_control_refuses_attribution_and_fails(self):
        r = self._report([
            self._branch('control', 'control', clean=False),
            self._branch('load-off', 'restore',
                         tail={'status': 'established'}),
        ])
        verdict, bad, notes, rc = q.decide(r, 150.0)
        self.assertEqual(verdict, 'QUEUE FAIL')
        self.assertEqual(rc, 1)
        self.assertTrue(any('control-window-not-clean' in b for b in bad), bad)

    def test_unestablished_tail_is_unresolved_not_ok(self):
        r = self._report([
            self._branch('control', 'control'),
            self._branch('load-off', 'restore',
                         tail={'status': 'not-established',
                               'reason': 'not decisive'}),
        ])
        verdict, bad, notes, rc = q.decide(r, 150.0)
        self.assertEqual(verdict, 'QUEUE UNRESOLVED')
        self.assertNotEqual(verdict, 'QUEUE OK')
        self.assertEqual(rc, 1)

    def test_missing_marker_branch_fails(self):
        r = self._report([
            self._branch('control', 'control'),
            self._branch('load-off', 'restore',
                         problem='no preload marker', tail=None),
        ])
        verdict, bad, _, rc = q.decide(r, 150.0)
        self.assertEqual(verdict, 'QUEUE FAIL')
        self.assertTrue(any('no preload marker' in b for b in bad), bad)
        self.assertEqual(rc, 1)

    def test_restore_window_step_fails_even_with_clean_control(self):
        b = self._branch('load-off', 'restore',
                         tail={'status': 'established'})
        b['analysis']['counter_steps_in_window'] = {'underrun_frames': [(9, 0, 1)]}
        r = self._report([self._branch('control', 'control'), b])
        verdict, bad, _, _ = q.decide(r, 150.0)
        self.assertEqual(verdict, 'QUEUE FAIL')
        self.assertTrue(any('bridge counter moved' in x for x in bad), bad)

    def test_source_save_change_fails(self):
        r = self._report([self._branch('control', 'control'),
                          self._branch('load-off', 'restore',
                                       tail={'status': 'established'})])
        r['source_save_unchanged'] = False
        _, bad, _, _ = q.decide(r, 150.0)
        self.assertIn('source_save_unchanged', bad)


def _signal(n, seed=12345):
    """A deterministic, non-periodic, non-silent test signal."""
    import array
    out = array.array('h')
    x = seed
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out.append((x % 8001) - 4000)
    return out


class TestOutputTailCheck(unittest.TestCase):
    """Model: ring fill = src_before - out_before (equal rates), so the unread
    source frames at the boundary are src[out_before:src_before] and, with no
    flush, they are the NEXT thing the device plays (out[out_before + i] ==
    src[out_before + i]). A flushed ring would skip them."""

    def test_unflushed_ring_content_is_identified(self):
        src = _signal(20000)
        out = q.array.array('h', src)
        depth, s0 = 1024, 5000
        o0 = s0 - depth
        r = q.output_tail_check(src, out, s0, o0, depth, window=1024,
                                max_lag=64)
        self.assertEqual(r['status'], 'established', r)
        self.assertGreater(r['corr_pre'], 0.95)
        self.assertGreater(r['corr_pre'] - r['corr_post'], 0.10)

    def test_flushed_ring_is_not_identified(self):
        # Post-bridge output contains post-restore content where the pre-tail
        # should be: the check must refuse to claim old audio survived.
        src = _signal(20000)
        out = q.array.array('h', src)
        depth, s0 = 1024, 5000
        o0 = s0 - depth
        other = _signal(1024, seed=999)
        for i in range(depth):
            out[o0 + i] = other[i]
        r = q.output_tail_check(src, out, s0, o0, depth, window=1024,
                                max_lag=64)
        self.assertEqual(r['status'], 'not-established', r)
        self.assertIn('not decisive', r['reason'])

    def test_silent_output_window_is_not_established(self):
        src = _signal(20000)
        out = q.array.array('h', [0] * len(src))
        r = q.output_tail_check(src, out, 5000, 3976, 1024, window=1024,
                                max_lag=64)
        self.assertEqual(r['status'], 'not-established')
        self.assertIn('near-silent', r['reason'])

    def test_lag_search_recovers_a_shifted_copy(self):
        # The bridge can apply a small fractional-frame offset; the search
        # must find the copy even when it is not at lag 0.
        src = _signal(20000)
        out = q.array.array('h', [0] * 40)
        for k in range(40, len(src)):
            out.append(src[k - 40])
        depth, s0, o0 = 1024, 5000, 3976
        r = q.output_tail_check(src, out, s0, o0, depth, window=1024,
                                max_lag=64)
        self.assertEqual(r['status'], 'established', r)
        self.assertEqual(r['lag_frames'], 40)

    def test_correlation_core(self):
        a = _signal(512)
        self.assertAlmostEqual(q.normalized_correlation(a, a, 1), 1.0, 5)
        neg = q.array.array('h', [-v for v in a])
        self.assertAlmostEqual(q.normalized_correlation(a, neg, 1), -1.0, 5)


class BranchEnvTest(unittest.TestCase):
    """The run environment is explicit: ambient GBARECOMP_* is dropped."""

    class Args:
        trace = '/fixture/trace.csv'
        heal_cache = None

        def __init__(self, extra_env):
            self.extra_env = extra_env

    def test_parent_gbarecomp_vars_are_dropped_and_overrides_applied(self):
        import os
        import tempfile
        os.environ['GBARECOMP_AMBIENT'] = 'must-not-leak'
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td)
                args = self.Args(['GBARECOMP_PHASE_MARKERS=0',
                                  'GBARECOMP_EVENT_WATCH=1',
                                  'GBARECOMP_EVENT_CANARY=8',
                                  'GBARECOMP_SDL_COST=25',
                                  'GBARECOMP_NO_GAMEPAD=1',
                                  'GBARECOMP_LOAD_TRACE=1',
                                  'GBARECOMP_HEAL_PREWARM_MAP=1',
                                  'GBARECOMP_HEAL_OUT_OF_PROCESS_LOAD=0'])
                env = q.branch_env('20:save1', 'off', out, args)
                self.assertNotIn('GBARECOMP_AMBIENT', env)
                self.assertEqual(env['GBARECOMP_PHASE_MARKERS'], '0')
                self.assertEqual(env['GBARECOMP_EVENT_WATCH'], '1')
                self.assertEqual(env['GBARECOMP_EVENT_CANARY'], '8')
                self.assertEqual(env['GBARECOMP_SDL_COST'], '25')
                self.assertEqual(env['GBARECOMP_NO_GAMEPAD'], '1')
                self.assertEqual(env['GBARECOMP_LOAD_TRACE'], '1')
                self.assertEqual(env['GBARECOMP_HEAL_PREWARM_MAP'], '1')
                self.assertEqual(env['GBARECOMP_HEAL_OUT_OF_PROCESS_LOAD'], '0')
                self.assertEqual(env['GBARECOMP_ASSIST_SCRIPT'], '20:save1')
                self.assertEqual(env['GBARECOMP_AUDIO_CAPTURE'],
                                 str(out / 'cap'))
                self.assertNotIn('GBARECOMP_SAVELOAD_PHASE_SYNC', env)
        finally:
            del os.environ['GBARECOMP_AMBIENT']

    def test_branch_settings_cannot_be_overridden(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for item in ('GBARECOMP_SAVELOAD_PHASE_SYNC=0',
                         'GBARECOMP_INPUT_REPLAY=/wrong/trace.csv',
                         'GBARECOMP_AUDIO_CAPTURE=/wrong/cap',
                         'GBARECOMP_PHASE_MARKERS=unexpected',
                         'GBARECOMP_EVENT_WATCH=2',
                         'GBARECOMP_EVENT_WATCH=',
                         'GBARECOMP_EVENT_CANARY=abc',
                         'GBARECOMP_EVENT_CANARY=2000',
                         'GBARECOMP_EVENT_CANARY=-5',
                         'GBARECOMP_EVENT_CANARY=',
                         'GBARECOMP_SDL_COST=abc',
                         'GBARECOMP_SDL_COST=2000',
                         'GBARECOMP_SDL_COST=-5',
                         'GBARECOMP_SDL_COST=',
                         'GBARECOMP_NO_GAMEPAD=2',
                         'GBARECOMP_NO_GAMEPAD=',
                         'GBARECOMP_LOAD_TRACE=2',
                         'GBARECOMP_LOAD_TRACE=',
                         'GBARECOMP_HEAL_PREWARM_MAP=2',
                         'GBARECOMP_HEAL_PREWARM_MAP=',
                         'GBARECOMP_HEAL_OUT_OF_PROCESS_LOAD=yes',
                         'PATH=/wrong/path'):
                with self.subTest(item=item), self.assertRaises(ValueError):
                    q.branch_env('20:save1', 'both', out, self.Args([item]))

    def test_heal_cache_is_per_run_by_default_and_persistent_when_given(self):
        """Default cache is fresh per run; --heal-cache pins one dir for all."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'run-1'
            env = q.branch_env('20:save1', 'off', out, self.Args([]))
            self.assertEqual(env['GBARECOMP_HEAL_CACHE'], str(out / 'cache'))
            warm = Path(td) / 'warm-cache'
            args = self.Args([])
            args.heal_cache = warm
            env2 = q.branch_env('20:save1', 'off', out, args)
            self.assertEqual(env2['GBARECOMP_HEAL_CACHE'], str(warm))
            # A second run with the same warm dir must resolve to the same
            # cache, i.e. the harness does not clear or rename it.
            other = Path(td) / 'run-2'
            env3 = q.branch_env('20:save1', 'off', other, args)
            self.assertEqual(env3['GBARECOMP_HEAL_CACHE'],
                             env2['GBARECOMP_HEAL_CACHE'])
            self.assertEqual(env3['GBARECOMP_AUDIO_CAPTURE'],
                             str(other / 'cap'))

    def test_malformed_env_override_fails_loudly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            args = self.Args(['MALFORMED'])
            with self.assertRaises(ValueError):
                q.branch_env('20:save1', 'off', Path(td), args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
