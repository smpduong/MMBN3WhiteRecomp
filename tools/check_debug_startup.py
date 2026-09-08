#!/usr/bin/env python3
"""Bounded debugger startup/stepping check; NOT an audio latency measurement.
Uses an isolated save and preserves complete logs. Cache preload is bypassed
by default; --warm-cache tests normal preload. Existing cache is never removed.
"""
import argparse
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import time
from _probe_common import Client

ROOT = pathlib.Path(__file__).resolve().parent.parent

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--warm-cache', action='store_true')
    ap.add_argument('--timeout', type=float, default=30)
    args = ap.parse_args()
    runs = ROOT / 'build' / 'runs'
    runs.mkdir(parents=True, exist_ok=True)
    out = pathlib.Path(tempfile.mkdtemp(prefix='debug-startup-', dir=runs))
    env = os.environ.copy()
    env['GBARECOMP_HEAL_WARM_LOAD'] = '1' if args.warm_cache else '0'
    # Also isolate any new on-demand compiled shards in bypass mode.
    if not args.warm_cache:
        env['GBARECOMP_HEAL_CACHE'] = str(out / 'cache')
    else:
        env['GBARECOMP_HEAL_CACHE'] = str(ROOT / 'recomp_cache')
    save = ROOT / 'saves' / 'mmbn3_white_usa.sav'
    before = hashlib.sha256(save.read_bytes()).hexdigest() if save.exists() else None
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    cmd = [str(ROOT / 'build/MMBN3WhiteRecomp'), str(ROOT / 'game.toml'), '--no-window', '--tcp', str(port),
           '--rom', str(ROOT / 'roms/mmbn3_white_usa.gba'),
           '--bios', str(ROOT.parent / 'gbarecomp/bios/gba_bios.bin'),
           '--save', str(out / 'test.sav')]
    (out / 'command.json').write_text(json.dumps({'argv': cmd, 'warm_cache': args.warm_cache}, indent=2))
    result = {'ok': False, 'warm_cache': args.warm_cache}
    client = None
    with open(out / 'stdout.log', 'wb') as stdout, open(out / 'stderr.log', 'wb') as stderr:
        start = time.monotonic()
        proc = subprocess.Popen(cmd, cwd=out, env=env, stdout=stdout, stderr=stderr)
        try:
            client = Client(port, proc, timeout=args.timeout)
            result['connect_seconds'] = round(time.monotonic() - start, 3)
            result['before'] = client.call(cmd='run_status')
            for _ in range(3):
                reply = client.call(cmd='step')
                if not reply.get('ok'):
                    raise RuntimeError(f'step failed: {reply}')
            result['after'] = client.call(cmd='run_status')
            result['ok'] = result['after'].get('frame', 0) > result['before'].get('frame', 0)
            result['screenshot_sha256'] = client.save_screenshot(out / 'frame.ppm')
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            if client:
                client.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            result['exit_code'] = proc.returncode
            after = hashlib.sha256(save.read_bytes()).hexdigest() if save.exists() else None
            result['personal_save_unchanged'] = before == after
            result['ok'] = result['ok'] and before == after and proc.returncode == 0
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(out)
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
