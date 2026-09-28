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

## Addendum — R16 divergence follow-up, 2026-09-24 (measured, route-level only)

Four hot IWRAM mixer dispatch PCs from the recorded route were admitted as
reviewed `[[extra_func]]` roots (each a verified PUSH-prologue function
inside the existing mixer `code_copy` span; `03006F54` contains an internal
call, so it is documented as a function, not a leaf). The two sourceless
stack PCs were left out. On the identical 8500-frame route: failed heal
targets 6 → 2, interpreted instructions down about 22%, and the four PCs
are absent from misses (statically covered). The engine gained
once-per-PC heal-failure diagnostics (unit-tested) and now names the two
remaining RAM failures with reason instead of failing silently. Coverage
label stays NOT_STATIC.

Replay determinism (source PCM, 9,321,346 samples each, identical inputs
and starting save): baseline vs one four-root run differs by exactly one
16-bit sample (index 2037684, ~31.1 s); a second four-root run matches
baseline exactly; a zero-root rerun under the same patched engine differs
by exactly one sample each at two other positions (indexes 1991599 and
4994774). All other samples are bit-identical across all four captures.
The flakes occur in both variants at run-varying positions, so a strict
determinism claim fails, while no systematic codegen divergence is evident.
Plausible causes under investigation: capture-ring copy race at heal
boundaries, transitional backend edges under varying heal scheduling, or
audio-HW-state phase; guest CPU/RAM checkpoints (see below) do not
implicate the four roots' steady-state code.

Save/load audio verdict (new `tools/audio_restore_check.py`, battle-state
route, guest-frame-aligned segments): **not equivalent**. Post-restore
guest RAM diverges from the uninterrupted branch within about 4 guest
frames (3 IWRAM bytes plus IRQ-bank residue at the same frame/PC), and the
divergence reproduces with healing fully disabled (pure interpreter both
branches), ruling out a native-vs-bridge backend edge. Restored CPU/RAM
digests match exactly at the load boundary, so the mechanism is post-load
state (I/O, audio-HW voice phase, or device timing) rather than a bad
restore image. The known ch3/ch4/wave-RAM reset is one audio-HW delta;
the RAM path needs further tracing. Snapshot format unchanged.

Follow-up localization (same-frame digest pairs, pure interpreter):
EWRAM/VRAM/PAL/OAM and active registers are bit-identical at the fork;
only an IWRAM task-context block (saved-CPSR N/Z bit, two counters),
IRQ-bank LR/SPSR residue, timer0 counter (±2 ticks), and PPU dot phase
(±1) differ. First divergence lands on the second post-load guest frame
(27964; frame 27963 still matches) with no input edge nearby, and the fork
persists at least 400 frames — a persistent fork, not a transient blip.
Idle elision off and identical inputs change nothing. Writer: game IRQ
dispatcher entry `0x03005E00` (caught via watchpoint writing the
interrupted-CPSR image; the IWRAM block is its IRQ-stack area), so the
class is IRQ-delivery timing, not task logic. Trigger mechanism (measured
via a read-only `runtime_pending_cycle_stats` hook, no behavior change):
save/load boundaries routinely carry unflushed device cycles the snapshot
does not cover (save: pending 52 / budget 84; loads at different pumps:
293/234 and 63/864), and post-load evolution varies with that residue
(same file, same inputs: digests `84383c51` vs `284497c9` vs uninterrupted
`144f6a58`). The constant ch3/ch4/wave reset therefore cannot alone
explain the variance. Standing isolated cause: stale host
pending-cycle/event-budget state flushing into restored devices (observed
as ±2 timer ticks and N/Z IRQ-context residue); proposed minimal fix
(not applied): flush pending devices plus resync the event budget inside
the save/load boundary, or snapshot the two counters with a versioned
compatibility plan. Watchpoint note: abort-on-address only observes paths
that emit trace events with word-aligned addresses, so native/interp
guest stores do not trip it; the writer above was caught at function
granularity instead.

Restore-harness repair (same date): `tools/audio_restore_check.py` now
reports capture validity separately from audio equivalence (exit 0 = both,
1 = valid run but non-equivalent audio, 2 = invalid/missing events or
failed self-test), parses and asserts save/load frame+PC+digest identity
plus event order/uniqueness, aligns segments by guest-frame arithmetic
(predicted split 442191 vs found 442166), and carries an always-on
negative control plus unit tests (`tools/test_audio_restore_check.py`,
11 passing). Live no-load control exits 2 as designed. The stale-tail
metric is source continuity only, not host-queue proof. Voice activity at
the battle save point is measured live: NR52 channel flags show PSG
voices 1-4 on (wave + noise live), MP2K reports 4/12 channels active, so
the snapshot's ch3/ch4/wave-RAM reset destroys live voice state on every
restore of this route. First divergent writer caught via watchpoint: the
game IRQ dispatcher entry `0x03005E00` (static native) writing the
interrupted-CPSR image — the IWRAM "context block" is its IRQ stack area,
confirming IRQ-delivery timing (not task logic) as the divergence class.
Root trigger (which device event fires first-differently) still open;
candidate next step is host pending-cycle/budget logging or controlled
save-phase variance, not a format change.

The `03005EFC` hang-watchdog sample is byte-identical in baseline and R16
evidence (same PC/cycles/VBlank/registers) at a deterministic early-route
point; the route completes with matching final state. Recorded as a
pre-existing matching dump with route completion only; deeper onset
tracing deferred.

Limits: one route, one host rate (65536 Hz), one machine; no speaker,
device, fast-forward, or cross-platform claims. Raw captures stay ignored
and local.

---

## Addendum 2 — wave/noise fixture + fade re-check, 2026-09-24

Engine `audio_snapshot_tests` (two strict entries, no `WILL_FAIL`):
`--check=preserved` requires serialized channels (ch1/ch2) to restore
exactly; `--check=omitted` passes only by demonstrating the specific gap
(ch1/ch2 intact, ch3/ch4 silent, early first difference). Measured:
square voices restore bit-exactly (ch1/ch2 energies identical pre/post),
while wave (ch3) and noise (ch4) energies drop to zero post-restore with
the first differing sample at index 3. Crashes, setup failures, and wrong
results exit distinctly nonzero and cannot masquerade as demonstrated.
Flip omitted to full equivalence only with a versioned serializer change
plus compatibility plan.
Separately, the R19 sprite-fade fix was re-checked live on real game
code: battle-entry white flash washes uniformly, and the game-over
brightness-decrease fade reaches pure black with zero lingering bright
pixels (linger metric 0.35 → 0.000 by mid-fade); GAME OVER text renders
cleanly. Story-dialogue portraits specifically still need deeper
progression to stage.

## Addendum 3 — phase-normalization experiment, 2026-09-24

Historical note: the pure-interpreter rejoin claim in this addendum was
subsequently found to involve a stalled guest. It does not establish that a
restored game ran forward; see Addendum 3a and 4. The normal-healing results
are separate.

A read-only probe showed save/load boundaries routinely carry unflushed
device cycles the snapshot does not cover. An opt-in diagnostic path
(`GBARECOMP_SAVELOAD_PHASE_SYNC`, default off, no format change) now
flushes pending device time before saving and drops stale residue plus
re-arms the horizon after loading (file and in-memory paths). Under pure
interpreter with identical state/inputs, uninterrupted vs restored
branches at the same guest frame/PC produced identical digests with the
opt-in on (`144f6a58` both; control diverged `144f` vs `8438`), but the
guest had not advanced past its boot frame; that result is non-diagnostic
for post-load gameplay. The opt-in leaves uninterrupted execution unchanged
on the measured route. A narrow regression script
(`tools/test_save_restore_phase.py`) asserts the recorded digest comparison;
its exit 0 does not establish meaningful interpreter progression. With
normalization, restore audio still differs
identically (residual 2198.76, first diff offset 0): the remaining audio
gap is the ch3/ch4/wave-RAM voice loss, independent of the RAM fork.
No default behavior changed; the opt-in is experiment-only pending
review of a permanent fix with compatibility plan.

### Addendum 3a — per-mode scope correction, 2026-09-25

> **SUPERSEDED IN PART — 2026-09-25.** The pure-interpreter cells below were
> measured on a STALLED guest: every `interp` cell's final save reports the
> boot frame 27959, i.e. the guest advanced 0 frames between boot and the
> comparison point. The `save`/interp and `both`/interp "rejoin 3/3" rows are
> therefore a same-instant CPU/memory comparison at the boot state, not
> evidence that a restored guest ran on. The sentence "the save-side flush
> alone is sufficient ONLY under the pure interpreter" is **WITHDRAWN**, and
> the harness now treats `save` as measure-only on both backends. The healing
> rows are unaffected. See Addendum 5a for the corrected framing.

Addendum 3's pure-interpreter digest match is historical but not a
post-load rejoin result: those cells advanced zero guest frames. The normal
healing cells did advance. The historical per-mode matrix on the one banked
battle route (`tools/phase_sync_matrix.py`, pumps 25/100/405; the intended
>=400 post-load guest frames did not occur in interpreter cells) was:

| mode | pure interpreter (stalled; non-diagnostic) | normal healing |
| --- | --- | --- |
| `off` | forks 3/3 (control) | forks 3/3 (control) |
| `save` | rejoins 3/3 | **1/3, and 2/7 in a finer sweep** |
| `load` | forks 3/3 (control) | forks 3/3 (control) |
| `both` | rejoins 3/3 | rejoins 3/3, and 6/6 in a repeated matrix |

The earlier conclusion that save-side flush alone suffices under pure
interpreter is **withdrawn**. Under normal healing, save-only is boundary
dependent and **is not established as safe**; the load-side reset contributes
to the observed both-mode result. The current harness treats save-only as
measure-only on both backends and checks off/load negative controls in
aggregate. Its both-mode digest assertion on a stalled interpreter cell is
only a same-instant check, not evidence of gameplay equivalence; a future
progression gate is needed before that backend can support such a claim.

Offline `--verdict` now applies the same standard as a live run
(`repeat_stable` included; a report missing a gate key is reported MISSING
and fails rather than passing). Reports written before the horizon
assertion existed carry no `horizon_check`; those are recomputed from the
stored save/load pending arrays, and a cell whose stored data cannot decide
the horizon is reported `unverifiable` and fails instead of passing
vacuously.

Scope, restated precisely because it is easy to over-read: the rejoin
criterion is frame + PC + guest CPU/memory digest at the boundary and at the
final save. It is **not** full PPU/timer/APU equivalence and **not** audible
equivalence. The horizon check confirms the post-load event budget equals the
restored device's cycles-to-next-event; it does not confirm the guest went on
to behave identically, only that the mechanism it targets is in place. One
route, one host rate, one machine. Phase sync remains default-off and
experiment-only.

## Addendum 4 — in-memory (rewind) restore path, 2026-09-25

The file matrix above exercises only the FILE save/load path. The engine has
a second restore path, `do_savestate_load_bytes`, reachable only from the
rewind branch of the host-input pump. `tools/inmem_rewind_check.py` covers
it. No engine change was made.

**Trigger, established rather than assumed.** `do_savestate_save_bytes` /
`do_savestate_load_bytes` have exactly one caller (the rewind capture/rewind
branch), reached by `GBARECOMP_ASSIST_SCRIPT="<pump>:rewind"` in a windowed
run; the game sets `opts.rewind_history_seconds = 10` with a 15-frame capture
interval. Every cell asserts the requested assist script ran verbatim, that a
`rewind_loaded` line appeared (logged only by that branch), and that NO
`savestate_loaded slot=` line appeared (logged only by the file path) — so a
passing cell cannot have gone through the file path.

**Geometry (measured, identical in every cell).** Restore at pump 300 from
boot frame 27959: guest frame at the trigger 28257, restored frame 28184, so
73 frames back. That is the engine's rule (newest capture at least 60 frames
old, captures every 15 frames) landing on the 15-frame grid — 73 not 60, as
the quantization predicts. Boundary identity held in every cell: the restored
PC (`0x08000bc4`) and 384 KB guest-memory digest (`b7af6e7bd5d283a9`) equal
the uninterrupted run's at guest frame 28184. The in-memory round trip is
exact; what differs is what happens *after* it.

**Per-mode results, default healing backend, one rewind point, pumps
25..715, longest post-restore observation 403 guest frames:**

| mode | CPU/mem digest rejoin | boundary | horizon (pending/budget/mixer) | per-frame sampled register deltas (see 5e) | equivalent |
| --- | --- | --- | --- | --- | --- |
| `off` | no (fork) | yes | no | diverges | no |
| `save` | yes | yes | no | diverges | no |
| `load` | no (fork) | yes | no | diverges | no |
| `both` | **yes** | yes | **yes** | **6476/6476 records, no divergence** | **yes** |

`off`, `save` and `load` all first diverge at guest frame **28186**, two
frames after the restore, at **`0x04000100` (TM0CNT_L)** — `0xd6` in the
uninterrupted run against `0xd9` / `0xd9` / `0xd8` in the restored runs. That
is the same timer-counter signature (±2–3 ticks) the earlier file-path
investigation reported, reproduced independently on the in-memory path.

The stale-horizon mechanism is visible directly. At guest frame 28197 the
uninterrupted run reports `pending=18 budget=240`; `off` reports
`pending=14 budget=244` and `load` `pending=13 budget=245` — the pre-rewind
host residue, unrestored, exactly as the design note predicts. `both` reports
`pending=0 budget=240`, matching. The serialized mixer sample counter matches
in every mode, so the sample stream position is not what the horizon defect
moves.

**Save-only is not equivalent here, and the digest alone would have hidden
it.** `save` rejoins the CPU/memory digest at all five observation points
including +403 frames, yet still fails the horizon at every probe and still
diverges the sampled register deltas at frame 28186. This is a stronger result
than the file matrix's "boundary dependent" and it is why the harness now
reports the layers separately: a digest rejoin is not device equivalence.

**Sound registers.** The rewind tick's shadow-vs-restored delta is 31 records
touching `io`/`ppu`/`sound`/`timer`, identical in all four modes, including
NR51 (0x63–0x65), NR53/NR54, SOUND1CNT_X, SOUNDCNT_L and the sound FIFO
(0xa0–0xa5). This describes how the I/O registers differ *across the rewound
span*; it is **not** a serialization gap (a register the snapshot omits keeps
its live value and would not appear here). The Sound 3/4 + wave-RAM coverage
question is unchanged and is settled by the engine's `audio_snapshot_tests`
(`--check=preserved` / `--check=omitted`, both passing), not by this listing.

**Honest limits.**
- Sampled-register-delta agreement is **not audible equivalence** and not
  full device equivalence (see 5e). Nothing
  here measures host audio-queue state after a load, and phase sync does not
  address the un-serialized Sound 3/4 / wave RAM. Audio restore remains
  non-equivalent.
- One route, one rewind point, one host rate, one machine. `both` clearing
  6476 consecutive sampled register deltas across 403 post-restore frames is strong
  evidence on this route, not a general guarantee.

**Blocker: the pure-interpreter backend cannot drive this path at all.** The
rewind needs at least 75 guest frames of capture history. On this fixture the
interpreter advanced **4 guest frames in 715 pumps** (measured with a 10-save
ladder: frames 27959 for pumps 5–125, 27960 for pumps 155–275) versus 1 frame
per pump when healing, and the engine itself reports `rewind history is not
ready yet` (with 3 hang-watchdog hits). The harness gates on this before
spending the cell and reports those four cells as BLOCKED / not measured
(exit 2 if nothing at all could be measured). This is a backend speed limit,
not a phase-sync result, and it reproduces with no rewind in the script.

**Consequence for the retained file-matrix `interp` cells.** The same stall
applies there: every `interp` cell's final save reports guest frame 27959,
i.e. the boot frame, so the guest never advanced a frame between the boot load
and the comparison point. Those cells are a same-instant CPU/memory
comparison at the boot state; the "≥400 post-load guest frames" claim does
**not** hold for them, and their `rejoin` verdicts should be read that way.
The `heal` cells do advance and are unaffected by this correction. The
`both`/heal claim does not depend on the interp cells at all.

## Addendum 5 — boundary sweep + host audio queue, 2026-09-25

### 5a. The defect is BOUNDARY-DEPENDENT (corrects Addendum 4's framing)

Addendum 4 measured one rewind point and reported "`off` forks, `both`
rejoins". Sweeping the in-memory rewind across three restore boundaries
(`tools/inmem_rewind_check.py --pumps 200 300 450 --frames 1400`, healing
backend, longest post-restore observation 403 guest frames each) shows that
framing was too clean. Per-boundary result, `equivalent` meaning CPU/memory
digest **and** host pending/budget/mixer marker **and** all recorded
PPU/timer/DMA/sound/IRQ per-frame sampled register deltas:

| rewind pump | restore frame | frames back | `off` | `save` | `load` | `both` |
| --- | --- | --- | --- | --- | --- | --- |
| 200 | 28094 | 63 | **equivalent** | fork | fork | **equivalent** |
| 300 | 28184 | 73 | fork | fork (digest rejoined, device did not) | fork | **equivalent** |
| 450 | 28334 | 73 | **equivalent** | fork | fork | **equivalent** |

- `both` is equivalent at **3/3** boundaries (19221 / 17545 / 15237 sampled
  register deltas matched with zero divergence, at every boundary, +403
  frames out).
- `off` forks at **1/3**. The defect is real but intermittent here, and it is
  not a function of rewind distance: pumps 300 and 450 are both 73 frames back
  and only one forks.
- `load` forks at **3/3**.
- `save` is **0/3** equivalent. At pump 300 it rejoined the CPU/memory digest
  at all five observation points yet still failed the horizon and still
  diverged the sampled register-delta sequence — the clearest single
  demonstration that a
  digest rejoin is not device equivalence.

So `off` is **not** a reliable negative control on this route, and Addendum
4's framing ("off forks") is corrected: the honest statement is that the
defect is boundary-dependent and was exposed at 1 of 3 sampled in-memory
boundaries (and at 3 of 3 sampled file-path load pumps). First divergences
remain on `0x04000100` (TM0CNT_L) at ±1–3 ticks in most cells; `load` at pump
200 diverged harder and differently — the first sampled deltas reported
`0x04000014` (DISPSTAT) in U versus `0x04000100` (TM0CNT_L) in the restored
branch. Per-frame sampling cannot establish the underlying write order.

