"""One GATT instance per duplicated UUID, the same one for every op."""
import unittest
from unittest.mock import Mock, patch

import dbus

from fakebluez import BODY_LOCATION, HR_MEASUREMENT, HR_SERVICE, FakeBluez, dbus_error, helper

ADDR = "AA:BB:CC:DD:EE:01"


class DuplicateServiceTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBluez()
        self.dev, self.locked_hr, self.open_hr = self.fake.watch(ADDR)
        self.bz = self.fake.handle()

    def test_services_lists_each_uuid_once(self):
        self.assertEqual(self.bz.services(ADDR), [{"uuid": HR_SERVICE}])

    def test_characteristics_come_from_the_open_instance(self):
        self.assertEqual(self.bz.characteristics(ADDR, HR_SERVICE),
                         [{"uuid": HR_MEASUREMENT, "flags": ["notify"]}])

    def test_equal_instances_take_the_newest(self):
        fake = FakeBluez()
        dev = fake.device(ADDR)
        fake.char(fake.service(dev, 0x10, HR_SERVICE), 0x11, HR_MEASUREMENT, ["notify"])
        newer = fake.char(fake.service(dev, 0x20, HR_SERVICE), 0x21, HR_MEASUREMENT, ["notify"])
        bz = fake.handle()
        self.assertEqual([p for p, _ in bz.characteristic_paths(ADDR, HR_SERVICE)], [newer])
        self.assertEqual(bz.char_path(ADDR, HR_MEASUREMENT, "notify"), newer)

    def test_read_goes_to_an_instance_that_can_read(self):
        self.assertEqual(self.bz.char_path(ADDR, HR_MEASUREMENT, "read"), self.locked_hr)
        self.bz.read(ADDR, HR_MEASUREMENT)
        self.assertIn((self.locked_hr, "ReadValue"), self.fake.calls)

    def test_single_instance_is_unchanged(self):
        fake = FakeBluez()
        dev = fake.device(ADDR)
        svc = fake.service(dev, 0x10, HR_SERVICE)
        hr = fake.char(svc, 0x11, HR_MEASUREMENT, ["read", "notify"])
        fake.char(svc, 0x14, BODY_LOCATION, ["read"])
        bz = fake.handle()
        self.assertEqual(bz.services(ADDR), [{"uuid": HR_SERVICE}])
        self.assertEqual([c["uuid"] for c in bz.characteristics(ADDR, HR_SERVICE)],
                         [HR_MEASUREMENT, BODY_LOCATION])
        self.assertEqual(bz.notify(ADDR, HR_MEASUREMENT, True), hr)

    def test_missing_characteristic_is_a_ble_error(self):
        with self.assertRaises(helper.BleError):
            self.bz.char_path(ADDR, "00002a63-0000-1000-8000-00805f9b34fb")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBluez()
        self.dev, self.locked_hr, self.open_hr = self.fake.watch(ADDR)
        self.bz = self.fake.handle()

    def test_security_refusal_falls_through_to_the_twin(self):
        # Rank the locked one first, as if it were the better notifier.
        self.fake.props(self.open_hr, helper.CHAR_IFACE)["Flags"] = ["read", "notify"]
        self.fake.tree[self.locked_hr][helper.CHAR_IFACE]["Flags"] = ["notify"]
        self.assertEqual(self.bz.notify(ADDR, HR_MEASUREMENT, True), self.open_hr)
        self.assertEqual([c for c in self.fake.calls if c[1] == "StartNotify"],
                         [(self.locked_hr, "StartNotify"), (self.open_hr, "StartNotify")])

    def test_other_refusal_does_not_fall_through(self):
        self.fake.fail[(self.open_hr, "StartNotify")] = dbus_error("org.bluez.Error.Failed")
        with self.assertRaisesRegex(helper.BleError, "StartNotify failed"):
            self.bz.notify(ADDR, HR_MEASUREMENT, True)
        self.assertNotIn((self.locked_hr, "StartNotify"), self.fake.calls)

    def test_stop_goes_to_the_recorded_path(self):
        self.assertEqual(self.bz.notify(ADDR, HR_MEASUREMENT, False, self.locked_hr), self.locked_hr)
        self.assertIn((self.locked_hr, "StopNotify"), self.fake.calls)

    def test_resubscribe_asks_the_recorded_path_first(self):
        self.fake.fail.pop((self.locked_hr, "StartNotify"))
        self.bz.notify(ADDR, HR_MEASUREMENT, True, self.locked_hr)
        self.assertEqual([c for c in self.fake.calls if c[1] == "StartNotify"],
                         [(self.locked_hr, "StartNotify")])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBluez()
        self.dev, self.locked_hr, self.open_hr = self.fake.watch(ADDR)
        self.s = self.fake.server()

    def notify(self, enable):
        return self.s.worker_dispatch("notify", {"addr": ADDR, "char": HR_MEASUREMENT, "enable": enable})

    def test_stop_targets_the_path_that_subscribed(self):
        # Subscribe lands on the open instance; then the tree changes so that
        # ranking again would pick the locked one.  The stop must not follow.
        self.notify(True)
        self.assertEqual(self.s.sub_path[(ADDR, HR_MEASUREMENT)], self.open_hr)
        self.fake.props(self.open_hr, helper.CHAR_IFACE)["Flags"] = ["read", "notify"]
        self.fake.props(self.locked_hr, helper.CHAR_IFACE)["Flags"] = ["notify"]
        self.notify(False)
        self.assertEqual([c for c in self.fake.calls if c[1] == "StopNotify"],
                         [(self.open_hr, "StopNotify")])
        self.assertFalse(self.s.subscribed)
        self.assertFalse(self.s.sub_path)

    def test_values_from_the_subscribed_instance_reach_the_game(self):
        self.notify(True)
        self.s.on_properties_changed(helper.CHAR_IFACE, {"Value": [0x00, 72]}, path=self.open_hr)
        self.s.drain_outbox()
        self.s.send.assert_called_once_with(
            {"ev": "value", "addr": ADDR, "char": HR_MEASUREMENT, "value": "0048"})

    def test_chars_maps_the_chosen_instance(self):
        reply = self.s.worker_dispatch("chars", {"addr": ADDR, "service": HR_SERVICE})
        self.assertEqual(reply, {"chars": [{"uuid": HR_MEASUREMENT, "flags": ["notify"]}]})
        self.assertEqual(self.s.char_addr, {self.open_hr: (ADDR, HR_MEASUREMENT)})

    def test_disconnect_forgets_the_path(self):
        self.notify(True)
        self.s.worker_dispatch("disconnect", {"addr": ADDR})
        self.assertFalse(self.s.sub_path)


