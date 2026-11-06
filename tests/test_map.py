"""Tests for the station map: offline tiles, /api/stations and the projection.

python3 -m unittest discover rxd. The projection tests check a Python copy of
the web UI's Web Mercator maths, and, where node is installed, run the UI's
own JavaScript (between "map:projection begin" and "end") against it.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rxd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
NOW = 1793967600.0  # 2026-11-06 12:20:00 UTC


def mkfile(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class FakeWspr:
    """What StationMap reads from a WsprDecoder: a lock and a deque of spots."""

    def __init__(self, spots):
        import collections
        self.lock = threading.Lock()
        self.lines = collections.deque(spots)


def spot(t, call, grid, snr, dbm=37):
    return {"time": t, "call": call, "grid": grid, "snr": snr, "dbm": dbm, "freq": 7040100}


# ---------------------------------------------------------------- tiles

class Tiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        # meshtastic-ui's layout: maps/<style>/z/x/y.png
        self.maps = os.path.join(self.dir, "maps")
        self.root = os.path.join(self.maps, "openstreetmap")
        mkfile(os.path.join(self.root, "1", "0", "1.png"), PNG)
        mkfile(os.path.join(self.root, "6", "14", "25.png"), PNG)
        mkfile(os.path.join(self.root, "2", "1", "1.jpg"), b"\xff\xd8\xff")
        mkfile(os.path.join(self.dir, "secret.png"), b"secret")

    def test_finds_the_style_directory_and_zooms(self):
        root, zooms = rxd.tile_root(("", "/nonexistent", self.maps))
        self.assertEqual(root, os.path.realpath(self.root))
        self.assertEqual(zooms, [1, 2, 6])
        # a z/x/y tree given directly works too
        self.assertEqual(rxd.tile_root((self.root,)), (os.path.realpath(self.root), [1, 2, 6]))
        self.assertEqual(rxd.tile_root(("/nonexistent",)), (None, []))

    def test_tile_file_rejects_traversal(self):
        root = os.path.realpath(self.root)
        self.assertEqual(rxd.tile_file(root, "/tiles/6/14/25.png")[1], "image/png")
        self.assertEqual(rxd.tile_file(root, "/tiles/2/1/1.jpg")[1], "image/jpeg")
        for bad in ("/tiles/../secret.png", "/tiles/1/../../../secret.png", "/tiles/1/0/..%2f..%2fsecret.png",
                    "/tiles/1/0/1.png/../../../secret.png", "/tiles/%2e%2e/secret.png",
                    "/tiles/-1/0/0.png", "/tiles/1/0/1.txt", "/tiles/1/0/1", "/tiles/1/2/0.png",
                    "/tiles/1/0/2.png", "/tiles/23/0/0.png", "/tiles//etc/passwd"):
            self.assertEqual(rxd.tile_file(root, bad), (None, None), bad)
        # a link inside the tree that points out of it is refused too
        os.symlink(os.path.join(self.dir, "secret.png"), os.path.join(self.root, "1", "0", "0.png"))
        self.assertEqual(rxd.tile_file(root, "/tiles/1/0/0.png"), (None, None))
        self.assertEqual(rxd.tile_file(None, "/tiles/1/0/1.png"), (None, None))


class FakeRx:
    def __init__(self, decoder=None, wspr=None):
        self.s = types.SimpleNamespace(decoder=decoder)
        self.wspr = wspr


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        root = os.path.join(cls.dir, "tiles")
        mkfile(os.path.join(root, "3", "1", "2.png"), PNG)
        mkfile(os.path.join(root, "3", "1", "3.webp"), b"RIFF")
        mkfile(os.path.join(cls.dir, "outside.png"), b"no")
        js8 = os.path.join(cls.dir, "DIRECTED.TXT")
        mkfile(js8, b"2026-11-06 12:10:00\t7.078000\t1200\t-12\tN0CALL: @HB HEARTBEAT \xe2\x99\xa2 \n")
        mkfile(os.path.join(cls.dir, "ALL.TXT"),
               b"2026-11-06 12:09:45-12  0.3 1200 A  3+pVJzNoOkJW         3    N0CALL: @HB HEARTBEAT EN34\n")
        cls.http = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
        cls.http.rx = FakeRx("wspr", FakeWspr([spot("2026-11-06T12:00:00Z", "K1ABC", "FN42", -20)]))
        cls.http.js8 = rxd.Js8Feed(js8)
        cls.http.tile_root = rxd.tile_root((root,))
        cls.http.stations = rxd.StationMap(
            js8=cls.http.js8, wspr=lambda: cls.http.rx.wspr, all_path=os.path.join(cls.dir, "ALL.TXT"),
            grids_path=os.path.join(cls.dir, "grids.json"), clock=lambda: NOW, me=("KA1PID", "EM13lc"))
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        shutil.rmtree(cls.dir)

    def get(self, path):
        url = "http://%s:%d%s" % (*self.http.server_address, path)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_tile_and_content_types(self):
        code, ctype, body = self.get("/tiles/3/1/2.png")
        self.assertEqual((code, ctype, body), (200, "image/png", PNG))
        code, ctype, body = self.get("/tiles/3/1/3.webp")
        self.assertEqual((code, ctype), (200, "image/webp"))

    def test_missing_tile_is_404(self):
        code, ctype, body = self.get("/tiles/3/1/4.png")
        self.assertEqual((code, ctype), (404, "application/json"))
        self.assertEqual(json.loads(body), {"error": "no such tile"})

    def test_traversal_is_404(self):
        for bad in ("/tiles/../outside.png", "/tiles/3/1/..%2f..%2f..%2foutside.png",
                    "/tiles/%2e%2e/%2e%2e/outside.png", "/tiles/3/../../outside.png"):
            code, ctype, body = self.get(bad)
            self.assertEqual(code, 404, bad)
            self.assertNotEqual(body, b"no")

    def test_info(self):
        code, ctype, body = self.get("/tiles/info.json")
        doc = json.loads(body)
        self.assertEqual((code, ctype), (200, "application/json"))
        self.assertEqual((doc["available"], doc["zooms"], doc["min"], doc["max"]), (True, [3], 3, 3))
        self.assertIn("OpenStreetMap", doc["attribution"])
        # with no tiles at all, the map still works: grid lines only
        saved, self.http.tile_root = self.http.tile_root, (None, [])
        try:
            code, _, body = self.get("/tiles/info.json")
            self.assertEqual(json.loads(body), {"available": False, "zooms": [], "min": None,
                                                "max": None, "attribution": None})
            self.assertEqual(self.get("/tiles/3/1/2.png")[0], 404)
        finally:
            self.http.tile_root = saved

    def test_stations(self):
        code, ctype, body = self.get("/api/stations")
        doc = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(doc["decoder"], "wspr")
        self.assertEqual(doc["me"]["grid"], "EM13lc")
        by = {(s["mode"], s["call"]): s for s in doc["stations"]}
        self.assertEqual(set(by), {("js8", "N0CALL"), ("wspr", "K1ABC")})
        self.assertEqual(by[("js8", "N0CALL")]["grid"], "EN34")
        self.assertEqual(by[("wspr", "K1ABC")]["dbm"], 37)
        self.assertEqual(self.get("/api/stations?hours=x")[0], 400)
        self.assertEqual(self.get("/api/stations?hours=0")[0], 400)


# ---------------------------------------------------------------- stations

class Stations(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.directed = os.path.join(self.dir, "DIRECTED.TXT")
        self.all = os.path.join(self.dir, "ALL.TXT")
        self.grids = os.path.join(self.dir, "rxd", "grids.json")

    def make(self, wspr=None, now=NOW):
        return rxd.StationMap(js8=rxd.Js8Feed(self.directed), wspr=lambda: wspr, all_path=self.all,
                              grids_path=self.grids, clock=lambda: now, me=("KA1PID", "EM13lc"))

    def test_js8_grid(self):
        self.assertEqual(rxd.js8_grid("WD4UQ: @HB HEARTBEAT EM88"), ("WD4UQ", "EM88"))
        self.assertEqual(rxd.js8_grid("W1AW: @ALLCALL CQ CQ CQ fn31PR ♢"), ("W1AW", "FN31pr"))
        self.assertEqual(rxd.js8_grid("K1ABC: N0CALL GRID EM13LC"), ("K1ABC", "EM13lc"))
        for none in ("WD4UQ: @HB HEARTBEAT", "KK7BXZ: N7EQQ HEARTBEAT SNR -09", "N0CALL: K1ABC EN34",
                     "@HB: HEARTBEAT EM88", "", None, "LHU/UJE//0K: @HB HEARTBEAT AR05"):
            self.assertIsNone(rxd.js8_grid(none), none)

    def test_merges_js8_and_wspr(self):
        mkfile(self.all, (
            "2026-11-06 11:00:00  7.078 MHz  JS8\n"
            "2026-11-06 11:05:15 -8 -0.0  502 A  3+pSA3e0urMi         3    WD4KQX: @HB HEARTBEAT CN87 \n"
            "2026-11-06 11:20:15-15 -0.0  502 A  3+pSA3e0urMi         3    WD4KQX: @HB HEARTBEAT CN87 \n"
            "2026-11-06 11:30:30-12  0.3  605 B  3+pVJzNoOkJW         3    WD4UQ: @HB HEARTBEAT EM88 \n"
            "2026-11-06 11:31:00-10  0.3  605 B  OD4rVbnJT6NW         1    K4XQQ: KA1PID \n").encode())
        mkfile(self.directed, (
            "2026-11-06 11:40:00\t7.078000\t900\t-3\tWD4UQ: KA1PID SNR -10 ♢ \n"
            "2026-11-06 11:41:00\t7.078000\t900\t-20\tK4XQQ: KA1PID HELLO ♢ \n"
            "2026-11-06 11:42:00\t7.078000\t900\t-20\t@HB: nonsense\n").encode())
        wspr = FakeWspr([spot("2026-11-06T12:00:00Z", "K1ABC", "FN42", -24, 23),
                         spot("2026-11-06T12:10:00Z", "K1ABC", "FN42", -18, 30),
                         spot("2026-11-06T12:12:00Z", "PJ4/K1ABC", None, -27)])
        doc = self.make(wspr).snapshot()
        by = {(s["mode"], s["call"]): s for s in doc["stations"]}
        self.assertEqual(set(by), {("js8", "WD4KQX"), ("js8", "WD4UQ"), ("wspr", "K1ABC")})
        s = by[("js8", "WD4UQ")]
        self.assertEqual((s["grid"], s["snr"], s["last"], s["last_snr"]),
                         ("EM88", -3, "2026-11-06T11:40:00Z", -3))
        self.assertEqual((s["lat"], s["lon"]), (38.5, -83.0))
        self.assertEqual(s["km"], rxd.grid_km("EM13lc", "EM88"))
        k = by[("js8", "WD4KQX")]
        self.assertEqual((k["snr"], k["last"], k["last_snr"]), (-8, "2026-11-06T11:20:15Z", -15))
        w = by[("wspr", "K1ABC")]
        self.assertEqual((w["snr"], w["dbm"], w["last"]), (-18, 30, "2026-11-06T12:10:00Z"))
        self.assertIsNone(k["dbm"])
        # newest first
        self.assertEqual(doc["stations"][0]["call"], "K1ABC")
        self.assertEqual(doc["no_grid"], {"js8": 1, "wspr": 1})  # K4XQQ, PJ4/K1ABC
        self.assertEqual(doc["me"], {"call": "KA1PID", "grid": "EM13lc",
                                     "lat": rxd.grid_latlon("EM13lc")[0], "lon": rxd.grid_latlon("EM13lc")[1]})

    def test_window(self):
        mkfile(self.directed, b"2026-11-04 11:40:00\t7.078000\t900\t-3\tWD4UQ: @HB HEARTBEAT EM88\n")
        self.assertEqual(self.make().snapshot()["stations"], [])
        self.assertEqual(len(self.make().snapshot(72)["stations"]), 1)

    def test_grids_are_remembered(self):
        mkfile(self.all, b"2026-11-06 09:30:30-12  0.3  605 B  3+pVJzNoOkJW         3    WD4UQ: @HB HEARTBEAT EM88 \n")
        self.make().snapshot()
        with open(self.grids) as fh:
            self.assertEqual(json.load(fh)["WD4UQ"]["grid"], "EM88")
        # later, with the heartbeat gone from the logs, WD4UQ is heard again
        os.remove(self.all)
        mkfile(self.directed, b"2026-11-06 12:00:00\t7.078000\t900\t-6\tWD4UQ: KA1PID SNR -10\n")
        doc = self.make().snapshot()
        self.assertEqual([(s["call"], s["grid"], s["last"]) for s in doc["stations"]],
                         [("WD4UQ", "EM88", "2026-11-06T12:00:00Z")])
        # a WSPR spot's grid is learned too, and a newer one wins
        wspr = FakeWspr([spot("2026-11-06T12:10:00Z", "WD4UQ", "EM89", -20)])
        self.make(wspr).snapshot()
        with open(self.grids) as fh:
            self.assertEqual(json.load(fh)["WD4UQ"]["grid"], "EM89")

    def test_bad_cache_is_ignored(self):
        mkfile(self.grids, b"{not json")
        self.assertEqual(self.make().snapshot()["stations"], [])
        mkfile(self.grids, json.dumps({"W1AW": {"grid": "nonsense"}, "K1": "x"}).encode())
        self.assertEqual(self.make().grids, {})

    def test_all_txt_is_read_incrementally(self):
        mkfile(self.all, b"2026-11-06 11:30:30-12  0.3  605 B  3+pVJzNoOkJW         3    WD4UQ: @HB HEARTBEAT EM88 \n")
        m = self.make()
        self.assertEqual(len(m.snapshot()["stations"]), 1)
        with open(self.all, "ab") as fh:
            fh.write(b"2026-11-06 11:50:00 -4  0.3  700 A  3+pVJzNoOkJW         3    N9BQQ: @HB HEARTBEAT EN53 \n"
                     b"2026-11-06 11:51:00 -4  0.3  700 A  3+pVJzNoO")  # a line still being written
        doc = m.snapshot()
        self.assertEqual(sorted(s["call"] for s in doc["stations"]), ["N9BQQ", "WD4UQ"])
        self.assertEqual(len(m.heard["WD4UQ"]), 1)


# ---------------------------------------------------------------- projection

TILE = 256


def merc_x(lon, z):
    return (lon + 180) / 360 * TILE * 2 ** z


def merc_y(lat, z):
    s = math.sin(max(-85.05112878, min(85.05112878, lat)) * math.pi / 180)
    return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * TILE * 2 ** z


def unmerc_y(y, z):
    return math.degrees(math.atan(math.sinh(math.pi - 2 * math.pi * y / (TILE * 2 ** z))))


def grid_px(grid, z):
    lat, lon = rxd.grid_latlon(grid)
    return merc_x(lon, z), merc_y(lat, z)


class Projection(unittest.TestCase):
    def test_known_points(self):
        self.assertEqual((merc_x(0, 0), merc_y(0, 0)), (128, 128))
        self.assertEqual((merc_x(-180, 3), merc_x(180, 3)), (0, 2048))
        self.assertAlmostEqual(merc_y(85.05112878, 2), 0, places=3)
        self.assertAlmostEqual(merc_y(-85.05112878, 2), 1024, places=3)
        self.assertEqual(merc_y(90, 2), merc_y(85.05112878, 2))  # clamped, not infinite
        for lat in (-60, -12.5, 0, 33.1, 71):
            self.assertAlmostEqual(unmerc_y(merc_y(lat, 5), 5), lat, places=9)

    def test_grid_squares_land_in_the_right_tiles(self):
        # EM13lc (north of Dallas) is in z6 tile 14/25, the tile showing Dallas
        x, y = grid_px("EM13lc", 6)
        self.assertEqual((int(x // TILE), int(y // TILE)), (14, 25))
        # a grid square's centre is half a square from its corner
        x0, _ = grid_px("EM13", 6)
        self.assertAlmostEqual(x0, merc_x(-97.0, 6))
        _, yc = grid_px("JO62", 0)  # Berlin, north of the equator: upper half
        self.assertLess(yc, 128)
        _, ys = grid_px("QF56", 0)  # Sydney: lower half
        self.assertGreater(ys, 128)
        # each zoom level doubles the pixels
        a, b = grid_px("FN31pr", 4), grid_px("FN31pr", 5)
        self.assertAlmostEqual(b[0], 2 * a[0])
        self.assertAlmostEqual(b[1], 2 * a[1])

    def run_js(self, expr):
        node = shutil.which("node")
        if not node:
            self.skipTest("node isn't installed")
        with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as fh:
            m = re.search(r"// map:projection begin\n(.*?)// map:projection end", fh.read(), re.S)
        self.assertIsNotNone(m, "the projection block is missing from index.html")
        out = subprocess.run([node, "-e", m[1] + "\nconsole.log(JSON.stringify(" + expr + "));"],
                             capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout)

    def test_javascript_matches(self):
        cases = [(lat, lon, z) for lat in (-70, -33.9, 0, 33.1042, 64.2) for lon in (-179.9, -97.04, 0, 151.2)
                 for z in (0, 1, 6, 9.5)]
        got = self.run_js("[" + ",".join("[mercX(%r,%r),mercY(%r,%r),unmercY(mercY(%r,%r),%r)]"
                                         % (lon, z, lat, z, lat, z, z) for lat, lon, z in cases) + "]")
        for (lat, lon, z), (x, y, back) in zip(cases, got):
            self.assertAlmostEqual(x, merc_x(lon, z), places=6)
            self.assertAlmostEqual(y, merc_y(lat, z), places=6)
            self.assertAlmostEqual(back, lat, places=6)

    def test_great_circle(self):
        pts = self.run_js("[greatCircle(0,0,0,90,2), greatCircle(33.1,-97.04,51.5,-0.1,32), greatCircle(10,10,10,10,4)]")
        self.assertEqual(len(pts[0]), 3)
        self.assertAlmostEqual(pts[0][1][0], 0, places=9)
        self.assertAlmostEqual(pts[0][1][1], 45, places=9)
        line = pts[1]
        self.assertAlmostEqual(line[0][0], 33.1, places=9)
        self.assertAlmostEqual(line[-1][1], -0.1, places=9)
        # Texas to London bows north of both ends, and every step is the same length
        self.assertGreater(max(p[0] for p in line), 51.5)

        def km(a, b):
            la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
            h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
            return 2 * 6371 * math.asin(math.sqrt(h))
        steps = [km(a, b) for a, b in zip(line, line[1:])]
        self.assertLess(max(steps) - min(steps), 1e-6)
        self.assertEqual(pts[2], [[10, 10]] * 5)


if __name__ == "__main__":
    unittest.main()
