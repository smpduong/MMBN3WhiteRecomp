#!/usr/bin/env python3
"""Real SRAM save -> clean quit -> NEW process -> Continue (criterion B2).

Separate from debugger save-states: this exercises the game's own save path
(SRAM bytes on disk) across a genuine process boundary.

Flow (all evidence under build/rt_sram/<stamp>/):
 1. copy SOURCE save -> test.sav (SOURCE hashed before/after, never written);
    isolated heal cache seeded warm from the shared cache (write-isolated)
 2. proc1: explicit game.toml + --save/--rom/--bios/--tcp; stdout/stderr
    retained; cwd=run dir; monotonic deadlines; child-PID-only teardown
 3. continue, title-stable gate (bounded), START -> menu, A -> net scene
    (scene identity checked as in roundtrip_check.py)
 4. record pre-save evidence: BG scroll regs, screenshot, test.sav bytes hash
 5. bounded save-menu exploration: fixed matrix of opener+cursor+A sequences
    (each followed by a settle wait past the ~1s periodic save-flush window);
    after every attempt hash test.sav. First hash change wins; all attempts,
    screenshots and hashes are logged. If nothing changes the file, the run
    FAILS as BLOCKED (no in-game save staged) with full evidence.
 6. on a save: confirm through any completion dialog (bounded A taps until the
    file hash is stable 5s), clean quit (pause+quit, patient bounded wait,
    exit 0 required; forced termination fails the run). TCP sessions flush
    SRAM at EXIT (debug path keeps exit-only flush), so the save bytes are
    hashed AFTER p1 exits and must differ from the source (S18: 310 bytes).
 7. proc2: genuinely new process with the SAME test.sav; Continue; verify the
    save bytes are stable across relaunch+load, and the AT-ENTRY area matches
    the post-save area (frac<0.30; the walk-proof walk afterwards moves away
    by design, so persistence compares pre-walk shots).

Overall PASS requires: scene entry in both processes, a save-op hash change,
clean exits, source untouched, bytes stable across relaunch, scene re-entered.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtrip_check import (Client, Fail, drive_to_net, free_port, sha_hex,
                             wait_worker_drained)  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RELEASED = 0x3FF
A = 0x3FE
BBTN = 0x3FD
START = 0x3F7
SELECT = 0x3FB
UP = 0x3BF
DOWN = 0x37F
LEFT = 0x3DF
RIGHT = 0x3EF
RB = 0x2FF
LB = 0x1FF


def launch(out, test_sav, cache_dir, tag, logf):
    port = free_port()
    cmd = [str(ROOT / "build" / "MMBN3WhiteRecomp"), "game.toml",
           "--save", str(test_sav),
           "--rom", str(ROOT / "roms" / "mmbn3_white_usa.gba"),
           "--bios", str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin"),
           "--tcp", str(port)]
    env = dict(__import__("os").environ)
    env["GBARECOMP_HEAL_CACHE"] = str(cache_dir)
    # Same preload-bypass rationale as roundtrip_check.py (B7 shutdown
    # sample); D runs keep warm-load ON.
    env["GBARECOMP_HEAL_WARM_LOAD"] = "0"
    (out / f"{tag}-command.json").write_text(json.dumps(
        {"argv": cmd, "cwd": str(out), "port": port,
         "env_overrides": {"GBARECOMP_HEAL_CACHE": str(cache_dir),
                           "GBARECOMP_HEAL_WARM_LOAD": "0"}}, indent=1))
    so = open(out / f"{tag}-stdout.log", "wb")
    se = open(out / f"{tag}-stderr.log", "wb")
    proc = subprocess.Popen(cmd, cwd=out, env=env, stdin=subprocess.DEVNULL,
                            stdout=so, stderr=se)
    logf.write(f"[{tag}] pid={proc.pid} port={port}\n")
    logf.flush()
    return proc, port, so, se


def enter_scene(client, out, proc, tag, note):
    """Shared verified navigation; returns (ok, menu_hash, p1_hash, entry).
    Hashes/entry are '' when entry fails (never None — callers subscript).
    entry is the at-entry gameplay shot filename (pre-walk) for area
    persistence compares."""
    try:
        menu_h, p1_h, net = drive_to_net(client, out, proc, tag, note)
        return True, menu_h, p1_h, net.get("entry_shot", "")
    except Fail as e:
        note(f"[{tag}] scene entry failed: {e}")
        return False, "", "", ""


def graceful_quit(client, proc, tag, note, wait_cap=300.0):
    if client is not None:
        client.close()
    t_end = time.monotonic() + wait_cap
    code = None
    while time.monotonic() < t_end:
        code = proc.poll()
        if code is not None:
            break
        time.sleep(5)
    forced = False
    if code is None:
        proc.kill()  # child PID only; a forced end FAILS the run
        code = proc.wait(timeout=10)
        forced = True
    note(f"[{tag}] exit={code} forced={forced}")
    return code, forced


def file_hash(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-src", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (args.out or (ROOT / "build" / "rt_sram" / stamp)).resolve()
    args.save_src = args.save_src.resolve()
    out.mkdir(parents=True, exist_ok=False)
    logf = open(out / "driver.log", "w")
    res = {"checks": {}, "attempts": []}

    def note(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    def check(name, passed, expected, observed):
        res["checks"][name] = {"pass": bool(passed), "expected": expected,
                               "observed": observed}
        note(f"check {name}: {'PASS' if passed else 'FAIL'} "
             f"(expected {expected}; observed {observed})")
        return bool(passed)

    src_before = sha_hex(args.save_src)
    res["source_save"] = str(args.save_src)
    res["source_sha_before"] = src_before
    test_sav = out / "test.sav"
    shutil.copyfile(args.save_src, test_sav)
    assert file_hash(test_sav) == src_before
    cache_dir = out / "heal-cache"
    shutil.copytree(ROOT / "recomp_cache", cache_dir)
    note(f"source {src_before[:16]} copied to disposable test.sav; cache seeded")

    code1 = forced1 = code2 = forced2 = None
    client1 = client2 = None
    proc1 = proc2 = None
    try:
        # ---- proc1: enter, verify scene, explore the save menu ----
        proc1, port1, so1, se1 = launch(out, test_sav, cache_dir, "p1", logf)
        try:
            client1 = Client(port1, proc1)
            ok1, menu1, p1, _e1 = enter_scene(client1, out, proc1, "p1", note)
            check("p1_scene_entry", ok1, "menu->net scene per drive_to_net",
                  f"menu={menu1[:12]} P1={p1[:12]}")
            if not ok1:
                raise Fail("p1 did not reach the net scene")

            pre_hash = file_hash(test_sav)
            pre_scroll = client1.scroll()
            pre_shot = client1.shot(out / "p1-pre-save.ppm")
            note(f"pre-save file={pre_hash[:16]} scroll={pre_scroll[:16]} "
                 f"shot={pre_shot[:12]}")
            # Make a gameplay change first (movediag pattern): UP then DOWN
            # toward the witnessed area transition, so the later save has
            # state worth persisting and the relaunch can verify the AREA.
            client1.call(cmd="set_keyinput", value=UP)
            time.sleep(2.5)
            client1.call(cmd="set_keyinput", value=DOWN)
            time.sleep(4.0)
            client1.call(cmd="set_keyinput", value=RELEASED)
            time.sleep(1.0)
            walk_shot = client1.shot(out / "p1-post-walk.ppm")
            walk_scroll = client1.scroll()
            note(f"post-walk shot={walk_shot[:12]} "
                 f"scroll={pre_scroll[:16]}->{walk_scroll[:16]}")
            # Advance any story dialog the walk triggered (S14: the Lan's-room
            # trigger leaves a textbox open that interferes with menu nav).
            # Loop A until the bottom textbox clears (white_bottom<0.40:
            # story textbox 0.75 vs gameplay floor 0.16, calibrated), max 20.
            # A longer cutscene needs its flag-setting END reached (S15's
            # fixed 6 As stopped mid-cutscene, SRAM never dirtied).
            def white_bottom():
                raw = client1.shot_raw(out / "_trigger.ppm")
                n = tot = 0
                for y in range(110, 160):
                    for x in range(240):
                        i = 3 * (y * 240 + x)
                        tot += 1
                        if raw[i] > 200 and raw[i + 1] > 200 and raw[i + 2] > 200:
                            n += 1
                return n / tot

            cleared = None
            for i in range(20):
                w = white_bottom()
                if w < 0.40:
                    cleared = i
                    break
                client1.tap(A, hold=0.3, gap=1.5)
            time.sleep(2.0)
            trigger_shot = client1.shot(out / "p1-post-trigger.ppm")
            trigger_scroll = client1.scroll()
            note(f"trigger cleared after {cleared} advances "
                 f"(None = still open at 20); "
                 f"shot={trigger_shot[:12]} "
                 f"scroll={walk_scroll[:16]}->{trigger_scroll[:16]}")
            res["trigger_cleared_after"] = cleared

            # Closed-loop row sweep. S14 design (S1-S13 lessons):
            # - The PET list template is captured FRESH after each pet_open
            #   (adaptive run template): PLACE/panels depend on the area, so
            #   a committed template fails after an area warp. Cursor default
            #   is stable within a run, so list-list MAD stays <1.
            # - Confirms are conditional on the witnessed save-dialog
            #   template; completion ("OK! Your save is complete!") is a
            #   separate template and witnesses the save even when the game
            #   rewrites byte-identical SRAM (S12: state == file).
            # - Winner = bytes changed OR dialog sequence through completion.
            #   Area persistence across relaunch carries the roundtrip proof.
            from roundtrip_check import blue_frac as _blue_frac
            _tpl = json.load(open(ROOT / "tools" / "pet_list_template.json"))
            _tnx, _tny = (_tpl["grid_nx"], _tpl["grid_ny"])
            _tthr = _tpl["threshold_mad"]
            _txmin = _tpl.get("mask_x_min", 0)
            _back_n = [0]
            _dtpl = json.load(open(ROOT / "tools" / "save_dialog_templates.json"))
            _dgrids = list(_dtpl["templates"].values())
            _dnx, _dny, _dthr = (_dtpl["grid_nx"], _dtpl["grid_ny"],
                                 _dtpl["threshold_mad"])
            _ctpl = json.load(open(ROOT / "tools" / "save_complete_template.json"))
            _cband0, _ccells = (_ctpl["band"], _ctpl["cells"])
            _cnx, _cny, _cthr = (_ctpl["grid_nx"], _ctpl["grid_ny"],
                                 _ctpl["threshold_mad"])
            _run_tpl = [None]

            def _grid(raw):
                tot = [0] * (_tnx * _tny)
                cnt = [0] * (_tnx * _tny)
                for y in range(160):
                    for x in range(_txmin, 240):
                        i = 3 * (y * 240 + x)
                        c = (y * _tny // 160) * _tnx + (x * _tnx // 240)
                        tot[c] += (raw[i] + raw[i + 1] + raw[i + 2]) // 3
                        cnt[c] += 1
                return [t / c if c else None for t, c in zip(tot, cnt)]

            def _mad(g, ref):
                s, n = 0.0, 0
                for a, b in zip(g, ref):
                    if a is not None and b is not None:
                        s += abs(a - b)
                        n += 1
                return s / n

            def at_pet_list(path):
                # Adaptive run template (captured at pet_open in this area).
                raw = client1.shot_raw(out / path)
                mad = _mad(_grid(raw), _run_tpl[0])
                return mad < _tthr, mad, raw

            def _dgrid(raw):
                tot = [0] * (_dnx * _dny)
                cnt = [0] * (_dnx * _dny)
                for y in range(160):
                    for x in range(240):
                        i = 3 * (y * 240 + x)
                        c = (y * _dny // 160) * _dnx + (x * _dnx // 240)
                        tot[c] += (raw[i] + raw[i + 1] + raw[i + 2]) // 3
                        cnt[c] += 1
                return [t / c for t, c in zip(tot, cnt)]

            def _dmad(g):
                return min(sum(abs(a - b) for a, b in zip(g, t)) / len(g)
                           for t in _dgrids)

            def save_dialog_open(path):
                # Either witnessed dialog variant (Yes/No + erase Yes/No).
                raw = client1.shot_raw(out / path)
                mad = _dmad(_dgrid(raw))
                return mad < _dthr, mad

            def save_complete_shown(path):
                # Button band only (Yes/No row): completion shows message
                # text, dialogs show buttons+arrow. v2 calibration:
                # completions 0.0, dialogs >=13.7, other >=51.
                raw = client1.shot_raw(out / path)
                tot = [0] * (_cnx * _cny)
                cnt = [0] * (_cnx * _cny)
                for y in range(160):
                    for x in range(240):
                        i = 3 * (y * 240 + x)
                        c = (y * _cny // 160) * _cnx + (x * _cnx // 240)
                        tot[c] += (raw[i] + raw[i + 1] + raw[i + 2]) // 3
                        cnt[c] += 1
                g = [t / c for t, c in zip(tot, cnt)]
                idx = [y * _cnx + x for x, y in _ccells]
                band = [g[i] for i in idx]
                mad = (sum(abs(a - b) for a, b in zip(band, _cband0))
                       / len(band))
                return mad < _cthr, mad

            openers = [("START", START), ("SELECT", SELECT)]
            winner = None
            completion_witnessed = [False]
            attempt = 0

            def pet_open():
                for _ in range(3):
                    client1.tap(BBTN, hold=0.2, gap=0.4)
                time.sleep(1.0)
                client1.tap(okey)
                time.sleep(2.0)
                for _ in range(10):
                    client1.tap(UP, hold=0.15, gap=0.25)
                # Adaptive template: this area's fresh PET list.
                ref = client1.shot_raw(out / f"_listref-{attempt:02d}.ppm")
                _run_tpl[0] = _grid(ref)
                note(f"pet opened; list template captured")

            def back_to_list():
                # B, then a full 2 s settle: submenu-exit transitions render
                # intermediate frames that flunk the template and cause
                # overshoot to gameplay (witnessed in S7: 0.8 s was short).
                # Every check frame is retained (post-mortem labeling).
                for _ in range(6):
                    _back_n[0] += 1
                    bp = f"_back-{attempt:02d}-{_back_n[0]:02d}.ppm"
                    ok, mad, _raw = at_pet_list(bp)
                    if ok:
                        return True
                    note(f"back-out: mad={mad:.1f} ({bp}), pressing B")
                    client1.tap(BBTN, hold=0.2, gap=0.4)
                    time.sleep(2.0)
                return False

            for oname, okey in openers:
                for sweep_dir, skey in (("down", DOWN), ("up", UP)):
                    pet_open()
                    restarts = 0
                    for step in range(12):
                        # One row step, open it, then drive the save dialogs
                        # to completion: after every A, check completion
                        # FIRST (it auto-dismisses in ~2s; S16 missed it at
                        # +3s), then the dialog templates, then the hash.
                        # A witnessed chain dialog->...->completion wins even
                        # when the game rewrites byte-identical SRAM (S12:
                        # state == file is a no-op write, not a failed save).
                        client1.tap(skey, hold=0.2, gap=0.3)
                        client1.tap(A)
                        time.sleep(2.0)
                        h, confirmed = file_hash(test_sav), 0
                        saw_dialog = False
                        for rnd in range(6):
                            if h != pre_hash:
                                break
                            comp, cmad = save_complete_shown(
                                f"_comp-{attempt:02d}-{rnd}.ppm")
                            if comp and (saw_dialog or confirmed > 0):
                                completion_witnessed[0] = True
                                note(f"attempt {attempt} round {rnd}: SAVE "
                                     f"COMPLETION shown (cmad={cmad:.1f})")
                                break
                            dlg, dmad = save_dialog_open(
                                f"_dlg-{attempt:02d}-{rnd}.ppm")
                            note(f"attempt {attempt} round {rnd}: "
                                 f"dialog={dlg} dmad={dmad:.1f} "
                                 f"comp={comp} cmad={cmad:.1f}")
                            if not dlg and not comp:
                                break
                            client1.tap(A, hold=0.2, gap=0.5)
                            time.sleep(1.5)
                            confirmed += 1
                            saw_dialog = saw_dialog or dlg
                            h = file_hash(test_sav)
                        # The SRAM write can flush seconds after the game's
                        # completion message (witnessed S12 attempt 6: message
                        # shown, file unchanged at first read, mtime updated
                        # minutes later). Poll for the change before judging.
                        if h == pre_hash and confirmed > 0:
                            t_end = time.monotonic() + 30.0
                            while time.monotonic() < t_end:
                                time.sleep(2.0)
                                h = file_hash(test_sav)
                                if h != pre_hash:
                                    note(f"attempt {attempt}: save flushed "
                                         f"late: {h[:16]}")
                                    break
                        time.sleep(1.0)
                        h = file_hash(test_sav)
                        shot = client1.shot(
                            out / f"p1-attempt-{attempt:02d}.ppm")
                        entry = {"attempt": attempt, "opener": oname,
                                 "sweep": sweep_dir, "step": step,
                                 "confirms": confirmed,
                                 "file_hash": h, "changed": h != pre_hash,
                                 "shot": shot[:12]}
                        res["attempts"].append(entry)
                        note(f"attempt {attempt}: opener={oname} "
                             f"sweep={sweep_dir} step={step} "
                             f"confirms={confirmed} changed={h != pre_hash} "
                             f"file={h[:12]} shot={shot[:12]}")
                        attempt += 1
                        if h != pre_hash or completion_witnessed[0]:
                            winner = entry
                            break
                        if not back_to_list():
                            restarts += 1
                            note(f"attempt {attempt}: list not recovered; "
                                 f"full reset #{restarts}")
                            if restarts > 2:
                                note("too many resets; ending sweep")
                                break
                            pet_open()
                    if winner is not None:
                        break
                if winner is not None:
                    break
            check("save_op_changes_file", winner is not None,
                  "bytes changed OR save completion witnessed after confirms",
                  f"winner={winner} completion={completion_witnessed[0]} "
                  f"attempts={attempt}")
            if winner is None:
                raise Fail("BLOCKED: no bounded menu sequence saved "
                           f"({attempt} attempts, file unchanged, no "
                           f"completion witnessed)")

            # Confirm through any completion dialog; file must go stable.
            for _ in range(4):
                client1.tap(A, hold=0.2, gap=1.0)
            time.sleep(5.0)
            saved_hash = file_hash(test_sav)
            # Back out to gameplay and capture the POST-SAVE AREA: the
            # relaunch must re-enter this same area (persistence proof).
            for _ in range(4):
                client1.tap(BBTN, hold=0.2, gap=0.5)
            time.sleep(3.0)
            saved_area = client1.shot_raw(out / "p1-post-save-area.ppm")
            saved_scroll = client1.scroll()
            from roundtrip_check import frac_changed as _frac
            res["save_effect"] = {
                "pre_hash": pre_hash, "saved_hash": saved_hash,
                "pre_scroll": pre_scroll, "saved_scroll": saved_scroll,
                "completion_witnessed": completion_witnessed[0],
                "winner": winner}
            note(f"post-save file={saved_hash[:16]} "
                 f"completion={completion_witnessed[0]} "
                 f"scroll_same={saved_scroll == pre_scroll}")
            client1.ensure_parked()
            wait_worker_drained(client1, proc1, note)
            code1, forced1 = graceful_quit(client1, proc1, "p1", note)
            client1 = None
            check("p1_clean_exit", code1 == 0 and not forced1,
                  "exit 0, no forced termination",
                  f"exit={code1} forced={forced1}")
            if code1 != 0 or forced1:
                raise Fail("p1 did not exit cleanly")

            # TCP sessions flush SRAM at EXIT (the debug path keeps
            # exit-only flush), so the save bytes land here — not during
            # the witnessed completion. S18 proved it: mid-session reads
            # stayed 8340… while the post-exit file was 485e… (310 bytes).
            post_exit_hash = file_hash(test_sav)
            res["post_exit_hash"] = post_exit_hash
            changed = (post_exit_hash != src_before)
            check("save_bytes_persisted_at_exit", changed,
                  f"post-exit test.sav != source {src_before[:16]}",
                  f"observed {post_exit_hash[:16]}")
            if not changed:
                note("no-op write (state == file); area persistence below "
                     "still carries the roundtrip proof")

            # ---- proc2: genuinely new process, same test.sav ----
            res["relaunch_file"] = post_exit_hash
            proc2, port2, so2, se2 = launch(out, test_sav, cache_dir, "p2", logf)
            client2 = Client(port2, proc2)
            ok2, menu2, p1b, entry2 = enter_scene(client2, out, proc2, "p2", note)
            check("p2_scene_entry", ok2, "menu->net scene per drive_to_net",
                  f"menu={menu2[:12]} P1={p1b[:12]}")
            if not ok2:
                raise Fail("p2 did not reach gameplay")
            after_hash = file_hash(test_sav)
            # Area persistence against the AT-ENTRY shot (pre-walk): the
            # walk-proof walk legitimately moves away afterwards.
            entry_raw = (out / entry2).read_bytes().split(b"\n", 3)[3]
            after_entry = entry_raw
            stable = (after_hash == post_exit_hash)
            area_frac = _frac(saved_area, after_entry)
            # Same area re-entered: mostly identical pixels (animation
            # shimmer ≪ area change). Threshold 0.30 separates same-area
            # (<0.05 observed for parked repeats) from area changes (>0.9).
            area_same = area_frac < 0.30
            check("save_bytes_stable_across_relaunch", stable,
                  f"test.sav == post-exit bytes {post_exit_hash[:16]}",
                  f"observed {after_hash[:16]}")
            check("relaunch_reenters_saved_area", area_same,
                  "post-Continue area matches post-save area (frac<0.30)",
                  f"area_frac={area_frac:.4f}")
            after_scroll = client2.scroll()
            res["position_evidence"] = {
                "saved_scroll": saved_scroll, "after_scroll": after_scroll,
                "area_frac": round(area_frac, 4),
                "note": "area re-entry (not pixel identity) is the claim; "
                        "scroll may legitimately reset to the area spawn"}
            client2.ensure_parked()
            wait_worker_drained(client2, proc2, note)
            code2, forced2 = graceful_quit(client2, proc2, "p2", note)
            client2 = None
            check("p2_clean_exit", code2 == 0 and not forced2,
                  "exit 0, no forced termination",
                  f"exit={code2} forced={forced2}")
        finally:
            for tag, so, se in (("p1", locals().get("so1"), locals().get("se1")),
                                ("p2", locals().get("so2"), locals().get("se2"))):
                for fh in (so, se):
                    try:
                        if fh is not None:
                            fh.close()
                    except Exception:
                        pass
    except Fail as e:
        res["failure"] = str(e)
        note(f"FAILED: {e}")
    except Exception as e:  # never lose the evidence packet on a crash
        res["failure"] = f"{type(e).__name__}: {e}"
        note(f"FAILED: {res['failure']}")
    finally:
        res.setdefault("cleanup_exit", {})
        for tag2, client, proc in (("p1", client1, proc1),
                                   ("p2", client2, proc2)):
            try:
                if proc is not None and proc.poll() is None:
                    # Park + drain before quit on all paths (see roundtrip).
                    try:
                        if client is not None:
                            client.ensure_parked(timeout_s=60.0)
                            wait_worker_drained(client, proc, note, cap_s=180.0)
                    except Exception as e:
                        note(f"drain before quit failed: {e}")
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            pass
                    t_end = time.monotonic() + 420
                    while time.monotonic() < t_end and proc.poll() is None:
                        time.sleep(2)
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=10)
                        res.setdefault("cleanup_forced", []).append(proc.pid)
                res["cleanup_exit"][tag2] = proc.poll() if proc else None
                note(f"[{tag2}] final exit={res['cleanup_exit'][tag2]}")
            except Exception as e:
                res.setdefault("cleanup_errors", []).append(str(e))
    res["source_sha_after"] = sha_hex(args.save_src)
    res["source_untouched"] = (res["source_sha_after"] == src_before)
    note(f"source_untouched={res['source_untouched']}")
    for fh in ("so1", "so2", "se1", "se2"):
        try:
            fh_obj = locals().get(fh)
            if fh_obj is not None:
                fh_obj.close()
        except Exception:
            pass
    with open(out / "results.json", "w") as f:
        json.dump(res, f, indent=1)
    ok = ("failure" not in res and res.get("source_untouched")
          and all(c["pass"] for c in res["checks"].values()))
    res["ok"] = ok
    with open(out / "results.json", "w") as f:
        json.dump(res, f, indent=1)
    print(("PASS" if ok else "FAIL") + f": {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
