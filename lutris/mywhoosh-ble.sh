#!/usr/bin/env bash
# The Linux half of the Bluetooth stack, as Lutris drives it.
#
#   mywhoosh-ble.sh check     once, at the end of the install
#   mywhoosh-ble.sh start     Lutris' pre-launch script
#   mywhoosh-ble.sh stop      Lutris' post-exit script
#
# The installer copies this next to blehelper.py inside the game directory and
# points the game's prelaunch_command/postexit_command at it, so a user starts
# MyWhoosh from Lutris and the helper comes and goes with it.  Without the
# helper running the game's Bluetooth simply reads as off -- there is no error
# on screen -- which is why `start` says so out loud instead of failing quietly.
#
# Nothing here ever exits non-zero: a problem with Bluetooth must not stop the
# game from launching, and a problem at install time must not throw away a
# finished download.  Say what is wrong, then get out of the way.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
HELPER="$DIR/blehelper.py"
PIDFILE="$DIR/blehelper.pid"
LOG="${MYWHOOSH_SHIM_LOG:-$DIR/session.log}"
PORT="${MYWHOOSH_BLE_PORT:-27019}"
ADAPTER="${MYWHOOSH_BLE_ADAPTER:-hci0}"
# The prefix is the game directory's parent structure: this script lives in
# <prefix>/bleshim, and the prefix is what holds drive_c.
PREFIX="${WINEPREFIX:-$(dirname "$DIR")}"
MONO="$PREFIX/drive_c/windows/mono/mono-2.0/lib"

# Lutris hands its pre-launch script the game's own environment, which carries
# the Lutris runtime's LD_LIBRARY_PATH.  The system python3 loading dbus and gi
# against those libraries is a segfault or an ImportError, depending on the
# distribution.  The helper is a Linux program and wants a Linux environment.
clean_env() {
    unset LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME
}

# Is something already serving the port?  Asked of /proc rather than by
# connecting to it: the helper serves one client at a time and a new connection
# drops the old one, so a probe would unsubscribe the sensors of a game that is
# already riding.  /proc/net/tcp lists the listening socket as 0100007F:<port>
# in state 0A, and needs no tool the distribution might not have.
port_open() {
    local hex
    hex=$(printf '0100007F:%04X' "$PORT")
    grep -qi " $hex .* 0A " /proc/net/tcp 2>/dev/null
}

say() { echo "[mywhoosh-ble] $*"; }

# Both at install time and at launch: the same three questions, asked of the
# machine rather than of the user.
deps_report() {
    local ok=0

    if command -v python3 >/dev/null 2>&1; then
        say "ok    python3 ($(python3 -V 2>&1))"
    else
        say "MISSING python3 -- the helper cannot run at all"
        ok=1
    fi

    local missing=""
    python3 -c "import dbus" 2>/dev/null || missing="$missing dbus-python"
    python3 -c "import gi; gi.require_version('GLib','2.0')" 2>/dev/null || missing="$missing PyGObject"
    if [ -z "$missing" ]; then
        say "ok    python dbus + PyGObject"
    else
        say "MISSING python modules:$missing"
        say "      Debian/Ubuntu:  sudo apt install python3-dbus python3-gi"
        say "      Fedora:         sudo dnf install python3-dbus python3-gobject"
        say "      Arch:           sudo pacman -S python-dbus python-gobject"
        ok=1
    fi

    if python3 - "$ADAPTER" <<'PY' 2>/dev/null
import sys
import dbus
b = dbus.SystemBus().get_object("org.bluez", "/")
objs = b.GetManagedObjects(dbus_interface="org.freedesktop.DBus.ObjectManager")
sys.exit(0 if "/org/bluez/" + sys.argv[1] in objs else 1)
PY
    then
        say "ok    BlueZ adapter $ADAPTER"
    else
        say "WARN  no BlueZ adapter $ADAPTER -- is bluetoothd running, and the adapter on?"
        say "      check with:  bluetoothctl list  /  bluetoothctl power on"
        say "      another adapter: set MYWHOOSH_BLE_ADAPTER in Lutris' environment variables"
        ok=1
    fi

    return $ok
}

notify() {
    command -v notify-send >/dev/null 2>&1 && \
        notify-send -a MyWhoosh -u critical "MyWhoosh: no Bluetooth" "$1" 2>/dev/null
    say "$1"
}

case "${1:-}" in
check)
    clean_env
    say "checking the Bluetooth setup"
    for dll in Windows.dll System.Runtime.WindowsRuntime.dll MyWhooshShim.dll; do
        if [ -f "$MONO/$dll" ]; then
            say "ok    $dll in the prefix's mono tree"
        else
            say "MISSING $MONO/$dll"
        fi
    done
    # mono/4.5/mscorlib.dll is wine-mono's own; without it the directory above
    # is one we created by copying into it, and the game will not start at all.
    mono_ok=0
    [ -f "$MONO/mono/4.5/mscorlib.dll" ] || {
        mono_ok=1
        say "MISSING wine-mono in this prefix ($MONO)"
        say "      the game itself needs it -- reinstall without disabling Mono"
    }
    [ -x "$HELPER" ] || chmod +x "$HELPER" 2>/dev/null
    [ -f "$HELPER" ] || say "MISSING $HELPER"

    if deps_report && [ "$mono_ok" = 0 ]; then
        say "ready -- start MyWhoosh from Lutris and pair your trainer in the game"
    else
        say "the game will run, but it will report Bluetooth as off until the above is fixed"
    fi
    exit 0
    ;;

start)
    clean_env
    # Rotate before starting, but only if we are the ones starting: a helper
    # already running holds the old file open, and renaming it underneath would
    # send the rest of its output to a file named .prev.
    if port_open; then
        say "a helper is already listening on 127.0.0.1:$PORT -- leaving it alone"
        exit 0
    fi

    # One log per run, with the previous one kept: the interesting failures
    # (a sensor that never appears) are read after quitting the game.
    [ -f "$LOG" ] && mv -f "$LOG" "$LOG.prev" 2>/dev/null
    say "log: $LOG"

    if [ ! -f "$HELPER" ]; then
        notify "the Bluetooth helper is missing ($HELPER)"
        exit 0
    fi

    if ! deps_report >> "$LOG" 2>&1; then
        sed -n 's/^/  /p' "$LOG" >&2
        notify "Bluetooth support is not set up on this machine -- see $LOG"
        exit 0
    fi

    setsid nohup python3 "$HELPER" -v --port "$PORT" --adapter "$ADAPTER" \
        >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"

    # The shim retries the connection every 5 s, so a slow start is survivable;
    # waiting here is only so that a helper which dies immediately is reported
    # now rather than looking like a trainer that will not pair.
    for _ in $(seq 40); do
        port_open && { say "helper up on 127.0.0.1:$PORT (pid $(cat "$PIDFILE"))"; exit 0; }
        kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
        sleep 0.25
    done

    rm -f "$PIDFILE"
    notify "the Bluetooth helper failed to start -- see $LOG"
    tail -5 "$LOG" >&2 2>/dev/null
    exit 0
    ;;

stop)
    # Only what we started: a helper someone is running by hand in a terminal
    # has no pidfile here and is none of our business.
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        kill "$pid" 2>/dev/null && say "stopped the helper (pid $pid)"
        for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        kill -9 "$pid" 2>/dev/null
        rm -f "$PIDFILE"
    fi
    exit 0
    ;;

*)
    echo "usage: $0 check|start|stop" >&2
    exit 0
    ;;
esac
