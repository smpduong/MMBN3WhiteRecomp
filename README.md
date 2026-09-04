# MMBN3WhiteRecomp — Mega Man Battle Network 3 White, recompiled

Static recompilation of **Mega Man Battle Network 3 White** (GBA) to native PC,
built on [`gbarecomp`](https://github.com/smpduong/gbarecomp) (fork of
`mstan/gbarecomp`, PolyForm Noncommercial 1.0.0).

> Status — initial bring-up scaffold (v0.0.0). Boots via real GBA BIOS (LLE);
> coverage, audio, and save validation in progress. Uncovered targets fall back
> to the interpreter, are reported, and get folded into `game.toml`.

This is not a decompilation or source port. No ROM, BIOS, save, or generated
ROM-derived source is included.

## ROM identity

| Target | Game | Code | SHA-1 | Debug port |
|---|---|---|---|---|
| `MMBN3WhiteRecomp` | MMBN3 White USA | A6BE | `ff45038ae6d01cde4eae25a02dcb8bed29e07a6f` | 19863 |

See `baserom.md`. Runtime hash-gates the ROM.

## Quick start

1. `git clone --recurse-submodules <this repo> && cd MMBN3WhiteRecomp`
   (or point `-DGBARECOMP_ROOT=../gbarecomp` at the sibling fork checkout).
2. Copy your legally obtained dump to `roms/mmbn3_white_usa.gba` + BIOS to
   `../gbarecomp/bios/gba_bios.bin`.
3. `cmake -S . -B build && cmake --build build`
4. `./build/MMBN3WhiteRecomp --rom roms/mmbn3_white_usa.gba`
5. `tools/regen.sh` regenerates `generated/`; never hand-edit it.

Reference: `../ROMS/Mega-Man Battle Network 3 - White # GBA.GBA`,
`reference-video/Megaman_Battle_Network_3_Story_Walkthrough*` (oracle only).
