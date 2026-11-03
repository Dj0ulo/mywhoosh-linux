"""The helper answers or survives whatever one request or signal throws at it."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("blehelper", Path(__file__).resolve().parents[1] / "blehelper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def server():
    bluez = helper.Bluez.__new__(helper.Bluez)
    bluez.adapter_path = "/org/bluez/hci0"
    bluez.om = Mock()
    bluez.om.GetManagedObjects.return_value = {
        "/org/bluez/hci0": {helper.ADAPTER_IFACE: {"Powered": True}}}
    s = helper.Server(bluez, 0)
    s.send = Mock()
    s.post = Mock()
    return s


class ReaderTests(unittest.TestCase):
    def test_request_that_throws_is_refused_and_reader_survives(self):
        s = server()
        conn = Mock()
        conn.recv.return_value = b'{"id":3,"op":"services"}\n{"id":4,"op":"radios"}\n'
        s.client = conn
        handled = []
        def handle(line):
            handled.append(line)
            if b'"id":3' in line:
                raise RuntimeError("boom")
        with patch.object(s, "handle", side_effect=handle):
            self.assertTrue(s.on_data(conn, 0))
        self.assertEqual(len(handled), 2)          # the second line still ran
        reply = s.send.call_args.args[0]
        self.assertEqual(reply["id"], 3)
        self.assertFalse(reply["ok"])
        self.assertIn("boom", reply["error"])

    def test_refusal_without_id_sends_nothing(self):
        s = server()
        s.refuse_unhandled(b'not json', RuntimeError("x"))
        s.refuse_unhandled(b'{"op":"scan"}', RuntimeError("x"))
        s.send.assert_not_called()

    def test_non_object_request_is_ignored(self):
        s = server()
        s.handle(b'[1, 2, 3]')
        s.handle(b'"scan"')
        s.send.assert_not_called()

    def test_scan_unexpected_error_is_refused(self):
        s = server()
        with patch.object(s, "start_scan", side_effect=KeyError("Discovering")):
            s.handle(b'{"id":9,"op":"scan","enable":true}')
        reply = s.send.call_args.args[0]
        self.assertEqual(reply["id"], 9)
        self.assertFalse(reply["ok"])
        self.assertIn("KeyError", reply["error"])


class SignalTests(unittest.TestCase):
    def test_value_without_invalidated_list_still_reaches_the_game(self):
        s = server()
        path = "/org/bluez/hci0/dev_AA/service0010/char0011"
        s.char_addr[path] = ("AA", "00002a37-0000-1000-8000-00805f9b34fb")
        s.subscribed.add(("AA", "00002a37-0000-1000-8000-00805f9b34fb"))
        s.on_properties_changed(helper.CHAR_IFACE, {"Value": [0x00, 72]}, path=path)
        event = s.post.call_args.args[0]
        self.assertEqual(event["value"], "0048")


if __name__ == "__main__":
    unittest.main()