FTMS = helper.FTMS_SERVICE
FTMS_CONTROL = "00002ad9-0000-1000-8000-00805f9b34fb"
NUS = helper.NUS_SERVICE
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"


class NordicUartTests(unittest.TestCase):
    """NUS after FTMS took the game's control point (issue #25)."""

    def trainer(self, ftms=True, nus=True, nus_uuid=NUS):
        fake = FakeBluez()
        dev = fake.device(ADDR, Connected=True, ServicesResolved=True)
        if ftms:
            fake.char(fake.service(dev, 0x10, FTMS), 0x11, FTMS_CONTROL, ["write", "indicate"])
        if nus:
            fake.char(fake.service(dev, 0x20, nus_uuid), 0x21, NUS_RX, ["write", "write-without-response"])
        return fake

    def test_hidden_beside_ftms(self):
        bz = self.trainer().handle()
        self.assertEqual(bz.services(ADDR), [{"uuid": FTMS}])

    def test_matched_whatever_the_case(self):
        bz = self.trainer(nus_uuid=NUS.upper()).handle()
        self.assertEqual(bz.services(ADDR), [{"uuid": FTMS}])

    def test_shown_without_ftms(self):
        bz = self.trainer(ftms=False).handle()
        self.assertEqual(bz.services(ADDR), [{"uuid": NUS}])

    def test_ftms_alone_is_unchanged(self):
        bz = self.trainer(nus=False).handle()
        self.assertEqual(bz.services(ADDR), [{"uuid": FTMS}])

    def test_said_once_per_device(self):
        bz = self.trainer().handle()
        with patch.object(helper, "log") as log:
            bz.services(ADDR)
            bz.services(ADDR)
        self.assertEqual(log.call_count, 1)


class OptionTypeTests(unittest.TestCase):
    """Options go out typed: without introspection, dbus-python cannot guess
    an empty dict at all and guesses a{ss} for a string one."""

    def setUp(self):
        self.fake = FakeBluez()
        self.dev, self.locked_hr, self.open_hr = self.fake.watch(ADDR)
        self.bz = self.fake.handle()
        self.ch = Mock()
        self.ch.ReadValue.return_value = []
        self.bz.char_at = Mock(return_value=self.ch)

    def assert_sv(self, options):
        self.assertIsInstance(options, dbus.Dictionary)
        self.assertEqual(options.signature, "sv")

    def test_read_options(self):
        self.bz.read(ADDR, HR_MEASUREMENT)
        self.assert_sv(self.ch.ReadValue.call_args[0][0])

    def test_write_options(self):
        self.fake.props(self.locked_hr, helper.CHAR_IFACE)["Flags"] = ["write"]
        self.bz.write(ADDR, HR_MEASUREMENT, b"\x00", True)
        self.assert_sv(self.ch.WriteValue.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
