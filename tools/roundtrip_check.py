#!/usr/bin/env python3
"""Gameplay/state roundtrip on a DISPOSABLE save copy (criterion B).

Flow (all evidence under build/rt_roundtrip/<stamp>/):
 1. copy SOURCE save -> test.sav (SOURCE hashed before/after, never written);
    seed an isolated heal-cache dir from the shared cache (warm + write-isolated)
 2. launch with explicit game.toml + --save/--rom/--bios/--tcp; stdout/stderr
    retained; cwd=run dir; monotonic deadlines; teardown kills only the child PID
 3. continue, wait title-stable (bounded)
 4. START -> stable post-START screen, routed by content (blue PET menu ->
    B closes to gameplay; otherwise A selects Continue). continue_loads_net
    passes only if the gameplay scene verifies: bright upper half (not the
    black intro), low blue (not PET), running cart PC, advancing frames.
 5. movement: idle pixel-change fraction vs walk pixel-change fraction over the
    same duration, plus BG scroll (camera-state) delta. Passes if the walk
    fraction clearly exceeds idle animation OR scroll moves; both recorded.
 6. savestate_save with parked registers/state_hash/frame recorded; walk away;
    savestate_load; restore passes only if registers + state_hash + frame +
    pixels all match the saved values; continued execution passes if frames
    advance and the scene animates afterwards (state, not just a still image)
 7. pause: 4x run_status 1s apart must show identical frame+vblank with
    parked:true, plus stable state_hash; resume must advance frames.
    Parked-screenshot hashes are recorded informationally (renderer shimmer
    is not pause evidence either way).
 8. graceful quit (pause, then quit, patient bounded wait for the worker join);
    exit 0 required. Forced termination is recorded and FAILS the run.

Does NOT prove an in-game SRAM save point; see tools/sram_roundtrip_check.py.
Rewind has no TCP command and bypasses it anyway; host-control-queue rewind
is covered by the windowed assist-script run (see docs/CODEX_REVIEW_GATE_1.md).
"""

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASED = 0x3FF
UP = 0x3FF & ~0x040
RIGHT = 0x3FF & ~0x010


class Fail(Exception):
    pass


