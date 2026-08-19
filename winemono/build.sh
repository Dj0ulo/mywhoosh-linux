#!/usr/bin/env bash
# Build the shim, patch wine-mono's System.Core.dll, build the probe.
#
#   ./build.sh
#
# Env overrides: WINE_MONO (source runtime tree), CECIL (Mono.Cecil.dll)
set -e
cd "$(dirname "$0")"

WINE_MONO="${WINE_MONO:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/share/wine/mono/wine-mono-10.0.0}"
[ -d "$WINE_MONO" ] || { echo "wine-mono tree not found at $WINE_MONO" >&2; exit 1; }

SYSTEM_CORE=$(echo "$WINE_MONO"/lib/mono/gac/System.Core/*/System.Core.dll)
[ -f "$SYSTEM_CORE" ] || { echo "System.Core.dll not found under $WINE_MONO" >&2; exit 1; }

# wine-mono ships Cecil in its own GAC, so nothing needs downloading.
CECIL="${CECIL:-$(echo "$WINE_MONO"/lib/mono/gac/Mono.Cecil/0.11*/Mono.Cecil.dll)}"
[ -f "$CECIL" ] || { echo "Mono.Cecil.dll not found (set CECIL=)" >&2; exit 1; }

mkdir -p build
cp -f "$CECIL" build/Mono.Cecil.dll

mcs -target:library -platform:x64 -out:build/MyWhoosh.ComEventShim.dll ComEventShim.cs
mcs -out:build/PatchSystemCore.exe -r:build/Mono.Cecil.dll PatchSystemCore.cs
mcs -platform:x64 -out:build/ShimProbe.exe ShimProbe.cs

# SinkEmitProbe needs the game's embedded Bonjour interop types.
GAME_LIBS="${GAME_LIBS:-$HOME/Games/mywhoosh/drive_c/MyWhoosh/MyWhoosh/Content/Libraries/Win64}"
if [ -f "$GAME_LIBS/WindowsConnectivity.dll" ]; then
    cp -f "$GAME_LIBS/WindowsConnectivity.dll" build/
    mcs -platform:x64 -out:build/SinkEmitProbe.exe -r:build/MyWhoosh.ComEventShim.dll \
        -r:build/WindowsConnectivity.dll SinkEmitProbe.cs
    mcs -platform:x64 -out:build/ReflProbe.exe -r:build/WindowsConnectivity.dll ReflProbe.cs
    mcs -platform:x64 -out:build/SinkInvokeProbe.exe -r:build/MyWhoosh.ComEventShim.dll \
        -r:build/WindowsConnectivity.dll SinkInvokeProbe.cs
else
    echo "note: WindowsConnectivity.dll not found, skipping the interop probes (set GAME_LIBS=)"
fi

rm -f build/System.Core.dll
mono build/PatchSystemCore.exe "$SYSTEM_CORE" build/MyWhoosh.ComEventShim.dll build/System.Core.dll

echo
echo "built build/System.Core.dll + build/MyWhoosh.ComEventShim.dll"
echo "install with:  WINEPREFIX=<prefix> ./install.sh"