**Harness rules corrected to match.** `both` is asserted per boundary (the
fix must not itself be boundary-dependent). `off` and `load` are now asserted
in AGGREGATE — each must fork at least one sampled boundary, which is the bar
for a non-vacuous negative control; per-cell "must fork" wording had failed
two of the three measured `off` cells for rejoining. `save` is measure-only
on both backends: the former `save`+interp True assertion is **withdrawn**,
because those cells came from the stalled-guest interpreter runs described
below. Reports now carry a `rule_version`, and `--verdict` recomputes
rule-derived verdicts instead of inheriting a stored rule's, printing a note
when the stored rule differs. A retained report written before that field
existed can be re-verdict'd without modifying it: `--verdict-json <path>`
(available in both `phase_sync_matrix.py` and `inmem_rewind_check.py`) writes a
CURRENT-RULE copy, preserving the original as historical evidence. Current-rule
copies of the four retained matrix reports are kept as
`build/audio-restore/*-currentrule.json`, and
`build/inmem-rewind/multipump-20260925-currentrule.json` for the multipump
sweep; each recomputes the enforced expectations (a stale `save`+interp
`expect_rejoin=True` is preserved under `expect_rejoin_as_stored` and resolved
to `None`), the horizon check, and the failure tags.

### 5b. Host audio queue across a restore (new tool, previously "not implemented")

`tools/audio_restore_check.py` states outright that its stale-tail metric
"does NOT test the SDL host bridge queue (source is captured before the
bridge). Queue leakage would need post-bridge callback capture with the load
event mapped onto the callback timeline (not implemented)." No tool read
`output.wav`. `tools/host_audio_queue_check.py` now does, with no engine
change, using the `GBARECOMP_AUDIO_CAPTURE` per-record host-bridge
instrumentation (one `P` push record per guest frame, plus the post-bridge
`C` callback timeline).

- **Exposure, measured.** At the instant of a restore the host ring holds
  **20.3–27.5 ms** of already-generated audio (means over 3 repeats:
  `rewind-off` 20.60 ms, `load-off` 21.32 ms, `rewind-both` 27.17 ms), inside
  a ~20–51 ms steady-state band and consistent with the configured
  `cushion=40ms`. The engine flushes **nothing** on either restore path
  (code-verified: no audio-device call in `do_savestate_load` or
  `do_savestate_load_bytes`), so that much pre-restore audio plays before
  restored audio is heard. Queue leakage is therefore **present and
  ~20–27 ms**, not absent. The no-restore control read higher (41.7–43.2 ms);
  since the control contains no restore, that difference is not a restore
  effect and is not interpreted here.
- **No hard device failure attributable to the restore.** Across 12
  restore-branch runs, zero stretch, zero underrun and zero overflow steps
  landed within ±3 pushes of the action. The bridge's `stretch_frames` /
  `underrun_events` are cumulative and step at session priming and shutdown
  (a producer stall makes the bridge conceal by looping the last waveform —
  `recomp_audio_drc.h`), observed at push indices 9 and 11, never near the
  action. An earlier single-run reading that looked like "stretch jumps to
  2077 frames right after the rewind" was **coincidence** and is withdrawn;
  the tool now reports step indices, not totals, and its attribution gate
  refuses to blame a restore for a background artifact.
- **Phase sync does not change host-queue behaviour** (`rewind-off` 20.60 ms
  vs `rewind-both` 27.17 ms, both inside the same band, both clean). It was
  never intended to.

### 5b-CORRECTION — marker-aligned re-measurement, 2026-09-25

The 5b figures above are kept as historical evidence but they do NOT support
an audible-stale-tail claim, and this subsection supersedes them. What the
first revision actually did: it **did not open or compare `output.wav`** (the
claims to the contrary were wrong; it only parsed `<prefix>-events.csv`); it
indexed producer ('P') records by the assist pump number **without a verified
mapping** (`runtime.cpp` pushes audio BEFORE `pump_host_input`, the boot
`--load-state` emits its own markers, and a 500-frame run produces 498-499
pushes); and the **20.3-27.5 ms** figures were single ring-fill samples near a
guessed push index -- samples of the servo's oscillating ~40 ms fill -- not a
measurement of how much pre-restore audio played afterwards.

**What changed.** A read-only action marker was added to the audio capture:
`AudioCapture` appends `kind='M'` records (short label + steady_clock ns +
ring fill + bridge counters) and `HostWindow::audio_capture_marker` writes
them under the same bridge mutex as the P/C records. `runtime.cpp` emits them
at the real boundaries: `presave`/`postsave` (file save), `preload`/`postload`
(file load), `premem-save`/`postmem-save` and `premem-load`/`postmem-load`
(in-memory snapshot/restore). The vocabulary was then extended to host
actions: `fast-on`/`fast-off` (fast-forward LEVEL edges, from the Turbo
hotkey, the UI latch, or the assist script), `pause`/`resume` (applied pause
transitions only; a state-checked request that changes nothing records
nothing), and `rewind-trigger` (the rewind request reaching the dispatcher,
distinct from the `premem-load`/`postmem-load` restore it may or may not
cause). No behavior changed: markers are recorded only
when a capture is active, and nothing else touches the bridge or device. The
CSV gained a trailing `label` column; `audio_bridge_replay` accepts both
headers and re-records markers. `tools/host_audio_queue_check.py` was rebuilt
on them (rule `2026-09-25-marker-aligned`): the action boundary is the last
marker with the expected label, the scripted action must also appear in the
log on the expected path, counter steps are now scanned on the MERGED event
stream (a pull-side step was invisible to the old P-only scan) and attributed
by steady_clock window, and **a capture with no markers is refused (exit 2)
pump-index fallback is refused**.

**Re-measured (12 runs: control + `rewind-off`/`rewind-both`/`load-off` x3,
pump 300, 500 frames, same fixture and binary; evidence
`build/host-audio-queue/queue-20260925-marker-aligned.{json,log}`):**

- **Ring depth at the exact restore boundary: 47.7-50.3 ms** (3,127-3,295
  source frames at 65,536 Hz; three repeats each: `rewind-off`
  47.72-49.36 ms, `rewind-both` 48.85-50.28 ms, `load-off` 48.96-49.33 ms).
  The no-restore control at the same route point read 43.96-47.85 ms. That
  depth IS queued pre-restore audio: the restore paths still make no
  audio-device call, so none of it is flushed. This replaces the old
  20.3-27.5 ms figure.
- **Post-bridge output: pre-restore samples ARE identifiable after the
  restore, 9/9 branches.** `output.wav` (the callback buffer handed to SDL,
  pre-device) was compared against the source tail: best zero-mean
  correlation `corr_pre` 0.990-0.997 at lag 0/-1 frame from the position an
  unflushed ring predicts, over the whole queued window (L = ring depth),
  against `corr_post` -0.08..+0.10 for post-restore source content. The
  self-test for this check (synthetic delayed copy vs synthetic flushed
  ring) is in `tools/test_host_audio_queue_check.py`.
- **No bridge counter step in any restore window**: 9/9 restore branches had
  zero stretch/underrun/overflow steps within +/-150 ms of the boundary, and
  the 3/3 controls were clean, so nothing here needed attribution refusal.
- **Verdict `QUEUE OK`** (exit 0) under the tool's rule: a noisy control,
  missing marker alignment, or an undecisive output check yields
  `QUEUE FAIL`/`QUEUE UNRESOLVED` instead, and `QUEUE UNRESOLVED` never counts
  as OK. Binary sha256 `4016d1a08234e70c948f93e7a3c7d54a9a97683f9acd36ece1128333d55352c5`;
  source save sha256 unchanged (`8340b0db...`).
