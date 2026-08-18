#!/usr/bin/env bash
# Run ILDump.exe under wine-mono inside the MyWhoosh prefix.
#
# Host Mono lacks System.ServiceProcess, which WindowsConnectivity references;
# resolving a method's local-variable signature then fails before any IL is read.
# wine-mono ships the full 4.5 BCL, so we disassemble there instead.
#
#   ./ildump.sh <Type.Name> [MethodName]
set -e
cd "$(dirname "$0")"

GAME_LIBS="${GAME_LIBS:-$HOME/Games/mywhoosh/drive_c/MyWhoosh/MyWhoosh/Content/Libraries/Win64}"
export WINEPREFIX="${WINEPREFIX:-$HOME/Games/mywhoosh}"
WINE="${WINE:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/bin/wine}"
[ -x "$WINE" ] || WINE=wine

[ -f ILDump.exe ] || mcs -out:ILDump.exe ILDump.cs
cp -f "$GAME_LIBS/WindowsConnectivity.dll" .

WINEDEBUG=-all "$WINE" ILDump.exe WindowsConnectivity.dll "$@" 2>/dev/null
