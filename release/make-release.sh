#!/usr/bin/env bash
# Build a release bundle: everything prebuilt, so installing it needs no
# compiler, no mono-devel and no p7zip -- only Wine, which Lutris supplies.
#
#   release/make-release.sh [<outdir>]     # default: release/dist
#
# What the bundle deliberately does NOT contain:
#
#   * a patched System.Core.dll.  wine-mono's own copy is rewritten inside the
#     prefix at install time (winemono/install.sh), so the bundle is not tied to
#     one wine-mono version and nothing derived from wine-mono is redistributed.
#   * Mono.Cecil.dll.  Taken from the prefix's own wine-mono tree instead.
#   * anything from the game, or any of Apple's Bonjour.
set -e
cd "$(dirname "$0")/.."
REPO="$PWD"

VER="$(git describe --tags --always --dirty 2>/dev/null || date +%Y%m%d)"
OUT="${1:-$REPO/release/dist}"
NAME="mywhoosh-connectivity-$VER"
STAGE="$OUT/$NAME"

for tool in mcs python3 x86_64-w64-mingw32-gcc cc; do
    command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 1; }
done

echo "== building $VER"
./winemono/build.sh >/dev/null
./winmd/build.sh    >/dev/null
./exportshim/build.sh >/dev/null
./fakesensor/build.sh >/dev/null

echo "== staging $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE"/{winemono/build,winmd/build,exportshim/build,fakesensor/build}

install -m755 install.sh "$STAGE/"
install -m644 README.md  "$STAGE/README.md"
install -m644 release/BUNDLE.md "$STAGE/INSTALL.md"
printf '%s\n' "$VER" > "$STAGE/VERSION"

install -m755 winemono/install.sh   "$STAGE/winemono/"
install -m644 winemono/build/PatchSystemCore.exe \
              winemono/build/MyWhoosh.ComEventShim.dll "$STAGE/winemono/build/"

install -m755 winmd/install.sh "$STAGE/winmd/"
install -m644 winmd/build/Windows.dll \
              winmd/build/System.Runtime.WindowsRuntime.dll "$STAGE/winmd/build/"

install -m755 exportshim/install.sh "$STAGE/exportshim/"
install -m644 exportshim/build/MyWhooshShim.dll "$STAGE/exportshim/build/"

install -m755 fakesensor/install.sh fakesensor/run.sh fakesensor/blebridge.py "$STAGE/fakesensor/"
# The .so is the one native Linux artifact, so ship its source too: a bundle
# built elsewhere may not load here, and install.sh rebuilds it if so.
install -m644 fakesensor/dotlocal_shim.c fakesensor/README.md "$STAGE/fakesensor/"
install -m644 fakesensor/build/fakebonjour.dll fakesensor/build/bonjourstub.exe "$STAGE/fakesensor/build/"
install -m755 fakesensor/build/dotlocal_shim.so "$STAGE/fakesensor/build/"

echo "== packing"
tar -C "$OUT" -czf "$OUT/$NAME.tar.gz" "$NAME"
( cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
rm -rf "$STAGE"

echo
echo "$OUT/$NAME.tar.gz  ($(du -h "$OUT/$NAME.tar.gz" | cut -f1))"
cat "$OUT/$NAME.tar.gz.sha256"
echo
echo "install it with:  tar xf $NAME.tar.gz && $NAME/install.sh <prefix>"
