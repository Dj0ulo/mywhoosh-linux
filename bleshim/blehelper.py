#!/usr/bin/env python3
"""
Serve Linux's Bluetooth to MyWhoosh's own BLE stack, running under Wine.

The game's BT_* path is written against WinRT, which Wine does not have and
mono cannot project.  ./src/Windows.cs answers those WinRT calls from inside the
prefix instead -- but it cannot reach BlueZ itself, because BlueZ is on D-Bus,
D-Bus is a unix socket, and Wine's winsock has no AF_UNIX.  So this does the
BlueZ half on the Linux side and the two halves talk over loopback TCP.

The protocol is newline-delimited JSON, deliberately dumb: it is a GATT client
API and nothing more, so that the day Wine's own Bluetooth stack grows
characteristic writes, notifications and scanning, the shim can drop this
process without anything above it changing.

    ->  {"id":1,"op":"scan","enable":true}
    <-  {"id":1,"ok":true}
    <-  {"ev":"advert","addr":"AA:BB:...","name":"Smart Trainer","uuids":[...],"rssi":-61}
    ->  {"id":2,"op":"connect","addr":"AA:BB:..."}
    <-  {"id":2,"ok":true,"name":"Smart Trainer"}
    ->  {"id":3,"op":"services","addr":"AA:BB:..."}
    <-  {"id":3,"ok":true,"services":[{"uuid":"00001826-..."}]}
    ->  {"id":4,"op":"notify","addr":"AA:BB:...","char":"00002ad2-...","enable":true}
    <-  {"id":4,"ok":true}
    <-  {"ev":"value","addr":"AA:BB:...","char":"00002ad2-...","value":"44024a00..."}

Ops: radios, scan, connect, disconnect, services, chars, read, write, notify.
Events: advert, value, connection.

Usage:
    ./blehelper.py                  # serve on 127.0.0.1:27019
    ./blehelper.py --list           # scan, print what is on the air, exit
    ./blehelper.py --port 27019 --adapter hci0

Start it before the game; nothing has to be configured per trainer, because the
game does its own scanning, pairing and slot filling through it.  This connects
to nothing by itself: it holds no device the game has not asked for, and several
sensors are simply several independent connections.

## Threading, and why it matters

BlueZ operations have wildly different latencies: `Adapter1.StartDiscovery`
answers in milliseconds, while `Device1.Connect` blocks for up to BlueZ's own
thirty-second timeout, and a device whose services never resolve (a phone
waiting for a pairing that is never granted) eats the whole budget before
failing.  Earlier versions ran every operation on the GLib main-loop thread, so
one slow Connect() froze *every* other request -- including the periodic "is
Bluetooth on" question the game asks from its UI thread, several times a
second.  That is what a user sees when the whole window stops answering.

Now the main loop only ever runs the instantly-answerable work -- the radio
question, from an in-memory cache, and scan toggling -- and one worker thread
takes everything potentially slow, in order, on its own D-Bus connection.  A
connect that takes half a minute now delays only other connection-type
operations; scans, radio polls and the stream of notifications keep flowing the
whole time.

The worker hands its replies to the main loop through a queue, and the main
loop is the only thread that ever writes to the socket, so a reply is never
written by two threads at once and cannot be interleaved within a line.

## Quiescence, and why connects need it

The game retries an unreached device every five seconds for as long as
Bluetooth is on (its `DidFailToCreateBluetoothDevice`, read out of its IL).
Each attempt costs BlueZ a full connect timeout and, on a device that wants
pairing -- an iPhone relaying a heart rate, say -- wakes the far half into a
pairing prompt its owner has to dismiss, over and over.  So a failed connect
puts the address on a ladder: the first attempt passes through untouched;
later attempts are answered on sight with a failure and go nowhere near the
radio until the address has rested ten, then twenty, then forty seconds,
capped at two minutes.  A successful connect clears the ladder, and so does
the game disconnecting the device first.
"""

import argparse
import collections
import errno
import json
import queue
import socket
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
SERVICE_IFACE = "org.bluez.GattService1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"

CONNECT_TIMEOUT = 25          # seconds waiting for a link and its GATT tree
ADVERT_TIMEOUT = 20           # seconds waiting for a named device to be on the air
# The engine's own reconnect cadence is five seconds.  A failed connect has
# already cost a twenty-second wait for the device to appear, and BlueZ holds
# its own ~30 s link timeout before that.  Letting the game fire one of those
# into the radio every five seconds is what kept waking everything up.
CONNECT_BACKOFF_BASE = 10     # seconds after the first failure
CONNECT_BACKOFF_MAX = 120     # seconds between attempts at the ladder's top

verbose = False


def log(fmt, *a):
    sys.stderr.write("[%s blehelper] %s\n"
                     % (time.strftime("%H:%M:%S"), fmt % a if a else fmt))
    sys.stderr.flush()


def trace(fmt, *a):
    if verbose:
        log(fmt, *a)


