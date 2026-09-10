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
    <-  {"ev":"advert","addr":"FA:55:...","name":"Tacx Flux","uuids":[...],"rssi":-61}
    ->  {"id":2,"op":"connect","addr":"FA:55:..."}
    <-  {"id":2,"ok":true,"name":"Tacx Flux 06189"}
    ->  {"id":3,"op":"services","addr":"FA:55:..."}
    <-  {"id":3,"ok":true,"services":[{"uuid":"00001826-..."}]}
    ->  {"id":4,"op":"notify","addr":"FA:55:...","char":"00002ad2-...","enable":true}
    <-  {"id":4,"ok":true}
    <-  {"ev":"value","addr":"FA:55:...","char":"00002ad2-...","value":"44024a00..."}

Ops: radios, scan, connect, disconnect, services, chars, read, write, notify.
Events: advert, value, connection.

Usage:
    ./blehelper.py                  # serve on 127.0.0.1:27019
    ./blehelper.py --list           # scan, print what is on the air, exit
    ./blehelper.py --port 27019 --adapter hci0

Start it before the game; nothing has to be configured per trainer, because the
game does its own scanning, pairing and slot filling through it.  Unlike
../fakesensor/blebridge.py on the `dev` branch, this connects to nothing by
itself: it holds no device the game has not asked for, and several sensors are
several independent connections.
"""

import argparse
import errno
import json
import socket
import sys
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

verbose = False


def log(fmt, *a):
    sys.stderr.write("[%s blehelper] %s\n"
                     % (time.strftime("%H:%M:%S"), fmt % a if a else fmt))
    sys.stderr.flush()


def trace(fmt, *a):
    if verbose:
        log(fmt, *a)


class Bluez:
    """BlueZ, in the few verbs the shim needs of it."""

    def __init__(self, bus, adapter):
        self.bus = bus
        self.adapter_path = "/org/bluez/%s" % adapter
        self.om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)
        self.scanning = False

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
        return dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE)

    def radios(self):
        """Every adapter BlueZ has, and whether it is powered.

        The game asks this first and shows the user "Bluetooth is off" for an
        empty or unpowered answer, so it comes from Adapter1.Powered -- the one
        source that cannot disagree with what a scan will then do.  (sysfs looks
        like the easier route from inside the prefix and is not: current kernels
        have no /sys/class/bluetooth/hciN/flags to read at all.)
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

    def start_scan(self):
        if self.scanning:
            return
        try:
            self.adapter().SetDiscoveryFilter({"Transport": "le", "DuplicateData": dbus.Boolean(True)})
        except dbus.DBusException as e:
            log("discovery filter refused (%s), scanning anyway", e.get_dbus_name())
        try:
            self.adapter().StartDiscovery()
        except dbus.DBusException as e:
            if e.get_dbus_name() != "org.bluez.Error.InProgress":
                raise
        self.scanning = True
        log("scanning")

    def stop_scan(self):
        if not self.scanning:
            return
        self.scanning = False
        try:
            self.adapter().StopDiscovery()
        except dbus.DBusException as e:
            trace("StopDiscovery: %s", e.get_dbus_name())
        log("scan stopped")

    def wait_for_advert(self, addr, seconds):
        """Scan until this device is on the air, whatever the client asked for.

        The game connects to a device it saw in a scan it has since stopped, and
        a trainer drops its radio the moment the cranks stop turning.  Waiting
        here turns "connect failed" into "connect took a moment", and the scan
        state the client asked for is restored afterwards.
        """
        if self.on_air(addr):
            return True
        was_scanning = self.scanning
        self.start_scan()
        loop = GLib.MainLoop()
        state = {"seen": False}

        def poll():
            if not self.on_air(addr):
                return True
            state["seen"] = True
            loop.quit()
            return False

        GLib.timeout_add(300, poll)
        GLib.timeout_add_seconds(seconds, lambda: (loop.quit(), False)[1])
        loop.run()
        if not was_scanning:
            self.stop_scan()
        return state["seen"]

    # -------------------------------------------------------------- connect

    def connect(self, addr):
        """Connect and wait for the GATT tree, the way WinRT's
        FromBluetoothAddressAsync leaves a device ready to be walked."""
        path = self.device_path(addr)
        if path not in self.objects():
            if not self.wait_for_advert(addr, ADVERT_TIMEOUT):
                raise BleError("%s is not on the air (asleep, or out of range)" % addr)

        props = dbus.Interface(self.bus.get_object(BLUEZ, path), PROPS_IFACE)
        if not props.Get(DEVICE_IFACE, "Connected"):
            if not self.on_air(addr) and not self.wait_for_advert(addr, ADVERT_TIMEOUT):
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

        loop = GLib.MainLoop()
        state = {"ok": False}

        def resolved():
            try:
                if not props.Get(DEVICE_IFACE, "ServicesResolved"):
                    return True
            except dbus.DBusException:
                loop.quit()
                return False
            state["ok"] = True
            loop.quit()
            return False

        GLib.timeout_add(300, resolved)
        GLib.timeout_add_seconds(CONNECT_TIMEOUT, lambda: (loop.quit(), False)[1])
        loop.run()
        if not state["ok"]:
            raise BleError("connected to %s but its services never resolved" % addr)

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


