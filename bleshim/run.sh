#!/usr/bin/env bash
# Launch MyWhoosh with the BLE shim, and the helper it needs, in one command.
#
#   ./run.sh                       # the test prefix below
#   WINEPREFIX=<prefix> ./run.sh
#   ./run.sh --helper-only         # just the Linux half, to watch it work
#
# What this arranges, and why each part is here:
#
#   * blehelper.py, started first and stopped on exit.  Without it the game's
#     scan finds nothing and its Bluetooth reads as off -- there is no error to
#     see, which is exactly why this script starts it rather than reminding you.
#   * MYWHOOSH_SHIM_LOG, so the shim, the export shim and the helper all end up
#     in one file per run instead of in a window that has scrolled away.
#   * Lutris' own Wine.  Nothing here needs a particular build (there is no
#     LD_PRELOAD and no Bonjour any more), but the game does.
set -e
cd "$(dirname "$0")"

export WINEPREFIX="${WINEPREFIX:-$HOME/Games/mywhoosh-6.1.2-no-patch}"
GAME="${GAME:-$WINEPREFIX/drive_c/MyWhoosh/MyWhoosh.exe}"
WINE="${WINE:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/bin/wine}"
[ -x "$WINE" ] || WINE=wine

LOG="${MYWHOOSH_SHIM_LOG:-/tmp/bleshim-$(date +%H%M%S).log}"
export MYWHOOSH_SHIM_LOG="$LOG"

if pgrep -f "blehelper.py" > /dev/null; then
    echo "helper already running"
    HELPER=
else
    ./blehelper.py -v >> "$LOG" 2>&1 &
    HELPER=$!
    trap 'kill $HELPER 2>/dev/null' EXIT
    sleep 1
fi

echo "log: $LOG"
[ "$1" = "--helper-only" ] && { wait $HELPER; exit 0; }

[ -f "$GAME" ] || { echo "no game at $GAME (set GAME=)" >&2; exit 1; }
echo "prefix: $WINEPREFIX"
echo "watch it with:  tail -f $LOG"

cd "$(dirname "$GAME")"
# Through tee, so the export shim -- which is native code inside the game and
# prints to stderr -- ends up in the same file as the shim and the helper.
WINEDEBUG="${WINEDEBUG:--all}" "$WINE" "$GAME" "${@:2}" 2>&1 | tee -a "$LOG"
