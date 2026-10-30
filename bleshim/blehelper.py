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
import dbus.service
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
GATT_REBUILD_GAP = 90         # seconds between record lifts for one address

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
        """Path of the device whose address is `addr`, by any name it carries.

        Addresses in the protocol are what a client was shown: usually the
        BlueZ object-path name (`dev_61_C4...`), but after a bond BlueZ
        re-keys the record to the peripheral's identity address -- an
        iPhone's `Address` reads E8:5F... while the object keeps the RPA
        path it was discovered under.  The naming rule is tried first (the
        common case costs nothing extra); only when it misses do we crawl
        the tree for the property match, so a remembered identity address
        keeps connecting across key rotations.
        """
        direct = "%s/dev_%s" % (self.adapter_path, addr.upper().replace(":", "_"))
        if direct in self.objects():
            return direct
        want = addr.upper()
        for path, ifaces in sorted(self.objects().items()):
            if not path.startswith(self.adapter_path + "/dev_"):
                continue
            d = ifaces.get(DEVICE_IFACE)
            if d and str(d.get("Address", "")).upper() == want:
                return path
        return direct

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
        # iOS's bond records can leave ServicesResolved stuck false while the
        # tree is fully present (a link that lived through our own helper
        # restarts, say) -- the tree being there is the truth worth trusting.
        end = time.monotonic() + CONNECT_TIMEOUT
        prefix = path + "/"
        while True:
            try:
                if props.Get(DEVICE_IFACE, "ServicesResolved"):
                    break
            except dbus.DBusException:
                raise BleError("%s went away while resolving its services" % addr)
            if any(p.startswith(prefix) and SERVICE_IFACE in ifaces
                   for p, ifaces in self.objects().items()):
                break
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

    def lift_record(self, addr):
        """Drop the BlueZ device record entirely, the documented way to force
        a GATT re-discovery on the next connect.

        There is no refresh API on Device1, and BlueZ trusts whatever cache
        it holds: a record whose discovery once came back empty (a phone
        bridge whose app had closed, say) stays "resolved with nothing" and
        every later walk sees an empty tree forever.  Removing the record and
        connecting anew runs discovery from zero and writes a real cache --
        and it re-pairs on the way, which the pairing agent handles while the
        phone asks its owner to confirm once.  Real protocol, the only one
        that is honest about this trap.
        """
        self.adapter().RemoveDevice(dbus.ObjectPath(self.device_path(addr)))
        log("%s: record removed -- the next connect re-pairs and rediscovers", addr)

    # ----------------------------------------------------------------- gatt

    # Some devices carry two services with the same UUID.  The Apple watch
    # does it deliberately: its private heart-rate service reads and notifies
    # over an encrypted link (a no-bond client gets InsufficientEncryption),
    # while the broadcast instance it raises during a workout carries a
    # notify-only 2a37 whose values are free -- that is the channel aimed at
    # third-party receivers like this game.  The protocol carries no service
    # path, only UUIDs, so with duplicates in the tree every op used to
    # merge both instances' characteristics into one list (two 2a37s and two
    # 2a38s under one roof, plenty to break the game's enumeration) and act
    # on whichever object sorted first.  Now one instance representative per
    # duplicated UUID is chosen -- the one most likely to work without a
    # bond -- and every listing, read, write and subscribe acts on that
    # same view of the device.

    def services(self, addr):
        """One representative per service UUID, in tree order.

        The game walks every service it is shown and keeps its own map keyed
        by UUID; a peripheral hosting two instances of one service (the
        watch's two 180d) handed it the same key twice.  Windows, where this
        code was written, shows a client one object per service, so answer
        the same: characteristics() picks its representative with the same
        score, keeping the two views coherent.
        """
        prefix = self.device_path(addr) + "/"
        best = {}
        for path, ifaces in sorted(self.objects().items(), reverse=True):
            svc = ifaces.get(SERVICE_IFACE)
            if svc and path.startswith(prefix):
                uuid = str(svc["UUID"]).lower()
                score = self.instance_score(path)
                if uuid not in best or score > best[uuid]:
                    best[uuid] = score
        out, shown = [], set()
        for path, ifaces in sorted(self.objects().items()):
            svc = ifaces.get(SERVICE_IFACE)
            if svc and path.startswith(prefix):
                uuid = str(svc["UUID"]).lower()
                if uuid not in shown:
                    shown.add(uuid)
                    out.append({"uuid": uuid})
        return out

    def service_paths(self, addr, service_uuid):
        prefix = self.device_path(addr) + "/"
        return sorted(path for path, ifaces in self.objects().items()
                      if path.startswith(prefix)
                      and SERVICE_IFACE in ifaces
                      and str(ifaces[SERVICE_IFACE]["UUID"]).lower() == service_uuid.lower())

    def _instance_chars(self, service_path):
        return [ch for ch in (ifaces.get(CHAR_IFACE)
                              for path, ifaces in sorted(self.objects().items())
                              if str(ifaces.get(CHAR_IFACE, {}).get("Service", ""))
                              == service_path)
                if ch]

    def instance_score(self, service_path):
        """Rank a service instance among same-UUID siblings.

        An instance able to notify through a property that is not a read
        (Apple's broadcast pattern: a notify-only 2a37) outranks one that
        cannot; any notifier beats none; equal scores keep the tree order.
        """
        best = 1
        for ch in self._instance_chars(service_path):
            flags = {str(f) for f in ch.get("Flags", [])}
            if flags & {"notify", "indicate"}:
                best = max(best, 2 if flags & {"read"} else 3)
        return best

    def characteristics(self, addr, service_uuid):
        paths = self.service_paths(addr, service_uuid)
        if not paths:
            return []
        chosen = paths[0]
        if len(paths) > 1:
            # Ties take the newest path: a peripheral raises its broadcast
            # instance after its private one, so last seen is the live one.
            chosen = max(sorted(paths, reverse=True), key=self.instance_score)
        out = []
        for path, ifaces in sorted(self.objects().items()):
            ch = ifaces.get(CHAR_IFACE)
            if not ch or str(ch["Service"]) != chosen:
                continue
            out.append({"uuid": str(ch["UUID"]).lower(),
                        "flags": [str(f) for f in ch.get("Flags", [])]})
        return out

    # Flags an ask can require.  The shim sends only an address and a UUID,
    # so for a duplicated UUID the best instance for each intention is
    # resolved here; for single instances this is a no-op pass-through.
    WANT_FLAGS = {
        "notify": ("notify", "indicate"),
        "read": ("read",),
        "write": ("write", "write-without-response", "reliable-write"),
    }

    def char_candidates(self, addr, char_uuid, want=None):
        """Char paths sharing a UUID, the best for the ask first.

        Ranking rides the service instance's score, so the instance the
        characteristics list presents is also the one the read, the write
        and the subscribe land on; among those, candidates actually
        carrying the ask's flags go before candidates that do not.
        """
        prefix = self.device_path(addr) + "/"
        found = []
        for path, ifaces in sorted(self.objects().items(), reverse=True):
            ch = ifaces.get(CHAR_IFACE)
            if not ch or not path.startswith(prefix):
                continue
            if str(ch["UUID"]).lower() != char_uuid.lower():
                continue
            flags = {str(f) for f in ch.get("Flags", [])}
            wanted = self.WANT_FLAGS.get(want, ())
            fits = not wanted or bool(flags & set(wanted))
            found.append((0 if fits else 1, -self.instance_score(str(ch["Service"])), path))
        if not found:
            raise BleError("%s has no characteristic %s" % (addr, char_uuid))
        found.sort(key=lambda t: t[:2])
        return [path for _, _, path in found]

    def char_path(self, addr, char_uuid, want=None):
        return self.char_candidates(addr, char_uuid, want)[0]

    def read(self, addr, char_uuid):
        path = self.char_path(addr, char_uuid, "read")
        try:
            value = dbus.Interface(self.bus.get_object(BLUEZ, path), CHAR_IFACE).ReadValue({})
        except dbus.DBusException as e:
            raise BleError("ReadValue failed: %s" % e.get_dbus_message())
        return bytes(bytearray(value))

    def write(self, addr, char_uuid, value, with_response):
        options = {"type": "request" if with_response else "command"}
        path = self.char_path(addr, char_uuid, "write")
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, path), CHAR_IFACE).WriteValue(
                dbus.Array(value, signature="y"), options)
        except dbus.DBusException as e:
            raise BleError("WriteValue failed: %s" % e.get_dbus_message())

    def notify(self, addr, char_uuid, enable):
        """Subscribing is where duplicates bite: an encrypted instance
        refuses a no-bond ask with InsufficientEncryption while the sibling
        instance -- the broadcast one, on a watch running a workout -- would
        have taken the same ask.  Walk the candidates in rank order; the
        first that subscribes is returned so the event mapping records the
        object that actually carries the values."""
        method = "StartNotify" if enable else "StopNotify"
        cands = self.char_candidates(addr, char_uuid, "notify" if enable else None)
        failure = None
        for path in cands:
            intf = dbus.Interface(self.bus.get_object(BLUEZ, path), CHAR_IFACE)
            try:
                getattr(intf, method)()
                return path
            except dbus.DBusException as e:
                # Subscribing twice is the game re-arming a sensor it already
                # has, which is not a failure worth reporting up.
                if e.get_dbus_name() == "org.bluez.Error.InProgress" and enable:
                    trace("%s %s already notifying", addr, char_uuid)
                    return path
                failure = e
                if e.get_dbus_name() in ("org.bluez.Error.InsufficientEncryption",
                                         "org.bluez.Error.InsufficientAuthorization",
                                         "org.bluez.Error.InsufficientAuthentication",
                                         "org.bluez.Error.AuthenticationRejected"):
                    continue
                break
        raise BleError("%s failed: %s" % (method, failure.get_dbus_message()))


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

        # One GATT rebuild per address per gap at most: the heal removes a
        # record and re-pairs, and doing that twice in a row for one tap
        # would lift a record that is already healthy.
        self.gatt_heal = {}
        self.gatt_heal_lock = threading.Lock()

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

        The game going away must not let a scan run on: the next process to
        use the adapter would inherit it.  Jobs still queued are discarded;
        the job already running on the worker finishes, and its reply finding
        no client is send()'s no-op.

        Notifications, though, are kept: a common way to reach this path is
        the helper itself being replaced (`deploy.sh` plus a restart), with
        the same shim reconnecting moments later.  Stopping every CCCD then
        would freeze the trainer fields for a game that believes itself
        subscribed and never re-asks.  restore_notifications() rebuilds this
        process's gates from BlueZ's own Notifying flags on the next start,
        so the state stays truthful across successors; whoever wants things
        quiet stops them explicitly (the helper exit path keeps them on for
        the future instance to pick up, as on exit the choices are all
        "a new helper" or "a new boot" -- on a new boot BlueZ starts clean).
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
        # The gates (subscribed/char_addr) stay put: the usual way this runs
        # is the helper being swapped, with the same shim reconnecting a few
        # seconds later -- a game mid-ride never re-asks for a CCCD it
        # believes is active, so its values must not be gated off.  The next
        # helper START re-derives these maps from BlueZ's Notifying flags
        # (restore_notifications), which keeps them from drifting stale; the
        # only case left unpicked-up is "game gone forever", where the
        # notifications idle harmlessly against a socket nobody reads.
        with self.meta_lock:
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
                # must outlive anything one request can do: a request that
                # has already been read will be timed out by the shim, but a
                # reader that dies leaves every later request unanswered
                # while the game waits silently.
                log("unhandled on %r: %s: %s", line[:80], type(e).__name__, e)
        return True

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
            # The main loop owes the shim a reply for every request it reads;
            # an unexpected exception here must be refused, not absorbed --
            # a swallowed exception answers nothing, and a game in waiting
            # sits out its whole timeout (this exact shape is how the
            # 2026-10-29 freeze opened, via a mis-merged Bluez field).
            except Exception as e:                   # noqa: BLE001
                log("scan: unexpected: %s: %s", type(e).__name__, e)
                self.send({"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e)})
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
        desc = {k: v for k, v in req.items() if k != "id"}
        trace("%s queued (id %s) %s", op, rid, json.dumps(desc, sort_keys=True))
        self.work.put((req, rid))

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
            reply_blob = json.dumps({k: v for k, v in reply.items() if k != "id"},
                                    sort_keys=True, default=str)
            trace("-> id %s op %s: %s", rid, op, reply_blob[:400])
            self.post(reply)

    def maybe_rebuild_gatt(self, bz, addr):
        """Empty GATT on a paired device: lift the record, pair again, and
        give the tree a second life.

        Runs on the worker thread after a `services` came back empty for a
        device BlueZ still calls paired: BlueZ is then trusting a resolved-
        but-empty cache and will never discover again on its own (the trap
        the phone-bridge walked into days after its first pairing - the app
        had closed when the record was minted, the cache minted empty, and
        every later walk read `[]`).  The lift makes the next connect run a
        real discovery and hand out a fresh cache; the pairing agent carries
        the SMP half, and the phone asks its owner to confirm once.

        This never delays the walk that found the emptiness: the job had
        already answered its caller honestly before reaching here, and the
        NEXT walk of this address sees the rebuilt tree.  One rebuild per
        address per GATT_REBUILD_GAP keeps a sleeping phone from losing its
        bond in a tap-loop; a rebuild that stopped at "not on the air" leaves
        the record lifted and then watches for its bridge, so the re-pairing
        runs the moment the phone app wakes -- no cache command by hand, no
        extra tap in the game, only the prompt to accept.
        """
        with self.gatt_heal_lock:
            now = time.monotonic()
            if now - self.gatt_heal.get(addr, 0.0) < GATT_REBUILD_GAP:
                trace("%s: a GATT rebuild is still resting within the gap", addr)
                return
        if not bz.device_props(addr).get("Paired"):
            return
        with self.gatt_heal_lock:
            self.gatt_heal[addr] = time.monotonic()
        log("%s: paired but its GATT tree reads empty -- lifting the record "
            "to force a rediscovery", addr)
        try:
            bz.lift_record(addr)
        except dbus.DBusException as e:
            log("%s: record lift refused (%s); keeping what BlueZ has", addr,
                e.get_dbus_message())
            return
        try:
            name = bz.connect(addr)
            count = len(bz.services(addr))
            if count:
                log("%s: re-paired as %r, %d service(s) discovered -- "
                    "a next walk of this sensor serves the tree", addr, name, count)
            else:
                log("%s: re-paired as %r but the tree came up empty again -- "
                    "the bridge side is not serving yet", addr, name)
        except (BleError, dbus.DBusException) as e:
            log("%s: no rebuild yet (%s); the record stays lifted and this "
                "watch pairs on sight of its bridge", addr, e)
            # The usual miss: the bridge is asleep (the phone app closed, iOS
            # suspended it).  The record is already lifted, so nothing about
            # the cache stays rotten -- the only thing left to do is pair,
            # and the pairing prompt is the phone's to answer.  Watch for the
            # bridge's return and carry the whole second half then, so that
            # opening the phone app is the user's whole part: they accept the
            # prompt, the tree rebuilds, the next walk serves it.  Bounded by
            # the same gap that rate-limits repeats, so a bridge that never
            # comes cannot hold the worker longer than its own window, and a
            # later walk of this sensor re-pairs the same way.
            if not bz.on_air(addr) and bz.wait_for_advert(addr, GATT_REBUILD_GAP,
                                                          getattr(bz, "host", None)):
                try:
                    name = bz.connect(addr)
                    count = len(bz.services(addr))
                    log("%s: the bridge returned -- re-paired as %r, %d "
                        "service(s) discovered; the next walk serves the tree",
                        addr, name, count)
                except (BleError, dbus.DBusException) as e2:
                    log("%s: re-pair on its return refused (%s); a later walk "
                        "of this sensor re-pairs", addr, e2)

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
            out = bz.services(addr)
            if not out:
                # An empty tree on a paired device is BlueZ trusting a
                # resolved-but-empty cache; the lift (worker-side) makes the
                # NEXT walk of this sensor discover for real.  This reply
                # still leaves right now.
                self.maybe_rebuild_gatt(bz, addr)
            return {"services": out}

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
            used = bz.notify(addr, char, enable)
            with self.meta_lock:
                self.char_addr[used] = (addr, char)
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

    def restore_notifications(self):
        """Rebuild char_addr/subscribed from BlueZ's Notifying flags.

        A helper restart is invisible to the game -- the shim holds its own
        connection state and never re-asks for a CCCD it believes is active.
        But the CCCD lives on, on BlueZ's side: a trainer that was streaming
        keeps streaming into this process, whose gate (`subscribed`) was
        wiped, so every packet was being dropped before reaching the game.
        Walking the tree for characteristics BlueZ reports as Notifying puts
        that state back and the values resume without a device round-trip.
        """
        try:
            objs = self.main_bluez.objects()
        except dbus.DBusException as e:
            log("notify restore: %s", e.get_dbus_message())
            return
        device_prefixes = sorted(
            (path for path in objs if "/dev_" in path and DEVICE_IFACE in objs[path]),
            key=len, reverse=True)
        restored = []
        with self.meta_lock:
            for path, ifaces in objs.items():
                c = ifaces.get(CHAR_IFACE)
                if not c or not c.get("Notifying"):
                    continue
                owner = next((dev for dev in device_prefixes
                              if path.startswith(dev + "/")), None)
                if owner is None:
                    continue
                addr = objs[owner][DEVICE_IFACE].get("Address")
                if not addr:
                    continue
                addr = str(addr).upper()
                uuid = str(c["UUID"]).lower()
                self.char_addr[path] = (addr, uuid)
                self.path_addr[owner] = addr
                self.subscribed.add((addr, uuid))
                restored.append("%s/%s" % (addr, uuid[:8]))
        if restored:
            log("notify state restored for %d characteristic(s): %s",
                len(restored), ", ".join(restored))

    def notif_state_loop(self):
        """Periodically re-arm subscriptions BlueZ let fall over.

        Protocol-wise a subscription has three keepers: the game's belief,
        the helper's gate and the CCCD on BlueZ's side.  Helper hand-offs,
        suspend races or a bluetoothd hiccup can tear one while the others
        stand; a game that believes itself subscribed never re-asks, so the
        movement must come from here: a Notifying seen false while the link
        is up gets the same StartNotify the game's subscribe would have
        issued.  Nothing is triggered while everything agrees -- the pass
        only ever logs when it actually re-arms something.
        """
        time.sleep(12)              # let whatever connects connect first
        while True:
            try:
                self.heal_notifications()
            except Exception as e:  # noqa: BLE001 -- the watcher survives all
                log("self-heal pass failed: %r", e)
            time.sleep(10)

    def heal_notifications(self):
        with self.meta_lock:
            pairs = list(self.subscribed)
        if not pairs:
            return
        objs = self.main_bluez.objects()
        for addr, uuid in pairs:
            try:
                path = self.main_bluez.char_path(addr, uuid)
            except (BleError, IndexError):
                continue
            ch = objs.get(path, {}).get(CHAR_IFACE)
            if not ch:
                continue
            with self.meta_lock:
                if path not in self.char_addr:
                    self.char_addr[path] = (addr, uuid)
            device_path = self.main_bluez.device_path(addr)
            d = objs.get(device_path, {}).get(DEVICE_IFACE)
            if not d or not d.get("Connected"):
                continue
            if ch.get("Notifying"):
                continue
            try:
                self.main_bluez.notify(addr, uuid, True)
                log("self-heal: re-armed %s %s", addr, uuid)
            except (BleError, dbus.DBusException) as e:
                log("self-heal: StartNotify(%s, %s) failed: %s", addr, uuid, e)

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
        if invalidated is None:
            # Never seen from BlueZ, whose PropertiesChanged always carries
            # the third argument -- but a 2026-10-30 session had a two-
            # argument PropertiesChanged land here and crash the handler:
            # dbus-python calls it straight, a positional shortfall is a
            # TypeError in the loop's signal dispatch, and for the char
            # event that carries the heart rate that would strand every
            # later subscription.  Name it and carry on.
            trace("PropertiesChanged on %s %s: no invalidated list", path, iface)
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
            # Heart-rate notifications are the one stream worth a trace line:
            # the trainers' fields repeat several times a second and would
            # flood the log, so only 2a37 is named, with its decoded BPM.
            if uuid.startswith("00002a37"):
                val = bytes(bytearray(changed["Value"]))
                flags = val[0] if val else 0
                if len(val) >= 3 and flags & 0x01:
                    bpm = val[1] | (val[2] << 8)
                elif len(val) >= 2:
                    bpm = val[1]
                else:
                    bpm = 0
                trace("value %s 00002a37 = %d bpm (flags 0x%02x)", addr, bpm, flags)
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


# ------------------------------------------------------------------ agent
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MANAGER = "org.bluez.AgentManager1"
AGENT_PATH = "/org/bluez/blehelper/agent"


class PairingAgent(dbus.service.Object):
    """org.bluez.Agent1 answering the SMP exchanges BlueZ cannot sit through.

    The sensors that stream heart rate over an encrypted characteristic make
    the link demand pairing the moment the game writes the CCCD.  BlueZ then
    looks for an agent to carry the exchange; with none registered on this
    desktop the SMP stalls, the peripheral drops the link, and the game's
    subscribe dies with NotConnected.  This agent carries it instead: passkeys
    and confirmations are logged verbatim (so the code shown on the phone can
    be compared) while the machine-side answer is always accept -- a trainer
    or a watch bridge is not a MITM target worth a full pairing every cycle.
    """

    def __init__(self, bus):
        dbus.service.Object.__init__(self, bus, AGENT_PATH)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        log("agent: released by BlueZ")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log("agent: RequestPinCode %s -> 000000", device)
        return "000000"

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pin):
        log("agent: DisplayPinCode %s %s", device, pin)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        log("agent: RequestPasskey %s -> replying 0 (no keyboard here)", device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouu", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log("agent: ★ passkey for %s: %06d", device, passkey)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        # The phone shows the same six digits and waits for its user; our side
        # confirms on its own so the exchange never waits on the console.
        log("agent: ★★ pairing asked for %s, code %06d -- accepted", device, passkey)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log("agent: RequestAuthorization %s -> accepted", device)

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log("agent: AuthorizeService %s %s -> accepted", device, uuid)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        log("agent: exchange cancelled by BlueZ")


def register_pairing_agent(bus):
    """Register with AgentManager1; failure is logged and non-fatal.

    A desktop running another agent keeps its own: both registries can hold
    agents, BlueZ simply picks one -- ours exists so bluetoothd always finds
    somebody, which is the whole job.
    """
    try:
        agent = PairingAgent(bus)
        manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                                 AGENT_MANAGER)
        manager.RegisterAgent(AGENT_PATH, "DisplayYesNo")
        manager.RequestDefaultAgent(AGENT_PATH)
        log("agent: registered at %s (DisplayYesNo, machine side auto-accepts; "
            "passkeys are logged here)", AGENT_PATH)
        return agent
    except dbus.DBusException as e:
        log("agent: unavailable (%s) -- encrypted characteristics on the phone "
            "side will keep dropping the link", e.get_dbus_message())
        return None


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

    register_pairing_agent(bus)

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
    server.restore_notifications()
    server.listen()
    # The worker thread that takes every potentially slow BlueZ operation: it
    # builds its own D-Bus connection when it starts, which is why it is
    # started after the loop thread has its own bus in place.
    threading.Thread(target=server.worker_loop,
                     name="blehelper-worker", daemon=True).start()
    threading.Thread(target=server.notif_state_loop,
                     name="blehelper-selfheal", daemon=True).start()

    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        server.drop_client()
        log("stopped")


if __name__ == "__main__":
    main()
