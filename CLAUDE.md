# MMBN3WhiteRecomp contributor notes

Consumes sibling `../gbarecomp` fork checkout (`smpduong/gbarecomp`,
PolyForm Noncommercial 1.0.0). Pass `-DGBARECOMP_ROOT=<path>` only to test
another engine tree.

## Correctness boundary

- LLE baseline: real BIOS + original ARM/Thumb guest code; HLE opt-in only.
- Do not hand-edit `generated/*`. Fix metadata/engine, run `tools/regen.sh`.
- Interpreter bridge is discovery only, not a strict static result.
- Keep ROMs, BIOS, saves, generated code, binaries, diagnostics out of Git
  (see `.gitignore`). Reference `../ROMS/...White...gba` + `.sav/.sgm` and
  `../reference-video/Megaman_Battle_Network_3_*` as oracles only.
