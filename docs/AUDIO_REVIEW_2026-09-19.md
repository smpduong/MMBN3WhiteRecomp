# MMBN3 White audio and reliability review, 2026-09-19

Exploration and early combat run, but gameplay still uses interpreter fallback
and runtime compilation. The project is experimental, not a completed static
port or a decompilation. This report supersedes the overconfident acceptance
summary in `CODEX_REVIEW_GATE_1.md` without deleting its historical evidence.

## Reproduced audio issue

The user's September 9 playtest reported crackling at 2:08–2:18. That capture
had silent video and buffer counters, so it could not itself prove the waveform
cause. The September 19 replay uses the actual recorded input trace and the
same initial save SHA-256. It captures both source audio and SDL callback audio.

The old bridge reproduces 9,327,104 recorded output samples exactly from the
captured source data and callback order. The same 128–138-second interval
contains 18.417ms of concealed shortages. The corrected bridge with a 40ms
target has zero shortages in that interval and after 20 seconds on that same
schedule. It retains one startup concealment episode in the full-run result.

The engine fixes smooth transitions to/from concealed audio, rebuild the queue
after a shortage, preserve filter history on overflow, and prevent short
scheduling delays from accumulating permanent slowdown. The 40ms target adds
15ms of buffer budget compared with 25ms. See the engine's
[`docs/AUDIO_BRIDGE_REVIEW.md`](https://github.com/smpduong/gbarecomp/blob/main/docs/AUDIO_BRIDGE_REVIEW.md)
for the controlled 25/30/35/40ms comparison and exact replay commands.

## Local evidence

Raw evidence stays under ignored `build/audio-review/` and is not published:

- `baseline-i71rou7s`: original bridge, recorder enabled, 8500 frames, exit 0,
  complete WAVs, original save unchanged. Includes exploration and combat.
- `fixed-8xaz504w`: intermediate crossfade/recovery/pacer fixes with the old
  25ms target. This still had shortages and ran alongside a build/test workload;
  it is not the accepted final result or a controlled performance comparison.
- `final-40ms-9cytbfj4`: final 40ms build, 8500/8500 frames, exit 0, complete
  capture, original save unchanged. Source duration 142.232s; callback duration
  142.3125s. Zero unfilled frames and zero overflow; one startup concealment
  episode (3154 samples, 48.126ms). Zero new shortages after 20 seconds and
  throughout 128–138 seconds. Exact replay of the final callback stream also
  has zero mismatches across 9,326,592 output samples.

All three runs produced byte-identical source WAVs (SHA-256
`125a9d97d383dae20227971b91733b69b57fb50de39cad709a85765e4812216f`).
This isolates host playback differences from guest audio generation on that
route. It does not prove that the source waveform matches original hardware.

Final executable SHA-256:
`83f81cf270e1874ebfe0a3ada3dcea8830a64522a5d06fc3bba751b3e0a15259`.
All 8500 frame-phase rows are present. Host presentation gap median 16.733ms,
p99 23.971ms, maximum 38.308ms; these are host call timestamps, not scanout
measurements. The corrected audio buffer tolerated this scheduling variation.

Use `tools/capture_audio_run.py --label NAME --replay /path/to/inputs.csv` to
repeat the route with your own local inputs. The runner retains an initial
save copy, preserves the personal save, captures exact device-call ordering,
hashes the executable and recordings, and requires a clean exit, requested
frame count, and complete audio capture. Output WAVs do not include downstream
SDL/device processing and do not measure physical button-to-sound latency.

## Reliability improvements

- Engine pin: `577846ce7755c428c50777468a9f63c8631e4b58`.
- A fresh GitHub clone of game `7f304a8`, using the published setup script
  and pinned engine, regenerated BIOS/game code and built successfully. Its
  independent engine suite passed 31/31 tests. A 300-frame headless strict
  boot exited 0 with zero dispatch misses, interpreted instructions, or healed
  targets. This is boot-only evidence, not strict gameplay coverage. Local
  setup, test, and strict-boot logs are retained in `build/audio-review/clean-*`.
- Engine regression suite: 31/31 tests passed; continuity tests also passed
  AddressSanitizer and UndefinedBehaviorSanitizer.
- Existing Python parser tests: 5/5 passed.
- `build/winreplay/audio-final-controls-20260919-082004`: 600/600 frames,
  600 applied-input rows, zero input mismatches, two observed backward restores,
  matching save/load frame/PC/memory digest, pause/resume and rewind verified,
  exit 0, original save unchanged. This tests host controls, not audio snapshot
  completeness or physical speaker behavior.
- The windowed harness now requires observed backward frame crossings for
  requested load/rewind operations; the former informational pass was vacuous.
- Harness reports include concealed shortage growth, not only hard underruns.
- Audio initialization and capture write failures are reported explicitly.
- Audio/video captures are ignored by Git. No ROM, BIOS, save, generated game
  code, cache, or raw recording is part of this publication.
- The setup engine pin is updated to the published fix, and CMake's default
  engine path matches the documented sibling checkout layout.

## Outstanding work

- Confirm the physical listening experience. These recordings stop at SDL's
  callback boundary; the original September 9 session has no recorded sound.
- Complete broader playthrough testing, especially victory/rewards, subsequent
  battles, area changes, and long save/reload sessions. An 8500-frame route is
  not a whole-game compatibility claim.
- Investigate remaining unresolved runtime targets. Baseline: 345 dispatch
  misses, 339 healed targets, 9,523,614 interpreted instructions, six failures.
  Final live replay: 348 misses, 342 healed targets, 9,138,908 interpreted
  instructions, six failures. Scheduling changes affect discovery counts.
  Function counts are discovery data, not a completion percentage.
- Address audio snapshot completeness and flushing of host audio on load/rewind
  with compatibility tests. Existing wave/noise channel state is omitted from
  snapshots; this was identified in review but is not changed here.
- Active background compilation can still delay shutdown: queued jobs are
  cancelled, but a running compiler job is joined. A hung compiler needs a
  bounded cancellation design, not a false successful-test label.
- Cross-platform audio/device testing and comparison with an independent
  emulator/hardware oracle remain necessary for broader accuracy claims.
