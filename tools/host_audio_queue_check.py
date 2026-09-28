#!/usr/bin/env python3
"""Host audio-bridge behaviour across a restore (marker-aligned, bounded).

The open question: the engine performs no flush of the host audio ring on
EITHER restore path, so audio generated before a restore is still queued and
keeps playing after it. The engine's own comments call that "a separate future
flush concern", and `tools/audio_restore_check.py` states outright that its
stale-tail metric "does NOT test the SDL host bridge queue (source is captured
before the bridge)".

WHAT THIS TOOL MEASURES, AND WHAT IT DOES NOT

  measured   Ring fill (ms of already-generated source audio) at the exact
             restore boundary, read from an in-engine action marker that is
             recorded under the same bridge mutex as the producer pushes and
             the device pulls. That fill IS queued pre-restore audio: the
             restore paths do not flush the ring (code-verified, and the
             output-side check below looks for it).
  measured   Bridge counter steps (stretch / underrun / overflow) inside a
             time window around the boundary, attributed ONLY if the
             no-restore control's equivalent window is clean.
  tested     Whether pre-restore samples are identifiable in the POST-BRIDGE
             output WAV (see "output tail check" below). If the correlation
             is not decisive this tool reports "not established" and does NOT
             print QUEUE OK.
  NOT done   No claim about what a listener heard. The output WAV is the
             engine's callback buffer handed to SDL, post-bridge but
             pre-device; downstream mixing, resampling and speakers are
             outside every capture here.

ALIGNMENT (the previous version's core defect)

The earlier revision indexed producer ('P') records by the assist pump number
and assumed the action was at push record #pump. That is not established:
`runtime.cpp` pushes audio BEFORE `pump_host_input`, so an action observed at
pump N lands at a push index that is not exactly N, and captures can contain
extra/short pushes. This version does NOT use the pump number for alignment.
It locates the absolute event boundary from a marker record written by the
engine at the action itself (`savestate_audio`... no longer stdout-only):

  kind 'M' records in `<prefix>-events.csv`, one per host action:
    presave / postsave          file save
    preload / postload          file load
    premem-save / postmem-save  in-memory (rewind) snapshot
    premem-load / postmem-load  in-memory (rewind) restore
    fast-on / fast-off          fast-forward level edge (Turbo/latch/script)
    pause / resume              APPLIED host pause transition
    rewind-trigger              rewind request reached the dispatcher

Markers carry a steady_clock ns, the ring fill, and the bridge counters,
recorded while holding the audio mutex, so the marker's position in the
event stream is ordered exactly against the P and C records. Boundary
selection uses only the label it asks for, so the extra host-action markers
are ignored here (and can be analyzed by their own tools). Captures written
before the marker existed have no markers: this tool REJECTS them as
unusable for alignment rather than silently falling back to pump arithmetic.

Per branch, against a no-restore control run:

  ring_at_boundary_ms   ring fill at the action marker (exact boundary).
  ring_depth_frames     the same fill in source frames (the exposure window).
  counter_steps_window  bridge counter steps whose own steady_clock ns lies
                        within +/- --window-ms of the marker. Attribution is
                        refused when a control repeat shows a step in its own
                        equivalent window.
  output_tail           correlation of the pre-restore source tail against
                        the post-bridge output right after the boundary, plus
                        the same output window against post-restore source.

CONTROL

The control branch runs the same route with a save one pump before where the
restore fires and NO restore; its boundary is the last `presave` marker. A
control whose counter window is not clean means a background artifact can land
there, so no restore-branch step may be blamed on the restore. The final
verdict gate REQUIRES control-clean attribution: a noisy control yields
QUEUE UNRESOLVED (exit 1), never QUEUE OK.

Exit codes: 0 = every claim this tool makes is established (control clean,
marker alignment verified, exposure measured, post-bridge tail identified);
1 = run(s) valid but a claim is NOT established or a hard counter failure was
measured; 2 = invalid run (missing fixture, missing markers, capture failure).

Private assets are used only via disposable copies in an ignored run dir; the
source save is copied and hash-verified, never written.
"""
import argparse
import array
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOOL_RULE_VERSION = '2026-09-25-marker-aligned'

EVENT_RE = re.compile(r'assist_script_event pump=(\d+) action=(\S+)')
MARKER_RE = re.compile(r'savestate_audio phase=(\S+) marker=(\d+)')
REWINDS_RE = re.compile(r'rewind_loaded frame=(\d+)')
FILE_LOAD_RE = re.compile(r'savestate_loaded slot=(\d+) .*?frame=(\d+)')
SAVE_RE = re.compile(
    r'savestate_saved slot=(\d+) .*?pc=(0x[0-9a-f]+) frame=(\d+) '
    r'mem=([0-9a-f]+)')
