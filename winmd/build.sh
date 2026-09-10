#!/usr/bin/env bash
# Build the two stub assemblies WindowsConnectivity.dll needs and cannot find.
#
#   ./build.sh
#
# Env overrides: GAME_LIBS (where to find WindowsConnectivity.dll to check
# the stubs against).
set -e
cd "$(dirname "$0")"

mkdir -p build

mcs -target:library -out:build/Windows.dll Windows.cs

# mcs refuses to name an assembly System.Runtime.WindowsRuntime (CS0281:
# mscorlib grants that name friend access and we cannot sign with Microsoft's
# key), so build it one character longer and shorten the name afterwards.
mcs -target:library -out:build/System.Runtime.WindowsRuntimeX.dll \
    -r:build/Windows.dll SystemRuntimeWindowsRuntime.cs
mv -f build/System.Runtime.WindowsRuntimeX.dll build/System.Runtime.WindowsRuntime.dll
./rename_assembly.py build/System.Runtime.WindowsRuntime.dll System.Runtime.WindowsRuntime

GAME_LIBS="${GAME_LIBS:-$HOME/Games/mywhoosh/drive_c/MyWhoosh/MyWhoosh/Binaries/Win64}"
if [ -f "$GAME_LIBS/WindowsConnectivity.dll" ]; then
    echo
    ./members.py "$GAME_LIBS/WindowsConnectivity.dll" --check
else
    echo
    echo "note: WindowsConnectivity.dll not found, skipping the coverage check (set GAME_LIBS=)"
fi

echo
echo "built build/Windows.dll + build/System.Runtime.WindowsRuntime.dll"
echo "install with:  WINEPREFIX=<prefix> ./install.sh"
