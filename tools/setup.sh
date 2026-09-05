#!/bin/sh
# Fresh-checkout setup for MMBN3WhiteRecomp (sibling-checkout arrangement).
#
# Layout produced (WORKSPACE defaults to the current directory):
#   $WORKSPACE/MMBN3WhiteRecomp   this repository
#   $WORKSPACE/gbarecomp          smpduong/gbarecomp @ pinned revision
#
# Private inputs (never committed, never uploaded); supply as env:
#   MMBN3_ROM   path to your legally obtained MMBN3 White (USA) dump
#   GBA_BIOS    path to your legally obtained GBA BIOS dump (16 KiB)
#
# Usage: MMBN3_ROM=/path/to/white.gba GBA_BIOS=/path/to/gba_bios.bin \
#          [WORKSPACE=/path/to/ws] tools/setup.sh
#
# Verifies every step and aborts (nonzero) with the failing command logged.
set -eu

ENGINE_REPO="https://github.com/smpduong/gbarecomp.git"
ENGINE_REV="425d941235fab49aedb744cd4bbf974eed88c808"
ARM_SUB_REV="14be3cfbd889edf8bf74a83b7deb728539fe4c80"
ROM_SHA1="ff45038ae6d01cde4eae25a02dcb8bed29e07a6f"
ROM_SIZE="8388608"
BIOS_SHA1="300c20df6731a33952ded8c436f7f186d25d3492"
BIOS_SIZE="16384"

GAME_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="${WORKSPACE:-$(dirname "$GAME_DIR")}"
ENGINE="$WORKSPACE/gbarecomp"
BUILD_TYPE="${CMAKE_BUILD_TYPE:-Release}"

log() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] FATAL: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing prerequisite: $1"; }
run() { log "+ $*"; "$@" || die "exit $? from: $*"; }

[ -n "${MMBN3_ROM:-}" ] || die "set MMBN3_ROM to your ROM dump path"
[ -n "${GBA_BIOS:-}" ] || die "set GBA_BIOS to your BIOS dump path"
[ -f "$MMBN3_ROM" ] || die "ROM not found: $MMBN3_ROM"
[ -f "$GBA_BIOS" ] || die "BIOS not found: $GBA_BIOS"
for t in git cmake cc python3; do need "$t"; done
[ -f /opt/homebrew/include/SDL2/SDL.h ] || [ -f /usr/include/SDL2/SDL.h ] \
  || die "SDL2 headers not found (macOS: brew install sdl2)"

sha1_of() { python3 -c "import hashlib,sys; print(hashlib.sha1(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }
size_of() { python3 -c "import os,sys; print(os.path.getsize(sys.argv[1]))" "$1"; }

log "workspace=$WORKSPACE"
log "verifying private inputs (hashes only, files never leave this machine)"
[ "$(sha1_of "$MMBN3_ROM")" = "$ROM_SHA1" ] \
  || die "ROM SHA-1 mismatch (want $ROM_SHA1)"
[ "$(size_of "$MMBN3_ROM")" = "$ROM_SIZE" ] || die "ROM size mismatch"
[ "$(sha1_of "$GBA_BIOS")" = "$BIOS_SHA1" ] \
  || die "BIOS SHA-1 mismatch (want $BIOS_SHA1)"
log "ROM+BIOS identities verified"

if [ -d "$ENGINE/.git" ]; then
  log "engine checkout exists; verifying revision"
  [ "$(git -C "$ENGINE" rev-parse HEAD)" = "$ENGINE_REV" ] \
    || die "engine at $(git -C "$ENGINE" rev-parse HEAD), want $ENGINE_REV (set a fresh WORKSPACE or update manually)"
else
  run git clone --recurse-submodules "$ENGINE_REPO" "$ENGINE"
  run git -C "$ENGINE" checkout --detach "$ENGINE_REV"
  run git -C "$ENGINE" submodule update --init --recursive
fi
run git -C "$ENGINE" submodule status | grep -q "$ARM_SUB_REV" \
  || { git -C "$ENGINE" submodule status; die "arm-recomp-core not at $ARM_SUB_REV"; }
log "engine @ $(git -C "$ENGINE" rev-parse --short HEAD), submodule pinned ok"

log "building engine"
run cmake -S "$ENGINE" -B "$ENGINE/build" -DCMAKE_BUILD_TYPE="$BUILD_TYPE"
run cmake --build "$ENGINE/build" -j 8
[ -x "$ENGINE/build/gba_recompile" ] || die "gba_recompile missing"

log "installing BIOS copy + recompiling BIOS (absolute --out)"
run cp "$GBA_BIOS" "$ENGINE/bios/gba_bios.bin"
ABS_OUT="$ENGINE/src/runtime/generated_bios"
run "$ENGINE/build/gba_recompile" --bios "$ENGINE/bios/gba_bios.bin" \
  --config "$ENGINE/bios/gba_bios.toml" --out "$ABS_OUT"
[ -s "$ABS_OUT/bios_recompiled.cpp" ] || die "BIOS codegen produced nothing"
grep -q "void reset_vector" "$ABS_OUT/bios_recompiled.cpp" \
  || die "BIOS codegen missing reset_vector"
grep -q "0x00000138u" "$ABS_OUT/bios_dispatch_table.cpp" \
  || die "BIOS dispatch table looks empty"

log "staging ROM + generating game code"
run cp "$MMBN3_ROM" "$GAME_DIR/roms/mmbn3_white_usa.gba"
run sh "$GAME_DIR/tools/regen.sh" --engine "$ENGINE"
[ -s "$GAME_DIR/generated/recompiled_000.cpp" ] \
  || die "game codegen produced nothing"

log "configuring game (assert real BIOS, not the stub)"
CONFIRM="$(cmake -S "$GAME_DIR" -B "$GAME_DIR/build" \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" -DGBARECOMP_ROOT="$ENGINE" 2>&1)" \
  || { printf '%s\n' "$CONFIRM"; die "game configure failed"; }
printf '%s\n' "$CONFIRM" | grep -q "BIOS recompiled output present" \
  || { printf '%s\n' "$CONFIRM"; die "build would link the EMPTY BIOS dispatch stub"; }
log "configure ok (real BIOS linked)"
run cmake --build "$GAME_DIR/build" -j 8
[ -x "$GAME_DIR/build/MMBN3WhiteRecomp" ] || die "game binary missing"

log "setup complete"
log "launch from: $GAME_DIR"
log "  ./build/MMBN3WhiteRecomp --rom roms/mmbn3_white_usa.gba --bios ../gbarecomp/bios/gba_bios.bin"