- **Capture replay exactness re-checked** on one new capture:
  `audio_bridge_replay --target-ms 40 --require-exact` reports
  `mismatched_samples: 0` (and with the default 25 ms target it does not, as
  expected -- the replay must use the capture's 40 ms cushion).

**Host-action markers (format/API validation, 2026-09-25).** One 140-frame
capture with `GBARECOMP_ASSIST_SCRIPT="5:fast_on;9:fast_off;11:pause;"
"13:resume;136:rewind"` recorded, in order: `fast-on` (ring fill 50.13 ms),
`fast-off` (3.28 ms), `pause` (30.87 ms), `resume` (7.45 ms), `rewind-trigger`
(51.32 ms) immediately followed by `premem-load`/`postmem-load` at the same
fill, and the `host_pause`/`rewind_loaded` stdout lines agreed. This validates
the marker API and timeline only. The ring-fill movement across those edges
(fast-forward and pause each drained the ring in this run) is a single-run
observation that those markers make measurable, not a claim about fast-forward
or pause audio behavior; the capture is retained under
`build/host-audio-queue/host-action-markers-*`.

**What this does and does not prove.** It proves that, on this route and
configuration, the ring holds ~48-50 ms of already-generated audio at the
restore boundary and that this specific pre-restore source material is what
the post-bridge callback renders immediately after the restore (sample-level
correlation, not inference). It does NOT measure how long that stale audio
remains perceptible, and it says nothing about downstream SDL/device mixing or
speakers. The 5b attribution statement (no counter step attributable to the
restore) is unchanged and now measured with exact time alignment.

### 5c. Code asymmetry found while doing 5b (latent, not currently observable)

`do_savestate_load` (file) calls `gba_mod_audio_on_savestate_load()` — which
stops all fixed and stream delivery and restores native gain to 100.
`do_savestate_load_bytes` (in-memory/rewind) does **not**. Extracted call sets:

    FILE  do_savestate_load        -> gba_mod_audio_on_savestate_load,
                                       sync_frame_counter, runtime_load_phase_reset,
                                       runtime_fp_reset, (+ 2 logging-only probes)
    MEM   do_savestate_load_bytes  -> sync_frame_counter, runtime_load_phase_reset,
                                       runtime_fp_reset

So a rewind leaves mod/plugin audio delivery state untouched where a file load
clears it. **Latent for MMBN3**: the game registers no mod audio
(`src/main.cpp`: "No mod catalog yet"; nothing in `src/` or `game.toml` calls
the mod-audio API), so the call is a no-op here today. It is recorded because
it becomes live as soon as a mod with audio is enabled, and because the
asymmetry is the kind of thing that should be decided deliberately rather
than inherited. No engine change was made.

**Proposed fix (NOT applied).** Call `gba_mod_audio_on_savestate_load()` from
`do_savestate_load_bytes` as well, or factor the two load paths through one
helper so the two cannot drift. Before applying it, an engine test should pin
the intended semantics for in-memory restore (rewind) specifically: whether a
rewind should stop plugin/native audio delivery exactly like a file load, or
whether rewind is intended to be lighter-weight. That decision is behavioral
and belongs in review, not in this checkpoint.

### 5d. Still not established

- **Audio restore is still non-equivalent.** Everything above is sampled
  register-delta and queue-depth evidence. The Sound 3/4 + wave-RAM
  serialization gap is untouched (`audio_snapshot_preserved` /
  `audio_snapshot_omitted` both still pass), and nothing here measures what a
  listener heard. The pre-restore tail IS now measured at the boundary
  (~48–50 ms) and identified sample-level in the post-bridge output (5b
  correction); its perceptual length/effect is still not measured.
- **No device-state digest exists.** The register comparison is per-frame
  sampled deltas (5e); intermediate writes, cancellations, internal device
  state and within-frame ordering are unobserved. A device-state probe with a
  stated coverage definition and unit tests would be a separate project.
- **No host-queue flush is applied.** Draining or rebuilding the ring on a
  restore would be a gameplay/audio behavior change. It is deliberately NOT
  implemented in this work and would need its own before/after measurement
  (including how a flush interacts with servo priming and the 40 ms cushion).
  The markers added here make that measurement possible; they do not perform
  it.
- **The reported crackle at 2:08–2:18 is untouched.** No run here reaches
  frame ~7680; the longest is 1400 frames. That window remains open and is
  not explained by anything measured here.
- Still one route, one host rate (65536 Hz), one machine, three restore
  boundaries.

### 5e. What the register comparison IS (sampling semantics, corrected)

The audit correction that triggered this subsection: the harness's I/O
comparison is **not** a register write stream. `GBARECOMP_WRAM_TRACE` routes
through `wram_trace_tick` (`runtime.cpp`), which samples the raw bytes ONCE
PER PPU FRAME and emits only the NET change. Intermediate writes inside a
frame, writes that cancel back to the previous value, and all internal device
state are invisible, and there is no within-frame ordering. It is a
**per-frame sampled register-delta** comparison. The 19221 / 17545 / 15237
matches above are matches of that sampled surface — they are NOT full device
equivalence, and the harness/docs now say so everywhere. If a stronger claim
is ever wanted, it needs a separate read-only device-state digest probe with
an explicit coverage definition (what is included, what is deliberately not)
and its own unit tests; that probe does not exist today.

### 5f. Host-action edge sweep: fast-forward / pause / resume, 2026-09-25

New read-only tool `tools/host_action_ring_sweep.py` (rule
`2026-09-25-host-action-sweep`; parsing and step detection imported from
`tools/host_audio_queue_check.py`, so 5b-CORRECTION's marker-aligned
definitions are the same code). From the banked fixture
`build/winreplay/gate2-battleI-action/rom.state1` + trace
`build/winreplay/gate2-battleJ-action2/trace.csv`, 240 frames, phase sync
off:

- edges: `GBARECOMP_ASSIST_SCRIPT="20:save1;120:fast_on;140:fast_off;170:pause;190:resume;210:save2"`
- control: same fixture/flags, `"20:save1"` — no fast-forward, pause or
  resume at all

and measures ring fill and bridge counter steps **strictly inside** each
marker-bounded interval, on the merged P/C/M steady-clock stream. Counter
steps in the first 1.0 s after the first producer push are `startup_steps`
(context): the initial state load can stall the producer and drain the ring
with no host action (observed in 2/10 edges runs and 1/10 control runs here;
4/5 controls in an earlier 160-frame batch) and is never attributed.

Two `--repeat 5` batches, 10 edges + 10 control runs total:

- `build/host-action-sweep/sweep-20260925-settled.json` — **SWEEP OK**
  under the tool's clean-control rule (controls step-free 5/5). This is a
  batch-level comparison, not proof that the actions exclusively caused the
  edge steps.
- `build/host-action-sweep/sweep-20260925-settled-b2.json` — **SWEEP FAIL,
  attribution refused**: control r3 moved `stretch_frames` 11 times after a
  98.8 ms no-action producer stall (below). A batch whose own no-action runs
  step cannot attribute the edge steps; it reports them as measurements only.

These retained JSON files carry the original `2026-09-25-host-action-sweep`
rule label and were not overwritten. A read-only re-verdict under the stricter
`2026-09-26-host-action-sweep-v2` gate leaves the two five-repeat outcomes
unchanged. It rejects the older `build/host-action-sweep/sweep-smoke2.json`
(`SWEEP OK` under the old rule) because one paired repeat cannot support a
repeatability claim. Version 2 also checks control timeline integrity,
settle-window placement and complete metric coverage; an `OK` remains a
control-clean association within that batch, not causal proof.

Measured (pooled n=10; fill in ms; med = median):

| | fast-on -> fast-off | pause -> resume |
| --- | --- | --- |
| interval length | 99.53–100.30 ms | 223.4–226.1 ms |
| ring fill at opening edge | 42.48–52.83 (med 47.17) | 41.50–57.86 (med 57.69) |
| ring fill at closing edge | 0.229–0.244 (med 0.238) | 0.232–0.244 (med 0.240) |
| loop concealment (`stretch_frames`) | 44.63–57.50 ms | 79.99 ms, 10/10 |
| fade-to-silence (`underrun_frames` = unfilled output frames) | 0 ms, 10/10 | 89.04–105.40 ms |
| fill back at 40 ms after the closing edge | 33.5–34.1 ms | 36.3–36.7 ms |
| overflow drops | 0, 10/10 | 0, 10/10 |

Mechanism (read from `src/runtime/recomp_audio_drc.h` + `host_window.cpp`,
matching the data): with fast-forward active the runtime skips
`push_audio_samples` entirely, and while paused no guest frames run, so in
both intervals the ring is only drained while the device keeps pulling
(512 frames = 7.8125 ms of source audio per pull). The action outlasts the
ring's fill in every repeat, so the ring reaches ~0.24 ms in 20/20
intervals. A shortfall below the engine's stall-concealment limit
(`stretch_limit_ms = 80`, i.e. 80 ms x 65536/1000 = 5242 output frames) is
covered by a pitch-preserving loop of the most recent audio; past it the
bridge fades to silence and counts unfilled output frames. The data matches
that exactly: the fast interval's concealment (44.63–57.50 ms) tracks
interval minus fill and never reaches the cap, while the pause interval hides
exactly 79.99 ms (5242 output frames) of its stall by looping and spends the
rest (89.04–105.40 ms) fading out. So a pause edge does not merely "dip" —
the device output is a looped copy for ~80 ms and then hold/silence until
live audio resumes.

Controls (same fixture, no fast/pause/resume, n=10): 8/10 runs had a largest
producer gap of 17.5–19.2 ms, never fell below 19.5 ms of post-prime fill and
moved no counter after the settle window. 2/10 did what the edge intervals
do, with no host action at all:

- control r0-b2: 80.9 ms producer gap at +83 ms (startup; pre-settle, reported
  as `startup_steps` = 7), drained to 0.229 ms.
- control r3-b2: 98.8 ms producer gap at +3280 ms, post-settle, drained to
  0.240 ms and took 11 stretch steps. The device kept pulling normally across
  it (pull records ~10.65 ms apart throughout), so this was an
  emulator/producer stall, not a device stall. Cause not isolated: the only
  coincident anomaly is an irregular rewind-history capture cadence
  (in-memory save markers at +3247 then +3566 ms, versus ~251 ms elsewhere in
  the same run), and the in-memory save those markers bracket took 90 us, so
  the slow part is not the serialize call itself. This is why batch 2 refuses
  attribution.

What this does and does not support:

- SUPPORTED: at these scripted actions the ring reaches ~0.24 ms and the
  bridge steps in **20/20 intervals**; the concealment amounts are explained
  by interval length minus fill; the 80 ms concealment cap is hit exactly on
  every pause (10/10) and on no fast-forward (0/10); overflow drops are 0 in
  20/20; the cushion rebuilds to 40 ms after ~34 ms (fast-off) and ~36 ms
  (resume) in every repeat.
- NOT SUPPORTED: that these steps are exclusive to the actions. The no-action
  control produced the same drain + concealment in 2/10 runs (1 pre-settle at
  startup, 1 post-settle). The actions make the drain deterministic (a
  100 ms / 223 ms producer mute versus a 42–58 ms ring); a control hits it
  only when something else stalls the producer that long (2/10 runs here,
  load-dependent). A strict causal claim needs more than one step-free
  control batch: matched no-action windows and wider repeats are needed,
  especially given the post-settle stall in batch 2.

Limits: one fixture/route, one machine, one host rate (`got=65536Hz/512`,
CoreAudio); the capture is the SDL callback buffer (pre-device, so no
perceptual claim); 10 runs per branch, and the two intervals inside one run
are not independent; the recovery times have the resolution of one record
(pull blocks are 512 frames = 7.8125 ms of source audio, observed record
spacing 0.02–11 ms), so sub-millisecond spreads above are not meaningful
beyond "about one pull block"; the control lacks the edges script's
`210:save2`; no engine behavior was changed and no ring flush is applied
(5d still stands).

Commands / evidence (`build/` is ignored; source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745` verified
unchanged before versus after each batch (not after each individual run);
measured binary sha256
`a5f226f707ea82c7783a49ecfe57bc5098c6e993d0d1a04a5aafc26a87bb50b6`):

```
python3 tools/host_action_ring_sweep.py --repeat 5 --json build/host-action-sweep/sweep-20260925-settled.json
python3 tools/host_action_ring_sweep.py --repeat 5 --json build/host-action-sweep/sweep-20260925-settled-b2.json
python3 tools/test_host_action_ring_sweep.py    # 30 tests
```

`test_host_action_ring_sweep.py` covers the rules the numbers rely on: steps
counted only strictly inside the marker pair on the merged stream, pre-settle
steps excluded from attribution but kept as context, malformed marker sets
(missing / duplicate / out-of-order) refused rather than guessed, and
`decide()` refusing attribution whenever a control moved a counter.

### 5g. Matched-window no-action baseline, 2026-09-26

New read-only comparator `tools/host_action_baseline.py` (rule
`2026-09-26-matched-window-v1`) supplies the apples-to-apples comparison
§5f asked for: in runs with no host action at all, do windows of the same
duration as the scripted intervals show the same ring drain, bridge counter
steps and producer gap? It reanalyzes existing captures offline; it never
writes to a run dir or a retained JSON.

Method (declared rules, not tuned per run):

- For each edges run the two action intervals are recomputed with the
  sweep's own `interval_metrics`, so fill/steps keep the reviewed
  strictly-inside-the-markers semantics.
- For each no-action run the settle boundary is recomputed from the raw
  capture (first producer push + 1.0 s) and windows of the PAIRED duration
  (the paired edges run's measured interval; 99.5–100.3 ms / 223.4–227.3 ms
  here) slide over the post-settle P/C/M stream. Anchors are P/C records;
  a window is the half-open span `[anchor, anchor+D)` and is skipped when
  the capture ends before it closes.
- `fill_min_ms` is the minimum fill inside the span; a counter step counts
  when `anchor_idx < step_idx < close_idx` (the same boundary rule as the
  action intervals); `gap_overlap_ms` is the largest overlap of a
  consecutive-push gap with the window; steps outside every window are
  reported, not dropped.
- The RUN is the statistical unit: per run the worst matched window is
  summarised, and the batch counts runs, never overlapping windows.
- Rejection: missing/failed/truncated runs, missing event files, malformed
  timelines, push-free captures, a post-settle span shorter than 2x the
  longest window, fewer than 10 windows per duration, and missing /
  inconsistent / non-65536 `source_rate` all REFUSE the batch. A matched
  no-action window is always reported; nothing is silently dropped.

A no-action run "matches" when any matched window drains to <= 2.0 ms (the
sweep's empty threshold). The comparison is CONSERVATIVE, not time-locked:
control windows are scanned anywhere after the settle boundary, so the
control side is the whole run's best effort to reproduce an action; no
pump-to-push arithmetic or route-time alignment is claimed.

Retained reanalysis (read-only; the retained JSONs/logs and their mtimes are
untouched):

| batch | pairs | no-action runs scanned | separated: worst matched-window drain (per run) | matching runs |
| --- | --- | --- | --- | --- |
| `sweep-20260925-settled` | 5 | 5 | 19.70–20.22 ms, 0 steps, gap overlap <= 17.67 ms | none |
| `sweep-20260925-settled-b2` | 5 | 5 | 19.51–20.84 ms, 0 steps, gap overlap <= 18.21 ms | r3: 0.240 ms, 11 steps, 98.79 ms gap at +3.3243 s |
| fresh `sweep-20260926-baseline-v2` | 5 edges / 10 no-action runs (control + control2) | 10 | 19.47–21.83 ms, 0 steps, gap overlap <= 18.47 ms | control r2: 0.239 ms, 10 steps, 100.88 ms gap at +1.3297 s; control2 r4: 0.235 ms, 10 steps, 86.02 ms gap at +2.5131 s |

Each separated run's worst 100 ms or 223 ms post-settle window still holds
19.47+ ms of fill and takes zero counter steps (518–564 windows scanned per
run); each matching run reproduces the action-interval state (drain to
~0.24 ms with ~10 stretch steps in one window). Out of 20 retained + fresh
no-action runs, 17 separated and 3 matched. Action intervals themselves
measure 0.229–0.244 ms of fill in 30/30 interval measurements across the
three batches; the no-action stalls drain to 0.235–0.240 ms — the same
state, not a near miss.

Fresh paired repetitions into the new ignored path
`build/host-action-baseline/` (5 edges + 5 control + 5 control2; source save
and binary hashes below):

```
python3 tools/host_action_ring_sweep.py --repeat 5 \
  --run-root build/host-action-baseline/runs \
  --control-script-2 "20:save1;210:save2" \
  --json build/host-action-baseline/sweep-20260926-baseline-v2.json
python3 tools/host_action_baseline.py \
  --sweep build/host-action-sweep/sweep-20260925-settled.json \
  --sweep build/host-action-sweep/sweep-20260925-settled-b2.json \
  --sweep build/host-action-baseline/sweep-20260926-baseline-v2.json \
  --json build/host-action-baseline/matched-20260926-all.json
```

The fresh edges branch reproduces §5f on the same fixture: fill at
fast-off 0.2314–0.2432 ms and at resume 0.2342–0.2439 ms (5/5), fast
concealment 7–9 stretch steps, pause concealment 11 stretch steps plus
12–14 underrun steps, cushion back at 40 ms after 33.4–33.9 ms (fast-off)
and 36.4–36.7 ms (resume). The fresh batch's sweep verdict is SWEEP FAIL /
attribution refused because its own no-action runs moved counters (the two
stalls below). That is the v2 gate working as designed, not a run failure.
Startup concealment bursts still occur in both branches (fresh edges
`startup_steps` 0–51, no-action 0–12) and stay excluded by the settle rule.

Stall status (explicit). The 98.8 ms no-action producer stall is OBSERVED,
and the matched-window comparator flags it; it is NOT causally explained.
Known instances, all post-settle and all with no host action at all:

- retained b2 control r3: 98.79 ms gap at +3280 ms, drain 0.240 ms, 11 steps
- fresh control r2: 100.88 ms gap at +1276.8 ms, drain 0.239 ms, 10 steps
- fresh control2 r4: 86.02 ms gap at +2465.3 ms, drain 0.235 ms, 10 steps

Common signature in all three: device pulls continue every ~10.66 ms
(observed intervals 0.01–11.07 ms) while producer pushes stop, so this is a
producer/emulator stall, not a device stall. Each stall coincides with a
delayed rewind-history capture marker interval (251 ms cadence stretched to
319 / 335 / 320 ms), while the in-memory save bracketed by those markers
itself takes ~90 us. No other coincidence was found. Whether the stall is
the rewind capture cadence, host scheduling on a shared machine, or another
producer-side pause is not isolated, and the matched-window tool makes no
causal claim.

Save-checkpoint symmetry. The retained control script (`20:save1`) lacks
the edges branch's `210:save2`. Two checks bound that asymmetry: (1) the
action intervals close at the `190:resume` marker before save2 exists, so
save2 cannot alter them, and a read-only probe shows normal 16.3–17.5 ms
producer gaps around the save2 marker in 10/10 retained and 5/5 fresh edges
runs (no stall); (2) the fresh batch adds a second no-action branch,
`--control-script-2 "20:save1;210:save2"`, with the same assist checkpoints
and no fast-forward/pause/resume, so 10 no-action runs (control + control2)
were scanned instead of 5. The control2 runs did execute both saves (slot 1
and 2; slot 2 at guest frame 28167 versus 28147 in the edges branch — the
actions shift the guest timeline after the intervals; the checkpoints are
the same assist frames). control2 counter steps count toward the same
control-clean gate.

Old-vs-v2 verdicts (read-only re-verdict of the retained JSONs; no artifact
overwritten; log `build/host-action-baseline/reverdict-20260926.txt`):

| retained artifact | file rule | file verdict | current gate |
| --- | --- | --- | --- |
| `sweep-smoke.json` | 2026-09-25 | FAIL | FAIL (missing edge/control runs) |
| `sweep-smoke2.json` | 2026-09-25 | OK | FAIL (a single pair cannot carry a repeatability claim) |
| `sweep-20260925.json` | 2026-09-25 | FAIL | FAIL (control moved a counter) |
| `sweep-20260925-settled.json` | 2026-09-25 | OK | OK |
| `sweep-20260925-settled-b2.json` | 2026-09-25 | FAIL | FAIL (control moved a counter) |

What this does and does not support:

- SUPPORTED: with matched durations, 17/20 no-action runs never come close
  to the action intervals in 518–564 scanned windows each (worst window
  19.47+ ms of fill, zero steps), while 3/20 no-action runs reproduce the
  action state exactly (drain to 0.235–0.240 ms with 10–11 steps in one
  window). The comparator caught every post-settle no-action stall in the
  retained and fresh captures, so the method is not blind to matched
  behavior; the fast/pause drains remain reproducible (0.229–0.244 ms in
  30/30 interval measurements).
- NOT SUPPORTED: exclusivity, causality, or perceptual claims. Three of
  twenty no-action runs did the same thing with no host action whatsoever.
  The actions are deterministic where scripted (interval length minus
  fill), but the no-action stall is a competing source at roughly 15% on
  this fixture, and the windows are conservative, not time-matched, on a
  small sample.

Limits: same single fixture/route/machine/host rate as §5f; the 2.0 ms
match threshold, 10-window floor and 2x-span floor are declared
conventions, not physics; stalls and startup bursts may be load-dependent
(the machine is shared); a matched window shows the same host-ring state,
not the same mechanism; capture is still the pre-device SDL callback
buffer, so no perceptual claim; no engine behavior was changed and no ring
flush is applied (5d still stands).

Commands / evidence / tests (`build/` ignored; source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745` and
binary `a5f226f707ea82c7783a49ecfe57bc5098c6e993d0d1a04a5aafc26a87bb50b6`
verified unchanged):

```
python3 tools/test_host_action_baseline.py    # 34 tests
python3 tools/test_host_action_ring_sweep.py  # 38 tests (2 new: control2 gate)
```

Evidence: `build/host-action-baseline/sweep-20260926-baseline-v2.{json,log}`,
`matched-20260926-{fresh,retained-v1,all}.{json,log}`,
`reverdict-20260926.txt`, per-run captures in
`build/host-action-baseline/runs/`. The comparator's 34 tests cover boundary
counter steps (anchor / closing record excluded, strictly-inside counted),
settle exclusion, no/too-few-window and short-span rejection, malformed and
push-free captures, rate and run-record rejection, run-level aggregation,
paired-duration sourcing, steps outside windows, bounded gap overlap, and
the OK / NOT SEPARATED / REFUSED / INSUFFICIENT verdicts.

Tool changes for fresh evidence (v2 gate semantics unchanged): the sweep
tool gains `--run-root` (default still `build/host-action-sweep`) and
`--control-script-2` (default absent; adds the `control2_runs` branch whose
steps count toward the same control-clean gate); without those flags its
behavior and retained re-verdicts are identical to the reviewed v2 tool.

### 5h. Stall attribution: which code path blocks the producer, 2026-09-26

§5g left the 86–101 ms no-action stalls with a signature but no owner:
device pulls continue at the normal ~10.6 ms cadence while producer pushes
stop, and the delayed rewind-capture intervals suggest the producer is
blocked in host code, not in the emulated device. This section attributes
that block. Engine source was instrumented and both binaries rebuilt; the
change is read-only (records into the active capture only, no behavior
change) and is described in the engine's `AUDIO_BRIDGE_REVIEW.md`.

What was added (engine `src/runtime/runtime.cpp`, `host_window.cpp`):

- Phase brackets in the capture's own mutex-serialized steady-clock stream,
  emitted only while a host-audio capture is active: `fh-enter/-exit`,
  `render-enter/-exit`, `present-enter/-exit`, `audiopush-enter/-exit`,
  `pumpfn-enter/-exit`, `rewindcall-enter/-exit`, `rewind-fire`,
  `premem-save`/`postmem-save`, `rewind-store`/`rewind-fail`,
  `hostpump-enter/-exit`, `pace-enter/-exit`.
- A second, finer layer inside `HostWindow::pump` so a stall there is not a
  dead end: `pumppoll-enter/-exit` around each `SDL_PollEvent` call,
  `pumphandle-enter/-exit` around the handling of one dequeued event
  (balanced on the loop's `continue` paths by a scope guard), and
  `svcev-enter/-exit` around `SDL_PumpEvents` in the mid-frame
  `service_events` hook.
- Marker balance was verified in real captures: every label's enter/exit
  counts match, the nesting stack validates, and `pumppoll` pairs equal
  pump calls plus events dequeued (241 + 11 = 252 in one smoke capture).

New tool `tools/host_stall_attribution.py` (current rule
`2026-09-26-stall-attribution-v3`; 27 unit tests) finds consecutive-push
gaps ≥ 40 ms, classifies each by the fast/pause edges INSIDE it and by the
settle boundary (`startup` / `fast-forward` / `paused` / `no-action`),
then for `no-action` gaps ranks adjacent marker intervals by overlap and
accepts an attribution only at ≥ 20 ms and ≥ 50% of the gap. Anything else
is reported UNATTRIBUTED with the largest interval, never dropped; legacy
captures without phase markers are refused (`--gaps-only` scans and
classifies their gaps but attributes nothing). Rule v3 treats a gap that
*starts* before the 1-second startup cutoff as startup even if the next
push lands afterward; v2 incorrectly counted such a boundary gap as a
post-settle stall. The earlier v2 outputs are retained as historical files.

The stall it caught. One natural no-action stall occurred in 180
marker-bearing runs (`build/host-stall-attr/sweep-20260926-stall-v2.json`,
edges r7, capture `edges-r7-pgc7lfqy`): a 93.21 ms push gap at +1.1357 s
whose top interval is `hostpump-enter -> hostpump-exit` at 90.0505 ms
(normal ≤ ~60 µs), inside `pumpfn-enter` — 12 device pulls at a 10.64 ms
median continued throughout and the fill drained 48.85 → 0.243 ms. Every
rewind-capture marker in that run costs microseconds (`rewindcall` p99.9
0.18 ms, max 2.12 ms over 24,773 calls in 98 runs), so the stall is not in
rewind-history capture, the in-memory save, or the snapshot store: it is
inside `HostWindow::pump()`.

Which part of `pump()`. The poll/handle split was added after that stall
was recorded, so the 90 ms is not subdivided in that capture. In the 98
runs that do carry the split: `pumphandle` (all per-event work: hotkeys,
touch, runtime-UI/ImGui dispatch, input read) has a maximum of 0.0003 ms
over 1,085 events — our event handling cannot account for a 90 ms block.
The separate split captures show that the SDL/platform pump *can* be slow:
`pumppoll` p50 0.018 ms, p99 0.055 ms, 11 intervals > 1 ms, max 32.35 ms; `svcev`
(`SDL_PumpEvents` from the mid-frame service hook) reaches 231.44 ms with
54 intervals > 1 ms. Those other runs cannot localize the recorded 90 ms
`HostWindow::pump()` interval to `SDL_PollEvent` versus another subpath.
The deeper split can test that distinction if a future stall lands in a
split capture. (A later probe answers the narrower question of whether a
stalled `svcev` call also holds up *another thread's* event-push call — it did
not in the captured instances; queue admission was not recorded; §5m. The recorded
90 ms interval still has neither split, so its poll-versus-handling subpath
remains unknown.)

A later split capture did catch a natural no-action stall, but not in that
interval. Running the attribution tool (not `--gaps-only`) over the route-1
markers-ON cell (`build/host-stall-attr/r1-on-p6a.json`, control2 r0,
capture `control2-r0-wjgkzvcz`, binary `1ace74cd…8151b`) localizes a 69.12 ms
gap at +1.5052 s to `svcev-enter -> svcev-exit` at 50.191 ms (72.6% of the
gap) — `SDL_PumpEvents` in the mid-frame `service_events` hook, accepted by
the tool's threshold. Every `win.pump()` interval inside that gap is
micro-scale (`pumppoll` 0.0147 ms, no `pumphandle`), the pacer wait is its
usual 14.95 ms, 9 device pulls continued at a 10.65 ms median, and the fill
drained to 0.2437 ms in 6 steps (the comparator's match for that run). This
is a second host block in the same SDL event-pumping layer, but it is not
the recorded 90 ms `hostpump` interval, so that interval's poll-versus-
handling subpath is still unknown. Evidence: `review-r1on-gap-window.txt`
(ranked overlaps and every marker in the gap), `review-attr-r1on-v3.{json,log}`.

Stall rate, same detector both sides. Running the same ≥40 ms gap detector
over the pre-instrumentation captures (`--gaps-only`, rule v3) finds 3 no-action
gaps in 47 runs: 98.79 ms at +3.2803 s (retained `sweep-20260925-settled-b2`
edges r3 — the run the matched-window comparator also matched), 100.88 ms at
+1.2768 s (control r2), and 86.02 ms at +2.4653 s (control2 r4). The
additional 50.62 ms gap starting at +0.9625 s crosses the 1-second
boundary and is now classified as startup, not no-action; its fill drain
also stayed above the matched-window method's 2.0 ms threshold. Over the 180
marker-bearing runs the same detector finds 1 no-action gap (the r7 stall
above). Per binary: `a5f226f7` 3/47, the `hostpump`-level build `bfc6e798`
1/90, the split build `16e4ccf9` 0/90. These descriptive rates are from
different sessions and the marker stream itself costs one mutex-protected
record per marker (~13–25/frame, and more after the split), so the
instrumentation may perturb the very scheduling hiccup it is measuring
(§5k estimates *mean* route-2 wall-time overhead, not rare pause duration;
§5l observes route-1 stalls in 1/60 ON versus 2/60 OFF runs, but its
repeatedly checked, run-level interval cannot rule out a rate effect at a
guaranteed 95% confidence level). All prior §5f/§5g conclusions rest on the
pre-instrumentation binaries and are untouched by this section.

What this does and does not support:

- SUPPORTED: in a natural stalled run, the producer was blocked inside
  `HostWindow::pump()` for 90.05 ms of a 93.21 ms gap while device pulls
  continued; rewind-history capture, snapshot serialization and history
  store account for microseconds in that same run. The marker stream is
  balanced, nest-validated, and refuses malformed input.
- SUGGESTIVE, not established for the stalled instance: separate split
  captures show slow `pumppoll`/`svcev` intervals, while per-event handling
  stayed very short. The 90 ms stalled capture lacks the split markers,
  so its exact subpath remains unknown. §5m adds that the later `svcev`
  stalls, when probed directly, block nothing on the queue: SDL keeps
  accepting event pushes from other threads throughout the interval.
- NOT SUPPORTED: a root cause inside SDL/the CGEvents layer (no
  intra-SDL instrumentation exists here), or any claim that the stall is an
  engine defect. The observed rate difference across sessions/binaries is
  not a causal marker-effect estimate; §5i adds a within-binary toggle but
  is still too small to resolve that question.

Limits: single fixture/route/machine/host rate as §5f; the natural stall
sample remains small (4 post-settle no-action stalls across 227 runs and three binaries;
the one stall caught in a split-marker capture (on `1ace74cd`, §5i) localizes
to `svcev`, not to the recorded `hostpump` interval);
the attribution thresholds (40 ms gap, 20 ms / 50% acceptance) are declared
conventions; a `hostpump` interval is measured between marker records, so
it excludes only what the instrumented brackets cover, and the finer layer
is verified for balance but has not yet caught a stall of its own.

Commands / evidence / tests (`build/` ignored; source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745`
verified unchanged):

```
python3 tools/test_host_stall_attribution.py          # 27 tests
python3 tools/host_stall_attribution.py --sweep <sweep json> [...] --json <out>
python3 tools/host_stall_attribution.py --gaps-only --sweep <pre-instr json> [...]
```

Binaries (each batch JSON records its own `binary_sha256`, so the split is
auditable): pre-instrumentation
`a5f226f707ea82c7783a49ecfe57bc5098c6e993d0d1a04a5aafc26a87bb50b6` (all
§5f/§5g evidence), `hostpump`-level markers
`bfc6e7987f9daf7b769813096bc41ae5f5032cc17bbf5af14751c939090f1424`
(`sweep-20260926-stall-v1/v2`, `load-*`), poll/handle split
`16e4ccf9262ea829f5270b8f037ded7cc4e61bd4823140ab3962a19ed6ca550c`
(`smoke-v3`, `hunt-A3..A14`), plus the `GBARECOMP_PHASE_MARKERS` toggle build
`1ace74cd3f9d7867f937ff4bef3f1277d7ef32f6aa5b36b90b379a507db8151b`
(§5i/§5j cells, `r1-on-p6a`, `r2-pilot-*`). Evidence: `build/host-stall-attr/` —
`attribution-20260926-instrumented-v2.{json,log}` (historical v2 report:
188 runs including smoke captures, 1 unexplained no-action gap, attributed),
`gapscan-20260926-preinstrumented.{json,log}` (historical v2 report:
47 pre-instrumentation runs, 4 classified no-action gaps; rule v3 excludes
one crossing the startup cutoff; none attributable by design),
`gapscan-20260926-preinstrumented-v3.json` (corrected 47-run rescan:
3 no-action gaps, all unattributed by design),
`sweep-20260926-stall-v2.json` + `attribution-20260926-v1/v2.*` (the
stall detail), `hunt-A3..A14.*`, `smoke-v3.*`.

Tool changes: `host_stall_attribution.py` gains the three split/pump label
groups above in `PATH_MAP` (+ 3 tests) and `--gaps-only` (+ 2 tests); rule
label moved to `2026-09-26-stall-attribution-v2`, then to v3 for the
startup-boundary and gaps-only corrections. Earlier
`attribution-20260926-{v1,v2,all}` outputs were produced under rule v1 and
are kept as-is.

### 5i. Is the stall rate fixture-specific? second route, more pairs, 2026-09-26

§5g found that 3 of 20 no-action runs matched the action intervals in its
retained fixture. That is an observed sample fraction, not a stable
fixture-specific rate. §5h saw a lower rate after instrumentation, across
binaries and sessions. This addendum adds a second banked route measured
in one session on one binary and a within-session marker-toggle comparison;
neither comparison by itself isolates a cause.

Design and the confound it exposes:

- Two routes, back-to-back on binary
  `1ace74cd3f9d7867f937ff4bef3f1277d7ef32f6aa5b36b90b379a507db8151b`, same
  fixed scripts, batches alternating between routes. Route 1 is the
  §5f/§5g fixture (`gate2-battleI-action/rom.state1` +
  `gate2-battleJ-action2/trace.csv`); route 2 is a different banked route,
  `gate2-battleT-last/{rom.state1,trace.csv}`. Two batches per route at
  `--repeat 6` and three branches each (edges/control/control2) = 36 runs
  (24 no-action) per route, versus 15-20 runs in §5g.
- Run dirs execute with `cwd` = the run directory, so fixture paths must be
  ABSOLUTE; a relative `--trace` fails the launch ("could not open input
  trace") and the run is refused. Recorded here so it is not repeated.
- New `GBARECOMP_PHASE_MARKERS` toggle (`=0`): the capture and the
  action/save markers stay, the §5h phase records (brackets plus
  `rewind-fire`/`rewind-store`/`rewind-fail`) are dropped — the
  pre-instrumentation *marker-recording* profile in the current binary.
  The classification call/branch still executes, so this is not identical
  to the old binary. A narrow Ghidra check of the exact
  `1ace74cd…8151b` Mach-O confirmed the environment check and label
  classification remain ahead of the capture mutex when markers are off.
  Because the
  harness strips parent `GBARECOMP_*` variables on purpose, the sweep tool
  gained `--env KEY=VALUE` (recorded as `env_overrides` in the report).
- Host-work asymmetry, same guest work on both routes (240 frames; 256,764-
  257,852 source / 286,208 output frames): route 1 heals 37-98 PCs per run
  (`HEALED->native` lines, median 94) and takes 10.0-21.4 s wall (median
  17.3); route 2 heals 3-4 PCs and takes 4.5-4.9 s. The heavy fixture
  generates substantially more host-side gcc compile work. Route content
  and self-heal work are confounded here; these data do not separate them.

| cell (same session, same detectors) | runs | stall runs (no-action gap >=40 ms) | gaps | matched control runs |
|---|---:|---:|---|---:|
| route 1, markers off | 36 (24 controls) | 1 (edges r4) | 2: 128.47 ms @+1.4036 s, 112.66 ms @+3.7027 s | 0/24 |
| route 1, markers on | 18 (12 controls) | 1 (control2 r0) | 1: 69.12 ms @+1.5052 s (50.19 ms inside `svcev`, see §5h) | 1/12 |
| route 2, markers off | 36 (24 controls) | 0 | 0 | 0/24 |
| route 2 + 6 CPU burners | 36 (24 controls) | 1 borderline | 1: 41.05 ms @+1.3056 s, no counter steps | 0/24 |

The single markers-ON gap is the only natural no-action stall caught on the
split binary, and attribution mode (not `--gaps-only`) localizes 50.191 of
its 69.12 ms to the `svcev` bracket (`SDL_PumpEvents` in `service_events`),
not to `HostWindow::pump()`; see §5h.

Marker toggle (one session, one fixture): markers off = 2 gaps in 36 runs;
markers on = 1 gap in 18 runs, and the comparator matched that control run
(fill drained to 0.2437 ms with 6 steps inside a 69.12 ms producer gap).
The counts are too sparse and the cells unequal to establish either a
marker effect or its absence. The cross-session spread in §5h also cannot
be assigned to session/load variation rather than instrumentation or other
differences.

Fixture-specific? Not established:

- The new paired route-1 batches matched 1/36 controls (0/24 with markers
  off, 1/12 with markers on), versus 3/20 in the earlier retained sample.
  The samples span sessions and binaries; this does not estimate a fixed
  route-specific stall probability or explain the difference.
- The light route 2 gave no stalls in 36 unloaded runs; under 6 added CPU
  burners it produced one 41.05 ms pause with no counter steps and no
  matched window (borderline by construction, since 40 ms is the detector's
  own threshold).
- Stalled route-1 runs were at the 53rd-89th percentile in wall time and
  73rd-100th in healed-PC lines within their batches. These are not all
  wall-time tail events. More host work is a plausible contributor, but
  the route comparison is confounded by guest content, self-heal work,
  and session conditions. A light route under demonstrably effective load
  would be needed to test the workload explanation.

Manipulation check on the load cell: per-run wall time stayed at 4.5 s
median under the burners (machine load average rose from ~9.5 to ~13.4, and
the runs were not slowed), so the added load did not bite on this host and
that row is weak evidence in both directions.

Limits: one machine; the 3/20 versus 1/36 matched-control comparison crosses
binaries and sessions and is descriptive, not a causal test; route 2's audio
content was not profiled, so "light" here means self-heal/wall cost, not
music complexity; the detector threshold (40 ms) is the only reason the
41.05 ms row is marginal; §5g's matched-window method and §5h's gap scan
answer different questions and are reported separately, as above.

Commands / evidence / tests (`build/` ignored; source save unchanged at
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745`):

```
python3 tools/host_action_ring_sweep.py --repeat 6 \
    --run-root <new ignored dir> --json <out.json> \
    --env GBARECOMP_PHASE_MARKERS=0 --control-script-2 '20:save1;210:save2' \
    --state <abs>/build/winreplay/<route>/rom.state1 \
    --trace <abs>/build/winreplay/<route>/trace.csv
python3 tools/host_action_baseline.py --sweep <json> [...] --json <out>
python3 tools/host_stall_attribution.py --gaps-only --sweep <json> [...]
```

Tests: `tools/test_host_audio_queue_check.py` 30 (parent env dropped,
only the phase-marker toggle may be overridden, malformed `--env` fails
loudly); the sweep and comparator
tools are unchanged apart from `--env`/`env_overrides` and the existing
suites still pass. Evidence: `build/host-stall-attr/r1-p6{a,b}.json`,
`r1-on-p6a.json`, `r2-p6{a,b}.json`, `r2-load-p6{a,b}.json`,
`matched-20260926-routes.{json,log}`, `matched-20260926-r1on.json`,
`gapscan-20260926-routes.{json,log}`, `gapscan-20260926-r1on.json`,
`gapscan-20260926-r2load.json`, per-run captures beside each report.

### 5j. Route-2 load-manipulation pilot: markers ON, loaded vs unloaded, 2026-09-26

§5i's load cell used six CPU burners and did not slow its runs, leaving the
workload explanation untested. This pilot repeats the manipulation check on
a smaller, cleaner design with the marker stream ON so any gap could be
attributed: same binary `1ace74cd…8151b`, same route-2 fixture, three cells
of `--repeat 2` (2 edges + 2 control + 2 control2 = 6 runs each), all with
`GBARECOMP_PHASE_MARKERS=1`, run back-to-back in one session on an 18-core
host:

| cell | added load | wall delta vs unloaded (6 pairs) | pairs slowed | 1-min loadavg |
|---|---|---:|---:|---|
| unloaded | none | baseline: 4.47-4.50 s controls, 4.67-4.84 s edges | — | ~10-12 ambient |
| loaded A | 24 × `yes > /dev/null` | min -2.3%, median +0.4%, max +1.1% | 4/6 | 12.4 -> 15.6 |
| loaded B | 72 × `yes > /dev/null` (4× cores) | min -0.6%, median +2.2%, max +3.9% | 5/6 | 12.2 -> peak 43.8 |

Guest work was fingerprinted per pair and did not change: `presented` = 240
frames in all 18 runs, per-pair `saves` hashes identical, source (guest PCM)
frames identical (262,240 controls / 257,852 edges), and every run's
`HEALED->native` `xN` multipliers sum to 23 healed PCs (the printed address
set varies slightly — 2-4 lines over 2-3 addresses — but the 23 total is
constant across all 18 runs). Host-side output callback frames vary run to run in the edges branch
(275,968-286,208; identical in the controls) — a capture-window boundary
count, not guest work. The 72-burner cell's 1 Hz sample shows the emulator
process averaging 14.9% of one CPU (max 37.6%) with all 72 burners alive,
and each ~4.5 s run is mostly its ~14.9 ms per-frame pacer wait (frame
interval ~18.7 ms unloaded, ~19.2 ms loaded): the loop has little CPU work
for oversubscription to bite on at these load levels. Neither cell's slowdown is established (4/6 and 5/6 pairs;
two-sided sign tests p = 0.69 and p = 0.22, worst pair +3.9%).

Stall side: the same detectors find nothing in these cells.
`host_stall_attribution.py` (full attribution mode, markers present):
STALL-ATTR OK, 0 unexplained gaps in 18 runs — only the edges branch's
expected fast/pause gaps, whose top interval is `pace-enter -> pace-exit`
at 14.6-14.8 ms. The matched-window comparator over the same cells:
MATCHED-WINDOW OK, every control/control2 action window separated from its
controls' windows (4 runs / 4 pairs per cell).

Verdict: no admissible load was found that demonstrably slows route 2
without changing guest work — 6 burners failed in §5i, 24 and 72 fail here,
and the concurrent-instance follow-up below fails too — so this stops at the
pilot. No larger loaded batch was run, and the workload explanation for
stall-rate differences remains untested.

Follow-up, same day (ran): the proposed concurrent-instance check was run
and also fails its gate. Same binary and route-2 fixture, markers ON: a
fresh unloaded reference cell, then a loaded cell while a second instance of
the same emulator looped the same 240-frame route back-to-back in its own
directory (9 iterations; 2 emulator processes alive in 23 of 24 1 Hz
samples; summed CPU up to ~63% of one core; 1-min loadavg 9.2-10.1). Result:
0/6 pairs slowed — per-pair deltas -2.6% to -0.4%, median -1.3%, the
opposite of the gate direction — with guest fingerprints identical
(`presented` = 240, save hashes, source frames, 23 healed PCs). The fresh
reference cell itself ran 1-2% slower than the earlier unloaded cell while
the loaded cell matched it, so the small spread is session drift, not a load
effect. No loaded attribution batch was run.

Remaining escalation (not run): 3+ concurrent instances, or a second
instance plus burners. Everything tried so far — 6/24/72 CPU burners or one
concurrent same-binary instance — leaves route-2 wall time unchanged, so the
workload explanation stays untested and this line of work stops here.

Evidence (all `build/` ignored; burners killed, `pgrep -x yes` = 0 after):
`r2-pilot-unloaded.{json,log}`, `r2-pilot-loaded.{json,log}` (24 burners),
`r2-pilot-loaded72.{json,log}` (72 burners), `r2-pilot72-cpu.txt` (1 Hz
load/CPU samples), `r2-pilot-burners.pids` + `r2-pilot72-burners.pids`,
`r2-pilot-pairs.{py,txt}` (per-pair table), `r2-pilot-run.sh` (72-burner
runner), `review-r2-pilot-attr.{json,log}`, `review-r2-pilot-matched.{json,log}`;
follow-up: `r2-conc-{unloaded,loaded}.{json,log}`, `r2-conc-load/` (helper
dir with 9 iteration logs), `r2-conc-samples.txt` (1 Hz), `r2-conc-pairs.txt`,
`r2-conc-run.sh`. Exact pilot run count: 18 (3 cells × 6); concurrent check
adds 12 (2 cells × 6). Source save unchanged at
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745`.

```
python3 tools/host_action_ring_sweep.py --repeat 2 \
    --run-root build/host-stall-attr/r2-pilot-<cell> --json <out.json> \
    --env GBARECOMP_PHASE_MARKERS=1 --control-script-2 '20:save1;210:save2' \
    --state <abs>/build/winreplay/gate2-battleT-last/rom.state1 \
    --trace <abs>/build/winreplay/gate2-battleT-last/trace.csv
python3 tools/host_stall_attribution.py --sweep <cells...> --json <out>
python3 tools/host_action_baseline.py --sweep <cells...> --json <out>
# concurrent-instance follow-up (reference cell, helper loop, loaded cell):
bash build/host-stall-attr/r2-conc-run.sh
```

### 5k. Marker-record cost: paired ON/OFF on the same fixture, 2026-09-26

§5h/§5i flagged that the marker stream itself (one mutex-protected record per
marker, ~19-20 records/frame) may perturb the very scheduling hiccup it
measures, and that the earlier ON/OFF stall counts were too sparse to say.
This design estimates the mean wall-time side of that cost with paired cells on one
binary (`1ace74cd…8151b`), alternating `GBARECOMP_PHASE_MARKERS=1` and `=0`
cells on the same fixture, scripts and session so each pair is
drift-controlled. (An initial route-1 sizing pass at `--repeat 1` was refused
by the sweep's own claim gate — "need at least two paired edge/control
repeats" — and re-run at `--repeat 2`; the refused runs are kept, not used.)

| route | cells (pairs) | mean Δ (ON−OFF) | nominal 95% CI (paired t) | ON slower | fingerprints |
|---|---:|---:|---|---:|---|
| route 2 | 6 (18) | +7.8 ms (+0.17%) | [-37.3, +52.9] ms ([-0.8%, +1.2%]) | 11/18 (p=0.33) | 18/18 identical |
| route 1 | 4 (12) | -527 ms (-3.1%) | [-1251, +196] ms | 4/12 (p=0.39) | frames/saves/source 12/12; heal totals 0/12 |

Route 2 (all six cells SWEEP OK; fingerprints = 240 frames, save hashes,
source frames, 23 healed PCs, identical in all 18 pairs) gives a mean
ON-minus-OFF wall-time difference of +7.8 ms per ~4.5 s run; its nominal
paired-t 95% interval is [-37.3, +52.9] ms. This interval concerns the
*mean total run time*, not the longest pause within a run or the chance of a
rare 86–101 ms stall. A 20,000-sample bootstrap gives a similar mean interval
(mean 95% [-34.4, +46.1] ms; controls-only subset +22.5 ms [-15.8, +59.2]).
The stream adds a median 4,590 phase records per ON run (~19.1/frame; 0 when
suppressed) with identical action-marker counts, so the implied per-marker
cost is ~1.7 µs on average (roughly 12 µs at the positive interval endpoint,
assuming the whole mean difference is marker cost); this is not a per-marker
worst-case limit. Route 1 cannot isolate a cost: its
per-pair wall differences span -3.16 s to +0.58 s because self-heal/compile
timing dominates (heal totals differ in every pair, frames/saves/source do
not), and one OFF cell was refused by the control-clean gate. The route-1
interval brackets zero and gives no useful estimate of marker cost.

Stall-rate side. The same >= 40 ms detector over the 60 paired runs finds one
no-action gap, in an OFF route-1 run (`r1mark2-off-b` control r1: 93.52 ms at
+2.5472 s, 10 pulls at a 10.66 ms median) — the run whose audio counters
moved (12 stretch frames, 1 underrun), which is also that cell's
control-not-clean cause. The matched-window comparator calls its window NOT
SEPARATED (fill drained to 0.2416 ms at +2.5884 s against action windows
0.2366/0.2425 ms), i.e. the classic no-action signature. Paired rates are ON
0/30 vs OFF 1/30: far too sparse to estimate a marker effect on stall rate in
either direction. The measured mean wall-time overhead is small on route 2,
while the rare-stall rate and per-call pause cost remain open.

Commands / evidence (`build/` ignored; source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745`
unchanged; binary `1ace74cd3f9d7867f937ff4bef3f1277d7ef32f6aa5b36b90b379a507db8151b`):

```
python3 tools/host_action_ring_sweep.py --repeat 2 --run-root <cell> \
    --json <cell>.json --env GBARECOMP_PHASE_MARKERS=1|0 \
    --control-script-2 '20:save1;210:save2' --state <abs> --trace <abs>
python3 tools/host_stall_attribution.py --gaps-only --sweep <cells...> --json <out>
python3 tools/host_stall_attribution.py --sweep <ON cells...> --json <out>
python3 tools/host_action_baseline.py --sweep r1mark2-off-b.json --json <out>
```

Evidence: `r2mark-{on,off}-{a,b,c}.{json,log}` (36 runs), `r1mark2-{on,off}-{a,b}.{json,log}`
(24 runs), `r1mark-{on,off}-{a,b,c}.*` (size pass, refused), `mark-cost-run.sh`,
`mark-cost-analyze.py`, `mark-cost-report.txt`, `mark-cost-r2.txt`,
`mark-cost-r1.txt`, `review-mark-cost-{gapscan,attr}.{json,log}`,
`review-mark-cost-matched-b.{json,log}`. Pair counts: 60 runs (30 ON, 30 OFF).

### 5l. Pre-registered paired ON/OFF stall-rate batch, route 1, 2026-09-26

§5k estimated the marker stream's mean wall-time cost and left the rate side open;
§5i's counts (2 gaps/36 off, 1/18 on) could not separate a rate effect from
session noise. This batch was sized and run against a written
pre-registration (`build/host-stall-attr/rate-batch-preregistration.md`,
written before any run): route 1, same binary `1ace74cd…8151b`, cycles of one
ON cell then one OFF cell (`--repeat 2` = 6 runs each), primary endpoint =
runs with >= 1 post-settle no-action producer gap >= 40 ms (rule v3), with
the two-sided 95% Newcombe difference CI recomputed after every cycle from
cycle 4 on; stop when its half-width <= 8 pp, cap 12 cycles. It stopped at
cycle 10 (120 runs, 60 per arm; 35.7 min wall from the first to the last run,
34.6 min of run time) with a half-width of 7.9 pp:

| arm | stall runs | rate | nominal Wilson 95% (fixed sample) | total gaps | wall median |
|---|---:|---:|---|---:|---:|
| ON | 1/60 | 0.017 | [0.003, 0.089] | 2 (both in one run) | 17.2 s |
| OFF | 2/60 | 0.033 | [0.009, 0.114] | 2 | 17.3 s |

The computed Newcombe interval on (ON - OFF) is [-9.8, +5.9] percentage
points, with a point estimate of -1.7 points. Its nominal 95% coverage is a
*fixed-sample, independent-run* calculation. The pre-registered stopping
rule checked its width after every cycle from 4 onward, and runs within
cells/session can be correlated; neither feature is accounted for by that
interval. Thus +5.9 points is **not** a demonstrated 95%-confidence
exclusion threshold for a marker-induced increase. The observed counts do
not establish a beneficial or harmful rate effect. The secondary cluster
bootstrap over cycles is [-5.0, +0.0] points, but only three stall runs
across ten cycles make it unstable: every resample had ON <= OFF because the
two OFF stall runs were in cycles 2 and 8 and the one ON stall run in cycle 2.
A larger batch with inference designed for its stopping rule and clustered
runs would be needed for a reliable rate bound.

Stall detail (all three stall runs carry the classic fill-drain signature:
the matched-window comparator marks each window NOT SEPARATED against the
action windows). ON, cycle 2 control r0: two gaps, 101.26 ms at +3.4767 s and
51.80 ms at +4.0333 s, both localizing to `svcev` (84.561 ms and 35.111 ms
inside the bracket, 83.5% and 67.8% of the gap) — `SDL_PumpEvents` in the
mid-frame `service_events` hook — with every `win.pump()` interval in those
gaps <= 0.025 ms. These are the second and third natural stalls caught on the
split binary, and both localize to `svcev`, never to a `pump()` subpath. OFF,
cycle 2 control2 r0: 95.19 ms at +1.0981 s; OFF, cycle 8 control2 r1:
55.69 ms at +2.6864 s (drains to 0.2344/0.2414 ms at their window ends).

Context: 4 of the 20 cells were refused by the sweep's control-clean gate
(a no-action run moving an audio counter; that gate answers the
edge-attribution question, not the rate endpoint). The route-1 base rate
here (OFF 3.3%) is below the earlier sessions' samples, echoing §5i's
session-to-session spread. Runs within a session are correlated; the
pre-registered criterion treats runs as independent and the secondary
interval resamples cycles, as pre-registered. Neither is a calibrated
sequential interval here.

Commands / evidence (`build/` ignored; source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745`
unchanged):

```
bash build/host-stall-attr/rate-batch-run.sh <cycle 1..12>   # one ON + one OFF cell
python3 build/host-stall-attr/rate-batch-analyze.py          # STOP/CONTINUE + CI
python3 tools/host_stall_attribution.py --sweep r1rate-on-2.json
python3 tools/host_action_baseline.py --sweep r1rate-on-2.json r1rate-off-2.json r1rate-off-8.json
```

Evidence: `rate-batch-preregistration.md`, `rate-batch-run.sh`,
`rate-batch-analyze.py`, `rate-batch-analysis.txt`,
`r1rate-{on,off}-{1..10}.{json,log}` (120 runs, plus `r1rate-off-*` /
`r1rate-on-2` control-clean verdicts as noted), `review-r1rate-attr-on2.{json,log}`
(ON-stall localization), `review-r1rate-matched.{json,log}` (fill-drain check
on the three stall runs).

### 5m. Event-push progress during long SDL pump/poll calls, 2026-09-27

§5h attributes the natural no-action stalls to `HostWindow::pump()`, and
§5k/§5l localize the modern ones to `svcev-enter -> svcev-exit`
(`SDL_PumpEvents` in the mid-frame `service_events` hook). That leaves a
narrower question: can another thread complete an SDL event push while the
main thread is inside the long call? Two capture-gated engine probes test the
push path; the previous marker set only showed when the call started and ended.
The historical captures did not record the push return value, so they cannot
prove the event was actually admitted to SDL's queue.

Probes (both opt-in and default off; when enabled without a capture they
still execute but record no markers):

- `GBARECOMP_EVENT_WATCH=1` installs an SDL event watch and records one
  `evwatch:<type hex>` marker when the callback runs on the event-posting
  thread. On this Mac's SDL2-compat backend, the callback can run *before*
  final queue admission. Its timestamp shows watch activity, not proof of
  successful insertion.
- `GBARECOMP_EVENT_CANARY=<interval ms>`, honoured only together with the
  watch, starts a helper thread that pushes one private user event
  (`0x8000` in these runs) per interval and brackets the `SDL_PushEvent` call with
  `canary-push-enter`/`canary-push-exit`. That is an independent,
  scheduled push attempt every ~10 ms; its matching watch record lands
  inside its own bracket. The revised probe also records
  `canary-push-queued` or `canary-push-rejected` from `SDL_PushEvent`'s return
  value; **the historical `970c2e13…a250a5` captures lack this result**.
  The thread is joined in `HostWindow::close()`
  before any SDL teardown; the watch is removed before its `HostWindow*`
  userdata can be destroyed. The loop drains and ignores the user events.

The two probes compose with the marker toggle: a run with
`GBARECOMP_PHASE_MARKERS=0 GBARECOMP_EVENT_WATCH=1` still records watch
timestamps while emitting no phase-stream records at all, and adding the
canary in that mode pushes events without recording the canary brackets
(verified: 2,534 `evwatch` records, zero `canary-push-*` labels). So the
watch activity can be measured with the phase-marker stream off as well;
that watch-only mode cannot establish queue admission.

The probe capture reports used attribution rule v5: both label families were
excluded from the main-thread nesting check and adjacent-interval ranking,
so attribution semantics were identical with the probes on. The current v6
tool keeps that behavior for valid records, also recognizes explicit push
results, and refuses malformed labels that merely start with `evwatch:` or
`canary-push-`; the historical v5 reports are
retained. The sweep's `--env` allowlist additionally accepts
`GBARECOMP_EVENT_CANARY=0-1000`. Binary for b2-b4, the canary smoke, and
the two toggle-control cells:
`970c2e13cf41189ceeaba47787e78a95b127aaf9c70d480ed502f65fcda250a5`;
the initial watch-only `probe-smoke` cell and b1 used predecessor
`0930203524dd66ed58c2d5e9851771756a8da4887d9dc2d4c07fa68085a20ddb`.

Runs: four route-1 batches of 18 runs (b1-b4: one edges + two no-action
branches × 6 repeats), three preliminary six-run probe/toggle cells, and a
3-run route-2 canary smoke = **93 probe runs**. B2/b3/b4 plus that smoke carry
the canary (57 runs, 24,264 pushes). Detectors: `STALL-ATTR OK` on every batch and
`MATCHED-WINDOW OK` on b1-b3 and the smoke. In b4 there is exactly one
unexplained gap — the post-settle stall in the paragraph below — and it is
also what fails the sweep's control-clean gate (its no-action run drained a
bridge counter to 0.2315 ms) and makes its window `NOT SEPARATED`, the same
fill-drain signature §5k/§5l record for genuine stalls. Before b4, b1,
the canary smoke, b2 and b3 caught 17 stall-scale `svcev` calls — 4 in b1
(watch only), 1 in the smoke, 1 in b2, 11 in b3 — spanning 64.9-100.8 ms.
All 17 are in the startup
window at
t ≈ 96-172 ms (the concealment burst the sweep excludes before its 1.0 s
settle cutoff; every one is `startup=True`) inside the same reproducible
producer gap: previous push 13.3-13.8 ms before the bracket, next push
2.9-3.4 ms after it, an 81-118 ms gap.

Result — in these instrumented captures, the call returns late while
another thread's `SDL_PushEvent` calls and event-watch callbacks complete
quickly. The old captures did not record whether those pushes were accepted
into the queue, nor when the game thread consumed any event. Across 24,264 canary
pushes the duration of `SDL_PushEvent` had p50 3.5-4.1 µs, p95 <= 7.0 µs and
a worst case of 0.3083 ms (b2 0.0966 ms, smoke 0.0475 ms); no push exceeded
5 ms, ever, stalled call or not. Inside the 13 startup stall calls that carry
canary brackets, 7-11 pushes completed in 2.6-24.2 µs each, at intervals spread
across the whole block, ending from 93.3 ms to 1.3 ms *before* the bracket
closed — and each push's own `evwatch:00008000` record sits inside its own
bracket. On SDL2-compat, that callback can precede queue admission; the
historical record proves watch activity mid-block, not accepted insertion.
Raw example
(`probe-smoke-c1`, edges r0, `SDL_PumpEvents` 129.789 -> 202.459 ms =
72.67 ms): canary pushes at 132.7, 142.8, 152.8, 162.2, 172.2, 181.6, 191.0
and 199.4 ms (each 4-30 µs), plus one SDL-generated event (`0x158`) whose
watch callback ran mid-block at 137.5 ms. The longest interchange,
`probe-r1-b3` edges r0:
`svcev` 100.84 ms with 10 pushes ending 93.3 ... 4.9 ms before the return.

Supporting structure. With a genuine external push stream present, watch records
land all over the timeline rather than at pump ends: in b2, 6,103 of the
7,651 records sit inside `pace` waits, 97 in `present`, 64 in `pumppoll`,
30 in `svcev`, 6 in `hostpump`. That is a direct counterexample to a
watch-callback blackout, but not by itself to a queue-admission failure. The
opposite pattern in b1 — all 4,260 records inside a
pump call, because its only stream was SDL's own — is evidence that the
`0x659` `GAMEPAD_SENSOR_UPDATE` stream is *produced by the pump call itself*
and lands in its final microseconds (e.g. `last_from_end` 0.0045 ms in
b3 control2 r5), not that the queue was blocked. The engine opens the
attached DualSense's gyro (`controller=DualSense ... gyro=enabled` is in the
stderr of all 583 logged runs), and that event stream is intermittent —
4,210 events in b1, 0 in the smoke, 51 non-canary in b2, 9,622 in b3 — so it
tracks device-reporting windows, not a fixed rate.

The post-settle case, caught next. A pre-registered hunt (the rule, endpoint
and cap were written before the first capture:
`build/host-stall-attr/probe-prereg-postsettle.md`) sized at §5l's ~1-3 %
per-run base rate, batches of 18 runs with both probes, stop at the first
post-settle stall whose inside the probes can read. It was caught in the
first batch, b4: `control r4`, a `svcev` call of **157.955 ms at +3.864 s**
(177.8 ms producer gap; previous push 14.5 ms before the bracket, next 5.3 ms
after). The tool's top marker interval overlaps 157.9548 ms of the gap
(about 89 % of 177.7779 ms, not 100 %) and attributes it to
`svcev-enter -> svcev-exit`, context `no-action`, `post_settle`, and its
window is `NOT SEPARATED` (control drain 0.2315 ms vs action drains
0.2379/0.2357 ms: the classic signature). Inside that call, **16 canary
pushes completed in 2.2-6.7 µs each**, spaced ~10 ms apart and ending from
148.8 ms to **0.84 ms** before the call returned, with all 16 of their own
matching watch records (plus one SDL `0x158` watch record) recorded inside it.
That is the same push-path behaviour as the 13 startup instances: the helper
was not blocked for the call's duration, post-settle or not. Queue admission
was not recorded. It is longer than the earlier post-settle
instances (35.1-101.3 ms), so it widens the observed range rather than
confirming it exactly.

What this does not establish:

- The exact `svcev` subpath, beyond this instance. The caught post-settle
  call behaves like the startup ones, but n=1: §5k/§5l's four post-settle stalls (84.56, 35.11,
  50.19, 101.26 ms) still have no probe capture of their own, and none of
  them is 157.955 ms. No subpath is inferred from one phase to the other.
- What the call blocks *on*, and whether historical pushes entered the queue.
  There is still no intra-SDL instrumentation, and the old canary records
  omitted the `SDL_PushEvent` result. They show that another thread's push
  call and watch callback were not held for the full long interval, but do
  not name the cause or show when the main thread processes queued input.
  The watch and helper
  thread also change the workload, so their timings are not an uninstrumented
  stall-rate estimate. In particular this does not turn the historic 90 ms
  `hostpump` interval into a solved one.
- The two other long `hostpump` brackets in b4 are separate startup events:
  177.725 ms (edges r0) and 79.755 ms (control r3). Their finer `pumppoll`
  brackets cover 177.709 and 79.744 ms, respectively, while canary push calls
  and watch callbacks continued. They show that `SDL_PollEvent` can also
  return late in these probes, but do not retrospectively identify the historic 90 ms
  `hostpump` subpath.

Reproducibility correction: the original local `build/host-stall-attr/probe-context.py`
counted *any* watched event inside a canary bracket as a canary delivery.
On b4 that gave 7,687, three more than the 7,684 actual canary pushes.
The tracked `tools/probe_context.py` reads each run's registered event type
from its log, counts only matching watch records, rejects malformed bracket
pairs, and uses the detector's gap-start startup rule. The 24,264-push total
and the 16 matching watch records inside the post-settle call remain unchanged.
The older context text files are retained as historical output.

Post-review correction and direct queue-result evidence. The watch now
unregisters on close, before its `HostWindow*` userdata can be destroyed or a
reopen can install a duplicate. Marker calls also return before taking the
audio mutex when no capture is active, removing a default diagnostic cost
from ordinary uncaptured gameplay. The revised canary records SDL's actual
push result, not merely its watch callback:

- On intermediate binary `1bd864a2…e7bae9a7`, an 18-run route-1 batch
  (`codex-queue-outcome-r1-b5.json`) recorded **7,743 queued, 0 rejected**
  canary pushes. Two post-settle `pumppoll` intervals held the game thread:
  no-action control r2 had an 84.831 ms producer gap and 81.673 ms poll call
  at +1.160 s with **9 queued** pushes inside; edges r2 had an 83.74 ms gap
  and 80.375 ms poll call at +1.003 s with **8 queued** pushes inside. The
  control r2 gap also drained the audio ring to 0.2337 ms with 10 stretch
  steps; the matched-window comparator marks it `NOT SEPARATED` from an
  action drain. All 18 runs exited 0, presented 240 frames, and had no
  timeout or truncated capture. `STALL-ATTR OK` attributes both unexpected
  gaps; the comparison sweep's `SWEEP FAIL` is its intentional
  control-not-clean gate, not a failed game run. The edges r2 gap begins only
  3 ms past the 1 s settle cutoff; the no-action control r2 starts 160 ms
  past it.
- On final relinked binary
  `bf6280aeb869db26942285669648626d99cf56ce4c532c385b3260169dee9444`,
  a two-run smoke (`codex-final-probe-smoke.json`) recorded **884 queued, 0
  rejected** canary pushes. Its no-action control r0 caught the exact
  post-settle `svcev` subpath: a **103.859 ms producer gap at +3.3708 s**,
  with **87.1845 ms** inside `SDL_PumpEvents` and **10 successful queued
  pushes during that call**. The audio ring drained to 0.2312 ms with 10
  stretch steps, so the matched-window comparator says `NOT SEPARATED`.
  Both runs exited 0, presented 240 frames, and had no timeout or truncated
  capture; the source save was unchanged. `STALL-ATTR OK` localized the gap.
  The sweep says `SWEEP FAIL` because this one-repeat smoke is below its
  two-repeat claim gate *and* its no-action control moved an audio counter.
  Neither condition invalidates the direct queue-result timestamps, but this
  smoke is not a stall-rate or action-attribution study.

Thus, under the revised opt-in probes, another thread demonstrably enqueues
events during both post-settle `SDL_PollEvent` and `SDL_PumpEvents` stalls.
This does **not** identify why the main thread stays inside SDL, prove when
queued player input is consumed, measure the uninstrumented stall rate, or
retroactively add queue results to the historical `970c2e13…a250a5` captures.

Commands / evidence (`build/` ignored; source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745`
unchanged):

```
python3 tools/host_action_ring_sweep.py --repeat 6 --run-root build/host-stall-attr/probe-r1-bN \
    --json build/host-stall-attr/probe-r1-bN.json \
    --env GBARECOMP_PHASE_MARKERS=1 --env GBARECOMP_EVENT_WATCH=1 \
    --env GBARECOMP_EVENT_CANARY=8 --control-script-2 '20:save1;210:save2' \
    --state <ABS route-1 state> --trace <ABS route-1 trace>
python3 tools/probe_context.py probe-r1-bN.json --min-ms 30
python3 tools/host_stall_attribution.py --sweep probe-r1-bN.json
python3 tools/host_action_baseline.py --sweep probe-r1-bN.json
```

Evidence: `probe-r1-b{1,2,3,4}.{json,log}`, `probe-smoke-c1.{json,log}`,
`probe-r1-b{1,2,3,4}-context.txt` and `probe-smoke-c1-context.txt` (bracket,
delivery and canary tables; historical counts require the correction above),
`tools/probe_context.py`,
`probe-prereg-postsettle.md` (the pre-registered hunt),
`review-probe-r1-b{1,2,3,4}-attr.{json,log}` with the post-settle placement,
`review-probe-smoke-c1-attr.{json,log}`,
`review-probe-r1-b4-matched.{json,log}` (NOT SEPARATED for that stall) and
`review-probe-matched{,-b1}.{json,log}`.
Tests: the Python suite (including probe-context checks) and
34/34 engine CTest.

### 5n. The long SDL pump/poll call is a host-side AppKit/dyld park, 2026-09-27

§5m established that the long `svcev`/`pumppoll` calls are `SDL_PumpEvents` /
`SDL_PollEvent` and that other threads keep pushing events during them. It
could not say whether the calling thread was *working* or *waiting*, or which
code held it. This addendum answers both for the newly measured long SDL
calls. It does not retroactively attribute every historical `hostpump` gap;
those without an SDL-call trace remain unlocalized.

**New probe — `GBARECOMP_SDL_COST=<ms>` (engine, opt-in, default off).** It
times every `SDL_PumpEvents` / `SDL_PollEvent` call in wall time *and thread
CPU* time (`CLOCK_THREAD_CPUTIME_ID`), plus whole-process CPU
(`getrusage`), and writes `[sdl-cost]` stderr lines: one per call at or above
the threshold and one per-run summary, each carrying a run-relative `t_ms`.
Stderr only, and independent of the capture, the phase markers, the event
watch and the canary, so it can measure a genuinely uninstrumented run
(enabled: two clock reads per SDL call; disabled: one static-bool test).

**A second opt-in arm — `GBARECOMP_NO_GAMEPAD=1` (engine, default off).** It
skips the SDL game-controller/sensor subsystem for the window, to separate
SDL's HIDAPI gamepad/gyro polling from SDL's core platform pump. Scripted
runs take input from the replay trace, so only the host input layer changes
(240 presented frames and identical save hashes in every arm).

**Result 1 — a long call burns almost no CPU.** Six un-sampled long calls
(route 1 and route 2, both gamepad arms) span 66.3–113.5 ms wall with only
0.536–10.568 ms of thread CPU (roughly 0.8–10 %). The emulation thread is not
executing SDL code for the bulk of those calls; it is parked.

**Result 2 — not the gamepad/sensor polling path.** Same binary, same
route-2 fixture and scripts: gamepad+gyro enabled gave 1 stall in 10 runs;
the subsystem disabled gave 3 in 20, at the same magnitude and the same
signature (67–91 ms wall, 0.54–0.70 ms CPU). The arm is underpowered for a
rate claim, but it does not show the stall disappearing when the gamepad
disappears, so SDL's HIDAPI gamepad/gyro polling is not required for it.

**Result 3 — stack capture.** `sample` (1 ms interval, attached by PID
immediately after launch) was run alongside the probe. Every sampled run that
also contained a >= 25 ms `[sdl-cost]` call showed the same dominant parked
main-thread stack; runs without such a call showed none (7/7 route-2 stall
runs across batches, 5/5 in the first batch, 3/3 in the route-1 hunt, 0 in
otherwise-clean runs):

```
HostWindow::service_events
 -> SDL_PumpEventsInternal -> Cocoa_PumpEvents -> Cocoa_PumpEventsUntilDate
 -> -[NSApplication(NSEventRouting) nextEventMatchingMask:untilDate:inMode:dequeue:]
 -> _DPSNextEvent -> _DPSBlockUntilNextEventMatchingListInMode
 -> RunCurrentEventLoopInMode -> _CFRunLoopRunSpecificWithOptions
 -> __CFRunLoopRun -> __CFRunLoopDoTimers -> __CFRunLoopDoTimer
 -> __CFRUNLOOP_IS_CALLING_OUT_TO_A_TIMER_CALLBACK_FUNCTION__
 -> __NSFireDelayedPerform
 -> -[NSAutoFillHeuristicController _showOrHideAutoFillForCurrentTextInputContextIfAppropriate]
 -> +[NSAutoFillHeuristicController _inputContext] -> _NSGetBoolAppConfig
 -> _NSAutoFillIsChromiumBasedAppDefaultValueFunction
 -> +[NSBundle _bundleWithIdentifier:andLibraryName:]
 -> _CFBundleGetBundleWithIdentifierAndLibraryName
 -> _CFBundleEnsureBundlesUpToDateWithHint
 -> _CFBundleDYLDCopyLoadedImagePathsForHint
 -> dyld4::APIs::_dyld_get_image_name
 -> dyld4::RuntimeLocks::withLoadersReadLock -> _os_unfair_lock_lock_slow
 -> __ulock_wait2
```

129 samples sat in that subtree in one run, 127 of them parked on the dyld
loaders read lock. A second, one-time startup path appears under window/`NSApp`
setup:

```
HostWindow::open -> SDL_InitSubSystem(SDL_INIT_VIDEO) -> SDL_VideoInit
 -> Cocoa_CreateDevice -> Cocoa_RegisterApp -> -[NSApplication finishLaunching]
 -> -[NSApplication(NSMenuUpdating) _customizeMainMenu]
 -> -[NSApplication(NSMenuUpdating) _addTextInputMenuItems:]
 -> +[NSWritingToolsCoordinator isWritingToolsAvailable]
 -> WritingToolsUILibraryCore -> _sl_dlopen -> dyld4::APIs::dlopen_from
 -> ... -> dyld4::RemoteNotificationResponder::blockOnSynchronousEvent
```

with 247 samples inside the `dlopen`. A sibling instance of the same AutoFill
path soft-links `SafariPlatformSupport` (`getSPSafariPlatformSupportClass ->
_sl_dlopen`) and is the first `NSAutoFillHeuristicController` evidence found
in §5m's rebuild (`sample-s2`).

So the cost is AppKit soft-linking/querying helper frameworks and enumerating
loaded images **from inside the NSApplication run loop that SDL's Cocoa pump
drives synchronously**, while the emulation thread is blocked in
`nextEventMatchingMask:`. The observable expense is a **dyld lock /
synchronous-notification park**, not emulator code and not SDL's own input
handling.

**Result 4 — the post-settle stalls (the crackle-relevant ones) share the
signature.** Route 1 (the §5k/§5l fixture) with the probe and `sample`:
run 8 caught a post-settle call at `t_ms=2791` — 187.43 ms wall, **1.573 ms
thread CPU**, 6.66 ms process CPU — and run 11 at `t_ms=1641` — 88.23 ms /
1.588 ms / 4.998 ms. Both runs' samples contain the AutoFill/dyld subtree
above. An un-sampled route-1 batch (7 runs, probe only, no capture, markers,
watch, canary or `sample`) caught one startup stall (113.5 ms wall / 7.0 ms
CPU at `t_ms=297`) and no post-settle stall: at §5l's ~1–3 % per-run rate,
seven runs cannot be expected to.

**Confound, stated plainly — `sample` perturbs.** `sample` attaches as an
external agent and
`dyld4::RemoteNotificationResponder::blockOnSynchronousEvent` is exactly the
notification path to such an agent, so a sampled run pays a cost a normal run
may not. Sampled runs stalled far more often than un-sampled ones in the same
session (5/6 and 3/8 sampled vs 1/10 and 3/20 un-sampled). Sampled **rates**
are therefore not usable, and the specific dyld-internal frames seen only
under `sample` may be partly amplified. The un-sampled `[sdl-cost]` numbers
(thread CPU far below wall) are the load-bearing evidence that the park is
real with no sampler attached; the stacks identify which code path parks.

**Session/warming caveat.** The stall rate fell monotonically across one
session's batches (first smoke 108 ms; then 5/6 sampled; later batches 0/20)
while the same binary and fixture were unchanged. The most plausible cause is
OS-level warming of the soft-linked frameworks and their app-config answers.
Cross-session stall rates are consequently not comparable, and no
uninstrumented absolute rate is claimed here — only the mechanism.

**Candidate fix — NOT applied, and only partly tested.** Two directions:

1. *Stop AppKit from running the heuristic.* The AppKit app-config key behind
the stack is `NSEnableTextInputContextBasedAutoFill` (the literal string is in
AppKit, and the stack reaches
`_NSEnableTextInputContextBasedAutoFillDefaultValueFunction`);
`NSAutoFillHeuristicsEnabled` and `NSAutoFillPanelEnabled` are siblings. A
sandboxed test — fake `HOME` plus `__CFPREFERENCES_AVOID_DAEMON=1`, so only
`build/` was written — did **not** demonstrably disable it: that arm still
produced stalls whose samples contained the same
`NSAutoFillHeuristicController` subtree (3 of 20 runs), so neither the key nor
that injection path is verified. A real fix would set the default in-process
for the application domain before video init and must be re-measured.
2. *Remove the dyld contention.* The main thread parks on the dyld loaders
lock while enumerating images; the writer is a concurrent `dlopen`, and the
emulator's own overlay/heal loading dlopens compiled objects — route 1 heals
37–98 PCs per run against 3–4 on route 2, the same asymmetry §5i measured.
Making overlay loading not contend with the main thread during play (preload
or prelink, one load instead of many, or a preallocated executable arena
instead of per-heal `dlopen`) is the fix direction that does not depend on a
private AppKit default. It is a design change, not attempted here.

**What this does and does not establish.**

- ESTABLISHED: long `SDL_PumpEvents`/`SDL_PollEvent` calls are a main-thread
  *park* (thread CPU at 0.8–10 % of wall, 6 un-sampled instances), inside
  AppKit code that SDL's Cocoa pump invokes synchronously, with the expense
  showing as dyld loaders-lock and synchronous-notification waits; the SDL
  gamepad/sensor subsystem is not required for it; AppKit's
  `NSAutoFillHeuristicController` and the one-time WritingTools soft-link are
  the two concretely named callers.
- NOT ESTABLISHED: which single `dlopen` holds the dyld write lock in a given
  stall (the engine's overlay loads, AppKit's own soft-links and the OS page
  cache all move together) — **answered by §5o: the engine's own heal `dlopen`,
  measured by window intersection, and a cold heal cache is what makes those
  loads long and frequent**; whether disabling the AppKit default removes the
  stall (the one injected-default arm did not); and the uninstrumented
  post-settle stall rate. §5m's other open items stand unchanged.
- Still not explained: the perceptual crackle. This addendum explains *why* a
  producer stall can be 35–190 ms while the thread appears idle, and it makes
  the stall a host-process/host-OS event rather than an emulator or audio
  bug — it does not measure what a listener hears, and no mitigation is
  applied, so the default behaviour is unchanged.

Limits: one machine (macOS 27.0, 18 cores, Apple silicon) and the same
SDL2-compat-over-SDL3 build chain; `sample` is a macOS tool, so the stack
evidence is single-platform; one session per batch, and the warming caveat
above; `GBARECOMP_SDL_COST` and `GBARECOMP_NO_GAMEPAD` are diagnostic toggles
with no default behaviour change.

Commands / evidence (`build/` ignored; source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745` unchanged;
measured binary sha256 `e7dc0d267f74771f2a9ab76510fef0ce0d8445976bfeb3bb50720dbeff343297`):

```
# un-sampled probe batches (route 2: gate2-battleT-last; route 1: gate2-battleI-action)
GBARECOMP_SDL_COST=25 <exe> game.toml --window --frames 240 ... --load-state <abs state>
# arm B adds GBARECOMP_NO_GAMEPAD=1
# stack capture (macOS): attach by pid immediately after launch
sample <pid> 4 -mayDie -file sample.txt
# fix hypothesis, sandboxed (no write outside build/)
HOME=<ignored>/home __CFPREFERENCES_AVOID_DAEMON=1 \
  <exe> ...   # with NSEnableTextInputContextBasedAutoFill=false in that HOME
```

Evidence: `build/host-stall-attr/sdlcost-gp-{on,off}-a/` (30 un-sampled runs),
`.../sdlcost-probe-a/`, `.../r1cost/` (7 un-sampled route-1 runs),
`.../sample-s1/`, `.../sample-s2/` (sampled route-2), `.../r1hunt/` (12
sampled route-1 runs, the post-settle stacks), `.../autofill-{on,off}/`
(the sandboxed default test), plus the runner scripts `sdlcost-smoke.sh`,
`sdlcost-batch.sh`, `sample-smoke.sh`, `sample-route1.sh`,
`autofill-off-smoke.sh`, `r1cost-batch.sh`.
Tests: 199/199 Python and 34/34 engine CTest on this binary; both builds;
`git diff --check` clean in both repos.

### 5o. The write-lock holder is the engine's own heal `dlopen`, and a cold
### heal cache manufactures the stall, 2026-09-27

§5n left one question open: *which* `dlopen` holds dyld's loaders write lock
while the main thread's event pump waits. It is the engine's own overlay/heal
loader, and the reason the earlier batch could not see it is that a cold heal
cache makes each freshly compiled shard cost far more than anyone assumed.

**New probe — `GBARECOMP_LOAD_TRACE=1` (engine, header-only, opt-in, default
off).** `load_trace.h` wraps the engine's only runtime `dlopen`
(`overlay_compile.cpp::load_and_resolve`, POSIX path) and prints one stderr
line per load: `[load-trace] begin_us= end_us= wall_us= tid= path=`. The
`[sdl-cost]` lines gained a `mono_us=` stamp taken after the call, so an SDL
call occupies `[mono_us - wall_us, mono_us]` on the same monotonic clock as
the load windows. Authorship is then an **arithmetic window intersection**, not
an inference from log order. With the toggle off the probe is one cached bool
test; the clean 120-frame boot above prints no `[load-trace]`/`[sdl-cost]` line.

**1. A newly compiled shard is expensive to load, and the cost is per *path*.**
Standalone probe (`build/host-stall-attr/dyldprobe/shardprobe.c`, a 17 KB shard
from the route-1 heal cache): first `dlopen` of a path never loaded in this
process **142.6 ms**; the same path again in the same process 0.12–0.17 ms;
that path again in three *fresh* processes 0.18–0.28 ms; the same bytes copied
to a new path in a fresh process **151.0 ms** and **44.3 ms**. The shards are
ad-hoc linker-signed (`codesign -dvvv`: `flags=0x20002(adhoc,linker-signed)`),
so the first load of a new file pays dyld's signature validation and mapping
inside the loaders write lock, and pays ~0.2 ms afterwards. **Every
self-heal run with a fresh cache pays it ~90 times.**

**2. In a cold-cache run the engine holds that lock for most of the run.**
Route-1 fixture, capture on, 18 un-sampled runs, 240 frames each
(`loadwin-route1.sh loadwin-a 18 GBARECOMP_SDL_COST=3`): 1,666 loads totalling
**215.7 s of 325.6 s = 66.2 % duty**, mean ~130 ms per load, worst 766 ms.

**3. Every clean long pump call lands inside one of those load windows.**
Park = wall − thread CPU ≥ 3 ms. Across 25 runs (cold 18 + warm 6 + 1 smoke)
there are 6 parks ≥ 20 ms whose CPU is ≤ 3 ms, and **all 6 intersect an engine
load window that covers ~99 % of the park** — the pump woke when that `dlopen`
released the lock:

| run | wall | CPU | t_ms | overlap | blocking load |
|---|---|---|---|---|---|
| loadwin-a/r3 | 117.20 ms | 2.04 ms | 483 | 116.31 ms | `080B0ABE_61B5EE7B_t.dll` |
| loadwin-a/r10 | 84.87 ms | 0.71 ms | 201 | 84.12 ms | `0800B560_F409DDCE_t.dll` |
| loadwin-a/r10 | 40.75 ms | 1.46 ms | 1102 | 39.53 ms | `08003BB2_C662DC9A_t.dll` |
| loadwin-a/r15 | 95.90 ms | 0.67 ms | 212 | 95.17 ms | `0800B560_F409DDCE_t.dll` |
| loadwin-a/r16 | 93.92 ms | 0.80 ms | 210 | 93.04 ms | `0800B560_F409DDCE_t.dll` |
| loadwin-warm/r3 | 121.89 ms | 2.90 ms | 238 | 118.88 ms | `080B0B36_F409DDCE_t.dll` |

Four further ≥ 20 ms calls had no engine load in their window. All are early
(`t_ms` 24–233) and burn 10–14 ms of CPU — roughly half work, so they are not
clean parks and are **not** attributed; their mechanism is still unknown.

**4. Sampled cross-check (reader and writer side of the same lock).** In the
§5n `sample` reports the dominant loaders-write-lock holder is
`worker_main -> overlay_compile_one -> load_and_resolve -> dlopen ->
dyld4::RuntimeLocks::withLoadersWriteLockAndProtectedStack` at **237–356 of
~2 500 samples** in every run. AppKit's own soft-links do take the same write
lock, but an order of magnitude less: `-[NSApplication
_registerApplicationWithUIIntelligence] -> LNProcessInstanceRegistryClient ->
_sl_dlopen` on a `com.apple.root.utility-qos` queue (25–37 samples) and
`+[LNAppConnectionListener sharedListener]` (AppIntents bridge), plus the
main thread's one-time startup `WritingToolsUILibraryCore` load (~256 samples,
which parks the main thread itself, not the pump). **No observed park is
explained by an AppKit soft-link.**

**5. Why the dyld-tracing arm could not see it.** In the 12 traced runs
(`dyldop-a/`) a `dlopen` was in flight for only **18–19 % of the log stream**
(164–172 entries with matching exits), so 0/3 misses at that arm's three parks
was expected (p ≈ 0.53). The trace arm was underpowered, not contradictory;
the load-window arm above is the powered version.

**6. The loaders lock is one unfair mutex, and reads hold it too.**
`build/host-stall-attr/dyldprobe/lockprobe.c` measures per-call latency on N
threads: `_dyld_get_image_name` **serializes** (4 threads 8.4 M ops/s total vs
15.4 M/s single-threaded, per-thread 2.1 M/s), while `_dyld_objc_class_count`
and `dyld_get_active_platform` scale linearly (16.4 M -> 61.4 M ops/s). So the
AppKit reader (`_CFBundleDYLDCopyLoadedImagePathsForHint` ->
`_dyld_get_image_name` -> `withLoadersReadLock` -> `_os_unfair_lock_lock_slow`)
waits for *any* holder, and the enormous `_dyld_objc_class_count` torrents
visible in the dyld trace hold nothing. This retires the earlier suspicion
that read-side API traffic was the blocker.

**7. The measurement protocol, not just the engine, produces these stalls.**
The sweep tool's `branch_env()` points `GBARECOMP_HEAL_CACHE` at a fresh
`<out>/cache`, and every evidence script in this document copied that
(`rm -rf $d/cache` after each run), so **every audit run recompiled ~90
shards and paid a first-path validation for each one**: ~90 write-lock holds
of ~130 ms, 66 % of the run. Re-running the same fixture against one
persistent cache (`loadwin-cacheab.sh loadwin-warm 6 warm`) drops the run from
~19.6 s to 4.7–5.7 s (no gcc) and the duty cycle from 58.9 % to 9.7 % as the
miss set settles — and the single remaining clean park (121.89 ms) still maps
to a *newly compiled* shard's first load. A player who heals once and replays
pays this on new shards only; the audit's cold-cache protocol pays it on ~90
shards every single run.

**Fix direction (not implemented here, no default changed).** Two levels:
(a) *protocol* — never run the audio-critical A/B or a crackle replay against a
cold heal cache; reuse one warm cache so the measurement stops manufacturing
the defect it measures; (b) *design* — cut the number of first-path loads
(batch many PCs per shard instead of one shard per function) or stop
`dlopen`ing healed code at all by shipping the static corpus, which is what
`GBARECOMP_STRICT_STATIC` acceptance mode already does. Both are changes to
heal policy, and neither is attempted in this pass.

**What this does and does not establish.**

- ESTABLISHED: on this host, the dyld loaders write lock that parks the
  emulation thread's event pump is held by the **engine's own heal/overlay
  `dlopen`**; the park ends when that load releases the lock; a single load
  window is 44–766 ms when the shard's path has never been loaded (ad-hoc
  signature validation), ~0.2 ms afterwards; the sampled writer stack and the
  un-sampled window intersection agree; AppKit's soft-links take the same lock
  but do not account for any observed park.
- ESTABLISHED (protocol): the cold-cache-per-run protocol used by §5h–§5n and
  the sweep tool produces ~90 fresh-path loads and ~66 % lock duty per run, so
  the *rate* of 88–187 ms post-settle parks in those sections is at least
  partly an artifact of the measurement harness. Rates measured that way are
  not player-representative.
- NOT ESTABLISHED: what a listener hears (no audio capture was evaluated by
  ear here); the mechanism of the four early, half-CPU long calls; the
  post-settle park rate under a warm cache with a settled miss set (the warm
  arm is 6 runs, and its runs are 3–4× shorter because the compiles are gone);
  whether any of this is the perceptual crackle.
- Limits: one machine (macOS 27.0, 18 cores, Apple silicon); the traced window
  is the `dlopen` call, and dyld releases the lock before `dlopen` returns, so
  a *non*-overlap does exonerate the engine for that event while an overlap
  only bounds the overlap; the trace is stderr I/O on the loading thread when
  enabled; cold and warm arms are different protocols, so their park counts
  are not a paired comparison; attribution is by window intersection, never by
  observing the lock itself; `GBARECOMP_LOAD_TRACE` is a diagnostic toggle with
  no default behaviour change (clean 120-frame boot: rc 0, 120 frames
  presented, 0 dispatch misses, 0 unmapped, no probe lines).

Commands / evidence (source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745` unchanged;
measured binary sha256
`396a348f268fd9ac93f24586fcb64e6263958ed7dca395b4cd629dba55475f2b`):

```
# window intersection: engine load windows vs SDL-call windows
./loadwin-route1.sh loadwin-a 18 GBARECOMP_SDL_COST=3      # cold cache, fresh dir per run
./loadwin-cacheab.sh loadwin-warm 6 warm                  # one persistent cache
python3 loadwin-analyze.py loadwin-a --min-ms 3 --park-us 2000
# dyld-named authorship (perturbs; stream order brackets each dlopen)
./dyldop-route1.sh dyldop-a 12 GBARECOMP_SDL_COST=5       # DYLD_PRINT_APIS=1 inside
python3 dyldop-analyze.py dyldop-a --park-us 3000 --hard-ms 5
# standalone: per-path cost and lock semantics
./dyldprobe/shardprobe <shard.dll> 5      # 142 ms first path load, then ~0.15 ms
./dyldprobe/lockprobe name 4 2            # vs: lockprobe api 4 2
```

Evidence: `build/host-stall-attr/loadwin-a/` (18 cold-cache runs, 1 666 load
windows), `.../loadwin-warm/` (6 warm-cache runs), `.../loadwin-smoke/`,
`.../loadwin-scope-check/`,
`.../dyldop-a/` (12 traced runs), `.../dyldprobe/{shardprobe,lockprobe}`,
`.../normal-boot-dlopen/` (inert-probe boot), with the scripts
`loadwin-route1.sh`, `loadwin-cacheab.sh`, `loadwin-analyze.py`,
`dyldop-route1.sh`, `dyldop-analyze.py`.
Tests: 199/199 Python and 34/34 engine CTest on this binary; both builds;
`git diff --check` clean in both repos.

### 5p. Prototype fix: pay the shard's first-map cost off the loaders lock,
### 2026-09-27

§5o identified the mechanism but not the exact step inside `dlopen`. Three
micro-experiments pinned it (`build/host-stall-attr/dyldprobe/fixprobe.c`, a
never-loaded 17 KB shard, fresh path each time):

| preparation before `dlopen` | preparation cost | then `dlopen` |
|---|---|---|
| none | — | 41.4 / 114.6 ms |
| 2 s settle (page cache, write-back done) | — | 45.0 / 58.5 ms |
| `read()` the whole file first | 33–243 us | 36.6 / 37.6 / 59.0 ms |
| `mmap(PROT_READ)` + touch + `munmap` | 32 / 57 / 226 us | 36.4 / 38.8 / 41.4 ms |
| **`mmap(PROT_READ|PROT_EXEC)`** + touch + `munmap` | **36.9 / 51.3 / 71.1 ms** | **283 / 292 / 298 us** |
| overwrite an already-loaded path with different bytes | 490–558 us | 0.31 / 1.94 ms |

The cost is charged at the **first executable mapping of a path**: it is not
page-in, not write-back, and not the shard's contents (a validated path loads
cheaply even when its bytes are replaced by a different shard). So the engine
can pay it through a mapping it owns, on its own worker thread, with **no dyld
lock held**, and then `dlopen` the cheap case.

