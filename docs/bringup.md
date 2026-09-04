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

## Next
1. First-divergence native-vs-interp on the entry/crt0 path (`GBA_COSIM`
   state-hash gates, or `runtime_trace`/`registers` at `0x08000000` in both
   modes). Suspects: crt0 interworking/stack lowering, native IRQ re-entry
   (cf. R0 depth-105), present-in-place frame resume.
2. Upstream issue to `mstan/gbarecomp` with this repro (engine @ fork point,
   game SHA1 `ff45038a…`, `skip_intro=false`, interp-renders/native-parks).
3. Then: IWRAM `[[code_copy]]` capture + `0x08001CBA` jump-table sizing.
