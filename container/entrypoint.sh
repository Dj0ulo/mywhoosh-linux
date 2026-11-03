#!/usr/bin/env bash
# Runs inside the container, as the host user, with one edition's volume on
# /data.  On every launch:
#
#   1. create the Wine prefix, the first time
#   2. bring the prefix's copy of the shim, DXVK and vkd3d-proton to the image's
#   3. install the game, the first time (mywhoosh-update.py --install)
#   4. mywhoosh-ble.sh start   -- updates the game, starts the Bluetooth helper
#   5. run the game, and wait for every Wine process to end
#   6. mywhoosh-ble.sh stop    -- on every way out, by trap
#
# Laid out exactly as a Lutris install is -- /data/prefix is Lutris' $GAMEDIR,
# with the shim in its bleshim/ -- so mywhoosh-ble.sh and the updater run here
# unchanged, and find the game the way they do there.
#
#   entrypoint.sh            the game
#   entrypoint.sh shell      a shell in the same environment, for debugging
#
# MYWHOOSH_EDITION=standard|hd picks the game.  The launcher sets it, and gives
# each edition its own volume; a volume remembers which one it holds.
set -u

say() { echo "[mywhoosh] $*"; }
die() { echo "[mywhoosh] $*" >&2; exit 1; }

EDITION="${MYWHOOSH_EDITION:-standard}"
case "$EDITION" in
standard) STORE_ID=9ndh0f2vhzx2; GAME_DIR=MyWhoosh ;;
hd)       STORE_ID=9nvdcph3xdz2; GAME_DIR=MyWhooshHD ;;
*)        die "MYWHOOSH_EDITION must be standard or hd, not '$EDITION'" ;;
esac

DATA=/data
export WINEPREFIX="$DATA/prefix"
GAME="$WINEPREFIX/drive_c/$GAME_DIR"
SHIM="$WINEPREFIX/bleshim"
BLE="$SHIM/mywhoosh-ble.sh"

# The volume is home: Wine puts the profile's Documents and AppData links
# under it, and podman's keep-id would otherwise hand us the host's home path,
# which does not exist in here.
export HOME="$DATA/home"
# Wine names the profile directory after the user.  podman --userns=keep-id
# has an entry for us in /etc/passwd, docker --user does not; the same name
# either way keeps the game's settings in the same place.
export USER=mywhoosh LOGNAME=mywhoosh
# What the Lutris installers put in the game's environment (lutris/*.yml).
export MYWHOOSH_SHIM_LOG="${MYWHOOSH_SHIM_LOG:-$SHIM/session.log}"
export MONO_PATH="Z:$SHIM"

[ -w "$DATA" ] || die "$DATA is not writable by uid $(id -u) -- the volume was created by another user; remove it and launch again"
# Made here, not by wineboot: Wine creates a prefix only inside a directory
# the user owns, and /data is the image's, mode 1777.
mkdir -p "$HOME" "$WINEPREFIX" || die "cannot write to $DATA"

# ------------------------------------------------------------------- edition
# Two editions in one volume would be two games in one prefix, and the updater
# refuses that ("holds more than one game").  Say so before a 4 GB download.
if [ -f "$DATA/edition" ]; then
    have=$(cat "$DATA/edition")
    [ "$have" = "$EDITION" ] || die "this volume holds MyWhoosh $have, not $EDITION -- the launcher gives each edition its own"
else
    echo "$EDITION" > "$DATA/edition"
fi

# ------------------------------------------------------------- exit and signals
# Every way out stops the helper: a normal exit, a failed install, Ctrl-C,
# `docker stop`.  Long steps run in the background and are waited for, because
# bash defers a trap until a foreground child returns -- and a 2 GB download
# is a long time to ignore Ctrl-C.
child=
cleanup() {
    trap - EXIT
    [ -x "$BLE" ] && "$BLE" stop
}
on_signal() {
    say "interrupted"
    # As PID 1, -1 is every other process in the container: the updater, the
    # helper, the game.  The download resumes at the next launch.
    if [ $$ = 1 ]; then kill -TERM -1 2>/dev/null; else [ -n "$child" ] && kill -TERM "$child" 2>/dev/null; fi
    wineserver -k 2>/dev/null
    wait 2>/dev/null
    cleanup
    exit 130
}
trap cleanup EXIT
trap on_signal INT TERM HUP

