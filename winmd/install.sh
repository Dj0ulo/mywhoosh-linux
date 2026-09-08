#!/usr/bin/env bash
# Drop the stub assemblies where mono looks for them, inside one Wine prefix.
#
#   WINEPREFIX=<prefix> ./install.sh
#   WINEPREFIX=<prefix> ./install.sh --restore
#
# They go in the prefix's own wine-mono tree, not the game directory: MyWhoosh
# hashes WindowsConnectivity.dll and refuses to load it if anything next to it
# changed it, and nothing about this fix needs a game file touched.  mono probes
# c:\windows\mono\mono-2.0\lib for a referenced assembly by simple name, which
# is exactly this directory.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to install into}"
TARGET="$WINEPREFIX/drive_c/windows/mono/mono-2.0/lib"

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }
[ -d "$TARGET" ] || { echo "no wine-mono in $WINEPREFIX (expected $TARGET)" >&2; exit 1; }

STUBS="Windows.dll System.Runtime.WindowsRuntime.dll"

if [ "$1" = "--restore" ]; then
    for dll in $STUBS; do
        rm -f "$TARGET/$dll" && echo "  removed $TARGET/$dll"
    done
    echo "done -- the game will crash on launch again unless it is patched"
    exit 0
fi

for dll in $STUBS; do
    [ -f "build/$dll" ] || ./build.sh
done

for dll in $STUBS; do
    cp -f "build/$dll" "$TARGET/$dll"
    echo "  installed $TARGET/$dll"
done

echo "done -- leave WindowsConnectivity.dll unpatched, or the game will not load it"
