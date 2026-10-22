#!/usr/bin/env python3
"""
A helper with no BlueZ in it, for testing without hardware.

Speaks exactly the protocol ./blehelper.py speaks -- newline-delimited JSON
over loopback TCP, the ops and events listed at the top of that file -- and
answers it from an in-memory model of two devices:

    AA:BB:CC:DD:EE:01   "Fake Flux 2"     cycling power (1818) + FTMS (1826)
    AA:BB:CC:DD:EE:02   "Fake HRM"        heart rate (180d)

...which is enough to walk the same path the game walks: scan, connect, GATT
tree, subscriptions with notifications flowing, an FTMS control write that gets
its indication answered.  What it cannot model is BlueZ's own quirks -- real
link timeouts, pairing, service resolution -- so it is a harness for the shim's
behaviour, not a stand-in for the real thing.

Its reason to exist is timing.  `--slow` and `--fail` inject delays and errors
per operation, so a test can hold a connect open for twenty seconds and prove
that nothing else in the shim waits for it:

    ./fakehelper.py --port 27020 --slow connect:5000 --slow read:300

`--slow op:ms` delays that op's reply; `--fail op` makes every call of that op
answer with an error.  Several may be given, comma-separated.

One request at a time would hand the tests a serial helper, which is precisely
the behaviour the real one no longer has, so each request is served in its own
thread here: a radios poll answers instantly however long a connect takes,
matching the real helper's separate main loop and worker.
"""

import argparse
import json
import socket
import sys
import threading
import time

TRAINER = "AA:BB:CC:DD:EE:01"
HRM = "AA:BB:CC:DD:EE:02"

UUID_FEATURE = "00002a5e-0000-1000-8000-00805f9b34fb"
UUID_POWER = "00002a63-0000-1000-8000-00805f9b34fb"
UUID_INDOOR = "00002ad2-0000-1000-8000-00805f9b34fb"  # indoor bike data
UUID_CONTROL = "00002ad9-0000-1000-8000-00805f9b34fb"
UUID_HR = "00002a37-0000-1000-8000-00805f9b34fb"

# The virtual GATT tree, by device.  Adjust to taste; the game only walks
# what a scan suggests and the shim only relays what the helper answers.
SERVICES = {
    TRAINER: {
        "services": [
            "00001818-0000-1000-8000-00805f9b34fb",
            "00001826-0000-1000-8000-00805f9b34fb",
        ],
        "chars": {
            "00001818-0000-1000-8000-00805f9b34fb": [
                {"uuid": UUID_POWER, "flags": ["notify"]},
                {"uuid": UUID_FEATURE, "flags": ["read"]},
            ],
            "00001826-0000-1000-8000-00805f9b34fb": [
                {"uuid": UUID_INDOOR, "flags": ["notify"]},
                {"uuid": UUID_CONTROL, "flags": ["write", "indicate"]},
            ],
        },
        "name": "Fake Flux 2",
    },
    HRM: {
        "services": [
            "0000180d-0000-1000-8000-00805f9b34fb",
        ],
        "chars": {
            "0000180d-0000-1000-8000-00805f9b34fb": [
                {"uuid": UUID_HR, "flags": ["notify"]},
            ],
        },
        "name": "Fake HRM",
    },
}

SLOW = {}
FAIL = set()


def log(msg):
    sys.stderr.write("[fakehelper] %s\n" % msg)
    sys.stderr.flush()


def slow_for(op):
    ms = SLOW.get(op, 0)
    if ms:
        time.sleep(ms / 1000.0)


