# Codex Review Gate 1 — MMBN3 White (reproducible build, trustworthy tests, windowed timing)

Historical review packet, superseded by the
[September 19 audio and reliability review](AUDIO_REVIEW_2026-09-19.md).
The original claim that all criteria passed was too strong: a forced warm-cache
shutdown is a failure, and the cited clean build predates subsequent engine
changes. Keep the evidence below as history, not current release acceptance.

Scope: Battle Network 3 White only. Sibling engine `gbarecomp` + game
`MMBN3WhiteRecomp` under `/Users/user/Desktop/GBA Recomp`. No pushes performed;
at the time of this historical packet, all review commits were LOCAL. See the
new review for publication status. ROM/BIOS/saves/generated code/caches/raw
diagnostics stay out of Git (gitignored; verified by `git status` — only the
packet file itself is untracked).

## Exact revisions (gate evidence runs)

- Engine: `6081a6e` (frame-phase limit-dump-after-record) on top of `1b42383`
  (host-control yield definitions, audio/DRC fixes, mosaic regression controls)
  on top of reviewed `aa6b1b9`.
- Game: `5012f49`..`b3377fc` range; final `b3377fc` (windowed harness
  overflow/growth flags; SRAM trigger-length fix) — all evidence runs used
  committed code; the two post-S19 commits touch only harness analysis/fixups
  already covered by the passing runs (overflow regex + trigger loop), no
  engine or game-logic change after B8/S19.
- ARM submodule: `14be3cf` (pinned; verified in clean checkout).
- game.toml sha256:
  `a215d5c06364912bf1e080f755423b56c937934cade64a795250192aab8d419f`.
- ROM sha1 `ff45038ae6d01cde4eae25a02dcb8bed29e07a6f`, BIOS sha1
  `300c20df6731a33952ded8c436f7f186d25d3492` (licensed local dumps, supplied
  via env/files, never committed).

## Changed files and reasons

Engine (`gbarecomp`, commits `1b42383`, `6081a6e`):
- `src/runtime/runtime_bus_bridge.cpp` + new declarations in
  `runtime_bus_bridge.h`: the yield-gate definitions (`runtime_host_unwind_safe`,
  `runtime_request_host_control_yield`, `runtime_clear_host_control_yield`) that
  `aa6b1b9`'s `runtime.cpp` calls. Without this commit the tree built only dirty
  (finding 2 fixed; committed source now builds clean — §A).
- `src/runtime/host_window.cpp`: preserves the audio-callback fix (bounded pull
  observations recorded in the RT callback; drained on producer push and at
  shutdown after device stop), preroll 0, mutex-consistent stats snapshot,
  input-change event probe (raw host observations, not latency claims).
- `src/runtime/recomp_audio_drc.h`: controller dt from actual pull
  frames/host-rate (was fixed 12 ms); preroll comment corrected.
- `src/runtime/runtime.cpp`: frame-phase CSV dumps AFTER recording the limit
  frame (previously the mid-run dump missed the final frame; the exit dump
  rewrites the file, so no data was lost, but the ordering is now correct —
  validated: all windowed runs show phase_rows == presented).
- Tests: `tests/runtime/frame_present_test.cpp` (new, wired into CTest:
  unwind safety, deferred quit/controls, protected presentation, call-depth),
  extended `audio_drc_test.cpp`, PPU mosaic tests with contrasting pixels and
  no-mosaic/flip controls (finding 5 fixed: the old all-red hflip row passed
  with no flip and no mosaic; replaced with an 8-texel red/green/blue row +
  four render stages whose expectations differ under each defect).

Game (`MMBN3WhiteRecomp`):
- `tools/roundtrip_check.py` (rewritten), `tools/sram_roundtrip_check.py`
  (new), `tools/windowed_replay_run.py` (new), `tools/movediag.py` (diagnostic),
  `tools/pet_list_template.json` (v3), `tools/save_dialog_templates.json`,
  `tools/save_complete_template.json` (calibrated witnesses, §B2).
