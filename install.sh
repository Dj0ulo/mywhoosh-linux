#!/usr/bin/env bash
# Install the in-app connectivity stack into one Wine prefix.
#
#   ./install.sh [<prefix>]              # prefix defaults to $WINEPREFIX
#   ./install.sh --restore [<prefix>]
#   ./install.sh --verify  [<prefix>]
#
# This is what replaces patch/patch_windows_connectivity_dll.py.  That patch
# made the game start by making it stop loading WindowsConnectivity.dll at all --
# MyWhoosh hashes that file and silently skips the load if a single byte differs
# (dircon/README.md) -- so with it applied nothing here is ever reached.  Every
# component below therefore lives *outside* the game tree and leaves every game
# file byte-identical.  Do not run the patcher on a prefix set up by this script.
#
# Runs the same way from a git checkout (building what it needs) or from a
# release bundle (build/ prepopulated, no compiler required).
#
# Order is not cosmetic:
#
#   1. winemono   copies a fresh wine-mono into the prefix, so it must go first:
#                 it rm -rf's c:\windows\mono\mono-2.0, which would take 2 and 3
#                 with it.
#   2. winmd      stub Windows / System.Runtime.WindowsRuntime, into that tree.
#   3. exportshim MyWhooshShim.dll, into that tree.
#   4. fakesensor Bonjour's two COM classes, the service name the game gates on,
#                 and the .local resolver shim.
set -e
cd "$(dirname "$0")"
REPO="$PWD"
LUTRIS_WINE="$HOME/.local/share/lutris/runners/wine"

MODE=install
case "$1" in
    --restore) MODE=restore; shift ;;
    --verify)  MODE=verify;  shift ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \?//'; exit 0 ;;
    --*) echo "unknown option: $1" >&2; exit 1 ;;
esac

PREFIX="${1:-$WINEPREFIX}"
[ -n "$PREFIX" ] || { echo "usage: $0 [--restore|--verify] <prefix>" >&2; exit 1; }
[ -d "$PREFIX/drive_c" ] || { echo "no Wine prefix at $PREFIX" >&2; exit 1; }
PREFIX="$(cd "$PREFIX" && pwd)"
export WINEPREFIX="$PREFIX"

newest() { ls -d "$@" 2>/dev/null | sort -V | tail -1; }

# ------------------------------------------------------------------- the runner
# Whatever Lutris will launch the game with is what has to be patched, so ask
# Lutris: the game's own config if one points at this prefix, else the default.
if [ -z "$WINE" ]; then
    ver=$(grep -rl "prefix: $PREFIX\$" "$HOME/.config/lutris/games/"*.yml 2>/dev/null \
          | head -1 | xargs -r sed -n 's/^ *version: *\(GE-\|wine-\|lutris-\|proton\)/\1/p' | head -1)
    [ -n "$ver" ] || ver=$(sed -n 's/^ *version: *//p' "$HOME/.config/lutris/runners/wine.yml" 2>/dev/null | head -1)
    if [ -n "$ver" ] && [ -x "$LUTRIS_WINE/$ver/bin/wine64" ]; then
        WINE="$LUTRIS_WINE/$ver/bin/wine64"
    else
        WINE="$(newest "$LUTRIS_WINE"/*/bin/wine64)"
    fi
fi
[ -x "$WINE" ] || WINE="${WINE%64}"
[ -x "$WINE" ] || WINE="$(command -v wine64 || command -v wine || true)"
[ -x "$WINE" ] || { echo "no wine found (set WINE=)" >&2; exit 1; }
export WINE
export WINEFSYNC="${WINEFSYNC:-1}" WINEESYNC="${WINEESYNC:-1}"

# The runtime that goes into the prefix comes from the same runner.
if [ -z "$WINE_MONO" ]; then
    runner="$(cd "$(dirname "$WINE")/.." && pwd)"
    WINE_MONO="$(newest "$runner"/share/wine/mono/wine-mono-* /usr/share/wine/mono/wine-mono-*)"
fi
[ -d "$WINE_MONO" ] || { echo "no wine-mono tree found (set WINE_MONO=)" >&2; exit 1; }
export WINE_MONO

GAME_LIBS="${GAME_LIBS:-$PREFIX/drive_c/MyWhoosh/MyWhoosh/Binaries/Win64}"
export GAME_LIBS

say() { printf '\n== %s\n' "$*"; }

# ------------------------------------------------------------------- restore
if [ "$MODE" = restore ]; then
    say "restoring $PREFIX"
    "$REPO/fakesensor/install.sh" --restore || true
    "$REPO/exportshim/install.sh" --restore || true
    "$REPO/winmd/install.sh" --restore || true
    echo
    echo "left in place: the patched wine-mono in the prefix (harmless on its own)."
    echo "the game will crash at launch until winmd/ is reinstalled or the DLL is patched."
    exit 0
