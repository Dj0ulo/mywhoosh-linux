#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${WINE:?Set WINE to the selected Wine executable}"
: "${WINEPREFIX:?Set WINEPREFIX to an existing prefix with Bonjour installed}"
[[ -d "$WINEPREFIX/drive_c/windows/system32" && -d "$WINEPREFIX/dosdevices" ]] || {
	printf 'The test requires an existing prefix.\n' >&2
	exit 1
}
bash build.sh
mcs -r:build/BonjourCompat.dll -r:System.Windows.Forms -out:build/TestComWrapper.exe tests/TestComWrapper.cs
env -u MONO_GAC_PREFIX -u MONO_PATH "$WINE" "$PWD/build/SetupBonjour.exe"
env -u MONO_GAC_PREFIX -u MONO_PATH "$WINE" "$PWD/build/SetupBonjour.exe" --prepare-only
export MONO_GAC_PREFIX="Z:$PWD/build/bonjour"
export MONO_PATH="Z:$PWD/build${MYWHOOSH_TEST_SHIM_DIR:+;Z:$MYWHOOSH_TEST_SHIM_DIR}"
"$WINE" "$PWD/build/SetupBonjour.exe" --verify
"$WINE" "$PWD/build/TestComWrapper.exe" "$@"