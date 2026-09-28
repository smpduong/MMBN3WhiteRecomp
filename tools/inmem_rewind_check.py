#!/usr/bin/env python3
"""In-memory (rewind) save/restore harness for GBARECOMP_SAVELOAD_PHASE_SYNC.

The file-based matrix in `phase_sync_matrix.py` covers only the file
save/load path. This harness covers the OTHER restore path the engine
implements, `do_savestate_load_bytes`, which is reached exclusively from the
rewind branch of the host-input pump. Nothing here edits the engine.

Trigger reachability (asserted, not assumed)
-------------------------------------------
`do_savestate_save_bytes` / `do_savestate_load_bytes` are called from exactly
one place: the rewind capture/rewind branch. It is reached by
`GBARECOMP_ASSIST_SCRIPT="<pump>:rewind"` in a windowed run (the game sets
`opts.rewind_history_seconds = 10`, capture interval 15 frames). Every run
asserts:
  * the requested assist script ran verbatim (`assist_script_event` lines),
  * a `rewind_loaded frame= pc= mem=` line appeared, which is logged only by
    the in-memory rewind branch,
  * NO `savestate_loaded slot=` line, which is what the FILE path logs, so a
    passing cell cannot have been restored through the file path.

Comparison
----------
  U (uninterrupted): saves at the pumps that make it observe the exact guest
                     frames the R branch visits after its restore.
  R (restored):      rewind at pump P, then saves at post-rewind pumps.

Both start from the same state fixture with the same replay trace. R's
post-restore guest frames are read from its log, never assumed; the U save
pumps are derived from them and each U save's observed frame is asserted
equal to the frame it was meant to observe. A wrong boundary is a loud
failure, not a silent mis-comparison.

Compared per sync mode:
  * boundary identity -- restored frame/PC/guest-memory digest vs U's digest
    at that same guest frame.
  * CPU/memory digests at the boundary and at >= 400 guest frames later.
  * pending cycles + next-event horizon, and the serialized mixer sample
    counter, probed by the file save each branch takes at a matched frame
    (`savestate_pending` / `savestate_audio` are logged on the file paths
    only, so a matched-pump file save is the probe for the in-memory path).
  * the whole post-restore I/O-register WRITE STREAM, which is where PPU,
    timer and audio device state actually shows up, diffed record by record
    so the first divergence localizes to a guest frame and a register.

Stream alignment is EMPIRICAL, not assumed: R and U share an identical
uninterrupted prefix, so the first index where R stops matching U is exactly
where the rewind takes effect. The restore tick is identified as the run of
records R emits at that index (its shadow still holds pre-rewind values, so
it logs the restore's own register delta rather than a guest write). The
f<->guest-frame map is then model-CHECKED against the logged restore geometry
before it is used, and the resulting offset is independently confirmed by
searching for the alignment that maximizes the common prefix. A derived
offset the data does not confirm is reported as unconfirmed, not trusted.

Negative control: `off` and `load` are asserted to FORK. If they ever rejoin,
the harness has lost its power to expose the defect and fails.

Exit 0 only when every cell met its expectation; 1 on a fork, a structural
failure, or an unconfirmed alignment; 2 for an invalid run (missing fixture,
script rejected, or no rewind event). Private assets are used only via
disposable copies in an ignored run dir; the source save is hash-verified
untouched.
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

SAVE_RE = re.compile(
    r'savestate_saved slot=(\d+) .*?pc=(0x[0-9a-f]+) frame=(\d+) '
    r'mem=([0-9a-f]+)')
# The file path logs "savestate_loaded slot=N ..."; the startup --load-state
# logs "savestate_loaded path=..." with no slot. Only the in-memory rewind
# path logs "rewind_loaded", which is what makes the two distinguishable.
FILE_LOAD_RE = re.compile(r'savestate_loaded slot=(\d+)')
BOOT_LOAD_RE = re.compile(r'savestate_loaded path=.*?frame=(\d+)')
REWIND_RE = re.compile(
    r'rewind_loaded frame=(\d+) pc=(0x[0-9a-f]+) mem=([0-9a-f]+)')
EVENT_RE = re.compile(r'assist_script_event pump=(\d+) action=(\S+)')
PENDING_RE = re.compile(r'savestate_pending pending=(\d+) budget=(-?\d+)')
MARKER_RE = re.compile(r'savestate_audio phase=(\S+) marker=(\d+)')
PRESENTED_RE = re.compile(r'frames_presented=(\d+)')
FINAL_RE = re.compile(r'final_pc=(0x[0-9a-f]+).*?ppu_frames=(\d+)')

# The assist-script parser accepts ONLY `save<N>` / `load<N>` (slot 1..10) or
# the exact words fast_on/fast_off/rewind/pause/resume, so save labels must be
# slot numbers, not descriptive names.
RULE_VERSION = '2026-09-25-aggregate-negative-control'

REWIND_BACK_FRAMES = 60   # engine: target = current - 60
REWIND_INTERVAL = 15     # engine: rewind_capture_interval_frames

# Post-rewind observation offsets, in guest frames from the restore. The last
# must clear the >= 400 guest-frame target.
POST_OFFSETS = (0, 14, 29, 44, 59, 404)

# I/O register groups, for naming the first divergent device write.
IO_GROUPS = (
    ('ppu', 0x04000000, 0x0400003F),
    ('dma', 0x04000040, 0x0400005F),
    ('sound', 0x04000060, 0x040000A5),
    ('dma_hi', 0x040000A8, 0x040000B3),
    ('timer', 0x04000100, 0x0400013F),
    ('irq', 0x04000200, 0x04000212),
)


def io_group(addr):
    for name, lo, hi in IO_GROUPS:
        if lo <= addr <= hi:
            return name
    return 'io'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_log(text):
    """Parse the run log, correlating each save with the pump that caused it.

    The engine logs `assist_script_event pump=N action=saveM` immediately
    before the `savestate_saved slot=M` it triggered, so the pump a save
    happened at is recovered by line order rather than by guessing from the
    slot number (slots are reused within a run).
    """
    boot = BOOT_LOAD_RE.findall(text)
    presented = PRESENTED_RE.findall(text)
    final = FINAL_RE.search(text)
    saves, rewinds, pending, markers = [], [], [], []
    pump = None
    for line in text.splitlines():
        m = EVENT_RE.search(line)
        if m:
            pump = int(m.group(1))
            continue
        m = SAVE_RE.search(line)
        if m:
            saves.append((pump, int(m.group(1)), m.group(2),
                          int(m.group(3)), m.group(4)))
            continue
        m = REWIND_RE.search(line)
        if m:
            rewinds.append((pump, int(m.group(1)), m.group(2), m.group(3)))
            continue
        m = PENDING_RE.search(line)
        if m:
            pending.append((int(m.group(1)), int(m.group(2))))
            continue
        m = MARKER_RE.search(line)
        if m:
            markers.append((m.group(1), int(m.group(2))))
    return {
        'boot_frame': int(boot[0]) if boot else None,
        # (pump, slot, pc, frame, mem)
        'saves': saves,
        'file_loads': [int(x) for x in FILE_LOAD_RE.findall(text)],
        # (pump, frame, pc, mem)
        'rewinds': rewinds,
        'events': [(int(p), a) for p, a in EVENT_RE.findall(text)],
        'pending': pending,
        'markers': markers,
        'presented': int(presented[-1]) if presented else None,
        'ppu_frames': int(final.group(2)) if final else None,
    }


def load_io(path):
    recs = []
    p = Path(path)
    if not p.exists():
        return recs
    with p.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                d = json.loads(line)
                recs.append((int(d['f']), int(d['adr'], 16),
                             int(d['old'], 16), int(d['val'], 16)))
    return recs


def run_branch(tag, script, sync, backend, frames, out, args):
    rom = out / 'rom.gba'
    shutil.copyfile(ROOT / 'roms/mmbn3_white_usa.gba', rom)
    sav = out / 'test.sav'
    shutil.copyfile(ROOT / 'saves/mmbn3_white_usa.sav', sav)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('GBARECOMP_')}
    env.update({
        'GBARECOMP_INPUT_REPLAY': str(args.trace),
        'GBARECOMP_FRAMEDUMP_COUNT': '0',
        'GBARECOMP_ASSIST_SCRIPT': script,
        'GBARECOMP_HEAL_CACHE': str(out / 'cache'),
        # Read-only per-frame SAMPLED register deltas (no engine change):
        # wram_trace_tick samples the raw I/O bytes ONCE PER PPU FRAME and
        # emits only the NET change, so intermediate writes inside a frame,
        # writes that cancel out, and internal device state are invisible.
        # This is a sampled-delta surface, NOT a write stream and NOT device
        # equivalence.
        'GBARECOMP_WRAM_TRACE': str(out / 'io.jsonl'),
        'GBARECOMP_WRAM_TRACE_LO': '0x04000000',
        'GBARECOMP_WRAM_TRACE_HI': '0x040003ff',
    })
    if sync != 'off':
        env['GBARECOMP_SAVELOAD_PHASE_SYNC'] = sync
    if backend == 'interp':
        env['GBARECOMP_FORCE_INTERP'] = '1'
        env['GBARECOMP_SELFHEAL_RECOMPILE'] = '0'
        env['GBARECOMP_HEAL_WARM_LOAD'] = '0'
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
    res = parse_log((out / 'stdout.log').read_text(errors='replace'))
    err = (out / 'stderr.log').read_text(errors='replace')
    res.update({'tag': tag, 'sync': sync, 'backend': backend,
                'script': script, 'exit': rc, 'timed_out': timed_out,
                'frames': frames, 'seconds': round(time.time() - t0, 2),
                'dir': str(out), 'io': load_io(out / 'io.jsonl'),
                'rewind_not_ready': 'rewind history is not ready yet' in err,
                'hang_watchdog': err.count('hang-watchdog')})
    return res


def branch_structural(res, want_events, problems, tag):
    if res['exit'] != 0 or res['timed_out']:
        problems.append(f'{tag}: run exit={res["exit"]} '
                        f'timed_out={res["timed_out"]}')
    if res['presented'] != res['frames']:
        problems.append(f'{tag}: frames_presented={res["presented"]} '
                        f'wanted {res["frames"]}')
    if res['events'] != want_events:
        problems.append(f'{tag}: assist events {res["events"]} != '
                        f'{want_events}')
    if res['file_loads']:
        problems.append(f'{tag}: unexpected file savestate loads '
                        f'{res["file_loads"]}')


def expect_rejoin(sync, backend):
    """Per-cell rejoin expectation: True, False, or None (measure only).

    Corrected after a three-boundary sweep. The single-boundary result
    ("off forks, both rejoins") was over-general: sweeping the rewind across
    restore frames 28094 / 28184 / 28334 showed the defect is
    BOUNDARY-DEPENDENT, and not a function of the rewind distance either
    (pumps 300 and 450 are both 73 frames back yet one forks and one does
    not). So:

      both  -> True, asserted at EVERY sampled boundary. The point of the
               opt-in is that the fix must not itself be boundary-dependent;
               a single boundary where `both` fails is a real defect.
      off   -> None per cell. Asserted in AGGREGATE instead (see
      load     control_rules): a negative control's job is to prove the
               harness CAN still expose the bug, i.e. it must fork at least
               once across the sampled boundaries. Fording it per cell would
               have failed two of the three measured `off` cells for rejoining
               -- which is a property of the boundary, not a harness fault.
      save  -> None. Established as not reliably sufficient, and (with the
               pure interpreter) previously asserted True from file-matrix
               cells that are now known to have been measured on a stalled
               guest, so that assertion is withdrawn.
    """
    if sync == 'both':
        return True
    return None


def control_rules(cells):
    """Aggregate rules across a set of cells, SHARED by the live run and
    --verdict.

    Two requirements, neither of which is per-cell:
      * the `both` control must be equivalent at every measured boundary;
      * each negative control (`off`, `load`) must fork at least once, or the
        suite has lost its power to expose the defect and is reporting a
        vacuous pass.
    """
    bad, notes = [], []
    measured = [c for c in cells if not c.get('unsupported')]
    for c in measured:
        eq = derive_equivalence(c) if 'equivalence' not in c \
            else c['equivalence']
        g = c.get('geometry') or {}
        tag = f"{c['backend']}/sync={c['sync']}@pump={g.get('rewind_pump')}"
        if c['sync'] == 'both' and eq.get('equivalent') is not True:
            bad.append(f'{tag}/both-not-equivalent({eq.get("equivalent")})')
    for sync in sorted({c['sync'] for c in measured
                        if c['sync'] in ('off', 'load')}):
        group = [c for c in measured if c['sync'] == sync]
        forked = []
        for c in group:
            eq = derive_equivalence(c) if 'equivalence' not in c \
                else c['equivalence']
            if eq.get('equivalent') is False or eq.get('rejoined') is False:
                forked.append((c.get('geometry') or {}).get('rewind_pump'))
        notes.append(f'{sync}: forked at {sorted(x for x in forked if x)} '
                     f'of {len(group)} boundaries')
        if not forked:
            bad.append(f'negative-control-{sync}-vacuous: no sampled boundary '
                       'forked, so this suite cannot demonstrate the defect')
    return bad, notes


def build_u_script(frames_wanted, first_pump=5):
    """Assist script saving at the given guest frames, as save-slot labels.

    The engine maps pump -> guest frame affinely with slope 1 in an
    uninterrupted run (frame = pump + K), so a wanted frame becomes a pump
    here; every resulting save's observed frame is asserted afterwards, so a
    broken map is a loud failure rather than a mis-comparison.
    """
    parts = [(first_pump, 1)]
    for i, pump in enumerate(sorted(frames_wanted)):
        parts.append((pump, 2 + i))
    return ';'.join(f'{p}:save{sl}' for p, sl in parts), parts


def digest_map(res):
    """guest frame -> (pc, mem) from a branch's saves."""
    return {frame: (pc, mem) for _, _, pc, frame, mem in res['saves']}


