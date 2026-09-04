#!/bin/sh
# Regenerate generated/ from the verified ROM via the sibling fork checkout.
# Usage: tools/regen.sh [--rom roms/mmbn3_white_usa.gba]
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${GBARECOMP_ROOT:-$ROOT/../gbarecomp}"
ROM="${1:-$ROOT/roms/mmbn3_white_usa.gba}"
OUT="$ROOT/generated"
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
"$ENGINE/build/gba_recompile" --config "$ROOT/game.toml" --rom "$ROM" --output "$OUT"
