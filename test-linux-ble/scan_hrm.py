#!/usr/bin/env python3
"""Scan for Bluetooth Low Energy Heart Rate Monitor (HRM) devices using BlueZ D-Bus API."""

import sys
import time
import signal
import dbus
import dbus.mainloop.glib
from gi.repository import GLib

# Heart Rate Service UUID (Bluetooth SIG assigned)
HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

found_devices = {}


def is_heart_rate_device(properties):
    uuids = properties.get("UUIDs", [])
    return HEART_RATE_SERVICE_UUID in [str(u).lower() for u in uuids]


def print_device(path, properties):
    addr = properties.get("Address", "??:??:??:??:??:??")
    name = properties.get("Name", "<unknown>")
    rssi = properties.get("RSSI", "N/A")
    print(f"  [HRM] {name} | {addr} | RSSI: {rssi} dBm | {path}")


def interfaces_added(path, interfaces):
    if DEVICE_IFACE not in interfaces:
        return
    props = interfaces[DEVICE_IFACE]
    if is_heart_rate_device(props) and path not in found_devices:
        found_devices[path] = props
        print_device(path, props)


def properties_changed(interface, changed, invalidated, path=None):
    if interface != DEVICE_IFACE:
        return
    if path and path not in found_devices:
        # New device detected via property update — fetch full properties
        try:
            obj = bus.get_object(BLUEZ_SERVICE, path)
            props_iface = dbus.Interface(obj, PROPERTIES_IFACE)
            props = props_iface.GetAll(DEVICE_IFACE)
            if is_heart_rate_device(props):
                found_devices[path] = props
                print_device(path, props)
        except dbus.DBusException:
            pass
    elif path in found_devices:
        # Update RSSI for already-found device
        if "RSSI" in changed:
            found_devices[path]["RSSI"] = changed["RSSI"]


def get_adapter(bus):
    manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE, "/"), OBJECT_MANAGER_IFACE)
    objects = manager.GetManagedObjects()
    for path, ifaces in objects.items():
        if ADAPTER_IFACE in ifaces:
            return path, ifaces[ADAPTER_IFACE]
    return None, None


def scan_existing_devices(bus):
    """Print HRM devices already known to BlueZ before we started scanning."""
    manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE, "/"), OBJECT_MANAGER_IFACE)
    for path, ifaces in manager.GetManagedObjects().items():
        if DEVICE_IFACE in ifaces:
            props = ifaces[DEVICE_IFACE]
            if is_heart_rate_device(props) and path not in found_devices:
                found_devices[path] = props
                print_device(path, props)


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    global bus
    bus = dbus.SystemBus()

    adapter_path, adapter_props = get_adapter(bus)
    if not adapter_path:
        print("Error: No Bluetooth adapter found. Is BlueZ running?", file=sys.stderr)
        sys.exit(1)

    adapter_name = adapter_props.get("Name", adapter_path)
    print(f"Using adapter: {adapter_name} ({adapter_path})")
    print(f"Scanning for Heart Rate Monitor devices (UUID {HEART_RATE_SERVICE_UUID})...")
    print("Press Ctrl+C to stop.\n")

    # Listen for new devices and property changes
    bus.add_signal_receiver(
        interfaces_added,
        signal_name="InterfacesAdded",
        dbus_interface=OBJECT_MANAGER_IFACE,
        bus_name=BLUEZ_SERVICE,
    )
    bus.add_signal_receiver(
        properties_changed,
        signal_name="PropertiesChanged",
        dbus_interface=PROPERTIES_IFACE,
        bus_name=BLUEZ_SERVICE,
        path_keyword="path",
    )

    # Print any HRM devices already cached by BlueZ
    scan_existing_devices(bus)

    # Start discovery
    adapter = dbus.Interface(bus.get_object(BLUEZ_SERVICE, adapter_path), ADAPTER_IFACE)
    try:
        adapter.StartDiscovery()
    except dbus.DBusException as e:
        print(f"Error starting discovery: {e}", file=sys.stderr)
        sys.exit(1)

    loop = GLib.MainLoop()

    def on_sigint(_sig, _frame):
        print(f"\nStopping scan. Found {len(found_devices)} HRM device(s).")
        try:
            adapter.StopDiscovery()
        except dbus.DBusException:
            pass
        loop.quit()

    signal.signal(signal.SIGINT, on_sigint)

    loop.run()


if __name__ == "__main__":
    main()
