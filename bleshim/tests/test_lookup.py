"""Devices are found by the address the game holds, even after a bond renames it."""
import unittest
from unittest.mock import patch

from fakebluez import ADAPTER, HR_MEASUREMENT, HR_SERVICE, FakeBluez, helper

RANDOM = "4A:11:22:33:44:55"        # what the phone advertised under, and its path
IDENTITY = "E8:5F:00:11:22:33"      # what Address reads once bonded


class DevicePathTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBluez()
        self.phone = self.fake.device(IDENTITY, path_addr=RANDOM, Connected=True, ServicesResolved=True)
        self.trainer = self.fake.device("02:00:00:00:00:01")
        self.bz = self.fake.handle()

    def test_named_path_is_found_directly(self):
        self.assertEqual(self.bz.device_path("02:00:00:00:00:01"), self.trainer)
        self.assertEqual(self.bz.device_path(RANDOM.lower()), self.phone)

    def test_bonded_address_is_found_by_its_property(self):
        self.assertEqual(self.bz.device_path(IDENTITY), self.phone)
        self.assertEqual(self.bz.device_path(IDENTITY.lower()), self.phone)

    def test_lookup_enumerates_once(self):
        self.bz.device_path(IDENTITY)
        self.assertEqual(self.bz.om.GetManagedObjects.call_count, 1)

    def test_a_given_tree_is_not_fetched_again(self):
        self.bz.device_path(IDENTITY, self.fake.tree)
        self.bz.om.GetManagedObjects.assert_not_called()

    def test_unknown_address_gives_the_named_path(self):
        self.assertEqual(self.bz.device_path("02:00:00:00:00:09"), ADAPTER + "/dev_02_00_00_00_00_09")

    def test_device_on_another_adapter_is_not_taken(self):
        self.fake.tree["/org/bluez/hci1/dev_X"] = {helper.DEVICE_IFACE: {"Address": "02:00:00:00:00:09"}}
        self.assertEqual(self.bz.device_path("02:00:00:00:00:09"), ADAPTER + "/dev_02_00_00_00_00_09")

    def test_gatt_walk_follows_the_bonded_address(self):
        svc = self.fake.service(self.phone, 0x10, HR_SERVICE)
        hr = self.fake.char(svc, 0x11, HR_MEASUREMENT, ["notify"])
        self.assertEqual(self.bz.services(IDENTITY), [{"uuid": HR_SERVICE}])
        self.assertEqual(self.bz.char_path(IDENTITY, HR_MEASUREMENT), hr)

    def test_connect_by_bonded_address(self):
        self.fake.props(self.phone, helper.DEVICE_IFACE)["Alias"] = "Phone"
        self.assertEqual(self.bz.connect(IDENTITY), "Phone")
        self.assertNotIn((self.phone, "Connect"), self.fake.calls)    # already connected

    def test_removal_of_a_renamed_device_reports_the_disconnect(self):
        s = self.fake.server()
        s.remember_paths()
        s.connected[IDENTITY] = True
        del self.fake.tree[self.phone]
        s.on_interfaces_removed(self.phone, [helper.DEVICE_IFACE])
        s.drain_outbox()
        s.send.assert_called_once_with({"ev": "connection", "addr": IDENTITY, "connected": False})


class ServicesResolvedTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBluez()
        self.dev = self.fake.device(IDENTITY, Connected=True, ServicesResolved=False, Alias="Phone")
        self.bz = self.fake.handle()
        sleep = patch.object(helper.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def connect(self, settle, timeout):
        with patch.object(helper, "SERVICES_SETTLE", settle), \
             patch.object(helper, "CONNECT_TIMEOUT", timeout):
            return self.bz.connect(IDENTITY)

    def test_present_tree_counts_once_settled(self):
        self.fake.service(self.dev, 0x10, HR_SERVICE)
        self.assertEqual(self.connect(0, 60), "Phone")

    def test_present_tree_does_not_count_before_settling(self):
        self.fake.service(self.dev, 0x10, HR_SERVICE)
        with self.assertRaisesRegex(helper.BleError, "never resolved"):
            self.connect(60, 0)

    def test_empty_tree_still_times_out(self):
        with self.assertRaisesRegex(helper.BleError, "never resolved"):
            self.connect(0, 0)

    def test_tree_of_a_dropped_link_does_not_count(self):
        self.fake.service(self.dev, 0x10, HR_SERVICE)
        self.fake.props(self.dev, helper.DEVICE_IFACE)["Connected"] = False
        self.fake.props(self.dev, helper.DEVICE_IFACE)["RSSI"] = -60
        with self.assertRaisesRegex(helper.BleError, "never resolved"):
            self.connect(0, 0)
        self.assertIn((self.dev, "Connect"), self.fake.calls)


if __name__ == "__main__":
    unittest.main()
