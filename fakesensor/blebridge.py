#!/usr/bin/env python3
"""
Serve a real Bluetooth LE trainer over Wahoo Direct Connect, from Linux.

MyWhoosh's WD_* path talks Dircon to a TCP socket (see ../dircon/README.md), and
../fakesensor/fakebonjour.c makes the game discover a sensor at 127.0.0.1 without
any mDNS.  That pair proved the loop with hard-coded numbers.  This is the same
socket, backed by actual hardware: it connects to a BLE fitness device through
BlueZ -- on the Linux side, where Bluetooth works, instead of through Wine's
WinRT stack, which does not (../README.md, blocker 1) -- and proxies its GATT
tree onto the wire.

Dircon maps onto GATT almost one to one, which is why this is a proxy and not a
translation: msgId 1 lists services, 2 lists a service's characteristics, 3
reads, 4 writes, 5 subscribes, and 6 is a notification pushed by the server.  So
every service the trainer really has is what the game really sees -- including
its control point, so resistance writes reach the hardware.

A trainer that stops being pedalled drops its radio, and BlueZ then removes the
device entirely; the bridge waits for it to advertise again, reconnects and
restores the client's subscriptions, leaving the Dircon socket up so the game
never sees its device go away.

A heart-rate strap joins the trainer on the same socket: --hr-mac connects it
too and its heart-rate service goes onto the one Dircon endpoint.  It has to be
that way round -- the game holds one Direct Connect connection at a time, and
pairs a second slot by matching serials against the sensor it already has (see
Bridge).  Each device is still advertised under its own name, so the trainer's
slots list the trainer and the heart-rate slot lists the strap; behind both is
the one connection.  MyWhoosh will also not offer a Direct Connect sensor for
the heart-rate slot by itself -- see ../exportshim/ExportShim.cs, ScanRescue --
so the shim has to be installed for the strap to appear.

Usage:
    ./blebridge.py                      # pick the first fitness device seen
    ./blebridge.py --mac FA:55:E5:BE:21:A5 --port 36866
    ./blebridge.py --mac FA:55:E5:BE:21:A5 --hr-mac D1:23:6E:0C:47:B8
    ./blebridge.py --list               # scan and print candidates, then exit

Start this before the game: on connect it writes each device's name, serial,
address, port and capabilities into <prefix>/drive_c/fakesensor-device, which
fakebonjour reads when the game loads it, so nothing has to be configured per
trainer.  The file also tells the DLL not to bind the Dircon ports itself, tells
the shim which sensor can fill which pairing slot, and is removed on exit.  Pass --prefix if the prefix cannot be guessed, or --no-handshake to go
back to advertising through FAKESENSOR_NAME/SERIAL/PORT in the game's
environment (printed on startup in that case).
"""

import argparse
import atexit
import errno
import glob
import os
import socket
import struct
import time
import sys
import uuid

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


def u16(x):
    return "%08x-0000-1000-8000-00805f9b34fb" % x


# Indoor Bike Data (0x2AD2) flags, LSB first.  Worth decoding rather than
# guessing: MyWhoosh reads exactly these bits on the first notification to
# decide what the trainer can provide, and only then starts reading the values.
# Notably cadence -- DirconSensor.ProcessIndoorBikeDataNotification sets
# hasCadenceFrom2AD2 from bit 2, SetDevicePreference turns that into
# getCadenceFrom2AD2, and without it the cadence field is never parsed at all.
IBD_FLAGS = [
    (0, "InstantaneousSpeed", True),   # bit 0 is "More Data": 0 means speed present
    (1, "AverageSpeed", False),
    (2, "InstantaneousCadence", False),
    (3, "AverageCadence", False),
    (4, "TotalDistance", False),
    (5, "ResistanceLevel", False),
    (6, "InstantaneousPower", False),
    (7, "AveragePower", False),
    (8, "ExpendedEnergy", False),
    (9, "HeartRate", False),
    (10, "MetabolicEquivalent", False),
    (11, "ElapsedTime", False),
    (12, "RemainingTime", False),
]