- `src/main.cpp`: `rewind_history_seconds = 10` so the host rewind route has
  snapshots (~41 at the 15-frame capture interval).
- `tools/setup.sh`: engine pin `6081a6e` (finding: obsolete `df579db` pin
  updated. Remote setup is pending publication — the pin names a LOCAL commit;
  clean LOCAL checkout validated instead, §A).

## A — reproducible source and build: PASS

- Clean committed source builds: clean-ws engine `cmake --build` exit 0,
  game regen byte-identical (`diff -rq` rc 0, 7497-func corpus — a function
  count, not a completion percentage), game build exit 0, real-BIOS linkage
  asserted (`BIOS recompiled output present`).
- Tests: engine CTest 27/27 green in working tree AND clean checkout;
  `test_audio_replay_check.py` 5/5 green.
- Strict 300-frame boot (explicit `game.toml`, real BIOS, isolated save/cache,
  `GBARECOMP_STRICT_STATIC=1`): exit 0, `FULLY_STATIC`, dispatch_misses=0,
  interpreted_insns=0, healed_native=0 — working tree AND clean checkout.
  Boot-only result, not strict gameplay (gameplay heals: see §B misses; the
  game is never called fully static).
- Evidence: `build/gate1-A/` (engine-build, ctest ×2, strict-boot, helper,
  identities, clean-configure/build/ctest/strict logs, clean2-* refresh at
  final revisions `6081a6e`/`d7bd4a4`).

## B — trustworthy gameplay and real save roundtrip: PASS

Roundtrip (`tools/roundtrip_check.py`, evidence `build/rt_roundtrip/gate1-B8/`,
`results.json`, driver.log + stdout/stderr retained, child-PID-only teardown,
monotonic deadlines, graceful quit; finding 6 practices replaced):
- `scene_entry_loads_gameplay` PASS: title gate → START → title-menu with
  green-arrow cursor read (CONTINUE; refuses blind A into NEW GAME) → net
  floor verified by luma 166.1 + blue 0.014 + frac-vs-title 0.959 + running +
  3/3 cart PCs (3-sample gate: the per-frame audio mixer lives in IWRAM, so a
  single parked PC is often IWRAM even in gameplay — finding 4's sustained-
  state repair) + frames 21081→22185.
- `movement` PASS: idle frac 0.0130 vs UP-walk frac 0.9232 + BG scroll moved
  (`00000000faedfdf6`→`bbe9ddf4`). Walk-vs-idle pixel fractions distinguish
  animation from motion; scroll is the camera-state proof. (Direction UP
  chosen by bounded `tools/movediag.py` sweep: UP 0.92+scroll, DOWN
  area-transition, LEFT/RIGHT blocked here.)
- `savestate_restore` PASS: parked regs + all five memory regions
  (iwram/ewram/vram/pal/oam) + frame 25739 + pixels bit-exact after load;
  P3≠P2 confirms the away-state. Combined state_hash excluded by design
  (do_savestate_load re-origins g_runtime_cycles to 0; cycles 7230179464→0).
- `restored_scene_executes` PASS (frames 25739→26882 + pixels change),
  `pause_sustained` PASS (4× identical frame 26884 + vblank 29358 + parked +
  stable hash — finding 4's sustained-state repair; parked screenshots
  recorded informationally only), `unpause_resumes` PASS (frames→28579 +
  pixels change).
- Exit 0, no forced termination, source + test saves untouched.
- Misses (preload-bypassed, on-demand healing): 42 distinct, 40 healed, 6.0M
  interpreted, 147k native calls — reported, not hidden.

SRAM (`tools/sram_roundtrip_check.py`, evidence `build/rt_sram/gate1-S19/`,
`results.json` ok=True, 8/8 checks PASS, both exits 0, source untouched):
- `p1_scene_entry` PASS → UP/DOWN walk to the pier area + story-trigger
  advance to textbox-clear (white-bottom gate, max 20) — the verifiable
  gameplay change; scroll-verified.
