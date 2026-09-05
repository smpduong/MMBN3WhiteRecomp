# MMBN3 White bring-up log

## R0 — first boot (skip_intro=true, Debug, 473 funcs)
Window opened (Metal). Self-heal recorded 10 cart misses + 2 BIOS (`0x18` x669,
`0x128` x370). Run abandoned: `runtime_irq: handler at depth 105 did not iret
after 4000000 dispatches (R15=0x08000000)` — IRQ machinery nesting into the
cart entry with zeroed IWRAM (no handler installed yet).

## R1 — merged 10 cart roots → 494 funcs
`0x08001CBA/C2/CA/D2` admitted as individual roots (jump-table candidate —
size the dispatcher into one `[[jump_table]]` later). `0x08132CE0` hottest
(x29). BIOS `0x18/0x128` are engine-owned, not duplicated here.

## R2 — verbose 30s → 496 funcs
R1 roots covered (no repeats). New: Thumb interwork entry `0x0802B354` into
ARM function + interior resume `0x08001E8E` (resume=true). IWRAM misses
`0x03005E00 arm / 0x03005FD0 / 0x030068BA thumb` need `[[code_copy]]` with
ROM-source backing — NOT admittable as bare extra_func. Capture deferred
copies weren't live yet (see R3).

## R3 — skip_intro=false (full BIOS boot)
Hypothesis: skip path synthesizes zeroed IRQ state; real BIOS installs its
handler first. Result: no abandon, BIOS heals itself (770 BIOS funcs +2 more
`0x68 / 0x210C x263`, engine-owned).Deterministic park: `final_pc=0x08000000`,
`vcount=126`, PAL/VRAM/OAM all zero after 8560 frames (Debug) and 5249 frames
(Release) — same endpoint, `unmapped=0 io_unhandled=0`, cart corpus covers
everything reached. IWRAM all zeros at 25s: native parks before any copy.

## R4 — oracle verdict: GBARECOMP_FORCE_INTERP=1, 90s
Interpreter backend: `final_pc=0x03006842` (inside the R2 IWRAM region),
`pal=674/1024 vram=26580/98304 oam=178/1024`, 5358 frames presented,
`dispatch_misses=0`. **Video alive under interp; blank under native.**
Bug isolated to the generated native path (codegen/dispatch/IRQ-resume),
not the hardware model. Copies ARE live once boot progresses (IWRAM
`0x030068xx` executes) — recapture `[[code_copy]]` sources after the
native entry bug is fixed.

## R5 — ROOT CAUSE: stray BIOS output dir, game linked empty stub (2026-09-04)
The `gba_recompile --bios` run used a CWD-relative default `--out`, writing
`bios_recompiled.cpp` to a stray `<selection>/src/` tree while the engine
kept linking `bios_dispatch_stub.cpp` (empty table). All native BIOS
execution was interpreter bridging; a bridge on the 0x138 exception path
flipped R2 with no insn between fp records, latching the 0x344 poll (695002
identical iterations at 2s and 60s) with blank PPU. Interp was unaffected.
Fix (local only, no upstream): moved the 4 files into
`gbarecomp/src/runtime/generated_bios/` (gitignored, BIOS-derived), deleted
the stray tree, reconfigured (`-- BIOS recompiled output present — linking`,
1.1→1.8MB). First boot after: video state identical to the interp oracle
(`pal 674/1024 vram 26580/98304`, same cycles `62191750`), execution deep in
cart `0x8133xxx`, idle elision active. Env-bisects (PRESENT_IN_PLACE=0,
BIOS_HLE=1) had exonerated those paths beforehand.

## R6/R7 — corpus rounds (496 → 1484 → 1572 funcs)
- R6 (230-entry frag, 13 unsized jump regions admitted as roots):
  `iwram_sound_mixer` code_copy (`0x03005E00 ← 0x08235F00`, 0x17AC; maximal
  exact run) covering the x685089 IWRAM miss. misses 215→29, interp
  106M→42M, native_calls 218K→329K.
- R7 (+31, 2nd code_copy `0x0300416E ← 0x0813293A` 0x403; held sourceless
  `0x03007AF0/0x03007B44` per policy): misses→4, interp→0.94M.
