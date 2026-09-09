#!/usr/bin/env bash
# Install the patched runtime into a Wine prefix.
#
#   WINEPREFIX=<prefix> ./install.sh
#
# wine-mono is looked up in the prefix (c:\windows\mono\mono-2.0) before the
# runner's shared copy, so this affects one prefix only and the runner tree is
# never written to.  The copy is hard-linked, so it costs a few hundred kB
# rather than the 230 MB the tree weighs.
#
# ComAwareEventInfo is patched, not replaced: whichever wine-mono the runner
# ships is copied in, and then that copy's own System.Core.dll is rewritten.
# With a checkout the rewrite has already happened on the Linux side
# (build/System.Core.dll); in a release bundle PatchSystemCore.exe is run
# inside the prefix instead, which is byte-for-byte the same result and needs
# no mono-devel.  Shipping a prebuilt System.Core.dll would tie the bundle to
# one wine-mono version, which is the thing worth avoiding here.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to patch}"
WINE_MONO="${WINE_MONO:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/share/wine/mono/wine-mono-10.0.0}"
VERSION="$(basename "$WINE_MONO")"
# mscoree probes c:\windows\mono\mono-2.0 first, whatever the runtime version is.
TARGET="$WINEPREFIX/drive_c/windows/mono/mono-2.0"

WINE="${WINE:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/bin/wine64}"
[ -x "$WINE" ] || WINE="${WINE%64}"
[ -x "$WINE" ] || WINE=wine64
command -v "$WINE" >/dev/null 2>&1 || WINE=wine

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }
[ -d "$WINE_MONO" ] || { echo "wine-mono tree not found at $WINE_MONO (set WINE_MONO=)" >&2; exit 1; }

if [ ! -f build/System.Core.dll ] && [ ! -f build/PatchSystemCore.exe ]; then
    [ -x ./build.sh ] || { echo "nothing in build/ and no build.sh to make it" >&2; exit 1; }
    ./build.sh
fi
[ -f build/MyWhoosh.ComEventShim.dll ] || { echo "missing build/MyWhoosh.ComEventShim.dll" >&2; exit 1; }

echo "installing $VERSION into $TARGET"
rm -rf "$TARGET"
mkdir -p "$(dirname "$TARGET")"
cp -al "$WINE_MONO" "$TARGET"

PATCHED="$PWD/build/System.Core.dll"
WORK=""
if [ ! -f "$PATCHED" ]; then
    # No Linux-side build: rewrite the copy we just made, from inside the prefix.
    # Cecil comes out of that same tree, so nothing has to be redistributed.
    CECIL="${CECIL:-$(echo "$TARGET"/lib/mono/gac/Mono.Cecil/0.11*/Mono.Cecil.dll)}"
    [ -f "$CECIL" ] || { echo "Mono.Cecil.dll not found in $TARGET (set CECIL=)" >&2; exit 1; }
    WORK="$(mktemp -d)"
    cp -f build/PatchSystemCore.exe build/MyWhoosh.ComEventShim.dll "$CECIL" "$WORK/"
    cp -f "$(echo "$TARGET"/lib/mono/gac/System.Core/*/System.Core.dll)" "$WORK/System.Core.in.dll"
    echo "  patching ComAwareEventInfo with the prefix's own runtime"
    ( cd "$WORK" && WINEPREFIX="$WINEPREFIX" WINEDEBUG="${WINEDEBUG:-fixme-all,err-all}" \
        "$WINE" PatchSystemCore.exe System.Core.in.dll MyWhoosh.ComEventShim.dll \
                System.Core.out.dll ) | sed -n 's/^patched/  patched/p'
    PATCHED="$WORK/System.Core.out.dll"
    [ -f "$PATCHED" ] || { echo "the in-prefix patch produced nothing" >&2; exit 1; }
fi

# rm before cp: the tree is hard-linked, so writing in place would edit the runner's copy.
for dll in "$TARGET"/lib/mono/gac/System.Core/*/System.Core.dll "$TARGET"/lib/mono/4.5/System.Core.dll; do
    rm -f "$dll"
    cp "$PATCHED" "$dll"
    cp build/MyWhoosh.ComEventShim.dll "$(dirname "$dll")/"
    echo "  patched $(realpath --relative-to="$TARGET" "$dll")"
done

if [ -n "$WORK" ]; then rm -rf "$WORK"; fi

echo "done -- run winemono/ShimProbe.exe or dircon/BonjourProbe.exe in this prefix"
