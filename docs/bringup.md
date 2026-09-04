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

## R10+ — demo-campaign sweeps, fully static boot (2026-09-04)
`GBARECOMP_DEMO_INPUT=campaign` headless `--frames 1200` sweeps:
R6 merged 271 new (34 jt regions admitted as roots; 602 entries, 3019 funcs);
R7 merged 75 more. 300-frame verify: **0 misses, 0 interpreted, failed=0**,
healed cache reused — boot path fully static. Only the 2 held IWRAM probes
recur. Misses now come only from deeper gameplay past title (needs longer
campaign runs + real input exploration).

## Next (no upstream contact without express permission)
1. Keep rolling the frontier (regen → build → 60s verify → merge).
2. Size the 13+ jump-table regions into `[[jump_table]]` (table-base hunt).
3. Resolve `failed=2..6` heal failures per round (likely jump interiors).
4. Admit `0x03007AF0/0x03007B44` only with sourced `[[code_copy]]`.
