#!/bin/sh
# MMBN3WhiteRecomp regression suite: one command, nonzero on failure.
# Usage: ./tools/regress.sh
# Each test gets a timeout, an isolated dir under build/regress/<ts>/,
# explicit starting state and retained logs. identities.txt records the
# exact executable/configuration/framework under test.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
SUITE="$ROOT/build/regress/$TS"
mkdir -p "$SUITE"
EXE="$ROOT/build/MMBN3WhiteRecomp"
ROM="$ROOT/roms/mmbn3_white_usa.gba"
BIOS="$ROOT/../gbarecomp/bios/gba_bios.bin"
PASS=0; FAIL=0; SKIPPED=""

{
  echo "exe=$EXE"
  echo "exe_size=$(python3 -c "import os;print(os.path.getsize('$EXE'))")"
  echo "game_rev=$(git -C "$ROOT" rev-parse HEAD)"
  echo "game_toml_sha256=$(python3 -c "import hashlib;print(hashlib.sha256(open('$ROOT/game.toml','rb').read()).hexdigest())")"
  echo "engine_rev=$(git -C "$ROOT/../gbarecomp" rev-parse HEAD)"
  echo "arm_sub=$(git -C "$ROOT/../gbarecomp" submodule status | tr -s ' ')"
  echo "rom_sha1=$(python3 -c "import hashlib;print(hashlib.sha1(open('$ROM','rb').read()).hexdigest())")"
  echo "bios_sha1=$(python3 -c "import hashlib;print(hashlib.sha1(open('$BIOS','rb').read()).hexdigest())")"
} > "$SUITE/identities.txt"
cat "$SUITE/identities.txt"

# run_with_timeout <seconds> <name> <log> <cmd...>: nonzero out on timeout/fail
run_with_timeout() {
  secs="$1"; name="$2"; log="$3"; shift 3
  python3 - "$secs" "$log" "$@" <<'EOF'
import subprocess, sys
secs, log = int(sys.argv[1]), sys.argv[2]
cmd = sys.argv[3:]
with open(log, "wb") as f:
    try:
        r = subprocess.run(cmd, timeout=secs, stdout=f, stderr=subprocess.STDOUT)
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        f.write(f"\n[TIMEOUT after {secs}s]\n".encode())
        sys.exit(124)
EOF
}

report() { # report <name> <code> [<note>]
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL($2): $1 ${3:-}"; fi
}
skip() { SKIPPED="$SKIPPED $1"; echo "SKIP: $1 ($2)"; }

echo "== suite dir: $SUITE"

run_with_timeout 150 key-ident "$SUITE/key_ident.log" \
  python3 "$ROOT/tools/key_ident.py" --port 19861
report "key-ident (11 KEYINPUT round-trips)" $?

# NOTE: never prefix VAR=x to run_with_timeout (for shell functions the
# assignment persists and leaks into later tests). Per-test env goes
# through `env` inside the call.
run_with_timeout 240 strict-boot "$SUITE/strict_boot.log" \
  env GBARECOMP_STRICT_STATIC=1 \
  "$EXE" --no-window --frames 30 --rom "$ROM" --bios "$BIOS"
code=$?
if [ $code -eq 0 ] && grep -q "FULLY_STATIC" "$SUITE/strict_boot.log" \
  && grep -q "interpreter_bridge=ABORT" "$SUITE/strict_boot.log"; then
  report "strict-boot (FULLY_STATIC, bridge=ABORT)" 0
else
  report "strict-boot" 1 "(see strict_boot.log)"
fi

run_with_timeout 900 title-menu "$SUITE/title_menu.log" \
  python3 "$ROOT/tools/boot_to_title.py" --port 19862 \
  --advance-timeout 180 --ready-timeout 600 --stable 3 --menu-timeout 60
report "title-menu (title + New Game screenshots)" $?

mkdir -p "$SUITE/savetest"
REF_SAV="${MMBN3_SAV:-/Users/user/Desktop/GBA Recomp/ROMS/Mega-Man Battle Network 3 - White # GBA.sav}"
if [ -f "$REF_SAV" ]; then
python3 -c "open('$SUITE/savetest/import32.sav','wb').write(open('$REF_SAV','rb').read()[:32768])"
run_with_timeout 240 save-import "$SUITE/save_import.log" \
  "$EXE" --no-window --frames 60 --save "$SUITE/savetest/import32.sav" \
  --rom "$ROM" --bios "$BIOS"
code=$?
if [ $code -eq 0 ] && grep -q "save_loaded.*32768/32768" "$SUITE/save_import.log"; then
  report "save-import (32KB loads)" 0
else
  report "save-import" 1 "(see save_import.log)"
fi
else
skip "save-import" "no reference save (set MMBN3_SAV)"
fi

skip "first-controllable-scene" "needs driven play past menu (title/menu proven)"
skip "save-reload-roundtrip" "needs a real in-game save point"
skip "tutorial-battle" "pending gameplay reach"

echo "== PASS=$PASS FAIL=$FAIL SKIPPED:$SKIPPED"
[ "$FAIL" -eq 0 ]