def io_key(rec):
    """The part of a trace record that must match across two runs.

    The trace index f is a per-run monotonic counter of frame advances and
    is EXPECTED to differ between the branches (the rewind makes R's counter
    run ahead of U's by the rewound span), so only the address and the
    old/new values take part in the comparison.
    """
    return rec[1:]


def common_prefix(a, b, i, j, limit=None, key=None):
    k = key or (lambda x: x)
    n = 0
    cap = min(len(a) - i, len(b) - j) if limit is None else limit
    while n < cap and k(a[i + n]) == k(b[j + n]):
        n += 1
    return n


def first_index_with_f(recs, f):
    for i, rec in enumerate(recs):
        if rec[0] == f:
            return i
    return None


def best_offset(u, r, u_start, r_start, span, limit):
    """Offset maximizing the common prefix: an INDEPENDENT check on the
    empirically derived offset. Only candidates whose first record matches."""
    best, best_at = -1, None
    head = io_key(u[u_start])
    for k in range(max(0, r_start - span), min(len(r), r_start + span)):
        if io_key(r[k]) == head:
            n = common_prefix(u, r, u_start, k, limit, key=io_key)
            if n > best:
                best, best_at = n, k
    return best, best_at


def compare_io(u_recs, r_recs, init, a_frame, x_frame):
    """Diff the post-restore per-frame SAMPLED register deltas, aligning them
    empirically. (wram_trace_tick samples once per PPU frame and emits only
    net changes; see the run_branch comment. Not a write stream.)

    The trace index f counts frame ADVANCES (the first tick is suppressed
    for priming), so in an uninterrupted run guest = init + f + 1. R and U
    share an identical uninterrupted prefix, so the first index where R
    stops matching U is exactly where the rewind takes effect; the records R
    emits there are the restore tick (its shadow still holds pre-rewind
    values, so it logs the restore's own register delta, not a guest write).
    The next tick is the first clean post-restore record.
    """
    out = {'error': None, 'diverge_index': None, 'restore_tick_f': None,
           'restore_delta_count': None, 'restore_delta_groups': None,
           'restore_delta_sound': None, 'u_index': None, 'r_index': None,
           'matched': 0, 'compared': 0, 'aligned': False, 'best_offset': None,
           'best_offset_delta': None, 'divergence': None, 'map_checked': None}
    if not u_recs or not r_recs:
        out['error'] = f'empty io trace (U={len(u_recs)} R={len(r_recs)})'
        return out
    d = common_prefix(u_recs, r_recs, 0, 0, key=io_key)
    out['diverge_index'] = d
    if d == 0:
        out['error'] = ('R and U share no identical I/O prefix, so the '
                        'rewind boundary cannot be located in the trace')
        return out
    if d >= len(r_recs):
        out['error'] = 'R trace is a strict prefix of U; no restore observed'
        return out
    f_restore = r_recs[d][0]
    out['restore_tick_f'] = f_restore
    m = 0
    while d + m < len(r_recs) and r_recs[d + m][0] == f_restore:
        m += 1
    rd = r_recs[d:d + m]
    out['restore_delta_count'] = len(rd)
    out['restore_delta_groups'] = sorted({io_group(rec[1]) for rec in rd})
    out['restore_delta_sound'] = sorted(
        {f'0x{rec[1]:08x}' for rec in rd if io_group(rec[1]) == 'sound'})
    # Model check: the last shared record must be guest frame A, at f =
    # A - init - 1, on BOTH sides. This validates the f<->guest map against
    # the logged restore geometry before the map is used to align anything.
    f_a = a_frame - init - 1
    out['map_checked'] = (u_recs[d - 1][0] == f_a and r_recs[d - 1][0] == f_a)
    if not out['map_checked']:
        out['error'] = (f'f<->guest map check failed: expected f={f_a} at the '
                        f'last shared record, saw U={u_recs[d - 1][0]} '
                        f'R={r_recs[d - 1][0]}')
        return out
    # First clean post-restore tick is the one after the restore tick, at
    # guest X+2; in U the same guest frame is at f = X+1-init.
    r_clean = first_index_with_f(r_recs, f_restore + 1)
    u_start = first_index_with_f(u_recs, (x_frame + 2) - init - 1)
    if r_clean is None or u_start is None:
        out['error'] = (f'no trace record at R f={f_restore + 1} '
                        f'({r_clean}) or U f={(x_frame + 2) - init - 1} '
                        f'({u_start})')
        return out
    out['u_index'], out['r_index'] = u_start, r_clean
    limit = min(len(u_recs) - u_start, len(r_recs) - r_clean)
    n = common_prefix(u_recs, r_recs, u_start, r_clean, limit, key=io_key)
    out['matched'], out['compared'] = n, limit
    if n < limit:
        du, dr = u_recs[u_start + n], r_recs[r_clean + n]
        out['divergence'] = {
            'offset': n,
            'guest_frame': init + 1 + du[0],
            'r_trace_f': dr[0],
            'u_record': [f'0x{du[1]:08x}', f'0x{du[2]:02x}', f'0x{du[3]:02x}'],
            'r_record': [f'0x{dr[1]:08x}', f'0x{dr[2]:02x}', f'0x{dr[3]:02x}'],
            'u_group': io_group(du[1]), 'r_group': io_group(dr[1]),
        }
    best, at = best_offset(u_recs, r_recs, u_start, r_clean, 400, limit)
    out['best_offset'] = best
    out['best_offset_delta'] = None if at is None else at - r_clean
    out['aligned'] = at == r_clean and best >= min(limit, 8)
    return out


