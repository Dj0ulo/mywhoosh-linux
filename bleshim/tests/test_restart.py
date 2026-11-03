"""Readings survive the client or the helper going away for a moment."""
import unittest
from unittest.mock import Mock, patch

from fakebluez import HR_MEASUREMENT, FakeBluez, helper

ADDR = "AA:BB:CC:DD:EE:01"


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBluez()
        self.dev, self.locked_hr, self.open_hr = self.fake.watch(ADDR)
        self.s = self.fake.server()
        self.timers = Mock(return_value=41)
        for name, mock in (("timeout_add_seconds", self.timers), ("source_remove", Mock())):
            p = patch.object(helper.GLib, name, mock)
            p.start()
            self.addCleanup(p.stop)

    def subscribe(self):
        self.s.worker_dispatch("notify", {"addr": ADDR, "char": HR_MEASUREMENT, "enable": True})

    def stops(self):
        return [c for c in self.fake.calls if c[1] == "StopNotify"]

    def starts(self):
        return [c for c in self.fake.calls if c[1] == "StartNotify" and c[0] == self.open_hr]


class GraceTests(Base):
    def test_drop_keeps_subscriptions_for_the_grace_period(self):
        self.subscribe()
        self.s.client = Mock()
        self.s.drop_client()
        self.assertEqual(self.stops(), [])
        self.assertIn((ADDR, HR_MEASUREMENT), self.s.subscribed)
        self.timers.assert_called_once_with(helper.SUBSCRIPTION_GRACE, self.s.end_grace)

    def test_grace_ending_without_a_client_stops_them(self):
        self.subscribe()
        self.s.drop_client()
        self.assertFalse(self.s.end_grace())
        self.assertEqual(self.stops(), [(self.open_hr, "StopNotify")])
        self.assertFalse(self.s.subscribed)
        self.assertFalse(self.s.sub_path)
        self.assertFalse(self.s.char_addr)
        self.assertIsNone(self.s.grace_timer)

    def test_returning_client_cancels_the_grace(self):
        self.subscribe()
        self.s.drop_client()
        self.s.cancel_grace()
        helper.GLib.source_remove.assert_called_once_with(41)
        self.assertIsNone(self.s.grace_timer)
        self.assertIn((ADDR, HR_MEASUREMENT), self.s.subscribed)

    def test_grace_ending_with_a_client_back_stops_nothing(self):
        self.subscribe()
        self.s.drop_client()
        self.s.client = Mock()
        self.s.end_grace()
        self.assertEqual(self.stops(), [])
        self.assertIn((ADDR, HR_MEASUREMENT), self.s.subscribed)

    def test_values_still_flow_to_a_client_back_in_time(self):
        self.subscribe()
        self.s.drop_client()
        self.s.client = Mock()
        self.s.on_properties_changed(helper.CHAR_IFACE, {"Value": [0x00, 72]}, path=self.open_hr)
        self.s.drain_outbox()
        self.s.send.assert_called_once_with(
            {"ev": "value", "addr": ADDR, "char": HR_MEASUREMENT, "value": "0048"})

    def test_nothing_to_keep_arms_no_timer(self):
        self.s.drop_client()
        self.timers.assert_not_called()

    def test_a_second_drop_does_not_arm_a_second_timer(self):
        self.subscribe()
        self.s.drop_client()
        self.s.drop_client()
        self.timers.assert_called_once()


class HealTests(Base):
    def setUp(self):
        super().setUp()
        self.subscribe()
        self.s.client = Mock()
        self.fake.props(self.open_hr, helper.CHAR_IFACE)["Notifying"] = False
        self.fake.calls.clear()

    def test_dropped_notification_is_re_armed(self):
        self.s.heal_notifications(self.s.worker_bluez)
        self.assertEqual(self.starts(), [(self.open_hr, "StartNotify")])
        self.assertEqual(self.s.sub_path[(ADDR, HR_MEASUREMENT)], self.open_hr)

    def test_standing_notification_is_left_alone(self):
        self.fake.props(self.open_hr, helper.CHAR_IFACE)["Notifying"] = True
        self.s.heal_notifications(self.s.worker_bluez)
        self.assertEqual(self.fake.calls, [])

    def test_disconnected_device_is_left_alone(self):
        self.fake.props(self.dev, helper.DEVICE_IFACE)["Connected"] = False
        self.s.heal_notifications(self.s.worker_bluez)
        self.assertEqual(self.starts(), [])

    def test_nothing_is_re_armed_without_a_client(self):
        self.s.client = None
        self.s.heal_notifications(self.s.worker_bluez)
        self.assertEqual(self.starts(), [])

    def test_failed_re_arm_does_not_escape(self):
        self.fake.fail[(self.open_hr, "StartNotify")] = helper.dbus.exceptions.DBusException(
            "x", name="org.bluez.Error.Failed")
        self.s.run_heal()
        self.assertIn((ADDR, HR_MEASUREMENT), self.s.subscribed)

    def test_pass_is_queued_only_with_a_client_and_only_once(self):
        self.s.client = None
        self.assertTrue(self.s.schedule_heal())
        self.assertTrue(self.s.work.empty())
        self.s.client = Mock()
        self.s.schedule_heal()
        self.s.schedule_heal()
        self.assertEqual(self.s.work.qsize(), 1)
        self.assertIs(self.s.work.get_nowait()[0], helper.HEAL)
        self.s.run_heal()
        self.assertFalse(self.s.heal_queued)
        self.s.send.assert_not_called()

    def test_drop_discards_a_queued_pass(self):
        self.s.schedule_heal()
        self.s.drop_client()
        self.assertTrue(self.s.work.empty())
        self.assertFalse(self.s.heal_queued)


class RestoreTests(Base):
    def test_notifying_characteristics_are_picked_up(self):
        self.fake.props(self.open_hr, helper.CHAR_IFACE)["Notifying"] = True
        self.fake.tree["/org/bluez/hci1/dev_X/service0010/char0011"] = {
            helper.CHAR_IFACE: {"UUID": HR_MEASUREMENT, "Notifying": True}}
        self.s.restore_notifications()
        self.assertEqual(self.s.subscribed, {(ADDR, HR_MEASUREMENT)})
        self.assertEqual(self.s.sub_path, {(ADDR, HR_MEASUREMENT): self.open_hr})
        self.assertEqual(self.s.char_addr, {self.open_hr: (ADDR, HR_MEASUREMENT)})
        self.timers.assert_called_once_with(helper.SUBSCRIPTION_GRACE, self.s.end_grace)

    def test_nothing_notifying_arms_no_timer(self):
        self.s.restore_notifications()
        self.assertFalse(self.s.subscribed)
        self.timers.assert_not_called()


if __name__ == "__main__":
    unittest.main()
