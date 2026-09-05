# SUPERSEDED — do not file as written (2026-09-05)

The diagnosis below is obsolete. The "native park with blank PPU" was our
own build error (BIOS recompiler output landed in a stray CWD-relative
directory; the game linked the empty dispatch stub), and the "interp renders
vs native black" split was compounded by TCP probes that never sent
`continue` (paused core). After fixing both locally, native boots to the
title with graphics. No upstream defect is established; nothing here has
been reported anywhere. Retained as investigation history only.

---

# Upstream issue draft — native park vs interp render (MMBN3 White)

Target: `mstan/gbarecomp` @ `425d941` (+ `arm-recomp-core` @ `14be3cf`),
macOS arm64, AppleClang 21, SDL2, Release.

## Game
Mega Man Battle Network 3 White (USA), A6BE, 8388608 bytes,
SHA1 `ff45038ae6d01cde4eae25a02dcb8bed29e07a6f`,
save `SRAM_V @ 0x231C30`. No existing game repo; bring-up config at
`smpduong/MMBN3WhiteRecomp` (ROM-free, PolyForm-NC): 496-function corpus,
`skip_intro=false`, real BIOS SHA1 `300c20df…3492` (LLE, 770 BIOS funcs).

## Symptom
Native backend parks deterministically at cart entry with blank PPU:
`final_pc=0x08000000 unmapped=0 io_unhandled=0`, `vcount=126`,
`pal 0/1024 vram 0/98304 oam 0/1024` after 5249–8560 presented frames
(Debug and Release identical endpoint). Cart corpus covers everything
reached (`dispatch_misses` = BIOS PCs only, all `HEALED->native`).
With `skip_intro=true`, instead: `runtime_irq: handler at depth 105 did
not iret after 4000000 dispatches (R15=0x08000000)` into zeroed IWRAM.

## Oracle
Same binary with `GBARECOMP_FORCE_INTERP=1` (90s): `final_pc=0x03006842`
(IWRAM copy region), `pal 674/1024 vram 26580/98304 oam 178/1024`, 5358
frames presented, `dispatch_misses=0`. Video alive under interp.

## Isolation
Recomp-vs-interp, same binary/cart/BIOS/config: renders vs parks.
Hardware model exonerated; bug is in the generated native path —
suspects: crt0 interworking/stack lowering (`gf_start_vector` →
`gf_afunc_080000C0` reads correct in isolation), native IRQ
dispatch/return (cf. depth-105 nesting), present-in-place frame resume.
`registers`/`runtime_trace` TCP queries return defaults at this commit,
so no live ring data; verdict rests on exit reports + `misses` query.

## Repro
1. `gba_recompile --bios gba_bios.bin --config bios/gba_bios.toml`
2. `gba_recompile --config game.toml --rom <mmbn3-white.gba> --out generated/`
3. Build game, run `--rom … --bios …`, compare vs
   `GBARECOMP_FORCE_INTERP=1` run. Exit report lines quoted above.