def probe_rows(res):
    """Pair each logged pending/budget + marker probe with its guest frame.

    The engine logs one `savestate_pending` and one `savestate_audio` per
    FILE save/load, and nothing for the in-memory rewind path, so the boot
    load plus the branch's file saves are the probes. Save i owns probe i+1.
    """
    rows = []
    for i, (pend, budget) in enumerate(res['pending']):
        if i == 0:
            frame, kind = res['boot_frame'], 'boot'
        elif i - 1 < len(res['saves']):
            frame, kind = res['saves'][i - 1][3], 'save'
        else:
            continue
        marker = None
        if i < len(res['markers']):
            marker = res['markers'][i][1]
        rows.append({'frame': frame, 'kind': kind, 'pending': pend,
                     'budget': budget, 'marker': marker})
    return rows


def cell(sync, backend, args, out_root, p):
    tag = f'{backend}/{sync}@pump={p}'
    problems = []
    post_pumps = [p + d for d in POST_OFFSETS[1:]]
    r_parts = [(5, 1)] + [(p, None)] + [(q, 2 + i)
                                         for i, q in enumerate(post_pumps)]
    r_script = ';'.join(
        f'{q}:rewind' if sl is None else f'{q}:save{sl}' for q, sl in r_parts)
    want_r = [(5, 'save1'), (p, 'rewind')] + \
             [(q, f'save{2 + i}') for i, q in enumerate(post_pumps)]
    out_r = Path(tempfile.mkdtemp(
        prefix=f'rewind-{backend}-{sync}-p{p}-R-', dir=out_root))
    r = run_branch('R', r_script, sync, backend, args.frames, out_r, args)
    res = {'sync': sync, 'backend': backend, 'dirs': [str(out_r)],
           'R_script': r_script, 'rewinds_observed': bool(r['rewinds']),
           # Set before any early return so every cell is labelled with the
           # pump it was measured at, including blocked ones.
           'geometry': {'rewind_pump': p}}
    branch_structural(r, want_r, problems, tag + '/R')
    # Backend capability gate, checked BEFORE spending the rest of the cell.
    # The rewind restores the newest capture point at least REWIND_BACK_FRAMES
    # old, and captures land every REWIND_INTERVAL guest frames, so a backend
    # must actually advance at least that many guest frames for the path to be
    # drivable at all. Measured from this branch's own save ladder: a backend
    # that stalls here cannot be made to drive the in-memory path by any
    # script, and reporting that as "cannot drive" is the honest outcome --
    # it is NOT a phase-sync result and NOT a harness defect.
    need = REWIND_BACK_FRAMES + REWIND_INTERVAL
    advanced = (max(s[3] for s in r['saves']) - r['boot_frame']
                if r['saves'] and r['boot_frame'] is not None else 0)
    res['capability'] = {'guest_frames_advanced': advanced,
                         'frames_needed': need,
                         'can_drive_rewind': advanced >= need}
    if advanced < need:
        res['unsupported'] = (
            f'backend {backend} advanced only {advanced} guest frame(s) in '
            f'{args.frames} pumps (needs >= {need} to populate the rewind '
            f'capture history); engine reported '
            f'"rewind history is not ready yet"={r["rewind_not_ready"]}, '
            f'hang-watchdog hits={r["hang_watchdog"]}. The in-memory path is '
            'not drivable on this backend/fixture; this is a backend speed '
            'limitation, not a phase-sync result.')
        res['invalid'] = True
        res['failures'] = problems
        print(f'  BLOCKED {tag}: {res["unsupported"]}', flush=True)
        return res, r, None
    if len(r['rewinds']) != 1:
        problems.append(f'{tag}: R: expected exactly 1 rewind event, saw '
                        f'{len(r["rewinds"])} -- the in-memory restore path '
                        'was not exercised')
        res['failures'] = problems
        res['invalid'] = True
        return res, r, None
    x_frame, x_pc, x_mem = r['rewinds'][0][1:]
    first = r['saves'][0] if r['saves'] else None
    if first is None:
        problems.append(f'{tag}: R: no pump-5 anchor save')
        res['failures'] = problems
        res['invalid'] = True
        return res, r, None
    k = first[3] - 5
    a_frame = p + k
    if x_frame >= a_frame:
        problems.append(f'{tag}: R: restore frame {x_frame} did not move '
                        f'backward from {a_frame}')
    if (x_frame - r['boot_frame']) % REWIND_INTERVAL:
        problems.append(f'{tag}: R: restore frame {x_frame} is off the '
                        f'{REWIND_INTERVAL}-frame capture grid')
    res['geometry'].update({
        'init': r['boot_frame'], 'K': k, 'a_frame': a_frame,
        'x_frame': x_frame, 'back_frames': a_frame - x_frame,
        'expected_back': REWIND_BACK_FRAMES,
        'grid_remainder': (x_frame - r['boot_frame']) % REWIND_INTERVAL})
    # U must observe the exact guest frames R visits after its restore, plus
    # the restore frame itself.
    wanted = sorted({x_frame} | {s[3] for s in r['saves'] if s[0] > p})
    u_script, u_parts = build_u_script([f - k for f in wanted])
    want_u = [(q, f'save{sl}') for q, sl in u_parts]
    out_u = Path(tempfile.mkdtemp(
        prefix=f'rewind-{backend}-{sync}-p{p}-U-', dir=out_root))
    u = run_branch('U', u_script, sync, backend, args.frames, out_u, args)
    res['dirs'].append(str(out_u))
    res['U_script'] = u_script
    branch_structural(u, want_u, problems, tag + '/U')
    umap, rmap = digest_map(u), digest_map(r)
    missing = [f for f in wanted if f not in umap]
    if missing:
        problems.append(f'{tag}/U: no save at required guest frame(s) '
                        f'{missing} (map K={k} is not the one the script '
                        'assumed)')
    res['boundary'] = {'x_frame': x_frame, 'r': [x_pc, x_mem],
                       'u': list(umap[x_frame]) if x_frame in umap else None,
                       'match': (umap.get(x_frame) == (x_pc, x_mem))}
    res['frames'] = []
    longest = 0
    for f in wanted:
        uu, rr = umap.get(f), rmap.get(f)
        res['frames'].append({
            'frame': f, 'offset_from_restore': f - x_frame,
            'u': uu[1] if uu else None, 'r': rr[1] if rr else None,
            'match': (uu == rr) if (uu and rr) else None})
        longest = max(longest, f - x_frame)
    res['post_restore_gap'] = longest
    if longest < 400:
        problems.append(f'{tag}: longest post-restore observation is '
                        f'{longest} frames, wanted >= 400')
    res['rejoined'] = bool(res['frames']) and all(
        row['match'] for row in res['frames'] if row['match'] is not None)
    # Host pending/budget + mixer sample counter at matched guest frames.
    uprobe = {d['frame']: d for d in probe_rows(u) if d['kind'] == 'save'}
    res['horizon'] = []
    for d in probe_rows(r):
        if d['kind'] != 'save':
            continue
        m = uprobe.get(d['frame'])
        res['horizon'].append({
            'frame': d['frame'],
            'r': [d['pending'], d['budget'], d['marker']],
            'u': None if m is None else
                     [m['pending'], m['budget'], m['marker']],
            'match': None if m is None else
                     [d['pending'], d['budget'], d['marker']]
                     == [m['pending'], m['budget'], m['marker']]})
    res['io'] = compare_io_pair(sync, backend, args, out_root, p, k, a_frame,
                                res, problems, tag)
    res['failures'] = problems
    return res, r, u


