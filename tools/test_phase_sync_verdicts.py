#!/usr/bin/env python3
"""Unit tests for phase_sync_matrix verdict rules (no game run required).

Covers the rules that decide whether a matrix report counts as evidence, so
that a rule change cannot silently weaken a verdict:

  * expect_rejoin -- the per-(sync, backend) rejoin expectation, including
    that save-only under normal healing is NOT asserted in either direction
    (it is 1/3 and 2/7 in the measurements, so neither True nor False is
    honest).
  * cell_failures -- structural checks apply to EVERY cell regardless of
    whether a rejoin is expected, so a save+heal cell that never ran cannot
    pass the offline verdict.
  * report_failures -- source-save, uninterrupted-stability and repeat
    stability gates are all required, so a report that does not reproduce
    cannot be re-verdicted OK offline.
  * cell_horizon -- a legacy report with no stored horizon_check is
    recomputed from the stored pending arrays, and a cell whose stored data
    cannot decide the horizon is unverifiable rather than a silent pass.
  * horizon_pinned -- the arithmetic tying the post-load budget to the
    restored device's cycles-to-next-event.
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import phase_sync_matrix as m  # noqa: E402
import inmem_rewind_check as ir  # noqa: E402


def make_cell(sync='both', backend='heal', load_pump=100, rejoin=True,
              u_pending=((4, 187), (0, 84), (0, 84)),
              r_pending=((4, 187), (0, 84), (0, 84), (0, 84))):
    """A structurally complete, passing cell by default.

    Pending arrays follow the observed engine line order: U logs
    [init-load, save1, save2] and R logs [init-load, save1, load1, save2].
    The defaults model a `both`-mode cell, where the save-side flush leaves
    nothing pending at the save (so the restored horizon equals the budget,
    84) and the load-side reset pins R's load to exactly that.
    """
    return {
        'load_pump': load_pump, 'sync': sync, 'backend': backend,
        'ran_ok': True, 'events_ok': True, 'load_event_observed': True,
        'boundary_identity': True, 'frame_match': True, 'rejoined': rejoin,
        'U': {'pending': [list(p) for p in u_pending]},
        'R': {'pending': [list(p) for p in r_pending]},
    }


def make_report(cells):
    return {'cells': cells, 'source_save_unchanged': True,
            'uninterrupted_stable_across_syncs': True, 'repeat_stable': True}


class TestExpectRejoin(unittest.TestCase):
    def test_both_is_asserted_per_cell(self):
        for backend in ('interp', 'heal'):
            self.assertIs(m.expect_rejoin('both', backend), True, backend)

    def test_negative_controls_are_measure_only_per_cell(self):
        # The fork is boundary-dependent, so "must fork" cannot be asserted
        # per cell; it is asserted in aggregate instead.
        for backend in ('interp', 'heal'):
            for sync in ('off', 'load'):
                self.assertIsNone(m.expect_rejoin(sync, backend),
                                  f'{sync}/{backend}')

    def test_save_is_measure_only_on_both_backends(self):
        # The save+interp assertion is withdrawn: those cells came from the
        # stalled-guest interp runs.
        for backend in ('interp', 'heal'):
            self.assertIsNone(m.expect_rejoin('save', backend), backend)

    def test_horizon_expected_only_when_load_bit_set(self):
        self.assertFalse(m.horizon_expected('off'))
        self.assertFalse(m.horizon_expected('save'))
        self.assertTrue(m.horizon_expected('load'))
        self.assertTrue(m.horizon_expected('both'))


class TestNegativeControlRules(unittest.TestCase):
    def test_control_that_forks_somewhere_passes(self):
        cells = [make_cell(sync='off', rejoin=False),
                 make_cell(sync='off', rejoin=True, load_pump=200)]
        bad, notes = m.negative_control_rules(cells)
        self.assertEqual(bad, [])
        self.assertTrue(any('off' in n for n in notes))

    def test_control_that_never_forks_is_vacuous_and_fails(self):
        # Every off cell rejoining means the suite cannot demonstrate the
        # defect, so its passes are uninformative, not good news.
        cells = [make_cell(sync='off', rejoin=True)]
        bad, _ = m.negative_control_rules(cells)
        self.assertIn('negative-control-off-vacuous', bad)

    def test_missing_control_group_is_not_flagged(self):
        self.assertEqual(m.negative_control_rules(
            [make_cell(sync='both', rejoin=True)]), ([], []))

    def test_vacuous_control_fails_through_report_failures(self):
        d = make_report([make_cell(sync='off', rejoin=True)])
        self.assertIn('negative-control-off-vacuous', m.report_failures(d))


class TestCellFailures(unittest.TestCase):
    def test_clean_cell_passes(self):
        self.assertEqual(m.cell_failures(make_cell()), [])

    def test_structural_failures_apply_even_with_no_rejoin_expectation(self):
        # save+heal has no asserted rejoin, so a rejoin bit cannot save it:
        # the structural checks must still fire.
        for key in ('ran_ok', 'events_ok', 'load_event_observed',
                    'boundary_identity', 'frame_match'):
            c = make_cell(sync='save', backend='heal')
            c[key] = False
            self.assertIn(key, m.cell_failures(c), key)

    def test_rejoin_mismatch_fails_where_asserted(self):
        self.assertTrue(any('rejoin' in f
                            for f in m.cell_failures(make_cell(rejoin=False))))
        # ...and is only reported, not failed, where it is not asserted.
        self.assertEqual(
            m.cell_failures(make_cell(sync='save', backend='heal',
                                      rejoin=False)), [])

    def test_negative_control_rejoining_is_not_a_per_cell_failure(self):
        # off rejoining at one boundary is a property of that boundary; it is
        # reported and handled by the aggregate rule, not failed here.
        self.assertEqual(
            m.cell_failures(make_cell(sync='off', rejoin=True)), [])

    def test_off_rejoining_is_caught_by_the_aggregate_rule(self):
        # A single off cell that rejoins is not a per-cell failure, but an off
        # group where NOTHING forks is a vacuous negative control.
        alone = make_report([make_cell(sync='off', rejoin=True)])
        self.assertIn('negative-control-off-vacuous',
                      m.report_failures(alone))
        withfork = make_report([make_cell(sync='off', rejoin=True),
                                make_cell(sync='off', rejoin=False,
                                          load_pump=200)])
        self.assertNotIn('negative-control-off-vacuous',
                         m.report_failures(withfork))

    def test_horizon_asserted_only_for_load_bit(self):
        # broken horizon in a load-bit cell fails
        c = make_cell(sync='load', rejoin=False,
                      u_pending=((4, 187), (52, 84), (52, 84)),
                      r_pending=((4, 187), (0, 84), (99, 84), (0, 84)))
        self.assertIn('horizon', m.cell_failures(c))
        # ...and is ignored where the reset does not run
        c = make_cell(sync='save', backend='interp',
                      r_pending=((4, 187), (0, 84), (99, 84), (0, 84)))
        self.assertEqual(m.cell_failures(c), [])


class TestCellHorizon(unittest.TestCase):
    def test_stored_check_is_used_and_labelled(self):
        c = make_cell()
        c['horizon_check'] = {'applicable': True, 'holds': True,
                              'restored': 136, 'observed': [0, 136]}
        h = m.cell_horizon(c)
        self.assertEqual(h['source'], 'stored')
        self.assertTrue(h['holds'])

    def test_legacy_report_without_stored_check_is_recomputed(self):
        c = make_cell()
        self.assertNotIn('horizon_check', c)
        h = m.cell_horizon(c)
        self.assertEqual(h['source'], 'recomputed')
        self.assertTrue(h['applicable'])
        self.assertTrue(h['holds'])

    def test_unverifiable_horizon_fails_rather_than_passing(self):
        # A legacy report whose stored pending arrays are too short to decide
        # must not count as a pass.
        c = make_cell(sync='both')
        c['U']['pending'] = [[4, 187]]
        c['R']['pending'] = [[4, 187]]
        self.assertFalse(m.cell_horizon(c)['applicable'])
        self.assertIn('horizon-unverifiable', m.cell_failures(c))

    def test_horizon_pinned_arithmetic(self):
        # Restored cycles-to-next-event is the U save budget plus the U save
        # pending (the device materialized point trails the master clock).
        u = {'pending': [[4, 187], [52, 84], [52, 84]]}
        r = {'pending': [[4, 187], [0, 84], [0, 136], [0, 84]]}
        applicable, holds, restored, observed = m.horizon_pinned(u, r)
        self.assertTrue(applicable)
        self.assertEqual(restored['restored_horizon'], 136)
        self.assertEqual(observed, [0, 136])
        self.assertTrue(holds)

        # Not applicable when the stored arrays are too short.
        self.assertEqual(m.horizon_pinned({'pending': []}, {'pending': []}),
                         (False, False, None, None))

    def test_horizon_pinned_rejects_unpinned_budget(self):
        u = {'pending': [[4, 187], [52, 84], [52, 84]]}
        r = {'pending': [[4, 187], [0, 84], [7, 200], [0, 84]]}
        _, holds, _, _ = m.horizon_pinned(u, r)
        self.assertFalse(holds)


class TestReportFailures(unittest.TestCase):
    def test_clean_report_passes(self):
        self.assertEqual(m.report_failures(make_report([make_cell()])), [])

    def test_repeat_stable_is_required(self):
        # This is the offline/live disagreement that was fixed: the offline
        # path used to print repeat_stable without requiring it.
        d = make_report([make_cell()])
        d['repeat_stable'] = False
        self.assertIn('repeat_stable', m.report_failures(d))

    def test_missing_gate_key_fails_and_is_reported(self):
        for key in ('source_save_unchanged',
                    'uninterrupted_stable_across_syncs', 'repeat_stable'):
            d = make_report([make_cell()])
            del d[key]
            self.assertIn(key, m.report_failures(d), key)

    def test_cell_failure_is_reported_with_its_tag(self):
        c = make_cell(sync='both')
        c['ran_ok'] = False
        bad = m.report_failures(make_report([c]))
        self.assertTrue(any(b.startswith('heal/sync=both/load=100/ran_ok')
                            for b in bad), bad)

    def test_offline_and_live_agree_on_the_same_report(self):
        # The live path verdicts the report it just wrote, so both paths go
        # through report_failures. Guard the invariant that a legacy report
        # (no stored horizon_check) verdicts identically either way.
        d = make_report([make_cell(), make_cell(sync='save', backend='heal',
                                                rejoin=False)])
        self.assertEqual(m.report_failures(copy.deepcopy(d)),
                         m.report_failures(copy.deepcopy(d)))


class TestVerdictExistingEndToEnd(unittest.TestCase):
    """Drives the real --verdict entry point on synthetic reports."""

    def verdict(self, report):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'r.json'
            p.write_text(json.dumps(report, indent=2))
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.verdict_existing(p)
            return rc, buf.getvalue()

    def test_clean_report_verdict_ok(self):
        rc, out = self.verdict(make_report([make_cell()]))
        self.assertEqual(rc, 0, out)
        self.assertIn('MATRIX OK', out)

    def test_repeat_unstable_report_fails(self):
        d = make_report([make_cell()])
        d['repeat_stable'] = False
        rc, out = self.verdict(d)
        self.assertEqual(rc, 1, out)
        self.assertIn('MATRIX FAIL', out)
        self.assertIn('repeat_stable', out)

    def test_unverifiable_horizon_fails_offline(self):
        c = make_cell(sync='both')
        c['U']['pending'] = [[4, 187]]
        c['R']['pending'] = [[4, 187]]
        rc, out = self.verdict(make_report([c]))
        self.assertEqual(rc, 1, out)
        self.assertIn('unverifiable', out)

    def test_legacy_save_heal_report_is_reported_not_asserted(self):
        # A legacy save+heal report that fails to rejoin must NOT fail the
        # verdict, and must not be reported as a rejoin mismatch either.
        rc, out = self.verdict(
            make_report([make_cell(sync='save', backend='heal',
                                   rejoin=False)]))
        self.assertEqual(rc, 0, out)
        self.assertIn('expect=None', out)

    def test_legacy_save_heal_report_that_never_ran_still_fails(self):
        c = make_cell(sync='save', backend='heal', rejoin=False)
        c['ran_ok'] = False
        rc, out = self.verdict(make_report([c]))
        self.assertEqual(rc, 1, out)
        self.assertIn('ran_ok', out)

    def test_stale_rule_report_prints_a_note(self):
        d = make_report([make_cell()])
        d['rule_version'] = '2026-09-24-old-rule'
        rc, out = self.verdict(d)
        self.assertEqual(rc, 0, out)
        self.assertIn('NOTE', out)
        self.assertIn('old-rule', out)

    def test_current_rule_report_needs_no_note(self):
        d = make_report([make_cell()])
        d['rule_version'] = m.RULE_VERSION
        rc, out = self.verdict(d)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('NOTE', out)


class TestReverdictArtifact(unittest.TestCase):
    """The retained multipump JSON predates the current rule and still stores
    obsolete off-cell rejoin(want=False) tags. Re-verdicting must produce a
    fresh artifact under the current rule_version while leaving the stored
    report byte-for-byte intact as historical evidence."""

    def stored(self):
        return {
            'rule_version': '2026-09-24-old',
            'source_save_unchanged': True,
            'uninterrupted_stable_across_syncs': True,
            'cells': [
                {'backend': 'heal', 'sync': 'off', 'rejoined': True,
                 'geometry': {'rewind_pump': 200},
                 'failures': ['rejoin(want=False,got=True)']},
                {'backend': 'heal', 'sync': 'both', 'rejoined': True,
                 'geometry': {'rewind_pump': 300}, 'failures': []},
            ],
        }

    def test_obsolete_tags_move_to_failures_as_stored(self):
        stored = self.stored()
        art = ir.reverdict_artifact(stored, 'stored.json', ['whatever'], [])
        self.assertEqual(art['rule_version'], ir.RULE_VERSION)
        self.assertEqual(
            art['derived_from']['stored_rule_version'], '2026-09-24-old')
        by_pump = {c['geometry']['rewind_pump']: c for c in art['cells']}
        self.assertEqual(by_pump[200]['failures_as_stored'],
                         ['rejoin(want=False,got=True)'])
        self.assertEqual(by_pump[200]['failures'], [])
        # The stored report is not mutated in memory either.
        self.assertEqual(stored['cells'][0]['failures'],
                         ['rejoin(want=False,got=True)'])

    def test_current_rule_mismatch_is_recomputed(self):
        stored = self.stored()
        stored['cells'][1]['rejoined'] = False
        art = ir.reverdict_artifact(stored, 'stored.json', [], [])
        self.assertEqual(art['cells'][1]['failures'],
                         ['rejoin(want=True,got=False)'])

    def test_verdict_existing_writes_artifact_without_touching_stored(self):
        import contextlib
        import io
        import tempfile
        stored = self.stored()
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / 'stored.json'
            op = Path(td) / 'current.json'
            sp.write_text(json.dumps(stored, indent=2))
            before = sp.read_text()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ir.verdict_existing(sp, op)
            self.assertEqual(sp.read_text(), before)
            art = json.loads(op.read_text())
            self.assertEqual(art['rule_version'], ir.RULE_VERSION)
            self.assertIn('recomputed_failures', art)
            self.assertIn('verdict', art)


class TestMatrixReverdictArtifact(unittest.TestCase):
    """phase_sync_matrix --verdict-json makes a stored matrix report
    unambiguous under the current rule without touching the stored file."""

    def test_matrix_artifact_recomputes_a_stale_expectation(self):
        c = make_cell(sync='save', backend='interp', rejoin=True)
        c['expect_rejoin'] = True  # what the withdrawn save+interp rule stored
        stored = make_report([make_cell(sync='both'), c])
        art = m.reverdict_artifact(stored, 'stored.json', [], [])
        self.assertEqual(art['rule_version'], m.RULE_VERSION)
        save_cell = [x for x in art['cells'] if x['sync'] == 'save'][0]
        self.assertIsNone(save_cell['expect_rejoin'])
        self.assertTrue(save_cell['expect_rejoin_as_stored'])
        self.assertEqual(save_cell['failures'], [])
        # ...and the stored report keeps its original claim.
        self.assertTrue(stored['cells'][1]['expect_rejoin'])

    def test_matrix_artifact_written_without_touching_stored(self):
        import contextlib
        import io
        import tempfile
        stored = make_report([make_cell(sync='both')])
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / 'stored.json'
            op = Path(td) / 'current.json'
            sp.write_text(json.dumps(stored, indent=2))
            before = sp.read_text()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.verdict_existing(sp, op)
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertEqual(sp.read_text(), before)
            art = json.loads(op.read_text())
            self.assertEqual(art['rule_version'], m.RULE_VERSION)
            self.assertIn('recomputed_failures', art)
            self.assertEqual(art['verdict'], 'MATRIX OK')


if __name__ == '__main__':
    unittest.main(verbosity=2)
