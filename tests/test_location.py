"""Tests for the station's GPS location (rxd/location.py) and its use in rxd.

No real gpsd: the stream is built here, byte by byte, and served from a fake
gpsd socket where a test needs one. Test positions are made up (FN42, in
central Massachusetts, and grid centres), never the station's own.
"""
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest

import _paths  # noqa: F401  (puts rxd/ on sys.path)
import location as L
import rxd
import station

# A clone u-blox's NMEA, as seen on a real receiver: a placeholder position
# (119 degrees 60 minutes east), 0 m altitude, no satellites.
FAKE_GGA = b"$GPGGA,162907.00,3000.0000,N,11960.0000,E,1,11,0.99,-7.85,M,7.850,M,,*76\r\n"
FAKE_TPV = {"class": "TPV", "device": "/dev/ttyS0", "mode": 3, "lat": 30.0, "lon": 120.0,
            "alt": 0.0, "eph": 9.5}
EMPTY_SKY = {"class": "SKY", "device": "/dev/ttyS0", "nSat": 0, "uSat": 0, "satellites": []}

# A made-up position with digits that can be searched for: it must never
# show up anywhere but here.
SECRET = (42.1234567, -71.6543219)
SECRET_GRID = "FN42ec"
SECRET_DIGITS = ("42.12", "71.65", "1234567", "6543219")


def nav_sol(lat, lon, fix=3, sv=10, pacc_m=2.5, ok=True, h=50.0):
    x, y, z = L.geodetic_to_ecef(lat, lon, h)
    p = bytearray(52)
    struct.pack_into("<IiH", p, 0, 100000, 0, 2390)
    p[10], p[11] = fix, 0x0D if ok else 0x0C
    struct.pack_into("<iiiI", p, 12, round(x * 100), round(y * 100), round(z * 100),
                     round(pacc_m * 100))
    struct.pack_into("<H", p, 44, 150)
    p[47] = sv
    return L.ubx_frame(0x01, 0x06, bytes(p))


def nav_posllh(lat, lon, hacc_m=3.0):
    p = struct.pack("<IiiiiII", 100000, round(lon * 1e7), round(lat * 1e7), 50000, 77000,
                    round(hacc_m * 1000), 4000)
    return L.ubx_frame(0x01, 0x02, p)


def nav_status(fix=3, ok=True):
    p = bytearray(16)
    p[4], p[5] = fix, 0x0D if ok else 0x0C
    return L.ubx_frame(0x01, 0x03, bytes(p))


def js(doc):
    return json.dumps(doc).encode() + b"\r\n"


def tpv(lat, lon, mode=3, eph=12.0):
    return js({"class": "TPV", "device": "/dev/ttyS0", "mode": mode, "lat": lat, "lon": lon,
               "eph": eph})


def sky(used):
    return js({"class": "SKY", "device": "/dev/ttyS0", "nSat": used + 4, "uSat": used,
               "satellites": [{"PRN": i + 1, "used": i < used} for i in range(used + 4)]})


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def gps(hold=180.0, **kw):
    clock = Clock()
    logs = []
    g = L.GpsLocation(clock=clock, log=logs.append, hold=hold, **kw)
    g.connected = True
    return g, clock, logs


def run_epochs(g, clock, n, frame_fn, step=1.0):
    for _ in range(n):
        g.feed(frame_fn())
        clock.t += step


# ----------------------------------------------------------------- parsing

