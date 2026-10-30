#!/bin/bash
# Deploy the freshly built Bluetooth stack into the existing MyWhoosh prefix,
# with a reversible backup and a verification pass.
#
#   scripts/deploy.sh            # prefix ~/Games/mywhoosh
#   scripts/deploy.sh <other>    # a different prefix
#
# What it installs: the four files the prefix's bleshim uses
# (Windows.dll, System.Runtime.WindowsRuntime.dll, MyWhooshShim.dll and
# blehelper.py), built from this tree right before.  Nothing else moves:
# mywhoosh-ble.sh and the Lutris configuration stay untouched, and the
# game's own directory is never written to -- MyWhoosh hashes
# WindowsConnectivity.dll and drops the whole BLE path at the smell of
# anything else, so the hash is re-checked before and after.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${1:-$HOME/Games/mywhoosh}"
DST="$PREFIX/bleshim"
GAME_DLL="$PREFIX/drive_c/MyWhoosh/MyWhoosh/Binaries/Win64/WindowsConnectivity.dll"

W_DLL="$REPO/bleshim/build/Windows.dll"
W_RT="$REPO/bleshim/build/System.Runtime.WindowsRuntime.dll"
S_DLL="$REPO/exportshim/build/MyWhooshShim.dll"
HELPER="$REPO/bleshim/blehelper.py"
for f in "$W_DLL" "$W_RT" "$S_DLL" "$HELPER"; do
    [ -f "$f" ] || { echo "ABSENT: $f -- run bleshim/build.sh and exportshim/build.sh first" >&2; exit 1; }
done
if [ ! -f "$GAME_DLL" ]; then
    echo "the game's WindowsConnectivity.dll is not where it was expected: $GAME_DLL" >&2
    exit 1
fi
if [ ! -f "$DST/mywhoosh-ble.sh" ]; then
    echo "$DST does not look like a prefix bleshim (mywhoosh-ble.sh missing)" >&2
    exit 1
fi

game_hash_before=$(sha256sum "$GAME_DLL" | cut -d' ' -f1)

# ------------------------------------------------------------ backup
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$DST/shim-backup-$STAMP"
mkdir -p "$BACKUP"
echo "== backing up what is there now into $BACKUP"
for name in Windows.dll System.Runtime.WindowsRuntime.dll MyWhooshShim.dll blehelper.py; do
    if [ -f "$DST/$name" ]; then
        cp -a "$DST/$name" "$BACKUP/"
        echo "   $name  $(sha256sum "$DST/$name" | cut -c1-16)..."
    else
        echo "   $name  (absent -- nothing to back up)"
    fi
done

# ------------------------------------------------------------ install
echo "== installing"
cp -f "$W_DLL"  "$DST/Windows.dll"
cp -f "$W_RT"   "$DST/System.Runtime.WindowsRuntime.dll"
cp -f "$S_DLL"  "$DST/MyWhooshShim.dll"
cp -f "$HELPER" "$DST/blehelper.py"
chmod 644 "$DST/Windows.dll" "$DST/System.Runtime.WindowsRuntime.dll" "$DST/MyWhooshShim.dll"
chmod 755 "$DST/blehelper.py"

echo "== fingerprints as installed (source -> destination)"
for pair in "$W_DLL:$DST/Windows.dll" "$W_RT:$DST/System.Runtime.WindowsRuntime.dll" \
            "$S_DLL:$DST/MyWhooshShim.dll" "$HELPER:$DST/blehelper.py"; do
    src=${pair%%:*}; dst=${pair#*:}
    a=$(sha256sum "$src" | cut -d' ' -f1)
    b=$(sha256sum "$dst" | cut -d' ' -f1)
    if [ "$a" = "$b" ]; then echo "   ok   $(basename "$dst")  ${a:0:16}..."
    else echo "   CORRUPT $dst (expected ${a:0:16}..., found ${b:0:16}...)"; exit 1; fi
done

# ------------------------------------------------------------ integrity of the game file
game_hash_after=$(sha256sum "$GAME_DLL" | cut -d' ' -f1)
echo "== integrity of the game's own DLL"
if [ "$game_hash_before" = "$game_hash_after" ]; then
    echo "   ok   WindowsConnectivity.dll unchanged (${game_hash_after:0:16}...)"
else
    echo "   ALERT: WindowsConnectivity.dll changed during the deployment!" >&2
    echo "   before: $game_hash_before" >&2
    echo "   after:  $game_hash_after" >&2
    exit 1
fi

# ------------------------------------------------------------ helper lifecycle
if ss -ltn 2>/dev/null | grep -q ':27019 '; then
    echo "== helper: already serving on port 27019 -- skipping the start/stop pass"
    echo "   (restart the game or re-run mywhoosh-ble.sh to feed it the new helper)"
else
    echo "== helper lifecycle"
    "$DST/mywhoosh-ble.sh" start >/dev/null 2>&1 || true
    up=no
    for i in $(seq 1 30); do
        if (exec 3<>"/dev/tcp/127.0.0.1/27019") 2>/dev/null; then exec 3<&- 3>&-; up=yes; break; fi
        sleep 0.2
    done
    if [ "$up" = yes ]; then echo "   ok   started, answering on 127.0.0.1:27019"; else
        echo "   FAIL: the helper is not listening on 27019 -- see mywhoosh-ble.sh's log" >&2
        exit 1
    fi
    "$DST/mywhoosh-ble.sh" stop >/dev/null 2>&1 || true
    sleep 1
    if pgrep -af "$PREFIX/bleshim/blehelper.py" >/dev/null 2>&1; then
        echo "   FAIL: a blehelper.py from this prefix survived the stop:" >&2
        pgrep -af "$PREFIX/bleshim/blehelper.py" >&2
        exit 1
    fi
    echo "   ok   stopped, no stray process"
    echo "   (Lutris starts it again the way the game itself does)"
fi

echo
echo "Done. Prefix: $PREFIX   Backup: $BACKUP"
echo "Roll back with:  scripts/restore.sh <this prefix>"