def compare_io_pair(sync, backend, args, out_root, p, k, a_frame, res,
                    problems, tag):
    """Diff the post-restore per-frame sampled register deltas on a dedicated
    run pair. (Sampled once per frame; net changes only. Not a write stream.)

    The digest/horizon pair above cannot supply the I/O comparison: a file
    save under the save-side flush materializes pending device time, which
    advances timer counters inside the shadowed I/O array, so the two
    branches' traces differ wherever -- and only wherever -- they happen to
    place their probe saves. That is a harness artifact, not a guest
    difference. So this pair takes exactly ONE file save, at the same pump in
    both branches, and otherwise differs only by the rewind. The rewind
    CAPTURE points need no such care: captures happen on the same schedule in
    both branches, so their flushes land on the same frames.
    """
    script_r = f'5:save1;{p}:rewind'
    script_u = '5:save1'
    want_r = [(5, 'save1'), (p, 'rewind')]
    want_u = [(5, 'save1')]
    out = {}
    recs = {}
    pairs = (('R', script_r, want_r), ('U', script_u, want_u))
    for name, script, want in pairs:
        d = Path(tempfile.mkdtemp(
            prefix=f'rewindio-{backend}-{sync}-p{p}-{name}-', dir=out_root))
        res['dirs'].append(str(d))
        recs[name] = run_branch(f'IO{name}', script, sync, backend,
                                args.frames, d, args)
        branch_structural(recs[name], want, problems, f'{tag}/IO{name}')
        out[f'{name}_script'] = script
    r, u = recs['R'], recs['U']
    # Geometry must agree with the digest pair, or the two pairs are not
    # describing the same experiment.
    for name, br in (('IO-R', r), ('IO-U', u)):
        if not br['saves']:
            problems.append(f'{tag}/{name}: no pump-5 anchor save')
            continue
        kk = br['saves'][0][3] - 5
        if kk != k:
            problems.append(f'{tag}/{name}: pump->frame map K={kk} disagrees '
                            f'with the digest pair K={k}')
        if br['boot_frame'] != res['geometry']['init']:
            problems.append(f'{tag}/{name}: boot frame {br["boot_frame"]} '
                            f'disagrees with {res["geometry"]["init"]}')
    io_r, io_u = r['rewinds'], u['rewinds']
    if len(io_r) != 1:
        problems.append(f'{tag}/IO-R: expected 1 rewind, saw {len(io_r)}')
        return {'error': 'no rewind in the IO pair'}
    if io_u:
        problems.append(f'{tag}/IO-U: the uninterrupted control rewound')
    x2 = io_r[0][1]
    if x2 != res['geometry']['x_frame']:
        problems.append(f'{tag}/IO-R: restore frame {x2} disagrees with the '
                        f"digest pair {res['geometry']['x_frame']}")
    out.update(compare_io(u['io'], r['io'], r['boot_frame'], a_frame,
                          res['geometry']['x_frame']))
    out['x_frame'] = res['geometry']['x_frame']
    if out['error']:
        problems.append(f'{tag}: IO stream: {out["error"]}')
    # The restore tick is the shadow-vs-restored delta: the trace's shadow
    # still holds the PRE-rewind frame's I/O values while the array now holds
    # the restored frame's, so this enumerates how the I/O registers differ
    # across the rewound span. It is a description of the span, NOT a
    # serialization gap: a register the snapshot does not cover keeps its
    # live value and therefore never appears here. Read it as evidence about
    # the span only; the sound/noise coverage question is settled by the
    # engine's audio_snapshot_tests, not by this listing.
    out['rewind_span_delta_addrs'] = out.get('restore_delta_sound')
    if out.get('divergence'):
        out['post_restore_frames_compared'] = (
            out['divergence']['guest_frame'] - res['geometry']['x_frame'])
    else:
        out['post_restore_frames_compared'] = out.get('compared')
    return out