class Ubx(unittest.TestCase):
    def test_frame_and_checksum(self):
        f = L.ubx_frame(0x01, 0x06, b"\x00" * 52)
        self.assertEqual(f[:4], b"\xb5\x62\x01\x06")
        self.assertEqual(struct.unpack_from("<H", f, 4)[0], 52)
        self.assertEqual(f[-2:], L.ubx_checksum(f[2:-2]))

    def test_nav_sol(self):
        f = nav_sol(*SECRET, fix=3, sv=10, pacc_m=4.2)
        sol = L.parse_nav_sol(f[6:-2])
        self.assertEqual((sol["fix"], sol["ok"], sol["sv"], sol["acc_m"]), (3, True, 10, 4.2))
        lat, lon = L.ecef_to_geodetic(*sol["ecef"])
        self.assertAlmostEqual(lat, SECRET[0], places=6)
        self.assertAlmostEqual(lon, SECRET[1], places=6)
        self.assertIsNone(L.parse_nav_sol(b"\x00" * 20))

    def test_nav_posllh_and_status(self):
        pos = L.parse_nav_posllh(nav_posllh(*SECRET, hacc_m=3.5)[6:-2])
        self.assertAlmostEqual(pos["lat"], SECRET[0], places=6)
        self.assertAlmostEqual(pos["lon"], SECRET[1], places=6)
        self.assertEqual(pos["acc_m"], 3.5)
        self.assertEqual(L.parse_nav_status(nav_status(2, False)[6:-2]), {"fix": 2, "ok": False})

    def test_stream_of_mixed_bytes_split_anywhere(self):
        # UBX, NMEA and gpsd JSON, in one stream cut into 7-byte pieces.
        g, clock, _ = gps(hold=0)
        data = (b"\x00garbage" + js({"class": "DEVICES", "devices": [{"path": "/dev/ttyS0"}]}) +
                FAKE_GGA + nav_sol(*SECRET) + tpv(30.0, 120.0) + b"$GPZDA,1*00\r\n")
        for i in range(0, len(data), 7):
            g.feed(data[i:i + 7])
        self.assertEqual(g.live(), (SECRET_GRID, "gps/ubx"))
        self.assertEqual(g.devices, 1)
        self.assertIsNotNone(g.nmea_fake_t)

    def test_bad_checksum_is_skipped(self):
        g, clock, _ = gps(hold=0)
        f = bytearray(nav_sol(*SECRET))
        f[20] ^= 0xFF
        g.feed(bytes(f))
        self.assertIsNone(g.ubx)
        g.feed(nav_sol(*SECRET))
        self.assertEqual(g.live(), (SECRET_GRID, "gps/ubx"))

    def test_posllh_alone_with_nav_status_and_sky(self):
        g, clock, _ = gps(hold=0)
        g.feed(sky(8) + nav_status(3) + nav_posllh(*SECRET))
        self.assertEqual(g.live(), (SECRET_GRID, "gps/ubx"))
        self.assertEqual(g.status()["satellites"], 8)


class Geodesy(unittest.TestCase):
    def test_known_points(self):
        a, b = 6378137.0, 6356752.314245
        for xyz, ll in (((a, 0, 0), (0, 0)), ((0, a, 0), (0, 90)), ((-a, 0, 0), (0, 180)),
                        ((0, 0, b), (90, 0)), ((0, 0, -b), (-90, 0))):
            lat, lon = L.ecef_to_geodetic(*xyz)
            self.assertAlmostEqual(lat, ll[0], places=7)
            self.assertAlmostEqual(abs(lon) if ll[1] == 180 else lon, ll[1], places=7)

    def test_round_trip(self):
        for lat, lon, h in ((SECRET[0], SECRET[1], 50), (-33.87, 151.21, 30), (64.1, -21.9, 0),
                            (-77.85, 166.67, 2000), (0.5, -0.5, -20)):
            back = L.ecef_to_geodetic(*L.geodetic_to_ecef(lat, lon, h))
            self.assertAlmostEqual(back[0], lat, places=8)
            self.assertAlmostEqual(back[1], lon, places=8)

    def test_grid(self):
        self.assertEqual(L.maidenhead(*SECRET), SECRET_GRID)
        self.assertEqual(L.maidenhead(-33.87, 151.21), "QF56od")
        self.assertEqual(L.maidenhead(90, 180), "RR99xx")
        self.assertEqual(L.maidenhead(-90, -180), "AA00aa")
        for g in ("EM13lc", "FN31pr", "QF56od", "JO01aa"):
            self.assertEqual(L.maidenhead(*rxd.grid_latlon(g)), g)
        # through ECEF, as a receiver would report it
        self.assertEqual(L.maidenhead(*L.ecef_to_geodetic(
            *L.geodetic_to_ecef(*rxd.grid_latlon("EM13lc"), 178))), "EM13lc")


# ------------------------------------------------------------- placeholders

