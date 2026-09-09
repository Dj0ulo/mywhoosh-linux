#!/usr/bin/env bash
# Take over Bonjour's role inside one Wine prefix: its two COM classes, the
# service name the game gates on, and the .local lookup Wine cannot do.
#
#   WINEPREFIX=<prefix> ./install.sh            # take over
#   WINEPREFIX=<prefix> ./install.sh --restore   # give it all back
#
# The DLL is copied into the prefix's system32 and registered by bare name, so
# nothing outside the prefix is touched and no path conversion is needed.
#
# Artifacts are taken from build/.  With a checkout they are built on demand;
# in a release bundle they are already there and no compiler is needed.
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
SVC='Bonjour Service'
# Only a service we installed ourselves may be stopped or deleted on --restore.
SVC_MARKER="$WINEPREFIX/fakesensor-bonjour-stub"

if [ "$1" = "--restore" ]; then
    if [ -f "$SVC_MARKER" ]; then
        "$WINE" net stop "$SVC" >/dev/null 2>&1 || true
        "$WINE" sc delete "$SVC" >/dev/null 2>&1 || true
        rm -f "$SVC_MARKER" "$WINEPREFIX/drive_c/windows/bonjourstub.exe"
        echo "removed the '$SVC' stub"
    fi
    rm -f "$WINEPREFIX/dotlocal_shim.so"
    rm -f "$WINEPREFIX/drive_c/windows/system32/fakebonjour.dll"
    [ -f "$SAVED" ] || { echo "nothing saved in $SAVED" >&2; exit 1; }
    # Driven with reg, not regedit on the saved file: importing @="" leaves the
    # value pointing at us, and a prefix that never had Bonjour needs the key
    # gone rather than emptied.
    for clsid in "$SERVICE" "$MANAGER"; do
        prev=$(sed -n "/{${clsid#\{}/,/^$/s/^@=\"\(.*\)\"$/\1/p" "$SAVED" | head -1)
        prev=${prev//\\\\/\\}
        key="HKCR\\CLSID\\$clsid\\InprocServer32"
        if [ -n "$prev" ]; then
            "$WINE" reg add "$key" /ve /t REG_SZ /d "$prev" /f >/dev/null
            echo "  $clsid -> $prev"
        else
            "$WINE" reg delete "$key" /f >/dev/null 2>&1 || true
            echo "  $clsid -> (unregistered, as it was)"
        fi
    done
    echo "restored the previous registration recorded in $SAVED"
    exit 0
fi

need() {
    [ -f "build/$1" ] && return 0
    [ -x ./build.sh ] || { echo "missing build/$1 and no build.sh to make it" >&2; exit 1; }
    ./build.sh
    [ -f "build/$1" ] || { echo "build.sh did not produce build/$1" >&2; exit 1; }
}
need fakebonjour.dll
need bonjourstub.exe
need dotlocal_shim.so

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

# ------------------------------------------------------------ the service gate
# GetNetworkState() refuses to go on unless the SCM reports a service named
# exactly "Bonjour Service" Running.  Nothing behind the gate is ever asked of
# it -- discovery is answered in-process by fakebonjour.dll -- so a stub that
# only reports Running is enough, and it replaces installing Apple's
# mDNSResponder out of the game's SDK bundle.  Apple's own service, if this
# prefix has one, is left exactly where it is.
if "$WINE" sc query "$SVC" 2>/dev/null | tr -d '\r' | grep -q RUNNING; then
    echo "  '$SVC' is already RUNNING (leaving it alone)"
else
    cp -f build/bonjourstub.exe "$WINEPREFIX/drive_c/windows/bonjourstub.exe"
    # start= auto so the SCM brings it back by itself after a wineserver -k,
    # which is every second game launch.
    "$WINE" sc create "$SVC" binPath= 'C:\windows\bonjourstub.exe' \
            start= auto >/dev/null 2>&1 || true
    "$WINE" net start "$SVC" >/dev/null 2>&1 || true
    if "$WINE" sc query "$SVC" 2>/dev/null | tr -d '\r' | grep -q RUNNING; then
        : > "$SVC_MARKER"
        echo "  '$SVC' stub installed and RUNNING"
    else
        echo "  WARNING: could not start '$SVC' -- Direct Connect will stay gated" >&2
    fi
fi

# ------------------------------------------------------- the .local resolver
# The game resolves the sensor by the .local name discovery hands back, which
# Wine cannot look up.  Keep the shim inside the prefix so the launch env can
# point at it without knowing where this checkout is.
SO="$WINEPREFIX/dotlocal_shim.so"
cp -f build/dotlocal_shim.so "$SO"
# A prebuilt .so can be wrong for this machine (glibc, or a bundle built
# elsewhere); ld.so says so on any command, so ask it before trusting it.
if LD_PRELOAD="$SO" /bin/true 2>&1 | grep -q "$SO"; then
    if [ -f dotlocal_shim.c ] && command -v cc >/dev/null; then
        echo "  prebuilt dotlocal_shim.so will not load here, rebuilding it"
        cc -shared -fPIC -O2 -Wall -o "$SO" dotlocal_shim.c -ldl
    else
        echo "  WARNING: $SO will not load and cannot be rebuilt (need cc)" >&2
    fi
fi
echo "  installed $SO (LD_PRELOAD it at launch)"

echo "done -- run ./run.sh"
