#!/usr/bin/env bash
# Build the two assemblies the game loads instead of WinRT.
#
#   ./build.sh
#
# Env overrides: GAME_LIBS (where to find WindowsConnectivity.dll, to check the
# surface against the game's own metadata).
#
# These supersede ../winmd/build/: same assembly names, same 34 types and 60
# members, answers backed by blehelper.py instead of empty.  Install one or the
# other into a prefix, never both.
set -e
cd "$(dirname "$0")"

mkdir -p build

mcs -target:library -out:build/Windows.dll src/Windows.cs src/Backend.cs src/Json.cs src/Loader.cs

# mcs refuses to name an assembly System.Runtime.WindowsRuntime (CS0281:
# mscorlib grants that name friend access and we cannot sign with Microsoft's
# key), so build it one character longer and shorten the name afterwards.
mcs -target:library -out:build/System.Runtime.WindowsRuntimeX.dll \
    -r:build/Windows.dll src/SystemRuntimeWindowsRuntime.cs
mv -f build/System.Runtime.WindowsRuntimeX.dll build/System.Runtime.WindowsRuntime.dll
../winmd/rename_assembly.py build/System.Runtime.WindowsRuntime.dll System.Runtime.WindowsRuntime

GAME_LIBS="${GAME_LIBS:-$HOME/Games/mywhoosh/drive_c/MyWhoosh/MyWhoosh/Binaries/Win64}"
if [ -f "$GAME_LIBS/WindowsConnectivity.dll" ]; then
    echo
    # The game's metadata is the contract: anything it references and this does
    # not implement is a TypeLoadException in the middle of a ride.
    ../winmd/members.py "$GAME_LIBS/WindowsConnectivity.dll" --check --build-dir build
else
    echo
    echo "note: WindowsConnectivity.dll not found, skipping the coverage check (set GAME_LIBS=)"
fi

echo
echo "built build/Windows.dll + build/System.Runtime.WindowsRuntime.dll"
echo "install with:  WINEPREFIX=<prefix> ./install.sh"
echo "and start ./blehelper.py before the game"