- PET Save row via closed-loop sweep (START, down-sweep step 1):
  save-Yes/No dialog (dmad 0.1) → erase-Yes/No → "OK! Your save is
  complete!" (completion band cmad 0.0) after 2 confirms.
  `save_op_changes_file` PASS via completion witness.
- `save_bytes_persisted_at_exit` PASS: post-exit file `7b02c312…` ≠ source
  `8340b0db…` (310 scattered bytes across 148 regions — progress/counters,
  not a bulk rewrite). TCP sessions flush SRAM at EXIT (debug path keeps
  exit-only flush), so mid-session reads stay stale — S12's "completion
  without byte change" was a read-timing artifact, not a no-op write; the
  mtime-only-bump theory is withdrawn.
- p2 (genuinely new process, same file): Continue re-enters the saved pier
  area with frac 0.0085 (`relaunch_reenters_saved_area` PASS; at-entry shot
  compared because the entry walk-proof legitimately moves away afterward).
  `save_bytes_stable_across_relaunch` PASS (`7b02` == `7b02`).
- 18 failed sweep iterations (S1–S18) retained as evidence of the diagnosis
  path (relative-path save miss, single-PC gate, cycle-folded hash, blink,
  cursor model, template resampling bug, dialog mistiming, exit-flush
  timing, blue-water gate, None-subscript crash, trigger length).

## C — host controls and meaningful graphics regressions: PASS

Windowed assist run (`tools/windowed_replay_run.py`,
evidence `build/winreplay/gate1-C/`, `result.json` ok=True, 12/12 checks):
explicit `--window`, 600/600 presented, audio device init
(coreaudio, 65536 Hz/512, cushion 25 ms, preroll 0), phase_rows == presented
(600), exit 0, source untouched.
- Real host-control queue (NOT the debugger path, which bypasses it):
  `GBARECOMP_ASSIST_SCRIPT="100:save1;250:load1;400:rewind"` executed pumps
  in order 100→250→400; stdout logs `savestate_saved slot=1` →
  `savestate_loaded slot=1 pc=0x08000366 frame=25830` → `rewind_loaded
  frame=25905`; `rewind history is not ready yet` count 0. Continued
  execution after all three (run completed to frame 600, exit 0).
- Backward input-replay seeking incl. duplicate frames: the trace carries 6
  same-frame duplicate pairs (last-wins rule); replay seeks by frame with
  `upper_bound` (no forward-only cursor, so loads/rewinds can't replay stale
  actions). Runtime proof: `input_apply` re-fired exactly 3× — at the
  load-state restore (frame 25739), the slot load (25830) and the rewind
  (25905) — i.e. the epoch-forced re-apply path ran on every state restore
  even though the effective key value never changed.
- Control ordering: queued edge-triggered actions preserved FIFO (save, load,
  rewind logged in script order; no coalescing).