def describe_ibd(value):
    """What an Indoor Bike Data notification says it carries, and the fields
    MyWhoosh cares about, parsed the way the game parses them."""
    if len(value) < 2:
        return "short (%d bytes)" % len(value)
    flags = value[0] | (value[1] << 8)
    present = [name for bit, name, inverted in IBD_FLAGS
               if bool(flags >> bit & 1) != inverted]

    # Fields appear in flag order; walk them to find cadence and power.
    off, cadence, power = 2, None, None
    for bit, name, inverted in IBD_FLAGS:
        if bool(flags >> bit & 1) == inverted:
            continue
        size = {"TotalDistance": 3, "ExpendedEnergy": 6, "HeartRate": 1,
                "MetabolicEquivalent": 1}.get(name, 2)
        if name == "InstantaneousCadence" and off + 2 <= len(value):
            cadence = (value[off] | (value[off + 1] << 8)) / 2.0   # 0.5 rpm units
        if name == "InstantaneousPower" and off + 2 <= len(value):
            power = int.from_bytes(value[off:off + 2], "little", signed=True)
        off += size

    out = "flags=0x%04x [%s]" % (flags, " ".join(present))
    if power is not None:
        out += " power=%dW" % power
    if cadence is not None:
        out += " cadence=%.0frpm" % cadence
    return out


# The services worth exposing to a cycling game.  Everything else on a trainer
# is either plumbing (GAP/GATT, device info) or vendor private, and offering it
# only gives the game more to misparse.  --all overrides.
FITNESS_SERVICES = {
    u16(0x1818): "Cycling Power",
    u16(0x1816): "Cycling Speed and Cadence",
    u16(0x1826): "Fitness Machine",
    u16(0x180D): "Heart Rate",
    u16(0x180F): "Battery",
}

# The characteristics a pairing slot actually needs.  MyWhoosh decides what a
# sensor can do from the GATT tree it finds, so reporting the same thing to the
# shim (through the handshake) is what keeps a strap out of the trainer list and
# a power-only trainer out of the heart-rate list -- neither of which the game
# can tell apart before it has connected.
CH_POWER_MEASUREMENT = u16(0x2A63)
CH_INDOOR_BIKE_DATA = u16(0x2AD2)
CH_CSC_MEASUREMENT = u16(0x2A5B)
CH_FTMS_CONTROL = u16(0x2AD9)
CH_HEART_RATE = u16(0x2A37)

# Dircon's own property bits, which are not GATT's.
DP_READ, DP_WRITE, DP_NOTIFY = 1, 2, 4

# BlueZ's ReadValue/WriteValue take an a{sv} of options.  Ours is always empty,
# and dbus-python cannot guess a signature from an empty dict, so say it here.
NO_OPTIONS = dbus.Dictionary({}, signature="sv")

MSG_SERVICES, MSG_CHARS, MSG_READ, MSG_WRITE, MSG_NOTIFY_ENABLE, MSG_NOTIFICATION = 1, 2, 3, 4, 5, 6

RC_OK, RC_UNEXPECTED, RC_UNKNOWN_SERVICE, RC_UNKNOWN_CHAR, RC_UNSUPPORTED = 0, 1, 2, 3, 4


def log(fmt, *a):
    sys.stderr.write("bridge: " + (fmt % a if a else fmt) + "\n")
    sys.stderr.flush()


# The DLL reads its advertised identity from this file, inside the prefix, at
# load time; see ../fakesensor/fakebonjour.c (HANDSHAKE_PATH).  Writing it here
# is what spares a user from having to put their own trainer's name, serial and
# MAC into the environment of whatever launches the game: the bridge knows all
# three the moment it connects, and the game is started afterwards.
HANDSHAKE = "fakesensor-device"


