#!/usr/bin/env python3
"""Shared harness for TCP-driven playback probes.

Every probe must:
- launch the game with stdout/stderr preserved into its run directory,
- connect, send {"cmd":"continue"} and confirm the guest runs,
- verify guest execution ADVANCES (screenshots and/or miss counters)
  before treating any observation as meaningful,
- poll with deadlines (no blind sleeps), detect early process exit,
- release all buttons during cleanup,
- save each run in a uniquely named directory.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import socket
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASED = 0x3FF


class ProbeError(RuntimeError):
    pass


class Client:
    def __init__(self, port: int, proc: subprocess.Popen, timeout: float = 30.0):
        deadline = time.time() + timeout
        last: OSError | None = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise ProbeError(
                    f"game exited during connect (code {proc.returncode})")
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), 5.0)
                self.sock.settimeout(20.0)
                self.buf = b""
                return
            except OSError as e:
                last = e
                time.sleep(0.3)
        raise ProbeError(f"no TCP on {port} after {timeout}s: {last}")

    def call(self, **req):
        self.sock.sendall(json.dumps(req).encode() + b"\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ProbeError("runtime closed the debug socket")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        try:
            return json.loads(line.decode())
        except json.JSONDecodeError as e:
            raise ProbeError(f"bad JSON from runtime: {e}")

    def screenshot_hash(self) -> str:
        r = self.call(cmd="screenshot")
        if not r.get("ok"):
            raise ProbeError(f"screenshot failed: {r}")
        return hashlib.sha256(bytes.fromhex(r["data"])).hexdigest()

    def save_screenshot(self, path: pathlib.Path) -> str:
        r = self.call(cmd="screenshot")
        if not r.get("ok"):
            raise ProbeError(f"screenshot failed: {r}")
        raw = bytes.fromhex(r["data"])
        with open(path, "wb") as f:
            f.write(f"P6\n{r['w']} {r['h']}\n255\n".encode() + raw)
        return hashlib.sha256(raw).hexdigest()

    def read_io(self, addr: int, length: int) -> str:
        r = self.call(cmd="read_io", addr=addr, len=length)
        if not r.get("ok"):
            raise ProbeError(f"read_io {addr:#x} failed: {r}")
        return r["data"]

    def set_keys(self, value: int = RELEASED):
        r = self.call(cmd="set_keyinput", value=value)
        if not r.get("ok"):
            raise ProbeError(f"set_keyinput failed: {r}")

    def misses(self) -> dict:
        r = self.call(cmd="misses")
        if not r.get("ok", True):
            raise ProbeError(f"misses query failed: {r}")
        return r

    def resume(self):
        r = self.call(cmd="continue")
        if not r.get("ok") or r.get("run") != "running":
            raise ProbeError(f"continue not confirmed: {r}")

    def close(self):
        try:
            self.set_keys(RELEASED)
        except (OSError, ProbeError, RuntimeError):
            pass
        try:
            self.call(cmd="quit")
        except (OSError, ProbeError, RuntimeError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def new_run_dir(outdir: pathlib.Path, name: str) -> pathlib.Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = outdir / f"{name}-{stamp}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def launch(exe: pathlib.Path, rom: pathlib.Path, bios: pathlib.Path,
           port: int, rundir: pathlib.Path, cwd: pathlib.Path,
           extra_args: list | None = None) -> subprocess.Popen:
    cmd = [str(exe), "--tcp", str(port),
           "--rom", str(rom), "--bios", str(bios)] + (extra_args or [])
    out = open(rundir / "stdout.log", "wb")
    err = open(rundir / "stderr.log", "wb")
    with open(rundir / "command.txt", "w") as f:
        f.write(" ".join(cmd) + f"\ncwd={cwd}\n")
    return subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err)


def check_alive(proc: subprocess.Popen, what: str):
    code = proc.poll()
    if code is not None:
        raise ProbeError(f"game exited early during {what} (code {code}); "
                         f"see stdout.log/stderr.log in the run dir")


def wait_advancing(client: Client, proc: subprocess.Popen,
                   deadline_s: float, poll_s: float = 2.0,
                   stable_needed: int = 0) -> dict:
    """Confirm the guest advances. Returns evidence dict.

    Advancement = screenshot hash changes between polls, or miss-counter
    activity (native_calls/interpreted/healed grow). With stable_needed > 0,
    additionally require that many consecutive identical frames afterwards
    (a settled screen, e.g. title ready) — returned as stable_hash.
    g_runtime_cycles is NOT used: it is known-stuck in some runners.
    """
    t0 = time.time()
    prev_hash = client.screenshot_hash()
    prev_m = client.misses()
    stable_run = 0
    while True:
        if time.time() - t0 > deadline_s:
            raise ProbeError(
                f"no guest advancement within {deadline_s}s "
                f"(hash={prev_hash[:12]}, misses={prev_m})")
        time.sleep(poll_s)
        check_alive(proc, "wait_advancing")
        h = client.screenshot_hash()
        m = client.misses()
        moved = (h != prev_hash or m != prev_m)
        if not moved:
            stable_run = 0
            prev_hash, prev_m = h, m
            continue
        if stable_needed <= 0:
            return {"hash": h, "misses": m}
        # movement seen; now require stability
        prev_hash, prev_m = h, m
        stable_run = 0
        while time.time() - t0 < deadline_s:
            time.sleep(poll_s)
            check_alive(proc, "wait_stable")
            h2 = client.screenshot_hash()
            if h2 != prev_hash:
                prev_hash = h2
                stable_run = 0
                continue
            stable_run += 1
            if stable_run >= stable_needed:
                return {"hash": h2, "misses": client.misses(),
                        "stable_hash": h2}
        raise ProbeError("screen never settled within deadline")


def cleanup(client: Client | None, proc: subprocess.Popen,
            rundir: pathlib.Path):
    if client is not None:
        client.close()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    with open(rundir / "exit_code.txt", "w") as f:
        f.write(f"{proc.returncode}\n")
