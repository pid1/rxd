"""Tests for the offline callsign database (callsign_db.py) and rxd's use of it.

python3 -m unittest discover rxd. Nothing here downloads anything: the FCC,
Census and cty.dat files are small synthetic samples served from a local HTTP
server, and the geocoder is a fake.
"""
import gzip
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  rxd/ and clients/tui/ on sys.path
import callsign_db as cdb  # noqa: E402
import rxd  # noqa: E402

NOW = 1793967600.0  # 2026-11-06 12:20:00 UTC

# HD: 1 HD, 2 usid, 5 call, 6 status, 7 service. EN: 2 usid, 5 call, 6 entity
# type, 8 entity name, 9 first, 11 last, 16 street, 17 city, 18 state, 19 zip, 20 PO box, 23 FRN.


def hd(usid, call, status="A", service="HA"):
    f = ["HD", usid, "", "", call, status, service] + [""] * 52
    return "|".join(f)


def en(usid, call, street, city, state, zipc, po="", name="Pat Q Example", frn="0001234567"):
    first, last = name.split()[0], name.split()[-1]
    f = ["EN", usid, "", "", call, "L", "", name, first, "Q", last, "", "5551234567", "",
         "pat@example.invalid", street, city, state, zipc, po, "", "000", frn, "I", "", "", "", "", "", ""]
    return "|".join(f)


HD_LINES = [hd("1", "KA1PID"), hd("2", "W1AW"), hd("3", "K1OQX", status="E"),
            hd("4", "WQAB123", service="ZA"), hd("5", "N0BOX", service="HV"), hd("6", "KH6XYZ")]
EN_LINES = [en("1", "KA1PID", "100 Sample Rd", "Denton", "TX", "762011234"),
            en("2", "W1AW", "225 Main St", "Newington", "CT", "06111", name="Hiram Percy Maxim"),
            en("3", "K1OQX", "1 Old Way", "Oldtown", "ME", "04468"),
            en("4", "WQAB123", "9 Business Pk", "Dallas", "TX", "75201"),
            en("5", "N0BOX", "", "Smalltown", "KS", "66002", po="77"),
            en("6", "KH6XYZ", "5 Nowhere Ln", "Hilo", "HI", "99999")]
ZCTA = ("GEOID|GEOIDFQ|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG\n"
        "76201|860Z200US76201|1|0|1|0|33.2290|-97.1300\n"
        "06111|860Z200US06111|1|0|1|0|41.6880|-72.7290\n"
        "66002|860Z200US66002|1|0|1|0|39.5560|-95.1350\n")
CTY = """United States:            05:  08:  NA:   37.60:    91.87:     5.0:  K:
    AA,K,N,W,=N2QXZ/MM(7),KH6<20.0/155.0>;
Canada:                   05:  09:  NA:   44.35:    78.75:     5.0:  VE:
    VA,VE,VE3;
Canary Islands:           33:  36:  AF:   28.32:    15.85:     0.0:  EA8:
    EA8,EH8,=AO8A;
Spain:                    14:  37:  EU:   40.32:     3.43:    -1.0:  EA:
    AM,EA,EH;
Curacao:                  09:  11:  SA:   12.17:    69.00:     4.0:  PJ2:
    PJ2,PJ4;
Duplicate:                05:  08:  NA:    0.00:     0.00:     0.0:  K:
    K;
"""
SECRETS = ("Pat", "Example", "Hiram", "Maxim", "Sample Rd", "Main St", "Nowhere", "0001234567",
           "pat@example", "5551234567", "Denton", "Newington", "Business")