def select_adapter(objects, requested="auto"):
    """Select a powered adapter without assuming Linux assigned it hci0."""
    adapters = {str(path): interfaces[ADAPTER_IFACE]
                for path, interfaces in objects.items() if ADAPTER_IFACE in interfaces}
    if requested != "auto":
        path = "/org/bluez/" + requested
        if path not in adapters:
            raise BleError("no BlueZ adapter %s; check bluetoothctl list" % requested)
        if not adapters[path].get("Powered", False):
            raise BleError("Bluetooth adapter %s is powered off" % requested)
        return requested
    powered = sorted(path for path, props in adapters.items() if props.get("Powered", False))
    if not powered:
        raise BleError("no powered Bluetooth adapter; check bluetoothctl list / bluetoothctl power on")
    # hci0 was the only default before auto-selection; a machine with a second
    # powered adapter that worked then must keep working.
    if len(powered) > 1 and "/org/bluez/hci0" in powered:
        return "hci0"
    if len(powered) != 1:
        raise BleError("multiple powered Bluetooth adapters (%s); set MYWHOOSH_BLE_ADAPTER or --adapter"
                       % ", ".join(path.rsplit("/", 1)[-1] for path in powered))
    return powered[0].rsplit("/", 1)[-1]


class Bluez:
    """BlueZ, in the few verbs the shim needs of it.

    One instance per D-Bus connection: dbus-python connections are not
    thread-safe, so the main loop and the worker each carry their own.  This
    class holds no state of its own -- whatever two threads must agree on, the
    scan toggle chief among it, lives in the Server that owns both instances.
    """

    def __init__(self, bus, adapter):
        self.bus = bus
        self.adapter_path = "/org/bluez/%s" % adapter
        self.om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)

    # --------------------------------------------------------------- lookup

    def objects(self):
        return self.om.GetManagedObjects()

    def device_path(self, addr):
        return "%s/dev_%s" % (self.adapter_path, addr.upper().replace(":", "_"))

    def device_props(self, addr):
        return self.objects().get(self.device_path(addr), {}).get(DEVICE_IFACE, {})

    def on_air(self, addr):
        """Is this device actually advertising, or merely remembered?

        BlueZ keeps an object for every device it has ever seen, and connecting
        to one that is not there costs a 30s timeout in Connect().  RSSI is set
        from an advertisement received during the current discovery and cleared
        when discovery stops, which is exactly the distinction worth making --
        a trainer that has been asleep since yesterday looks identical without
        it.
        """
        d = self.device_props(addr)
        return "RSSI" in d or bool(d.get("Connected"))

    def adapter(self):
        props = self.objects().get(self.adapter_path, {}).get(ADAPTER_IFACE)
        if props is None:
            raise BleError("Bluetooth adapter %s disappeared; it is used again if it returns under "
                           "that name, otherwise relaunch the game" % self.adapter_path.rsplit("/", 1)[-1])
        if not props.get("Powered", False):
            raise BleError("Bluetooth adapter is powered off")
        return dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE)

    def radios(self):
        """Every adapter BlueZ has, and whether it is powered.

        The game asks this first and shows the user "Bluetooth is off" for an
        empty or unpowered answer, so it comes from Adapter1.Powered -- the one
        source that cannot disagree with what a scan will then do.  (sysfs looks
        like the easier route from inside the prefix and is not: current kernels
        have no /sys/class/bluetooth/hciN/flags to read at all.)

        The server answers the game from Server.radios_cache rather than
        calling here per poll; this method runs at startup, on demand the first
        time, and whenever an adapter object changes underneath us.
        """
        out = []
        for path, ifaces in sorted(self.objects().items()):
            a = ifaces.get(ADAPTER_IFACE)
            if not a:
                continue
            out.append({"name": path.rsplit("/", 1)[-1],
                        "address": str(a.get("Address", "")),
                        "powered": bool(a.get("Powered", False))})
        return out

    # ------------------------------------------------------------ discovery

    # Stateless on purpose: whether a discovery is running is one fact shared
    # by the main loop and the worker, kept in Server.scanning.  A flag per
    # Bluez handle could disagree with it and skip a StopDiscovery.
    def start_scan(self):
        adapter = self.adapter()
        try:
            # Introspection can disappear during a controller reset. An explicit
            # a{sv} signature prevents dbus-python inferring a string dictionary
            # and throwing TypeError before the server can return a D-Bus error.
            adapter.SetDiscoveryFilter(
                dbus.Dictionary({"Transport": "le", "DuplicateData": dbus.Boolean(True)}, signature="sv"),
                signature="a{sv}")
        except dbus.DBusException as e:
            log("discovery filter refused (%s), scanning anyway", e.get_dbus_name())
        try:
            adapter.StartDiscovery()
        except dbus.DBusException as e:
            if e.get_dbus_name() != "org.bluez.Error.InProgress":
                raise

    def stop_scan(self):
        try:
            self.adapter().StopDiscovery()
        except (BleError, dbus.DBusException) as e:
            trace("StopDiscovery: %s", e)

    def wait_for_advert(self, addr, seconds, host=None):
        """Scan until this device is on the air, whatever the client asked for.

        The game connects to a device it saw in a scan it has since stopped,
        and a trainer drops its radio the moment the cranks stop turning.
        Waiting here turns "connect failed" into "connect took a moment".

        Scanning state is shared with the main loop (whether a discovery is
        running is one fact, not one per thread), but the D-Bus calls ride
        this thread's own connection: `host`, when given, is the Server, and
        it reports the shared flag and serialized the toggle.  A scan that
        *this* wait started is stopped again on the way out.

        Runs on the worker thread; a restful poll of the worker's own
        connection instead of BlueZ signals keeps the code readable.
        """
        end = time.monotonic() + seconds
        started_here = False
        seen = False
        while True:
            if self.on_air(addr):
                seen = True
                break
            if time.monotonic() >= end:
                break
            if host is not None:
                if not host.shared_scanning():
                    host.request_scan(True, self)
                    started_here = True
            time.sleep(0.3)
        if started_here and host:
            host.request_scan(False, self)
        return seen

    # -------------------------------------------------------------- connect

    def connect(self, addr):
        """Connect and wait for the GATT tree, the way WinRT's
        FromBluetoothAddressAsync leaves a device ready to be walked.

        Blocking is expected here: this runs on the worker thread, the one
        place that may take tens of seconds, and `dev.Connect()` is exactly
        such a call -- BlueZ holds the method call until its own link timeout
        while our main loop keeps serving scans, radio polls and notification
        events as if nothing were happening, which is the point.
        """
        host = getattr(self, "host", None)
        path = self.device_path(addr)
        if path not in self.objects():
            log("waiting to see %s advertising", addr)
            if not self.wait_for_advert(addr, ADVERT_TIMEOUT, host):
                raise BleError("%s is not on the air (asleep, or out of range)" % addr)

        props = dbus.Interface(self.bus.get_object(BLUEZ, path), PROPS_IFACE)
        if not props.Get(DEVICE_IFACE, "Connected"):
            if not self.on_air(addr) and not self.wait_for_advert(addr, ADVERT_TIMEOUT, host):
                raise BleError("%s is not on the air (asleep, or out of range)" % addr)
            dev = dbus.Interface(self.bus.get_object(BLUEZ, path), DEVICE_IFACE)
            log("connecting to %s", addr)
            try:
                dev.Connect()
            except dbus.DBusException as e:
                raise BleError("Connect() failed: %s" % e.get_dbus_message())

        try:
            # An untrusted device is one BlueZ will not let reconnect on its
            # own, and a trainer that sleeps between intervals needs to.
            if not props.Get(DEVICE_IFACE, "Trusted"):
                props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
        except dbus.DBusException:
            pass

        # The GATT tree takes a moment once the link is up; BlueZ flips
        # ServicesResolved when it is complete.  Polling on this thread's own
        # connection is cheap and free of any main-loop dependency.
        end = time.monotonic() + CONNECT_TIMEOUT
        while True:
            try:
                if props.Get(DEVICE_IFACE, "ServicesResolved"):
                    break
            except dbus.DBusException:
                raise BleError("%s went away while resolving its services" % addr)
            if time.monotonic() >= end:
                raise BleError("connected to %s but its services never resolved" % addr)
            time.sleep(0.3)

        name = str(self.device_props(addr).get("Alias", "") or "")
        log("%s connected as %r", addr, name)
        return name

    def disconnect(self, addr):
        path = self.device_path(addr)
        if path not in self.objects():
            return
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, path), DEVICE_IFACE).Disconnect()
            log("%s disconnected", addr)
        except dbus.DBusException as e:
            trace("Disconnect(%s): %s", addr, e.get_dbus_name())

    # ----------------------------------------------------------------- gatt

    def services(self, addr):
        prefix = self.device_path(addr) + "/"
        out = []
        for path, ifaces in sorted(self.objects().items()):
            svc = ifaces.get(SERVICE_IFACE)
            if svc and path.startswith(prefix):
                out.append({"uuid": str(svc["UUID"]).lower()})
        return out

    def characteristics(self, addr, service_uuid):
        objs = self.objects()
        prefix = self.device_path(addr) + "/"
        wanted = {path for path, ifaces in objs.items()
                  if path.startswith(prefix)
                  and SERVICE_IFACE in ifaces
                  and str(ifaces[SERVICE_IFACE]["UUID"]).lower() == service_uuid.lower()}
        out = []
        for path, ifaces in sorted(objs.items()):
            ch = ifaces.get(CHAR_IFACE)
            if not ch or str(ch["Service"]) not in wanted:
                continue
            out.append({"uuid": str(ch["UUID"]).lower(),
                        "flags": [str(f) for f in ch.get("Flags", [])]})
        return out

    def char_path(self, addr, char_uuid):
        prefix = self.device_path(addr) + "/"
        for path, ifaces in sorted(self.objects().items()):
            ch = ifaces.get(CHAR_IFACE)
            if ch and path.startswith(prefix) and str(ch["UUID"]).lower() == char_uuid.lower():
                return path
        raise BleError("%s has no characteristic %s" % (addr, char_uuid))

    def char(self, addr, char_uuid):
        return dbus.Interface(self.bus.get_object(BLUEZ, self.char_path(addr, char_uuid)), CHAR_IFACE)

    def read(self, addr, char_uuid):
        try:
            value = self.char(addr, char_uuid).ReadValue({})
        except dbus.DBusException as e:
            raise BleError("ReadValue failed: %s" % e.get_dbus_message())
        return bytes(bytearray(value))

    def write(self, addr, char_uuid, value, with_response):
        options = {"type": "request" if with_response else "command"}
        try:
            self.char(addr, char_uuid).WriteValue(dbus.Array(value, signature="y"), options)
        except dbus.DBusException as e:
            raise BleError("WriteValue failed: %s" % e.get_dbus_message())

    def notify(self, addr, char_uuid, enable):
        char = self.char(addr, char_uuid)
        try:
            if enable:
                char.StartNotify()
            else:
                char.StopNotify()
        except dbus.DBusException as e:
            # Subscribing twice is the game re-pairing a sensor it already has,
            # which is not a failure worth reporting up.
            if e.get_dbus_name() == "org.bluez.Error.InProgress" and enable:
                trace("%s %s already notifying", addr, char_uuid)
                return
            raise BleError("%s failed: %s" % ("StartNotify" if enable else "StopNotify",
                                              e.get_dbus_message()))