class Placeholders(unittest.TestCase):
    def test_the_real_fake_sentence(self):
        s = FAKE_GGA.decode().strip()
        self.assertTrue(L.nmea_ok(s))
        self.assertIn("minutes of 60", L.nmea_placeholder(s))
        self.assertFalse(L.nmea_ok(s[:-2] + "00"))
        self.assertIsNone(L.nmea_placeholder(
            "$GPRMC,162907.00,A,4207.4074,N,07139.2593,W,0.0,0.0,061126,,,A*7E"))
        self.assertIsNone(L.nmea_placeholder(L.nmea_ok.__doc__ or ""))

    def test_tpv_checks(self):
        self.assertEqual(L.tpv_problem(FAKE_TPV, 11), "placeholder")
        self.assertEqual(L.tpv_problem(dict(FAKE_TPV, lat=0.0, lon=0.0), 11), "placeholder")
        self.assertEqual(L.tpv_problem(dict(FAKE_TPV, lat=31.0, lon=121.0), 11), "placeholder")
        good = {"mode": 3, "lat": SECRET[0], "lon": SECRET[1], "eph": 15.0}
        self.assertIsNone(L.tpv_problem(good, 9))
        self.assertEqual(L.tpv_problem(dict(good, mode=1), 9), "no fix")
        self.assertEqual(L.tpv_problem(good, 0), "no satellites")
        self.assertEqual(L.tpv_problem(good, 4), "satellites")
        self.assertEqual(L.tpv_problem(good, None), "satellites")
        self.assertEqual(L.tpv_problem(dict(good, eph=250.0), 9), "accuracy")
        self.assertEqual(L.tpv_problem(good, 9, nmea_fake=True), "placeholder")

    def test_fake_nmea_real_ubx_uses_ubx(self):
        g, clock, _ = gps(hold=0)
        g.feed(js({"class": "DEVICES", "devices": [{}]}))
        for _ in range(3):
            g.feed(FAKE_GGA + js(EMPTY_SKY) + js(FAKE_TPV) + nav_sol(*SECRET, sv=10, pacc_m=2.6))
            clock.t += 1
        self.assertEqual(g.live(), (SECRET_GRID, "gps/ubx"))
        st = g.status()
        self.assertEqual((st["state"], st["source"], st["satellites"], st["accuracy_m"]),
                         ("fix", "ubx", 10, 3))
        self.assertIn("GPS position rejected: the receiver's NMEA reports a placeholder position; "
                      "using its binary fix instead", st["reason"])

    def test_fake_nmea_alone_is_rejected(self):
        g, clock, logs = gps(hold=0)
        for _ in range(3):
            g.feed(FAKE_GGA + js(EMPTY_SKY) + js(FAKE_TPV))
            clock.t += 1
        self.assertIsNone(g.live())
        st = g.status()
        self.assertEqual(st["state"], "rejected")
        self.assertIn("placeholder", st["reason"])
        self.assertTrue(any("rejected" in m for m in logs))

    def test_honest_nmea_is_used(self):
        g, clock, _ = gps(hold=0)
        g.feed(sky(9) + tpv(*SECRET, eph=15.0))
        self.assertEqual(g.live(), (SECRET_GRID, "gps/tpv"))
        self.assertEqual(g.status()["accuracy_m"], 15)

    def test_unbelievable_ubx(self):
        for kw, word in (({"sv": 3}, "only 3 satellites"), ({"pacc_m": 400}, "about 400 m"),
                         ({"fix": 1}, None), ({"ok": False}, None)):
            g, clock, _ = gps(hold=0)
            g.feed(nav_sol(*SECRET, **kw))
            self.assertIsNone(g.live(), kw)
            st = g.status()
            if word:
                self.assertEqual(st["state"], "rejected")
                self.assertIn(word, st["reason"])
            else:
                self.assertEqual(st["state"], "no fix")

    def test_ubx_wins_over_tpv(self):
        g, clock, _ = gps(hold=0)
        other = rxd.grid_latlon("FN31pr")
        g.feed(sky(9) + nav_sol(*SECRET) + tpv(*other))
        self.assertEqual(g.live(), (SECRET_GRID, "gps/ubx"))

    def test_states(self):
        g = L.GpsLocation(clock=Clock())
        self.assertEqual(g.status()["state"], "unavailable")
        g.connected = True
        g.feed(js({"class": "DEVICES", "devices": []}))
        self.assertEqual(g.status()["reason"], "gpsd has no GPS receiver")
        g, clock, _ = gps()
        g.feed(sky(0) + tpv(0, 0, mode=1))
        self.assertEqual(g.status()["state"], "no fix")
        clock.t += 60
        self.assertEqual(g.status()["state"], "unavailable")


