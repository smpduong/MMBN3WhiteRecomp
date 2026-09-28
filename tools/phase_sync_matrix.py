#!/usr/bin/env python3
"""Controlled A/B matrix for the opt-in save/load phase normalization.

Independent audit harness for GBARECOMP_SAVELOAD_PHASE_SYNC. It runs, from a
banked mid-battle state with identical inputs/state/build:

  U (uninterrupted): save slot 1 at pump 5, save it again at pump 9.
  R (restored):      save slot 1 at pump 5, load it at pump LOAD, save again
                     at pump LOAD+5.

Both branches reach the same guest frame/PC for their final save (asserted;
the pump->guest-frame offset after a load differs by the one re-presented
frame). The R branch replays the same guest timeline as U, so their final save
digest (frame, PC, guest-state digest) must be identical whenever the restore
is phase-exact.

Expected outcome per sync mode, as MEASURED on this route (see expect_rejoin
for the enforced rule; these are route-level results, not general claims):

  off                 -> FORKS at all three sampled load pumps 25/100/405
                        (negative control). The parallel in-memory sweep found
                        the fork to be boundary-DEPENDENT (1 of 3 restore
                        boundaries), so off/load are asserted in aggregate --
                        they must fork at least once -- not per pump.
  load                -> FORKS at all three sampled load pumps (control)
  save, pure interp   -> rejoined 3/3, but those cells were measured on a
                        STALLED guest (final save reports the boot frame, so
                        the guest advanced 0 frames). The claim is withdrawn;
                        save is measure-only on both backends.
  save, normal healing-> BOUNDARY DEPENDENT. It does NOT rejoin in general:
                        rejoined 1/3 in the 25/100/405 matrix and 2/7 in a
                        finer sweep. This mode is NOT established as safe and
                        is deliberately not asserted in either direction.
  both, interp or heal-> rejoins at every sampled load pump (3/3 and 6/6 in
                        the repeated matrix). The in-memory sweep found `both`
                        equivalent at 3 of 3 restore boundaries, so the
                        boundary-independence of `both` is asserted -- but only
                        3 boundaries have been sampled, so this is route-level
                        evidence, not a general guarantee.

The causal reading: the snapshot's device-state lag (save side) is a real
defect, but it is NOT the only one on this route. Under normal healing the
stale host event budget (load side) is also required, which is why
`save`-only is neither reliably sufficient nor asserted. See the docstring of
expect_rejoin and the run report for the per-cell reasoning.

Matrix axes:
  --syncs    off | save | load | both   (off = default; save/load isolate
             each half of the opt-in normalization)
  --backends interp (GBARECOMP_FORCE_INTERP=1) | heal (default healing)
  --pumps    one or more LOAD pumps

Per cell it records the observed assist events (asserted), pending/budget at
save and load, the two final save digests, and whether R rejoined U. It also
cross-checks that every uninterrupted (U) digest is identical across sync
modes -- the opt-in must not perturb uninterrupted execution.

Exit 0 only when every cell behaved as expected (rejoin where a rejoin is
asserted, fork for the off/load negative controls, U stable across modes,
digests stable across repeats); 1 otherwise. Cells whose expectation is
None (save-only under healing) are measured and reported but neither pass
nor fail on the rejoin bit. Private assets are used only via disposable
copies in an ignored run dir; the source save is hash-verified untouched.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Rule label recorded in every report and checked (as a printed note, not a
# gate) by --verdict, so a stored report cannot be mistaken for a current one.
# Same rule semantics as tools/inmem_rewind_check.py: aggregate negative
# controls, measured (not asserted) save-only, horizon assertion on load-bit
# cells, and structural checks on every cell.
RULE_VERSION = '2026-09-25-aggregate-negative-control'

SAVE_RE = re.compile(
    r'savestate_saved slot=(\d+) .*?pc=(0x[0-9a-f]+) frame=(\d+) '
    r'mem=([0-9a-f]+)')
LOAD_RE = re.compile(
    r'savestate_loaded slot=(\d+) .*?pc=(0x[0-9a-f]+) frame=(\d+) '
    r'mem=([0-9a-f]+)')
EVENT_RE = re.compile(r'assist_script_event pump=(\d+) action=(\S+)')
PENDING_RE = re.compile(r'savestate_pending pending=(\d+) budget=(-?\d+)')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_branch(tag, assist, sync, backend, frames, out, exe, state, trace,
               timeout):
    rom = out / 'rom.gba'
    shutil.copyfile(ROOT / 'roms/mmbn3_white_usa.gba', rom)
    sav = out / 'test.sav'
    shutil.copyfile(ROOT / 'saves/mmbn3_white_usa.sav', sav)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('GBARECOMP_')}
    env.update({
        'GBARECOMP_INPUT_REPLAY': str(trace),
        'GBARECOMP_FRAMEDUMP_COUNT': '0',
        'GBARECOMP_ASSIST_SCRIPT': assist,
        'GBARECOMP_HEAL_CACHE': str(out / 'cache'),
    })
    if sync != 'off':
        env['GBARECOMP_SAVELOAD_PHASE_SYNC'] = sync
    if backend == 'interp':
        # Pure interpreter: no native dispatch, no runtime self-heal.
        env['GBARECOMP_FORCE_INTERP'] = '1'
        env['GBARECOMP_SELFHEAL_RECOMPILE'] = '0'
        env['GBARECOMP_HEAL_WARM_LOAD'] = '0'
    else:
        # Normal healing: engine defaults (self-heal + warm load on).
        pass
    cmd = [str(exe), str(ROOT / 'game.toml'), '--window',
           '--frames', str(frames), '--rom', str(rom),
           '--bios', str(ROOT.parent / 'gbarecomp/bios/gba_bios.bin'),
           '--save', str(sav), '--load-state', str(state)]
    t0 = time.time()
    with (out / 'stdout.log').open('w') as so, \
            (out / 'stderr.log').open('w') as se:
        proc = subprocess.Popen(cmd, cwd=out, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=so, stderr=se)
        try:
            rc = proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            rc, timed_out = None, True
    text = (out / 'stdout.log').read_text(errors='replace')
    return {
        'tag': tag, 'sync': sync, 'backend': backend,
        'assist': assist, 'exit': rc, 'timed_out': timed_out,
        'seconds': round(time.time() - t0, 2),
        'events': [[int(p), a] for p, a in EVENT_RE.findall(text)],
        'saves': [[m[0], m[1], int(m[2]), m[3]]
                  for m in SAVE_RE.findall(text)],
        'loads': [[m[0], m[1], int(m[2]), m[3]]
                  for m in LOAD_RE.findall(text)],
        'pending': [[int(p), int(b)] for p, b in PENDING_RE.findall(text)],
        'presented': [int(x) for x in re.findall(r'frames_presented=(\d+)',
                                                 text)],
    }


def expect_events(res, want):
    got = [tuple(e) for e in res['events']]
    return got == [tuple(w) for w in want], got


def cell(load_pump, sync, backend, args, out_root):
    s1, s2 = 5, 9
    r1, r2 = 5, load_pump + 5
    frames = load_pump + args.margin
    out_u = Path(tempfile.mkdtemp(prefix=f'{sync}-{backend}-U{load_pump}-',
                                  dir=out_root))
    out_r = Path(tempfile.mkdtemp(prefix=f'{sync}-{backend}-R{load_pump}-',
                                  dir=out_root))
    u = run_branch('U', f'{s1}:save1;{s2}:save2', sync, backend, frames,
                   out_u, args.exe, args.state, args.trace, args.timeout)
    r = run_branch('R', f'{r1}:save1;{load_pump}:load1;{r2}:save2', sync,
                   backend, frames, out_r, args.exe, args.state, args.trace,
                   args.timeout)
    result = {'load_pump': load_pump, 'sync': sync, 'backend': backend,
              'frames': frames, 'U': u, 'R': r, 'dirs': [str(out_u),
                                                         str(out_r)]}
    # Observed assist events must match the requested script (pump numbers
    # alone are not evidence the action ran at that pump).
    ev_ok_u, got_u = expect_events(u, [(s1, 'save1'), (s2, 'save2')])
    ev_ok_r, got_r = expect_events(
        r, [(r1, 'save1'), (load_pump, 'load1'), (r2, 'save2')])
    result['events_ok'] = ev_ok_u and ev_ok_r
    result['events_seen'] = {'U': got_u, 'R': got_r}
    # The load event must actually appear (an R branch with no load is not a
    # restore test at all).
    result['load_event_observed'] = bool(r['loads']) and ev_ok_r
    # Save/load identity at the load boundary.
    ident_ok = False
    if r['saves'] and r['loads']:
        _, spc, sfr, smem = r['saves'][0]
        _, lpc, lfr, lmem = r['loads'][0]
        ident_ok = (spc, sfr, smem) == (lpc, lfr, lmem)
    result['boundary_identity'] = ident_ok
    # Final save digest comparison (frame + PC + guest digest).
    same_frame = pc_ok = False
    if len(u['saves']) >= 2 and len(r['saves']) >= 2:
        uf, rf = u['saves'][-1], r['saves'][-1]
        same_frame = uf[2] == rf[2]
        pc_ok = uf[1] == rf[1]
        result['U_final'] = uf
        result['R_final'] = rf
        result['rejoined'] = (uf[1], uf[2], uf[3]) == (rf[1], rf[2], rf[3])
        result['frame_match'] = same_frame and pc_ok
    else:
        result['rejoined'] = False
        result['frame_match'] = False
    # Post-load horizon invariant: iff the load-side reset is active, the
    # restored budget must equal the restored device's cycles-to-next-event
    # (U save budget + pending) and pending must be cleared. This is the
    # mechanism the whole audit reduces to, asserted directly.
    result['horizon_check'] = cell_horizon(result)
    result['ran_ok'] = (u['exit'] == 0 and r['exit'] == 0 and
                        not u['timed_out'] and not r['timed_out'] and
                        u['presented'] and r['presented'] and
                        u['presented'][-1] == frames and
                        r['presented'][-1] == frames)
    return result


def horizon_pinned(run_u, run_r):
    """Does the post-load horizon equal the restored cycles-to-next-event?

    The snapshot stores device state but not the host horizon. The restored
    device's true cycles-to-next-event is (U save budget + U save pending):
    device materialized point is `pending` behind the master clock, so its
    remaining horizon is that much larger than the host budget. When the
    load-side reset runs it re-arms the budget to exactly that value and
    drops the stale pending, so R's load-boundary (pending, budget) must be
    (0, restored_horizon). Returns (applicable, holds, restored, observed).
    """
    up, rp = run_u['pending'], run_r['pending']
    if len(up) < 2 or len(rp) < 3:
        return False, False, None, None
    u_save = up[1]            # U: [init-load, save1, save2]
    r_load = rp[2]            # R: [init-load, save1, load1, save2]
    restored = u_save[1] + u_save[0]
    holds = (r_load[0] == 0 and r_load[1] == restored)
    return True, holds, {'pending': u_save[0], 'budget': u_save[1],
                         'restored_horizon': restored}, r_load


def horizon_expected(sync):
    # The load-side reset runs only when sync includes the load bit, so the
    # post-load budget must be pinned to the restored horizon exactly then.
    return sync in ('load', 'both')


def cell_horizon(c):
    """Normalized horizon check for one cell, whether stored or recomputed.

    Reports written before the horizon assertion existed carry no
    'horizon_check' key. Those are recomputed from the cell's stored
    save/load pending arrays so an offline re-verdict applies the same rule as
    a live run. 'applicable' False means the report does not hold enough
    pending samples to decide: UNVERIFIABLE, which is reported as such and is
    never treated as a pass.
    """
    h = c.get('horizon_check')
    if h is not None:
        h = dict(h)
        h.setdefault('applicable', True)
        h['source'] = 'stored'
        return h
    applicable, holds, restored, observed = horizon_pinned(c['U'], c['R'])
    return {'applicable': applicable, 'holds': holds, 'restored': restored,
            'observed': observed, 'source': 'recomputed'}


def cell_failures(c):
    """Failure tags for one cell.

    SHARED by the live and --verdict paths so the two verdict standards cannot
    drift apart (an earlier version gated the structural checks only on cells
    that had a rejoin expectation, which let a save+heal cell that never ran
    pass the offline verdict).

      structural -- the run completed, the requested assist script actually
                    ran, the load was really observed, the load boundary
                    identity holds, and both branches reached the same final
                    frame/PC. Required for EVERY cell.
      rejoin     -- asserted only where expect_rejoin() is not None. A
                    save+heal cell is measured and reported, never asserted:
                    save-only does not reliably rejoin under normal healing.
      horizon    -- asserted only where the load-side reset actually runs. An
                    unverifiable horizon (legacy report, not enough pending
                    samples) fails loudly rather than passing vacuously.
    """
    fails = [key for key in ('ran_ok', 'events_ok', 'load_event_observed',
                             'boundary_identity', 'frame_match')
             if not c.get(key)]
    want = expect_rejoin(c['sync'], c['backend'])
    if want is not None and want != c.get('rejoined'):
        fails.append(f'rejoin(want={want},got={c.get("rejoined")})')
    if horizon_expected(c['sync']):
        h = cell_horizon(c)
        if not h.get('applicable'):
            fails.append('horizon-unverifiable')
        elif not h.get('holds'):
            fails.append('horizon')
    return fails


def report_failures(d):
    """Failure tags for a whole report. SHARED by the live and --verdict paths.

    All three report-level gates are required for MATRIX OK in both paths:
    an unverified source save, an opt-in that perturbed uninterrupted
    execution, or digests that do not reproduce across repeats each
    invalidate every cell measured under those conditions. A report that
    predates a gate key is reported MISSING and fails, since the gate cannot
    be verified from the artifact.
    """
    fails = []
    for c in d['cells']:
        tag = f"{c['backend']}/sync={c['sync']}/load={c['load_pump']}"
        fails += [f'{tag}/{f}' for f in cell_failures(c)]
    for key in ('source_save_unchanged', 'uninterrupted_stable_across_syncs',
                'repeat_stable'):
        if not d.get(key):
            fails.append(key)
    agg, _ = negative_control_rules(d['cells'])
    fails += agg
    return fails


def expect_rejoin(sync, backend):
    """The ENFORCED per-cell rejoin expectation, per (sync, backend).

    Returns True to assert the rejoin bit, or None to measure and report it
    without asserting in either direction. (The `backend` argument is
    retained for report compatibility; the rule no longer depends on it --
    see below.)

    Corrected 2026-09-25 after an in-memory sweep showed the defect is
    BOUNDARY-DEPENDENT, and after the pure-interpreter cells were found to
    have been measured on a stalled guest (their final saves all report the
    boot frame, so "the guest advanced 0 frames" -- see the AUDIO_REVIEW
    addenda):

      both      -> True, asserted at EVERY sampled load pump. The opt-in's
                   value is that the fix is not itself boundary-dependent;
                   one pump where it fails is a real defect.
      off, load -> None per cell. These are negative controls and are
                   asserted in AGGREGATE (see negative_control_rules): a
                   negative control must fork at least once across the
                   sampled pumps, or the suite cannot demonstrate the defect
                   and its passes are vacuous. Per-cell "must fork" wording
                   breaks on a boundary that happens to rejoin.
      save      -> None. Measured 1/3 and 2/7 under normal healing, so
                   neither True nor False is honest. The former save+interp
                   True assertion is WITHDRAWN: those cells came from the
                   stalled-guest interp runs, so they cannot carry a rejoin
                   claim.
    """
    return True if sync == 'both' else None


def negative_control_rules(cells):
    """Aggregate rules for the negative controls, SHARED by the live and
    --verdict paths.

    Each of `off` and `load` must fork at least one sampled cell. That is the
    minimum bar for a non-vacuous negative control: it proves the harness can
    still expose the defect on at least one boundary. A control that rejoins
    everywhere is not a pass -- it is an uninformative run.
    """
    bad, notes = [], []
    for sync in ('off', 'load'):
        group = [c for c in cells if c['sync'] == sync]
        if not group:
            continue
        # A control "forked" when its restored branch did NOT rejoin the
        # uninterrupted one.
        forked = sorted({c['load_pump'] for c in group if not c['rejoined']})
        notes.append(f'{sync}: forked at {forked} of {len(group)} cells')
        if not forked:
            bad.append(f'negative-control-{sync}-vacuous')
    return bad, notes


def reverdict_artifact(d, path, bad, notes):
    """Copy of a stored report re-verdicted under the CURRENT rule.

    The stored file is left untouched as historical evidence. Per cell this
    recomputes the enforced rejoin expectation (an older rule asserted
    `save`+interp), the horizon check (legacy reports carry none) and the
    current failure tags, so the artifact cannot be read as if the old rule
    were still live. `*_as_stored` fields preserve what the run originally
    recorded.
    """
    art = dict(d)
    art['rule_version'] = RULE_VERSION
    art['derived_from'] = {'path': str(path),
                           'stored_rule_version': d.get('rule_version')}
    art['note'] = ('Re-verdict of a stored run under the current rule. '
                   'The stored report is kept unchanged as historical '
                   'evidence; rule-derived verdicts here supersede it.')
    art['recomputed_failures'] = bad
    art['control_notes'] = notes
    art['verdict'] = 'MATRIX OK' if not bad else 'MATRIX FAIL'
    cells = []
    for c in d['cells']:
        c2 = dict(c)
        c2['expect_rejoin_as_stored'] = c.get('expect_rejoin')
        c2['expect_rejoin'] = expect_rejoin(c['sync'], c['backend'])
        c2['horizon_check'] = cell_horizon(c)
        c2['failures'] = cell_failures(c)
        cells.append(c2)
    art['cells'] = cells
    return art


def verdict_existing(path, out_path=None):
    d = json.loads(Path(path).read_text())
    for c in d['cells']:
        e = expect_rejoin(c['sync'], c['backend'])
        h = cell_horizon(c)
        tag = f"{c['backend']}/sync={c['sync']}/load={c['load_pump']}"
        shown = ('unverifiable' if not h.get('applicable')
                 else str(h['holds']))
        print(f"{tag:34} rejoin={c['rejoined']} expect={e} "
              f"horizon={shown} ({h['source']}) {h['restored']}")
    bad = report_failures(d)
    def show(key):
        v = d.get(key)
        return 'MISSING' if v is None else v
    print(f"source save unchanged: {show('source_save_unchanged')}")
    print(f"uninterrupted stable across syncs: "
          f"{show('uninterrupted_stable_across_syncs')}")
    print(f"repeat stable: {show('repeat_stable')}")
    _, notes = negative_control_rules(d['cells'])
    for n in notes:
        print(f'control rule: {n}')
    if d.get('rule_version') != RULE_VERSION:
        print(f'NOTE: report was written under rule '
              f'{d.get("rule_version", "UNKNOWN")!r}; this tool enforces '
              f'{RULE_VERSION!r}. Rule-derived verdicts were recomputed.')
    ok = not bad
    print('MATRIX OK' if ok else f'MATRIX FAIL {bad}')
    if out_path:
        art = reverdict_artifact(d, path, bad, notes)
        Path(out_path).write_text(json.dumps(art, indent=2) + '\n')
        print(f're-verdict artifact written: {out_path} '
              f'(rule {RULE_VERSION}; source report kept unchanged)')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--exe', type=Path, default=ROOT / 'build/MMBN3WhiteRecomp')
    ap.add_argument('--state', type=Path,
                    default=ROOT / 'build/winreplay/gate2-battleI-action/'
                                     'rom.state1')
    ap.add_argument('--trace', type=Path,
                    default=ROOT / 'build/winreplay/gate2-battleJ-action2/'
                                     'trace.csv')
    ap.add_argument('--syncs', nargs='+',
                    default=['off', 'save', 'load', 'both'],
                    choices=['off', 'save', 'load', 'both'])
    ap.add_argument('--backends', nargs='+', default=['interp'],
                    choices=['interp', 'heal'])
    ap.add_argument('--pumps', nargs='+', type=int, default=[25, 100, 405])
    ap.add_argument('--margin', type=int, default=430,
                    help='frames to run past the load pump (>400 post-load)')
    ap.add_argument('--timeout', type=float, default=300)
    ap.add_argument('--repeat', type=int, default=1,
                    help='run the whole matrix N times and require every '
                         'cell digest to be stable across repeats')
    ap.add_argument('--json', type=Path, default=None)
    ap.add_argument('--verdict', type=Path, default=None,
                    help='do not run; re-verdict an existing --json report '
                         'with the current expectation rule')
    ap.add_argument('--verdict-json', type=Path, default=None,
                    help='with --verdict: also write a CURRENT-RULE copy of '
                         'the report (the stored file is never modified)')
    args = ap.parse_args()

    if args.verdict:
        return verdict_existing(args.verdict, args.verdict_json)
    if not args.state.exists() or not args.trace.exists():
        print(f'SKIP: missing fixture {args.state} / {args.trace}')
        return 1
    source = ROOT / 'saves/mmbn3_white_usa.sav'
    before = sha(source)
    out_root = ROOT / 'build/audio-restore'
    out_root.mkdir(parents=True, exist_ok=True)
    cells = []
    repeats = []
    for rep in range(max(1, args.repeat)):
        rep_start = len(cells)
        for backend in args.backends:
            for sync in args.syncs:
                for pump in args.pumps:
                    c = cell(pump, sync, backend, args, out_root)
                    c['repeat'] = rep
                    c['expect_rejoin'] = expect_rejoin(sync, backend)
                    cells.append(c)
                    uf = c.get('U_final', ['?', '?', '?', '?'])
                    rf = c.get('R_final', ['?', '?', '?', '?'])
                    print(f"[rep{rep} {backend} sync={sync} load={pump}] "
                          f"rejoin={c['rejoined']} "
                          f"expect={c['expect_rejoin']} "
                          f"frame_match={c['frame_match']} "
                          f"ran={c['ran_ok']} "
                          f"U={uf[1]}/{uf[2]}/{uf[3]} "
                          f"R={rf[1]}/{rf[2]}/{rf[3]} "
                          f"horizon={c['horizon_check']['holds']}"
                          f"{c['horizon_check']['restored']} "
                          f"fails={cell_failures(c)}",
                          flush=True)
        repeats.append([(c['backend'], c['sync'], c['load_pump'],
                         tuple(c.get('U_final', [])),
                         tuple(c.get('R_final', [])))
                        for c in cells[rep_start:]])
    src_ok = sha(source) == before
    # Uninterrupted (U) must be identical across sync modes for the same
    # backend+pump: the opt-in must not perturb uninterrupted execution.
    u_stable, u_mismatch = True, []
    seen = {}
    for c in cells:
        key = (c['backend'], c['load_pump'])
        sig = tuple(c.get('U_final', []))
        if key in seen and seen[key] != sig:
            u_stable = False
            u_mismatch.append((key, seen[key], sig))
        seen[key] = sig
    # Digest stability across repeats (same config must reproduce).
    repeat_stable = all(r == repeats[0] for r in repeats) if repeats else True
    report = {'cells': cells, 'source_save_unchanged': src_ok,
              'uninterrupted_stable_across_syncs': u_stable,
              'uninterrupted_mismatches': u_mismatch,
              'repeat_stable': repeat_stable,
              'rule_version': RULE_VERSION,
              'binary_sha256': sha(args.exe)}
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + '\n')
    print(f'\nsource save unchanged: {src_ok}')
    print(f'uninterrupted stable across sync modes: {u_stable}')
    print(f'repeat stable: {repeat_stable}')
    # Identical verdict standard to --verdict: re-verdict the report we just
    # wrote, so a live run and a later offline re-verdict of the same JSON
    # cannot disagree about the same evidence.
    bad = report_failures(report)
    ok = not bad
    _, notes = negative_control_rules(report['cells'])
    for n in notes:
        print(f'control rule: {n}')
    print('MATRIX OK' if ok else f'MATRIX FAIL {bad}')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
