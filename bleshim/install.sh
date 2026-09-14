#!/usr/bin/env bash
# Put the shim where mono will find it, inside one Wine prefix.
#
#   WINEPREFIX=<prefix> ./install.sh
#   WINEPREFIX=<prefix> ./install.sh --verify
#   WINEPREFIX=<prefix> ./install.sh --restore
#   WINEPREFIX=<prefix> ./install.sh --close-gate
#
# The assemblies go in the prefix's own wine-mono tree, never in the game
# directory: MyWhoosh hashes WindowsConnectivity.dll and silently declines to
# load it when a byte near it changed, and nothing here needs a game file
# touched.  mono probes c:\windows\mono\mono-2.0\lib for a referenced assembly
# by simple name, which is this directory, and `Windows` is exactly the name the
# game's metadata asks for.
#
# --restore puts ../winmd's inert stubs back rather than deleting anything: with
# no assembly of that name the game does not start at all.
#
# --close-gate removes the `dev` branch's Bonjour leftovers, if this prefix has
# them.  See "the Bonjour gate" below for why they are the one thing that can
# make this branch need a COM server and a patched wine-mono.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to install into}"
# Exported explicitly: the gate checks below shell out to wine, which reads it
# from the environment rather than from this shell.
export WINEPREFIX
TARGET="$WINEPREFIX/drive_c/windows/mono/mono-2.0/lib"
SHIM="Windows.dll System.Runtime.WindowsRuntime.dll"

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }
[ -d "$TARGET" ] || { echo "no wine-mono in $WINEPREFIX (expected $TARGET)" >&2; exit 1; }

# ------------------------------------------------------------ the Bonjour gate
# MyWhoosh only ever reaches Apple Bonjour's COM objects -- and, through them,
# the System.Runtime.InteropServices.ComAwareEventInfo that stock wine-mono
# leaves as NotImplementedException stubs -- when the SCM reports a service
# named exactly "Bonjour Service" in state Running.  Measured in the game's own
# IL: OpenBikeManager::GetNetworkState and WahooProgram::GetNetworkState are
# that test, each constructor stores it in isBonjourEnabled, and both call sites
# branch over their initialiser when it is false --
#
#   OpenBikeManager::OBM_Initialize   IL_0001 ldfld isBonjourEnabled
#                                     IL_0006 brfalse IL_0100 (the ret)
#   WahooProgram::.ctor               IL_0063 ldfld isBonjourEnabled
#                                     IL_0068 brfalse.s IL_0070 (past WFTNP_Init)
#
# Those two are the whole of it: nothing else in WindowsConnectivity.dll calls
# Marshal::GetTypeFromCLSID or constructs a ComAwareEventInfo.  So with the gate
# shut this branch needs no Bonjour COM server and no patched wine-mono, and
# with it open it needs both -- the COM objects must exist or OBM_Initialize
# dies with a COMException before Bluetooth is ever reached.
#
# A prefix that has run the `dev` branch has the gate propped open on purpose
# (fakesensor/install.sh installs a "Bonjour Service" stub, because Direct
# Connect needs everything behind it).  That is the one thing that can make this
# branch appear to require Bonjour.  A prefix that never ran `dev` has no such
# service and nothing to do here.
WINE="${WINE:-$HOME/.local/share/lutris/runners/wine/GE-Proton10-4/bin/wine64}"
[ -x "$WINE" ] || WINE="${WINE%64}"
[ -x "$WINE" ] || WINE=wine64
command -v "$WINE" >/dev/null 2>&1 || WINE=wine

SVC='Bonjour Service'
SERVICE_CLSID='{24CD4DE9-FF84-4701-9DC1-9B69E0D1090A}'   # DNSSDService
MANAGER_CLSID='{BEEB932A-8D4A-4619-AEFE-A836F988B221}'   # DNSSDEventManager
# fakesensor/install.sh drops this only for a service it created itself, so it
# is also our licence to remove one.  Apple's real Bonjour has no marker and is
# never touched.
SVC_MARKER="$WINEPREFIX/fakesensor-bonjour-stub"

gate_is_open() {
    WINEDEBUG=-all "$WINE" sc query "$SVC" 2>/dev/null | tr -d '\r' | grep -q RUNNING
}

report_gate() {
    if gate_is_open; then
        echo "  gate     OPEN -- '$SVC' is RUNNING in this prefix"
        echo "           the game will take the Bonjour path and then need a COM"
        echo "           server for $MANAGER_CLSID and a patched wine-mono."
        if [ -f "$SVC_MARKER" ]; then
            echo "           it is the dev branch's stub; ./install.sh --close-gate removes it"
        else
            echo "           NOT installed by us -- leaving it alone.  Stop it by hand if"
            echo "           this prefix is meant to be Bluetooth-only."
        fi
    else
        echo "  gate     shut -- no '$SVC' running, so the game skips Bonjour entirely"
    fi
}