def find_prefix():
    """A Wine prefix with fakebonjour installed, or None if we cannot tell.

    Preferring the marker the installer leaves (dotlocal_shim.so) over any
    directory that merely looks like a prefix keeps this from picking one of the
    user's other games.
    """
    if os.environ.get("WINEPREFIX"):
        return os.environ["WINEPREFIX"]
    seen, found = set(), []
    cands = []
    for yml in glob.glob(os.path.expanduser("~/.config/lutris/games/*.yml")):
        try:
            for line in open(yml):
                line = line.strip()
                if line.startswith("prefix:"):
                    cands.append(os.path.expanduser(line.split(":", 1)[1].strip()))
        except OSError:
            pass
    cands += sorted(glob.glob(os.path.expanduser("~/Games/*/")))
    for c in cands:
        c = c.rstrip("/")
        if c in seen:
            continue
        seen.add(c)
        if os.path.exists(os.path.join(c, "dotlocal_shim.so")):
            found.append(c)
    if len(found) == 1:
        return found[0]
    if not found:
        log("no prefix with fakebonjour installed found; pass --prefix to write "
            "the handshake, or set FAKESENSOR_* in the game's environment")
    else:
        log("several prefixes have fakebonjour installed, pass --prefix to pick one:")
        for f in found:
            log("    --prefix %s", f)
    return None


def write_handshake(prefix, devices):
    """Hand the DLL these sensors' identities, and take them back on the way out.

    One `name=' record per offered sensor, in order; a record's `caps=' is what
    the export shim filters the game's scan list on.  Several records can share
    a port and a serial -- that is how one connection is offered under each of
    its devices' names.
    """
    path = os.path.join(prefix, "drive_c", HANDSHAKE)
    body = "".join("name=%s\nserial=%d\nmac=%s\nport=%d\ncaps=%s\n"
                   % (d["name"], d["serial"], d["mac"], d["port"], ",".join(d["caps"]))
                   for d in devices)
    try:
        with open(path, "w") as f:
            f.write(body)
    except OSError as e:
        log("cannot write %s: %s", path, e)
        return
    for d in devices:
        log("handshake %s: name=%s serial=%d port=%d caps=%s",
            path, d["name"], d["serial"], d["port"], ",".join(d["caps"]) or "none")

    def clean():
        try:
            os.unlink(path)
        except OSError:
            pass
    atexit.register(clean)


def caps_for(chars):
    """Which MyWhoosh pairing slots a set of characteristics can fill, by name.

    Cadence is claimed for the power and indoor-bike characteristics as well as
    the CSC one, because all three can carry crank data and the game works out
    which at runtime, from the first notification's flags.
    """
    caps = []
    if chars & {CH_POWER_MEASUREMENT, CH_INDOOR_BIKE_DATA}:
        caps.append("power")
    if chars & {CH_POWER_MEASUREMENT, CH_INDOOR_BIKE_DATA, CH_CSC_MEASUREMENT}:
        caps.append("cadence")
    if CH_FTMS_CONTROL in chars:
        caps.append("controllable")
    if CH_HEART_RATE in chars:
        caps.append("hr")
    return caps


def dircon_props(flags):
    p = 0
    if "read" in flags:
        p |= DP_READ
    if "write" in flags or "write-without-response" in flags:
        p |= DP_WRITE
    if "notify" in flags or "indicate" in flags:
        p |= DP_NOTIFY
    return p


