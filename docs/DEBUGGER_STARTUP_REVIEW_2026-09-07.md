# Debugger startup investigation — 2026-09-07

## Confirmed cause and scope

A normal-cache headless TCP launch remained alive but did not accept a
connection during a bounded 45-second test. Its output log remained empty.
A macOS stack sample located startup inside overlay_loader_init ->
warm_load_cache_dir -> overlay_compile_one -> dlopen, loading cached native
shards. The debugger server is started only after this initialization.
This disproves the earlier assumption that game-thread starvation was
preventing the debugger from starting. The sample does not establish that
cache preload is the cause of in-game sound delay.

## Changes

- Engine: GBARECOMP_HEAL_WARM_LOAD=0 bypasses startup cache loading while
  preserving on-demand healing. The default behavior is unchanged. No cache
  files were deleted. Start/end messages and elapsed milliseconds now go to
  stderr so redirected output exposes this startup phase.
- Engine: removed the speculative 1 ms sleep while holding the debugger
  control mutex, and removed invalid input-to-audio/present telemetry.
  Existing host input/audio timestamp accessor additions were left intact.
- Added tools/check_debug_startup.py: isolated save, complete logs, bounded
  connection wait, frame advancement check, screenshot, clean quit, and
  cleanup limited to its own child process. It uses a private healing cache
  unless --warm-cache is selected. It is NOT a sound-latency benchmark.
- Left OpenCode's experimental tools/latency_probe.py untouched. Do not use
  its music-volume changes as evidence of button-to-sound latency.

## Verified

- Game rebuilt successfully (existing compiler/linker warnings remain).
- Actual game configuration, bypassed preload: connection in 0.308 seconds;
  three step requests advanced from frame 0 to frame 2 / three VBlank starts;
  screenshot retrieved; clean exit 0; personal save SHA-256 unchanged.
  Evidence: build/runs/debug-startup-wxatohyj/.
- Rebuilt game strict 300-frame boot: exit 0, FULLY_STATIC,
  zero dispatch misses, zero interpreted instructions, zero healed calls.
  Evidence: build/runs/strict-boot-zl2cdfl6/runtime.log.
- Existing engine test build: all 26 tests passed. This complements, but
  does not replace, the rebuilt-game integration checks above.

## Repeat

From this repository run `python3 tools/check_debug_startup.py`.
For a bounded comparison against the existing startup cache, add
`--warm-cache --timeout 45`. A timeout is recorded as a failed startup check,
not as proof that the process is deadlocked.

## Next: sound delay, not speculative buffer tuning

1. Launch an isolated-save windowed session with preload bypassed and
   confirm the intended title/menu visually. Record frame throughput and
   existing frame-phase timing separately from debugger stepping.
2. Pick one repeatable menu action with an identifiable sound. Align a
   timestamped input with the first corresponding generated audio samples
   and displayed response. Avoid confusing background music with the SFX.
3. Distinguish game logic delay from host audio queue delay. Headless TCP
   stepping cannot measure CoreAudio/speaker output latency. Use a loopback
   recording or synchronized external capture for end-to-end confirmation.
4. Only change audio pacing/queue behavior once measurements identify the
   responsible stage, then repeat the same action and regression tests.

No claim of fixed audible latency or complete gameplay coverage is made.
No commits, pushes, or external handoff were performed.