# -------------------------------------------------------------- steadiness

class Steadiness(unittest.TestCase):
    def test_held_for_the_hold_time(self):
        g, clock, logs = gps(hold=180)
        run_epochs(g, clock, 170, lambda: nav_sol(*SECRET))
        self.assertIsNone(g.live())
        st = g.status()
        self.assertEqual(st["state"], "fix")
        self.assertIn("holds one grid square for 3 minutes", st["reason"])
        run_epochs(g, clock, 15, lambda: nav_sol(*SECRET))
        self.assertEqual(g.live(), (SECRET_GRID, "gps/ubx"))
        self.assertEqual(g.status()["reason"], "GPS fix")
        self.assertTrue(any("accepted FN42ec" in m for m in logs))

    def test_a_gap_starts_the_clock_again(self):
        g, clock, _ = gps(hold=180)
        run_epochs(g, clock, 120, lambda: nav_sol(*SECRET))
        clock.t += 60   # the fix drops out for a minute
        run_epochs(g, clock, 120, lambda: nav_sol(*SECRET))
        self.assertIsNone(g.live())
        run_epochs(g, clock, 65, lambda: nav_sol(*SECRET))
        self.assertIsNotNone(g.live())
        clock.t += 60
        self.assertIsNone(g.live())   # lost
        self.assertEqual(g.steady.accepted, SECRET_GRID)

    def test_jitter_at_an_edge_does_not_flap(self):
        lat0, lon0, h, w = L.grid_box("FN42ec")
        edge = lon0 + w     # the line between FN42ec and FN42fc
        inside, across = (lat0 + h / 2, edge - w * 0.02), (lat0 + h / 2, edge + w * 0.05)
        self.assertEqual(L.maidenhead(*across), "FN42fc")
        s = L.Steady(hold=60, gap=20)
        t = 0
        for i in range(400):
            s.offer(*(inside if i % 2 else across), t)
            t += 1
        accepted = s.accepted
        self.assertIn(accepted, ("FN42ec", "FN42fc"))
        for i in range(600):
            self.assertIsNone(s.offer(*(inside if i % 3 else across), t))
            t += 1
        self.assertEqual(s.accepted, accepted)

    def test_a_real_move_is_followed(self):
        s = L.Steady(hold=60, gap=20)
        t = 0
        for _ in range(61):
            s.offer(*SECRET, t)
            t += 1
        self.assertEqual(s.accepted, SECRET_GRID)
        there = rxd.grid_latlon("FN31pr")
        got = [s.offer(*there, t + i) for i in range(61)]
        self.assertEqual(got[-1], "FN31pr")
        self.assertEqual(s.accepted, "FN31pr")


# ------------------------------------------------- precedence and saving

class FakeGps:
    def __init__(self, live=None, state="no fix", sats=None):
        self._live, self.state, self.sats = live, state, sats
        self.on_accept = None

    def live(self):
        return self._live

    def status(self):
        return {"state": self.state, "reason": "test", "satellites": self.sats,
                "accuracy_m": 3 if self.sats else None, "source": "ubx" if self._live else None,
                "gpsd": "127.0.0.1:2947"}


