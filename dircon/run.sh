#!/usr/bin/env bash
# Run the Dircon probe inside the MyWhoosh Wine prefix.
#
#   ./run.sh [scan_seconds] [read_seconds]
#
# Env overrides: WINEPREFIX, WINE, GAME_LIBS
set -e
cd "$(dirname "$0")"

export WINEPREFIX="${WINEPREFIX:-$HOME/Games/mywhoosh}"
WINE="${WINE:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/bin/wine}"
[ -x "$WINE" ] || WINE=wine

[ -f build/TestDircon.exe ] || ./build.sh

export WINEDEBUG="${WINEDEBUG:-fixme-all,err-all}"
cd build
exec "$WINE" TestDircon.exe "${1:-20}" "${2:-10}"