fi

# -------------------------------------------------------------------- verify
if [ "$MODE" = verify ]; then
    MONO="$PREFIX/drive_c/windows/mono/mono-2.0/lib"
    fail=0
    check() { if [ -e "$2" ]; then echo "  ok      $1"; else echo "  MISSING $1"; fail=1; fi; }
    say "checking $PREFIX"
    check "patched wine-mono"        "$MONO/mono/4.5/MyWhoosh.ComEventShim.dll"
    check "winmd stubs"              "$MONO/Windows.dll"
    check "winmd stubs (runtime)"    "$MONO/System.Runtime.WindowsRuntime.dll"
    check "export shim"              "$MONO/MyWhooshShim.dll"
    check "fake Bonjour COM server"  "$PREFIX/drive_c/windows/system32/fakebonjour.dll"
    check ".local resolver"          "$PREFIX/dotlocal_shim.so"
    check "pristine game DLL"        "$GAME_LIBS/WindowsConnectivity.dll"
    if "$WINE" sc query "Bonjour Service" 2>/dev/null | tr -d '\r' | grep -q RUNNING
        then echo "  ok      'Bonjour Service' RUNNING"
        else echo "  MISSING 'Bonjour Service' RUNNING"; fail=1; fi
    clsid=$("$WINE" reg query 'HKCR\CLSID\{24CD4DE9-FF84-4701-9DC1-9B69E0D1090A}\InprocServer32' \
            2>/dev/null | tr -d '\r' | sed -n 's/.*REG_SZ[[:space:]]*//p' | head -1)
    case "$clsid" in
        fakebonjour.dll) echo "  ok      DNSSDService -> fakebonjour.dll" ;;
        *) echo "  MISSING DNSSDService -> fakebonjour.dll (is: ${clsid:-unset})"; fail=1 ;;
    esac
    echo
    [ "$fail" = 0 ] && echo "the stack is installed" || echo "incomplete -- run $0 $PREFIX" >&2
    exit "$fail"
fi

# ------------------------------------------------------------------- install
[ -f "$GAME_LIBS/WindowsConnectivity.dll" ] || {
    echo "no WindowsConnectivity.dll under $GAME_LIBS -- is the game installed?" >&2
    echo "(set GAME_LIBS= if it lives somewhere else)" >&2; exit 1; }

# Only what is actually missing has to be built, so only then is a toolchain a
# requirement: a release bundle needs none of this.
missing=""
[ -f winemono/build/System.Core.dll ] || [ -f winemono/build/PatchSystemCore.exe ] || missing="$missing mcs"
[ -f winmd/build/Windows.dll ] || missing="$missing mcs python3"
[ -f exportshim/build/MyWhooshShim.dll ] || missing="$missing mcs"
[ -f fakesensor/build/fakebonjour.dll ] || missing="$missing x86_64-w64-mingw32-gcc"
[ -f fakesensor/build/dotlocal_shim.so ] || missing="$missing cc"
for tool in $(echo "$missing" | tr ' ' '\n' | sort -u); do
    command -v "$tool" >/dev/null || {
        echo "$tool not found, and something needs building with it" >&2
        echo "(mcs is in mono-devel, x86_64-w64-mingw32-gcc in mingw-w64)" >&2
        exit 1; }
done

echo "prefix:    $PREFIX"
echo "wine:      $WINE"
echo "wine-mono: $(basename "$WINE_MONO")"
[ -f VERSION ] && echo "version:   $(cat VERSION)"

# ComAwareEventInfo is a throw-only stub in wine-mono, and it is how the game
# wires its four Bonjour event handlers.  Patched per prefix: mscoree probes
# c:\windows\mono\mono-2.0 before the runner's shared tree.
say "1/4  patched wine-mono (ComAwareEventInfo)"
"$REPO/winemono/install.sh"

# The game's DLL references the Windows winmd, which does not exist under Wine,
# so BluetoothProgram cannot be laid out and the process dies at launch.
say "2/4  stub winmd assemblies"
"$REPO/winmd/install.sh"

# The four device-list exports take a byref array, which wine-mono's marshaller
# refuses; without this the first poll is a fatal MarshalDirectiveException.
say "3/4  export shim (byref-array device lists)"
"$REPO/exportshim/install.sh"

# Bonjour's two COM classes, the "Bonjour Service" name GetNetworkState() gates
# on, and the .local resolver.  No part of Apple's Bonjour is needed.
say "4/4  Bonjour replacement"
"$REPO/fakesensor/install.sh"

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

Check what landed at any time with:
  $0 --verify $PREFIX
TXT
