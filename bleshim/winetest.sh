#!/bin/bash
# The same thin scenario, but under the prefix's wine-mono -- where the
# shim's Task.Factory.StartNew and Task.Wait actually have to work.
#
#   ./winetest.sh
#   WINEPREFIX=<other prefix> ./winetest.sh
#   GE=<GE build>/files ./winetest.sh
#
# Starts fakehelper here on the Linux side, then runs TestBle --thin with
# the game's own runtime: Proton wine, the real prefix, MONO_PATH set
# the way Lutris sets it for the game.  Nothing else in the prefix is
# touched; no hardware is needed (the fake devices are in fakehelper).
set -u
cd "$(dirname "$0")"

export WINEPREFIX="${WINEPREFIX:-$HOME/Games/mywhoosh}"

# Any recent GE-Proton build will do; the newest one wins.  All of this
# only points Lutris at wine, the same way run.sh points it at Lutris'
# own runner.
GE="${GE:-$(ls -d "$HOME"/.local/share/Steam/compatibilitytools.d/GE-Proton*/files 2>/dev/null | sort -V | tail -1)}"
[ -n "$GE" ] || { echo "no GE-Proton under ~/.local/share/Steam/compatibilitytools.d -- set GE=<build>/files" >&2; exit 1; }

PORT="${MYWHOOSH_TEST_PORT:-27092}"

export PATH="$GE/bin:/usr/bin:/bin"
export LD_LIBRARY_PATH="$GE/lib:$GE/lib64:/usr/lib/x86_64-linux-gnu"
export WINEDLLPATH="$GE/lib64/wine:$GE/lib/wine"
export WINEDEBUG=-all
# The assemblies under test ride load the same way they do for the game:
# Lutris pins MONO_PATH to a Z: path, wine-mono's loader wants a Windows
# form, and the host's unix form gets rewritten to a nonsense folder inside
# the mapped drive.
export MONO_PATH="Z:$PWD/build"

python3 fakehelper.py --port "$PORT" --slow connect:3000 >/tmp/fakeserve-$PORT.log 2>&1 &
FAKEPID=$!
trap 'kill $FAKEPID 2>/dev/null' EXIT
for i in $(seq 1 30); do
    if (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then exec 3<&- 3>&-; break; fi
    sleep 0.1
done

MYWHOOSH_BLE_PORT="$PORT" wine build/TestBle.exe --thin
exit $?
