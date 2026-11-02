"""mDNS browsing for the OpenBike path, against a fake Avahi, without a network."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import dbus

spec = importlib.util.spec_from_file_location("blehelper", Path(__file__).resolve().parents[1] / "blehelper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

OBC = "_openbikecontrol._tcp"
BROWSER = "/Client1/ServiceBrowser1"


class FakeBus:
    """Records the signal receivers, so a test can fire Avahi's signals."""

    def __init__(self):
        self.receivers = {}

    def add_signal_receiver(self, handler, dbus_interface, signal_name, path_keyword):
        self.receivers[signal_name] = handler

    def get_object(self, name, path):
        return (name, path)

    def fire(self, signal, *args, path=BROWSER):
        self.receivers[signal](*args, path=path)


class ZeroconfTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.posted = []
        self.server = Mock()
        self.server.ServiceBrowserNew.return_value = dbus.ObjectPath(BROWSER)
        self.browser = Mock()
        self.zc = helper.Zeroconf(self.bus, self.posted.append)

        def interface(obj, iface):
            return self.server if iface == helper.Zeroconf.SERVER_IFACE else self.browser
        patcher = patch.object(helper.dbus, "Interface", side_effect=interface)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve_with(self, address="192.168.1.20", port=36867, host="bikecontrol-217e.local"):
        def call(*args, reply_handler, error_handler):
            txt = [dbus.Array([dbus.Byte(b) for b in b"version=1"])]
            reply_handler(args[0], args[1], args[2], args[3], args[4],
                          host, 0, address, dbus.UInt16(port), txt, 0)
        self.server.ResolveService.side_effect = call

    def test_browse_creates_one_ipv4_browser(self):
        self.zc.browse(OBC)
        self.zc.browse(OBC)
        self.server.ServiceBrowserNew.assert_called_once()
        args = self.server.ServiceBrowserNew.call_args[0]
        self.assertEqual((args[0], args[1], args[2]), (-1, 0, OBC))

    def test_found_service_is_posted_resolved(self):
        self.resolve_with()
        self.zc.browse(OBC)
        self.bus.fire("ItemNew", 2, 0, "BikeControl", OBC, "local", 0)
        self.assertEqual(len(self.posted), 1)
        ev = self.posted[0]
        self.assertEqual(ev["ev"], "service")
        self.assertEqual((ev["name"], ev["address"], ev["port"], ev["host"]),
                         ("BikeControl", "192.168.1.20", 36867, "bikecontrol-217e.local"))
        self.assertEqual(ev["txt"], ["version=1"])
        # The resolve asks for an IPv4 address on the interface it was seen on.
        args = self.server.ResolveService.call_args[0]
        self.assertEqual((args[0], args[5]), (2, 0))

    def test_other_browsers_on_the_bus_are_ignored(self):
        self.resolve_with()
        self.zc.browse(OBC)
        self.bus.fire("ItemNew", 2, 0, "Printer", "_ipp._tcp", "local", 0, path="/Client9/ServiceBrowser3")
        self.server.ResolveService.assert_not_called()
        self.assertEqual(self.posted, [])

    def test_second_browse_reannounces_what_was_found(self):
        self.resolve_with()
        self.zc.browse(OBC)
        self.bus.fire("ItemNew", 2, 0, "BikeControl", OBC, "local", 0)
        self.zc.browse(OBC)
        self.assertEqual([e["ev"] for e in self.posted], ["service", "service"])

    def test_removed_service_is_posted_lost_once(self):
        self.resolve_with()
        self.zc.browse(OBC)
        self.bus.fire("ItemNew", 2, 0, "BikeControl", OBC, "local", 0)
        self.bus.fire("ItemRemove", 2, 0, "BikeControl", OBC, "local", 0)
        self.bus.fire("ItemRemove", 2, 0, "BikeControl", OBC, "local", 0)
        self.assertEqual([e["ev"] for e in self.posted], ["service", "service_lost"])

    def test_service_on_two_interfaces_is_lost_only_from_both(self):
        self.resolve_with()
        self.zc.browse(OBC)
        self.bus.fire("ItemNew", 2, 0, "BikeControl", OBC, "local", 0)
        self.bus.fire("ItemNew", 3, 0, "BikeControl", OBC, "local", 0)
        self.bus.fire("ItemRemove", 2, 0, "BikeControl", OBC, "local", 0)
        self.assertEqual([e["ev"] for e in self.posted], ["service", "service"])
        self.bus.fire("ItemRemove", 3, 0, "BikeControl", OBC, "local", 0)
        self.assertEqual(self.posted[-1]["ev"], "service_lost")

    def test_stop_frees_the_browser_and_forgets(self):
        self.resolve_with()
        self.zc.browse(OBC)
        self.bus.fire("ItemNew", 2, 0, "BikeControl", OBC, "local", 0)
        self.zc.stop()
        self.browser.Free.assert_called_once()
        self.posted.clear()
        # A late result from the freed browser goes nowhere.
        self.bus.fire("ItemNew", 2, 0, "BikeControl", OBC, "local", 0)
        self.assertEqual(self.posted, [])

    def test_service_removed_mid_resolve_is_not_announced(self):
        pending = []
        self.server.ResolveService.side_effect = \
            lambda *args, reply_handler, error_handler: pending.append((args, reply_handler))
        self.zc.browse(OBC)
        self.bus.fire("ItemNew", 2, 0, "BikeControl", OBC, "local", 0)
        self.bus.fire("ItemRemove", 2, 0, "BikeControl", OBC, "local", 0)
        args, reply = pending[0]
        reply(*args[:5], "bikecontrol-217e.local", 0, "192.168.1.20", dbus.UInt16(36867), [], 0)
        self.assertEqual(self.posted, [])
        self.assertEqual(self.zc.resolved, {})

    def test_missing_avahi_is_a_plain_error(self):
        err = dbus.DBusException("no avahi", name="org.freedesktop.DBus.Error.ServiceUnknown")
        self.server.ServiceBrowserNew.side_effect = err
        with self.assertRaisesRegex(helper.BleError, "avahi-daemon is not running"):
            self.zc.browse(OBC)
        self.assertEqual(self.zc.browsers, {})


class ServerBrowseTests(unittest.TestCase):
    def setUp(self):
        self.server = helper.Server(Mock(), 0)
        self.server.zeroconf = Mock()
        self.sent = []
        self.server.send = self.sent.append

    def test_browse_is_answered_on_the_loop(self):
        self.server.handle(b'{"id":7,"op":"browse","type":"_openbikecontrol._tcp","enable":true}')
        self.server.zeroconf.browse.assert_called_once_with(OBC)
        self.assertEqual(self.sent, [{"id": 7, "ok": True}])
        self.assertTrue(self.server.work.empty())

    def test_browse_off_stops_that_type(self):
        self.server.handle(b'{"id":8,"op":"browse","type":"_openbikecontrol._tcp","enable":false}')
        self.server.zeroconf.stop.assert_called_once_with(OBC)

    def test_browse_error_reaches_the_client(self):
        self.server.zeroconf.browse.side_effect = helper.BleError("avahi-daemon is not running")
        self.server.handle(b'{"id":9,"op":"browse","type":"_openbikecontrol._tcp","enable":true}')
        self.assertEqual(self.sent[0]["ok"], False)
        self.assertIn("avahi", self.sent[0]["error"])

    def test_client_going_away_stops_browsing(self):
        self.server.drop_client()
        self.server.zeroconf.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
