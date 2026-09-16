#!/usr/bin/env bash
# Put the shim where mono will find it, inside one Wine prefix.
#
#   WINEPREFIX=<prefix> ./install.sh
#   WINEPREFIX=<prefix> ./install.sh --verify
#   WINEPREFIX=<prefix> ./install.sh --restore
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
# Both install and --verify report the Bonjour gate described below, because a
# prefix with a Bonjour service in it is the one thing that stops this working.
set -e
cd "$(dirname "$0")"

WINEPREFIX="${WINEPREFIX:?set WINEPREFIX to the prefix to install into}"
# Exported explicitly: the gate check below shells out to wine, which reads it
# from the environment rather than from this shell.
export WINEPREFIX
TARGET="$WINEPREFIX/drive_c/windows/mono/mono-2.0/lib"
SHIM="Windows.dll System.Runtime.WindowsRuntime.dll"

[ -d "$WINEPREFIX/drive_c" ] || { echo "no prefix at $WINEPREFIX" >&2; exit 1; }
[ -d "$TARGET" ] || { echo "no wine-mono in $WINEPREFIX (expected $TARGET)" >&2; exit 1; }

# ------------------------------------------------------------ the Bonjour gate
# MyWhoosh only ever reaches Apple Bonjour's COM objects -- and, through them,
# the System.Runtime.InteropServices.ComAwareEventInfo that wine-mono leaves as
# NotImplementedException stubs -- when the SCM reports a service named exactly
# "Bonjour Service" in state Running.  Read out of the game's own IL:
# OpenBikeManager::GetNetworkState and WahooProgram::GetNetworkState are that
# test, each constructor stores the answer in isBonjourEnabled, and both call
# sites branch over their initialiser when it is false --
#
#   OpenBikeManager::OBM_Initialize   IL_0001 ldfld isBonjourEnabled
#                                     IL_0006 brfalse IL_0100 (the ret)
#   WahooProgram::.ctor               IL_0063 ldfld isBonjourEnabled
#                                     IL_0068 brfalse.s IL_0070 (past WFTNP_Init)
#
# Those two are the whole of it: nothing else in WindowsConnectivity.dll calls
# Marshal::GetTypeFromCLSID or constructs a ComAwareEventInfo.  So with no such
# service the shim needs no COM server and no patched runtime, and with one the
# game needs both -- Apple's COM objects must exist, or OBM_Initialize dies with
# a COMException before Bluetooth is ever reached.  A prefix that has had
# Bonjour installed into it (by Apple's installer, by iTunes, or by hand) is
# therefore worth knowing about, and this only reports it: the service is not
# ours and is never touched.
WINE="${WINE:-wine}"
command -v "$WINE" >/dev/null 2>&1 || WINE=wine64

SVC='Bonjour Service'

gate_is_open() {
    WINEDEBUG=-all "$WINE" sc query "$SVC" 2>/dev/null | tr -d '\r' | grep -q RUNNING
}

report_gate() {
    if gate_is_open; then
        echo "  gate     OPEN -- '$SVC' is RUNNING in this prefix"
        echo "           the game will take the Bonjour path and then need Apple's"
        echo "           COM objects; stop the service if this prefix is meant to"
        echo "           be Bluetooth-only:"
        echo "               WINEPREFIX=$WINEPREFIX $WINE net stop \"$SVC\""
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