**Fix — `GBARECOMP_HEAL_PREWARM_MAP=1` (engine, opt-in, default off).** Before
the in-process `dlopen`, `overlay_compile.cpp` opens the shard, `mmap`s it
`PROT_READ|PROT_EXEC`, touches one byte per page, `munmap`s, and only then
loads it. A mapping failure falls through to the ordinary load; the shard is
about to be executed anyway, so nothing new is executed that was not executed
before — only a different thread pays the kernel's first-map cost.

**A/B on route 1** (identical fixture and protocol to §5o:
`gate2-battleI-action` state + `gate2-battleJ-action2` trace, fresh heal cache
per run, capture on, `GBARECOMP_SDL_COST=3`, `GBARECOMP_LOAD_TRACE=1`, 12 runs
per arm, **same binary**, only the toggle differs):

| metric (12 runs/arm) | OFF (control) | ON (fix) |
|---|---|---|
| engine `dlopen` windows | 1 100 | 1 129 |
| dyld loaders lock held in `dlopen` | 136.6 s = **64.12 % duty** | 1.34 s = **0.64 % duty** |
| worst single lock window | **265.5 ms** | **1.99 ms** |
| pump calls >= 20 ms | **12** (one per run, 78.3–108.1 ms) | **0** (worst 14.86 ms) |
| those overlapping an engine load | 12 / 12 | 0 |
| off-lock pre-warm work | 0 | 1 129, mean 127 ms, max 638 ms |
| run wall time | 14.8–18.7 s | 16.7–19.2 s |

