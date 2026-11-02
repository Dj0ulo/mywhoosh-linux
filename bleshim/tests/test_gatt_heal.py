"""GATT heal regressions: the empty-tree traps, what each one gets.

First trap: paired-but-empty (BlueZ trusting a resolved-but-empty cache)
-- the ordinary lift path, kept exactly as it mends.  Second trap: a
record re-created after a lift is UNPAIRED, and a bridge that woke with
its app closed settles connected-but-unpaired over a resolved-empty tree
-- the game has no encrypted characteristic to reach for, so pairing
never starts by itself; the heal pairs that record by hand, tolerates a
phone that declines or sleeps, and falls to the lift when the fresh
bond's own discovery still comes back empty.  The kernel can refuse with
Already Paired while the record reads unpaired -- a split no user-space
call mends; the heal says so and rests.

Fake BlueZ throughout (Mock bz handles): no hardware, no daemon.
"""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dbus

spec = importlib.util.spec_from_file_location("blehelper", Path(__file__).resolve().parents[1] / "blehelper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def record(**kw):
    props = {"Address": "02:00:00:00:00:09", "Alias": "Bridge",
             "Paired": False, "Connected": False,
             "ServicesResolved": False, "Trusted": True, "UUIDs": []}
    props.update(kw)
    return {"/org/bluez/hci5/dev_broken": {helper.DEVICE_IFACE: props}}


class HealTests(unittest.TestCase):
    UNPAIRED_LIVE = record(Paired=False, Connected=True, ServicesResolved=True)
    PAIRED_EMPTY = record(Paired=True, Connected=True, ServicesResolved=True)

    def setUp(self):
        bluez = helper.Bluez.__new__(helper.Bluez)
        bluez.adapter_path = "/org/bluez/hci5"
        bluez.om = Mock()
        bluez.om.GetManagedObjects.return_value = self.UNPAIRED_LIVE
        self.server = helper.Server(bluez, 0)
        self.bz = Mock()
        self.bz.device_props.return_value = self.UNPAIRED_LIVE["/org/bluez/hci5/dev_broken"][helper.DEVICE_IFACE]
        self.tree = [{"uuid": "00002a37-0000-1000-8000-00805f9b34fb"}]

    def heal(self):
        with patch.object(helper.time, "sleep") as _:
            self.server.maybe_rebuild_gatt(self.bz, "02:00:00:00:00:09")

    def test_connected_but_unpaired_pairs_by_hand(self):
        # The trap of the 2nd of November: link up, no bond, empty tree --
        # something only an explicit Pair can leave.
        self.bz.services.return_value = self.tree
        self.heal()
        self.bz.pair_device.assert_called_once()
        self.bz.lift_record.assert_not_called()   # the bond's discovery filled it
        self.bz.connect.assert_not_called()

    def test_connected_but_unpaired_pairs_then_falls_to_lift(self):
        # A phone app that died between pairing and discovery leaves the
        # cache empty: the settled bond then falls to the ordinary lift.
        self.bz.services.return_value = []
        self.bz.connect.return_value = "Bridge"
        self.heal()
        self.bz.pair_device.assert_called_once()
        self.bz.lift_record.assert_called_once_with("02:00:00:00:00:09")

    def test_kernel_split_refused_says_so_and_rests(self):
        # The drag from the 2nd of November, afternoon: the kernel kept the
        # bond the lift dropped, so Pair() is refused while Paired reads
        # false -- no user-space call mends it, the heal just says so.
        self.bz.pair_device.side_effect = dbus.exceptions.DBusException(
            "Already Paired", name="org.bluez.Error.AlreadyPaired")
        self.heal()
        self.bz.lift_record.assert_not_called()
        self.bz.connect.assert_not_called()
        self.assertIn("02:00:00:00:00:09", self.server.gatt_heal)

    def test_phone_asleep_refused_retries_later(self):
        # A prompt nobody saw (app sleeping, owner away) must rest inside
        # the gap, not beg at every walk.
        self.bz.pair_device.side_effect = dbus.exceptions.DBusException(
            "Authentication Failed", name="org.bluez.Error.AuthenticationFailed")
        self.heal()
        self.bz.lift_record.assert_not_called()
        self.assertIn("02:00:00:00:00:09", self.server.gatt_heal)

    def test_unpaired_unconnected_is_not_the_trap(self):
        # A asleep record (device object from a past scan) is ordinary
        # BlueZ life: the game's own flow pairs it on first touch.
        self.bz.device_props.return_value = record()["/org/bluez/hci5/dev_broken"][helper.DEVICE_IFACE]
        self.heal()
        self.bz.pair_device.assert_not_called()
        self.bz.lift_record.assert_not_called()

    def test_unpaired_still_resolving_is_not_the_trap(self):
        # A fresh link whose services are mid-walk gets no pair pressure:
        # the discovery may serve the tree in a second.
        self.bz.device_props.return_value = record(Connected=True, ServicesResolved=False)["/org/bluez/hci5/dev_broken"][helper.DEVICE_IFACE]
        self.heal()
        self.bz.pair_device.assert_not_called()

    def test_paired_empty_keeps_the_ordinary_lift(self):
        # The first trap, mended all along: paired-but-empty lifts, and the
        # fix above must not have startled it.
        self.bz.device_props.return_value = self.PAIRED_EMPTY["/org/bluez/hci5/dev_broken"][helper.DEVICE_IFACE]
        self.bz.services.return_value = []
        self.bz.connect.return_value = "Bridge"
        self.heal()
        self.bz.pair_device.assert_not_called()
        self.bz.lift_record.assert_called_once_with("02:00:00:00:00:09")

    def test_gap_rests_every_repeat(self):
        # One heal per address per gap, whichever branch mended it.
        self.bz.services.return_value = self.tree
        self.heal()
        self.heal()
        self.bz.pair_device.assert_called_once()

    def test_pair_device_without_record_is_honest(self):
        # The record vanished between the walk and the hand: say so, never
        # crash the worker.
        proxy = Mock()
        self.bluez_disabled = helper.Bluez.__new__(helper.Bluez)
        self.bluez_disabled.adapter_path = "/org/bluez/hci5"
        self.bluez_disabled.om = Mock()
        self.bluez_disabled.om.GetManagedObjects.return_value = {}
        self.bluez_disabled.bus = Mock()
        with patch.object(helper.dbus, "Interface", return_value=proxy):
            with self.assertRaisesRegex(helper.BleError, "no BlueZ record"):
                self.bluez_disabled.pair_device("02:00:00:00:00:09")
        proxy.Pair.assert_not_called()

    def test_bluez_pair_device_runs_device1_pair(self):
        # The hand call is the plain documented method, on the record's own
        # object path.
        proxy = Mock()
        bluez = helper.Bluez.__new__(helper.Bluez)
        bluez.adapter_path = "/org/bluez/hci5"
        bluez.om = Mock()
        bluez.om.GetManagedObjects.return_value = self.UNPAIRED_LIVE
        bluez.bus = Mock()
        with patch.object(helper.dbus, "Interface", return_value=proxy):
            bluez.pair_device("02:00:00:00:00:09")
        proxy.Pair.assert_called_once()


if __name__ == "__main__":
    unittest.main()