FINAL_RE = re.compile(r'frames_presented=(\d+)')

COUNTERS = ('stretch_frames', 'underrun_frames', 'overflow_frames')
DEFAULT_WINDOW_MS = 150.0


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── event parsing / timeline ────────────────────────────────────────────────

def read_events(path):
    """Parse the capture events CSV into typed dicts.

    Tolerates the legacy header without the trailing `label` column (those
    captures predate markers and are rejected later by the alignment check).
    """
    rows = []
    if not Path(path).exists():
        return rows
    with Path(path).open() as fh:
        for r in csv.DictReader(fh):
            row = {'kind': (r.get('kind') or '').strip(),
                   'ns': int(r['ns']),
                   'offset': int(r['offset']),
                   'frames': int(r['frames']),
                   'fill_ms': float(r['fill_ms']),
                   'stretch_frames': int(r['stretch_frames']),
                   'underrun_frames': int(r['underrun_frames']),
                   'overflow_frames': int(r['overflow_frames']),
                   'label': (r.get('label') or '').strip()}
            rows.append(row)
    return rows


def verify_timeline(events):
    """Structural sanity of the capture stream (no pump arithmetic involved).

    Checks that the mutex-serialized wall clock is monotonic, that producer
    and pull offsets are contiguous from zero, and reports how many marker
    records exist. A marker-free capture is reported but NOT treated as a
    usable alignment source; the caller decides that, so the same function can
    explain why a legacy capture was rejected.
    """
    problems = []
    prev_ns = None
    for r in events:
        if prev_ns is not None and r['ns'] < prev_ns:
            problems.append('event ns went backwards')
            break
        prev_ns = r['ns']
    pushes = [r for r in events if r['kind'] == 'P']
    pulls = [r for r in events if r['kind'] == 'C']
    markers = [r for r in events if r['kind'] == 'M']
    src = 0
    for r in pushes:
        if r['offset'] != src:
            problems.append(
                f"producer offset discontinuity: expected {src}, "
                f"got {r['offset']}")
            break
        src += r['frames']
    out = 0
    for r in pulls:
        if r['offset'] != out:
            problems.append(
                f"pull offset discontinuity: expected {out}, got {r['offset']}")
            break
        out += r['frames']
    unknown = sorted({r['kind'] for r in events} - {'P', 'C', 'M'})
    if unknown:
        problems.append(f'unknown record kinds {unknown}')
    return {
        'pushes': len(pushes),
        'pulls': len(pulls),
        'markers': len(markers),
        'marker_labels': [m['label'] for m in markers],
        'source_frames': src,
        'output_frames': out,
        'problems': problems,
    }


def marker_indices(events, label):
    return [i for i, r in enumerate(events)
            if r['kind'] == 'M' and r['label'] == label]


def find_marker(events, label, occurrence=-1):
    idxs = marker_indices(events, label)
    if not idxs:
        return None
    if abs(occurrence) > len(idxs):
        return None
    return idxs[occurrence]


def cursors_before(events, index):
    """Source/output frames fully pushed/pulled BEFORE `index` in the stream."""
    src = sum(r['frames'] for r in events[:index] if r['kind'] == 'P')
    out = sum(r['frames'] for r in events[:index] if r['kind'] == 'C')
    return src, out


def lead_in_frames(events):
    """Output frames the device pulled BEFORE the first producer push.

    The bridge emits faded silence until the ring primes, so the output
    timeline starts up to a few hundred ms before the source timeline. FIFO
    arithmetic (pushed - pulled = fill) only holds once that lead-in is
    subtracted.
    """
    out = 0
    for r in events:
        if r['kind'] == 'P':
            return out
        if r['kind'] == 'C':
            out += r['frames']
    return out


def ring_depth_at(events, index, source_rate):
    marker = events[index]
    frames = int(round(marker['fill_ms'] * source_rate / 1000.0))
    return {'fill_ms': marker['fill_ms'], 'frames': frames,
            'ns': marker['ns'], 'label': marker['label']}


def neighbours_of(events, index):
    """Nearest P/C records before and after the marker, for context."""
    out = {'before': None, 'after': None}
    for j in range(index - 1, -1, -1):
        if events[j]['kind'] in ('P', 'C'):
            out['before'] = {'kind': events[j]['kind'],
                             'ns': events[j]['ns'],
                             'fill_ms': events[j]['fill_ms']}
            break
    for j in range(index + 1, len(events)):
        if events[j]['kind'] in ('P', 'C'):
            out['after'] = {'kind': events[j]['kind'],
                            'ns': events[j]['ns'],
                            'fill_ms': events[j]['fill_ms']}
            break
    return out


