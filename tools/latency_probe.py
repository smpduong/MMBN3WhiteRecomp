#!/usr/bin/env python3
"""Deterministic frame-stepped latency probe: START -> menu-open vs menu blip.

Free-runs to title, parks, then single-steps guest frames. After each step:
- screenshot hash: first change = menu-open frame M
- cap-ring tail scan: first PSG/direct transient = blip at absolute sample S
The press lands on frame 0; both events are then comparable in guest frames.
"""

import hashlib
import json
import socket
import struct
import subprocess
import sys
import time

ROOT = "/Users/user/Desktop/GBA Recomp/MMBN3WhiteRecomp"
PORT = 19894


def call(s, **req):
    s.sendall(json.dumps(req).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(1 << 20)
        if not chunk:
            raise RuntimeError("closed")
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    return json.loads(line.decode())


def cap_tail(s, count=4096):
    r = call(s, cmd="audio_cap", count=count)
    assert r.get("ok"), r
    first = r.get("first")
    head = r.get("head")
    out = {}
    for k in ("ch1", "ch2", "ch3", "ch4", "direct_a", "direct_b", "mixed"):
        d = bytes.fromhex(r[k][: count * 4])
        out[k] = struct.unpack("<%dh" % count, d)
    return first, head, out


def wait_title(s):
    last = None
    run = 0
    t0 = time.time()
    while time.time() - t0 < 300:
        disp = call(s, cmd="read_io", addr=0x04000000, len=2)
        v = int.from_bytes(bytes.fromhex(disp["data"]), "little")
        h = hashlib.sha256(
            bytes.fromhex(call(s, cmd="screenshot")["data"])).hexdigest()
        layers = bool(v & 0x1F00) and not (v & 0x80)
        run = run + 1 if (layers and h == last) else 0
        last = h
        if run >= 3:
            return h
        time.sleep(2.0)
    raise RuntimeError("title never stabilized")


def main():
    proc = subprocess.Popen(
        [f"{ROOT}/build/MMBN3WhiteRecomp", "--tcp", str(PORT),
         "--rom", f"{ROOT}/roms/mmbn3_white_usa.gba",
         "--bios", "/Users/user/Desktop/GBA Recomp/gbarecomp/bios/gba_bios.bin"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 150
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", PORT), 8.0)
                break
            except OSError:
                if proc.poll() is not None:
                    print(f"game exited during startup: {proc.returncode}")
                    return 1
                time.sleep(2)
        else:
            print("no TCP after 150s")
            return 1
        s.settimeout(30.0)
        try:
            r = call(s, cmd="continue")
            assert r.get("ok"), r
            title_hash = wait_title(s)
            print("title stable; parking")
            call(s, cmd="pause")
            time.sleep(0.3)

            # hold START
            call(s, cmd="set_keyinput", value=0x3F7)
            import math
            menu_frame = None
            intro_frame = None
            music_frame = None
            prev_rms = None
            rms_series = []
            for f in range(240):
                step = call(s, cmd="step")
                if f == 3:
                    call(s, cmd="set_keyinput", value=0x3FF)  # release START
                scr = call(s, cmd="screenshot")
                h = hashlib.sha256(bytes.fromhex(scr["data"])).hexdigest()
                if menu_frame is None and h != title_hash:
                    menu_frame = f
                    print(f"menu-open at frame {f}")
                if menu_frame is not None and intro_frame is None and f >= 4:
                    if f == 6:
                        call(s, cmd="set_keyinput", value=0x3FE)  # A -> New Game
                    if f == 9:
                        call(s, cmd="set_keyinput", value=0x3FF)
                    if f >= 12 and h != menu_hash if False else False:
                        pass
                first, head, w = cap_tail(s, count=2048)
                mixed = w["mixed"]
                rms = math.sqrt(sum(x * x for x in mixed) / len(mixed))
                rms_series.append(rms)
                # menu open visual: save hash for later scene-change detection
                if menu_frame == f:
                    menu_hash = h
                # scene change after New Game confirm: detect big visual move
                if menu_frame is not None and intro_frame is None and f >= 12:
                    if h != menu_hash:
                        intro_frame = f
                        with open(f"{ROOT}/build/latency_intro.ppm", "wb") as fh:
                            fh.write(f"P6\n{scr['w']} {scr['h']}\n255\n".encode()
                                     + bytes.fromhex(scr["data"]))
                        print(f"intro scene at frame {f}")
                # music onset: rms jumps far above the menu-level envelope
                if prev_rms is not None and music_frame is None:
                    if rms > prev_rms * 2.2 and rms > 600:
                        music_frame = f
                        print(f"music onset at frame {f} (rms {prev_rms:.0f}->{rms:.0f})")
                prev_rms = rms
                if intro_frame is not None and music_frame is not None:
                    break
            print("SUMMARY:")
            print(f"  menu-open:  frame {menu_frame}")
            print(f"  intro-scene: frame {intro_frame}")
            print(f"  music-onset: frame {music_frame}")
            if intro_frame is not None and music_frame is not None:
                print(f"  skew: {music_frame - intro_frame} frames "
                      f"({(music_frame - intro_frame)*16.7:.0f} ms guest)")
            print(f"  rms series: {[round(v) for v in rms_series[::10]]}")
        finally:
            try:
                call(s, cmd="quit")
            except Exception:
                pass
            s.close()
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
