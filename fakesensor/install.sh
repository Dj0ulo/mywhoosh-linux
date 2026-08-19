#!/usr/bin/env bash
# Point Bonjour's two CLSIDs at build/fakebonjour.dll inside one Wine prefix.
#
#   WINEPREFIX=<prefix> ./install.sh            # take over
#   WINEPREFIX=<prefix> ./install.sh --restore   # give the CLSIDs back
#
# The DLL is copied into the prefix's system32 and registered by bare name, so
# nothing outside the prefix is touched and no path conversion is needed.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to patch}"
# The 64-bit loader on purpose: `wine reg` in a wow64 build edits the 32-bit
# registry view (HKCR\Wow6432Node), and the game is 64-bit, so the keys written
# there are simply never read.
WINE="${WINE:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/bin/wine64}"
[ -x "$WINE" ] || WINE="${WINE%64}"
[ -x "$WINE" ] || WINE=wine64
command -v "$WINE" >/dev/null 2>&1 || WINE=wine
export WINEPREFIX WINEDEBUG="${WINEDEBUG:-fixme-all,err-all}"

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }

SERVICE='{24CD4DE9-FF84-4701-9DC1-9B69E0D1090A}'   # DNSSDService
MANAGER='{BEEB932A-8D4A-4619-AEFE-A836F988B221}'   # DNSSDEventManager
SAVED="$WINEPREFIX/fakesensor-previous-clsids.reg"

if [ "$1" = "--restore" ]; then
    [ -f "$SAVED" ] || { echo "nothing saved in $SAVED" >&2; exit 1; }
    "$WINE" regedit "$(basename "$SAVED")" 2>/dev/null || \
        "$WINE" regedit "Z:${SAVED//\//\\}"
    echo "restored the previous registration from $SAVED"
    exit 0
fi

[ -f build/fakebonjour.dll ] || ./build.sh
cp -f build/fakebonjour.dll "$WINEPREFIX/drive_c/windows/system32/fakebonjour.dll"

# Keep whatever was there (Apple's dnssdX.dll, usually) so --restore can undo this.
if [ ! -f "$SAVED" ]; then
    {
        echo "REGEDIT4"
        echo
        for clsid in "$SERVICE" "$MANAGER"; do
            prev=$("$WINE" reg query "HKCR\\CLSID\\$clsid\\InprocServer32" 2>/dev/null \
                   | sed -n 's/.*REG_SZ[[:space:]]*//p' | head -1 | tr -d '\r')
            echo "[HKEY_CLASSES_ROOT\\CLSID\\$clsid\\InprocServer32]"
            echo "@=\"${prev//\\/\\\\}\""
            echo
        done
    } > "$SAVED"
    echo "previous registration saved to $SAVED"
fi

for clsid in "$SERVICE" "$MANAGER"; do
    "$WINE" reg add "HKCR\\CLSID\\$clsid\\InprocServer32" /ve /t REG_SZ \
            /d "fakebonjour.dll" /f >/dev/null
    # Apartment, like Bonjour's own registration: the objects are only ever
    # touched from the game's STA thread and nothing here is thread-safe.
    "$WINE" reg add "HKCR\\CLSID\\$clsid\\InprocServer32" /v ThreadingModel /t REG_SZ \
            /d "Apartment" /f >/dev/null
    echo "  $clsid -> fakebonjour.dll"
done

echo "done -- run ./run.sh"
