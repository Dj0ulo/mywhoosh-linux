#!/usr/bin/env bash
# Build the Dircon probe against the game's own WindowsConnectivity.dll.
set -e
cd "$(dirname "$0")"

GAME_LIBS="${GAME_LIBS:-$HOME/Games/mywhoosh/drive_c/MyWhoosh/MyWhoosh/Content/Libraries/Win64}"
DLL="$GAME_LIBS/WindowsConnectivity.dll"

[ -f "$DLL" ] || { echo "WindowsConnectivity.dll not found at $DLL" >&2
                   echo "set GAME_LIBS=<...>/Content/Libraries/Win64" >&2; exit 1; }

mkdir -p build
cp -f "$DLL" build/WindowsConnectivity.dll
mcs -platform:x64 -out:build/TestDircon.exe -r:build/WindowsConnectivity.dll TestDircon.cs
echo "built build/TestDircon.exe"