class Server:
    """One TCP client -- the game -- and the BlueZ state it asked for."""

    def __init__(self, bluez, port):
        self.bluez = bluez
        self.port = port
        self.client = None
        self.buffer = b""
        self.subscribed = set()             # (addr, char uuid) the client wants
        self.silent = set()                 # devices reported as having no service UUIDs
        self.char_addr = {}                 # char object path -> (addr, uuid)
        self.connected = {}                 # addr -> last reported Connected

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
        self.client = conn
        self.buffer = b""
        GLib.io_add_watch(conn, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_data)
        log("shim connected")
        return True

    def drop_client(self):
        """Let go of everything the client had asked for.

        The game going away must not leave a trainer subscribed and a scan
        running: the next process to use the adapter would inherit both.
        """
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
        for addr, uuid in list(self.subscribed):
            try:
                self.bluez.notify(addr, uuid, False)
            except (BleError, dbus.DBusException):
                pass
        self.subscribed.clear()
        self.char_addr.clear()
        self.silent.clear()
        self.bluez.stop_scan()

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

        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.strip()
            if line:
                self.handle(line)
        return True

    def send(self, obj):
        if self.client is None:
            return
        try:
            self.client.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError as e:
            log("send failed (%s); dropping the client", e)
            self.drop_client()

    # ------------------------------------------------------------- requests

    def handle(self, line):
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            log("unparsable request (%s): %r", e, line[:80])
            return

        rid = req.get("id")
        op = req.get("op")
        try:
            reply = self.dispatch(op, req)
        except BleError as e:
            trace("%s: %s", op, e)
            self.send({"id": rid, "ok": False, "error": str(e)})
            return
        except dbus.DBusException as e:
            log("%s: %s", op, e.get_dbus_message())
            self.send({"id": rid, "ok": False, "error": e.get_dbus_message()})
            return
        reply = dict(reply or {})
        reply.update({"id": rid, "ok": True})
        self.send(reply)

    def dispatch(self, op, req):
        addr = (req.get("addr") or "").upper()
        char = (req.get("char") or "").lower()

        if op == "radios":
            return {"radios": self.bluez.radios()}

        if op == "scan":
            if req.get("enable"):
                self.bluez.start_scan()
                self.announce_known()
            else:
                self.bluez.stop_scan()
            return {}

        if op == "connect":
            name = self.bluez.connect(addr)
            self.connected[addr] = True
            return {"name": name}

        if op == "disconnect":
            for key in [k for k in self.subscribed if k[0] == addr]:
                self.subscribed.discard(key)
            self.bluez.disconnect(addr)
            return {}

        if op == "services":
            return {"services": self.bluez.services(addr)}

        if op == "chars":
            chars = self.bluez.characteristics(addr, (req.get("service") or "").lower())
            for c in chars:
                try:
                    self.char_addr[self.bluez.char_path(addr, c["uuid"])] = (addr, c["uuid"])
                except BleError:
                    pass
            return {"chars": chars}

        if op == "read":
            return {"value": self.bluez.read(addr, char).hex()}

        if op == "write":
            value = bytes.fromhex(req.get("value") or "")
            self.bluez.write(addr, char, value, bool(req.get("response", True)))
            return {}

        if op == "notify":
            enable = bool(req.get("enable"))
            self.bluez.notify(addr, char, enable)
            self.char_addr[self.bluez.char_path(addr, char)] = (addr, char)
            if enable:
                self.subscribed.add((addr, char))
            else:
                self.subscribed.discard((addr, char))
            return {}

        raise BleError("unknown op %r" % op)

    # --------------------------------------------------------------- events

    def announce_known(self):
        """Report the devices already on the air when a scan starts.

        BlueZ only signals what changes, so a device that advertised a second
        before the game opened its pairing screen would otherwise stay invisible
        until it happened to move.
        """
        sent = 0
        for path, ifaces in self.bluez.objects().items():
            d = ifaces.get(DEVICE_IFACE)
            if not d or not path.startswith(self.bluez.adapter_path + "/"):
                continue
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
        if not uuids and addr not in self.silent:
            self.silent.add(addr)
            trace("%s advertises no service UUIDs; the game will ignore it", addr)
        self.send({"ev": "advert",
                   "addr": addr,
                   "name": str(props.get("Alias", "") or ""),
                   "uuids": uuids,
                   "rssi": int(props.get("RSSI", 0))})

    def on_properties_changed(self, iface, changed, invalidated, path=None):
        if iface == CHAR_IFACE and "Value" in changed:
            known = self.char_addr.get(path)
            if not known:
                return
            addr, uuid = known
            if (addr, uuid) not in self.subscribed:
                return
            self.send({"ev": "value", "addr": addr, "char": uuid,
                       "value": bytes(bytearray(changed["Value"])).hex()})
            return

        if iface != DEVICE_IFACE:
            return
        props = self.bluez.objects().get(path, {}).get(DEVICE_IFACE, {})
        addr = str(props.get("Address", "")).upper()
        if not addr:
            return
        if "Connected" in changed:
            now = bool(changed["Connected"])
            if self.connected.get(addr) != now:
                self.connected[addr] = now
                self.send({"ev": "connection", "addr": addr, "connected": now})
        if self.bluez.scanning and ("RSSI" in changed or "UUIDs" in changed or "Alias" in changed):
            self.advert(props)

    def on_interfaces_added(self, path, ifaces):
        d = ifaces.get(DEVICE_IFACE)
        if d and self.bluez.scanning:
            self.advert(d)

    def on_interfaces_removed(self, path, ifaces):
        # BlueZ removes a non-bonded device's object when it stops advertising,
        # which is what a sleeping trainer looks like.  The game hears about it
        # as a disconnection, and reconnecting is its decision, not ours.
        if DEVICE_IFACE not in ifaces:
            return
        for addr, was in list(self.connected.items()):
            if self.bluez.device_path(addr) == path and was:
                self.connected[addr] = False
                self.send({"ev": "connection", "addr": addr, "connected": False})


def list_devices(bluez):
    """Print what is on the air, the way --list does on the Dircon bridge."""
    bluez.start_scan()
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(8, lambda: (loop.quit(), False)[1])
    loop.run()
    bluez.stop_scan()

    rows = []
    for path, ifaces in sorted(bluez.objects().items()):
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
    ap.add_argument("--adapter", default="hci0", help="BlueZ adapter (default hci0)")
    ap.add_argument("--list", action="store_true", help="scan, print what is on the air, exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    verbose = args.verbose

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    bluez = Bluez(bus, args.adapter)
    try:
        bluez.objects()
    except dbus.DBusException as e:
        raise SystemExit("cannot talk to BlueZ (%s) -- is bluetoothd running?" % e.get_dbus_name())
    if bluez.adapter_path not in bluez.objects():
        raise SystemExit("no adapter at %s -- try --adapter" % bluez.adapter_path)

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
    server.listen()

    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        server.drop_client()
        log("stopped")


if __name__ == "__main__":
    main()
