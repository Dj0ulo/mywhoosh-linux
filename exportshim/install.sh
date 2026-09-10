#!/usr/bin/env bash
# Put the export shim where ../bleshim/src/Loader.cs looks for it.
#
#   WINEPREFIX=<prefix> ./install.sh
#   WINEPREFIX=<prefix> ./install.sh --restore
#
# Same directory as ../winmd's stubs -- the prefix's own wine-mono tree -- and
# for the same reason: MyWhoosh hashes WindowsConnectivity.dll and stops loading
# it if anything in the game tree changed, so nothing of ours may live there.
# The loader derives this path from its own assembly's location at run time, so
# the two stay in step without either being told where the prefix is.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to install into}"
TARGET="$WINEPREFIX/drive_c/windows/mono/mono-2.0/lib"

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }
[ -d "$TARGET" ] || { echo "no wine-mono in $WINEPREFIX (expected $TARGET)" >&2; exit 1; }

if [ "$1" = "--restore" ]; then
    rm -f "$TARGET/MyWhooshShim.dll" && echo "  removed $TARGET/MyWhooshShim.dll"
    echo "done -- the four device-list exports will be fatal again"
    exit 0
fi

if [ ! -f build/MyWhooshShim.dll ]; then
    # A release bundle ships build/ prepopulated and has no build.sh at all.
    [ -x ./build.sh ] || { echo "missing build/MyWhooshShim.dll and no build.sh" >&2; exit 1; }
    ./build.sh
fi
cp -f build/MyWhooshShim.dll "$TARGET/MyWhooshShim.dll"
echo "  installed $TARGET/MyWhooshShim.dll"
echo "done -- ../bleshim must be installed too, it is what calls Install()"