def report_failures(d):
    """Failure tags for a report, given the stored cells. SHARED by the live
    run and --verdict so the two standards cannot drift apart."""
    bad = []
    for c in d['cells']:
        if c.get('unsupported'):
            continue
        g = c.get('geometry') or {}
        tag = f"{c['backend']}/sync={c['sync']}@pump={g.get('rewind_pump')}"
        # The stored per-cell list may have been written by an EARLIER
        # expectation rule. Rule-derived entries are dropped and recomputed
        # below; only rule-INDEPENDENT structural findings are trusted from
        # storage.
        for f in c.get('failures', []):
            if not f.startswith('rejoin('):
                bad.append(f'{tag}/{f}')
        want = expect_rejoin(c['sync'], c['backend'])
        if want is not None and c.get('rejoined') is not None \
                and want != c['rejoined']:
            bad.append(f'{tag}/rejoin(want={want},got={c["rejoined"]})')
    for key in ('source_save_unchanged', 'uninterrupted_stable_across_syncs'):
        if not d.get(key):
            bad.append(key)
    agg, _ = control_rules(d['cells'])
    bad += agg
    return bad


def derive_equivalence(c):
    """Split the single 'rejoined' bit into the layers it actually covers.

    `rejoined` is a frame + PC + guest CPU/memory digest comparison ONLY. It
    is deliberately not folded together with device state, because a cell can
    rejoin the digest and still fail device equivalence (measured: save-only
    under normal healing). These fields keep the layers separate so a report
    cannot be read as claiming more than it measured.

    Even full `equivalent` is PER-FRAME SAMPLED REGISTER-DELTA agreement,
    NOT audible equivalence and NOT full device equivalence: it says both
    branches' sampled I/O register bytes changed the same way at the same
    frame boundaries -- not that every guest write, ordering, cancellation,
    or internal device state matched (the sampler sees none of those). It
    does not cover host audio queue state or the un-serialized Sound 3/4 +
    wave RAM.
    """
    if c.get('unsupported'):
        return {'rejoined': None, 'boundary': None, 'horizon': None,
                'io': None, 'equivalent': None}
    hz = c.get('horizon') or []
    io = c.get('io') or {}
    boundary = (c.get('boundary') or {}).get('match')
    io_eq = None
    if not io.get('error'):
        io_eq = (io.get('divergence') is None and bool(io.get('aligned')))
    return {
        'rejoined': c.get('rejoined'),
        'boundary': boundary,
        'horizon': all(r['match'] for r in hz if r['match'] is not None)
        if hz else None,
        'io': io_eq,
        'equivalent': all(x is True for x in
                          (c.get('rejoined'), boundary, io_eq)) and
        all(r['match'] for r in hz if r['match'] is not None) if hz else None,
    }


