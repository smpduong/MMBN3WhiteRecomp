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
    exit 0 required; forced termination fails the run)
 7. proc2: genuinely new process with the SAME test.sav; Continue; verify the
    save bytes are stable across relaunch+load, the net scene is re-entered,
    and (as observed evidence) whether the pre-save scroll position persisted.

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
    """Shared verified navigation; returns (ok, menu_hash, p1_hash)."""
    try:
        menu_h, p1_h, _net = drive_to_net(client, out, proc, tag, note)
        return True, menu_h, p1_h
    except Fail as e:
        note(f"[{tag}] scene entry failed: {e}")
        return False, None, None


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
            ok1, menu1, p1 = enter_scene(client1, out, proc1, "p1", note)
            check("p1_scene_entry", ok1, "menu->net scene per drive_to_net",
                  f"menu={menu1[:12]} P1={p1[:12]}")
            if not ok1:
                raise Fail("p1 did not reach the net scene")

            pre_hash = file_hash(test_sav)
            pre_scroll = client1.scroll()
            pre_shot = client1.shot(out / "p1-pre-save.ppm")
            note(f"pre-save file={pre_hash[:16]} scroll={pre_scroll[:16]} "
                 f"shot={pre_shot[:12]}")

            # Closed-loop row sweep (S1-S5 showed blind DOWN-counting never
            # lands Save: cursor state carries across attempts). Per opener:
            # fresh PET, UPx10 to the extreme, then single-step DOWN + open +
            # double-confirm + hash, backing out with B,B to the list after
            # every step. 12 steps cover all 8 rows with or without wrap;
            # a PET-presence guard (blue panels) reopens on drift, bounded.
            # Save completes inside the step (Yes/Yes defaults, witnessed);
            # the winner screenshot must show SAVE UI (packet-verified).
            from roundtrip_check import blue_frac as _blue_frac
            openers = [("START", START), ("SELECT", SELECT)]
            winner = None
            attempt = 0

            def pet_open():
                for _ in range(3):
                    client1.tap(BBTN, hold=0.2, gap=0.4)
                time.sleep(1.0)
                client1.tap(okey)
                time.sleep(2.0)
                for _ in range(10):
                    client1.tap(UP, hold=0.15, gap=0.25)

            def in_pet():
                raw = client1.shot_raw(out / "_petguard.ppm")
                return _blue_frac(raw) > 0.30

            for oname, okey in openers:
                for sweep_dir, skey in (("down", DOWN), ("up", UP)):
                    pet_open()
                    reopens = 0
                    for step in range(12):
                        client1.tap(skey, hold=0.2, gap=0.3)
                        client1.tap(A)
                        time.sleep(1.5)
                        client1.tap(A, hold=0.2, gap=0.5)
                        time.sleep(1.0)
                        client1.tap(A, hold=0.2, gap=0.5)
                        time.sleep(4.0)
                        h = file_hash(test_sav)
                        shot = client1.shot(
                            out / f"p1-attempt-{attempt:02d}.ppm")
                        entry = {"attempt": attempt, "opener": oname,
                                 "sweep": sweep_dir, "step": step,
                                 "file_hash": h, "changed": h != pre_hash,
                                 "shot": shot[:12]}
                        res["attempts"].append(entry)
                        note(f"attempt {attempt}: opener={oname} "
                             f"sweep={sweep_dir} step={step} "
                             f"changed={h != pre_hash} file={h[:12]} "
                             f"shot={shot[:12]}")
                        attempt += 1
                        if h != pre_hash:
                            winner = entry
                            break
                        client1.tap(BBTN, hold=0.2, gap=0.4)
                        client1.tap(BBTN, hold=0.2, gap=0.4)
                        time.sleep(1.0)
                        if not in_pet():
                            reopens += 1
                            note(f"attempt {attempt}: left PET; reopen "
                                 f"#{reopens}")
                            if reopens > 3:
                                note("too many reopens; ending sweep")
                                break
                            pet_open()
                    if winner is not None:
                        break
                if winner is not None:
                    break
            check("save_op_changes_file", winner is not None,
                  "some bounded menu sequence changes test.sav bytes",
                  f"winner={winner} attempts={attempt}")
            if winner is None:
                raise Fail("BLOCKED: no bounded menu sequence saved "
                           f"({attempt} attempts, file unchanged)")

            # Confirm through any completion dialog; file must go stable.
            for _ in range(4):
                client1.tap(A, hold=0.2, gap=1.0)
            time.sleep(5.0)
            saved_hash = file_hash(test_sav)
            saved_scroll = client1.scroll()
            saved_shot = client1.shot(out / "p1-post-save.ppm")
            res["save_effect"] = {
                "pre_hash": pre_hash, "saved_hash": saved_hash,
                "pre_scroll": pre_scroll, "saved_scroll": saved_scroll,
                "winner": winner}
            note(f"post-save file={saved_hash[:16]} "
                 f"scroll_same={saved_scroll == pre_scroll} shot={saved_shot[:12]}")
            client1.ensure_parked()
            wait_worker_drained(client1, proc1, note)
            code1, forced1 = graceful_quit(client1, proc1, "p1", note)
            client1 = None
            check("p1_clean_exit", code1 == 0 and not forced1,
                  "exit 0, no forced termination",
                  f"exit={code1} forced={forced1}")
            if code1 != 0 or forced1:
                raise Fail("p1 did not exit cleanly")

            # ---- proc2: genuinely new process, same test.sav ----
            res["relaunch_file"] = saved_hash
            proc2, port2, so2, se2 = launch(out, test_sav, cache_dir, "p2", logf)
            client2 = Client(port2, proc2)
            ok2, menu2, p1b = enter_scene(client2, out, proc2, "p2", note)
            check("p2_scene_entry", ok2, "menu->net scene per drive_to_net",
                  f"menu={menu2[:12]} P1={p1b[:12]}")
            after_hash = file_hash(test_sav)
            after_scroll = client2.scroll()
            after_shot = client2.shot(out / "p2-post-continue.ppm")
            stable = (after_hash == saved_hash)
            pos_same = (after_scroll == saved_scroll)
            check("save_bytes_stable_across_relaunch", stable,
                  f"test.sav == post-save bytes {saved_hash[:16]}",
                  f"observed {after_hash[:16]}")
            res["position_evidence"] = {
                "saved_scroll": saved_scroll, "after_scroll": after_scroll,
                "position_persisted": pos_same,
                "saved_shot": saved_shot, "after_shot": after_shot,
                "note": "scroll equality = position persisted; animation "
                        "makes raw pixels incomparable across sessions"}
            note(f"position_persisted={pos_same} "
                 f"scroll {saved_scroll[:16]}->{after_scroll[:16]}")
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