Every control run stalls exactly once at `t_ms` 195–224 — the moment the first
heal shard is compiled — and 78.5–107.3 ms of each 78.3–108.1 ms park sits
inside that load's window. With the fix no pump call reaches 20 ms and no park
intersects an engine load at all.

**Behaviour is unchanged**: both arms heal 94 shards,
`dispatch_misses=94 failed=0 unmapped=0`, `frames_presented=240`, cycle count
62 191 750, capture artifacts present; a clean 120-frame boot with the toggle
on is rc 0 / 120 frames / 0 dispatch misses.

**Second arm, implemented and rejected — `GBARECOMP_HEAL_OUT_OF_PROCESS_LOAD=1`.**
It pays the cost in a helper process (the same executable in a one-shot
`GBARECOMP_LOAD_VALIDATE` mode that loads paths and leaves before `main`).
Unit-level it works exactly as predicted (fresh path: 114.6 ms in-process load
before a helper run, 0.198 ms after), but the helper costs ~0.24 s of process
startup per shard (0.36 s wall for a bare helper run), i.e. a process spawn per
shard where the mapping pre-warm needs none. It is kept as an opt-in arm and as
the fallback if executable mapping is ever restricted; it was **not** A/B'd on
route 1.

**What this does and does not establish.**

- ESTABLISHED: the shard first-load cost is per path and is charged at the
  first executable mapping; moving it off dyld's loaders lock removes the
  engine's contribution to the pump park, which was a reproducible 78–108 ms
  stall in **12/12** control runs and **0/12** fixed runs.
