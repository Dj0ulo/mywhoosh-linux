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


if __name__ == "__main__":
    unittest.main()
