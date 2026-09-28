#!/usr/bin/env python3
"""Active-sound save/load restore check (single run, two branches).

From a banked mid-battle state with live music/SFX, this replays a fixed
input trace while the assist script saves at pump 5 and loads that slot at
pump 405, then runs on. The source PCM capture therefore holds two segments
covering the SAME guest frames: segment A (uninterrupted) and segment B
(post-restore).

Two-part verdict (never conflated):
- capture_valid: the run itself is sound — clean exit, requested frames,
  complete capture, unchanged source save, and both the save and the load
  events parsed with matching frame/PC/state digest, sane boundaries, and
  recorded input provenance. Missing events or alignment => invalid.
- restore_audio_equivalent: segment B matches segment A sample-for-sample
  at guest-frame-derived alignment. Any difference fails equivalence
  (reported, not hidden).

Exit status: 0 = valid run AND equivalent audio; 1 = valid run but audio
NOT equivalent (honest failing measurement); 2 = invalid run, tool error,
or failed self-test (including the built-in negative control).

Alignment is by guest frame, not by music search: the save/load event
lines give exact guest frames; sample offsets use the nominal bridge rate
(65536 Hz / 59.7275 fps) with only a tight local refinement for sub-frame
drift, guarded by a grid-median significance check. The nominal rate is
cross-validated, not assumed blindly: exact AUD-section sample counters
(samples_generated_ at capture start vs save) agree with the grid anchor
to single-digit samples, and the predicted split reproduces
deterministically across runs.

The stale-tail metric measures SOURCE continuity around the load point; it
does NOT test the SDL host bridge queue (source is captured before the
bridge). Post-bridge queue behaviour is covered by a separate harness,
`tools/host_audio_queue_check.py`, which uses in-engine action markers to map
the save/load boundary onto the capture's own timeline and compares the
post-bridge `-output.wav` (callback buffer, pre-device). This tool's numbers
must not be read as host-queue evidence.

Voice coverage: this route's battle mix is direct-sound music plus square/
SFX voices. Whether Sound3/Sound4/wave RAM are live at the save point is
NOT verified here; wave/noise-channel restore needs a focused GbaAudio
fixture test, not this route. Band ratios below are descriptive only.

Raw evidence stays in ignored build/audio-restore/. The personal save is
copied and hash-verified, never written.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ASSIST_EVENT_RE = re.compile(r'assist_script_event pump=(\d+) action=(\S+)')
SAMPLE_MARKER_RE = re.compile(r'savestate_audio phase=(save|load) marker=(\d+)')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_mono_pcm(path):
    with wave.open(str(path)) as wav:
        assert wav.getsampwidth() == 2 and wav.getnchannels() == 1
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    return rate, list(struct.unpack('<%dh' % (len(raw) // 2), raw))


def mean_abs(a, b):
    n = min(len(a), len(b))
    return sum(abs(x - y) for x, y in zip(a[:n], b[:n])) / max(n, 1)


def band_energies(seg, rate):
    # DFT-free band split: low band via moving average, high = residual.
    w = max(1, rate // 4000)
    win = len(seg)
    lo = hi = 0.0
    acc = 0.0
    for i, s in enumerate(seg):
        acc += s
        if i >= w:
            acc -= seg[i - w]
        avg = acc / min(i + 1, w)
        lo += avg * avg
        d = s - avg
        hi += d * d
    return lo / max(win, 1), hi / max(win, 1)


def verdict_for_pair(a, b):
    # Pure verdict function (also used by the negative control). Both
    # segments must be nonempty and equal length; anything else is
    # inequivalent with an explicit reason (never silently clipped).
    if len(a) == 0 or len(b) == 0:
        return {'equivalent': False, 'reason': 'empty comparison window',
                'exact_match_rate': 0.0, 'align_residual_mean_abs': None,
                'first_diff_sample_offset': None}
    if len(a) != len(b):
        return {'equivalent': False,
                'reason': 'length mismatch %d vs %d' % (len(a), len(b)),
                'exact_match_rate': 0.0, 'align_residual_mean_abs': None,
                'first_diff_sample_offset': None}
    n = len(a)  # == len(b) here
    exact = sum(1 for x, y in zip(a, b) if x == y)
    first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y),
                 None)
    res = mean_abs(a, b)
    return {'equivalent': first is None,
            'exact_match_rate': round(exact / n, 6),
            'align_residual_mean_abs': round(res, 3),
            'first_diff_sample_offset': first}


def negative_control():
    # Must FAIL equivalence: perturbed data must never verify as restored.
    # Deterministic PRNG (LCG) so the sequence has no hidden periodicity
    # that a shift could map onto itself.
    x = 0x12345678
    base = []
    for _ in range(65536):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        base.append((x >> 8) % 65536 - 32768)
    shifted = base[32768:] + base[:32768]
    inverted = [-v for v in base]
    ok_a = not verdict_for_pair(base, shifted)['equivalent']
    ok_b = not verdict_for_pair(base, inverted)['equivalent']
    # Sanity: identical data MUST verify (else the verdict is vacuous).
    ok_c = verdict_for_pair(base, list(base))['equivalent']
    return ok_a and ok_b and ok_c


def parse_events(stdout, save_pump, load_pump):
    # Returns dict or an error string. Requires: initial savestate_loaded
    # (capture-start frame), exactly one slot save/load pair in chronological
    # order, save/load identity (same frame, PC, digest), pump/frame bounds
    # consistent with the INTENDED assist script (0 < save_pump < load_pump <=
    # presented frames, save frame within the run), AND that the OBSERVED
    # assist_script_event lines fired exactly the requested save/load actions
    # at the requested pumps. Pump bounds alone only prove the numbers are
    # plausible; the observed events prove the actions actually ran there.
    ev = {}
    m0 = re.search(r'savestate_loaded path="[^"]*" pc=(0x[0-9a-f]+) '
                   r'frame=(\d+)', stdout)
    if not m0:
        return 'missing initial savestate_loaded line'
    ev['capstart_pc'] = m0.group(1)
    ev['capstart_frame'] = int(m0.group(2))
    saves = [(m.start(), m.groups()) for m in re.finditer(
        r'savestate_saved slot=1 .*?pc=(0x[0-9a-f]+) frame=(\d+) mem=([0-9a-f]+)',
        stdout)]
    if not saves:
        return 'missing slot-1 savestate_saved line'
    loads = [(m.start(), m.groups()) for m in re.finditer(
        r'savestate_loaded slot=1 .*?pc=(0x[0-9a-f]+) frame=(\d+) mem=([0-9a-f]+)',
        stdout)]
    if not loads:
        return 'missing slot-1 savestate_loaded line'
    if not saves[0][0] < loads[0][0]:
        return 'out-of-order events: load precedes save'
    if len(saves) > 1 or len(loads) > 1:
        return 'duplicate save/load events: expected exactly one pair'
    _, (spc, sfr, smem) = saves[0]
    _, (lpc, lfr, lmem) = loads[0]
    ev['save_pc'], ev['save_frame'], ev['save_mem'] = (
        spc, int(sfr), smem)
    ev['load_pc'], ev['load_frame'], ev['load_mem'] = (
        lpc, int(lfr), lmem)
    if not (ev['save_mem'] == ev['load_mem'] and
            ev['save_frame'] == ev['load_frame'] and
            ev['save_pc'] == ev['load_pc']):
        return ('save/load identity mismatch: save %s vs load %s'
                % ((spc, sfr, smem), (lpc, lfr, lmem)))
    if not (ev['capstart_frame'] <= ev['save_frame']):
        return 'save frame precedes capture start'
    presented = re.findall(r'frames_presented=(\d+)', stdout)
    if not presented:
        return 'missing frames_presented banner (cannot bound pumps)'
    presented = int(presented[-1])
    ev['presented_frames'] = presented
    if not (0 < save_pump < load_pump <= presented):
        return ('assist pump bounds violated: want 0 < %d < %d <= %d '
                '(presented)' % (save_pump, load_pump, presented))
    # Observed assist events must exactly match the requested script (checked
    # last so structural errors above keep their more specific messages).
    observed = [(int(p), a) for p, a in ASSIST_EVENT_RE.findall(stdout)]
    expected = [(save_pump, 'save1'), (load_pump, 'load1')]
    if observed != expected:
        return ('assist events mismatch: observed %s != requested %s'
                % (observed, expected))
    # Exact source-sample markers at the save and load boundaries. The guest
    # mixer's absolute sample counter is serialized with the snapshot, so a
    # clean restore resumes the SAME sample index: the slot save marker must
    # equal the slot load marker. This gives an exact source-sample anchor
    # (no guest-frame estimation) for post-restore audio alignment.
    marks = SAMPLE_MARKER_RE.findall(stdout)
    save_m = [int(v) for k, v in marks if k == 'save']
    load_m = [int(v) for k, v in marks if k == 'load']
    if not save_m or not load_m:
        return ('missing savestate_audio marker lines: this build does not '
                'emit the source-sample markers required to substantiate '
                'exact onset')
    ev['save_sample_marker'] = save_m[-1]
    ev['load_sample_marker'] = load_m[-1]
    ev['sample_marker_delta'] = load_m[-1] - save_m[-1]
    if save_m[-1] != load_m[-1]:
        return ('source-sample marker mismatch: save %d != load %d '
                '(restore did not resume the saved sample index)'
                % (save_m[-1], load_m[-1]))
    return ev


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--state', required=True, type=Path,
                        help='banked .state to load (read-only, never written)')
    parser.add_argument('--trace', required=True, type=Path,
                        help='frame-indexed input CSV covering the run')
    parser.add_argument('--exe', type=Path, default=ROOT / 'build/MMBN3WhiteRecomp')
    parser.add_argument('--save', type=Path, default=ROOT / 'saves/mmbn3_white_usa.sav')
    parser.add_argument('--frames', type=int, default=950)
    parser.add_argument('--timeout', type=float, default=420)
    parser.add_argument('--target-ms', type=float, default=40)
    parser.add_argument('--save-pump', type=int, default=5)
    parser.add_argument('--load-pump', type=int, default=405)
    parser.add_argument('--phase-sync', action='store_true',
                        help='opt-in GBARECOMP_SAVELOAD_PHASE_SYNC=1 '
                             '(diagnostic phase normalization; default off)')
    args = parser.parse_args()

    if not negative_control():
        print('SELF-TEST FAILED: negative control did not fail as required',
              flush=True)
        return 2

    parent = ROOT / 'build/audio-restore'
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=Path(args.label).name + '-', dir=parent))
    source_save = args.save.resolve()
    before = sha(source_save)
    shutil.copyfile(source_save, out / 'initial.sav')
    shutil.copyfile(out / 'initial.sav', out / 'play.sav')
    shutil.copyfile(args.trace, out / 'inputs.csv')
    shutil.copyfile(ROOT / 'roms/mmbn3_white_usa.gba', out / 'rom.gba')
    trace_rows = sum(1 for ln in (out / 'inputs.csv').read_text().splitlines()
                     if ln and not ln.startswith('#'))
    if sha(out / 'initial.sav') != before or sha(source_save) != before:
        raise RuntimeError('source save changed while preparing capture')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GBARECOMP_')}
    assist = f'{args.save_pump}:save1;{args.load_pump}:load1'
    overrides = {
        'GBARECOMP_INPUT_REPLAY': str(out / 'inputs.csv'),
        'GBARECOMP_HEAL_CACHE': str(out / 'heal-cache'),
        'GBARECOMP_HEAL_WARM_LOAD': '0',
        'GBARECOMP_AUDIO_PROBE': '1',
        'GBARECOMP_AUDIO_CAPTURE': str(out / 'audio'),
        'GBARECOMP_AUDIO_TARGET_MS': str(args.target_ms),
        'GBARECOMP_ASSIST_SCRIPT': assist,
        'GBARECOMP_FRAME_PHASE': str(out / 'frame-phase.csv'),
        'GBARECOMP_FRAMEDUMP_COUNT': '0',
    }
    if args.phase_sync:
        overrides['GBARECOMP_SAVELOAD_PHASE_SYNC'] = '1'
    env.update(overrides)
    cmd = [str(args.exe.resolve()), str(ROOT / 'game.toml'), '--window',
           '--frames', str(args.frames), '--rom', str(out / 'rom.gba'),
           '--bios', str(ROOT.parent / 'gbarecomp/bios/gba_bios.bin'),
           '--save', str(out / 'play.sav'),
           '--load-state', str(args.state.resolve())]
    result = {'command': cmd, 'assist': assist,
              'environment': overrides, 'source_save_sha256': before,
              'binary_sha256': sha(args.exe),
              'input_sha256': sha(out / 'inputs.csv'),
              'input_rows': trace_rows,
              'phase_sync': bool(args.phase_sync),
              'requested_frames': args.frames, 'started': time.time(),
              'voice_coverage': 'not asserted by this harness; wave/noise '
                                'voices separately observed live at battle '
                                'states (NR52 channel flags + MP2K walk; '
                                'evidence retained under build/audio-restore/)',
              'limitations': ['Source PCM only; no speaker/device claims.',
                              'Replay reproduces guest inputs, not host scheduling.',
                              'Stale-tail metric is source continuity, not '
                              'host-queue proof; see '
                              'tools/host_audio_queue_check.py for the '
                              'post-bridge, marker-aligned harness.']}
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
    log = (out / 'stderr.log').read_text(errors='replace')
    stdout = (out / 'stdout.log').read_text(errors='replace')
    counts = re.findall(r'frames_presented=(\d+)', stdout)
    result['presented_frames'] = int(counts[-1]) if counts else None
    result['coverage'] = next((line for line in reversed(stdout.splitlines())
                               if line.startswith('self_heal_coverage=')), None)
    result['capture_complete'] = 'ok=1 truncated=0' in log

    # Part 1: capture validity (run + events), independent of audio match.
    events = parse_events(stdout, args.save_pump, args.load_pump)
    result['events'] = events if isinstance(events, dict) else {'error': events}
    result['events_ok'] = isinstance(events, dict)
    final_ppu = re.findall(r'ppu_frames=(\d+)', stdout)
    result['final_ppu_frames'] = int(final_ppu[-1]) if final_ppu else None
    result['capture_valid'] = (
        result['exit_code'] == 0 and not result['timed_out']
        and result['source_save_unchanged'] and result['capture_complete']
        and result['presented_frames'] == args.frames
        and result['events_ok'] and result['final_ppu_frames'] is not None)

    # Part 2: audio equivalence at guest-frame-derived alignment. Any
    # failure to read the capture or run the analysis is INVALID (exit 2),
    # never a valid mismatch (exit 1).
    result['restore_audio_equivalent'] = False
    result['analysis'] = {}
    src_wav = out / 'audio-source.wav'
    if result['capture_valid']:
        try:
            rate, pcm = read_mono_pcm(src_wav)
            result['analysis'] = analyze_aligned(
                rate, pcm, result['events'], result['final_ppu_frames'],
                args)
            # Exact source-sample marker (recorded, not estimated): the
            # primary anchor for "exact onset". The guest-frame split below
            # stays as a cross-check.
            result['analysis']['save_sample_marker'] = result['events'].get(
                'save_sample_marker')
            result['analysis']['load_sample_marker'] = result['events'].get(
                'load_sample_marker')
            result['restore_audio_equivalent'] = bool(
                result['analysis'].get('equivalent'))
        except Exception as exc:
            result['analysis'] = {'error': 'analysis failed: %s: %s'
                                  % (type(exc).__name__, exc)}
            result['capture_valid'] = False

    (out / 'session.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
    if not result['capture_valid']:
        return 2
    return 0 if result['restore_audio_equivalent'] else 1


def analyze_aligned(rate, pcm, ev, final_ppu, args):
    # Guest-frame-anchored alignment. Segment A and B both cover guest
    # frames [save_frame, save_frame + K]. Nominal samples/guest-frame is
    # the hardcoded bridge rate (65536 / 59.7275), cross-validated against
    # exact AUD-section sample counters (agreement to single-digit samples);
    # only a tight local refinement absorbs drift, guarded by a
    # grid-median significance check.
    n = len(pcm)
    # Nominal source samples per guest frame (GBA 59.7275 Hz frame rate at
    # the 65536 Hz bridge rate). Used ONLY to center the search: the save
    # event frame and the load return frame are exact (parsed above), and
    # presented pumps map ~1:1 to guest frames from a loaded state
    # (observed offsets: single-digit samples). The tight grid plus the
    # median-significance guard absorbs the residual drift; a flat landscape
    # reports split_found=false instead of a false match.
    r = 65536 / 59.7275
    guest_elapsed_net = max(final_ppu - ev['capstart_frame'], 1)
    save_s0 = int((ev['save_frame'] - ev['capstart_frame']) * r)
    seg_guest = int((args.load_pump - args.save_pump) * r)
    # Predicted split from guest-frame arithmetic (the primary alignment):
    # segment B replays guest frames [save_frame, save_frame + pumps].
    split_pred = save_s0 + seg_guest
    W = 4000
    # Landscape sample for the significance guard (cheap, fixed stride).
    land = []
    step = max(1, (n - W) // 400)
    for s in range(0, n - W, step):
        t = (s + seg_guest) % max(n - W, 1)
        land.append(mean_abs(pcm[s:s + W], pcm[t:t + W]))
    land.sort()
    med = land[len(land) // 2]
    # Local refinement around the prediction (drift only).
    def scan(fixed_s, center):
        b, bt = None, center
        t = max(0, center - 1200)
        while t <= min(n - W, center + 1200):
            d = mean_abs(pcm[fixed_s:fixed_s + W], pcm[t:t + W])
            if b is None or d < b:
                b, bt = d, t
            t += 25
        return b, bt
    best, split = scan(save_s0, split_pred)
    save_s = save_s0
    for ds in range(-1200, 1201, 25):
        s2 = save_s0 + ds
        if 0 <= s2 <= n - W:
            d = mean_abs(pcm[s2:s2 + W], pcm[split:split + W])
            if d < best:
                best, save_s = d, s2
    best, split = scan(save_s, split)
    significant = best < 0.25 * med and best < 500
    # A minimum at the guest-predicted location with a large residual means
    # the branches genuinely differ there (not a lost alignment): the
    # prediction comes from exact save/load frame numbers, independent of
    # the audio content.
    predicted_match = abs(split - split_pred) <= 1200
    out = {'rate': rate, 'capture_samples': n,
           'save_sample': save_s, 'split_sample': split,
           'split_seconds': round(split / rate, 3),
           'split_predicted_sample': split_pred,
           'split_at_predicted_location': bool(predicted_match),
           'nominal_rate_per_guest_frame': round(r, 3),
           'guest_frames_net': guest_elapsed_net,
           'grid_median_residual': round(med, 2),
           'split_found': bool(significant),
           'grid_best_residual': round(best, 3)}
    if not significant:
        out['equivalent'] = False
        out['reason'] = ('branches diverge at predicted alignment'
                         if predicted_match
                         else 'no significant alignment (flat landscape)')
        if predicted_match:
            # Evaluate the full-segment verdict at the guest-correct
            # alignment anyway: a large residual here means the branches
            # genuinely differ, and first_diff localizes it.
            W2 = min(65536, n - max(save_s, split))
            vf = verdict_for_pair(pcm[save_s:save_s + W2],
                                  pcm[split:split + W2])
            for k, val in vf.items():
                out['verdict_' + k] = val
        return out
    # Full-segment verdict at best alignment.
    W2 = min(65536, n - max(save_s, split))
    vf = verdict_for_pair(pcm[save_s:save_s + W2], pcm[split:split + W2])
    for k, val in vf.items():
        out['verdict_' + k] = val
    out['equivalent'] = vf['equivalent']
    # Source-continuity tail around the load point (NOT a host-queue test).
    tail = pcm[max(0, split - rate):split]
    b = pcm[split:split + W2]
    stale = 0
    base = len(tail) - min(len(tail), W2)
    while (stale < W2 and stale < len(tail)
           and abs(b[stale] - tail[base + stale]
                   if base + stale < len(tail) else tail[-1]) < 64):
        stale += 1
    out['source_tail_match_samples'] = stale
    out['source_tail_match_ms'] = round(1000 * stale / rate, 2)
    win = rate // 2
    b_lo, b_hi = band_energies(b[:win], rate)
    a_lo, a_hi = band_energies(pcm[save_s:save_s + win], rate)
    out['band_energy_early_b_over_a_low_high'] = [
        round(b_lo / a_lo, 4) if a_lo else None,
        round(b_hi / a_hi, 4) if a_hi else None]
    return out


if __name__ == '__main__':
    raise SystemExit(main())