- NOT ESTABLISHED: what a listener hears (no ear-level audio evidence, and the
  residual host parking in the fixed arm is untouched); whether the residual
  short parks (fixed arm worst 14.86 ms, 13 park-like calls) are AppKit's own
  brief soft-link writers; whether the pre-warm survives a future macOS that
  restricts executable mapping of ad-hoc signed files (the out-of-process arm
  is the fallback there).
- The fix **moves** cost, it does not delete it: the 143 s of pre-warm in the ON
  arm is the same kernel work the OFF arm did while holding the lock, now
  idle-waiting on the heal worker. It stays opt-in, default off, POSIX-only
  (the `dlopen` branch), and is not adopted as the shipping default here.
- Limits: one machine (macOS 27.0, 18 cores, Apple silicon); cold-cache protocol
  in both arms; no paired measurement of audio quality; `[load-trace]` is
  stderr I/O when enabled.

Commands / evidence (source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745` unchanged;
measured binary sha256
`0708878497592d3c8b844d90da8249ec61611bcc1ff3b741968a63d3b8d551e8`):

```
./loadwin-route1.sh loadwin-fix-off 12 GBARECOMP_SDL_COST=3 GBARECOMP_HEAL_PREWARM_MAP=0
./loadwin-route1.sh loadwin-fix-on  12 GBARECOMP_SDL_COST=3 GBARECOMP_HEAL_PREWARM_MAP=1
python3 loadwin-analyze.py loadwin-fix-on --min-ms 3 --park-us 2000
./dyldprobe/fixprobe mmapx|mmapread|preread|rewrite|plain <dst> <src>
```

Evidence: `build/host-stall-attr/loadwin-fix-{off,on}/` (12 runs each),
`.../loadwin-fix-smoke/`, `.../dyldprobe/{fixprobe,shardprobe,lockprobe}`.
Changed for this prototype: engine `src/runtime/overlay_compile.cpp` (pre-warm
and helper arms), `src/runtime/load_trace.h` (`prewarm_wall_us` line), game
`tools/host_audio_queue_check.py` + `tools/test_host_audio_queue_check.py`
(allowlist for the two new toggles).
Tests: 199/199 Python and 34/34 engine CTest; both builds; `git diff --check`
clean in both repos.

### 5q. How much of a measured producer park the ring absorbs, and how long live
### audio is lost, 2026-09-27

Question: the park is 20–130 ms measured (35–190 ms across the audit). If a
cushion (ring fill target) change is going to be made, it needs numbers, not
feel: how much of a park does the ring pay for, and for how long is the output
not live audio?

**Method.** Parks come from the `[sdl-cost]` lines: `mono_us` (steady_clock) and
`wall_us` bracket the blocked SDL call on the emulation thread. `cap-events.csv`
records every bridge record on that same clock, serialized by the bridge mutex:
`P` (producer push) and `C` (device callback, 512 frames = 7.8 ms at 65536 Hz)
carry ring `fill_ms` plus the cumulative `stretch_frames` / `underrun_frames`
counters. Per park, therefore:

  absorbed = park start -> first concealed frame (the ring paid this)
  conceal  = frames synthesized by looping recent audio (<= 80 ms, audible)
  silence  = unfilled output frames past that cap (a dropout)
  nonlive  = conceal + silence = live audio lost
  lost_for = park start -> last non-live frame (loss outliving the park)

Analyzer: `build/host-stall-attr/audiopark-analyze.py` (walks every run dir with
both files; 836 run dirs scanned). Ring facts used: `taps=32` so `half=16`
source frames must be ahead of the read cursor, i.e. live play survives until
fill ~= 0 — the ring absorbs its whole fill and no more; `stretch_limit_ms=80`;
live play resumes only when `fill >= target_ms`.

**A. Cushion 40 ms (default), 21 runs / 20 primed parks** (the §5o cold-cache
control arm, one heal-load park per run):

| metric (per park) | value |
|---|---|
| park wall | p50 93.9 ms, max 130.0 ms |
| fill at park start | mean 36.1 ms (24–62) |
| absorbed | mean 34.4 ms = **36.4 % of the park** (median 27.3) |
| conceal | mean 76.1 ms; 11/20 parks **at the 80 ms cap** |
| silence | mean 8.7 ms; 10/20 parks (max 38.0) |
| NONLIVE | mean **84.8 ms** live audio lost per park |
| tail / lost_for | p50 21.5 ms / p50 118.1 ms |
| nonlive share of play | 1.97 % of 86.1 s measured play |

**B. Fresh arms at three cushions** (`audiopark-c60|c80|c100`, 6 runs each, same
cold-cache protocol so the park is the same *kind*, not the same size):

| cushion | parks | park wall | fill@start | absorbed | conceal | silence | nonlive | tail |
|---|---|---|---|---|---|---|---|---|
| 60 ms | **0/6 runs** (1 run had a 17.9 ms call, under 20 ms) | — | — | — | — | — | — | — |
| 80 ms | 4 | 70.1–95.1 ms | 61.9 ms | 59.8 ms (70.7 %) | 73.6 ms | 3.4 ms (2/4) | 77.0 ms | 53.2 ms |
| 100 ms | 6 | 70.1–74.2 ms | 85.3 ms | **= park wall (100 %)** | **0** | **0** | **0** | **0** |

**What the numbers say.**

1. `absorbed = min(park, fill at the instant the producer stops)`. Measured at
   three fills: 36.1 -> 34.4 ms at cushion 40, 61.9 -> 59.8 ms at cushion 80,
   85.3 -> park fully absorbed at cushion 100. Nothing else in the ring pays for
   a park: it drains to ~0 before it conceals.
2. Beyond the fill the loss is dominated by **looped audio, not silence**: the
   80 ms concealment cap binds in 11/20 parks at cushion 40. Silence only
   appears when the loss exceeds the cap, and it is small (mean 8.7 ms, max
   38 ms) next to the concealment.
3. The loss **outlives the park** by p50 21 ms (cushion 40) to 53 ms
   (cushion 80): live play only resumes once the ring refills to the target.
4. Between 40 and 80 ms the cushion buys almost nothing for the parks that
   actually hurt: for near-identical 94.8–95.9 ms parks the loss is 86.7 ms in
   **both** arms (26.6 absorbed + 80 conceal + 6.8 silence at 40; 59.8 absorbed
   + 80 conceal + 6.7 silence at 80). The cap, not the cushion, sets the loss.
   The cushion only pays when it exceeds the park (full absorption), which is
   what the cushion-100 arm shows.
5. Steady-state fill tracks the cushion (run-mean 39.9 ms at 40, 75.4 ms at 80),
   and the cushion *is* added end-to-end audio latency.

**C. Projection to other cushions** (model: absorbed=min(park,C),
conceal=clamp(park-C,0,80), silence=max(0,park-C-80), steady fill = C; applied to
the 22 measured parks):

| cushion | fully absorbed | conceal total | silence total | parks with silence | latency vs 40 ms |
|---|---|---|---|---|---|
| 40 ms | 1/22 | 1150.7 ms | 11.9 ms | 2 | +0 |
| 60 ms | 2/22 | 761.9 ms | 0 | 0 | +20 ms |
| 80 ms | 5/22 | 364.8 ms | 0 | 0 | +40 ms |
| 100 ms | 14/22 | 108.8 ms | 0 | 0 | +60 ms |
| 140 ms | 22/22 | 0 | 0 | 0 | +100 ms (needs ring_ms >= 180) |

Measured caveat on that model: at cushion 80 the observed fill at the park start
was 61.9 ms (0.77 × cushion), not 80 — so the model overstates the cushion by
roughly the fill/cushion ratio that the DRC's oscillation phase happens to give.
The measured arm, not the model, is the authority.

**Justification arithmetic for a cushion change.**

- Cost is latency, 1:1 with the cushion: 40 -> 80 costs +40 ms of audio delay;
  40 -> 100 costs +60 ms. It does not change video or input timing.
- +40 ms (80) buys: zero silence only for parks <= ~62 ms and no improvement at
  all for the 95–130 ms parks (measured, point 4 above).
- +60 ms (100) buys: every measured park (70–74 ms in that arm, and per the
  measured fill law any park <= ~85 ms) absorbed with **zero** concealment,
  zero silence, zero tail — versus 84.8 ms mean live-audio loss at cushion 40.
  Ring headroom is preserved: cap stays 150 ms (100+40 < 150) and
  `overflow_drops` was 0 in every captured run.
- It does **not** cover the 130 ms worst park (needs fill >= 130). Covering that
  means cushion ~= 150 ms, i.e. `ring_ms >= 190`, +110 ms latency — not
  recommended on this evidence, and beyond the current
  `GBARECOMP_AUDIO_TARGET_MS` clamp of 25–100 ms.
- The cause-side fix already exists: §5p's pre-warm removes the park entirely
  (0/12 runs stalled, worst SDL call 14.9 ms). The cushion is a second line of
  defence for stalls from other sources, not a substitute.

**ESTABLISHED** (measured, 836 run dirs scanned): the ring absorbs exactly the
fill present when the producer stops (3 arms); the residual loss is concealment
first and silence second, capped at 80 ms of concealment; loss outlives the park
by 21–53 ms; cushion 100 absorbs every park measured in that arm; a cushion of
40 -> 80 ms does not change the loss for parks larger than the fill.

**NOT ESTABLISHED**: anything about how it *sounds* (looped audio is an audible
artifact even when no silence occurs; there is no ear-level evidence here);
parks from sources other than the cold-cache heal load (the post-settle route-1
88/187 ms stalls of §5n/§5o were only measured under `sample` perturbation); a
paired within-run cushion comparison (each arm has its own park sizes: 95 ms at
cushion 80 vs 71 ms at cushion 100), and the pump/load collision is stochastic —
the cushion-60 arm produced **no** park at all, while the same protocol gave
12/12 in §5o.

Limits: one machine (macOS 27.0, 18 cores, Apple silicon); capture granularity
is one device callback (7.8 ms), so onset/offset carry ±8 ms; `[sdl-cost]`
threshold 3 ms; `[load-trace]` is stderr I/O when enabled; the cushion-40 cell is
the §5o control arm (cold cache, `GBARECOMP_HEAL_PREWARM_MAP=0`), not a fresh
batch.

Commands / evidence (no engine or game source changed for this section — analysis
scripts only, all under ignored `build/host-stall-attr/`; measured binary sha256
`0708878497592d3c8b844d90da8249ec61611bcc1ff3b741968a63d3b8d551e8`):

```
# cushion-40 population: parks + ring behaviour over the cold-cache control runs
# (51 loadwin-* run dirs: 21 runs with a park >= 20 ms, 20 of them primed)
python3 audiopark-analyze.py . --only loadwin --min-ms 20 --top 10 \
    --project 40,60,80,100,140
# fresh cushion arms (cold cache kept, audio capture on, SDL_COST=3, LOAD_TRACE=1)
bash audiopark-run.sh audiopark-c100 6 100
python3 audiopark-analyze.py audiopark-c60 audiopark-c80 audiopark-c100 --min-ms 20
# did the heal-load windows overlap the SDL calls in each arm?
python3 loadoverlap.py audiopark-c60 audiopark-c80 audiopark-c100 loadwin-fix-off
# whole-tree park population (all 836 captured/uncaptured logs, capture not needed)
python3 audiopark-analyze.py . --min-ms 20 --scan-parks
```

Evidence: `build/host-stall-attr/audiopark-analyze.py`, `.../audiopark-run.sh`,
`.../loadoverlap.py`, `.../audiopark-c{60,80,100}/` (6 runs each),
`.../loadwin-fix-off/` (cushion-40 cell).

### 5r. Route-1 stall-rate batch re-run with a warm persistent heal cache,
### 2026-09-27

§5l's paired ON/OFF batch gave every run a *fresh* heal cache (the sweep's
harness sets `GBARECOMP_HEAL_CACHE=<per-run temp dir>`). §5o-§5q then showed
that a fresh cache makes each run compile and first-load ~90 new shard paths,
each holding dyld's loaders write lock for ~120 ms, and that this lock hold is
what parks the emulation thread's event pump. So §5l's primary endpoint was
measured under a protocol that manufactures the very stall it counts. This
section re-runs the same batch with **one** protocol delta: a new
`--heal-cache DIR` option points every run at one persistent, pre-warmed
directory (recorded as `heal_cache` in the sweep JSON; `env_overrides` is
otherwise identical).

**Mechanism check first** (`warmcheck.sh`, current binary, `[load-trace]` +
`GBARECOMP_SDL_COST=3`, route-1 fixture, one run cold then two warm):

| run | cache | dlopens | load p50 | load total | SDL calls > 3 ms | worst SDL call |
|---|---|---:|---:|---:|---:|---|
| r1 | cold (fresh dir) | 91 | 119.6 ms | 11.50 s | 1 | **123.65 ms** (`site=pump`, t_ms 306) |
| r2 | warm, first use (populates) | 96 | 119.0 ms | 11.80 s | **0** | 0.64 ms |
| r3 | warm, repeat | 131 | **0.88 ms** | 4.54 s | **0** | 1.28 ms |

The persistent cache turns the ~120 ms per-shard lock hold into ~0.9 ms and the
park disappears. r2 cuts the other way and matters for interpretation: 11.8 s of
cold-style loads (~68 % duty) and still *zero* parks, i.e. the AppKit reader side
(`_CFBundleDYLDCopyLoadedImagePathsForHint` -> `_dyld_get_image_name` -> loaders
read lock) is intermittent too, so a park needs both a writer holding the lock
and that reader firing. Rate differences across arms are therefore noisy even
without the markers.

**Batch.** 10 cycles = 20 cells = 60 runs/arm, fixture and branch scripts
identical to §5l (`edges` fast/pause, `control` 20:save1, `control2`
20:save1;210:save2, `--repeat 2`, 240 frames), endpoint identical
(post-settle no-action producer gap >= 40 ms, rule v3, `--gaps-only`). All 20
cells `SWEEP OK`, source save unchanged
(`8340b0db…ab745`), control counter steps 0 in the warm cells. Run wall time
17.2 s -> 4.6 s (no recompiles), which is itself the protocol change.

| metric | §5l cold, fresh cache/run (exe `1ace74cd`) | §5r warm, one persistent cache (exe `07088784`) |
|---|---|---|
| **endpoint** runs with a post-settle gap >= 40 ms | ON 1/60, OFF 2/60 (3 gaps, worst 101.3 ms) | **ON 0/60, OFF 0/60** (0 gaps) |
| gaps >= 40 ms incl. startup (settle 0 s) | ON 9/60 (12 gaps, worst 182.9 ms), OFF 6/60 (9, 95.2 ms) | ON 1/60 (1 gap, 50.1 ms at +0.083 s), OFF 0/60 |
| Newcombe nominal 95% interval on (ON - OFF) | [-9.8, +5.9] pp | [-6.0, +6.0] pp, **degenerate** (no events) |
| Wilson 95% per arm | ON [0.3, 8.9] %, OFF [0.9, 11.4] % | ON/OFF [0.0, 6.0] % (rule of three ~5 %) |
| no-action runs with ANY post-settle ring step | ON 1/40 (100.9 ms conceal), OFF 3/40 (155.3 ms) | **ON 0/40, OFF 0/40 (0.0 ms)** |

**Does the earlier ON/OFF claim survive?**

- The earlier *difference* does not. §5l's three stall runs are not
  reproducible under the warm protocol: 0/120 runs post-settle, and the
  matched no-action contrast goes 4/80 cold -> 0/80 warm with zero ring loss.
  The event source was the cold-cache lock hold, which both arms shared
  equally; the 1-run ON/OFF gap in §5l was the right scale for it.
- The earlier *conclusion* — no demonstrated beneficial or harmful marker rate
  effect — is unchanged, but it is not strengthened: with 0 events in both arms
  the interval is degenerate. A single-look Wilson calculation gives a
  nominal 6.0 pp upper bound for the ON arm, but repeated looks and serially
  related runs mean that is not a confirmatory 95% bound on a marker-induced
  increase. Any rate effect this endpoint could see would have to be large.
- **The base rate in §5l (OFF 3.3 % on route 1) was a property of the
  cold-cache protocol, not of route 1.** With a warm cache the same fixture,
  scripts and endpoint gave 0/60, and 1/120 runs even when startup gaps are
  included (one 50.1 ms blip at +0.083 s, far from the 95-183 ms cold ones).
  The settle window also hides most of the cold signal in 240-frame runs:
  15/120 cold runs had a gap >= 40 ms including startup, only 3/120 after it.

**NOT ESTABLISHED / limits.** The cold column is a different binary
(`1ace74cd` vs `07088784`) and session, and the two protocols have very
different run durations (17.2 s vs 4.6 s), so how much of the heal queue lands
inside the 240-frame window differs; the warm batch alone is therefore not a
clean causal isolate — the same-session mechanism check above (same binary,
cold 123.65 ms park vs warm 0 calls > 3 ms) carries that part. Zero endpoint
events means no interval coverage claim beyond the per-arm bounds. Nothing here
re-measures §5k's per-callback marker cost, and only one machine, one fixture
and 240-frame runs were used.

Commands / evidence (source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745` unchanged):