class Peer:
    """One real BLE device, and the part of the Dircon service list it owns."""

    def __init__(self, bus, adapter, mac, expose_all):
        self.bus = bus
        self.adapter_path = "/org/bluez/%s" % adapter
        self.mac = mac.upper() if mac else None
        self.expose_all = expose_all
        self.name = None
        self.dev_path = None
        self.services = {}        # service uuid -> [(char uuid, props, char path)]
        self.by_char = {}         # char uuid -> char path
        self.traced = set()       # char uuids already logged once
        self.traced_at = {}       # ... and when they were last logged
        self.bridge = None        # the Dircon endpoint serving us
        self.watching = False     # PropertiesChanged receiver installed
        self.reconnecting = False
        self.om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)

    # ---------------------------------------------------------------- BlueZ

    def objects(self):
        return self.om.GetManagedObjects()

    def candidates(self):
        out = []
        for path, ifaces in self.objects().items():
            d = ifaces.get(DEVICE_IFACE)
            if not d:
                continue
            advertised = [str(u).lower() for u in d.get("UUIDs", [])]
            if any(u in FITNESS_SERVICES for u in advertised):
                out.append((path, str(d.get("Address", "?")), str(d.get("Alias", "?")), advertised))
        return out

    def scan(self, seconds, want_mac):
        adapter = dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE)
        try:
            adapter.SetDiscoveryFilter({"Transport": "le"})
        except dbus.DBusException as e:
            log("discovery filter refused (%s), scanning anyway", e.get_dbus_name())
        try:
            adapter.StartDiscovery()
        except dbus.DBusException as e:
            if e.get_dbus_name() != "org.bluez.Error.InProgress":
                raise
        loop = GLib.MainLoop()

        def found():
            if want_mac:
                if self.advertising(self.device_path(want_mac)):
                    loop.quit()
                    return False
            elif self.candidates():
                loop.quit()
                return False
            return True

        GLib.timeout_add(500, found)
        GLib.timeout_add_seconds(seconds, lambda: (loop.quit(), False)[1])
        loop.run()
        try:
            adapter.StopDiscovery()
        except dbus.DBusException:
            pass

    def device_path(self, mac):
        return "%s/dev_%s" % (self.adapter_path, mac.replace(":", "_"))

    def advertising(self, path):
        """Has an advertisement from this device actually arrived?

        BlueZ keeps an object for any device it has seen before, so being in
        the tree says nothing about being on the air -- and a trainer asleep in
        the tree costs a 30s Connect() timeout.  RSSI is set from a real
        advertisement received during discovery, and dropped when discovery
        stops, which is the distinction we need.
        """
        d = self.objects().get(path, {}).get(DEVICE_IFACE, {})
        return "RSSI" in d or bool(d.get("Connected"))

    def connect(self):
        """Find the device, connect, and wait for its GATT tree."""
        if self.mac:
            self.dev_path = self.device_path(self.mac)
            if not self.advertising(self.dev_path):
                log("waiting for %s to advertise", self.mac)
                self.scan(30, self.mac)
            if not self.advertising(self.dev_path):
                raise SystemExit(
                    "no advertisement from %s in 30s -- a trainer asleep looks "
                    "exactly like this, so turn the cranks and try again" % self.mac)
        else:
            if not self.candidates():
                log("scanning for a fitness device")
                self.scan(20, None)
            found = self.candidates()
            if not found:
                raise SystemExit("no BLE fitness device found; try --mac")
            self.dev_path, addr, alias, _ = found[0]
            self.mac = addr
            log("picked %s (%s)", alias, addr)

        props = dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path), PROPS_IFACE)
        dev = dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path), DEVICE_IFACE)
        if not props.Get(DEVICE_IFACE, "Connected"):
            log("connecting to %s", self.mac)
            dev.Connect()
        try:
            # A trainer that is not trusted is one BlueZ will not reconnect on
            # its own, and this one drops its radio the moment you stop pedalling.
            if not props.Get(DEVICE_IFACE, "Trusted"):
                props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
        except dbus.DBusException:
            pass

        loop = GLib.MainLoop()
        state = {"ok": False}

        def resolved():
            if not props.Get(DEVICE_IFACE, "ServicesResolved"):
                return True
            state["ok"] = True
            loop.quit()
            return False

        GLib.timeout_add(300, resolved)
        GLib.timeout_add_seconds(25, lambda: (loop.quit(), False)[1])
        loop.run()
        if not state["ok"]:
            raise SystemExit("connected to %s but its services never resolved" % self.mac)

        self.load_gatt()
        self.name = str(props.Get(DEVICE_IFACE, "Alias"))
        log("%s connected: %d service(s) exposed", self.name, len(self.services))
        return self.name

    def load_gatt(self):
        objs = self.objects()
        prefix = self.dev_path + "/"
        for path in sorted(objs):
            if not path.startswith(prefix):
                continue
            svc = objs[path].get(SERVICE_IFACE)
            if svc:
                u = str(svc["UUID"]).lower()
                if self.expose_all or u in FITNESS_SERVICES:
                    self.services[u] = []
        for path in sorted(objs):
            if not path.startswith(prefix):
                continue
            ch = objs[path].get(CHAR_IFACE)
            if not ch:
                continue
            svc_path = str(ch["Service"])
            svc = objs.get(svc_path, {}).get(SERVICE_IFACE)
            if not svc:
                continue
            su = str(svc["UUID"]).lower()
            if su not in self.services:
                continue
            cu = str(ch["UUID"]).lower()
            flags = [str(f) for f in ch["Flags"]]
            self.services[su].append((cu, dircon_props(flags), path))
            self.by_char[cu] = path
        for su, chars in self.services.items():
            log("  %s %s", su[:8], FITNESS_SERVICES.get(su, ""))
            for cu, p, _ in chars:
                log("    %s props=%d", cu[:8], p)

        # Once only: load_gatt runs again on every reconnect, and a second
        # receiver would deliver every notification twice.
        if not self.watching:
            self.bus.add_signal_receiver(
                self.on_char_props,
                dbus_interface=PROPS_IFACE,
                signal_name="PropertiesChanged",
                path_keyword="path",
            )
            self.watching = True

    def char_iface(self, cu):
        return dbus.Interface(self.bus.get_object(BLUEZ, self.by_char[cu]), CHAR_IFACE)

    def on_char_props(self, iface, changed, invalidated, path=None):
        if iface != CHAR_IFACE or "Value" not in changed:
            return
        for cu, p in self.by_char.items():
            if p == path:
                break
        else:
            return
        if cu not in self.bridge.notifying:
            return
        value = bytes(bytearray(changed["Value"]))
        self.trace_notification(cu, value)
        self.bridge.send(MSG_NOTIFICATION, 0, RC_OK, uuid.UUID(cu).bytes + value)

    def trace_notification(self, cu, value):
        """Log the first notification of each characteristic, and then keep
        logging Indoor Bike Data every few seconds -- what the trainer actually
        puts on the wire is the only way to tell whether the game *can* have
        cadence from it."""
        first = cu not in self.traced
        self.traced.add(cu)
        is_ibd = cu == u16(0x2AD2)
        now = time.monotonic()
        if not first and not (is_ibd and now - self.traced_at.get(cu, 0) >= 5):
            return
        self.traced_at[cu] = now
        if is_ibd:
            log("notification %s  %s", cu[:8], describe_ibd(value))
        elif first:
            log("notification %s  %s", cu[:8], value.hex())

    # ------------------------------------------------------------ reconnect

    def alive(self):
        """Is the trainer still on the end of a working GATT link?"""
        if not self.dev_path:
            return False
        ifaces = self.objects().get(self.dev_path)
        if not ifaces:
            # BlueZ drops the object entirely for a non-bonded device that goes
            # away, which is what a sleeping trainer looks like from here.
            return False
        d = ifaces.get(DEVICE_IFACE, {})
        return bool(d.get("Connected")) and bool(d.get("ServicesResolved"))

    def watchdog(self):
        """A trainer sleeping is normal, so treat it as normal: notice, wait for
        it to advertise again, reconnect, and put the client's subscriptions
        back. The Dircon socket is deliberately left up -- as far as the game is
        concerned its device never went anywhere, and data simply resumes."""
        if self.reconnecting or self.alive():
            return True
        self.reconnecting = True
        try:
            log("%s is gone (asleep or out of range); waiting for it",
                self.name or self.mac)
            self.services, self.by_char = {}, {}
            try:
                self.connect()
            except (SystemExit, dbus.DBusException) as e:
                log("  not back yet (%s); will keep trying", e)
                return True
            # The characteristic paths are new, so the merged list the client
            # reads and writes through has to point at them.
            self.bridge.remerge()

            resubscribed = 0
            for cu in sorted(self.bridge.notifying):
                if cu not in self.by_char:
                    continue          # another peer's subscription
                try:
                    self.char_iface(cu).StartNotify()
                    resubscribed += 1
                except dbus.DBusException as e:
                    log("  could not resubscribe %s: %s", cu[:8], e.get_dbus_name())
            log("reconnected; %d subscription(s) restored, client %s",
                resubscribed, "still attached" if self.bridge.client else "not attached")
            # The flags are read once per notification stream, so let them be
            # logged again for the new one.
            self.traced.clear()
            self.traced_at.clear()
            return True
        finally:
            self.reconnecting = False


