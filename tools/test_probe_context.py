#!/usr/bin/env python3
"""Unit checks for the portable SDL event-probe context analyzer."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_context as p  # noqa: E402


class ProbeContextTest(unittest.TestCase):
    def test_pair_all_rejects_incomplete_or_nested_brackets(self):
        good = [('M', 1, 'svcev-enter'),
                ('M', 2, 'canary-push-enter'),
                ('M', 3, 'canary-push-exit'),
                ('M', 4, 'svcev-exit')]
        self.assertEqual(p.pair_all(good, ['svcev', p.CANARY])['svcev'],
                         [(1, 4)])
        for rows in (good[:-1],
                     [('M', 1, 'canary-push-exit')],
                     [('M', 1, 'svcev-enter'),
                      ('M', 2, 'svcev-enter')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                p.pair_all(rows, ['svcev', p.CANARY])

    def test_canary_delivery_count_uses_registered_type(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'stderr.log').write_text(
                'host_window: event canary active: type=0x8001 interval=8ms\n')
            label = p.canary_watch_label({'dir': td}, [(10, 20)])
            self.assertEqual(label, 'evwatch:00008001')
            watches = [(12, label), (13, 'evwatch:00000659'),
                       (22, label)]
            self.assertEqual(p.count_canary_watch_callbacks(
                watches, [(10, 20)], label), 1)

    def test_missing_canary_type_refuses_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'stderr.log').write_text('no canary banner\n')
            with self.assertRaises(ValueError):
                p.canary_watch_label({'dir': td}, [(10, 20)])
            self.assertIsNone(p.canary_watch_label({'dir': td}, []))

    def test_startup_follows_producer_gap_start(self):
        self.assertTrue(p.is_startup_gap(999, 1000))
        self.assertFalse(p.is_startup_gap(1000, 1000))
        self.assertIsNone(p.is_startup_gap(None, 1000))

    def test_explicit_canary_results_validate_one_per_bracket(self):
        brackets = [(10, 20), (30, 40)]
        rows = [('M', 15, 'canary-push-queued'),
                ('M', 35, 'canary-push-rejected')]
        self.assertEqual(p.canary_outcomes(rows, brackets),
                         [(15, 'canary-push-queued'),
                          (35, 'canary-push-rejected')])
        self.assertEqual(p.canary_outcomes([], brackets), [])  # older captures
        for bad in (rows[:1], rows + [('M', 36, 'canary-push-queued')],
                    [('M', 14, 'canary-push-queued'),
                     ('M', 16, 'canary-push-rejected')]):
            with self.subTest(rows=bad), self.assertRaises(ValueError):
                p.canary_outcomes(bad, brackets)


if __name__ == '__main__':
    unittest.main()
