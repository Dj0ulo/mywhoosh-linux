"""Adapter/reset regressions using fake BlueZ objects, without touching hardware."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import dbus
import dbus.lowlevel

spec = importlib.util.spec_from_file_location("blehelper", Path(__file__).resolve().parents[1] / "blehelper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def adapter(name, powered=True):
    return {"/org/bluez/" + name: {helper.ADAPTER_IFACE: {"Powered": powered}}}


class AdapterTests(unittest.TestCase):
    def test_auto_selects_renumbered_adapter(self):
        self.assertEqual(helper.select_adapter(adapter("hci5")), "hci5")
        self.assertEqual(helper.select_adapter(adapter("hci1")), "hci1")

    def test_off_adapter_does_not_hide_powered_one(self):
        objects = adapter("hci0", False) | adapter("hci5")
        self.assertEqual(helper.select_adapter(objects), "hci5")

    def test_explicit_override_is_respected(self):
        objects = adapter("hci0") | adapter("hci5")
        self.assertEqual(helper.select_adapter(objects, "hci5"), "hci5")

    def test_several_powered_keeps_hci0(self):
        # The pre-auto default: a second powered adapter must not break it.
        self.assertEqual(helper.select_adapter(adapter("hci0") | adapter("hci5")), "hci0")

    def test_several_powered_without_hci0_is_ambiguous(self):
        with self.assertRaisesRegex(helper.BleError, "multiple powered"):
            helper.select_adapter(adapter("hci1") | adapter("hci5"))

    def test_no_silent_fallback_from_invalid_override(self):
        with self.assertRaisesRegex(helper.BleError, "no BlueZ adapter hci0"):
            helper.select_adapter(adapter("hci5"), "hci0")

    def test_absent_or_powered_off_adapter_fails(self):
        for objects in ({}, adapter("hci0", False)):
            with self.subTest(objects=objects), self.assertRaisesRegex(helper.BleError, "no powered"):
                helper.select_adapter(objects)
        with self.assertRaisesRegex(helper.BleError, "powered off"):
            helper.select_adapter(adapter("hci5", False), "hci5")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.bluez = helper.Bluez.__new__(helper.Bluez)
        self.bluez.adapter_path = "/org/bluez/hci5"
        self.bluez.scanning = False
        self.bluez.om = Mock()
        self.bluez.om.GetManagedObjects.return_value = adapter("hci5")

    def test_filter_marshals_without_introspection(self):
        proxy = Mock()
        def set_filter(options, **kwargs):
            message = dbus.lowlevel.MethodCallMessage(helper.BLUEZ, "/org/bluez/hci5",
                                                      helper.ADAPTER_IFACE, "SetDiscoveryFilter")
            message.append(options, **kwargs)
            self.assertEqual(str(message.get_signature()), "a{sv}")
            self.assertTrue(message.get_args_list()[0]["DuplicateData"])
        proxy.SetDiscoveryFilter.side_effect = set_filter
        with patch.object(self.bluez, "adapter", return_value=proxy):
            self.bluez.start_scan()
        proxy.StartDiscovery.assert_called_once()
        self.assertTrue(self.bluez.scanning)

    def test_vanished_adapter_returns_protocol_error(self):
        self.bluez.om.GetManagedObjects.return_value = {}
        server = helper.Server(self.bluez, 0)
        server.send = Mock()
        server.handle(b'{"id":7,"op":"scan","enable":true}')
        response = server.send.call_args.args[0]
        self.assertEqual(response["id"], 7)
        self.assertFalse(response["ok"])
        self.assertIn("disappeared", response["error"])
        self.assertFalse(self.bluez.scanning)

    def test_reset_during_filter_returns_error_without_starting_scan(self):
        proxy = Mock()
        proxy.SetDiscoveryFilter.side_effect = dbus.exceptions.DBusException(
            "adapter removed", name="org.freedesktop.DBus.Error.UnknownObject")
        proxy.StartDiscovery.side_effect = proxy.SetDiscoveryFilter.side_effect
        server = helper.Server(self.bluez, 0)
        server.send = Mock()
        with patch.object(self.bluez, "adapter", return_value=proxy):
            server.handle(b'{"id":8,"op":"scan","enable":true}')
        self.assertFalse(server.send.call_args.args[0]["ok"])
        self.assertFalse(self.bluez.scanning)

    def test_adapter_removal_clears_stale_session_state(self):
        self.bluez.scanning = True
        server = helper.Server(self.bluez, 0)
        server.send = Mock()
        server.connected["02:00:00:00:00:01"] = True
        server.subscribed.add(("02:00:00:00:00:01", "test"))
        server.char_addr["/test"] = ("02:00:00:00:00:01", "test")
        server.on_interfaces_removed("/org/bluez/hci5", [helper.ADAPTER_IFACE])
        self.assertFalse(self.bluez.scanning)
        self.assertFalse(server.connected)
        self.assertFalse(server.subscribed)
        self.assertFalse(server.char_addr)
        server.send.assert_called_once_with({"ev": "connection", "addr": "02:00:00:00:00:01", "connected": False})
        server.on_interfaces_removed("/org/bluez/hci5", [helper.ADAPTER_IFACE])
        self.assertEqual(server.send.call_count, 1)

    def test_stop_after_adapter_disappears_is_safe(self):
        self.bluez.scanning = True
        self.bluez.om.GetManagedObjects.return_value = {}
        self.bluez.stop_scan()
        self.assertFalse(self.bluez.scanning)

    def test_list_preserves_observations_before_stop(self):
        def objects():
            props = {"Address": "02:00:00:00:00:01", "Alias": "Test trainer", "UUIDs": []}
            if self.bluez.scanning:
                props["RSSI"] = -60
            return {"/org/bluez/hci5/dev_test": {helper.DEVICE_IFACE: props}}
        self.bluez.om.GetManagedObjects.side_effect = objects
        with patch.object(self.bluez, "start_scan", side_effect=lambda: setattr(self.bluez, "scanning", True)), \
             patch.object(self.bluez, "stop_scan", side_effect=lambda: setattr(self.bluez, "scanning", False)), \
             patch.object(helper.GLib, "MainLoop"), patch.object(helper.GLib, "timeout_add_seconds"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            helper.list_devices(self.bluez)
        self.assertIn("Test trainer", output.getvalue())
        self.assertFalse(self.bluez.scanning)


if __name__ == "__main__":
    unittest.main()
