#!/usr/bin/env bash
set -euo pipefail
directory="$(cd "$(dirname "$0")" && pwd)"
: "${WINE:?Lutris must supply the selected Wine executable}"
: "${WINEPREFIX:?Lutris must supply the game prefix}"
[[ $# -gt 0 ]] || { printf 'Usage: %s <game command> [arguments...]\n' "$0" >&2; exit 2; }
log="${MYWHOOSH_SHIM_LOG:-$directory/session.log}"
if ! env -u MONO_GAC_PREFIX -u MONO_PATH "$WINE" "$directory/SetupBonjour.exe" >> "$log" 2>&1; then
    printf 'Bonjour compatibility setup failed; the game was not started. See %s\n' "$log" >&2
    tail -n 20 "$log" >&2
    exit 1
fi
export MONO_GAC_PREFIX="Z:$directory/bonjour"
export MONO_PATH="Z:$directory${MONO_PATH:+;$MONO_PATH}"
exec "$@"