def zipped(name, text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return b.getvalue()


def fcc_zip(hd_lines=HD_LINES, en_lines=EN_LINES):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("HD.dat", "\r\n".join(hd_lines) + "\r\n")
        z.writestr("EN.dat", "\r\n".join(en_lines) + "\r\n")
        z.writestr("counts", "x")
    return b.getvalue()


class Files(BaseHTTPRequestHandler):
    """Serves server.files {path: bytes} with ETags, answering 304 when unchanged."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        body = self.server.files.get(self.path)
        if body is None or self.server.fail.get(self.path):
            self.send_response(self.server.fail.get(self.path) or 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        etag = '"%s"' % hashlib.md5(body).hexdigest()
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeGeocoder:
    """Places '100 Sample Rd' and '225 Main St'; anything else is No_Match."""

    PLACES = {"100 Sample Rd": (33.1050, -97.0420), "225 Main St": (41.7144, -72.7273),
              "1 New Rd": (33.2000, -97.2000), "5 Nowhere Ln": None}

    def __init__(self):
        self.calls = []

    def __call__(self, rows):
        self.calls.append(len(rows))
        return {r[0]: self.PLACES.get(r[1]) for r in rows}


class Builder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Files)
        cls.srv.files, cls.srv.fail, cls.srv.hits = {}, {}, []
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.urls = {"fcc": base + "/l_amat.zip", "zcta": base + "/zcta.zip", "cty": base + "/cty.dat"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = os.path.join(self.dir, "share", "callsigns.sqlite")
        self.work = os.path.join(self.dir, "work")
        self.srv.files.clear()
        self.srv.fail.clear()
        self.srv.files.update({"/l_amat.zip": fcc_zip(), "/zcta.zip": zipped("2025_Gaz_zcta_national.txt", ZCTA),
                               "/cty.dat": CTY.encode()})
        self.geo = FakeGeocoder()

    def update(self, **kw):
        kw.setdefault("geocoder", self.geo)
        return cdb.update(self.db, self.work, urls=self.urls, min_calls=1, min_prefixes=3, min_zips=1, **kw)

    def rows(self):
        con = sqlite3.connect(self.db)
        try:
            return {r[0]: r[1:] for r in con.execute("SELECT * FROM calls")}
        finally:
            con.close()

    def test_grid(self):
        self.assertEqual(cdb.latlon_grid(33.1050, -97.0420), "EM13lc")
        self.assertEqual(cdb.latlon_grid(33.1050, -97.0420, 4), "EM13")
        self.assertEqual(cdb.latlon_grid(-33.87, 151.21), "QF56od")
        self.assertEqual(cdb.latlon_grid(90, 180), "RR99xx")
        self.assertEqual(cdb.latlon_grid(-90, -180), "AA00aa")
        for g in ("EM13lc", "FN31pr", "QF56od", "JO62qm"):
            self.assertEqual(cdb.latlon_grid(*rxd.grid_latlon(g)), g)

    def test_parsers(self):
        active = cdb.parse_hd(HD_LINES)
        self.assertEqual(sorted(active.values()), ["KA1PID", "KH6XYZ", "N0BOX", "W1AW"])
        addrs = cdb.parse_en(EN_LINES, active)
        self.assertEqual(addrs["KA1PID"], ("100 Sample Rd", "Denton", "TX", "76201"))
        self.assertEqual(addrs["N0BOX"], ("", "Smalltown", "KS", "66002"))
        self.assertEqual(cdb.parse_zcta(ZCTA.splitlines())["06111"], (41.688, -72.729))
        self.assertEqual(cdb.parse_zcta(ZCTA.replace("|", "\t").splitlines())["76201"], (33.229, -97.13))
        with self.assertRaises(ValueError):
            cdb.parse_zcta(["NOPE|X", "1|2"])
        cty = {k: (g, e) for k, g, e in cdb.parse_cty(CTY)}
        self.assertEqual(cty["K"], (cdb.latlon_grid(37.60, -91.87, 4), "United States"))  # + West
        self.assertEqual(cty["KH6"][0], cdb.latlon_grid(20.0, -155.0, 4))  # <lat/lon> override
        self.assertEqual(cty["=AO8A"][1], "Canary Islands")
        self.assertEqual(cty["=N2QXZ/MM"][1], "United States")
        self.assertNotIn("Duplicate", [e for _, e in cty.values()])  # the first K wins

    def test_build_and_privacy(self):
        st = self.update()
        rows = self.rows()
        self.assertEqual(rows["KA1PID"], ("EM13lc", "addr", "TX"))
        self.assertEqual(rows["W1AW"][1], "addr")
        self.assertEqual(rows["N0BOX"], (cdb.latlon_grid(39.556, -95.135), "zip", "KS"))  # PO box
        self.assertNotIn("K1OQX", rows)      # expired
        self.assertNotIn("WQAB123", rows)    # not an amateur licence
        self.assertNotIn("KH6XYZ", rows)     # no geocode and no such ZIP: left to cty.dat
        self.assertEqual((st["addr"], st["zip"], st["none"]), (2, 1, 1))
        self.assertEqual(cdb.validate(self.db, min_calls=1, min_prefixes=3), [])  # the publish gate
        # nothing personal anywhere: the database, the cache, the work folder
        blobs = []
        for root, _, names in os.walk(self.dir):
            for n in names:
                with open(os.path.join(root, n), "rb") as fh:
                    blobs.append(fh.read())
        for b in blobs:
            for s in SECRETS:
                self.assertNotIn(s.encode(), b, s)
        con = sqlite3.connect(self.db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(calls)")]
        con.close()
        self.assertEqual(cols, ["call", "grid", "prec", "region"])
        # the raw downloads are gone
        self.assertEqual(sorted(os.listdir(self.work)), ["geocache.sqlite", "lock"])

    def test_unchanged_is_a_no_op(self):
        self.update()
        before = os.stat(self.db).st_mtime_ns
        self.srv.hits.clear()
        st = self.update()
        self.assertFalse(st["changed"])
        self.assertEqual(os.stat(self.db).st_mtime_ns, before)
        self.assertEqual(self.geo.calls, [3])  # geocoded once, on the first build only
        self.assertEqual(len(self.srv.hits), 3)  # three conditional GETs, all 304

    def test_incremental_geocoding(self):
        self.update()
        moved = [e if not e.startswith("EN|1|") else en("1", "KA1PID", "1 New Rd", "Denton", "TX", "76201")
                 for e in EN_LINES]
        self.srv.files["/l_amat.zip"] = fcc_zip(en_lines=moved)
        self.update()
        self.assertEqual(self.geo.calls, [3, 1])  # only the changed address
        self.assertEqual(self.rows()["KA1PID"][0], cdb.latlon_grid(33.2, -97.2))

    def test_resumes_and_keeps_the_download_until_done(self):
        st = self.update(geocode_max=1)
        self.assertEqual(st["pending"], 2)
        self.assertIn("l_amat.zip", os.listdir(self.work))
        self.assertEqual(sum(1 for r in self.rows().values() if r[1] == "addr"), 1)
        self.srv.files["/l_amat.zip"] = b"not fetched again"
        st = self.update()
        self.assertNotIn("pending", st)
        self.assertEqual(self.geo.calls, [1, 2])
        self.assertEqual(self.rows()["W1AW"][1], "addr")
        self.assertNotIn("l_amat.zip", os.listdir(self.work))

    def test_failure_keeps_the_old_database(self):
        self.update()
        with open(self.db, "rb") as fh:
            good = fh.read()
        for breakage in ({"/cty.dat": b"garbage"}, {"/l_amat.zip": b"not a zip"},
                         {"/l_amat.zip": fcc_zip(hd_lines=[])}):
            self.srv.files.update(breakage)
            if "/cty.dat" not in breakage:
                self.srv.files["/zcta.zip"] = zipped("z.txt", ZCTA + "66044|x|1|0|1|0|38.9|-95.2\n")
            with self.assertRaises(Exception):
                self.update()
            with open(self.db, "rb") as fh:
                self.assertEqual(fh.read(), good)
            self.setUp_files()
        self.srv.fail["/l_amat.zip"] = 500
        self.srv.files["/zcta.zip"] = zipped("z.txt", ZCTA + "66045|x|1|0|1|0|38.9|-95.2\n")
        with self.assertRaises(Exception):
            self.update()
        with open(self.db, "rb") as fh:
            self.assertEqual(fh.read(), good)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.db)) if n.endswith(".tmp")], [])

    def setUp_files(self):
        self.srv.files.update({"/l_amat.zip": fcc_zip(), "/zcta.zip": zipped("2025_Gaz_zcta_national.txt", ZCTA),
                               "/cty.dat": CTY.encode()})

    def test_geocoder_failure_keeps_a_usable_database(self):
        def broken(rows):
            raise RuntimeError("geocoder down")
        st = self.update(geocoder=broken)
        self.assertEqual(st["pending"], 3)
        # ZIP-level grids until the geocoder is back
        self.assertEqual(self.rows()["KA1PID"], (cdb.latlon_grid(33.229, -97.13), "zip", "TX"))
        self.update()  # and the next run finishes the job
        self.assertEqual(self.rows()["KA1PID"][1], "addr")

    def test_keyed_hashes(self):
        a = ("1 Main St", "Town", "TX", "76201")
        self.assertNotEqual(cdb.addr_hash(*a), cdb.addr_hash(*a, key="secret"))
        self.assertNotEqual(cdb.addr_hash(*a, key="secret"), cdb.addr_hash(*a, key="other"))
        self.assertEqual(cdb.addr_hash(*a, key="k"), cdb.addr_hash(" 1  MAIN st", "town", "tx", "76201", key="k"))

    def test_geocoder_response_parsing(self):
        text = ('"a1","100 SAMPLE RD, DENTON, TX, 76201","Match","Exact","100 SAMPLE RD, DENTON, TX, 76201",'
                '"-97.042,33.105","123","L"\n"a2","X","No_Match"\n"a3","Y","Tie"\n')
        self.assertEqual(cdb.parse_geocoder(text), {"a1": (33.105, -97.042), "a2": None, "a3": None})


class Lookup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.dir, "callsigns.sqlite")
        cdb.write_db(cls.db, [("KA1PID", "EM13lc", "addr", "TX"), ("W1AW", "FN31pr", "zip", "CT")],
                     cdb.parse_cty(CTY), {"schema": 1})
        cls.book = rxd.CallBook(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_calls(self):
        b = self.book
        self.assertEqual(b.lookup("KA1PID"), {"grid": "EM13lc", "prec": "addr", "region": "TX"})
        self.assertEqual(b.lookup("ka1pid/p")["prec"], "addr")
        self.assertEqual(b.lookup("KA1PID/QRP")["grid"], "EM13lc")
        self.assertEqual(b.lookup("W1AW")["prec"], "zip")
        # away from home: the country they're in, not the licence address
        self.assertEqual(b.lookup("VE3/KA1PID"), {"grid": b.lookup("VE3XYZ")["grid"], "prec": "country",
                                                  "region": "Canada"})
        self.assertEqual(b.lookup("KA1PID/VE3")["region"], "Canada")
        self.assertEqual(b.lookup("EA8/KA1PID")["region"], "Canary Islands")
        self.assertEqual(b.lookup("EA8/DL1ABC")["region"], "Canary Islands")
        self.assertEqual(b.lookup("PJ4/K1ABC")["region"], "Curacao")
        self.assertEqual(b.lookup("KA1PID/4")["prec"], "country")
        self.assertIsNone(b.lookup("KA1PID/MM"))
        # longest prefix, and exact calls first
        self.assertEqual(b.lookup("EA1ABC")["region"], "Spain")
        self.assertEqual(b.lookup("EA8ABC")["region"], "Canary Islands")
        self.assertEqual(b.lookup("AO8A")["region"], "Canary Islands")
        self.assertEqual(b.lookup("KH6ABC")["grid"], cdb.latlon_grid(20.0, -155.0, 4))
        self.assertEqual(b.lookup("K5XYZ"), {"grid": cdb.latlon_grid(37.6, -91.87, 4), "prec": "country",
                                             "region": "United States"})
        self.assertIsNone(b.lookup("ZZ9ZZ"))
        self.assertIsNone(b.lookup(""))
        self.assertIsNone(rxd.CallBook(os.path.join(self.dir, "missing.sqlite")).lookup("KA1PID"))

    def test_reopens_a_replaced_database(self):
        path = os.path.join(self.dir, "swap.sqlite")
        cdb.write_db(path, [("N0CALL", "EM13", "zip", "TX")], [], {})
        book = rxd.CallBook(path)
        self.assertEqual(book.lookup("N0CALL")["grid"], "EM13")
        cdb.write_db(path, [("N0CALL", "DM79lx", "addr", "CO")], [], {})
        self.assertEqual(book.lookup("N0CALL")["grid"], "DM79lx")


class Precedence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = os.path.join(self.dir, "callsigns.sqlite")
        cdb.write_db(self.db, [("KA1PID", "EM13lc", "addr", "TX"), ("WD4UQ", "FM18aa", "addr", "VA"),
                               ("N0ZQX", "EN34aa", "zip", "MN")], cdb.parse_cty(CTY), {"schema": 1})
        self.directed = os.path.join(self.dir, "DIRECTED.TXT")
        with open(os.path.join(self.dir, "ALL.TXT"), "w") as fh:
            fh.write("2026-11-06 11:30:30-12  0.3  605 B  3+pVJzNoOkJW         3    WD4UQ: @HB HEARTBEAT EM88 \n")
        with open(self.directed, "w") as fh:
            fh.write("2026-11-06 11:40:00\t7.078000\t900\t-3\tWD4UQ: KA1PID SNR -10\n"
                     "2026-11-06 11:41:00\t7.078000\t900\t-9\tN0ZQX: KA1PID HELLO\n"
                     "2026-11-06 11:42:00\t7.078000\t900\t-9\tVE3ABC: KA1PID HELLO\n"
                     "2026-11-06 11:43:00\t7.078000\t900\t-9\tZZ9ZZ: KA1PID HELLO\n")

    def test_heard_grid_wins_then_licence_then_country(self):
        class W:
            lock = threading.Lock()
            lines = __import__("collections").deque([
                {"time": "2026-11-06T12:00:00Z", "call": "PJ4/K1ABC", "grid": None, "snr": -20,
                 "dbm": 37, "freq": 7040100}])
        m = rxd.StationMap(js8=rxd.Js8Feed(self.directed), wspr=lambda: W, grids_path=os.path.join(self.dir, "g.json"),
                           all_path=os.path.join(self.dir, "ALL.TXT"), clock=lambda: NOW,
                           me=("KA1PID", "EM13lc"), book=rxd.CallBook(self.db))
        doc = m.snapshot()
        by = {s["call"]: s for s in doc["stations"]}
        self.assertEqual((by["WD4UQ"]["grid"], by["WD4UQ"]["src"]), ("EM88", "heard"))  # not FM18aa
        self.assertEqual((by["N0ZQX"]["grid"], by["N0ZQX"]["src"], by["N0ZQX"]["region"]), ("EN34aa", "zip", "MN"))
        self.assertIsNotNone(by["N0ZQX"]["km"])
        self.assertEqual((by["VE3ABC"]["src"], by["VE3ABC"]["region"], by["VE3ABC"]["km"]),
                         ("country", "Canada", None))
        self.assertEqual((by["PJ4/K1ABC"]["mode"], by["PJ4/K1ABC"]["src"]), ("wspr", "country"))
        self.assertNotIn("ZZ9ZZ", by)
        self.assertEqual(doc["no_grid"], {"js8": 1, "wspr": 0})
        # with no database, only heard grids count
        m2 = rxd.StationMap(js8=rxd.Js8Feed(self.directed), wspr=None, grids_path=os.path.join(self.dir, "g.json"),
                            all_path=os.path.join(self.dir, "ALL.TXT"), clock=lambda: NOW,
                            me=("KA1PID", "EM13lc"), book=rxd.CallBook(os.path.join(self.dir, "none")))
        self.assertEqual([s["call"] for s in m2.snapshot()["stations"]], ["WD4UQ"])


def good_db(path, n=3, **meta):
    rows = [("KA1PID", "EM13lc", "addr", "TX"), ("W1AW", "FN31pr", "addr", "CT"),
            ("N0BOX", "EM29qn", "zip", "KS")][:n]
    cdb.write_db(path, rows, cdb.parse_cty(CTY), dict({"schema": 1, "calls": len(rows)}, **meta))


class Validate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = os.path.join(self.dir, "c.sqlite")

    def check(self, path=None, **kw):
        kw = dict(dict(min_calls=1, min_prefixes=3), **kw)
        return cdb.validate(path or self.db, **kw)

    def test_a_good_build_passes(self):
        good_db(self.db)
        self.assertEqual(self.check(), [])

    def test_bad_builds_are_rejected(self):
        good_db(self.db)
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE calls ADD COLUMN name TEXT")
        con.commit()
        con.close()
        self.assertIn("schema", self.check()[0])
        for row in (("KA1PID", "EM13lc", "addr", "100 Sample Rd"), ("Pat Example", "EM13lc", "addr", "TX"),
                    ("KA1PID", "somewhere", "addr", "TX"), ("KA1PID", "EM13lc", "guess", "TX")):
            cdb.write_db(self.db, [row, ("W1AW", "FN31pr", "addr", "CT")], cdb.parse_cty(CTY), {"schema": 1})
            self.assertTrue(any("bad calls row" in p for p in self.check()), row)
        cdb.write_db(self.db, [("KA1PID", "EM13lc", "addr", "TX")], cdb.parse_cty(CTY), {"schema": 1, "frn": "x"})
        self.assertTrue(any("meta key" in p for p in self.check()))
        good_db(self.db)
        self.assertTrue(any("calls, outside" in p for p in self.check(min_calls=10)))
        self.assertTrue(any("prefixes" in p for p in self.check(min_prefixes=1000)))
        # spot checks
        cdb.write_db(self.db, [("KA1PID", "FN42aa", "addr", "MA"), ("W1AW", "FN31pr", "addr", "CT")],
                     cdb.parse_cty(CTY), {"schema": 1})
        self.assertTrue(any("KA1PID is at FN42aa" in p for p in self.check()))
        # too few geocoded, unless geocoding is still under way
        cdb.write_db(self.db, [("KA1PID", "EM13lc", "zip", "TX"), ("W1AW", "FN31pr", "zip", "CT")],
                     cdb.parse_cty(CTY), {"schema": 1})
        self.assertTrue(any("geocoded" in p for p in self.check()))
        cdb.write_db(self.db, [("KA1PID", "EM13lc", "zip", "TX"), ("W1AW", "FN31pr", "zip", "CT")],
                     cdb.parse_cty(CTY), {"schema": 1, "geocode_pending": 5})
        self.assertEqual(self.check(), [])
        with open(self.db, "wb") as fh:
            fh.write(b"junk" * 100)
        self.assertIn("not a readable database", self.check()[0])

    def test_counts_against_the_previous_build(self):
        prev = os.path.join(self.dir, "prev.sqlite")
        good_db(prev, 3, calls=1000)
        good_db(self.db, 3)
        self.assertTrue(any("previous build had 1000" in p for p in self.check(previous=prev)))
        good_db(prev, 3)
        self.assertEqual(self.check(previous=prev), [])


class Install(unittest.TestCase):
    """The Pi side: fetch the published build only when it changed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = os.path.join(self.dir, "share", "rxd", "callsigns.sqlite")
        src = os.path.join(self.dir, "built.sqlite")
        good_db(src)
        with open(src, "rb") as fh:
            self.gz = gzip.compress(fh.read())
        self.files = {"callsigns.sqlite.gz": self.gz,
                      "callsigns.sqlite.gz.sha256": (hashlib.sha256(self.gz).hexdigest() + "  callsigns.sqlite.gz\n").encode()}
        self.requests = []
        self.down = False

    def get(self, url, headers=None, timeout=60):
        name = url.rsplit("/", 1)[-1]
        self.requests.append((name, dict(headers or {})))
        if self.down:
            raise urllib.error.URLError("network is unreachable")
        body = self.files.get(name)
        etag = '"%s"' % hashlib.md5(body or b"").hexdigest()
        if body is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if (headers or {}).get("If-None-Match") == etag:
            raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
        return Resp(body, etag)

    def install(self):
        return cdb.install(self.db, "https://example.invalid/rel/", get=self.get)

    def test_installs_then_skips_when_unchanged(self):
        self.assertEqual(self.install(), "installed")
        self.assertEqual(rxd.CallBook(self.db).lookup("KA1PID")["grid"], "EM13lc")
        self.requests.clear()
        self.assertEqual(self.install(), "unchanged")
        # one conditional request for the checksum, answered 304; no database download
        self.assertEqual([r[0] for r in self.requests], ["callsigns.sqlite.gz.sha256"])
        self.assertIn("If-None-Match", self.requests[0][1])

    def test_same_checksum_new_etag_skips_the_download(self):
        self.install()
        self.files["callsigns.sqlite.gz.sha256"] += b"\n"  # a new ETag, the same sha256
        self.requests.clear()
        self.assertEqual(self.install(), "unchanged")
        self.assertNotIn("callsigns.sqlite.gz", [r[0] for r in self.requests])

    def old(self):
        self.install()
        with open(self.db, "rb") as fh:
            return fh.read()

    def assertKept(self, old):
        with open(self.db, "rb") as fh:
            self.assertEqual(fh.read(), old)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.db)) if n.endswith(".part")], [])

    def test_checksum_mismatch_keeps_the_old_database(self):
        old = self.old()
        other = os.path.join(self.dir, "other.sqlite")
        good_db(other, 2)
        with open(other, "rb") as fh:
            self.files["callsigns.sqlite.gz"] = gzip.compress(fh.read())
        self.files["callsigns.sqlite.gz.sha256"] = ("0" * 64 + "  callsigns.sqlite.gz\n").encode()
        with self.assertRaises(ValueError):
            self.install()
        self.assertKept(old)

    def test_network_failure_keeps_the_old_database(self):
        old = self.old()
        self.files["callsigns.sqlite.gz.sha256"] = b"f" * 64
        self.down = True
        with self.assertRaises(urllib.error.URLError):
            self.install()
        self.assertKept(old)
        self.assertEqual(cdb.main(["install", "--db", self.db, "--url", "http://127.0.0.1:9/"]), 1)
        self.assertKept(old)

    def test_a_bad_download_keeps_the_old_database(self):
        old = self.old()
        for body in (b"not gzip", gzip.compress(b"not sqlite" * 100)):
            self.files["callsigns.sqlite.gz"] = body
            self.files["callsigns.sqlite.gz.sha256"] = (hashlib.sha256(body).hexdigest() + "  x\n").encode()
            with self.assertRaises(Exception):
                self.install()
            self.assertKept(old)
        self.files["callsigns.sqlite.gz.sha256"] = b"<html>not a checksum</html>"
        with self.assertRaises(ValueError):
            self.install()
        self.assertKept(old)


class Resp(io.BytesIO):
    def __init__(self, body, etag):
        super().__init__(body)
        self.headers = {"ETag": etag}


if __name__ == "__main__":
    unittest.main()