class BleError(Exception):
    """Something the client asked for that the hardware would not do."""


class Quiescence:
    """Failed connects, spaced out.

    The game asks for the same unreached device every five seconds for as long
    as Bluetooth is on.  Attempts pass through normally at first; later ones
    are answered on sight with a failure and go nowhere near the radio until
    the address has rested its interval.  A success clears the penalty, and so
    does a disconnect -- the game asking to disconnect first reads as "I am
    starting this connection anew", not as a chance to pound the same failure.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.failures = {}                  # addr -> (rest_until, misses)

    def remaining(self, addr):
        """Seconds this address must still rest, or 0 when it may go now."""
        with self.lock:
            entry = self.failures.get(addr)
        if entry is None:
            return 0.0
        return max(0.0, entry[0] - time.monotonic())

    def penalize(self, addr):
        with self.lock:
            _, misses = self.failures.get(addr, (0, 0))
            misses += 1
            # The first reflection of a failure -- the attempt five seconds
            # later -- is worth passing on untouched, because a trainer might
            # just now be waking up.  After that, stretch: 20, 40, 80, capped.
            rest = min(CONNECT_BACKOFF_BASE * (2 ** (misses - 1)), CONNECT_BACKOFF_MAX)
            self.failures[addr] = (time.monotonic() + rest, misses)
        trace("%s: %d failed connect(s); next attempt rests %ds", addr, misses, int(rest))

    def clear(self, addr):
        with self.lock:
            self.failures.pop(addr, None)


class Server:
    """One TCP client -- the game -- and the BlueZ state it asks for."""

    def __init__(self, main_bluez, port):
        self.main_bluez = main_bluez
        self.bluez = main_bluez             # the handle the session state hangs on
        self.worker_bluez = None            # built on the worker thread itself
        self.port = port
        self.client = None
        self.buffer = b""

        # Small shared state, guarded: the scan toggle is touched by both
        # threads, and so are the subscription and connection maps (the worker
        # updates them from job results; signal handlers read them here).
        self.scan_lock = threading.Lock()
        self.scanning = False
        self.meta_lock = threading.Lock()
        self.subscribed = set()             # (addr, char uuid) the client wants
        self.silent = set()                 # devices reported without service UUIDs
        self.char_addr = {}                 # char object path -> (addr, uuid)
        self.connected = {}                 # addr -> last reported Connected
        self.path_addr = {}                 # BlueZ object path -> address

        # The radio state the game polls several times a second, held in
        # memory: refreshed at start, on demand the first time, and whenever an
        # adapter object appears, disappears or changes underneath us.
        self.radios_cache = []
        self.radios_lock = threading.Lock()

        # All socket writes happen on the main loop, out of this queue.  The
        # worker hands every reply here, and so do the signal handlers, so a
        # line is never written by two threads at once.
        self.outbox = collections.deque()

        # One job at a time, in arrival order: the sequence is what a session
        # looks like a session for, and a read must not land in the middle of
        # another device's link being built.
        self.work = queue.Queue()
        self.quiescence = Quiescence()

    # ------------------------------------------------------------ transport

    def listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", self.port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                raise SystemExit("port %d is already in use -- another blehelper?" % self.port)
            raise
        s.listen(1)
        s.setblocking(False)
        GLib.io_add_watch(s, GLib.IO_IN, self.on_accept)
        # The pump that drains the outbox: whatever thread queued a message,
        # the write to the game happens here and nowhere else.
        GLib.timeout_add(30, self.drain_outbox)
        log("listening on 127.0.0.1:%d", self.port)

    def on_accept(self, sock, condition):
        try:
            conn, _ = sock.accept()
        except OSError:
            return True
        if self.client is not None:
            # The shim opens one connection and keeps it; a second means the
            # game was restarted and the old socket is a corpse.
            log("replacing the previous client")
            self.drop_client()
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.meta_lock:
            self.buffer = b""
        self.client = conn
        GLib.io_add_watch(conn, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_data)
        log("shim connected")
        return True

    def drop_client(self):
        """Let go of everything the client had asked for.

        The game going away must not leave a trainer subscribed and a scan
        running: the next process to use the adapter would inherit both.  Jobs
        still queued are discarded; the job already running on the worker
        finishes, and its reply finding no client is send()'s no-op.
        """
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
        self.outbox.clear()
        try:
            while True:
                self.work.get_nowait()
        except queue.Empty:
            pass
        for addr, uuid in list(self.lookup_meta("subscribed")):
            try:
                self.main_bluez.notify(addr, uuid, False)
            except (BleError, dbus.DBusException):
                pass
        with self.meta_lock:
            self.subscribed.clear()
            self.char_addr.clear()
            self.silent.clear()
        self.stop_scan()

    def on_data(self, conn, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            log("shim disconnected")
            self.drop_client()
            return False
        try:
            chunk = conn.recv(65536)
        except OSError:
            chunk = b""
        if not chunk:
            log("shim disconnected")
            self.drop_client()
            return False

        lines = []
        with self.meta_lock:
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                line = line.strip()
                if line:
                    lines.append(line)
        for line in lines:
            try:
                self.handle(line)
            except Exception as e:                   # noqa: BLE001 -- the reader
                # must outlive anything one request can do: if it died here,
                # every later request would go unanswered while the game waits.
                log("unhandled on %r: %s: %s", line[:80], type(e).__name__, e)
                self.refuse_unhandled(line, e)
        return True

    def refuse_unhandled(self, line, e):
        """Answer a request whose handling blew up, if it carried an id.

        A refusal costs the game one failed call; silence costs it the
        shim's whole timeout for the same outcome.
        """
        try:
            rid = json.loads(line.decode("utf-8")).get("id")
        except Exception:                            # noqa: BLE001
            return
        if rid is not None:
            self.send({"id": rid, "ok": False,
                       "error": "%s: %s" % (type(e).__name__, e)})

    def send(self, obj):
        if self.client is None:
            return
        try:
            self.client.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError as e:
            log("send failed (%s); dropping the client", e)
            self.drop_client()

    def post(self, obj):
        """Hand a message to the main loop's writer (safe from any thread)."""
        self.outbox.append(obj)

    def drain_outbox(self):
        while self.outbox:
            self.send(self.outbox.popleft())
        return True

    # ---------------------------------------------------- scan/radio state

    def shared_scanning(self):
        """Whether a discovery is running -- one fact, any thread may ask."""
        with self.scan_lock:
            return self.scanning

    def request_scan(self, want, by_bluez):
        """Turn scanning on or off on behalf of any thread.

        The D-Bus call must ride the asking thread's own connection, so the
        thread hands in its own Bluez handle; the lock means two threads can
        never disagree about whether a discovery is running or stop each
        other's discovery out from under them.  No-op, not an error, when the
        state already agrees with the request.
        """
        with self.scan_lock:
            if bool(want) == bool(self.scanning):
                return
            # start_scan/stop_scan raise BleError for a vanished adapter and
            # DBusException for a controller reset mid-toggle; neither may be
            # swallowed here -- the caller turns the exception into the
            # protocol error the game needs to see.
            if want:
                by_bluez.start_scan()
            else:
                by_bluez.stop_scan()
            self.scanning = bool(want)
        log("scanning" if want else "scan stopped")

    def start_scan(self):
        self.request_scan(True, self.main_bluez)

    def stop_scan(self):
        self.request_scan(False, self.main_bluez)

    def refresh_radios(self):
        """One snapshot of Adapter1.Powered, straight from BlueZ."""
        try:
            radios = self.main_bluez.radios()
        except dbus.DBusException as e:
            log("radio refresh failed: %s", e.get_dbus_message())
            return
        with self.radios_lock:
            self.radios_cache = radios
        trace("radios: %d adapter(s); %s", len(radios),
              ", ".join("%s=%s" % (r["name"], r["powered"]) for r in radios))

    def cached_radios(self):
        with self.radios_lock:
            return list(self.radios_cache)

    # ------------------------------------------------------------ requests

    def handle(self, line):
        """Route one request.

        What the main loop can answer instantly, it does -- radio state from
        the cache, scan toggling plus the announce that follows, because those
        are short D-Bus calls by construction.  Everything else goes to the
        worker, in arrival order, and the reply comes back through the outbox.
        """
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            log("unparsable request (%s): %r", e, line[:80])
            return
        if not isinstance(req, dict):
            log("request is not an object: %r", line[:80])
            return

        rid = req.get("id")
        op = req.get("op")
        addr = (req.get("addr") or "").upper()

        if op == "radios":
            radios = self.cached_radios()
            if not radios:
                # A cache that was never filled (the first poll right after
                # launch, say): pay the one D-Bus call now, on the loop,
                # rather than leaving every poll unanswered.
                self.refresh_radios()
                radios = self.cached_radios()
            self.send({"id": rid, "ok": True, "radios": radios})
            return

        if op == "scan":
            # Keep the radio cache warm while we are here: it is free, and it
            # heals a Bluetooth whose Powered moved while the helper was down.
            # The reply goes straight to the socket -- handle runs on the main
            # loop, the one thread allowed to write it.
            try:
                if req.get("enable"):
                    self.start_scan()
                    self.announce_known()
                else:
                    self.stop_scan()
                self.refresh_radios()
            except BleError as e:
                log("scan: %s", e)
                self.send({"id": rid, "ok": False, "error": str(e)})
                return
            except dbus.DBusException as e:
                log("scan: %s", e.get_dbus_message())
                self.send({"id": rid, "ok": False, "error": e.get_dbus_message()})
                return
            except Exception as e:                   # noqa: BLE001
                # Every request read owes the shim a reply; anything else
                # would leave the game waiting out its whole timeout.
                log("scan: unexpected: %s: %s", type(e).__name__, e)
                self.send({"id": rid, "ok": False,
                           "error": "%s: %s" % (type(e).__name__, e)})
                return
            self.send({"id": rid, "ok": True})
            return

        if op == "connect":
            # Read before the queue: a game retrying the same unreachable
            # device every five seconds is met with an instant refusal rather
            # than with the worker parking on the same thirty-second Connect()
            # again.  A disconnect clears the ladder, so a user who asks to
            # disconnect first is answered with real attempts.
            rest = self.quiescence.remaining(addr)
            if rest > 0:
                trace("connect %s deferred; quiescence %.0fs remaining", addr, rest)
                # No countdown in the message: the shim logs it, and a line
                # that changes every retry would never be suppressed as a repeat.
                self.post({"id": rid, "ok": False,
                           "error": "%s is being retried too soon" % addr})
                return

        # Potentially slow: services, characters, reads, writes, and above all
        # the connect family.  Serialized, in order, on the worker.
        self.work.put((req, rid))
        trace("%s queued (id %s)", op, rid)

    # -------------------------------------------------------------- worker

    def worker_loop(self):
        """The thread that may block, for as long as BlueZ needs to.

        One D-Bus connection of its own -- dbus-python connections bind to the
        thread that creates them -- and one job at a time, so two connects can
        never race each other and a read cannot land in the middle of a link
        being set up.  Replies leave via the outbox and are written by the
        main loop, so this thread never touches the socket.
        """
        try:
            self.worker_bluez = Bluez(dbus.SystemBus(), self.main_bluez.adapter_path.rsplit("/", 1)[-1])
        except dbus.DBusException as e:
            log("worker cannot open its D-Bus connection (%s) -- slow operations will fail", e)
            while True:
                req, rid = self.work.get()
                self.post({"id": rid, "ok": False, "error": "helper's worker thread lost its D-Bus connection"})
            return
        # The worker reports the shared scan flag and asks the Server to flip
        # it; the D-Bus half of that toggle rides this thread's own handle.
        self.worker_bluez.host = self

        while True:
            req, rid = self.work.get()
            op = req.get("op")
            addr = (req.get("addr") or "").upper()
            try:
                reply = self.worker_dispatch(op, req)
            except BleError as e:
                if op == "connect" and addr:
                    self.quiescence.penalize(addr)
                trace("%s: %s", op, e)
                self.post({"id": rid, "ok": False, "error": str(e)})
                continue
            except dbus.DBusException as e:
                log("%s: %s", op, e.get_dbus_message())
                if op == "connect" and addr:
                    self.quiescence.penalize(addr)
                self.post({"id": rid, "ok": False, "error": e.get_dbus_message()})
                continue
            except Exception as e:                   # noqa: BLE001 -- a worker that
                # dies leaves every later request unanswered, and a malformed
                # id or a bad hex string must not be able to kill it.
                log("%s: unexpected: %s: %s", op, type(e).__name__, e)
                self.post({"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e)})
                continue
            reply = dict(reply or {})
            reply.update({"id": rid, "ok": True})
            self.post(reply)

    def worker_dispatch(self, op, req):
        """One job against BlueZ.  Runs on the worker thread, may block."""
        bz = self.worker_bluez
        addr = (req.get("addr") or "").upper()
        char = (req.get("char") or "").lower()

        if op == "connect":
            name = bz.connect(addr)
            with self.meta_lock:
                self.connected[addr] = True
            self.quiescence.clear(addr)
            return {"name": name}

        if op == "disconnect":
            with self.meta_lock:
                for key in [k for k in self.subscribed if k[0] == addr]:
                    self.subscribed.discard(key)
            bz.disconnect(addr)
            self.quiescence.clear(addr)     # a fresh ask after a disconnect is earned
            return {}

        if op == "services":
            return {"services": bz.services(addr)}

        if op == "chars":
            chars = bz.characteristics(addr, (req.get("service") or "").lower())
            with self.meta_lock:
                for c in chars:
                    try:
                        self.char_addr[bz.char_path(addr, c["uuid"])] = (addr, c["uuid"])
                    except BleError:
                        pass
            return {"chars": chars}

        if op == "read":
            return {"value": bz.read(addr, char).hex()}

        if op == "write":
            value = bytes.fromhex(req.get("value") or "")
            bz.write(addr, char, value, bool(req.get("response", True)))
            return {}

        if op == "notify":
            enable = bool(req.get("enable"))
            bz.notify(addr, char, enable)
            with self.meta_lock:
                self.char_addr[bz.char_path(addr, char)] = (addr, char)
                if enable:
                    self.subscribed.add((addr, char))
                else:
                    self.subscribed.discard((addr, char))
            return {}

        raise BleError("unknown op %r" % op)

    def lookup_meta(self, which):
        with self.meta_lock:
            if which == "subscribed":
                return set(self.subscribed)
            return set(self.char_addr.items())

    def addr_of(self, path):
        """The device address behind a BlueZ object path, from memory when we
        have it, one enumeration when not -- and then remembered."""
        with self.meta_lock:
            if path in self.path_addr:
                return self.path_addr[path]
        try:
            d = self.main_bluez.objects().get(path, {}).get(DEVICE_IFACE, {})
        except dbus.DBusException:
            return ""
        addr = str(d.get("Address", "")).upper()
        if addr:
            with self.meta_lock:
                self.path_addr[path] = addr
        return addr

    # --------------------------------------------------------------- events

    def remember_paths(self):
        """Record path -> address for every device BlueZ currently has.

        Everything that sends a PropertiesChanged signal arrives first as an
        InterfacesAdded during a scan or an announce during pairing, so the
        cache is normally hit; this one crawl makes certain of it at startup
        and after a helper restart with devices already paired.
        """
        try:
            objs = self.main_bluez.objects()
        except dbus.DBusException:
            return
        prefix = self.main_bluez.adapter_path + "/"
        with self.meta_lock:
            for path, ifaces in objs.items():
                d = ifaces.get(DEVICE_IFACE)
                if d and path.startswith(prefix):
                    self.path_addr[path] = str(d.get("Address", "")).upper()

    def announce_known(self):
        """Report the devices already on the air when a scan starts.

        BlueZ only signals what changes, so a device that advertised a second
        before the game opened its pairing screen would otherwise stay
        invisible until it happened to move again.
        """
        sent = 0
        try:
            objs = self.main_bluez.objects()
        except dbus.DBusException as e:
            log("announce: %s", e.get_dbus_message())
            return
        prefix = self.main_bluez.adapter_path + "/"
        for path, ifaces in objs.items():
            d = ifaces.get(DEVICE_IFACE)
            if not d or not path.startswith(prefix):
                continue
            with self.meta_lock:
                self.path_addr[path] = str(d.get("Address", "")).upper()
            if "RSSI" not in d and not d.get("Connected"):
                continue
            self.advert(d)
            sent += 1
        trace("announced %d device(s) already on the air", sent)

    def advert(self, props):
        addr = str(props.get("Address", "")).upper()
        if not addr:
            return
        # The game drops any advertisement with no service UUIDs
        # (BluetoothProgram::Advertisement_Received), so this is where a sensor
        # silently fails to appear -- worth a line when tracing.
        uuids = [str(u).lower() for u in props.get("UUIDs", [])]
        with self.meta_lock:
            newly_silent = not uuids and addr not in self.silent
            if newly_silent:
                self.silent.add(addr)
        if newly_silent:
            trace("%s advertises no service UUIDs; the game will ignore it", addr)
        self.post({"ev": "advert",
                   "addr": addr,
                   "name": str(props.get("Alias", "") or ""),
                   "uuids": uuids,
                   "rssi": int(props.get("RSSI", 0))})

    def on_properties_changed(self, iface, changed, invalidated=None, path=None):
        # `invalidated` defaults because dbus-python calls this straight: a
        # sender omitting it would be a TypeError swallowed by the dispatch,
        # and on the char branch that drops heart-rate values on the floor.
        if iface == CHAR_IFACE and "Value" in changed:
            with self.meta_lock:
                known = self.char_addr.get(path)
            if not known:
                return
            addr, uuid = known
            with self.meta_lock:
                if (addr, uuid) not in self.subscribed:
                    return
            self.post({"ev": "value", "addr": addr, "char": uuid,
                       "value": bytes(bytearray(changed["Value"])).hex()})
            return

        if iface == ADAPTER_IFACE:
            if path == self.main_bluez.adapter_path and "Powered" in changed:
                # Powered can move from rfkill, from a desktop toggle, from the
                # user's checkbox -- anything.  The next per-second poll of the
                # same question is answered from here, not by asking again.
                self.refresh_radios()
            return

        if iface != DEVICE_IFACE:
            return

        # A device's own signal: connect/disconnect worth telling the game
        # about, and advertisement refresh while a scan is running.
        interesting = "Connected" in changed or (
            self.scanning and ("RSSI" in changed or "UUIDs" in changed or "Alias" in changed))
        if not interesting:
            return

        addr = self.addr_of(path)
        if not addr:
            return

        if "Connected" in changed:
            now = bool(changed["Connected"])
            with self.meta_lock:
                was = self.connected.get(addr)
                self.connected[addr] = now
                props_addr_known = addr in self.path_addr
            if was != now:
                self.post({"ev": "connection", "addr": addr, "connected": now})

        if self.scanning and ("RSSI" in changed or "UUIDs" in changed or "Alias" in changed):
            # The signal carries only what changed; the constant fields
            # (Address, Alias, UUIDs as a whole) come from the object itself.
            try:
                props = self.main_bluez.objects().get(path, {}).get(DEVICE_IFACE, {})
            except dbus.DBusException:
                return
            if props:
                self.advert(props)

    def on_interfaces_added(self, path, ifaces):
        if ADAPTER_IFACE in ifaces:
            self.refresh_radios()
            return
        d = ifaces.get(DEVICE_IFACE)
        if not d:
            return
        with self.meta_lock:
            self.path_addr[path] = str(d.get("Address", "")).upper()
        if self.scanning:
            self.advert(d)

    def on_interfaces_removed(self, path, ifaces):
        if str(path) == self.main_bluez.adapter_path and ADAPTER_IFACE in ifaces:
            # The scan flag is the Server's, shared with the worker: left set,
            # start_scan() would no-op once the adapter returns and the game's
            # scan would never start again.
            with self.scan_lock:
                self.scanning = False
            with self.meta_lock:
                self.subscribed.clear()
                self.char_addr.clear()
                self.silent.clear()
                lost = [addr for addr, connected in self.connected.items() if connected]
                self.connected.clear()
            for addr in lost:
                self.post({"ev": "connection", "addr": addr, "connected": False})
            self.refresh_radios()
            log("Bluetooth adapter %s disappeared; it is used again if it returns under that name, "
                "otherwise relaunch the game", self.main_bluez.adapter_path.rsplit("/", 1)[-1])
            return
        if DEVICE_IFACE not in ifaces:
            return
        # BlueZ removes a non-bonded device's object when it stops advertising,
        # which is what a sleeping trainer looks like.  The game hears about it
        # as a disconnection, and reconnecting is its decision, not ours.
        addr = ""
        with self.meta_lock:
            for a, was in list(self.connected.items()):
                if self.main_bluez.device_path(a) == path and was:
                    addr = a
                    self.connected[a] = False
        if addr:
            self.post({"ev": "connection", "addr": addr, "connected": False})


def list_devices(bluez):
    """Print what is on the air, so the Linux half can be checked on its own."""
    bluez.start_scan()
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(8, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
        # BlueZ can clear RSSI when discovery stops. Keep the observed snapshot
        # or --list can report nothing despite a successful scan.
        objects = bluez.objects()
    finally:
        bluez.stop_scan()

    rows = []
    for path, ifaces in sorted(objects.items()):
        d = ifaces.get(DEVICE_IFACE)
        if not d or not path.startswith(bluez.adapter_path + "/"):
            continue
        if "RSSI" not in d:
            continue
        rows.append((str(d.get("Address", "?")),
                     str(d.get("Alias", "") or ""),
                     int(d.get("RSSI", 0)),
                     [str(u).lower() for u in d.get("UUIDs", [])]))
    if not rows:
        print("nothing advertising -- wake the sensor up (pedal, or touch the strap)")
        return
    for addr, name, rssi, uuids in sorted(rows, key=lambda r: -r[2]):
        print("%s  %-28s %4d dBm  %s" % (addr, name or "(no name)", rssi,
                                         " ".join(u[4:8] for u in uuids) or "no service UUIDs"))


def main():
    global verbose
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--port", type=int, default=27019, help="loopback port to serve on")
    ap.add_argument("--adapter", default="auto", help="BlueZ adapter (default: the sole powered adapter)")
    ap.add_argument("--check-adapter", action="store_true",
                    help="print the selected powered adapter and exit without scanning")
    ap.add_argument("--list", action="store_true", help="scan, print what is on the air, exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    verbose = args.verbose

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        bus = dbus.SystemBus()
        objects = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE).GetManagedObjects()
        adapter = select_adapter(objects, args.adapter)
    except dbus.DBusException as e:
        raise SystemExit("cannot talk to BlueZ (%s) -- is bluetoothd running?" % e.get_dbus_name())
    except BleError as e:
        raise SystemExit(str(e))
    if args.check_adapter:
        print(adapter)
        return
    bluez = Bluez(bus, adapter)

    if args.list:
        list_devices(bluez)
        return

    server = Server(bluez, args.port)
    bus.add_signal_receiver(server.on_properties_changed,
                            dbus_interface=PROPS_IFACE,
                            signal_name="PropertiesChanged",
                            path_keyword="path")
    bus.add_signal_receiver(server.on_interfaces_added,
                            dbus_interface=OM_IFACE,
                            signal_name="InterfacesAdded")
    bus.add_signal_receiver(server.on_interfaces_removed,
                            dbus_interface=OM_IFACE,
                            signal_name="InterfacesRemoved")
    server.refresh_radios()
    server.remember_paths()
    server.listen()
    # The worker thread that takes every potentially slow BlueZ operation: it
    # builds its own D-Bus connection when it starts, which is why it is
    # started after the loop thread has its own bus in place.
    threading.Thread(target=server.worker_loop,
                     name="blehelper-worker", daemon=True).start()

    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        server.drop_client()
        log("stopped")


if __name__ == "__main__":
    main()