# ── counter attribution ─────────────────────────────────────────────────────

def counter_steps(events, counter):
    """Every (index, before, after) step of a cumulative bridge counter.

    Computed over the MERGED event stream, not just the producer pushes: the
    servo and the stretch/underrun counters are updated on the pull side, so a
    step can land between two pushes and would be invisible on the P timeline
    alone. Step ns is the record's own steady_clock timestamp.
    """
    steps = []
    prev = 0
    for i, r in enumerate(events):
        v = r[counter]
        if v > prev:
            steps.append((i, prev, v))
        prev = v
    return steps


def steps_in_window(steps, events, index, window_ms):
    """Counter steps whose own ns lies within +/- window_ms of events[index]."""
    lo = events[index]['ns'] - int(window_ms * 1e6)
    hi = events[index]['ns'] + int(window_ms * 1e6)
    return [s for s in steps if lo <= events[s[0]]['ns'] <= hi]


# ── post-bridge output analysis ─────────────────────────────────────────────

def read_wav_mono(path):
    with wave.open(str(path), 'rb') as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f'{path}: not mono 16-bit PCM')
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    data = array.array('h')
    data.frombytes(raw)
    if sys.byteorder == 'big':
        data.byteswap()
    return rate, data


def _decimate(seq, decim):
    if decim <= 1:
        return list(seq)
    return list(seq[::decim])


def normalized_correlation(a, b, decim=2):
    """Zero-mean normalized cross-correlation of two equal-length sequences."""
    a = _decimate(a, decim)
    b = _decimate(b, decim)
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    num = da = db = 0.0
    for i in range(n):
        x, y = a[i] - ma, b[i] - mb
        num += x * y
        da += x * x
        db += y * y
    if da <= 0.0 or db <= 0.0:
        return 0.0
    return num / math.sqrt(da * db)


def output_tail_check(src, out, src_before, out_before, depth_frames,
                      window=4096, max_lag=64, decim=2, silence_peak=32):
    """Is the pre-restore source tail identifiable in the post-bridge output?

    Structural setup: the ring is FIFO and (in this configuration) the device
    rate equals the source rate with a <= 0.5% servo nudge, so the output after
    the boundary should begin with the resampled version of the last
    `depth_frames` source frames pushed before it. The check measures:

      corr_pre   best normalized correlation between that source tail and the
                 output window searched over +/- max_lag frames around the
                 position an unflushed ring predicts (reported lag is relative
                 to that position; 0 means exactly there).
      corr_post  the same output window against the post-restore source
                 content (the alternative hypothesis: a flush would have
                 moved this content up to the boundary).

    'established' requires corr_pre >= 0.85 AND a clear margin over corr_post.
    Everything else is reported as not-established, with the raw numbers, so
    a weak or ambiguous result cannot be dressed up as proof.
    """
    L = min(window, int(depth_frames), int(src_before),
            len(out) - int(out_before) - 1, len(src) - int(src_before))
    result = {'status': 'not-established', 'corr_pre': None,
              'corr_post': None, 'lag_frames': None, 'window_frames': L,
              'reason': ''}
    if L < 512:
        result['reason'] = f'window too short (L={L})'
        return result
    src_pre = src[src_before - L:src_before]
    src_post = src[src_before:src_before + L]
    peak_pre = max(abs(v) for v in src_pre[::8]) or 0
    if peak_pre < silence_peak:
        result['reason'] = 'pre-restore source window is near-silent'
        return result
    # The unread region is [src_before - depth, src_before); its last L frames
    # reach the device `depth - L` frames after the boundary in the unflushed
    # case, so that is the expected position and the search is centred on it.
    expected = int(depth_frames) - L
    best = None
    for lag in range(expected - max_lag, expected + max_lag + 1):
        lo = out_before + lag
        if lo < 0 or lo + L > len(out):
            continue
        c = normalized_correlation(src_pre, out[lo:lo + L], decim)
        if best is None or c > best[0]:
            best = (c, lag - expected)
    if best is None:
        result['reason'] = 'no output window fits the lag search'
        return result
    corr_pre, lag = best
    lo = out_before + expected + lag
    out_win = out[lo:lo + L]
    peak_out = max(abs(v) for v in out_win[::8]) or 0
    if peak_out < silence_peak:
        result['corr_pre'] = round(corr_pre, 4)
        result['reason'] = 'post-bridge output window is near-silent'
        return result
    corr_post = normalized_correlation(src_post, out_win, decim)
    result.update({'corr_pre': round(corr_pre, 4),
                   'corr_post': round(corr_post, 4),
                   'lag_frames': lag})
    if corr_pre >= 0.85 and (corr_pre - corr_post) >= 0.10:
        result['status'] = 'established'
        result['reason'] = ('pre-restore tail correlates in the post-bridge '
                            'output and post-restore content does not')
    else:
        result['reason'] = (f'not decisive (corr_pre={corr_pre:.3f}, '
                            f'corr_post={corr_post:.3f})')
    return result