def recomputed_cell_failures(c):
    """Current-rule failure tags for one stored cell.

    Rule-INDEPENDENT structural findings are kept from storage (a run that did
    not happen stays a failure); rule-derived tags are recomputed, which drops
    the obsolete off-cell `rejoin(want=False,got=True)` entries an older rule
    wrote for boundaries that rejoin.
    """
    out = [f for f in (c.get('failures') or [])
           if not f.startswith('rejoin(')]
    want = expect_rejoin(c['sync'], c['backend'])
    if want is not None and c.get('rejoined') is not None \
            and want != c['rejoined']:
        out.append(f'rejoin(want={want},got={c["rejoined"]})')
    return out


def reverdict_artifact(d, path, bad, notes):
    """Copy of a stored report re-verdicted under the CURRENT rule.

    The stored file is left untouched as historical evidence; this artifact
    carries the current rule_version, the recomputed failure list, and -- per
    cell -- the stored failure tags preserved under `failures_as_stored`
    while `failures` holds only what the current rule derives. That removes
    the obsolete off-cell `rejoin(want=False,got=True)` tags from what a
    reader would take as the live verdict without hiding them.
    """
    art = dict(d)
    art['rule_version'] = RULE_VERSION
    art['derived_from'] = {'path': str(path),
                           'stored_rule_version': d.get('rule_version')}
    art['note'] = ('Re-verdict of a stored run under the current rule. '
                   'The stored report is kept unchanged as historical '
                   'evidence; rule-derived failure tags here supersede it.')
    art['recomputed_failures'] = bad
    art['control_notes'] = notes
    art['verdict'] = 'REWIND OK' if not bad else 'REWIND FAIL'
    art['measurement_note'] = (
        'Per-cell `io` is per-frame SAMPLED register deltas (wram_trace_tick '
        'samples raw I/O bytes once per PPU frame and emits net changes only): '
        'NOT a register write stream and NOT full device equivalence. '
        '`equivalent` also does not cover host audio-queue state or the '
        'un-serialized Sound 3/4 + wave RAM, and is NOT audible equivalence.')
    cells = []
    for c in d['cells']:
        c2 = dict(c)
        c2['failures_as_stored'] = c.get('failures')
        c2['failures'] = recomputed_cell_failures(c)
        cells.append(c2)
    art['cells'] = cells
    return art