class FakeHelper(threading.Thread):
    """Serve one connection the way blehelper would, from memory."""

    def __init__(self, conn):
        threading.Thread.__init__(self, daemon=True)
        self.conn = conn
        self.scanning = False
        self.subscribed = set()             # (addr, char uuid)
        self.connected = set()
        self.dead = False

    # ------------------------------------------------------------- plumbing

    def send(self, obj):
        try:
            self.conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            self.dead = True

    def reply(self, rid, ok=True, **kw):
        obj = {"id": rid, "ok": ok}
        obj.update(kw)
        self.send(obj)

    def run(self):
        buffer = b""
        while not self.dead:
            try:
                chunk = self.conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    try:
                        req = json.loads(line.decode("utf-8"))
                    except Exception:                            # noqa: BLE001
                        log("unparsable line")
                        continue
                    threading.Thread(target=self.safe_handle, args=(req,),
                                     daemon=True).start()

    def safe_handle(self, req):
        try:
            self.handle(req)
        except Exception as e:                                   # noqa: BLE001
            log("handling %r failed: %s" % (req.get("op"), e))
            try:
                self.reply(req.get("id"), ok=False, error=str(e))
            except Exception:                                    # noqa: BLE001
                pass

    # -------------------------------------------------------------- requests

    def handle(self, req):
        rid = req.get("id")
        op = req.get("op", "")

        if op in FAIL:
            self.reply(rid, ok=False, error="%s sabotaged for tests" % op)
            return

        slow_for(op)

        if op == "radios":
            self.reply(rid, radios=[{"name": "hci0",
                                     "address": "00:11:22:33:44:55",
                                     "powered": True}])
            return

        if op == "scan":
            enable = bool(req.get("enable"))
            self.scanning = enable
            self.reply(rid)
            if enable:
                self.announce()
                self.kick_feed()
            return

        addr = (req.get("addr") or "").upper()

        if op == "connect":
            if addr not in SERVICES:
                self.reply(rid, ok=False, error="%s is not on the air" % addr)
                return
            self.connected.add(addr)
            self.reply(rid, name=SERVICES[addr]["name"])
            self.send({"ev": "connection", "addr": addr, "connected": True})
            return

        if op == "disconnect":
            self.connected.discard(addr)
            self.reply(rid)
            self.send({"ev": "connection", "addr": addr, "connected": False})
            return

        if self.is_unknown(addr):
            self.reply(rid, ok=False, error="%s is not connected" % addr)
            return

        if op == "services":
            self.reply(rid, services=[{"uuid": u} for u in SERVICES[addr]["services"]])
            return

        if op == "chars":
            service = (req.get("service") or "").lower()
            chars = SERVICES[addr]["chars"].get(service, [])
            self.reply(rid, chars=chars)
            return

        char = (req.get("char") or "").lower()

        if op == "read":
            if char == UUID_FEATURE:
                self.reply(rid, value="1b0200")          # some feature bits
            elif char == UUID_INDOOR:
                self.reply(rid, value="4402ce0000006400")
            else:
                self.reply(rid, value="00")
            return

        if op == "write":
            value = bytes.fromhex(req.get("value") or "")
            self.reply(rid)
            if char == UUID_CONTROL and value[:1] == b"\x00":
                # Request Control -> the trainer's response indication.
                threading.Timer(0.05, self.send, args=(
                    {"ev": "value", "addr": addr, "char": UUID_CONTROL,
                     "value": "8000" + value[1:2].hex() + "01"},)).start()
            return

        if op == "notify":
            enable = bool(req.get("enable"))
            key = (addr, char)
            if enable:
                self.subscribed.add(key)
            else:
                self.subscribed.discard(key)
            self.reply(rid)
            return

        self.reply(rid, ok=False, error="unknown op %r" % op)

    def is_unknown(self, addr):
        return addr not in self.connected

    # ------------------------------------------------------------- adverts

    def advert(self, addr):
        uuids = SERVICES[addr]["services"]
        self.send({"ev": "advert", "addr": addr,
                   "name": SERVICES[addr]["name"],
                   "uuids": uuids,
                   "rssi": -50})

    def announce(self):
        for addr in SERVICES:
            self.advert(addr)

    # ---------------------------------------------------------- value feed

    def kick_feed(self):
        if getattr(self, "feed_started", False):
            return
        self.feed_started = True
        threading.Thread(target=self.feed, daemon=True).start()

    def feed(self):
        n = 0
        while not self.dead:
            time.sleep(0.25)
            n += 1
            for (addr, char) in list(self.subscribed):
                if char == UUID_POWER:
                    watts = 180 + (n % 40)
                    # flags byte 0x04 -- power present -- then watts LE.
                    self.send({"ev": "value", "addr": addr, "char": char,
                               "value": "04%04x" % watts})
                elif char == UUID_HR:
                    bpm = 78 + (n % 7)
                    self.send({"ev": "value", "addr": addr, "char": char,
                               "value": "00%02x" % bpm})
                elif char == UUID_CONTROL:
                    continue          # answered through write responses only
                else:
                    self.send({"ev": "value", "addr": addr, "char": char,
                               "value": "4402ce0000006400"})


def serve(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    log("listening on 127.0.0.1:%d" % port)
    while True:
        conn, _ = s.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log("shim connected")
        FakeHelper(conn).start()


def main():
    global SLOW, FAIL
    ap = argparse.ArgumentParser(description="fake blehelper for shim tests")
    ap.add_argument("--port", type=int, default=27020)
    ap.add_argument("--slow", action="append", default=[],
                    help="op:ms -- delay that op's reply (repeatable)")
    ap.add_argument("--fail", action="append", default=[],
                    help="op -- make that op always fail (repeatable)")
    args = ap.parse_args()
    for item in args.slow:
        op, _, ms = item.partition(":")
        SLOW[op.strip()] = int(ms or 0)
    for op in args.fail:
        if op.strip():
            FAIL.add(op.strip())
    log("slow=%s fail=%s" % (SLOW, sorted(FAIL)))
    serve(args.port)


if __name__ == "__main__":
    main()