class Bridge:
    """One Dircon endpoint, serving one or more real devices as a single sensor.

    MyWhoosh runs exactly one Direct Connect connection at a time.  Both
    `DirconSensor::Connect` and `WahooProgram::ServiceResolved` refuse to start
    another while `WahooProgram::sensorTasks` is non-empty ("connection task is
    already running"), and the task they add to it is the socket's own read
    loop, which ends only when the connection does.  So a strap advertised on a
    second port is discovered, resolved, listed, pairable -- and never
    connected, which is what showed as a nonsense heart rate in game.

    The game's own answer for a device that fills several slots is the serial:
    `PairDevice` parses the serial out of the offered device and compares it
    with the sensor already paired for E_PowerSource; on a match it hooks that
    same connected sensor into the new slot instead of dialling anything
    (`features.heartConnectCallback = true`, `SendConnectCallbackManual(4,
    sensor)`).  That is what this class is for: every peer's services go onto
    one socket under one name and one serial, and the game pairs the one sensor
    into every slot its characteristics can fill.
    """

    def __init__(self, bus, port):
        self.bus = bus
        self.port = port
        self.peers = []
        self.services = {}        # service uuid -> [(char uuid, props, path)]
        self.by_char = {}         # char uuid -> the peer that has it
        self.notifying = set()    # char uuids the client subscribed to
        self.client = None
        self.buf = b""

    def add(self, peer):
        """Connect a device and put its services on our wire."""
        peer.bridge = self
        peer.connect()
        self.peers.append(peer)
        self.remerge()
        return peer

    def remerge(self):
        """Rebuild the merged view after any peer's GATT tree changes.

        First peer to offer a service keeps it: two devices with the same
        service UUID cannot both be answered under one Dircon identity, and the
        trainer is added first for that reason.
        """
        self.services, self.by_char = {}, {}
        for peer in self.peers:
            for su, chars in peer.services.items():
                if su in self.services:
                    log("%s also has %s %s; leaving its copy off the wire",
                        peer.name or peer.mac, su[:8], FITNESS_SERVICES.get(su, ""))
                    continue
                self.services[su] = chars
                for cu, _props, _path in chars:
                    self.by_char[cu] = peer

    def caps(self, peer=None):
        """Every pairing slot this sensor can fill; for one peer, only the ones
        its own characteristics -- the ones that made it onto the wire -- fill."""
        return caps_for({cu for cu, owner in self.by_char.items()
                         if peer is None or owner is peer})

    def char_iface(self, cu):
        return self.by_char[cu].char_iface(cu)

    # --------------------------------------------------------------- Dircon

    def send(self, msgid, seq, rc, body=b""):
        if not self.client:
            return
        pkt = struct.pack(">BBBBH", 1, msgid, seq, rc, len(body)) + body
        try:
            self.client.sendall(pkt)
        except OSError as e:
            log("client write failed (%s), dropping it", e)
            self.drop_client()

    def handle(self, msgid, seq, body):
        if msgid == MSG_SERVICES:
            out = b"".join(uuid.UUID(u).bytes for u in self.services)
            self.send(MSG_SERVICES, seq, RC_OK, out)

        elif msgid == MSG_CHARS:
            if len(body) < 16:
                return self.send(MSG_CHARS, seq, RC_UNEXPECTED)
            su = str(uuid.UUID(bytes=body[:16]))
            chars = self.services.get(su)
            if chars is None:
                log("characteristics asked for unknown service %s", su[:8])
                return self.send(MSG_CHARS, seq, RC_UNKNOWN_SERVICE, body[:16])
            out = body[:16]
            for cu, props, _ in chars:
                out += uuid.UUID(cu).bytes + bytes([props])
            self.send(MSG_CHARS, seq, RC_OK, out)

        elif msgid == MSG_READ:
            if len(body) < 16:
                return self.send(MSG_READ, seq, RC_UNEXPECTED)
            cu = str(uuid.UUID(bytes=body[:16]))
            if cu not in self.by_char:
                return self.send(MSG_READ, seq, RC_UNKNOWN_CHAR, body[:16])
            try:
                val = bytes(bytearray(self.char_iface(cu).ReadValue(NO_OPTIONS)))
            except dbus.DBusException as e:
                log("read %s failed: %s", cu[:8], e.get_dbus_name())
                return self.send(MSG_READ, seq, RC_UNSUPPORTED, body[:16])
            self.send(MSG_READ, seq, RC_OK, body[:16] + val)

        elif msgid == MSG_WRITE:
            if len(body) < 16:
                return self.send(MSG_WRITE, seq, RC_UNEXPECTED)
            cu = str(uuid.UUID(bytes=body[:16]))
            payload = body[16:]
            if cu not in self.by_char:
                return self.send(MSG_WRITE, seq, RC_UNKNOWN_CHAR, body[:16])
            try:
                self.char_iface(cu).WriteValue(dbus.Array(payload, signature="y"), NO_OPTIONS)
                rc = RC_OK
            except dbus.DBusException as e:
                log("write %s failed: %s", cu[:8], e.get_dbus_name())
                rc = RC_UNSUPPORTED
            log("write %s <- %s rc=%d", cu[:8], payload.hex(), rc)
            self.send(MSG_WRITE, seq, rc, body[:16])

        elif msgid == MSG_NOTIFY_ENABLE:
            if len(body) < 17:
                return self.send(MSG_NOTIFY_ENABLE, seq, RC_UNEXPECTED)
            cu = str(uuid.UUID(bytes=body[:16]))
            enable = body[16] != 0
            if cu not in self.by_char:
                return self.send(MSG_NOTIFY_ENABLE, seq, RC_UNKNOWN_CHAR, body[:17])
            try:
                if enable:
                    self.char_iface(cu).StartNotify()
                    self.notifying.add(cu)
                else:
                    self.notifying.discard(cu)
                    self.char_iface(cu).StopNotify()
                rc = RC_OK
            except dbus.DBusException as e:
                # InProgress means it is already notifying, which is what was asked for.
                if e.get_dbus_name() == "org.bluez.Error.InProgress":
                    self.notifying.add(cu)
                    rc = RC_OK
                else:
                    log("notify %s failed: %s", cu[:8], e.get_dbus_name())
                    rc = RC_UNSUPPORTED
            log("notify %s %s rc=%d", cu[:8], "on" if enable else "off", rc)
            self.send(MSG_NOTIFY_ENABLE, seq, rc, body[:17])

        else:
            log("unhandled request id=%d", msgid)
            self.send(msgid, seq, RC_UNSUPPORTED)

    # --------------------------------------------------------------- socket

    def drop_client(self):
        if self.client:
            try:
                self.client.close()
            except OSError:
                pass
        self.client = None
        self.buf = b""
        for cu in list(self.notifying):
            try:
                self.char_iface(cu).StopNotify()
            except dbus.DBusException:
                pass
        self.notifying.clear()

    def on_client_data(self, sock, condition):
        try:
            data = sock.recv(4096)
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return True
            data = b""
        if not data:
            log("client gone")
            self.drop_client()
            return False
        self.buf += data
        while len(self.buf) >= 6:
            ver, msgid, seq, _rc, length = struct.unpack(">BBBBH", self.buf[:6])
            if ver != 1:
                log("protocol version %d, dropping the client", ver)
                self.drop_client()
                return False
            if len(self.buf) < 6 + length:
                break
            body = self.buf[6:6 + length]
            self.buf = self.buf[6 + length:]
            try:
                self.handle(msgid, seq, body)
            except Exception as e:
                log("request id=%d failed: %s: %s", msgid, type(e).__name__, e)
                self.send(msgid, seq, RC_UNSUPPORTED)
            if not self.client:
                return False
        return True

    def on_accept(self, sock, condition):
        c, _ = sock.accept()
        if self.client:
            log("second client refused; one at a time")
            c.close()
            return True
        c.setblocking(False)
        self.client = c
        log("client connected")
        GLib.io_add_watch(c, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_client_data)
        return True

    def listen(self, host):
        l = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        l.bind((host, self.port))
        l.listen(4)
        l.setblocking(False)
        log("listening on %s:%d", host, self.port)
        GLib.io_add_watch(l, GLib.IO_IN, self.on_accept)
        return l


