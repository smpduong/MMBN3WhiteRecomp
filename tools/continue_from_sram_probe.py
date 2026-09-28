#!/usr/bin/env python3
"""Real in-game Continue from a game-written SRAM (bounded, disposable).

Closes the gap left by the earlier headless file-import run: importing a
game-written 32 KiB SRAM and quitting after 60 BIOS frames proves emulator
file I/O, not an in-game Continue. This probe loads a disposable copy of a
GAME-WRITTEN SRAM, launches the game windowed with TCP, and drives the real
title -> menu -> Continue flow (shared `drive_to_net`), which only passes if
the gameplay scene actually verifies (cart PC running, frames advancing,
scene identity). It fails loudly if Continue never enters gameplay.

Source SRAM is hashed before/after and never written; the run uses a
disposable copy under an ignored run dir. Evidence (stdout/stderr, shots,
results.json) stays local.

Exit 0 = in-game Continue verified; 1 = not verified; 2 = setup/launch error.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtrip_check import (Client, Fail, drive_to_net, free_port,  # noqa: E402
                             sha_hex)

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--sav', type=Path, required=True,
                    help='game-written SRAM to load (read-only; copied)')
    ap.add_argument('--label', default='continue')
    ap.add_argument('--out', type=Path, default=None)
    ap.add_argument('--timeout', type=float, default=360)
    args = ap.parse_args()
    args.sav = args.sav.resolve()
    if not args.sav.exists():
        print(f'SKIP: SRAM missing: {args.sav}')
        return 2
    parent = ROOT / 'build' / 'sram-continue'
    parent.mkdir(parents=True, exist_ok=True)
    out = (args.out or (parent / f"{args.label}-"
                        f"{time.strftime('%Y%m%d-%H%M%S')}")).resolve()
    out.mkdir(parents=True, exist_ok=False)
    src_before = sha_hex(args.sav)
    test_sav = out / 'test.sav'
    shutil.copyfile(args.sav, test_sav)
    shutil.copyfile(ROOT / 'roms/mmbn3_white_usa.gba', out / 'rom.gba')
    log = (out / 'driver.log').open('w')

    def note(msg):
        print(msg, flush=True)
        log.write(msg + '\n')
        log.flush()

    port = free_port()
    cmd = [str(ROOT / 'build' / 'MMBN3WhiteRecomp'), str(ROOT / 'game.toml'),
           '--save', str(test_sav), '--rom', str(out / 'rom.gba'),
           '--bios', str(ROOT.parent / 'gbarecomp/bios/gba_bios.bin'),
           '--tcp', str(port)]
    env = dict(os.environ)
    env['GBARECOMP_HEAL_CACHE'] = str(out / 'heal-cache')
    (out / 'command.json').write_text(json.dumps({'argv': cmd, 'port': port},
                                                 indent=1))
    so = (out / 'stdout.log').open('wb')
    se = (out / 'stderr.log').open('wb')
    proc = subprocess.Popen(cmd, cwd=out, env=env, stdin=subprocess.DEVNULL,
                            stdout=so, stderr=se)
    note(f'[{args.label}] pid={proc.pid} port={port}')
    result = {'argv': cmd, 'sram_source': str(args.sav),
              'sram_source_sha256_before': src_before,
              'test_sav_sha256': sha_hex(test_sav)}
    ok, detail = False, ''
    client = None
    try:
        client = Client(port, proc, timeout=120.0)
        menu_h, p1_h, net = drive_to_net(client, out, proc, args.label, note)
        result.update({'menu_hash': menu_h, 'p1_hash': p1_h, 'net': net,
                       'gameplay_entered': True})
        st = client.status()
        result['status_after_continue'] = st
        ok = True
    except Fail as exc:
        detail = str(exc)
        note(f'[{args.label}] FAIL: {detail}')
    except Exception as exc:  # noqa: BLE001
        detail = f'{type(exc).__name__}: {exc}'
        note(f'[{args.label}] ERROR: {detail}')
    # Clean quit (child PID only); forced end is recorded, not hidden.
    exit_code, forced = None, False
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    t_end = time.monotonic() + 120
    while time.monotonic() < t_end:
        exit_code = proc.poll()
        if exit_code is not None:
            break
        time.sleep(3)
    if exit_code is None:
        proc.kill()
        exit_code = proc.wait(timeout=10)
        forced = True
    so.close()
    se.close()
    log.close()
    text = (out / 'stdout.log').read_text(errors='replace')
    result.update({'play_ok': ok, 'detail': detail, 'exit': exit_code,
                   'forced': forced,
                   'sram_source_sha256_after': sha_hex(args.sav),
                   'sram_source_unchanged': sha_hex(args.sav) == src_before})
    for key, pat in (('frames_presented', r'frames_presented=(\d+)'),
                     ('ppu_frames', r'ppu_frames=(\d+)'),
                     ('final_pc', r'final_pc=(\S+)')):
        m = re.findall(pat, text)
        if m:
            result[key] = m[-1]
    (out / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    if not result['sram_source_unchanged']:
        print('FAIL: source SRAM changed')
        return 1
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
