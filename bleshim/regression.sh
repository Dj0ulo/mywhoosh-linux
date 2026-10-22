#!/bin/bash
# Regression runner for bleshim: starts fakehelper tuned for the requested
# scenario, runs TestBle, cleans up by PID.  Kept as a file so process
# matching never has to guess which command line a pattern belongs to.
#
#   ./regression.sh                    # --stub: full fake walk, 59 checks
#   ./regression.sh --stub --slow-radios
#                                      # radios bound test (helper answers in 9s)
#   ./regression.sh --thin             # the same walk with the host's mono;
#                                      # ./winetest.sh repeats it under the
#                                      # prefix's wine-mono, where it matters
set -u
cd "$(dirname "$0")"

MONO="${MONO:-mono}"
PORT="${MYWHOOSH_TEST_PORT:-27092}"
MODE="${1:---stub}"
EXTRA="${2:-}"

case "$MODE $EXTRA" in
*--slow-radios) HELPER_SLOW="--slow radios:9000" ;;
*)              HELPER_SLOW="--slow connect:6000 --slow read:350" ;;
esac

ARGS="$MODE"
[ -n "$EXTRA" ] && ARGS="$ARGS $EXTRA"

python3 fakehelper.py --port "$PORT" $HELPER_SLOW >/tmp/fakeserve-$PORT.log 2>&1 &
FAKEPID=$!
trap 'kill $FAKEPID 2>/dev/null' EXIT
for i in $(seq 1 30); do
    if (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then exec 3<&- 3>&-; break; fi
    sleep 0.1
done

MYWHOOSH_BLE_PORT="$PORT" "$MONO" build/TestBle.exe $ARGS
exit $?