class Precedence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state", "location.json")
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.dir)

    def saved(self):
        return L.SavedGrid(self.path, log=self.logs.append)

    def test_the_chain(self):
        default = ["EM13lc"]
        loc = L.Location(lambda: default[0], self.saved())
        self.assertEqual(loc.grid(), ("EM13lc", "station"))
        st = loc.status()
        self.assertEqual((st["grid_source"], st["gps"]["state"]), ("station", "off"))
        self.assertEqual(st["grid_reason"], "EM13lc from station config: GPS is off")
        default[0] = None
        self.assertEqual(loc.grid(), (None, None))
        self.assertIn("No grid", loc.status()["grid_reason"])
        default[0] = "EM13lc"

        fake = FakeGps(state="no fix")
        loc.attach(fake)
        self.assertEqual(loc.status()["grid_reason"], "EM13lc from station config: GPS has no fix")
        fake._live, fake.state, fake.sats = ("FN42ec", "gps/ubx"), "fix", 10
        fake.on_accept("FN42ec", "gps/ubx")
        st = loc.status()
        self.assertEqual((st["grid"], st["grid_source"], st["default_grid"], st["saved_grid"]),
                         ("FN42ec", "gps", "EM13lc", "FN42ec"))
        self.assertEqual(st["grid_reason"], "FN42ec from GPS (10 satellites); station config says EM13lc")
        # the fix goes: the saved one stands in
        fake._live, fake.state, fake.sats = None, "no fix", None
        st = loc.status()
        self.assertEqual((st["grid"], st["grid_source"]), ("FN42ec", "gps-saved"))
        self.assertEqual(st["grid_reason"],
                         "FN42ec from the last GPS fix: GPS has no fix; station config says EM13lc")
        # and after a restart, offline, with no GPS at all
        loc2 = L.Location(lambda: "EM13lc", self.saved())
        self.assertEqual(loc2.grid(), ("FN42ec", "gps-saved"))

    def test_settling_is_said(self):
        loc = L.Location(lambda: "EM13lc", self.saved(), FakeGps(state="fix", sats=9))
        self.assertEqual(loc.status()["grid_reason"], "EM13lc from station config: GPS is settling")

    def test_saved_atomically_and_only_the_grid(self):
        s = self.saved()
        s.save("FN42ec", "gps/ubx", when=1794009600)
        with open(self.path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc, {"grid": "FN42ec", "source": "gps/ubx", "accepted": "2026-11-07T00:00:00Z"})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["location.json"])
        mtime = os.stat(self.path).st_mtime_ns
        s.save("FN42ec", "gps/ubx")   # the same: not rewritten
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)

    def test_corrupt_state_file(self):
        os.makedirs(os.path.dirname(self.path))
        for body in ("{not json", "[]", '{"grid": "FN42", "source": "gps/ubx", "accepted": "x"}',
                     '{"grid": "FN42ec", "source": "phone", "accepted": "x"}', ""):
            with open(self.path, "w") as fh:
                fh.write(body)
            s = self.saved()
            self.assertIsNone(s.doc, body)
            self.assertIn("ignoring it", s.problem)
            loc = L.Location(lambda: "EM13lc", s)
            self.assertEqual(loc.grid(), ("EM13lc", "station"))
            self.assertIn("ignoring it", loc.status()["saved_problem"])
        s.save("FN42ec", "gps/tpv")   # a good save replaces it
        self.assertEqual(self.saved().doc["grid"], "FN42ec")
        self.assertIsNone(s.problem)


# ----------------------------------------------------------------- privacy

class Privacy(unittest.TestCase):
    def test_no_position_in_status_logs_or_state(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "location.json")
            g, clock, logs = gps(hold=30)
            loc = L.Location(lambda: "EM13lc", L.SavedGrid(path, log=logs.append))
            loc.attach(g)
            for _ in range(40):
                g.feed(FAKE_GGA + js(EMPTY_SKY) + js(FAKE_TPV) + nav_sol(*SECRET) +
                       nav_posllh(*SECRET) + tpv(*SECRET))
                clock.t += 1
            self.assertEqual(loc.grid(), (SECRET_GRID, "gps"))
            with open(path) as fh:
                saved = fh.read()
            ecef = ["%d" % abs(v) for v in L.geodetic_to_ecef(*SECRET, 50)]
            for text in (json.dumps(loc.status()), json.dumps(g.status()), "\n".join(logs), saved):
                for digits in SECRET_DIGITS + tuple(x[:6] for x in ecef):
                    self.assertNotIn(digits, text)
            self.assertIn(SECRET_GRID, saved)
            # nothing on the reader holds the position either
            held = json.dumps({k: v for k, v in vars(g).items()
                               if isinstance(v, (dict, list, tuple, str, int, float, type(None)))},
                              default=str)
            for digits in SECRET_DIGITS:
                self.assertNotIn(digits, held)
        finally:
            shutil.rmtree(d)