# ── run plumbing ────────────────────────────────────────────────────────────

def branch_env(script, sync, out, args):
    """Environment for one run branch.

    Parent GBARECOMP_* variables are dropped on purpose: a branch gets exactly
    the knobs named here, plus the explicit diagnostic toggles (phase markers,
    SDL event watch, event canary) if requested. The sweep records those in its JSON. Setting a GBARECOMP_* variable
    in the calling shell has no effect on the run. In particular, callers
    cannot override the fixture, capture, or phase-sync settings and still
    receive a report claiming the original branch was tested.

    HEAL CACHE. By default each run compiles into its own fresh cache
    (`out/cache`), which means every run pays the first-load cost of ~90 new
    shard paths (see AUDIO_REVIEW §5o-§5q: under a cold cache those loads hold
    dyld's loaders lock long enough to park the emulation thread's event pump).
    `--heal-cache DIR` points every run at one persistent directory instead, so
    a warm arm loads already-compiled, already-validated shard paths. The
    directory is never cleaned by the harness.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('GBARECOMP_')}
    heal_cache = getattr(args, 'heal_cache', None)
    env.update({
        'GBARECOMP_INPUT_REPLAY': str(args.trace),
        'GBARECOMP_FRAMEDUMP_COUNT': '0',
        'GBARECOMP_ASSIST_SCRIPT': script,
        'GBARECOMP_HEAL_CACHE': (str(heal_cache) if heal_cache
                                 else str(out / 'cache')),
        'GBARECOMP_AUDIO_CAPTURE': str(out / 'cap'),
    })
    if sync != 'off':
        env['GBARECOMP_SAVELOAD_PHASE_SYNC'] = sync
    for item in getattr(args, 'extra_env', None) or []:
        key, sep, value = str(item).partition('=')
        if not key or not sep:
            raise ValueError(f'--env expects KEY=VALUE, got {item!r}')
        if key in ('GBARECOMP_PHASE_MARKERS', 'GBARECOMP_EVENT_WATCH'):
            if value not in ('0', '1'):
                raise ValueError(f'--env {key} accepts only 0/1')
        elif key == 'GBARECOMP_EVENT_CANARY':
            # Probe interval in ms; 0 = off. Bounded so a typo cannot turn the
            # canary into a busy loop or a once-a-run no-op.
            if not value.isdigit() or int(value) > 1000:
                raise ValueError('--env GBARECOMP_EVENT_CANARY accepts an '
                                 'interval in ms (0-1000; 0 = off)')
        elif key == 'GBARECOMP_SDL_COST':
            # SDL event-call cost probe threshold in ms; 0 = off. Stderr-only,
            # so it is admitted for A/B runs that need per-call wall/CPU time.
            if not value.isdigit() or int(value) > 1000:
                raise ValueError('--env GBARECOMP_SDL_COST accepts a threshold '
                                 'in ms (0-1000; 0 = off)')
        elif key == 'GBARECOMP_NO_GAMEPAD':
            # Diagnostic arm: skip the SDL game-controller/sensor subsystem.
            if value not in ('0', '1'):
                raise ValueError(f'--env {key} accepts only 0/1')
        elif key in ('GBARECOMP_HEAL_PREWARM_MAP',
                     'GBARECOMP_HEAL_OUT_OF_PROCESS_LOAD'):
            # Heal-load contention arms: pre-warm a freshly compiled shard with
            # an executable mapping the engine owns (recommended), or pay its
            # first load in a helper process (measured slower). Both are opt-in
            # engines toggles with no default behaviour change.
            if value not in ('0', '1'):
                raise ValueError(f'--env {key} accepts only 0/1')
        elif key == 'GBARECOMP_LOAD_TRACE':
            # Engine-side dlopen window trace ([load-trace] lines, same
            # monotonic clock as the [sdl-cost] mono_us stamp). Stderr-only and
            # default off; admitted so a stall A/B can intersect the pump's
            # wait windows with the engine's own dynamic-load windows instead
            # of inferring authorship from log order.
            if value not in ('0', '1'):
                raise ValueError(f'--env {key} accepts only 0/1')
        else:
            raise ValueError('--env only permits GBARECOMP_PHASE_MARKERS=0/1, '
                             'GBARECOMP_EVENT_WATCH=0/1, '
                             'GBARECOMP_EVENT_CANARY=0-1000, '
                             'GBARECOMP_SDL_COST=0-1000, '
                             'GBARECOMP_NO_GAMEPAD=0/1, '
                             'GBARECOMP_LOAD_TRACE=0/1, '
                             'GBARECOMP_HEAL_PREWARM_MAP=0/1 or '
                             'GBARECOMP_HEAL_OUT_OF_PROCESS_LOAD=0/1')
        env[key] = value
    return env


def run_branch(tag, script, sync, frames, out, args):
    rom = out / 'rom.gba'
    shutil.copyfile(ROOT / 'roms/mmbn3_white_usa.gba', rom)
    sav = out / 'test.sav'
    shutil.copyfile(ROOT / 'saves/mmbn3_white_usa.sav', sav)
    prefix = out / 'cap'
    for suffix in ('-source.wav', '-output.wav', '-events.csv'):
        if Path(str(prefix) + suffix).exists():
            Path(str(prefix) + suffix).unlink()
    env = branch_env(script, sync, out, args)
    cmd = [str(args.exe), str(ROOT / 'game.toml'), '--window',
           '--frames', str(frames), '--rom', str(rom),
           '--bios', str(ROOT.parent / 'gbarecomp/bios/gba_bios.bin'),
           '--save', str(sav), '--load-state', str(args.state)]
    t0 = time.time()
    with (out / 'stdout.log').open('w') as so, \
            (out / 'stderr.log').open('w') as se:
        proc = subprocess.Popen(cmd, cwd=out, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=so, stderr=se)
        try:
            rc = proc.wait(timeout=args.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            rc, timed_out = None, True
    text = (out / 'stdout.log').read_text(errors='replace')
    err = (out / 'stderr.log').read_text(errors='replace')
    m = re.search(r'\[audio-capture\].*?source_frames=(\d+) '
                  r'output_frames=(\d+) events=(\d+)', err)
    events = read_events(Path(str(prefix) + '-events.csv'))
    return {
        'tag': tag, 'sync': sync, 'script': script, 'exit': rc,
        'timed_out': timed_out, 'seconds': round(time.time() - t0, 2),
        'dir': str(out), 'events_path': str(prefix) + '-events.csv',
        'source_wav': str(prefix) + '-source.wav',
        'output_wav': str(prefix) + '-output.wav',
        'events_observed': [(int(a), b) for a, b
                            in EVENT_RE.findall(text)],
        'markers': [(a, int(b)) for a, b in MARKER_RE.findall(text)],
        'saves': [(int(a), b, int(c), d)
                  for a, b, c, d in SAVE_RE.findall(text)],
        'rewinds': [int(x) for x in REWINDS_RE.findall(text)],
        'file_loads': [int(f) for f, _ in FILE_LOAD_RE.findall(text)],
        'presented': int(FINAL_RE.search(text).group(1))
        if FINAL_RE.search(text) else None,
        'events': events,
        'capture': {'source_frames': int(m.group(1)),
                    'output_frames': int(m.group(2)),
                    'events': int(m.group(3))} if m else None,
        'truncated': 'truncated=1' in err,
    }


def analyse_branch(res, action_kind, marker_label, window_ms,
                   presented, source_rate, expected_events=None,
                   action_seen=None):
    """Boundary-aligned analysis for one branch. No pump arithmetic."""
    events = res.pop('events')
    a = {'action_kind': action_kind, 'marker_label': marker_label,
         'problem': None, 'timeline': verify_timeline(events)}
    if not events:
        a['problem'] = 'no capture event records'
        return a, events
    if a['timeline']['problems']:
        a['problem'] = 'capture timeline inconsistent: ' + '; '.join(
            a['timeline']['problems'])
        return a, events
    label_hits = marker_indices(events, marker_label)
    idx = find_marker(events, marker_label, -1)
    a['marker_hits'] = len(label_hits)
    if idx is None:
        a['problem'] = (f'no {marker_label!r} marker in the capture: the '
                        'capture cannot be aligned to the action (marker '
                        'instrumentation required; pump-index fallback '
                        'refused)')
        return a, events
    # The scripted action must actually have run, independently of the marker:
    # observed assist events exactly as requested, plus the path-specific log
    # line (file load / rewind) so an in-memory cell cannot pass through the
    # file path or vice versa.
    if expected_events is not None:
        got = [tuple(e) for e in res.get('events_observed', [])]
        a['action_events_match'] = got == [tuple(e) for e in expected_events]
        if not a['action_events_match']:
            a['problem'] = (f'observed assist events {got} != requested '
                            f'{[tuple(e) for e in expected_events]}')
            return a, events
    if action_seen is not None:
        a['action_seen'] = action_seen(res)
        if not a['action_seen']:
            a['problem'] = ('the action did not appear in the run log under '
                            'the expected restore path')
            return a, events
    # The action is the LAST marker with this label: the boot --load-state
    # emits its own preload/postload pair before the scripted action, and the
    # rewind history capture emits many premem-save markers ahead of the
    # action. An earlier marker being present does not weaken the alignment;
    # the action-observed checks below pin the scripted action independently.
    a['marker_index'] = idx
    a['marker_ns'] = events[idx]['ns']
    a['boundary'] = ring_depth_at(events, idx, source_rate)
    a['neighbours'] = neighbours_of(events, idx)
    src_before, out_before = cursors_before(events, idx)
    a['source_frames_before'] = src_before
    a['output_frames_before'] = out_before
    # Buffer-state cross-check. The bridge's read cursor is
    # `src_before - fill`, so the output frames pulled beyond that cursor are
    # silence/priming/conceal frames that did not consume source audio. That
    # quantity is normally POSITIVE (the bridge primes at the 40 ms cushion
    # and conceals short producer stalls by emitting non-advancing output),
    # so it is reported, not failed. What must hold structurally is that the
    # recorded fill follows from the counters at all: a nonzero fill that
    # exceeds what has been pushed, or a cursor the output could not have
    # reached, means the marker is not describing this stream.
    cursor = src_before - a['boundary']['frames']
    a['cursor_at_boundary'] = cursor
    a['silence_or_priming_output_frames'] = out_before - cursor
    a['output_lead_in_frames'] = lead_in_frames(events)
    a['buffer_state_check'] = (0 < a['boundary']['frames'] <= src_before
                               and cursor >= 0 and out_before >= cursor)
    # Presented-frame sanity: one producer push per guest frame the guest ran
    # with audio enabled, with a small allowance for the final frame / startup.
    if presented is not None:
        a['push_records_minus_presented'] = (
            a['timeline']['pushes'] - presented)
        a['push_count_check'] = abs(a['timeline']['pushes'] - presented) <= 2
    else:
        a['push_count_check'] = None
    steps = {c: counter_steps(events, c) for c in COUNTERS}
    a['counter_steps_total'] = {c: len(s) for c, s in steps.items()}
    a['counter_steps_in_window'] = {
        c: steps_in_window(s, events, idx, window_ms) for c, s in steps.items()}
    a['counter_step_ns_in_window'] = {
        c: [events[s[0]]['ns'] for s in v]
        for c, v in a['counter_steps_in_window'].items()}
    a['action_window_clean'] = not any(a['counter_steps_in_window'].values())
    return a, events


# ── verdict ─────────────────────────────────────────────────────────────────

def decide(report, window_ms):
    """The single verdict gate used by the run, so no claim can outrun it.

    Hard failures (exit 1): source save changed, a branch invalid/incomplete,
    a bridge counter step inside a restore branch's window while the control
    is clean, or a control that cannot support attribution. A valid run whose
    post-bridge tail identification is not established is QUEUE UNRESOLVED
    (also exit 1): exposure is measured, audibility is not.
    """
    bad, notes = [], []
    if not report.get('source_save_unchanged'):
        bad.append('source_save_unchanged')
    branches = report['branches']
    controls = [b for b in branches if b['analysis']['action_kind'] == 'control']
    restores = [b for b in branches
                if b['analysis']['action_kind'] != 'control']
    if not controls:
        bad.append('no-control-branch')
    if not restores:
        bad.append('no-restore-branch')
    for b in branches:
        tag = f"{b['tag']}/r{b['repeat']}"
        a = b['analysis']
        if a.get('problem'):
            bad.append(f'{tag}: {a["problem"]}')
            continue
        if b['exit'] != 0 or b['timed_out'] or b['truncated'] \
                or not b['capture']:
            bad.append(f'{tag}: run did not complete cleanly '
                       f'(exit={b["exit"]}, timed_out={b["timed_out"]}, '
                       f'truncated={b["truncated"]})')
            continue
        if not a.get('push_count_check'):
            bad.append(f'{tag}: push record count does not align with '
                       f'presented frames '
                       f'({a.get("push_records_minus_presented")})')
        if not a.get('buffer_state_check'):
            bnd = a.get('boundary') or {}
            bad.append(f'{tag}: boundary buffer state is inconsistent with '
                       f'the captured counts '
                       f'(cursor={a.get("cursor_at_boundary")}, '
                       f'fill={bnd.get("frames")}, '
                       f'src_before={a.get("source_frames_before")})')
    usable_controls = [b for b in controls
                       if not b['analysis'].get('problem')]
    control_clean = [b['analysis'].get('action_window_clean')
                     for b in usable_controls]
    if usable_controls and not all(control_clean):
        bad.append('control-window-not-clean: a no-restore control moved a '
                   'bridge counter inside its own action window, so no '
                   'restore-branch step can be attributed to the restore')
        notes.append('attribution refused')
    for b in restores:
        if b['analysis'].get('problem'):
            continue
        tag = f"{b['tag']}/r{b['repeat']}"
        hits = {c: v for c, v in
                (b['analysis'].get('counter_steps_in_window') or {}).items()
                if v}
        if hits:
            bad.append(f'{tag}: bridge counter moved in the action window '
                       f'{hits}')
    # Post-bridge tail: exposure alone does not earn QUEUE OK.
    unestablished = []
    for b in restores:
        if b['analysis'].get('problem'):
            continue
        tail = (b.get('output_tail') or {}).get('status')
        if tail != 'established':
            unestablished.append(f"{b['tag']}/r{b['repeat']}: "
                                 f"{(b.get('output_tail') or {}).get('reason')}")
    ok = not bad
    if ok and unestablished:
        notes += unestablished
        return 'QUEUE UNRESOLVED', bad, notes, 1
    if ok:
        return 'QUEUE OK', bad, notes, 0
    return 'QUEUE FAIL', bad, notes, 1


# ── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--exe', type=Path, default=ROOT / 'build/MMBN3WhiteRecomp')
    ap.add_argument('--state', type=Path,
                    default=ROOT / 'build/winreplay/gate2-battleI-action/'
                                 'rom.state1')
    ap.add_argument('--trace', type=Path,
                    default=ROOT / 'build/winreplay/gate2-battleJ-action2/'
                                 'trace.csv')
    ap.add_argument('--pump', type=int, default=300)
    ap.add_argument('--frames', type=int, default=500)
    ap.add_argument('--repeat', type=int, default=3,
                    help='repeat every branch N times; the ring fill is '
                         'jittery, so the exposure claim needs a distribution')
    ap.add_argument('--window-ms', type=float, default=DEFAULT_WINDOW_MS,
                    help='half-width of the counter-attribution window around '
                         'each branch boundary marker')
    ap.add_argument('--source-rate', type=int, default=65536,
                    help='emulated source sample rate (Hz) used to convert '
                         'ring fill ms to frames')
    ap.add_argument('--timeout', type=float, default=300)
    ap.add_argument('--heal-cache', type=Path, default=None,
                    help='persistent heal-cache directory shared by every '
                         'run (warm-cache arm); default = a fresh cache per '
                         'run, which makes each run pay the first-load cost '
                         'of ~90 new shard paths')
    ap.add_argument('--json', type=Path, default=None)
    args = ap.parse_args()

    if not args.state.exists() or not args.trace.exists():
        print(f'SKIP: missing fixture {args.state} / {args.trace}')
        return 2

    p = args.pump
    save_events = [(5, 'save1'), (p - 1, 'save2')]
    branch_events = save_events + [(p + 15, 'save3')]
    branches = [
        # (tag, sync, script, marker label, action kind, expected assist
        #  events, path-specific log evidence for the action)
        ('control', 'off', f'5:save1;{p - 1}:save2', 'presave', 'control',
         save_events, None),
        ('rewind-off', 'off',
         f'5:save1;{p - 1}:save2;{p}:rewind;{p + 15}:save3',
         'premem-load', 'restore',
         save_events + [(p, 'rewind')] + [(p + 15, 'save3')], 'rewind'),
        ('rewind-both', 'both',
         f'5:save1;{p - 1}:save2;{p}:rewind;{p + 15}:save3',
         'premem-load', 'restore',
         save_events + [(p, 'rewind')] + [(p + 15, 'save3')], 'rewind'),
        ('load-off', 'off',
         f'5:save1;{p - 1}:save2;{p}:load1;{p + 15}:save3',
         'preload', 'restore',
         save_events + [(p, 'load1')] + [(p + 15, 'save3')], 'file_load'),
    ]
    seen_checks = {'rewind': lambda r: bool(r['rewinds']),
                   'file_load': lambda r: bool(r['file_loads'])}

    source = ROOT / 'saves/mmbn3_white_usa.sav'
    before = sha(source)
    out_root = ROOT / 'build/host-audio-queue'
    out_root.mkdir(parents=True, exist_ok=True)

    results = []
    for rep in range(max(1, args.repeat)):
        for tag, sync, script, marker, kind, expected, seen_kind in branches:
            d = Path(tempfile.mkdtemp(prefix=f'{tag}-r{rep}-', dir=out_root))
            res = run_branch(tag, script, sync, args.frames, d, args)
            res['repeat'] = rep
            a, events = analyse_branch(
                res, kind, marker, args.window_ms, res['presented'],
                args.source_rate, expected_events=expected,
                action_seen=seen_checks.get(seen_kind))
            res['analysis'] = a
            res['ndjson'] = [{'kind': r['kind'], 'ns': r['ns'],
                              'offset': r['offset'], 'frames': r['frames'],
                              'fill_ms': r['fill_ms'],
                              'stretch_frames': r['stretch_frames'],
                              'underrun_frames': r['underrun_frames'],
                              'overflow_frames': r['overflow_frames'],
                              'label': r['label']} for r in events]
            res['marker_boundary_check'] = {
                'stdout_markers': res['markers'],
                'csv_markers': a['timeline']['marker_labels'],
            }
            # Post-bridge output analysis for restore branches.
            if kind != 'control' and not a.get('problem'):
                try:
                    src_rate, src = read_wav_mono(res['source_wav'])
                    out_rate, out = read_wav_mono(res['output_wav'])
                    resolved = (src_rate == out_rate == args.source_rate)
                    if not resolved:
                        res['output_tail'] = {
                            'status': 'not-established',
                            'reason': f'rate mismatch src={src_rate} '
                                      f'out={out_rate} '
                                      f'arg={args.source_rate}'}
                    else:
                        res['output_tail'] = output_tail_check(
                            src, out, a['source_frames_before'],
                            a['output_frames_before'],
                            a['boundary']['frames'])
                except (ValueError, OSError) as e:
                    res['output_tail'] = {'status': 'not-established',
                                          'reason': f'WAV read failed: {e}'}
            results.append(res)
            q = res['analysis'].get('boundary') or {}
            tail = res.get('output_tail') or {}
            print(f"[{tag} r{rep}] marker={marker} "
                  f"fill_at_boundary={q.get('fill_ms')}ms "
                  f"({q.get('frames')} frames) "
                  f"window_clean={res['analysis'].get('action_window_clean')} "
                  f"window_steps="
                  f"{ {k: v for k, v in (res['analysis'].get('counter_steps_in_window') or {}).items() if v} } "
                  f"tail={tail.get('status', 'n/a')} "
                  f"corr_pre={tail.get('corr_pre')} "
                  f"corr_post={tail.get('corr_post')} "
                  f"pushes={res['analysis'].get('timeline', {}).get('pushes')} "
                  f"presented={res['presented']} {res['seconds']}s"
                  + (f" ERR={res['analysis']['problem']}"
                     if res['analysis'].get('problem') else ''),
                  flush=True)

    src_ok = sha(source) == before
    report = {
        'tool_rule_version': TOOL_RULE_VERSION,
        'branches': results,
        'source_save_unchanged': src_ok,
        'binary_sha256': sha(args.exe),
        'pump': p, 'frames': args.frames, 'repeat': args.repeat,
        'window_ms': args.window_ms, 'source_rate': args.source_rate,
    }
    verdict, bad, notes, rc = decide(report, args.window_ms)
    report['verdict'] = verdict
    report['failures'] = bad
    report['notes'] = notes
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + '\n')

    print(f'\nsource save unchanged: {src_ok}')
    print(f'binary sha256: {report["binary_sha256"]}')
    for tag in ('control', 'rewind-off', 'rewind-both', 'load-off'):
        rows = [r for r in results if r['tag'] == tag]
        if not rows:
            continue
        fills = [(r['analysis'].get('boundary') or {}).get('fill_ms')
                 for r in rows]
        fills = [f for f in fills if f is not None]
        clean = [r['analysis'].get('action_window_clean') for r in rows]
        if tag == 'control':
            print(f'control (no restore) boundary window clean: {clean}; '
                  f'marker fill samples={[round(f, 2) for f in fills]}ms')
        else:
            print(f'{tag}: ring fill at the exact restore boundary '
                  f'{min(fills) if fills else None}-'
                  f'{max(fills) if fills else None}ms over {len(fills)} '
                  f'repeats; window clean={clean}; '
                  f'post-bridge tail='
                  f'{[ (r.get("output_tail") or {}).get("status") for r in rows ]}')
    for n in notes:
        print(f'note: {n}')
    print(verdict + (f' {bad}' if bad else ''))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
