#!/usr/bin/env bash
# Put the shim where mono will find it, inside one Wine prefix.
#
#   WINEPREFIX=<prefix> ./install.sh
#   WINEPREFIX=<prefix> ./install.sh --verify
#   WINEPREFIX=<prefix> ./install.sh --restore
#
# The assemblies go in the prefix's own wine-mono tree, never in the game
# directory: MyWhoosh hashes WindowsConnectivity.dll and silently declines to
# load it when a byte near it changed, and nothing here needs a game file
# touched.  mono probes c:\windows\mono\mono-2.0\lib for a referenced assembly
# by simple name, which is this directory, and `Windows` is exactly the name the
# game's metadata asks for.
#
# --restore puts ../winmd's inert stubs back rather than deleting anything: with
# no assembly of that name the game does not start at all.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to install into}"
TARGET="$WINEPREFIX/drive_c/windows/mono/mono-2.0/lib"
SHIM="Windows.dll System.Runtime.WindowsRuntime.dll"

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }
[ -d "$TARGET" ] || { echo "no wine-mono in $WINEPREFIX (expected $TARGET)" >&2; exit 1; }

case "$1" in
--verify)
    for dll in $SHIM; do
        if [ ! -f "$TARGET/$dll" ]; then
            echo "  MISSING  $TARGET/$dll"
            continue
        fi
        if [ -f "build/$dll" ] && cmp -s "build/$dll" "$TARGET/$dll"; then
            echo "  ok       $TARGET/$dll (this build)"
        elif [ -f "../winmd/build/$dll" ] && cmp -s "../winmd/build/$dll" "$TARGET/$dll"; then
            echo "  stub     $TARGET/$dll (../winmd -- the game will find no devices)"
        else
            echo "  other    $TARGET/$dll (neither this build nor ../winmd)"
        fi
    done
    exit 0
    ;;
--restore)
    [ -d ../winmd/build ] || { echo "../winmd is not built; run ../winmd/build.sh" >&2; exit 1; }
    for dll in $SHIM; do
        cp -f "../winmd/build/$dll" "$TARGET/$dll"
        echo "  restored $TARGET/$dll (inert stub)"
    done
    echo "done -- the game starts and reports no Bluetooth devices"
    exit 0
    ;;
esac

for dll in $SHIM; do
    [ -f "build/$dll" ] && continue
    # A release bundle ships build/ prepopulated and has no build.sh at all.
    [ -x ./build.sh ] || { echo "missing build/$dll and no build.sh to make it" >&2; exit 1; }
    ./build.sh
    break
done

for dll in $SHIM; do
    cp -f "build/$dll" "$TARGET/$dll"
    echo "  installed $TARGET/$dll"
done

cat <<'EOF'
done.  Two things this prefix still needs:

  * ./blehelper.py running before the game, or it sees no Bluetooth at all
  * ../exportshim installed, or the game's device list crashes on its first poll

and WindowsConnectivity.dll must be left unpatched, or the game will not load it.
EOF
