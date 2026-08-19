#!/usr/bin/env bash
# Install the patched runtime into a Wine prefix.
#
#   WINEPREFIX=<prefix> ./install.sh
#
# wine-mono is looked up in the prefix (c:\windows\mono\mono-2.0) before the
# runner's shared copy, so this affects one prefix only and the
# runner tree is never written to.  The copy is hard-linked, so it costs a few
# hundred kB rather than the 230 MB the tree weighs.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to patch}"
WINE_MONO="${WINE_MONO:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/share/wine/mono/wine-mono-10.0.0}"
VERSION="$(basename "$WINE_MONO")"
# mscoree probes c:\windows\mono\mono-2.0 first, whatever the runtime version is.
TARGET="$WINEPREFIX/drive_c/windows/mono/mono-2.0"

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }
[ -f build/System.Core.dll ] || ./build.sh

echo "installing $VERSION into $TARGET"
rm -rf "$TARGET"
mkdir -p "$(dirname "$TARGET")"
cp -al "$WINE_MONO" "$TARGET"

# rm before cp: the tree is hard-linked, so writing in place would edit the runner's copy.
for dll in "$TARGET"/lib/mono/gac/System.Core/*/System.Core.dll "$TARGET"/lib/mono/4.5/System.Core.dll; do
    rm -f "$dll"
    cp build/System.Core.dll "$dll"
    cp build/MyWhoosh.ComEventShim.dll "$(dirname "$dll")/"
    echo "  patched $(realpath --relative-to="$TARGET" "$dll")"
done

echo "done -- run winemono/ShimProbe.exe or dircon/BonjourProbe.exe in this prefix"
