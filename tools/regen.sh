#!/bin/sh
# Regenerate generated/ from the verified ROM via the sibling fork checkout.
# Usage: tools/regen.sh [--rom PATH] [--config PATH] [--out DIR]
#                        [--engine PATH] [ROM_PATH positional, = --rom]
# Env overrides: GBARECOMP_ROOT, MMBN3_ROM.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${GBARECOMP_ROOT:-$ROOT/../gbarecomp}"
CONFIG="$ROOT/game.toml"
OUT="$ROOT/generated"
ROM="${MMBN3_ROM:-$ROOT/roms/mmbn3_white_usa.gba}"
while [ $# -gt 0 ]; do
  case "$1" in
    --rom) ROM="$2"; shift 2;;
    --rom=*) ROM="${1#--rom=}"; shift;;
    --config) CONFIG="$2"; shift 2;;
    --config=*) CONFIG="${1#--config=}"; shift;;
    --out) OUT="$2"; shift 2;;
    --out=*) OUT="${1#--out=}"; shift;;
    --engine) ENGINE="$2"; shift 2;;
    --engine=*) ENGINE="${1#--engine=}"; shift;;
    -h|--help)
      sed -n '2,4p' "$0"; exit 0;;
    --) shift; break;;
    -*) echo "unknown option: $1 (see --help)" >&2; exit 2;;
    *) ROM="$1"; shift;;
  esac
done
echo "engine=$ENGINE"
echo "rom=$ROM"
echo "out=$OUT"
test -x "$ENGINE/build/gba_recompile" || {
  echo "build engine first: cmake -S $ENGINE -B $ENGINE/build && cmake --build $ENGINE/build" >&2
  exit 1
}
test -f "$ROM" || {
  echo "copy your dump to $ROM (see baserom.md; never commit it)" >&2
  exit 1
}
"$ENGINE/build/gba_recompile" --config "$ROOT/game.toml" --rom "$ROM" --out "$OUT"
