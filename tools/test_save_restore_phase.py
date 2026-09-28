#!/usr/bin/env python3
"""Narrow regression test: save/load phase normalization (live, ~10 s).

Runs four short branches from a banked battle state with identical inputs:

  U-on : save pump 5, save pump 9   (phase sync ON)
  R-on : save pump 5, load pump 25, save pump 30   (phase sync ON)
  U-off: same as U-on with the opt-in OFF            (uninterrupted control)
  R-off: same as R-on with the opt-in OFF            (no-sync negative control)

Assertions (all must hold; exit 0 only then):
  1. Observed `assist_script_event` lines match the requested pump/action
     script for every branch (pump numbers alone are not proof the action
     ran at that pump).
  2. The load event is actually observed, and its save/load boundary identity
     (frame, PC, guest-memory digest) matches.
  3. With the opt-in ON, the restored branch's final save digest equals the
     uninterrupted branch's (it rejoins).
  4. Without the opt-in, the restored branch FORKS (negative control): the
     script must be able to show the bug it claims to fix.
  5. The opt-in leaves uninterrupted execution unchanged (U-on == U-off).

Uses disposable ROM/save copies in an ignored run dir; the source save is
hash-verified untouched. This one short U/R pair is a smoke test, not the
full matrix: `tools/phase_sync_matrix.py` covers pump 25/100/405, pure
interpreter vs normal healing, and >=400 post-load frames.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SAVE_RE = re.compile(
    r'savestate_saved slot=(\d+) .*?pc=(0x[0-9a-f]+) frame=(\d+) '
    r'mem=([0-9a-f]+)')
LOAD_RE = re.compile(
    r'savestate_loaded slot=(\d+) .*?pc=(0x[0-9a-f]+) frame=(\d+) '
    r'mem=([0-9a-f]+)')
EVENT_RE = re.compile(r'assist_script_event pump=(\d+) action=(\S+)')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_branch(tag, assist, frames, out, phase_sync):
    exe = ROOT / 'build/MMBN3WhiteRecomp'
    rom = out / 'rom.gba'
    shutil.copyfile(ROOT / 'roms/mmbn3_white_usa.gba', rom)
    sav = out / 'test.sav'
    shutil.copyfile(ROOT / 'saves/mmbn3_white_usa.sav', sav)
    state = ROOT / 'build/winreplay/gate2-battleI-action/rom.state1'
    trace = ROOT / 'build/winreplay/gate2-battleJ-action2/trace.csv'
    for p in (state, trace):
        if not p.exists():
            print(f'SKIP: fixture missing: {p}')
            return None
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('GBARECOMP_')}
    env.update({
        'GBARECOMP_SELFHEAL_RECOMPILE': '0',
        'GBARECOMP_HEAL_WARM_LOAD': '0',
        'GBARECOMP_INPUT_REPLAY': str(trace),
        'GBARECOMP_FRAMEDUMP_COUNT': '0',
        'GBARECOMP_ASSIST_SCRIPT': assist,
        'GBARECOMP_HEAL_CACHE': str(out / 'cache'),
    })
    if phase_sync:
        env['GBARECOMP_SAVELOAD_PHASE_SYNC'] = '1'
    cmd = [str(exe), str(ROOT / 'game.toml'), '--window',
           '--frames', str(frames), '--rom', str(rom),
           '--bios', str(ROOT.parent / 'gbarecomp/bios/gba_bios.bin'),
           '--save', str(sav), '--load-state', str(state)]
    with (out / 'stdout.log').open('w') as so, \
            (out / 'stderr.log').open('w') as se:
        proc = subprocess.Popen(cmd, cwd=out, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=so, stderr=se)
        try:
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            print(f'FAIL [{tag}]: timed out')
            return None
    if proc.returncode != 0:
        print(f'FAIL [{tag}]: exit {proc.returncode}')
        return None
    text = (out / 'stdout.log').read_text(errors='replace')
    return {
        'tag': tag, 'phase_sync': phase_sync,
        'saves': [[m[0], m[1], int(m[2]), m[3]]
                  for m in SAVE_RE.findall(text)],
        'loads': [[m[0], m[1], int(m[2]), m[3]]
                  for m in LOAD_RE.findall(text)],
        'events': [[int(p), a] for p, a in EVENT_RE.findall(text)],
    }


def main():
    t0 = time.time()
    source = ROOT / 'saves/mmbn3_white_usa.sav'
    before = sha(source)
    parent = ROOT / 'build/audio-restore'
    parent.mkdir(parents=True, exist_ok=True)
    dirs = {k: Path(tempfile.mkdtemp(prefix=f'phase-{k}-', dir=parent))
            for k in ('U-on', 'R-on', 'U-off', 'R-off')}
    u_on = run_branch('U-on', '5:save1;9:save2', 60, dirs['U-on'], True)
    r_on = run_branch('R-on', '5:save1;25:load1;30:save2', 60, dirs['R-on'],
                      True)
    u_off = run_branch('U-off', '5:save1;9:save2', 60, dirs['U-off'], False)
    r_off = run_branch('R-off', '5:save1;25:load1;30:save2', 60,
                       dirs['R-off'], False)
    runs = {'U-on': u_on, 'R-on': r_on, 'U-off': u_off, 'R-off': r_off}
    ok_src = sha(source) == before
    print(f'source save unchanged: {ok_src}')
    if any(v is None for v in runs.values()):
        print('FAIL: a branch run failed (see run dirs)')
        return 1

    failures = []
    # 1. Observed assist events match the requested script.
    want = {'U-on': [(5, 'save1'), (9, 'save2')],
            'U-off': [(5, 'save1'), (9, 'save2')],
            'R-on': [(5, 'save1'), (25, 'load1'), (30, 'save2')],
            'R-off': [(5, 'save1'), (25, 'load1'), (30, 'save2')]}
    for tag, run in runs.items():
        got = [tuple(e) for e in run['events']]
        if got != [tuple(w) for w in want[tag]]:
            failures.append(f'{tag}: assist events {got} != {want[tag]}')

    # 2. Load event observed with matching boundary identity.
    for tag in ('R-on', 'R-off'):
        run = runs[tag]
        if not run['loads']:
            failures.append(f'{tag}: no savestate_loaded observed')
            continue
        s = run['saves'][0]
        l = run['loads'][0]
        if (s[1], s[2], s[3]) != (l[1], l[2], l[3]):
            failures.append(f'{tag}: boundary identity {s} != {l}')

    # 3/4. Rejoin with the opt-in, fork without it (negative control).
    uf_on = runs['U-on']['saves'][-1]
    rf_on = runs['R-on']['saves'][-1]
    rf_off = runs['R-off']['saves'][-1]
    uf_off = runs['U-off']['saves'][-1]
    print(f"U-on  frame={uf_on[2]} pc={uf_on[1]} mem={uf_on[3]}")
    print(f"R-on  frame={rf_on[2]} pc={rf_on[1]} mem={rf_on[3]}")
    print(f"R-off frame={rf_off[2]} pc={rf_off[1]} mem={rf_off[3]}")
    rejoin_on = uf_on[1:] == rf_on[1:]
    fork_off = uf_off[1:] != rf_off[1:]
    print('restored branch rejoined (sync on):', rejoin_on)
    print('restored branch forked (sync off, negative control):', fork_off)
    if not rejoin_on:
        failures.append('R-on did not rejoin U-on (normalization failed)')
    if not fork_off:
        failures.append('R-off did not fork (negative control vacuous)')
    # 5. Uninterrupted execution unchanged by the opt-in.
    u_stable = uf_on[1:] == uf_off[1:]
    print('uninterrupted unchanged (U-on == U-off):', u_stable)
    if not u_stable:
        failures.append('opt-in perturbed uninterrupted execution')

    if not ok_src:
        failures.append('source save changed')
    print(f'elapsed {time.time() - t0:.0f}s dirs: '
          + ' '.join(str(d) for d in dirs.values()))
    if failures:
        for f in failures:
            print('FAIL:', f)
        return 1
    print('PASS: phase normalization rejoins; no-sync fork control holds')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
