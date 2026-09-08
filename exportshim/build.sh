#!/usr/bin/env bash
# Build the managed export shim.
#
#   ./build.sh
set -e
cd "$(dirname "$0")"

mkdir -p build
mcs -target:library -platform:x64 -out:build/MyWhooshShim.dll ExportShim.cs

echo "built build/MyWhooshShim.dll"
echo "install with:  WINEPREFIX=<prefix> ./install.sh"
