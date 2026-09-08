#!/usr/bin/env bash
# Install the whole in-app connectivity stack into one Wine prefix.
#
#   ./setup-connectivity.sh <prefix>
#   ./setup-connectivity.sh --restore <prefix>
#
# This is what replaces ../patch/patch_windows_connectivity_dll.py.  That patch
# made the game start by making it stop loading WindowsConnectivity.dll at all --
# MyWhoosh hashes that file and silently skips the load if a single byte differs
# (../dircon/README.md) -- so with it applied nothing here is ever reached.
# Every component below therefore lives *outside* the game tree and leaves every
# game file byte-identical.
#
# Order is not cosmetic:
#
#   1. winemono   copies a fresh wine-mono into the prefix, so it must go first:
#                 its install.sh rm -rf's c:\windows\mono\mono-2.0, which would
#                 take 2 and 3 with it.
#   2. winmd      stub Windows / System.Runtime.WindowsRuntime, into that tree.
#   3. exportshim MyWhooshShim.dll, into that tree.
#   4. fakesensor takes over Bonjour's two CLSIDs, and needs a service named
#                 "Bonjour Service" to already exist for the game's own gate.
set -e
cd "$(dirname "$0")/.."
REPO="$PWD"

RESTORE=0
if [ "$1" = "--restore" ]; then RESTORE=1; shift; fi

PREFIX="${1:-$WINEPREFIX}"
[ -n "$PREFIX" ] || { echo "usage: $0 [--restore] <prefix>" >&2; exit 1; }
PREFIX="$(cd "$PREFIX" && pwd)"
[ -d "$PREFIX/drive_c" ] || { echo "no Wine prefix at $PREFIX" >&2; exit 1; }
export WINEPREFIX="$PREFIX"

WINE="${WINE:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/bin/wine64}"
[ -x "$WINE" ] || WINE="${WINE%64}"
[ -x "$WINE" ] || WINE=wine
export WINEFSYNC="${WINEFSYNC:-1}" WINEESYNC="${WINEESYNC:-1}"

GAME_LIBS="$PREFIX/drive_c/MyWhoosh/MyWhoosh/Binaries/Win64"
export GAME_LIBS

say() { printf '\n== %s\n' "$*"; }

if [ "$RESTORE" = 1 ]; then
    say "restoring $PREFIX"
    WINEPREFIX="$PREFIX" "$REPO/fakesensor/install.sh" --restore || true
    WINEPREFIX="$PREFIX" "$REPO/exportshim/install.sh" --restore || true
    WINEPREFIX="$PREFIX" "$REPO/winmd/install.sh" --restore || true
    echo
    echo "left in place: the patched wine-mono in the prefix (harmless on its own)."
    echo "the game will crash at launch until winmd/ is reinstalled or the DLL is patched."
    exit 0
fi

for tool in mcs python3; do
    command -v "$tool" >/dev/null || { echo "$tool not found (mono-devel)" >&2; exit 1; }
done
command -v x86_64-w64-mingw32-gcc >/dev/null || {
    echo "x86_64-w64-mingw32-gcc not found (mingw-w64) -- needed for fakesensor" >&2; exit 1; }

[ -f "$GAME_LIBS/WindowsConnectivity.dll" ] || {
    echo "no WindowsConnectivity.dll under $GAME_LIBS -- is the game installed?" >&2; exit 1; }

# ---------------------------------------------------------------- 1. wine-mono
# ComAwareEventInfo is a throw-only stub in wine-mono, and it is how the game
# wires its four Bonjour event handlers.  Patched per prefix: mscoree probes
# c:\windows\mono\mono-2.0 before the runner's shared tree.
say "1/4  patched wine-mono (ComAwareEventInfo)"
"$REPO/winemono/build.sh"
"$REPO/winemono/install.sh"

# ------------------------------------------------------------------- 2. winmd
# The game's DLL references the Windows winmd, which does not exist under Wine,
# so BluetoothProgram cannot be laid out and the process dies at launch.
say "2/4  stub winmd assemblies"
"$REPO/winmd/build.sh"
"$REPO/winmd/install.sh"

# -------------------------------------------------------------- 3. exportshim
# The four device-list exports take a byref array, which wine-mono's marshaller
# refuses; without this the first poll is a fatal MarshalDirectiveException.
say "3/4  export shim (byref-array device lists)"
"$REPO/exportshim/build.sh"
"$REPO/exportshim/install.sh"

# --------------------------------------------------------------- 4. fakesensor
say "4/4  Bonjour replacement"
"$REPO/fakesensor/build.sh"

# The game checks for a service named exactly "Bonjour Service" in state Running
# before it does anything Dircon.  Only the *name and state* matter -- our COM
# server answers the actual discovery -- but the gate is real, so install Apple's
# service from the game's own bundle if the prefix has none.
if "$WINE" sc query "Bonjour Service" 2>/dev/null | grep -q RUNNING; then
    echo "  'Bonjour Service' is already RUNNING"
else
    SETUP="$PREFIX/drive_c/MyWhoosh/MyWhoosh/Content/Libraries/Win64/Dircon/bonjoursdksetup.exe"
    if [ -f "$SETUP" ] && command -v 7z >/dev/null; then
        echo "  installing Bonjour from the game's own SDK bundle"
        BJ="$PREFIX/bonjour-sdk"
        rm -rf "$BJ"; mkdir -p "$BJ"
        # The wrapper's own /quiet install fails with MSI 1603; the MSI inside works.
        7z x -o"$BJ" -y "$SETUP" >/dev/null
        MSI="$(find "$BJ" -iname 'Bonjour64.msi' | head -1)"
        [ -n "$MSI" ] || { echo "  no Bonjour64.msi inside the bundle" >&2; exit 1; }
        "$WINE" msiexec /i "$(printf 'Z:%s' "${MSI//\//\\}")" /quiet /norestart || true
        "$WINE" net start "Bonjour Service" >/dev/null 2>&1 || true
        if "$WINE" sc query "Bonjour Service" 2>/dev/null | grep -q RUNNING; then
            echo "  'Bonjour Service' is RUNNING"
        else
            echo "  WARNING: could not start 'Bonjour Service' -- Direct Connect will stay gated" >&2
        fi
    else
        echo "  WARNING: no 'Bonjour Service' and no way to install it" >&2
        echo "           (need $SETUP and 7z) -- Direct Connect will stay gated" >&2
    fi
fi

"$REPO/fakesensor/install.sh"

# The game resolves the sensor by a .local name, which Wine cannot look up.  Keep
# the shim inside the prefix so the Lutris launch env can point at it without
# knowing where this repo lives.
cp -f "$REPO/fakesensor/build/dotlocal_shim.so" "$PREFIX/dotlocal_shim.so"
echo "  installed $PREFIX/dotlocal_shim.so (LD_PRELOAD it at launch)"

say "done"
cat <<TXT
WindowsConnectivity.dll must stay pristine -- do NOT run
patch/patch_windows_connectivity_dll.py on this prefix; the game hashes that
file and stops loading it if anything changed.

At launch the prefix needs:
  LD_PRELOAD=$PREFIX/dotlocal_shim.so
and, for a hard-coded test sensor rather than real hardware:
  FAKESENSOR_NAME / FAKESENSOR_POWER / FAKESENSOR_BPM

For a real trainer, run this on the Linux side and set FAKESENSOR_EXTERNAL=1:
  fakesensor/blebridge.py --list
  fakesensor/blebridge.py --mac <address>
TXT