def main():
    ap = argparse.ArgumentParser(description="Serve a BLE trainer over Wahoo Direct Connect")
    ap.add_argument("--mac", help="device address; default is the first fitness device seen")
    ap.add_argument("--adapter", default="hci0")
    ap.add_argument("--port", type=int, default=36866, help="Dircon TCP port (default 36866)")
    ap.add_argument("--hr-mac", help="a heart-rate strap to serve alongside the trainer, "
                    "on the same socket, so the game can pair it too")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on")
    ap.add_argument("--all", action="store_true", dest="expose_all",
                    help="expose every GATT service, not just the fitness ones")
    ap.add_argument("--prefix", help="Wine prefix to write the handshake file into; "
                    "default is $WINEPREFIX, else the one prefix that has "
                    "fakebonjour installed")
    ap.add_argument("--no-handshake", action="store_true",
                    help="do not write the handshake file (advertise via FAKESENSOR_* instead)")
    ap.add_argument("--list", action="store_true", help="scan, print candidates, exit")
    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    trainer = Peer(bus, args.adapter, args.mac, args.expose_all)

    if args.list:
        trainer.scan(15, None)
        for path, addr, alias, uuids in trainer.candidates():
            known = [FITNESS_SERVICES[u] for u in uuids if u in FITNESS_SERVICES]
            print("%s  %-24s %s" % (addr, alias, ", ".join(known)))
        return

    b = Bridge(bus, args.port)
    b.add(trainer)
    if args.hr_mac:
        # Not a second sensor: the strap's heart-rate service joins the
        # trainer's on this one socket, because the game will only ever hold
        # one Direct Connect connection (see Bridge).
        b.add(Peer(bus, args.adapter, args.hr_mac, args.expose_all))
    b.listen(args.host)
    for peer in b.peers:
        GLib.timeout_add_seconds(5, peer.watchdog)

    # The serial is the device's identity as far as MyWhoosh is concerned: it
    # keys its saved pairing on it and then shows the *stored* name, so leaving
    # fakebonjour's default serial in place makes a real sensor come up under
    # whatever name the last one on that serial had -- and makes the game treat
    # it as already known, which keeps it out of the scan results.  Derive one
    # from the trainer's address.  It is also what the game matches a second
    # slot's pairing against, so every peer here has to answer to this one.
    #
    # One endpoint, but one name each in the scan list: what the game matches a
    # second slot's pairing on is that serial, not the name, so each peer can
    # be offered under its own name at the trainer's serial and port and the
    # heart-rate slot shows the strap instead of the trainer.  Pairing it hooks
    # the sensor already connected for the trainer into the new slot, which is
    # the one thing the game will do here.
    serial = int(trainer.mac.replace(":", ""), 16)
    devices = [{"name": p.name.replace(" ", "-"), "serial": serial,
                "mac": p.mac, "port": b.port, "caps": b.caps(p)}
               for p in b.peers if b.caps(p)]
    if len(devices) > 1:
        log("serving one sensor on port %d, offered as %s", b.port,
            " and ".join("%s (%s)" % (d["name"], ",".join(d["caps"])) for d in devices))

    prefix = None if args.no_handshake else (args.prefix or find_prefix())
    if prefix:
        write_handshake(prefix, devices)
    else:
        log("advertise this as: FAKESENSOR_NAME=%r FAKESENSOR_SERIAL=%d FAKESENSOR_PORT=%d",
            devices[0]["name"], devices[0]["serial"], devices[0]["port"])
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        log("stopping")
        b.drop_client()


if __name__ == "__main__":
    main()