```
# mechanism: cold vs warm cache for the same fixture/binary
bash warmcheck.sh warmcheck 3 40
# one ON cell + one OFF cell per cycle, warm persistent cache
bash rate-batch-warm-run.sh <cycle 1..12> [prefix r1warm]
python3 rate-batch-warm-analyze.py r1warm 12      # pre-registered endpoint + CI
python3 warm-report.py r1warm 12                  # + startup-inclusive gaps, ring
python3 warm-report.py r1rate 10                  # same census on the §5l batch
```

Evidence: `build/host-stall-attr/r1warm-{on,off}-{1..10}.{json,log}` (120 runs),
`.../r1warm-{on,off}-1/…` per-run captures, `.../r1warmpre-*/` (pre-warm cell),
`.../warm-heal-cache/` (the persistent cache, 131 dlls),
`.../warmcheck/r{1,2,3}/`, `.../rate-batch-warm-run.sh`,
`.../rate-batch-warm-analyze.py`, `.../warm-report.py`. Changed for this
protocol: game `tools/host_audio_queue_check.py` (`--heal-cache`, `branch_env`),
`tools/host_action_ring_sweep.py` (`--heal-cache`, `heal_cache` in the report),
`tools/test_host_audio_queue_check.py` (new test). Tests: 200/200 Python
(`python3 -m unittest discover -s tools`).