- R8 frontier: 2 cart roots (`0x08134A18/24`); the 2 held recur (x6, still
  sourceless — likely speculative dispatches, still unadmitted).

## R9+ — frame-bounded runs are the source of truth (2026-09-04)
`--frames N` (headless AND windowed) boots bit-identically native==interp
(6.8M fp records, 0 div through 62M cycles; windowed+frames matches
headless exactly incl. cart entry @76M cycles). Open-ended windowed runs
stop advancing guest cycles at exactly 62191750 with host presents
continuing — a runner/yield-path accounting stall, still open; NOT guest
divergence. `YIELD_ON_VBLANK=0` open-ended passes the 0x366 wait (reaches
0x0B76). Screenshots verified black pre-logo in both modes (display off,
DISPCNT=0 — consistent, not divergence). Steady state: 2 held IWRAM probes
(x6, sourceless) + `failed=2` heal counter with no log detail (same pair).
Input driver (`tools/play_inputs.py`) + screenshots (`tools/shot.py`) ready;
title input still to be driven once boot reaches it in bounded sessions.

## R10+ — demo-campaign sweeps; boot path static only with cache (2026-09-04)
`GBARECOMP_DEMO_INPUT=campaign` headless `--frames 1200` sweeps:
R6 merged 271 new (34 jt regions admitted as roots; 602 entries, 3019 funcs);
R7 merged 75 more. 300-frame verify: **0 misses, 0 interpreted, failed=0** —
but with 670+ routines loaded from the self-heal cache (`coverage=NOT_STATIC`,
warm_loaded>0). Corrected by R14 strict runs: boot (30f) and title (300f)
pass with cache disabled and fallback set to abort. Boot-path static is now
a measured strict result, not a cache-assisted inference. Only the 2 held
IWRAM probes recur. Misses now come only from deeper gameplay past title
(needs longer campaign runs + real input exploration).

## R11 — jump-table sizing attempt: cluster is callbacks, not a switch
The R3 "30 consecutive" cluster (0x21F9C–0x221B4) is NOT one switch: no
absolute refs; odd (Thumb-bit) refs come from the init table (0x32C) and
small local runs. Two verified runs (T1 @0x21FB8×3, T2 @0x22050×4, all
PUSH-prologue targets) looked sizeable, but T2's bytes overlap a live
function's literal pool (0x2204A flows into 0x22050) — recompiler correctly
rejects (`control-flow entries into data_range`). Reverted; per-case roots
stand (boot fully static without tables). Lesson: frag "consecutive"
grouping over-groups event-callback tables; verify target prologues AND
check for enclosing-function overlap before sizing.

## R12 — title reached, input verified with screenshots (2026-09-04)
Two operator traps found, both documented here so nobody re-pays them:
1. `--tcp` starts the guest PAUSED (`RS_PAUSED`); all queries work but the
   core never steps until `{"cmd":"continue"}`. Every pre-R12 TCP probe
   (black screenshots, frozen VCOUNT, zero IWRAM) was a paused core, not a
   freeze. Always `continue` first.
2. `gba_recompile --bios` defaults `--out` to CWD-relative
   `src/runtime/generated_bios` (see R5) — run it from the engine dir.
With `continue`: DISPCNT shows all layers on, title renders
(MEGAMAN BATTLE NETWORK 3 WHITE / PRESS START), Start tap advances to the
NEW GAME menu (cursor visible). Screenshots: `tools/shot.py`; attended
driver: `tools/boot_to_title.py`; scripted probe: `tools/playtest_probe.py`.
Open-ended "stall at 62M cycles" was heal-bound slowness + paused-mode
confusion compounded; `--frames` and continued sessions progress normally.

## R13 — backend items for playtesting (2026-09-04)
1. Saves: machinery verified by inspection (dirty-tracked, atomic tmp+rename,
   periodic+exit flush, config-relative paths). No .sav yet — game has not
   reached a save point; live write-through still to be watched on real play.
2. Keyboard: defaults complete (A=X B=Z Start=Return Select=RShift,
   D-pad=arrows, R=V L=C; kDefaultBinds, rebindable via keybinds.ini). Same
   KEYINPUT sink as the proven TCP path; physical presses untestable headless.
