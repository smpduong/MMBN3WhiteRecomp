#!/usr/bin/env python3
"""Bounded windowed replay with separate source/output WAVs and isolated saves.

The output WAV is the SDL callback stream, not a microphone/device recording.
Raw evidence stays in ignored build/audio-review/. Requires engine capture support.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parent.parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--replay', required=True, type=Path)
    parser.add_argument('--exe', type=Path, default=ROOT / 'build/MMBN3WhiteRecomp')
    parser.add_argument('--save', type=Path, default=ROOT / 'saves/mmbn3_white_usa.sav')
    parser.add_argument('--frames', type=int, default=8500)
    parser.add_argument('--timeout', type=float, default=220)
    parser.add_argument('--target-ms', type=float, default=40,
                        help='bridge target, 25..100 ms (production default 40)')
    args = parser.parse_args()
    if not 1 <= args.frames <= 12000:
        parser.error('frames must be 1..12000 (capture storage is bounded to 240 seconds)')
    if not 25 <= args.target_ms <= 100:
        parser.error('target-ms must be 25..100')
    parent = ROOT / 'build/audio-review'
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=Path(args.label).name + '-', dir=parent))
    source_save = args.save.resolve()
    before = sha(source_save)
    shutil.copyfile(source_save, out / 'initial.sav')
    shutil.copyfile(out / 'initial.sav', out / 'play.sav')
    shutil.copyfile(args.replay, out / 'inputs.csv')
    if sha(out / 'initial.sav') != before or sha(source_save) != before:
        raise RuntimeError('source save changed while preparing capture')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GBARECOMP_')}
    overrides = {
        'GBARECOMP_INPUT_REPLAY': str(out / 'inputs.csv'),
        'GBARECOMP_HEAL_CACHE': str(out / 'heal-cache'),
        'GBARECOMP_HEAL_WARM_LOAD': '0',
        'GBARECOMP_AUDIO_PROBE': '1',
        'GBARECOMP_AUDIO_CAPTURE': str(out / 'audio'),
        'GBARECOMP_AUDIO_TARGET_MS': str(args.target_ms),
        'GBARECOMP_FRAME_PHASE': str(out / 'frame-phase.csv'),
        'GBARECOMP_FRAMEDUMP_DIR': str(out),
        'GBARECOMP_FRAMEDUMP_START': str(args.frames),
        'GBARECOMP_FRAMEDUMP_COUNT': '1',
    }
    env.update(overrides)
    cmd = [str(args.exe.resolve()), str(ROOT / 'game.toml'), '--window',
           '--frames', str(args.frames), '--rom', str(ROOT / 'roms/mmbn3_white_usa.gba'),
           '--bios', str(ROOT.parent / 'gbarecomp/bios/gba_bios.bin'),
           '--save', str(out / 'play.sav')]
    result = {'command': cmd, 'environment': overrides, 'source_save_sha256': before,
              'binary_sha256': sha(args.exe), 'input_sha256': sha(out / 'inputs.csv'),
              'requested_frames': args.frames, 'started': time.time(),
              'limitations': ['Output WAV ends before SDL/device conversion.',
                              'Replay reproduces guest inputs, not original host scheduling.',
                              'Capture instrumentation can affect scheduling.']}
    (out / 'session.json').write_text(json.dumps(result, indent=2) + '\n')
    print(out, flush=True)
    with (out / 'stdout.log').open('w') as stdout, (out / 'stderr.log').open('w') as stderr:
        proc = subprocess.Popen(cmd, cwd=out, env=env, stdout=stdout, stderr=stderr)
        try:
            result['exit_code'] = proc.wait(timeout=args.timeout)
            result['timed_out'] = False
        except subprocess.TimeoutExpired:
            result['timed_out'] = True
            proc.terminate()
            try:
                result['exit_code'] = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                result['exit_code'] = proc.wait()
    result['ended'] = time.time()
    result['source_save_unchanged'] = sha(source_save) == before
    result['audio'] = {}
    for kind in ('source', 'output'):
        path = out / f'audio-{kind}.wav'
        if path.exists():
            with wave.open(str(path)) as wav:
                result['audio'][kind] = {'frames': wav.getnframes(), 'rate': wav.getframerate(),
                                        'seconds': wav.getnframes() / wav.getframerate(),
                                        'sha256': sha(path)}
    log = (out / 'stderr.log').read_text(errors='replace')
    stdout = (out / 'stdout.log').read_text(errors='replace')
    counts = re.findall(r'frames_presented=(\d+)', stdout)
    result['presented_frames'] = int(counts[-1]) if counts else None
    result['coverage'] = next((line for line in reversed(stdout.splitlines())
                               if line.startswith('self_heal_coverage=')), None)
    result['capture_complete'] = 'ok=1 truncated=0' in log
    result['ok'] = (result['exit_code'] == 0 and not result['timed_out']
                    and result['source_save_unchanged'] and result['capture_complete']
                    and result['presented_frames'] == args.frames)
    (out / 'session.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
