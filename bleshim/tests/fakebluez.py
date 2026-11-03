"""A BlueZ object tree in memory, reachable the way blehelper reaches BlueZ.

`FakeBluez.tree` is what GetManagedObjects answers.  Method calls made through
`dbus.Interface(bus.get_object(...), iface)` land in `calls` as
(path, method) and can be made to fail per path with `fail`.
"""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import dbus

spec = importlib.util.spec_from_file_location("blehelper", Path(__file__).resolve().parents[1] / "blehelper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

ADAPTER = "/org/bluez/hci0"
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BODY_LOCATION = "00002a38-0000-1000-8000-00805f9b34fb"


def dbus_error(name):
    return dbus.exceptions.DBusException(name.rsplit(".", 1)[-1], name=name)


class FakeBluez:
    def __init__(self):
        self.tree = {ADAPTER: {helper.ADAPTER_IFACE: {"Powered": True}}}
        self.calls = []
        self.fail = {}                      # (path, method) -> DBusException

    # ---------------------------------------------------------------- tree

    def device(self, addr, path_addr=None, **props):
        """A Device1; `path_addr` names its object path when it differs."""
        path = "%s/dev_%s" % (ADAPTER, (path_addr or addr).replace(":", "_"))
        self.tree[path] = {helper.DEVICE_IFACE: dict({"Address": addr}, **props)}
        return path

    def service(self, device_path, handle, uuid):
        path = "%s/service%04x" % (device_path, handle)
        self.tree[path] = {helper.SERVICE_IFACE: {"UUID": uuid, "Device": device_path}}
        return path

    def char(self, service_path, handle, uuid, flags, **props):
        path = "%s/char%04x" % (service_path, handle)
        self.tree[path] = {helper.CHAR_IFACE: dict(
            {"UUID": uuid, "Service": service_path, "Flags": list(flags)}, **props)}
        return path

    def props(self, path, iface):
        return self.tree[path][iface]

    # ----------------------------------------------------------------- bus

    def get_object(self, _name, path):
        fake = self

        class Proxy:
            def get_dbus_method(self, member, iface=None):
                def call(*args, **kwargs):
                    fake.calls.append((path, member))
                    err = fake.fail.get((path, member))
                    if err is not None:
                        raise err
                    if member == "Get":
                        return fake.tree.get(path, {}).get(args[0], {}).get(args[1], False)
                    if member == "ReadValue":
                        return []
                    if member == "StartNotify":
                        fake.tree[path][helper.CHAR_IFACE]["Notifying"] = True
                    if member == "StopNotify":
                        fake.tree[path][helper.CHAR_IFACE]["Notifying"] = False
                    return None
                return call
        return Proxy()

    def handle(self):
        """A helper.Bluez over this tree."""
        bz = helper.Bluez.__new__(helper.Bluez)
        bz.bus = self
        bz.adapter_path = ADAPTER
        bz.om = Mock()
        bz.om.GetManagedObjects.side_effect = lambda: self.tree
        return bz

    def server(self):
        s = helper.Server(self.handle(), 0)
        s.worker_bluez = self.handle()
        s.send = Mock()
        return s

    def watch(self, addr="AA:BB:CC:DD:EE:01"):
        """A heart-rate relay with two 180d: a locked one, then an open one."""
        dev = self.device(addr, Connected=True, ServicesResolved=True)
        locked = self.service(dev, 0x10, HR_SERVICE)
        locked_hr = self.char(locked, 0x11, HR_MEASUREMENT, ["read", "notify"])
        self.char(locked, 0x14, BODY_LOCATION, ["read"])
        open_ = self.service(dev, 0x20, HR_SERVICE)
        open_hr = self.char(open_, 0x21, HR_MEASUREMENT, ["notify"])
        self.fail[(locked_hr, "StartNotify")] = dbus_error("org.bluez.Error.InsufficientEncryption")
        return dev, locked_hr, open_hr
