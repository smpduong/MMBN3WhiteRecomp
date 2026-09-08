#!/usr/bin/env python3
"""Movement diagnosis: which held input actually moves MegaMan in the net area?
Bounded, isolated (disposable save copy, seeded cache, cwd=run dir, child PID
only). For each D-pad direction: EWRAM byte-diff, BG scroll delta, and
screenshot pixel fraction vs pre-hold. Also tries A-taps first (textbox?).
"""
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtrip_check import Client, Fail, free_port, sha_hex, frac_changed  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RELEASED = 0x3FF
A = 0x3FE
BBTN = 0x3FD
START = 0x3F7
DIRS = {"UP": 0x3BF, "DOWN": 0x37F, "LEFT": 0x3DF, "RIGHT": 0x3EF}


def main():
    out = ROOT / "build" / "gate1-movediag"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    logf = open(out / "driver.log", "w")

    def note(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    src = ROOT / "saves" / "mmbn3_white_usa.sav"
    src_before = sha_hex(src)
    test_sav = out / "test.sav"
    shutil.copyfile(src, test_sav)
    cache_dir = out / "heal-cache"
    shutil.copytree(ROOT / "recomp_cache", cache_dir)
    port = free_port()
    cmd = [str(ROOT / "build" / "MMBN3WhiteRecomp"), "game.toml",
           "--save", str(test_sav),
           "--rom", str(ROOT / "roms" / "mmbn3_white_usa.gba"),
           "--bios", str(ROOT.parent / "gbarecomp" / "bios" / "gba_bios.bin"),
           "--tcp", str(port)]
    import os
    env = dict(os.environ)
    env["GBARECOMP_HEAL_CACHE"] = str(cache_dir)
    res = {}
    with open(out / "stdout.log", "wb") as so, open(out / "stderr.log", "wb") as se:
        proc = subprocess.Popen(cmd, cwd=out, env=env, stdin=subprocess.DEVNULL,
                                stdout=so, stderr=se)
        client = None
        try:
            client = Client(port, proc)
            assert client.call(cmd="continue").get("ok")
            last, run, tg0 = None, 0, time.monotonic()
            while time.monotonic() - tg0 < 180:
                d = client.call(cmd="read_io", addr=0x04000000, len=2)
                v = int.from_bytes(bytes.fromhex(d["data"]), "little")
                h = client.shot(out / "_gate.ppm")
                if bool(v & 0x1F00) and not (v & 0x80) and h == last:
                    run += 1
                    if run >= 3:
                        break
                else:
                    run = 0
                last = h
                time.sleep(2.0)
            client.tap(START)
            time.sleep(3)
            client.tap(A)
            time.sleep(8)
            p1 = client.shot(out / "P1.ppm")
            note(f"P1={p1[:12]}")

            def ewram():
                r = client.call(cmd="read_ewram", addr=0x02000000, len=262144)
                assert r.get("ok"), r
                return bytes.fromhex(r["data"])

            # Dismiss any textbox first.
            for i in range(3):
                client.tap(A, hold=0.3, gap=1.0)
            note(f"after A-taps: {client.shot(out / 'after-A.ppm')[:12]}")

            table = []
            for dname, dkey in DIRS.items():
                e0 = ewram()
                s0 = client.scroll()
                f0 = client.shot_raw(out / f"{dname}-0.ppm")
                client.call(cmd="set_keyinput", value=dkey)
                time.sleep(2.5)
                f1 = client.shot_raw(out / f"{dname}-1.ppm")
                client.call(cmd="set_keyinput", value=RELEASED)
                e1 = ewram()
                s1 = client.scroll()
                pf = frac_changed(f0, f1)
                ed = sum(1 for x, y in zip(e0, e1) if x != y)
                row = {"dir": dname, "pixel_frac": round(pf, 5),
                       "ewram_bytes_changed": ed,
                       "scroll_moved": s1 != s0,
                       "scroll": f"{s0[:16]}->{s1[:16]}"}
                table.append(row)
                note(f"{dname}: pixel_frac={pf:.5f} ewram_diff={ed} "
                     f"scroll_moved={s1 != s0}")
                time.sleep(1.0)
            res["table"] = table
        except Fail as e:
            res["failure"] = str(e)
            note(f"FAILED: {e}")
        except Exception as e:
            res["failure"] = f"{type(e).__name__}: {e}"
            note(f"FAILED: {res['failure']}")
        finally:
            if client is not None:
                client.close()
            t_end = time.monotonic() + 240
            code = None
            while time.monotonic() < t_end:
                code = proc.poll()
                if code is not None:
                    break
                time.sleep(5)
            if code is None:
                proc.kill()
                code = proc.wait(timeout=10)
                res["forced"] = True
            res["exit_code"] = code
    res["source_untouched"] = (sha_hex(src) == src_before)
    (out / "results.json").write_text(json.dumps(res, indent=1))
    note(f"exit={res.get('exit_code')} source_untouched={res['source_untouched']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
