#!/bin/bash
# Put the previously installed Bluetooth stack back, from the most recent
# shim-backup-* that scripts/deploy.sh left in the prefix.
#
#   scripts/restore.sh            # prefix ~/Games/mywhoosh
#   scripts/restore.sh <other>    # a different prefix
#
# Same four files the deploy replaces, in the other direction; the game's
# own directory is not touched, so there is nothing to restore there -- it
# was never modified.
set -euo pipefail
PREFIX="${1:-$HOME/Games/mywhoosh}"
DST="$PREFIX/bleshim"

LATEST=$(ls -1dt "$DST"/shim-backup-* 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    echo "no shim-backup-* directory found in $DST" >&2
    exit 1
fi

if ss -ltn 2>/dev/null | grep -q ':27019 '; then
    echo "helper is up -- stopping it before the restore" >&2
    "$DST/mywhoosh-ble.sh" stop >/dev/null 2>&1 || true
    sleep 1
fi

echo "== restoring from $LATEST"
for name in Windows.dll System.Runtime.WindowsRuntime.dll MyWhooshShim.dll blehelper.py; do
    if [ -f "$LATEST/$name" ]; then
        cp -f "$LATEST/$name" "$DST/$name"
        chmod 644 "$DST/$name"
        [ "$name" = blehelper.py ] && chmod 755 "$DST/$name"
        echo "   ok   $name  <- $(sha256sum "$LATEST/$name" | cut -c1-16)..."
    else
        echo "   (-- $name was not in that backup)"
    fi
done

echo
echo "Done. The backup is kept in place ($LATEST);"
echo "remove it only once the new stack has proven itself."