3. Audio: pipeline proven live via TCP (34.9M samples, FIFO active, 4096-sample
   window max=24864 rms=9093 — real non-silent title audio). Speaker audibility
   unverifiable in this session.
4. R9 demo-2000: 30 merged (3315 funcs); 300-frame verify 0/0/0. failed=2
   persists opaquely (no log lines; same count as the held pair — likely them).

## R14 — strict baseline: boot+title pass; opening stops at stack stub (2026-09-05)
STRICT_STATIC=1 boot (30f) and title route (300f): FULLY_STATIC, 0 misses,
0 interp, cache bypassed (not deleted). Two genuine gaps found by aborts and
admitted after review: 0x08001E9A (POP{PC} stub twin of 0x08001E8E) and
0x0802B370 (PUSH-prologue function after literal pool). Corpus 3318 funcs.
Strict opening route (demo 1200f) aborts deterministically at 0x03007B44:
trace proves the game WRITES a Thumb stub onto its own stack (sp-relative
stores B510/1C04/... immediately precede dispatch) — runtime-synthesized
code with no ROM source, so no [[code_copy]] exists to admit; leaving it
unadmitted per policy (interpreter bridge is the correct fallback). 0x03007AF0
likely same class (nearby stack, x6 bridges, no source found) — pending capture.
Strict scope statement: boot and title routes pass from fresh processes;
full-game strict is not claimed (synthesized-code abort is by design).

## R15 — fresh-checkout acceptance (2026-09-05)
Separate clean checkout (/tmp-equivalent temp ws) via tools/setup.sh,
private ROM/BIOS supplied by env: engine pinned 425d941 (+arm-recomp-core
14be3cf) verified, SDL2/prereq checks pass, ROM+BIOS SHA-1 verified, BIOS
codegen via absolute --out (770 funcs), game regen 3318 funcs, configure
asserts real BIOS linkage, build ok, setup exit 0. Fresh binary strict 30f:
FULLY_STATIC 0/0/0. One setup.sh fix found by the run itself (missing roms/
saves/generated dirs on fresh clones). Stub .o still compiles in-tree but
the game links the real table (proven by strict BIOS baking).

## R16 — reproducible generation (2026-09-05)
Same tool binary + ROM + game.toml + engine rev, two temp dirs: 19 files,
diff -rq clean; dispatch_table sha256 bf7feb2f... identical in genA, genB,
AND the working-tree generated/. No embedded paths/timestamps (only guest
addresses in comments). No hand-edits to generated C++.

## R17 — save size resolved, import paths tested (2026-09-05)
Reference 64KB .sav = 32KB real SRAM data (10587 nonzero bytes) + 32KB
zero padding (emulator container artifact). game.toml 32768 is CORRECT:
real 256Kbit SRAM size, matches engine kDefaultSramSize; addressing mirrors
(off % size); oversize loads are rejected, not truncated. Tests (disposable
copies, build/save-test/, --save override): 32KB first-half imports cleanly
(save_loaded 32768/32768), boots identically, untouched when game does not
write (dirty tracking ok); 64KB direct import rejected gracefully
("save file too large ...") with file untouched and no crash. Config
UNCHANGED by design. Full save→restart→load proof still needs a real
in-game save point (past current gameplay reach).

## Backend status before playtesting (no upstream contact without permission)
VERIFIED: native boot to title with graphics; TCP input advances title to menu;
demo-campaign headless sweeps (3019 funcs, 0-miss boot); SRAM config detected;
IWRAM mixer + copy-2 static via code_copy.
REMAINING:
1. Host keyboard mapping in open-ended windowed play (only TCP input tested).
2. Audible audio on a real device (unverifiable headless; APU modeled).
3. SRAM save write-through on a real playthrough (path configured, unwatched).
4. g_runtime_cycles sticks at 62191750 in open-ended exit reports (cosmetic).
5. Coverage tail: failed=2 heals, 2 held IWRAM probes, unsized jump regions.

1. Keep rolling the frontier (regen → build → 60s verify → merge).
2. Size the 13+ jump-table regions into `[[jump_table]]` (table-base hunt).
3. Resolve `failed=2..6` heal failures per round (likely jump interiors).
4. Admit `0x03007AF0/0x03007B44` only with sourced `[[code_copy]]`.
