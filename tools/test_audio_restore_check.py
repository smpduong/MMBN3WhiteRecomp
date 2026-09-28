#!/usr/bin/env python3
"""Unit tests for audio_restore_check event parsing and verdicts.

Covers the end-to-end negative controls that must fail loudly instead of
passing vacuously: missing save/load lines, identity mismatch, out-of-order
events, duplicate event pairs, plus the PCM verdict (exact, divergent,
empty) and the always-on negative control.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_restore_check import (  # noqa: E402
    negative_control,
    parse_events,
    verdict_for_pair,
)

SAVE = ('savestate_saved slot=1 path="r/rom.state1" pc=0x08000bc4 '
        'frame=27962 mem=71d4dc5ac5ba57d6')
LOAD = ('savestate_loaded slot=1 path="r/rom.state1" pc=0x08000bc4 '
        'frame=27962 mem=71d4dc5ac5ba57d6')
BOOT = ('savestate_loaded path="r/load.state" pc=0x08000bc4 frame=27959')
PRESENTED = 'frames_presented=950'
ASSIST_SAVE = 'assist_script_event pump=5 action=save1'
ASSIST_LOAD = 'assist_script_event pump=405 action=load1'
MARK_SAVE = 'savestate_audio phase=save marker=1000'
MARK_LOAD = 'savestate_audio phase=load marker=1000'


def log(*lines):
    return '\n'.join(lines) + '\n'


class TestParseEvents(unittest.TestCase):
    def run_log(self, *lines):
        # The harness asserts the OBSERVED assist events, so every fixture
        # log carries the requested save1/load1 lines (tests that need them
        # altered/missing build their own log).
        return log(*(list(lines) + [ASSIST_SAVE, ASSIST_LOAD, MARK_SAVE,
                                    MARK_LOAD, PRESENTED]))

    def test_good_pair(self):
        ev = parse_events(self.run_log(BOOT, SAVE, LOAD), 5, 405)
        self.assertIsInstance(ev, dict)
        self.assertEqual(ev['save_frame'], 27962)
        self.assertEqual(ev['load_mem'], '71d4dc5ac5ba57d6')

    def test_missing_save(self):
        ev = parse_events(self.run_log(BOOT, LOAD), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('missing', ev)

    def test_missing_load(self):
        ev = parse_events(self.run_log(BOOT, SAVE), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('missing', ev)

    def test_identity_mismatch(self):
        bad = LOAD.replace('71d4dc5ac5ba57d6', '0' * 16)
        ev = parse_events(self.run_log(BOOT, SAVE, bad), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('mismatch', ev)

    def test_out_of_order(self):
        ev = parse_events(self.run_log(BOOT, LOAD, SAVE), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('out-of-order', ev)

    def test_duplicate_pairs(self):
        ev = parse_events(self.run_log(BOOT, SAVE, LOAD, SAVE, LOAD), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('duplicate', ev)

    def test_missing_boot(self):
        ev = parse_events(self.run_log(SAVE, LOAD), 5, 405)
        self.assertIsInstance(ev, str)

    def test_missing_presented_banner(self):
        ev = parse_events(log(BOOT, SAVE, LOAD), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('frames_presented', ev)

    def test_missing_assist_events(self):
        # Structurally valid but the requested actions never fired: invalid.
        ev = parse_events(log(BOOT, SAVE, LOAD, PRESENTED), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('assist events mismatch', ev)

    def test_missing_sample_markers(self):
        ev = parse_events(log(BOOT, ASSIST_SAVE, ASSIST_LOAD, SAVE, LOAD,
                              PRESENTED), 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('missing savestate_audio marker', ev)

    def test_sample_marker_mismatch(self):
        bad = log(BOOT, ASSIST_SAVE, ASSIST_LOAD, MARK_SAVE,
                  'savestate_audio phase=load marker=1001', SAVE, LOAD,
                  PRESENTED)
        ev = parse_events(bad, 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('marker mismatch', ev)

    def test_sample_marker_identity_recorded(self):
        ev = parse_events(self.run_log(BOOT, SAVE, LOAD), 5, 405)
        self.assertIsInstance(ev, dict)
        self.assertEqual(ev['save_sample_marker'], 1000)
        self.assertEqual(ev['sample_marker_delta'], 0)

    def test_assist_pump_mismatch(self):
        # save1 fired at the wrong pump / load1 never fired at the requested
        # pump: the pump bounds would still be satisfied, so only the observed
        # events can catch it.
        wrong = log(BOOT, ASSIST_SAVE, SAVE,
                    'assist_script_event pump=99 action=load1', LOAD, PRESENTED)
        ev = parse_events(wrong, 5, 405)
        self.assertIsInstance(ev, str)
        self.assertIn('assist events mismatch', ev)

    def test_pump_bounds_violated(self):
        # Load pump beyond the presented run cannot have happened.
        ev = parse_events(self.run_log(BOOT, SAVE, LOAD), 5, 5000)
        self.assertIsInstance(ev, str)
        self.assertIn('bounds violated', ev)
        # Inverted script order is rejected too.
        ev = parse_events(self.run_log(BOOT, SAVE, LOAD), 405, 5)
        self.assertIsInstance(ev, str)
        self.assertIn('bounds violated', ev)


class TestVerdict(unittest.TestCase):
    def test_exact(self):
        v = verdict_for_pair([1, 2, 3], [1, 2, 3])
        self.assertTrue(v['equivalent'])
        self.assertIsNone(v['first_diff_sample_offset'])

    def test_divergent(self):
        v = verdict_for_pair([1, 2, 3], [1, 2, 4])
        self.assertFalse(v['equivalent'])
        self.assertEqual(v['first_diff_sample_offset'], 2)

    def test_empty(self):
        v = verdict_for_pair([], [])
        self.assertFalse(v['equivalent'])
        self.assertIn('empty', v.get('reason', ''))

    def test_length_mismatch(self):
        v = verdict_for_pair([1, 2, 3], [1, 2])
        self.assertFalse(v['equivalent'])
        self.assertIn('length mismatch', v.get('reason', ''))

    def test_negative_control(self):
        self.assertTrue(negative_control())


if __name__ == '__main__':
    unittest.main()
