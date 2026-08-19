#!/usr/bin/env bash
# Build the fake Bonjour COM server and the .local resolver shim.
#
#   ./build.sh
set -e
cd "$(dirname "$0")"

CC_WIN="${CC_WIN:-x86_64-w64-mingw32-gcc}"
command -v "$CC_WIN" >/dev/null || { echo "$CC_WIN not found (install mingw-w64)" >&2; exit 1; }

mkdir -p build
$CC_WIN -shared -O2 -Wall -o build/fakebonjour.dll fakebonjour.c \
        -luuid -lole32 -loleaut32 -lws2_32
cc -shared -fPIC -O2 -Wall -o build/dotlocal_shim.so dotlocal_shim.c -ldl

echo "built build/fakebonjour.dll and build/dotlocal_shim.so"
echo "install with:  WINEPREFIX=<prefix> ./install.sh"
