#!/usr/bin/env bash
# Drive the Dircon harness against the fake sensor.
#
#   ./run.sh [scan_seconds]
#
# Needs, in the prefix: the patched wine-mono (../winemono/install.sh) and the
# fake COM server (./install.sh).  The LD_PRELOAD shim is what makes
# "<name>.local." resolvable; see dotlocal_shim.c.
set -e
cd "$(dirname "$0")"

export WINEPREFIX="${WINEPREFIX:-$HOME/Games/dircon-test}"
[ -f build/dotlocal_shim.so ] || ./build.sh

export LD_PRELOAD="$PWD/build/dotlocal_shim.so${LD_PRELOAD:+:$LD_PRELOAD}"
export FAKESENSOR_NAME="${FAKESENSOR_NAME:-FakeTrainer}"
export FAKESENSOR_POWER="${FAKESENSOR_POWER:-150}"
export FAKESENSOR_BPM="${FAKESENSOR_BPM:-75}"

exec ../dircon/run.sh "$@"