### 5s. Instrumenting the AppKit AutoFill reader path, and whether switching it
### off removes the parks, 2026-09-27

§5n identified the waiter from `sample` stacks: AppKit's AutoFill heuristic
walks the loaded-image list
(`_CFBundleDYLDCopyLoadedImagePathsForHint -> _dyld_get_image_name ->
dyld4::RuntimeLocks::withLoadersReadLock`), and §5o/§5r showed the writer is the
engine's heal `dlopen` holding the loaders write lock. This section measures the
reader in-process, with no `sample` and no staging, and tests two ways of
turning it off.

**Instrument — `readwatch.dylib`** (`build/host-stall-attr/readwatch/`, injected
with `DYLD_INSERT_LIBRARIES`): interposes `_dyld_image_count`,
`_dyld_get_image_name`, `_dyld_get_image_header`,
`_dyld_get_image_vmaddr_slide` process-wide, so every call from every image
(including system frameworks) is counted and timed, with the *calling image*
resolved by `dladdr`, optional raw backtraces, and per-API first/last-call
stamps plus a summary at exit. It can also (a) call
`+[NSAutoFillHeuristicController _inputContext]` on the main queue at chosen
offsets (`GBARECOMP_READWATCH_FIRE=ms,...`), which fires the reader on demand
instead of waiting for the OS to schedule the heuristic, and (b) register the
candidate AppKit AutoFill keys as NO in the NSRegistrationDomain
(`GBARECOMP_READWATCH_AUTOFILL=1`) for a disable arm — same dylib in both arms,
one environment variable differs, nothing written to disk.

**Positive control** (`readwatch/lockp` = a `_dyld_get_image_name` loop on the
main thread plus a thread `dlopen`ing 10 *fresh* signed dylib paths): the holder
spent 1193.0 ms in `dlopen`; the reader made 47.8 M calls and the instrument
counted **296 calls >= 3 ms, 2.174 s of wait, max 140.4 ms**. It measures the
wait; it does not miss it.

**Clock note.** readerwatch stamps `CLOCK_MONOTONIC`; the engine stamps
`std::chrono::steady_clock`. On this boot the measured offset is
**steady - mono = 4 399 626 us** (`readwatch/clkdiff`), constant; every
correlation below applies it. (Without it the two streams look 4.4 s apart.)

**Measured: the park is the reader waiting on the writer.** Cold-cache protocol
(heal worker compiling and first-loading ~90 new shard paths, i.e. holding the
loaders write lock ~73 % of the run as measured in the same runs) with the
reader fired on the main thread:

| run | fire at | wait inside `_dyld_get_image_name` | SDL call containing it | engine `dlopen` in flight |
|---|---|---:|---|---|
| rwf-ctl1 | 1.0 s | **67.678 ms** | `site=poll` **72.039 ms**, cpu 1.723 ms | yes |
| rwf-ctl4 | 0.7 s | **112.403 ms** | `site=poll` **114.068 ms**, cpu 1.588 ms | yes |
| rwf-afoff3 | 0.7 s | **94.202 ms** | `site=pump` **95.931 ms**, cpu 1.652 ms | yes |
| rwf-afoff4 | 0.7 s | **108.830 ms** | `site=poll` **110.536 ms**, cpu 1.615 ms | yes |
| rwevery | 0.7 s | 16.2 / 125.8 / 126.1 / 115.8 / 113.9 ms | `site=poll` **721.55 ms**, cpu 222.95 ms | yes |

- The waiting frame is named by the instrument itself:
  `caller=/System/Library/Frameworks/CoreFoundation.framework/…+0x885ec`
  (`_dyld_get_image_name`) and `+0x88614` (`_dyld_get_image_header`) — the same
  CoreFoundation function as in §5n's `sample` stacks, now seen without
  `sample`.
- Park wall = reader wait + 1.7–9 ms, thread CPU <= 1.7 ms: the park is the
  reader's wait, not work. Fired calls that do **not** overlap a writer hold
  wait <= 1 us and produce no park (rwf-ctl2, rwf-ctl3, rwf-afoff1/2).
- One fire can walk the list many times (4650 reader calls in the rwevery fire),
  and repeated contention inside one walk is what produced the 721.55 ms park
  there. The cold-cache single-park signature of §5o/§5q (one 60–115 ms park at
  t_ms 175–306) is one walk hitting one lock hold.

**Disable test: no.** Registering
`NSEnableTextInputContextBasedAutoFill`, `NSAutoFillHeuristicsEnabled`,
`NSAutoFillPanelEnabled` and `NSAutoFillHeuristicsAutoEnable` as NO in
NSRegistrationDomain did **not** stop the reader: the fired `_inputContext`
still walked the loaded-image list (identical image-call counts, 2796 vs 2796)
and the colliding fires still waited 94.2 / 108.8 ms and still parked the pump
95.9 / 110.5 ms. These few triggered runs show the tested registration did
not gate this reader path; they do not estimate equal stall rates. Combined
with §5n's failed fake-`HOME` attempt, there is currently **no demonstrated
way to switch the heuristic off** from the app/registration defaults reachable here; the
cause-side fix (§5p pre-warm, §5r warm cache) and the ring cushion (§5q) remain
the levers.

**Session state, and why an untriggered A/B was empty.** In this session the OS
stopped scheduling the heuristic on its own: untriggered cold-cache runs show
all image-list traffic in a ~34 us burst at startup (468–473 calls, max wait
1 us) while the writer still holds the lock ~73 % of the run — and **zero
parks**, where the same protocol parked 3/4 runs (59–75 ms, at t_ms 175–191)
40 minutes earlier. A fresh copy of the binary as a new app identity did not
restore it (0/4). Park and reader-fire disappeared together, which is itself
evidence the reader is necessary: the writer alone, at 73 % duty, is not
sufficient.

**NOT ESTABLISHED / limits.** The fire trigger calls private AppKit API
(`+_inputContext`; it returns nil and no crash was seen in 14 fires over 6
runs) — diagnostic only, not a shipping path. The injected dylib's presence may
perturb timing: cold-cache arms with injection parked 0/8 vs 4/8 without, but
the untriggered rate had already fallen to ~0 in that window, so the two are not
separable; the trigger exists precisely because the untriggered phenomenon is
not currently controllable. Fires dispatched to the main queue can coalesce
behind a long park (seen in rwevery). One machine, one fixture, cold-cache
protocol, 240-frame runs. Reader stamps need the clock offset above before they
can be intersected with engine stamps.

Commands / evidence (source save
`8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745` unchanged;
no engine or game source changed for this section — the instrument lives in
ignored `build/host-stall-attr/readwatch/`):

```
# build + validate the instrument
cc -O2 -fobjc-arc -dynamiclib -o readerwatch.dylib readerwatch.m \
   -framework Foundation -Wl,-undefined,dynamic_lookup && codesign -s - -f readerwatch.dylib
DYLD_INSERT_LIBRARIES=$PWD/readerwatch.dylib GBARECOMP_READWATCH_MS=3 ./lockp pc2/*.dylib   # positive control
./clkdiff                                                                                   # steady - mono offset
# reader arms: cold cache so the heal worker holds the loaders write lock
RW_FIRE=1000,1400,1800 bash readerwatch-run.sh rwf-ctl1 1 cold 0
RW_FIRE=1000,1400,1800 bash readerwatch-run.sh rwf-afoff1 1 cold 1     # autofill-defaults arm
python3 readerwatch-analyze.py rwf-ctl1 rwf-ctl4 rwf-afoff3 rwf-afoff4 rwevery
```

Evidence: `build/host-stall-attr/readwatch/{readerwatch.m,readerwatch.c,readerwatch.dylib,probe.c,lockp.c,clkdiff.cpp,blank.dylib,pc2/}`,
`.../readerwatch-run.sh`, `.../readerwatch-analyze.py`, arms
`.../rw-cold-{nowatch,inj}` (untriggered cold controls),
`.../rwf-{ctl,afoff}{1..4}`, `.../rwevery`, `.../rw-i*-{ctl,inj,afoff}`
(interleaved untriggered arms), `.../rw-freshapp/` (fresh app identity),
`.../rw-apnow` (audiopark protocol re-check).

### 5t. Post-halt review of the unfinished config-reader probe, 2026-09-27

Freebuff's later, interrupted extension of `readwatch.m` added four
`CFPreferences` interposes and `NSUserDefaults`/`NSBundle` swizzles to discover
which keys AppKit reads. That version was **not usable as-is**: both a default
injection and a config-enabled injection exited 139 in a tiny program that
only read one preference. The interposes called pointers obtained through
`dlsym(RTLD_NEXT)`; with config off (or the intended `NO_CF` arm) those
pointers were unset, and with config on they did not provide a safe original
call. The Objective-C value formatting also happened outside its documented
re-entry guard.

The local diagnostic now calls each original `CFPreferences` replacee directly
from its interposer (the same pattern used by the dyld-reader hooks), guards
Objective-C key/value conversion, and treats `NO_CF=1` as pass-through with
CF logging off. Malformed arbitrary `SET` values are rejected. The accompanying
`readerwatch-run.sh` now rejects invalid labels and existing output directories,
so it cannot erase prior arm evidence, and propagates a crashed game's exit
code instead of reporting success after cleanup. These diagnostic files remain
under ignored `build/host-stall-attr/`; they are not shipping engine code.

Verification: the CoreFoundation preference probe now exits 0 with the
injection off, with config logging on, and with `NO_CF=1`; the Objective-C
defaults/bundle probe also exits 0 and logs its expected test key. Two
120-frame game boots with the repaired injected library and config logging
exited 0, presented 120 frames each, had `failed=0` and `unmapped=0`, and
logged 19 filtered config reads among 760 and 759 observed reads respectively.
The trace includes AppKit reads of
`_NSEnableTextInputContextBasedAutoFill`, `NSAutoFillHeuristicsEnabled`, and
`NSEnableTextInputContextBasedAutoFill`; observing reads is **not** a gate
test, and no working off-switch is established. The original SRAM source hash
remains `8340b0db2ac49f5a323d961042ade0088a7b8413dcc98590d446364a259ab745`.

One separate opt-in prototype from §5p, the out-of-process preload helper,
had placed cache and executable paths into a shell command using double
quotes. Those paths are now single-quote escaped before the helper command
and log redirection. A rebuilt 120-frame game boot with an apostrophe in its
temporary heal-cache path exited 0, healed 50 PCs with `failed=0`, and wrote
50 helper validation logs. The final rebuilt game binary SHA-256 is
`1bec058d58011387e664eed201335c9698c12f51df59b969a56c946ba79388de`;
the repaired diagnostic dylib SHA-256 is
`f59832dd546f9f4cd64631fbbf860125ba9b66e70f2070d137b9bf29f756b33d`.
This does not promote that slower helper arm over
the measured in-process pre-warm option. The rebuilt engine passed 34/34
CTests; the game Python suite passed 200/200 tests. No audio-quality claim is
made by this repair, and the perceptual crackle remains unverified.