def sha_hex(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port, proc, timeout=90.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 8.0)
                self.sock.settimeout(20.0)
                self.buf = b""
                return
            except OSError as e:
                last = e
                if proc.poll() is not None:
                    raise Fail(f"game exited during startup: {proc.returncode}")
                time.sleep(0.5)
        raise Fail(f"no TCP after {timeout}s: {last}")

    def call(self, **req):
        self.sock.sendall(json.dumps(req).encode() + b"\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise Fail("socket closed")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line.decode())

    def shot_raw(self, path):
        r = self.call(cmd="screenshot")
        assert r.get("ok"), r
        d = bytes.fromhex(r["data"])
        with open(path, "wb") as f:
            f.write(f"P6\n{r['w']} {r['h']}\n255\n".encode() + d)
        return d

    def shot(self, path):
        return hashlib.sha256(self.shot_raw(path)).hexdigest()

    def tap(self, keys, hold=0.4, gap=0.6):
        self.call(cmd="set_keyinput", value=keys)
        time.sleep(hold)
        self.call(cmd="set_keyinput", value=RELEASED)
        time.sleep(gap)

    def ensure_parked(self, timeout_s=20.0):
        self.call(cmd="pause")
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            st = self.call(cmd="run_status")
            if st.get("parked"):
                return st
            time.sleep(0.5)
        raise Fail("core never parked for comparison shot")

    def status(self):
        return self.call(cmd="run_status")

    def regs(self):
        r = self.call(cmd="registers")
        assert r.get("ok"), r
        return {k: v for k, v in r.items() if k != "ok"}

    def state_hash(self):
        r = self.call(cmd="state_hash")
        assert r.get("ok"), r
        return r

    def scroll(self):
        r = self.call(cmd="read_io", addr=0x04000010, len=8)
        assert r.get("ok"), r
        return r["data"]

    def close(self):
        try:
            self.call(cmd="set_keyinput", value=RELEASED)
        except Exception:
            pass
        try:
            self.call(cmd="pause")
        except Exception:
            pass
        try:
            self.call(cmd="quit")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def drive_to_net(client, out, proc, tag, note):
    """Title -> menu -> Continue, all steps verified (shared by roundtrip and
    SRAM harnesses). Returns (menu_hash, p1_hash, net_evidence). Raises Fail
    with evidence if any step cannot be verified (no blind navigation)."""
    r = client.call(cmd="continue")
    assert r.get("ok"), r
    last, run, tg0 = None, 0, time.monotonic()
    while time.monotonic() - tg0 < 180:
        if proc.poll() is not None:
            raise Fail(f"{tag}: game exited during title gate: {proc.returncode}")
        d = client.call(cmd="read_io", addr=0x04000000, len=2)
        v = int.from_bytes(bytes.fromhex(d["data"]), "little")
        h = client.shot(out / f"{tag}-_gate.ppm")
        if bool(v & 0x1F00) and not (v & 0x80) and h == last:
            run += 1
            if run >= 3:
                break
        else:
            run = 0
        last = h
        time.sleep(2.0)
    else:
        raise Fail(f"{tag}: title never stabilized")
    note(f"[{tag}] title ready")
    title_hash = last
    title_raw = client.shot_raw(out / f"{tag}-title.ppm")
    # Title -> title-menu -> Continue, every step verified (witnessed against
    # real captures; detector boxes located by frame diffing, not by engine
    # internals):
    # 1. one START tap; poll up to 20 s for any screen that differs from the
    #    title hash (the menu may blink, so stability is NOT required here).
    # 2. read the green-arrow cursor (re-polled through blink-off phases):
    #    CONTINUE -> A; NEWGAME -> DOWN, re-verify CONTINUE, then A.
    # 3. verify the gameplay scene (net_check gates). If A-select fails the
    #    scene check, one B-tap covers the PET-menu-open case, then FAIL.
    menu_h, menu_raw = None, None
    client.tap(0x3F7, hold=0.5, gap=1.0)
    tg1 = time.monotonic()
    while time.monotonic() - tg1 < 20:
        if proc.poll() is not None:
            raise Fail(f"{tag}: game exited during menu navigation")
        cand = client.shot_raw(out / f"{tag}-_menu_try.ppm")
        ch = hashlib.sha256(cand).hexdigest()
        if ch != title_hash:
            menu_raw, menu_h = cand, ch
            note(f"[{tag}] post-START screen differs from title")
            break
        time.sleep(1.0)
    if menu_h is None:
        raise Fail(f"{tag}: START never changed the title screen")
    with open(out / f"{tag}-menu.ppm", "wb") as f:
        f.write(f"P6\n240 160\n255\n".encode() + menu_raw)
    cur, cur_raw = "UNKNOWN", menu_raw
    tg2 = time.monotonic()
    while cur == "UNKNOWN" and time.monotonic() - tg2 < 15:
        time.sleep(1.5)
        if proc.poll() is not None:
            raise Fail(f"{tag}: game exited during cursor read")
        cur_raw = client.shot_raw(out / f"{tag}-_cursor.ppm")
        cur = cursor_state(cur_raw)
    note(f"[{tag}] cursor={cur}")
    if cur == "NEWGAME":
        # DOWN must move NEWGAME->CONTINUE; verified, retried bounded (taps
        # can land in a blink/transition). A final UP covers inverted layouts.
        moved = False
        for n in range(3):
            client.tap(0x37F, hold=0.5, gap=1.0)  # DOWN
            tg3 = time.monotonic()
            cur = "UNKNOWN"
            while cur == "UNKNOWN" and time.monotonic() - tg3 < 8:
                time.sleep(1.0)
                cur_raw = client.shot_raw(out / f"{tag}-_cursor2.ppm")
                cur = cursor_state(cur_raw)
            note(f"[{tag}] cursor after DOWN #{n}={cur}")
            if cur == "CONTINUE":
                moved = True
                break
        if not moved and cur == "NEWGAME":
            client.tap(0x3BF, hold=0.5, gap=1.0)  # UP (wrap cover)
            tg4 = time.monotonic()
            cur = "UNKNOWN"
            while cur == "UNKNOWN" and time.monotonic() - tg4 < 8:
                time.sleep(1.0)
                cur_raw = client.shot_raw(out / f"{tag}-_cursor3.ppm")
                cur = cursor_state(cur_raw)
            note(f"[{tag}] cursor after UP={cur}")
    if cur != "CONTINUE":
        raise Fail(f"{tag}: cursor never on CONTINUE (saw {cur}; "
                   f"will not blind-press A into NEW GAME)")
    # Cursor verified on CONTINUE: select, advance any stray text, verify.
    banned = {title_hash, menu_h}
    client.tap(0x3FE)  # A = Continue
    time.sleep(8)
    for _ in range(2):
        client.tap(0x3FE, hold=0.3, gap=1.5)
    p1_raw, net = net_check(client, proc, out, tag, note,
                            f"{tag}-_net_try.ppm", banned, title_raw)
    if p1_raw is None:
        # One bounded fallback: the save may load with the PET menu open
        # (observed once); B closes it to gameplay.
        note(f"[{tag}] A-select failed scene check; one B-close fallback")
        client.tap(0x3FD, hold=0.3, gap=1.0)
        time.sleep(3)
        p1_raw, net = net_check(client, proc, out, tag, note,
                                f"{tag}-_net_try2.ppm", banned, title_raw)
    if p1_raw is None:
        raise Fail(f"{tag}: Continue never entered gameplay "
                   f"(A-select + B-close failed; see driver.log)")
    note(f"[{tag}] gameplay entered")
    with open(out / f"{tag}-P1_net.ppm", "wb") as f:
        f.write(f"P6\n240 160\n255\n".encode() + p1_raw)
    return menu_h, hashlib.sha256(p1_raw).hexdigest(), net


def wait_worker_drained(client, proc, note, cap_s=180.0):
    """Parked drain wait: poll heal counters until they stop changing (the
    background gcc worker has installed everything reachable) so the later
    quit-join has little left to wait out. Bounded; returns the last sample.
    """
    stable, last = 0, None
    t0 = time.monotonic()
    while time.monotonic() - t0 < cap_s:
        if proc.poll() is not None:
            break
        try:
            m = client.call(cmd="misses")
        except Exception:
            break
        key = (m.get("healed_native"), m.get("interpreted_insns"),
               m.get("distinct_misses"))
        if key == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = key
        time.sleep(10.0)
    note(f"worker drain wait done: stable_polls={stable} last={last}")
    return last


def frac_changed(a, b):
    assert len(a) == len(b) and len(a) > 0
    n = 0
    for x, y in zip(a, b):
        if x != y:
            n += 1
    return n / len(a)


def upper_luma(raw, w=240, h=160):
    # Mean luma of the upper half: the gameplay floor is bright, while the
    # intro cutscene is black with only a bottom textbox. Bytes are RGB triples.
    px = len(raw) // 3
    assert px == w * h, len(raw)
    tot, n = 0, 0
    for i in range(px // 2):
        r, g, b = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
        tot += (r + g + b) // 3
        n += 1
    return tot / n


def cursor_state(raw, w=240, h=160):
    """Title-menu cursor position from the green arrow sprite. Arrow boxes
    were located by diffing two menu frames (11x14 diff at x78..88 y118..131):
    the only changing region. Returns 'NEWGAME', 'CONTINUE' or 'UNKNOWN'
    (blink-off phase: the arrow blinks, so UNKNOWN means re-poll, not absent).
    Independent of engine internals: green arrow on a grey background."""
    px = len(raw) // 3
    assert px == w * h, len(raw)

    def green_in(x0, y0, x1, y1):
        n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = 3 * (y * w + x)
                r, g, b = raw[i], raw[i + 1], raw[i + 2]
                if g > 100 and g > r + 40 and g > b + 40:
                    n += 1
        return n

    ng = green_in(74, 103, 92, 118)
    ct = green_in(74, 116, 92, 133)
    if ct >= 20 and ct > 3 * max(ng, 1):
        return "CONTINUE"
    if ng >= 20 and ng > 3 * max(ct, 1):
        return "NEWGAME"
    return "UNKNOWN"


def blue_frac(raw, w=240, h=160):
    # Fraction of pixels strongly blue-dominant. The PET menu is mostly blue
    # panels; the title, title-menu and net floor are not. Routes the post-
    # START screen: PET menu -> B closes to gameplay; otherwise -> A selects.
    px = len(raw) // 3
    assert px == w * h, len(raw)
    n = 0
    for i in range(px):
        r, g, b = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
        if b > 60 and b > r + 30 and b > g + 30:
            n += 1
    return n / px


def net_check(client, proc, out, tag, note, shot_name, banned, title_raw):
    """Verify a gameplay scene shot. Returns (raw, evidence) or (None, ev).
    Thresholds validated offline (see review packet): PET blue=0.65,
    town blue=0.26, net blue=0.01, title blue=0.12, title-menu blue=0.05;
    intro luma=0.0, title-menu luma=43, town luma=111, title luma=151,
    net luma=166. frac-vs-title rejects title blink."""
    cand = client.shot_raw(out / shot_name)
    lum = upper_luma(cand)
    blue = blue_frac(cand)
    tvs = frac_changed(cand, title_raw)
    st1 = client.status()
    time.sleep(2.0)
    st2 = client.status()
    pc = int(st2.get("pc", "0x0"), 16)
    ch = hashlib.sha256(cand).hexdigest()
    ev = {"hash": ch, "luma": round(lum, 1), "blue_frac": round(blue, 3),
          "frac_vs_title": round(tvs, 3),
          "run": st2.get("run"), "pc": st2.get("pc"),
          "f0": st1.get("frame"), "f1": st2.get("frame")}
    ok = (ch not in banned and lum > 60.0 and blue < 0.40 and tvs > 0.25
          and st2.get("run") == "running"
          and 0x08000000 <= pc <= 0x09FFFFFF
          and isinstance(ev["f1"], int) and ev["f1"] > ev["f0"])
    note(f"[{tag}] scene check {shot_name}: ok={ok} {ev}")
    return (cand, ev) if ok else (None, ev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-src", type=Path, required=True,
                    help="SOURCE save to COPY (hashed before/after, never written)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--total-budget", type=float, default=1500.0)
    args = ap.parse_args()
    t_start = time.monotonic()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (args.out or (ROOT / "build" / "rt_roundtrip" / stamp)).resolve()
    args.save_src = args.save_src.resolve()
    out.mkdir(parents=True, exist_ok=False)
    log = open(out / "driver.log", "w")
    res = {"checks": {}, "argv": sys.argv}

    def note(msg):
        print(msg, flush=True)
        log.write(msg + "\n")
        log.flush()

    def budget_left():
        return args.total_budget - (time.monotonic() - t_start)

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
    res["test_sha_initial"] = sha_hex(test_sav)
    assert res["test_sha_initial"] == src_before
    assert sha_hex(args.save_src) == src_before
    note(f"source {src_before[:16]} copied to disposable test.sav")

    # Isolated heal cache, seeded warm from the shared cache (write-isolated).
    cache_dir = out / "heal-cache"
    shared_cache = ROOT / "recomp_cache"
    if shared_cache.is_dir():
        shutil.copytree(shared_cache, cache_dir)
        res["cache_seed"] = f"copied {shared_cache}"
    else:
        cache_dir.mkdir()
        res["cache_seed"] = "empty (no shared cache found)"
    note(f"heal cache: {res['cache_seed']}")

    port = free_port()
    cmd = [str(ROOT / "build" / "MMBN3WhiteRecomp"), "game.toml",
           "--save", str(test_sav),
           "--rom", str(ROOT / "roms" / "mmbn3_white_usa.gba"),
           "--bios", str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin"),
           "--tcp", str(port)]
    env = dict(__import__("os").environ)
    env["GBARECOMP_HEAL_CACHE"] = str(cache_dir)
    (out / "command.json").write_text(json.dumps(
        {"argv": cmd, "cwd": str(out), "port": port,
         "env_overrides": {"GBARECOMP_HEAL_CACHE": str(cache_dir)}}, indent=1))
    note(f"launch: {' '.join(cmd)}")

    code, forced = None, False
    with open(out / "stdout.log", "wb") as so, open(out / "stderr.log", "wb") as se:
        proc = subprocess.Popen(cmd, cwd=out, env=env, stdin=subprocess.DEVNULL,
                                stdout=so, stderr=se)
        res["child_pid"] = proc.pid
        client = None
        try:
            if budget_left() < 600:
                raise Fail("budget too small for a full roundtrip")
            client = Client(port, proc)
            menu_h, p1_h, net = drive_to_net(client, out, proc, "rt", note)
            scene_ok = True
            check("scene_entry_loads_gameplay", scene_ok,
                  "bright net scene + running + cart PC + advancing frames",
                  f"luma={net['luma']:.1f} run={net['run']} pc={net['pc']} "
                  f"frames={net['f0']}->{net['f1']}")
            note(f"P1 net area: {p1_h[:12]} menu: {menu_h[:12]}")

            # Movement: idle animation baseline vs driven walk, same durations.
            ia = client.shot_raw(out / "_idle_a.ppm")
            time.sleep(1.5)
            ib = client.shot_raw(out / "_idle_b.ppm")
            idle_frac = frac_changed(ia, ib)
            scroll_before = client.scroll()
            wa = client.shot_raw(out / "_walk_a.ppm")
            client.call(cmd="set_keyinput", value=UP)
            time.sleep(3.0)
            wb = client.shot_raw(out / "_walk_b.ppm")
            client.call(cmd="set_keyinput", value=RELEASED)
            time.sleep(1.0)
            walk_frac = frac_changed(wa, wb)
            scroll_after = client.scroll()
            scroll_moved = (scroll_after != scroll_before)
            pixel_ok = (walk_frac >= 5 * max(idle_frac, 1e-6)
                        and walk_frac >= 0.02)
            move_ok = pixel_ok or scroll_moved
            check("movement", move_ok,
                  "walk_frac >= 5*idle_frac and >= 0.02, and/or BG scroll moves",
                  f"idle_frac={idle_frac:.4f} walk_frac={walk_frac:.4f} "
                  f"scroll_moved={scroll_moved} "
                  f"scroll={scroll_before[:16]}->{scroll_after[:16]}")
            client.ensure_parked()
            p2 = client.shot(out / "P2_walked.ppm")
            note(f"P2 walked(parked): {p2[:12]}")

            # Save-state roundtrip with CPU/memory/frame state, parked.
            st_pre = client.status()
            regs_pre = client.regs()
            hash_pre = client.state_hash()
            ssfile = (out / "savestate" / "slot0.state").resolve()
            ssfile.parent.mkdir(parents=True, exist_ok=True)
            r = client.call(cmd="savestate_save", path=str(ssfile))
            assert r.get("ok"), r
            note(f"savestate saved: {[p.name for p in ssfile.parent.iterdir()]}")
            client.call(cmd="continue")
            time.sleep(0.5)
            client.call(cmd="set_keyinput", value=RIGHT)
            time.sleep(3.0)
            client.call(cmd="set_keyinput", value=RELEASED)
            time.sleep(1.0)
            client.ensure_parked()
            p3 = client.shot(out / "P3_moved_away.ppm")
            away = (p3 != p2)
            r = client.call(cmd="savestate_load", path=str(ssfile))
            assert r.get("ok"), r
            st_post = client.status()
            regs_post = client.regs()
            hash_post = client.state_hash()
            p4 = client.shot(out / "P4_restored.ppm")
            restore_ok = (regs_post == regs_pre
                          and hash_post.get("hash") == hash_pre.get("hash")
                          and st_post.get("frame") == st_pre.get("frame")
                          and p4 == p2 and away)
            check("savestate_restore", restore_ok,
                  "regs+state_hash+frame+pixels equal saved values; P3!=P2",
                  f"regs_eq={regs_post == regs_pre} "
                  f"hash_eq={hash_post.get('hash') == hash_pre.get('hash')} "
                  f"frame {st_pre.get('frame')}->{st_post.get('frame')} "
                  f"pixels_eq={p4 == p2} moved_away={away}")
            client.call(cmd="continue")
            time.sleep(2.0)
            st_fwd = client.status()
            client.ensure_parked()
            p4b = client.shot(out / "P4b_two_seconds_later.ppm")
            cont_ok = (st_fwd.get("frame", -1) > st_post.get("frame", -1)
                       and p4b != p4)
            check("restored_scene_executes", cont_ok,
                  "frames advance and pixels change after restore",
                  f"frame {st_post.get('frame')}->{st_fwd.get('frame')} "
                  f"pixels_changed={p4b != p4}")
            client.call(cmd="continue")

            # Sustained pause: 4 observations 1s apart, all frozen + parked.
            client.call(cmd="pause")
            time.sleep(0.5)
            frames, vblanks, parked = [], [], []
            for _ in range(4):
                st = client.status()
                frames.append(st.get("frame"))
                vblanks.append(st.get("vblank_starts"))
                parked.append(st.get("parked"))
                time.sleep(1.0)
            ha = client.state_hash()
            time.sleep(1.0)
            hb = client.state_hash()
            pause_ok = (len(set(frames)) == 1 and len(set(vblanks)) == 1
                        and all(parked) and ha.get("hash") == hb.get("hash"))
            check("pause_sustained", pause_ok,
                  "4x identical frame+vblank, parked, stable state_hash",
                  f"frames={frames} vblanks={vblanks} parked={parked} "
                  f"hash_stable={ha.get('hash') == hb.get('hash')}")
            h_a = client.shot(out / "P5_paused.ppm")
            time.sleep(2.0)
            h_b = client.shot(out / "P6_still_paused.ppm")
            res["parked_screenshots"] = {"P5": h_a, "P6": h_b,
                                         "identical": h_a == h_b,
                                         "note": "informational only; pause proof "
                                                 "is the frozen frame/vblank/hash "
                                                 "series above, not pixels"}
            note(f"parked shots identical={h_a == h_b} (informational)")
            client.call(cmd="continue")
            f_c0 = client.status().get("frame")
            time.sleep(3.0)
            f_c1 = client.status().get("frame")
            resume_ok = isinstance(f_c1, int) and f_c1 > f_c0
            h_c = client.shot(out / "P7_resumed.ppm")
            check("unpause_resumes", resume_ok and h_c != h_b,
                  "frames advance and pixels change after resume",
                  f"frames {f_c0}->{f_c1} pixels_changed={h_c != h_b}")

            m = client.call(cmd="misses")
            res["misses"] = {k: m.get(k) for k in
                             ("distinct_misses", "healed_native",
                              "interpreted_insns", "native_calls")}
            note(f"misses: {res['misses']}")
            # Let the background worker drain while parked so quit is prompt.
            client.ensure_parked()
            res["drain_last"] = wait_worker_drained(client, proc, note)
        except Fail as e:
            res["failure"] = str(e)
            note(f"FAILED: {e}")
        except Exception as e:  # never lose the evidence packet on a crash
            res["failure"] = f"{type(e).__name__}: {e}"
            note(f"FAILED: {res['failure']}")
        finally:
            if client is not None:
                client.close()
            # Patient graceful quit: the worker join waits out an in-flight
            # gcc compile. Bounded by the remaining budget (cap 300s).
            wait_s = max(10.0, min(300.0, budget_left()))
            t_end = time.monotonic() + wait_s
            while time.monotonic() < t_end:
                code = proc.poll()
                if code is not None:
                    break
                time.sleep(5)
            if code is None:
                proc.kill()  # child PID only; marks the run failed, not clean
                code = proc.wait(timeout=10)
                forced = True
            res["exit_code"] = code
            res["forced_termination"] = forced
            res["test_sha_after"] = sha_hex(test_sav)
            res["test_save_untouched"] = (res["test_sha_after"]
                                          == res["test_sha_initial"])
            note(f"exit={code} forced={forced} "
                 f"test_save_untouched={res['test_save_untouched']}")
    res["source_sha_after"] = sha_hex(args.save_src)
    res["source_untouched"] = (res["source_sha_after"] == src_before)
    note(f"source_untouched={res['source_untouched']}")
    with open(out / "results.json", "w") as f:
        json.dump(res, f, indent=1)
    ok = ("failure" not in res and code == 0 and not forced
          and res.get("source_untouched") and res.get("test_save_untouched")
          and all(c["pass"] for c in res["checks"].values()))
    print(("PASS" if ok else "FAIL") + f": {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