# ------------------------------------------------------------ fake gpsd

class FakeGpsd:
    """A gpsd that answers one WATCH with a canned stream, then idles."""

    def __init__(self, stream):
        self.stream = stream
        self.watch = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.addr = "127.0.0.1:%d" % self.sock.getsockname()[1]
        self.done = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        c, _ = self.sock.accept()
        with c:
            c.sendall(js({"class": "VERSION", "release": "3.22", "proto_major": 3}))
            self.watch = c.recv(4096)
            c.sendall(js({"class": "DEVICES", "devices": [{"path": "/dev/ttyS0",
                                                           "driver": "u-blox"}]}))
            for i in range(0, len(self.stream), 300):
                c.sendall(self.stream[i:i + 300])
            self.done.wait(10)

    def close(self):
        self.done.set()
        self.sock.close()


class OverASocket(unittest.TestCase):
    def test_reads_a_fake_gpsd(self):
        stream = b"".join(FAKE_GGA + js(EMPTY_SKY) + js(FAKE_TPV) + nav_sol(*SECRET, sv=10)
                          for _ in range(5))
        srv = FakeGpsd(stream)
        logs = []
        g = L.GpsLocation(srv.addr, log=logs.append, hold=0)
        g.start()
        try:
            for _ in range(100):
                if g.live():
                    break
                time.sleep(0.05)
            self.assertEqual(g.live(), (SECRET_GRID, "gps/ubx"))
            self.assertIn(b'"raw":2', srv.watch)
            st = g.status()
            self.assertEqual((st["state"], st["satellites"], st["gpsd"]), ("fix", 10, srv.addr))
            self.assertIn("placeholder", st["reason"])
        finally:
            g.stop.set()
            srv.close()

    def test_no_gpsd(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()   # nothing listens there now
        g = L.GpsLocation("127.0.0.1:%d" % port)
        g.RETRY_S = 60
        g.start()
        try:
            for _ in range(100):
                if g.conn_error:
                    break
                time.sleep(0.02)
            st = g.status()
            self.assertEqual(st["state"], "unavailable")
            self.assertIn("can't reach gpsd at 127.0.0.1:%d" % port, st["reason"])
        finally:
            g.stop.set()


# ---------------------------------------------------------------- in rxd

class InRxd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = {k: os.environ.get(k) for k in ("RXD_STATION", "RXD_LOCATION", "RXD_GPSD")}
        os.environ["RXD_STATION"] = os.path.join(self.dir, "station.json")
        os.environ["RXD_LOCATION"] = os.path.join(self.dir, "location.json")
        with open(os.environ["RXD_STATION"], "w") as fh:
            json.dump({"call": "KA1PID", "grid": "EM13lc"}, fh)
        rxd._location["loc"] = None
        self.gps = FakeGps(state="no fix")
        rxd.location().attach(self.gps)

    def tearDown(self):
        rxd._location["loc"] = None
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir)

    def move(self, grid):
        self.gps._live, self.gps.state, self.gps.sats = (grid, "gps/ubx"), "fix", 10
        self.gps.on_accept(grid, "gps/ubx")

    def test_status(self):
        st = rxd.station_status()
        self.assertEqual((st["call"], st["grid"], st["grid_source"], st["default_grid"]),
                         ("KA1PID", "EM13lc", "station", "EM13lc"))
        self.assertEqual(st["gps"]["state"], "no fix")
        self.move("FN42ec")
        st = rxd.station_status()
        self.assertEqual((st["grid"], st["grid_source"], st["default_grid"]),
                         ("FN42ec", "gps", "EM13lc"))
        self.assertEqual(rxd.station_id(), ("KA1PID", "FN42ec"))
        self.assertEqual(st["grid_reason"], "FN42ec from GPS (10 satellites); station config says EM13lc")

    def test_corrupt_saved_grid_is_a_station_problem(self):
        with open(os.environ["RXD_LOCATION"], "w") as fh:
            fh.write("{")
        rxd._location["loc"] = None
        st = rxd.station_status()
        self.assertEqual(st["grid_source"], "station")
        self.assertTrue(any("ignoring it" in p for p in st["problems"]))

    def test_wsprnet_picks_up_a_new_grid(self):
        sent = []
        net = rxd.WsprNet(ident=rxd.station_id, post=lambda f: (sent.append(f), (200, "1 spot(s) added"))[1])
        spot = {"date": "261106", "hhmm": "1636", "snr": -20, "dt": 0.1, "drift": 0,
                "mhz": "7.040100", "call": "K1ABC", "grid": "FN42", "dbm": 37}
        net.queue_slot([spot], 7038600)
        net.flush(7038600, True)
        self.move("FN31pr")
        net.queue_slot([spot], 7038600)
        net.flush(7038600, True)
        net.flush(7038600, False)
        self.assertEqual([f["rgrid"] for f in sent], ["EM13lc", "FN31pr", "FN31pr"])
        self.assertEqual(net.status()["grid"], "FN31pr")

    def test_wsprnet_waits_for_a_grid(self):
        with open(os.environ["RXD_STATION"], "w") as fh:
            json.dump({"call": "KA1PID"}, fh)
        sent = []
        net = rxd.WsprNet(ident=rxd.station_id, post=lambda f: (sent.append(f), (200, ""))[1])
        self.assertEqual(net.queue_slot([], 7038600), 0)
        net.flush(7038600, False)
        self.assertEqual(sent, [])
        self.move("FN42ec")
        net.flush(7038600, False)
        self.assertEqual(sent[0]["rgrid"], "FN42ec")

    def test_map_and_band_plan_follow_the_grid(self):
        d = tempfile.mkdtemp(dir=self.dir)
        m = rxd.StationMap(js8=None, wspr=lambda: None, all_path=os.path.join(d, "ALL.TXT"),
                           grids_path=os.path.join(d, "grids.json"), book=False)
        self.assertEqual(m.snapshot()["me"]["grid"], "EM13lc")
        self.move("FN42ec")
        me = m.snapshot()["me"]
        self.assertEqual((me["call"], me["grid"]), ("KA1PID", "FN42ec"))
        self.assertEqual((me["lat"], me["lon"]), rxd.grid_latlon("FN42ec"))   # the centre
        bp = rxd.BandPlanner.__new__(rxd.BandPlanner)
        bp._latlon = Ellipsis
        self.assertEqual(bp.latlon, rxd.grid_latlon("FN42ec"))
        self.move("EM13lc")
        self.assertEqual(bp.latlon, rxd.grid_latlon("EM13lc"))
        bp.latlon = (1.0, 2.0)
        self.assertEqual(bp.latlon, (1.0, 2.0))