run() {
    "$@" &
    child=$!
    wait "$child"
    local rc=$?
    child=
    return $rc
}

if [ "${1:-}" = shell ]; then
    shift
    exec bash "$@"
fi

# -------------------------------------------------------------------- prefix
# mshtml= : no Gecko download dialog; the game shows no web content.
# winemenubuilder.exe= : no menu entries or file associations, which would
# only land in the container's own home.
export WINEDLLOVERRIDES="mshtml=;winemenubuilder.exe="
if [ ! -f "$WINEPREFIX/system.reg" ]; then
    say "creating the Wine prefix ($(wine --version))"
    run wineboot --init || die "wineboot failed"
    run wineserver -w
fi

# No "Bonjour Service" may run in the prefix: with one, the game takes the
# Bonjour COM path wine-mono cannot handle (bleshim/ENGINEERING.md, "The
# Bonjour gate").  Nothing here installs it; this says so if something did.
if grep -q '^\[System\\\\CurrentControlSet\\\\Services\\\\Bonjour Service\]' "$WINEPREFIX/system.reg" 2>/dev/null; then
    say "WARNING: a \"Bonjour Service\" is registered in this prefix; the game's Bluetooth will not work"
    say "         remove it, or start over with a new volume"
fi

# ---------------------------------------------------- what comes from the image
# The image is what decides these versions, so a new image brings them into an
# existing volume.  Copied only when they differ.
sync_file() {   # src dst
    cmp -s "$1" "$2" || cp -f --remove-destination "$1" "$2"
}
mkdir -p "$SHIM"
for f in /opt/mywhoosh/bleshim/*; do
    sync_file "$f" "$SHIM/$(basename "$f")"
done
sys32="$WINEPREFIX/drive_c/windows/system32"
wow64="$WINEPREFIX/drive_c/windows/syswow64"
DXVK_DLLS="d3d8 d3d9 d3d10core d3d11 dxgi"
for d in $DXVK_DLLS; do
    sync_file "/opt/dxvk/x64/$d.dll" "$sys32/$d.dll"
    [ -d "$wow64" ] && sync_file "/opt/dxvk/x32/$d.dll" "$wow64/$d.dll"
done
overrides="${DXVK_DLLS// /,}=n,b"
# HD renders with Direct3D 12; as lutris/mywhoosh-hd.yml does, only it gets
# vkd3d-proton, and only the 64-bit DLLs -- the game is.
if [ "$EDITION" = hd ]; then
    for d in d3d12 d3d12core; do sync_file "/opt/vkd3d-proton/x64/$d.dll" "$sys32/$d.dll"; done
    overrides="$overrides;d3d12,d3d12core=n"
fi

# ------------------------------------------------------------------- the game
# A game directory with no manifest is a first launch, or one whose install
# was interrupted: --install picks up the download where it stopped.
if [ ! -f "$GAME/AppxManifest.xml" ]; then
    say "installing MyWhoosh ($EDITION) from the Microsoft Store into the volume"
    say "about $([ "$EDITION" = hd ] && echo 9 || echo 4) GB free needed there, for the package and the unpacked game"
    run python3 -I "$SHIM/mywhoosh-update.py" --install "$STORE_ID" "$GAME" ||
        die "the game could not be installed (see above); launch again to resume"
fi

# Updates the game (unless MYWHOOSH_AUTO_UPDATE=0), then starts the helper.
# It never fails: a Bluetooth problem must not keep the game from starting.
run "$BLE" start

# ------------------------------------------------------------------- display
# The launcher passes DISPLAY for X11 (XWayland included); with only
# WAYLAND_DISPLAY, Wine's own Wayland driver is used.  Wine picks X11 whenever
# DISPLAY is set, so a Wayland-only launch must not have one.
[ -n "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ] && say "using Wine's native Wayland driver"

export WINEDLLOVERRIDES="$WINEDLLOVERRIDES;$overrides"
say "starting MyWhoosh ($EDITION)"
cd "$GAME" || die "$GAME is missing"
# MyWhoosh.exe starts the shipping binary and may return before it does, so
# the game is over when the last Wine process is: wineserver -w waits for that.
run wine MyWhoosh.exe
rc=$?
run wineserver -w
say "MyWhoosh exited"
exit $rc
