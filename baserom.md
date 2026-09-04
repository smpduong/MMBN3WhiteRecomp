# Base ROM identity

Required input: Mega Man Battle Network 3 - White, game code `A6BE`.

- Size: 8,388,608 bytes
- SHA-1: `ff45038ae6d01cde4eae25a02dcb8bed29e07a6f`
- MD5: `68817204a691449e655cba739dbb0165`
- SHA-256: `a161aa80e9ee7e07e85bda5d1c93ad3d1415e35aee88380fe117997ebaf6c1c2`
- CRC32: `0x0be4410a`
- Header entry: ARM branch to `0x080000C0`
- Save signature: `SRAM_V` at ROM offset `0x00231C30`
- Local reference: `ROMS/Mega-Man Battle Network 3 - White # GBA.GBA` (do not copy into this repo)

Copy your legally obtained dump to `roms/mmbn3_white_usa.gba` (gitignored).
The ROM must never be committed. Reference saves/states beside it
(`.sav 64K`, `.sgm`) are validation oracles only.