class StationFile(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.pop("RXD_GPSD", None)

    def tearDown(self):
        if self.old is not None:
            os.environ["RXD_GPSD"] = self.old

    def test_gps_key(self):
        self.assertEqual(station.Station({}).gps, {"enabled": True, "gpsd": "127.0.0.1:2947"})
        self.assertEqual(station.Station({}).gpsd(), "127.0.0.1:2947")
        self.assertIsNone(station.Station({"gps": False}).gpsd())
        self.assertIsNone(station.Station({"gps": {"enabled": False}}).gpsd())
        self.assertEqual(station.Station({"gps": {"gpsd": "gps.local:3000"}}).gpsd(), "gps.local:3000")
        st = station.Station({"gps": {"enabled": "yes", "gpsd": "nope"}})
        self.assertEqual(st.gps, {"enabled": True, "gpsd": "127.0.0.1:2947"})
        self.assertEqual(len(st.problems), 2)
        self.assertIn("gps must be", station.Station({"gps": 3}).problems[0])

    def test_environment_wins(self):
        os.environ["RXD_GPSD"] = "off"
        self.assertIsNone(station.Station({}).gpsd())
        os.environ["RXD_GPSD"] = "[::1]:2947"
        self.assertEqual(station.Station({"gps": False}).gpsd(), "[::1]:2947")
        self.assertEqual(L.parse_hostport("[::1]:2947"), ("::1", 2947))


if __name__ == "__main__":
    unittest.main()
