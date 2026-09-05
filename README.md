# MMBN3WhiteRecomp — Mega Man Battle Network 3 White, recompiled

Static recompilation of **Mega Man Battle Network 3 White** (GBA) to native PC,
built on [`gbarecomp`](https://github.com/smpduong/gbarecomp) (fork of
`mstan/gbarecomp`, PolyForm Noncommercial 1.0.0).

> Status — bring-up in progress. Native boot→title with graphics verified;
> boot and title routes pass strict-static from fresh processes; opening
> content is covered iteratively (see `docs/bringup.md`). Uncovered targets
> fall back to the interpreter, are reported, and get folded into `game.toml`.

This is not a decompilation or source port. No ROM, BIOS, save, or generated
ROM-derived source is included.

## ROM identity

| Target | Game | Code | SHA-1 | Debug port |
|---|---|---|---|---|
| `MMBN3WhiteRecomp` | MMBN3 White USA | A6BE | `ff45038ae6d01cde4eae25a02dcb8bed29e07a6f` | 19863 |

See `baserom.md`. Runtime hash-gates the ROM.

## Quick start (fresh checkout)

Dependency arrangement: documented sibling checkouts (no git submodules).
The game lives at `<workspace>/MMBN3WhiteRecomp`; the framework fork at
`<workspace>/gbarecomp` (`smpduong/gbarecomp` @ pinned revision, see
`tools/setup.sh`). CMake defaults (`-DGBARECOMP_ROOT`) assume that layout.

Prerequisites: `git`, `cmake`, a C/C++ compiler, `python3`, SDL2.
Private inputs (yours, never committed): MMBN3 White (USA) ROM dump +
16 KiB GBA BIOS dump.

1. `git clone <this repo> <workspace>/MMBN3WhiteRecomp`
2. `cd <workspace>/MMBN3WhiteRecomp`
3. `MMBN3_ROM=/path/to/white.gba GBA_BIOS=/path/to/gba_bios.bin tools/setup.sh`
   — clones + pins the engine, builds it, hash-verifies your dumps,
   regenerates BIOS code (explicit absolute `--out` into the directory the
   framework links; aborts if the build would use the empty dispatch stub),
   regenerates game code, and builds the runner.
4. Launch from the game directory:
   `./build/MMBN3WhiteRecomp --rom roms/mmbn3_white_usa.gba --bios ../gbarecomp/bios/gba_bios.bin`
5. `tools/regen.sh [--rom PATH] [--config PATH] [--out DIR] [--engine PATH]`
   regenerates `generated/`; never hand-edit it.
6. TCP debugging starts paused: send `{"cmd":"continue"}` first
   (see `tools/shot.py`).

Reference: `../ROMS/Mega-Man Battle Network 3 - White # GBA.GBA`,
`reference-video/Megaman_Battle_Network_3_Story_Walkthrough*` (oracle only).