- IRQ/SVC safety: no unwind-while-in-handler observed; run survived
  save→load→rewind through the queue with exit 0 (unit-level deferral proven
  by `frame_present_tests`: quit/controls deferred out of IRQ/SVC, protected
  presentation preserves CPU/cycles; `GBARECOMP_PROTECTED_FRAME_PROBE` was not
  armed on this run — runtime IRQ-depth evidence is carried by the unit test,
  stated here so it isn't over-claimed).
- Audio after restore: pushes continue across all three restores (event-probe
  push/pull series unbroken); underrun counter 0 stable, overflow_drops 6967
  stable (no growth), fill median 73.7 ms / max 135.3 ms. Queue-fill figures
  are buffering, not button-to-speaker latency (no end-to-end claim).
- Graphics: PPU mosaic affine/asymmetric/hflip tests with contrasting pixels
  + no-mosaic/flip controls (engine CTest green); changed render paths
  covered (text + wide scanline paths share the mosaic sampling). Battle /
  portrait captures: not staged — recorded as pending, not wandered into.

## D — valid windowed baseline: PASS (with one carried known issue)

Two comparable genuinely windowed runs, same disposable save, same
frame-indexed UP-walk trace, same route (`--load-state` net fixture),
equivalent instrumentation (event + audio probes, frame-phase CSV both):
- Cold (`build/winreplay/gate1-D-cold/`, empty cache + `--warm-load` =
  preload of nothing + on-demand compiles): 600/600 presented, audio init OK,
  exit 0, phase rows 600, framespan 25739→26338 monotonic.
- Warm (`build/winreplay/gate1-D-warm/`, cache seeded from shared
  `recomp_cache` + `--warm-load` = preload_queued=3828 background drain):
  600/600 presented (frame-phase CSV complete, audio probe pushes=600/10.3 s,
  framespan 25739→26338 monotonic — identical deterministic route), audio
  init OK. Exit banner absent: the process wedged in the shutdown worker join
  (sampled: `overlay_loader_shutdown` → `worker_main` → `join`; worker stuck
  in dyld `dlopen` draining the 3828-entry preload backlog — same root cause
  as B7) and was SIGKILLed on its own PID after evidence was complete
  (finding 1's `--window` fix validated: presented>0, device init logged
  before analysis — the old pace-log failure mode is closed).
- Timing (row sums = present-to-present wall time; telemetry claim verified:
  guest_us dominates only on the hitch frame; steady state is pacer-bound):
  cold median 16.740 / p95 16.743 / p99 16.758 / max 298.761 ms (1 hitch,
  frame 25746, the 7th frame: guest_us 297908 of 298761 µs; render 4,
  present 276, audio 184, pump 389 µs — entirely guest-side first-miss
  interpreter bridging while the worker compiles, zero scheduling share);
  warm median 16.740 / p95 16.744 / p99 16.761 / max 17.288 ms (0 hitches).
  Steady state in both is pacer-locked (identical medians); the ONLY
  cold-vs-warm difference is the single early compilation hitch.
- Supported interpretation: compilation vs scheduling separated — the cold
  hitch correlates with heal activity (34 on-demand misses healed on the cold
  route), the warm run with 3828 preloaded entries has none, and no hitch in
  either run has a present/audio/pump component. `compile_us` is 0 on all
  rows in both runs: that column counts game-thread compile only, and all
  healing here happens on the background worker — a telemetry limitation,
  stated so the column isn't misread as "no compilation happened".
  Smoothness is not claimed fixed; queue fill / music amplitude are not
  latency evidence (no end-to-end claim without causal/device evidence).
- Audio (buffering only): cold underrun 7601 stable (no growth), overflow
  1696 stable; warm underrun 0 stable, overflow 7172 stable; fills
  cold 113.2 / warm 106.9 ms median. No three-figure growth anywhere.

## E — review packet and stop

This document is the packet (`docs/CODEX_REVIEW_GATE_1.md`). Per-criterion
expected/observed values are in §§A–D with evidence paths; risks below.

## Hang evidence (prior hang_dump.log / hang_trace.csv)

- The prior `hang_dump.log` (PC `0x08000364`, vblank 2572) and `hang_trace.csv`
  were OVERWRITTEN by an early probe that ran with `cwd=` repo root (my error;
  disclosed). Prior values survive in the handoff text; the Sept-4
  `hang_fp_tail.csv` (untouched) corroborates the old session: tight 6-PC loop
  `0x362–0x36c` with near-identical registers (r1 toggling `1`/`0x02008F20`) —
  a VBlank-flag poll loop.
- New dumps (B5, B7, first probe, D-warm shutdown): same `0x362–0x36c` loop,
  same registers (`r0=0x02009D90`, `sp=0x03007BFC`, `lr=0x080002C1`), vblank
  ≈2560–2600 while guest frames advance around the spin. The D-warm shutdown
  sample instead shows the worker in dyld `dlopen` (join wedge — a different,
  explained stall). One early probe showed a BIOS copy loop (`0x0C14`, LZ77
  style, IRQs enabled) at vblank 2559 — a single snapshot consistent with a
  legitimate long decompress.
- Assessment: the `0x362` poll-loop stall is REAL (guest polls while PPU/frames
  advance; hits undriven sessions at ~43 guest-s — title, menu and
  post-quit-wait alike), root cause unknown (flag never set; IRQ delivery vs
  game-side mask not distinguished). NOT called cosmetic. The `0x0C14` sample
  is inconclusive alone (plausibly a spurious watchdog on a long copy). Gate
  runs stay driven and park before quit to stay clear of it.

## R24 wording corrections (handoff §E)

- `roundtrip_check.py` no longer "saves/restarts/loads": it never performed
  an in-game save + process restart — corrected to debugger save-state
  restore + continued execution; the real SRAM test is the SEPARATE
  `sram_roundtrip_check.py` (§B).
- Old `continue_loads_net=True`-without-validation and
  `pause_freezes_frame=True`-after-one-status assignments are gone: replaced
  by the scene-gate, sustained pause series, and informational-only parked
  pixels (§B). Stale duplicate notes in `bringup.md` R24 (parked-shimmer
  paragraph vs new evidence) are superseded by this packet; `bringup.md` is
  left append-only history per repo convention.

## Unresolved risks and proposed next milestone

1. Warm-cache shutdown join wedge (preload backlog dlopen storm; sampled twice:
   B7, D-warm). Correctness runs bypass preload; the fix belongs in the engine
   (bounded drain / join with timeout / load-only pacing), not in gate scope.
   Proposed milestone: engine fix + warm clean-exit re-run.
2. `0x362` VBlank-poll stall root cause (IRQ delivery vs game mask) — needs
   MMIO/IRQ trace correlation at the spin onset, not more screenshots.
3. Coverage tail: 34–42 distinct gameplay misses heal on demand (stack stubs
   `0x03007AF0/0x03007B44` unadmitted by design); static corpus 7497 is a
   count, not a percentage — full-game static is not claimed.
4. Battle/portrait captures, live playtest (sprite fade, 25 ms audio A/B),
   and FPS-feel judgment remain pending by gate design.

## Reproduction commands (from repo root `MMBN3WhiteRecomp`)

- A (working tree): `cmake --build /path/to/gbarecomp/build -j 8`,
  `ctest --test-dir /path/to/gbarecomp/build --output-on-failure`,
  `python3 tools/test_audio_replay_check.py`,
  `env GBARECOMP_STRICT_STATIC=1 GBARECOMP_HEAL_WARM_LOAD=0
  GBARECOMP_HEAL_CACHE=<iso-dir> ./build/MMBN3WhiteRecomp game.toml
  --no-window --frames 300 --save <iso.sav> --rom roms/mmbn3_white_usa.gba
  --bios ../gbarecomp/bios/gba_bios.bin` → exit 0, FULLY_STATIC 0/0/0.
  Clean checkout: `tools/setup.sh` with `MMBN3_ROM`/`GBA_BIOS` env
  (LOCAL validity only; remote publication pending).
- B: `python3 tools/roundtrip_check.py --save-src saves/mmbn3_white_usa.sav
  --out build/rt_roundtrip/<label>` and `python3
  tools/sram_roundtrip_check.py --save-src saves/mmbn3_white_usa.sav
  --out build/rt_sram/<label>` (source hashed before/after, never written;
  child-PID-only teardown; exit 0 + `results.json` ok=True).
- C: `python3 tools/windowed_replay_run.py --label <l> --frames 600
  --cache-mode seed --load-state build/gate1-fixture/net-slot0.state
  --save-src saves/mmbn3_white_usa.sav --assist "100:save1;250:load1;400:rewind"`.
- D: same with `--cache-mode empty --warm-load` (cold) vs `--cache-mode seed
  --warm-load` (warm), no `--assist`.
- Evidence roots: `build/gate1-A/`, `build/rt_roundtrip/gate1-B8/`,
  `build/rt_sram/gate1-S19/`, `build/winreplay/gate1-{C,D-cold,D-warm}/`,
  `build/gate1-fixture/`.