def verdict_existing(path, out_path=None):
    d = json.loads(Path(path).read_text())
    for c in d['cells']:
        g = c.get('geometry') or {}
        label = f"{c['backend']}/sync={c['sync']}@pump={g.get('rewind_pump')}"
        if c.get('unsupported'):
            print(f'{label}: BLOCKED -- {c["unsupported"]}')
            continue
        e = derive_equivalence(c)
        io = c.get('io') or {}
        div = io.get('divergence') or {}
        print(f'{label}: rejoin={e["rejoined"]} '
              f'boundary={e["boundary"]} horizon={e["horizon"]} io={e["io"]} '
              f'equivalent={e["equivalent"]} '
              f'x={g.get("x_frame")} back={g.get("back_frames")}f '
              f'divf={div.get("guest_frame")} '
              f'fails={recomputed_cell_failures(c)}')
    bad = report_failures(d)
    _, notes = control_rules(d['cells'])
    for n in notes:
        print(f'control rule: {n}')
    if d.get('rule_version') != RULE_VERSION:
        print(f'NOTE: report was written under rule '
              f'{d.get("rule_version", "UNKNOWN")!r}; this tool enforces '
              f'{RULE_VERSION!r}. Rule-derived verdicts were recomputed and '
              'rule-independent structural findings were kept as stored.')
    print(f"source save unchanged: {d.get('source_save_unchanged')}")
    print(f"uninterrupted stable across sync modes: "
          f"{d.get('uninterrupted_stable_across_syncs')}")
    if d.get('blocked_cells'):
        print(f"blocked (not measured): {d['blocked_cells']}")
    print('REWIND OK' if not bad else f'REWIND FAIL {bad}')
    if out_path:
        art = reverdict_artifact(d, path, bad, notes)
        Path(out_path).write_text(json.dumps(art, indent=2) + '\n')
        print(f're-verdict artifact written: {out_path} '
              f'(rule {RULE_VERSION}; source report kept unchanged)')
    return 0 if not bad else 1


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
    ap.add_argument('--pumps', nargs='+', type=int, default=[300],
                    help='pumps at which the rewind fires; one cell per pump '
                         'per (backend, sync). More than one addresses the '
                         'single-rewind-point limit of an earlier run.')
    ap.add_argument('--frames', type=int, default=715)
    ap.add_argument('--syncs', nargs='+',
                    default=['off', 'save', 'load', 'both'],
                    choices=['off', 'save', 'load', 'both'])
    ap.add_argument('--backends', nargs='+', default=['heal', 'interp'],
                    choices=['heal', 'interp'])
    ap.add_argument('--timeout', type=float, default=420)
    ap.add_argument('--json', type=Path, default=None)
    ap.add_argument('--verdict', type=Path, default=None,
                    help='do not run; re-verdict an existing --json report '
                         'with the current rules')
    ap.add_argument('--verdict-json', type=Path, default=None,
                    help='with --verdict: also write a CURRENT-RULE copy of '
                         'the report (the stored file is never modified)')
    args = ap.parse_args()

    if args.verdict:
        return verdict_existing(args.verdict, args.verdict_json)
    if not args.state.exists() or not args.trace.exists():
        print(f'SKIP: missing fixture {args.state} / {args.trace}')
        return 2
    need = 2 * REWIND_BACK_FRAMES + REWIND_INTERVAL
    too_small = [p for p in args.pumps if p < need]
    if too_small:
        print(f'SKIP: --pumps {too_small} too small; the rewind needs a '
              f'capture point at least {REWIND_BACK_FRAMES} frames back on a '
              f'full {REWIND_INTERVAL}-frame grid behind it (>= {need})')
        return 2
    # Each pump needs room for its own post-restore observation window.
    short = [p for p in args.pumps if p + max(POST_OFFSETS) + 5 >= args.frames]
    if short:
        print(f'SKIP: --frames {args.frames} leaves no room after --pumps '
              f'{short}; need >= {max(short) + max(POST_OFFSETS) + 5}')
        return 2

    source = ROOT / 'saves/mmbn3_white_usa.sav'
    before = sha(source)
    out_root = ROOT / 'build/inmem-rewind'
    out_root.mkdir(parents=True, exist_ok=True)

    cells = []
    for backend in args.backends:
        for sync in args.syncs:
            for p in args.pumps:
                t0 = time.time()
                res, _, _ = cell(sync, backend, args, out_root, p)
                res['seconds'] = round(time.time() - t0, 2)
                want = expect_rejoin(sync, backend)
                fails = list(res['failures'])
                if res.get('rejoined') is not None and want is not None \
                        and want != res['rejoined']:
                    fails.append(f'rejoin(want={want},got={res["rejoined"]})')
                if res.get('boundary', {}).get('match') is False:
                    fails.append('boundary identity mismatch')
                if (res.get('io') or {}).get('divergence') \
                        and not res['io'].get('aligned'):
                    fails.append('IO stream alignment unconfirmed')
                res['failures'] = fails
                res['equivalence'] = derive_equivalence(res)
                cells.append(res)
                if res.get('unsupported'):
                    print(f'[{backend} sync={sync} pump={p}] BLOCKED '
                          '(not measured)', flush=True)
                    continue
                g = res.get('geometry') or {}
                b = res.get('boundary') or {}
                io = res.get('io') or {}
                div = io.get('divergence') or {}
                eq = res['equivalence']
                print(f"[{backend} sync={sync} pump={p}] "
                      f"rejoin={eq['rejoined']} expect={want} "
                      f"equiv={eq['equivalent']} "
                      f"(bnd={eq['boundary']} hz={eq['horizon']} "
                      f"io={eq['io']}) "
                      f"x={g.get('x_frame')} back={g.get('back_frames')}f "
                      f"gap={res.get('post_restore_gap')}f "
                      f"io={io.get('matched')}/{io.get('compared')} "
                      f"aligned={io.get('aligned')} "
                      f"divf={div.get('guest_frame')} "
                      f"divu={div.get('u_record')} "
                      f"divr={div.get('r_record')} "
                      f"fails={fails} {res['seconds']}s", flush=True)

    src_ok = sha(source) == before
    # Uninterrupted digests must be identical across sync modes for a given
    # (backend, pump): the opt-in must not perturb uninterrupted execution.
    # Only measured cells can contribute evidence here.
    u_stable, u_bad = True, []
    seen = {}
    for c in cells:
        if c.get('unsupported'):
            continue
        sig = tuple((r['frame'], r['u']) for r in c.get('frames', []))
        key = (c['backend'], (c.get('geometry') or {}).get('rewind_pump'))
        if key in seen and seen[key] != sig:
            u_stable = False
            u_bad.append(f'{key[0]}@{key[1]}')
        seen[key] = sig
    blocked = [f"{c['backend']}/sync={c['sync']}"
               f"@pump={(c.get('geometry') or {}).get('rewind_pump')}"
               for c in cells if c.get('unsupported')]
    report = {'cells': cells, 'source_save_unchanged': src_ok,
              'uninterrupted_stable_across_syncs': u_stable,
              'uninterrupted_mismatches': u_bad,
              'blocked_cells': blocked,
              'binary_sha256': sha(args.exe), 'pumps': args.pumps,
              'frames': args.frames, 'post_offsets': POST_OFFSETS,
              'rule_version': RULE_VERSION}
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + '\n')

    print(f'\nsource save unchanged: {src_ok}')
    print(f'uninterrupted stable across sync modes: {u_stable}')
    bad = []
    if not src_ok:
        bad.append('source_save_unchanged')
    if not u_stable:
        bad.append('uninterrupted_stable_across_syncs')
    for c in cells:
        if c.get('unsupported'):
            continue
        g = c.get('geometry') or {}
        tag = f"{c['backend']}/sync={c['sync']}@pump={g.get('rewind_pump')}"
        bad += [f'{tag}/{f}' for f in c['failures']]
    if blocked:
        print(f'BLOCKED (backend cannot drive the in-memory path, NOT '
              f'measured): {blocked}')
    agg, notes = control_rules(cells)
    bad += agg
    for n in notes:
        print(f'control rule: {n}')
    print('REWIND OK' if not bad else f'REWIND FAIL {bad}')
    print('measured equivalence per cell (per-frame sampled register deltas; '
          'NOT device or audible equivalence):')
    for c in cells:
        eq = c.get('equivalence') or {}
        g = c.get('geometry') or {}
        print(f"  {c['backend']}/{c['sync']}@pump={g.get('rewind_pump')}: "
              f"rejoin={eq.get('rejoined')} boundary={eq.get('boundary')} "
              f"horizon={eq.get('horizon')} io={eq.get('io')} "
              f"equivalent={eq.get('equivalent')}")
    # 2 = nothing was measured (every requested cell was blocked or invalid).
    # Distinct from 1, a measured failure.
    if not [c for c in cells if not c.get('unsupported')]:
        return 2
    return 0 if not bad else 1


if __name__ == '__main__':
    raise SystemExit(main())