case "$1" in
--verify)
    for dll in $SHIM; do
        if [ ! -f "$TARGET/$dll" ]; then
            echo "  MISSING  $TARGET/$dll"
            continue
        fi
        if [ -f "build/$dll" ] && cmp -s "build/$dll" "$TARGET/$dll"; then
            echo "  ok       $TARGET/$dll (this build)"
        elif [ -f "../winmd/build/$dll" ] && cmp -s "../winmd/build/$dll" "$TARGET/$dll"; then
            echo "  stub     $TARGET/$dll (../winmd -- the game will find no devices)"
        else
            echo "  other    $TARGET/$dll (neither this build nor ../winmd)"
        fi
    done
    report_gate
    exit 0
    ;;
--close-gate)
    if ! gate_is_open; then
        echo "nothing to do -- '$SVC' is not running in this prefix"
        report_gate
        exit 0
    fi
    if [ ! -f "$SVC_MARKER" ]; then
        cat >&2 <<EOF
'$SVC' is running here but $SVC_MARKER says we did not install it.
That is most likely Apple's own Bonjour, and this script will not touch it.
Stop it yourself if this prefix is meant to be Bluetooth-only:

    WINEPREFIX=$WINEPREFIX $WINE net stop "$SVC"
    WINEPREFIX=$WINEPREFIX $WINE sc delete "$SVC"
EOF
        exit 1
    fi
    export WINEPREFIX WINEDEBUG="${WINEDEBUG:-fixme-all,err-all}"
    "$WINE" net stop "$SVC" >/dev/null 2>&1 || true
    "$WINE" sc delete "$SVC" >/dev/null 2>&1 || true
    rm -f "$SVC_MARKER" "$WINEPREFIX/drive_c/windows/bonjourstub.exe"
    echo "  removed  the dev branch's '$SVC' stub"
    # The COM registration goes with it.  Only ours: a CLSID pointing anywhere
    # but fakebonjour.dll belongs to a real Bonjour and is left as it is.
    for clsid in "$SERVICE_CLSID" "$MANAGER_CLSID"; do
        cur=$("$WINE" reg query "HKCR\\CLSID\\$clsid\\InprocServer32" 2>/dev/null \
              | sed -n 's/.*REG_SZ[[:space:]]*//p' | head -1 | tr -d '\r')
        case "$cur" in
        fakebonjour.dll)
            "$WINE" reg delete "HKCR\\CLSID\\$clsid" /f >/dev/null 2>&1 || true
            echo "  removed  $clsid (was fakebonjour.dll)"
            ;;
        "")
            ;;
        *)
            echo "  kept     $clsid -> $cur (not ours)"
            ;;
        esac
    done
    rm -f "$WINEPREFIX/drive_c/windows/system32/fakebonjour.dll"
    rm -f "$WINEPREFIX/drive_c/fakesensor-table" "$WINEPREFIX/dotlocal_shim.so"
    cat <<EOF
done -- the game now skips Bonjour, so no COM server is needed here.

dev's patched wine-mono is not needed either once the gate is shut, since
ComAwareEventInfo is only ever reached behind it.  Dropping it is a separate
step, and it takes this shim with it -- the assemblies live in the same tree:

    rm -rf "$WINEPREFIX/drive_c/windows/mono/mono-2.0"   # back to the runner's copy
    WINEPREFIX="$WINEPREFIX" ./install.sh                # put the shim back
    WINEPREFIX="$WINEPREFIX" ../exportshim/install.sh
EOF
    exit 0
    ;;
--restore)
    [ -d ../winmd/build ] || { echo "../winmd is not built; run ../winmd/build.sh" >&2; exit 1; }
    for dll in $SHIM; do
        cp -f "../winmd/build/$dll" "$TARGET/$dll"
        echo "  restored $TARGET/$dll (inert stub)"
    done
    echo "done -- the game starts and reports no Bluetooth devices"
    exit 0
    ;;
esac

for dll in $SHIM; do
    [ -f "build/$dll" ] && continue
    # A release bundle ships build/ prepopulated and has no build.sh at all.
    [ -x ./build.sh ] || { echo "missing build/$dll and no build.sh to make it" >&2; exit 1; }
    ./build.sh
    break
done

for dll in $SHIM; do
    cp -f "build/$dll" "$TARGET/$dll"
    echo "  installed $TARGET/$dll"
done

report_gate

cat <<'EOF'
done.  Two things this prefix still needs:

  * ./blehelper.py running before the game, or it sees no Bluetooth at all
  * ../exportshim installed, or the game's device list crashes on its first poll

and WindowsConnectivity.dll must be left unpatched, or the game will not load it.
EOF
