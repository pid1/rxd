"""Tests for the band plan: radio's band profiles, band conditions, the sun,
scoring and history, and BandPlanner's choices (day, night, offline, the
wrong Ham It Up side, by hand), with no network and a fake `radio`.

    python3 -m unittest discover rxd
"""
import calendar
import contextlib
import io
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  rxd/ and clients/tui/ on sys.path
import rxd  # noqa: E402
from test_radio import load_radio  # noqa: E402

# N0NBH's solarxml.php as fetched on 2026-11-06 at 11:43 UTC.
HAMQSL_SAMPLE = """<?xml version="1.0" encoding="UTF-8" ?>
<solar>
	<solardata>
		<source url="http://www.hamqsl.com/solar.html">N0NBH</source>
		<updated> 06 Nov 2026 1142 GMT</updated>
		<solarflux>124</solarflux>
		<aindex> 6</aindex>
		<kindex> 2</kindex>
		<kindexnt>No Report</kindexnt>
		<xray>B8.7</xray>
		<sunspots>109</sunspots>
		<heliumline>121.9</heliumline>
		<protonflux>13</protonflux>
		<electonflux>2210</electonflux>
		<aurora> 2</aurora>
		<normalization>1.99</normalization>
		<latdegree>66.5</latdegree>
		<solarwind>356.3</solarwind>
		<magneticfield>  5.9</magneticfield>
		<calculatedconditions>
			<band name="80m-40m" time="day">Fair</band>
			<band name="30m-20m" time="day">Good</band>
			<band name="17m-15m" time="day">Fair</band>
			<band name="12m-10m" time="day">Fair</band>
			<band name="80m-40m" time="night">Good</band>
			<band name="30m-20m" time="night">Good</band>
			<band name="17m-15m" time="night">Fair</band>
			<band name="12m-10m" time="night">Poor</band>
		</calculatedconditions>
		<calculatedvhfconditions>
			<phenomenon name="vhf-aurora" location="northern_hemi">Band Closed</phenomenon>
			<phenomenon name="E-Skip" location="europe">Band Closed</phenomenon>
			<phenomenon name="E-Skip" location="north_america">Band Closed</phenomenon>
			<phenomenon name="E-Skip" location="europe_6m">Band Closed</phenomenon>
			<phenomenon name="E-Skip" location="europe_4m">Band Closed</phenomenon>
		</calculatedvhfconditions>
		<geomagfield>QUIET</geomagfield>
		<signalnoise>S1-S2</signalnoise>
		<fof2></fof2>
		<muffactor></muffactor>
		<muf>NoRpt</muf>
	</solardata>
</solar>
"""
# NOAA SWPC, trimmed from the same morning.
SWPC_FLUX = json.dumps([
    {"time_tag": "2026-11-05T22:00:00", "frequency": 2800, "flux": 122.0},
    {"time_tag": "2026-11-05T20:00:00", "frequency": 2800, "flux": 124.0}])
SWPC_KP = json.dumps([
    {"time_tag": "2026-11-06T03:00:00", "Kp": 0.67, "a_running": 3, "station_count": 8},
    {"time_tag": "2026-11-06T06:00:00", "Kp": 2.67, "a_running": 12, "station_count": 8}])
SWPC_KP_OLD = json.dumps([["time_tag", "Kp", "a_running", "station_count"],
                          ["2026-11-06 06:00:00.000", "5.33", "56", "8"]])

FN42 = rxd.grid_latlon("FN42")   # a test location (eastern Massachusetts), not the station's
# 21 June 2026: 17:00 UTC is early afternoon there, 06:00 UTC the middle of the night.
DAY = calendar.timegm((2026, 6, 21, 17, 0, 0))
NIGHT = calendar.timegm((2026, 6, 21, 6, 0, 0))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Radio:
    """The real `radio` (rxd/radio.py), with HOME in a temp dir."""

    _mod = None
    _home = None

    @classmethod
    def mod(cls):
        if cls._mod is None:
            cls._home = tempfile.mkdtemp()
            cls._mod = load_radio(cls._home)
        return cls._mod

    @classmethod
    def json(cls):
        r = cls.mod()
        out = io.StringIO()
        old = sys.argv
        sys.argv = ["radio", "--json"]
        try:
            with contextlib.redirect_stdout(out):
                r.main()
        finally:
            sys.argv = old
        return json.loads(out.getvalue())


class Profiles(unittest.TestCase):
    JS8 = {"160m": 1843500, "80m": 3578000, "60m": 5363000, "40m": 7078000,
           "30m": 10130000, "20m": 14078000, "17m": 18104000, "15m": 21078000,
           "12m": 24922000, "10m": 28078000, "6m": 50318000, "2m": 144178000}
    WSPR = {"2190m": 136000, "630m": 474200, "160m": 1836600, "80m": 3568600,
            "60m": 5364700, "40m": 7038600, "30m": 10138700, "20m": 14095600,
            "17m": 18104600, "15m": 21094600, "12m": 24924600, "10m": 28124600,
            "6m": 50293000, "2m": 144489000}

    def setUp(self):
        self.r = Radio.mod()
        self.rows = {p["name"]: p for p in Radio.json()["profiles"]}

    def test_every_band_has_a_profile_on_its_dial(self):
        for mode, dials in (("js8", self.JS8), ("wspr", self.WSPR)):
            for band, dial in dials.items():
                name = {"40m": mode}.get(band, "%s-%s" % (mode, band))
                row = self.rows[name]
                self.assertEqual((row["freq"], row["band"], row["decoder"], row["plan"]),
                                 (float(dial), band, mode, True), name)
                self.assertEqual(row["mode"], "USB")
        self.assertEqual(len([r for r in self.rows.values() if r["plan"]]), 26)

    def test_old_names_still_work(self):
        self.assertEqual(self.r.PROFILES["js8"]["frequency"], 7078000.0)
        self.assertEqual(self.r.PROFILES["js8"]["description"], "JS8, 40 m")
        self.assertEqual(self.r.PROFILES["ghostnet"]["frequency"], 7107000.0)
        self.assertEqual(self.r.PROFILES["wspr"]["frequency"], 7038600.0)
        self.assertEqual(self.r.PROFILES["wspr"]["description"], "WSPR, 40 m")
        self.assertFalse(self.rows["ghostnet"]["plan"])  # a net, not the band plan's 40 m
        self.assertEqual(self.r.ALIASES, {"js8-40m": "js8", "wspr-40m": "wspr"})
        self.assertNotIn("js8-40m", self.rows)

    def test_band_profiles_are_the_dial_and_a_decoder(self):
        p = self.r.PROFILES["js8-20m"]
        self.assertEqual((p["decoder"], p["frequency"]), ("js8", 14078000.0))
        self.assertEqual(self.r.decoder(p), "js8")
        w = self.r.PROFILES["wspr-20m"]
        self.assertEqual((w["decoder"], w["wspr_report"], w["frequency"]),
                         ("wspr", True, 14095600.0))

    def test_upconverter_side(self):
        for name, row in self.rows.items():
            if not row["plan"]:
                continue
            self.assertEqual(row["upconvert"], row["band"] not in ("6m", "2m"), name)
            self.assertEqual(self.r.upconverting(self.r.PROFILES[name]), row["upconvert"])
        self.assertTrue(self.rows["wspr-2190m"]["upconvert"])  # 136 kHz + 125 MHz

    def test_bands_of_other_profiles(self):
        self.assertEqual(self.rows["vhf"]["band"], "2m")
        self.assertEqual(self.rows["uhf"]["band"], "70cm")
        self.assertIsNone(self.rows["fm-105.3"]["band"])
        self.assertFalse(self.rows["vhf"]["plan"])
        self.assertEqual(self.r.band_of(14095600), "20m")
        self.assertIsNone(self.r.band_of(15000000))


class Conditions(unittest.TestCase):
    def test_parse_hamqsl(self):
        c = rxd.parse_hamqsl(HAMQSL_SAMPLE.encode())
        self.assertEqual((c["source"], c["sfi"], c["a"], c["k"]), ("hamqsl", 124, 6, 2))
        self.assertEqual(c["updated"], "06 Nov 2026 1142 GMT")
        self.assertEqual(c["bands"]["30m-20m"], {"day": "Good", "night": "Good"})
        self.assertEqual(c["bands"]["12m-10m"], {"day": "Fair", "night": "Poor"})
        self.assertEqual(c["vhf"]["E-Skip north_america"], "Band Closed")
        self.assertEqual(c["geomag"], "QUIET")
        with self.assertRaises(ValueError):
            rxd.parse_hamqsl(b"<solar><solardata></solardata></solar>")

    def test_parse_swpc_both_shapes(self):
        c = rxd.parse_swpc(SWPC_FLUX, SWPC_KP)
        self.assertEqual((c["source"], c["sfi"], c["k"], c["a"]), ("swpc", 122, 3, 12))
        self.assertEqual(set(c["bands"]), {"80m-40m", "30m-20m", "17m-15m", "12m-10m"})
        old = rxd.parse_swpc(SWPC_FLUX, SWPC_KP_OLD)
        self.assertEqual((old["k"], old["a"]), (5, 56))

    def test_model_follows_the_sun_and_storms(self):
        quiet_hi = rxd.model_conditions(200, 1)
        self.assertEqual(quiet_hi["12m-10m"]["day"], "Good")
        self.assertEqual(quiet_hi["12m-10m"]["night"], "Poor")
        low = rxd.model_conditions(68, 1)
        self.assertEqual(low["12m-10m"]["day"], "Poor")
        self.assertEqual(low["80m-40m"]["night"], "Good")
        storm = rxd.model_conditions(200, 8)
        self.assertEqual(storm["30m-20m"]["day"], "Poor")

    def fetcher(self, answers):
        calls = []

        def fetch(url, timeout=10.0):
            calls.append(url)
            a = answers.get(url)
            if a is None or isinstance(a, Exception):
                raise a or urllib.error.URLError("offline")
            return a.encode() if isinstance(a, str) else a
        return fetch, calls

    def test_fetch_cache_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "conditions.json")
            fetch, calls = self.fetcher({rxd.HAMQSL_URL: HAMQSL_SAMPLE})
            t = [DAY]
            c = rxd.Conditions(path, fetch=fetch, clock=lambda: t[0])
            self.assertTrue(c.due())
            self.assertTrue(c.refresh())
            self.assertEqual(c.current()["sfi"], 124)
            self.assertFalse(c.due())
            # the next rxd, after a switch, reads the cache and doesn't fetch
            c2 = rxd.Conditions(path, fetch=fetch, clock=lambda: t[0] + 60)
            self.assertFalse(c2.due())
            self.assertEqual(c2.current()["sfi"], 124)
            self.assertEqual(len(calls), 1)
            t[0] += 46 * 60
            self.assertTrue(c.due())
            # hamqsl down: NOAA
            fetch2, calls2 = self.fetcher({rxd.SWPC_FLUX_URL: SWPC_FLUX, rxd.SWPC_KP_URL: SWPC_KP})
            c.fetch = fetch2
            self.assertTrue(c.refresh())
            self.assertEqual(c.current()["source"], "swpc")
            # everything down: keep the last for a while, retry slowly, then give up on it
            c.fetch = self.fetcher({})[0]
            t[0] += 46 * 60
            self.assertFalse(c.refresh())
            self.assertIn("hamqsl", c.error())
            self.assertEqual(c.current()["source"], "swpc")
            self.assertFalse(c.due(t[0] + 10 * 60))
            self.assertTrue(c.due(t[0] + 16 * 60))
            self.assertIsNone(c.current(t[0] + 3 * 3600))


    def test_http_get_on_its_own(self):
        # in a fresh interpreter: this test module has imported urllib.request itself
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "solar.xml")
            with open(path, "w") as fh:
                fh.write(HAMQSL_SAMPLE)
            code = ("import sys; sys.path.insert(0, %r); import rxd; "
                    "print(rxd.parse_hamqsl(rxd.http_get('file://' + %r))['sfi'])"
                    % (_paths.RXD, path))
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 timeout=60)
            self.assertEqual(out.stdout.strip(), "124", out.stderr)


class Sun(unittest.TestCase):
    def test_sun_times_ignore_the_machine_time_zone(self):
        # The station's solar day, whatever zone the machine is in: on a UTC
        # runner an American sunset is after midnight.
        old = os.environ.get("TZ")
        try:
            seen = set()
            for tz in ("UTC", "America/Chicago", "Asia/Tokyo", "Pacific/Auckland"):
                os.environ["TZ"] = tz
                time.tzset()
                rise, sset = rxd.sun_times(DAY, FN42)
                self.assertTrue(rise and sset and rise < sset, tz)
                seen.add((rise, sset))
            self.assertEqual(len(seen), 1)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_day_night_and_gray_line(self):
        self.assertEqual(rxd.day_part(DAY, FN42), "day")
        self.assertEqual(rxd.day_part(NIGHT, FN42), "night")
        self.assertGreater(rxd.sun_elevation(*FN42, DAY), 50)
        self.assertLess(rxd.sun_elevation(*FN42, NIGHT), -15)
        rise, sset = rxd.sun_times(DAY, FN42)
        self.assertTrue(rise and sset and rise < sset)
        self.assertAlmostEqual((sset - rise) / 3600, 15.2, delta=0.3)  # June, 42.5 N
        self.assertEqual(rxd.day_part(rise + 10 * 60, FN42), "gray")
        self.assertIn(rxd.day_part(DAY), ("day", "gray", "night"))  # no location: the clock


class Scoring(unittest.TestCase):
    def setUp(self):
        self.cond = rxd.parse_hamqsl(HAMQSL_SAMPLE.encode())

    def test_conditions_and_time_of_day(self):
        s = {b: rxd.band_score(b, "day", self.cond, None)[0] for b in rxd.HF_BANDS}
        self.assertEqual(max(s, key=s.get), "20m")
        self.assertLess(s["160m"], s["40m"])
        n = {b: rxd.band_score(b, "night", self.cond, None)[0] for b in rxd.HF_BANDS}
        self.assertEqual(max(n, key=n.get), "40m")
        self.assertLess(n["10m"], n["80m"])
        u = {b: rxd.band_score(b, "night", None, None) for b in rxd.HF_BANDS}
        self.assertEqual(u["40m"][1], "usual")
        self.assertEqual(rxd.cond_value("6m", self.cond, "day"), 0.1)
        self.assertEqual(rxd.cond_value("6m", dict(self.cond, vhf={"x": "50MHz ES"}), "day"), 1.0)

    def test_what_was_heard_refines_it(self):
        base = rxd.band_score("17m", "day", self.cond, None)[0]
        lots = rxd.band_score("17m", "day", self.cond, (0.95, 10))[0]
        none = rxd.band_score("17m", "day", self.cond, (0.0, 10))[0]
        self.assertGreater(lots, base)
        self.assertLess(none, base)
        self.assertGreater(rxd.band_score("17m", "day", self.cond, (0.95, 1))[0], base)

    def test_history(self):
        with tempfile.TemporaryDirectory() as d:
            t = [DAY]
            h = rxd.BandHistory(os.path.join(d, "bands.json"), clock=lambda: t[0])
            self.assertEqual(h.heard("wspr-20m", DAY, 6.0, 2.0), (0.0, 0.0, None))
            h.add("wspr-20m", DAY, 1, 10, 10, visit=1)
            h.add("wspr-20m", DAY + 120, 1, 6, 6, visit=1)   # same visit: one sample
            h.add("wspr-30m", DAY, 1, 0, 0, visit=2)
            again = rxd.BandHistory(os.path.join(d, "bands.json"), clock=lambda: t[0])
            self.assertEqual(len(again.bands["wspr-20m"]["17"]), 1)
            s20, n20, r20 = again.heard("wspr-20m", DAY, 6.0, 2.0)
            s30, n30, r30 = again.heard("wspr-30m", DAY, 6.0, 2.0)
            self.assertEqual((r20, r30), (8.0, 0.0))
            self.assertGreater(s20, 0.7)
            self.assertEqual(s30, 0.0)
            self.assertEqual((n20, n30), (1.0, 0.5))  # a slot that heard nothing still counts
            # the neighbouring hour counts for half; a month on, for a quarter
            self.assertEqual(again.heard("wspr-20m", DAY + 3600, 6.0, 2.0)[1], 0.5)
            t[0] = DAY + 28 * 86400
            self.assertAlmostEqual(again.heard("wspr-20m", DAY, 6.0, 2.0)[1], 0.25, places=3)
            t[0] = DAY + 61 * 86400
            again.add("wspr-20m", t[0], 1, 1, 1, visit=3)
            self.assertEqual([x["v"] for x in again.bands["wspr-20m"]["17"]], [3])


class FakeWspr:
    def __init__(self):
        self.last, self.seq, self.lines = None, 0, []
        self.decoding, self.slot = None, None
        self.jobs = queue.Queue()

    def since(self, after):
        return {"seq": self.seq, "lines": [x for x in self.lines if x["seq"] > after]}

    def decoded(self, t, calls):
        for c in calls:
            self.seq += 1
            self.lines.append({"seq": self.seq, "call": c})
        self.last = {"slot": time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(t)),
                     "spots": len(calls)}


class FakeJs8:
    def __init__(self):
        self.seq, self.lines = 0, []

    def heard(self, dial, *calls):
        for c in calls:
            self.seq += 1
            self.lines.append({"seq": self.seq, "freq": dial, "from": c})

    def since(self, after):
        return {"seq": self.seq, "lines": [x for x in self.lines if x["seq"] > after]}


class FakeRx:
    def __init__(self, name):
        self.s = rxd.Settings(Radio.mod().PROFILES[name], name)
        self.wspr = FakeWspr() if self.s.decoder == "wspr" else None
        self.chain = None
        self.stop = threading.Event()
        self.src = None


class SwitchFile:
    """A test's own upconverter switch file ($RXD_SWITCH), restored after."""

    def use_switch(self, position=None):
        self.switch_file = os.path.join(self.dir, "switch.json")
        old = os.environ.get("RXD_SWITCH")
        os.environ["RXD_SWITCH"] = self.switch_file
        self.addCleanup(os.environ.__setitem__, "RXD_SWITCH", old)
        self.confirm(position)

    def confirm(self, position):
        if position is None:
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.switch_file)
        else:
            rxd.station_conf.confirm_switch(position, self.switch_file, now=self.t[0])


class Planner(SwitchFile, unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conf = os.path.join(self.dir, "bands-conf.json")
        self.rows = Radio.json()
        self.switched = []
        self.t = [DAY]
        self.online = True
        self.fetches = 0
        self.use_switch("upconvert")   # the Ham It Up, confirmed on UPCONVERT

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fetch(self, url, timeout=10.0):
        self.fetches += 1
        if not self.online:
            raise urllib.error.URLError("no route to host")
        if url == rxd.HAMQSL_URL:
            return HAMQSL_SAMPLE.encode()
        raise urllib.error.URLError("not in this test")

    def planner(self, name, conf=None, js8=None):
        if conf is not None:
            with open(self.conf, "w") as fh:
                json.dump(conf, fh)
        rx = FakeRx(name)
        clock = lambda: self.t[0]  # noqa: E731
        cond = rxd.Conditions(os.path.join(self.dir, "conditions.json"), fetch=self.fetch,
                              clock=clock)
        bp = rxd.BandPlanner(rx, js8, conf_path=self.conf, data_dir=self.dir,
                             profiles=lambda: self.rows, switch=self.switched.append,
                             conditions=cond, clock=clock, latlon=FN42,
                             synced=lambda: True, js8_alive=lambda: True)
        rx.bands = bp
        return bp

    def test_day_picks_20m_by_conditions(self):
        bp = self.planner("wspr")
        bp.tick()
        self.assertEqual(self.switched, ["wspr-20m"])
        st = bp.status()
        self.assertEqual((st["plan"], st["mode"], st["phase"]), ("auto", "wspr", "hold"))
        self.assertEqual(st["reason"], "20 m: best by conditions (SFI 124, K 2, day)")
        self.assertEqual(st["conditions"]["credit"],
                         "Band conditions courtesy of N0NBH, www.hamqsl.com")
        self.assertEqual(st["bands"], list(rxd.BAND_ORDER))  # every band, by default
        self.assertEqual(st["side"], "UPCONVERT")
        # the next rxd, on 20 m, stays and doesn't fetch again
        bp2 = self.planner("wspr-20m")
        self.t[0] += 30
        bp2.tick()
        self.assertEqual(self.switched, ["wspr-20m"])
        self.assertEqual(bp2.conf["plan"], "auto")
        self.assertEqual(bp2.status()["phase"], "hold")
        self.assertEqual(self.fetches, 1)

    def test_first_start_goes_to_the_best_band(self):
        # at night 40 m and 80 m are close; with no margin on the first start,
        # the best wins even if it's only a little better
        self.t[0] = NIGHT
        bp = self.planner("wspr-80m")
        bp.tick()
        self.assertEqual(self.switched, ["wspr"])
        bp = self.planner("wspr-80m", conf={"plan": "manual"})
        bp.configure({"plan": "auto"})
        bp.tick()  # not a first start: 80 m is close enough to stay
        self.assertEqual(self.switched, ["wspr"])

    def test_night_stays_on_40m(self):
        self.t[0] = NIGHT
        bp = self.planner("wspr")
        bp.tick()
        self.assertEqual(self.switched, [])
        self.assertEqual(bp.status()["reason"],
                         "40 m: still best by conditions (SFI 124, K 2, night)")
        # re-checked after every_min, not before
        self.t[0] += 29 * 60
        bp.tick()
        self.assertEqual(bp.status()["next_at"], NIGHT + 30 * 60)

    def test_js8_mode(self):
        bp = self.planner("wspr", conf={"mode": "js8"})
        bp.tick()
        self.assertEqual(self.switched, ["js8-20m"])

    def slots(self, bp, calls_per_slot, n=2):
        """n WSPR slots decoded on bp's band, two minutes apart."""
        w = bp.rx.wspr
        for _ in range(n):
            self.t[0] += 120
            w.decoded(self.t[0] - 120, ["K%dXX" % i for i in range(calls_per_slot)])
            bp.tick()

    def test_offline_scans_then_picks_what_was_heard(self):
        self.online = False
        conf = {"bands": ["40m", "30m", "20m"]}
        bp = self.planner("wspr", conf=conf)
        bp.tick()
        st = bp.status()
        self.assertEqual(st["phase"], "scan")
        self.assertEqual(st["scan"]["i"], 1)
        self.assertTrue(st["reason"].startswith("Scanning: 40 m, 1 of 3"), st["reason"])
        self.assertEqual(self.switched, [])  # 40 m is first: it's where we are
        self.assertIsNone(st["conditions"])
        self.assertIn("hamqsl", st["conditions_error"])
        self.slots(bp, 12)           # 12 spots a slot on 40 m
        self.t[0] += 3 * 60          # past the 6 minute dwell
        bp.tick()
        self.assertEqual(len(self.switched), 1)
        nxt = self.switched[-1]
        self.assertIn(nxt, ("wspr-30m", "wspr-20m"))
        self.assertTrue(bp.status()["reason"].startswith("Scanning: "))
        # radio restarts rxd on the next band; the scan goes on there
        for k in range(2):
            name = self.switched[-1]
            bp = self.planner(name)
            self.assertEqual(bp.conf["plan"], "auto")
            bp.tick()
            self.assertEqual(bp.status()["scan"]["i"], k + 2)
            self.slots(bp, 9 if name == "wspr-30m" else 4)
            self.t[0] += 3 * 60
            bp.tick()
        # done: back to the best heard at this hour, 40 m, though 20 m is usually
        # better by day
        self.assertEqual(self.switched[-1], "wspr")
        self.assertEqual(bp.status()["reason"],
                         "40 m: best heard at this hour (12.0 spots a slot); no band conditions")
        bp = self.planner("wspr")
        bp.tick()
        st = bp.status()
        self.assertEqual(st["phase"], "hold")
        self.assertTrue(st["reason"].startswith("40 m: best heard at this hour"))
        # an hour on, the hour next door is a guide; three hours on, it learns again
        self.t[0] = DAY + 3600 + 31 * 60
        bp.tick()
        self.assertEqual(bp.status()["phase"], "hold")
        self.t[0] = DAY + 3 * 3600 + 5 * 60
        bp.tick()
        self.assertEqual(bp.status()["phase"], "scan")
        # the history is on disk, per band and hour
        doc = read(os.path.join(self.dir, "bands.json"))
        self.assertEqual(doc["bands"]["wspr-30m"]["17"][0]["count"], 18)

    def test_conditions_coming_back_end_an_offline_scan(self):
        self.online = False
        bp = self.planner("wspr")
        bp.tick()
        self.assertEqual(bp.status()["phase"], "scan")
        self.online = True
        self.t[0] += 16 * 60  # past the retry wait
        bp.tick()
        self.assertEqual(bp.status()["phase"], "hold")
        self.assertEqual(self.switched, ["wspr-20m"])

    def test_scan_waits_for_the_wspr_decode(self):
        self.online = False
        bp = self.planner("wspr", conf={"bands": ["40m", "20m"]})
        bp.tick()
        self.slots(bp, 1)
        self.t[0] += 3 * 60
        bp.rx.wspr.decoding = self.t[0] - 120
        bp.tick()
        self.assertEqual(self.switched, [])  # a switch now would lose that slot's spots
        bp.rx.wspr.decoding = None
        bp.tick()
        self.assertEqual(self.switched, ["wspr-20m"])

    def test_js8_learning(self):
        self.online = False
        js8 = FakeJs8()
        bp = self.planner("js8", conf={"mode": "js8", "bands": ["40m", "20m"]}, js8=js8)
        bp.tick()
        self.assertEqual(bp.status()["phase"], "scan")
        for _ in range(7):
            self.t[0] += 60
            js8.heard(7078000.0, "W1AW", "K1ABC")
            js8.heard(14078000.0, "N0CALL")  # another dial: not this band's
            bp.tick()
        self.assertEqual(self.switched, ["js8-20m"])
        s = bp.history.bands["js8"]["17"][0]
        self.assertEqual((s["count"], s["calls"]), (14, 14))  # 7 minutes, 2 a minute

    def test_wrong_ham_it_up_side(self):
        bp = self.planner("wspr", conf={"bands": ["40m", "6m"]})
        cands, why = bp.candidates(self.t[0])
        self.assertEqual([r["name"] for r in cands], ["wspr"])
        self.assertEqual(why["6m"], "needs the Ham It Up on BYPASS")
        rows = {r["name"]: r for r in bp.describe()["profiles"]}
        self.assertEqual((rows["wspr-6m"]["eligible"], rows["wspr-6m"]["why_not"]),
                         (False, "needs the Ham It Up on BYPASS"))
        self.assertEqual(rows["wspr-20m"]["why_not"], "not enabled")
        # 6 m is never picked on this side, and cross_side no longer changes that
        bp.tick()
        self.assertNotIn("wspr-6m", self.switched)
        bp.configure({"cross_side": True})
        self.assertNotIn("wspr-6m", [r["name"] for r in bp.candidates(self.t[0])[0]])
        self.assertEqual(bp.status()["side_limit"], "Only UPCONVERT bands: 40 m")

    def test_bypass_side_uses_only_6m_and_2m(self):
        self.confirm("bypass")
        bp = self.planner("vhf")
        self.assertEqual(bp.status()["side"], "BYPASS")
        self.assertEqual([r["band"] for r in bp.candidates(self.t[0])[0]], ["6m", "2m"])
        bp.tick()
        self.assertEqual(len(self.switched), 1)
        self.assertIn(self.switched[0], ("wspr-6m", "wspr-2m"))
        self.assertEqual(bp.status()["side_limit"], "Only BYPASS bands: 6 m, 2 m")

    def test_no_bands_on_this_side(self):
        self.confirm("bypass")
        bp = self.planner("vhf", conf={"bands": ["40m", "20m"]})
        bp.tick()
        self.assertEqual(self.switched, [])
        self.assertEqual(bp.status()["reason"],
                         "Auto: no WSPR bands enabled on the Ham It Up's BYPASS side")

    def test_paused_until_the_switch_is_confirmed(self):
        self.confirm(None)              # just started up: nobody has said
        bp = self.planner("wspr")
        bp.tick()
        st = bp.status()
        self.assertEqual(self.switched, [])
        self.assertEqual((st["phase"], st["side"], st["side_limit"]), ("paused", None, None))
        self.assertEqual(st["reason"], "Paused: confirm the Ham It Up's switch")
        self.assertEqual(bp.candidates(self.t[0])[0], [])
        rows = {r["name"]: r for r in bp.describe()["profiles"]}
        self.assertEqual(rows["wspr-20m"]["why_not"], "the Ham It Up's switch isn't confirmed")
        # a scan asked for does nothing either
        bp.configure({"scan": True})
        bp.tick()
        self.assertEqual((self.switched, bp.status()["phase"]), ([], "paused"))
        # confirmed on UPCONVERT: it chooses at once, on that side
        self.confirm("upconvert")
        self.t[0] += 5
        bp.tick()
        self.assertEqual(self.switched, ["wspr-20m"])
        self.assertEqual(bp.status()["phase"], "hold")
        self.assertTrue(bp.status()["side_limit"].startswith("Only UPCONVERT bands: 2190 m to 10 m"))

    def test_keeps_to_the_confirmed_side(self):
        # rxd came back on 40 m, but the person says the switch is on BYPASS
        self.confirm("bypass")
        bp = self.planner("wspr")
        bp.tick()
        self.assertEqual(len(self.switched), 1)
        self.assertIn(self.switched[0], ("wspr-6m", "wspr-2m"))
        self.assertEqual(bp.status()["side"], "BYPASS")
        # flipped and confirmed back: UPCONVERT bands only again
        bp = self.planner(self.switched[0])
        self.confirm("upconvert")
        self.t[0] += 60
        bp.tick()
        self.assertEqual(self.switched[-1], "wspr-20m")

    def test_nothing_learned_unless_the_switch_is_right(self):
        self.online = False
        for position, name, plan in ((None, "wspr", "auto"), (None, "wspr", "manual"),
                                     ("bypass", "wspr", "manual"),
                                     ("bypass", "wspr", "auto")):
            self.confirm(position)
            bp = self.planner(name, conf={"plan": plan, "bands": ["40m", "6m"]})
            bp.tick()
            self.slots(bp, 12)
            self.assertEqual(bp.history.bands, {}, (position, plan))
        # confirmed on the side the profile needs: it learns
        self.confirm("upconvert")
        bp = self.planner("wspr", conf={"plan": "manual"})
        bp.tick()
        self.slots(bp, 12)
        self.assertEqual(bp.history.bands["wspr"]["17"][0]["count"], 24)

    def test_js8_learns_nothing_while_unknown(self):
        self.confirm(None)
        js8 = FakeJs8()
        bp = self.planner("js8", conf={"mode": "js8", "plan": "manual"}, js8=js8)
        bp.tick()
        for _ in range(7):
            self.t[0] += 60
            js8.heard(7078000.0, "W1AW", "K1ABC")
            bp.tick()
        self.assertEqual(bp.history.bands, {})
        # confirmed: what's heard from now on counts, not what was heard before
        self.confirm("upconvert")
        for _ in range(2):
            self.t[0] += 60
            js8.heard(7078000.0, "W1AW")
            bp.tick()
        self.assertEqual(bp.history.bands["js8"]["17"][0]["count"], 2)

    def test_no_switch_no_limit(self):
        st = os.path.join(self.dir, "station.json")
        with open(st, "w") as fh:
            json.dump({"upconverter": {"name": "Ham It Up", "switch": False}}, fh)
        old = os.environ["RXD_STATION"]
        os.environ["RXD_STATION"] = st
        self.addCleanup(os.environ.__setitem__, "RXD_STATION", old)
        self.confirm(None)
        bp = self.planner("wspr", conf={"bands": ["40m", "6m"]})
        self.assertEqual([r["band"] for r in bp.candidates(self.t[0])[0]], ["40m", "6m"])
        bp.tick()
        st = bp.status()
        self.assertNotEqual(st["phase"], "paused")
        self.assertEqual((st["side"], st["side_limit"]), ("UPCONVERT", None))

    def test_switched_by_hand_goes_manual(self):
        bp = self.planner("wspr")
        bp.tick()                                   # asks for 20 m
        bp = self.planner("js8-15m")                # but radio js8-15m happened instead
        self.assertEqual(bp.conf["plan"], "manual")
        bp.tick()
        self.assertEqual(self.switched, ["wspr-20m"])
        st = bp.status()
        self.assertEqual((st["plan"], st["phase"]), ("manual", "manual"))
        self.assertIn("js8-15m was chosen outside the band plan", st["reason"])
        self.assertEqual(read(self.conf)["plan"], "manual")
        # Auto again: it chooses at once
        bp.configure({"plan": "auto"})
        bp.tick()
        self.assertEqual(self.switched[-1], "wspr-20m")

    def test_picked_by_hand_goes_manual(self):
        bp = self.planner("wspr")
        bp.manual_pick("wspr-15m")
        self.assertEqual(bp.status()["plan"], "manual")
        bp2 = self.planner("wspr-15m")
        self.assertEqual(bp2.status()["reason"], "Manual: wspr-15m picked by hand")
        bp2.tick()
        self.assertEqual(self.switched, [])

    def test_configure(self):
        bp = self.planner("wspr", conf={"plan": "manual"})
        for bad in ({"plan": "sometimes"}, {"mode": "ft8"}, {"bands": ["11m"]},
                    {"dwell_min": 1}, {"every_min": 5}, {"cross_side": "yes"},
                    {"colour": "red"}, [], {"scan": 1}):
            with self.assertRaises(ValueError, msg=bad):
                bp.configure(bad)
        doc = bp.configure({"bands": ["20m", "40m"], "dwell_min": 8, "every_min": 60})
        self.assertEqual(doc["config"]["bands"], ["40m", "20m"])  # band order
        self.assertEqual(doc["status"]["plan"], "manual")
        self.assertEqual(len(doc["profiles"]), 26)
        self.assertEqual(doc["all_bands"][0], "2190m")
        doc = bp.configure({"bands": None})
        self.assertTrue(doc["status"]["default_bands"])
        # scan now: Auto, and a scan of the enabled bands from here
        doc = bp.configure({"scan": True, "bands": ["40m", "30m"]})
        self.assertEqual((doc["config"]["plan"], doc["status"]["phase"]), ("auto", "scan"))
        self.assertEqual(doc["status"]["scan"]["n"], 2)
        self.assertEqual(read(self.conf)["dwell_min"], 8)


class SwitchingThroughRadio(SwitchFile, unittest.TestCase):
    """The planner's switch goes through spawn_switch to `radio NAME`."""

    def test_fake_radio(self):
        with tempfile.TemporaryDirectory() as d:
            self.dir, self.t = d, [DAY]
            self.use_switch("upconvert")
            log = os.path.join(d, "radio.log")
            fake = os.path.join(d, "radio")
            rows = Radio.json()
            with open(fake, "w") as fh:
                fh.write("#!/bin/sh\nif [ \"$1\" = --json ]; then cat '%s'; else echo \"$@\" >> '%s'; fi\n"
                         % (os.path.join(d, "rows.json"), log))
            with open(os.path.join(d, "rows.json"), "w") as fh:
                json.dump(rows, fh)
            os.chmod(fake, 0o755)
            old = rxd.RADIO, rxd.shutil.which
            rxd.RADIO = fake
            rxd._profiles["doc"] = None
            rxd.shutil.which = lambda n: None if n == "systemd-run" else shutil.which(n)
            try:
                rx = FakeRx("wspr")
                cond = rxd.Conditions(os.path.join(d, "c.json"),
                                      fetch=lambda u, timeout=10: HAMQSL_SAMPLE.encode(),
                                      clock=lambda: DAY)
                bp = rxd.BandPlanner(rx, None, conf_path=os.path.join(d, "conf.json"),
                                     data_dir=d, conditions=cond, clock=lambda: DAY,
                                     latlon=FN42, synced=lambda: True)
                bp.tick()
                for _ in range(50):
                    if os.path.exists(log):
                        break
                    time.sleep(0.05)
                with open(log) as fh:
                    self.assertEqual(fh.read(), "wspr-20m\n")
            finally:
                rxd.RADIO, rxd.shutil.which = old
                rxd._profiles["doc"] = None


class Api(SwitchFile, unittest.TestCase):
    def api(self, srv, method, path, doc=None):
        req = urllib.request.Request(
            "http://%s:%d%s" % (*srv.server_address, path), method=method,
            data=None if doc is None else json.dumps(doc).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_get_and_post(self):
        with tempfile.TemporaryDirectory() as d:
            self.dir, self.t = d, [DAY]
            self.use_switch("upconvert")
            rows = Radio.json()
            rx = FakeRx("wspr")
            cond = rxd.Conditions(os.path.join(d, "c.json"),
                                  fetch=lambda u, timeout=10: HAMQSL_SAMPLE.encode(),
                                  clock=lambda: DAY)
            spawned = []
            rx.bands = rxd.BandPlanner(rx, None, conf_path=os.path.join(d, "conf.json"),
                                       data_dir=d, profiles=lambda: rows,
                                       switch=spawned.append, conditions=cond,
                                       clock=lambda: DAY, latlon=FN42, synced=lambda: True)
            srv = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
            srv.rx = rx
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            old = rxd.list_profiles, rxd.spawn_switch
            rxd.list_profiles = lambda: rows
            rxd.spawn_switch = spawned.append
            try:
                code, doc = self.api(srv, "GET", "/api/bands")
                self.assertEqual((code, doc["config"]["plan"]), (200, "auto"))
                self.assertEqual(doc["status"]["profile"], "wspr")
                code, doc = self.api(srv, "POST", "/api/bands", {"mode": "js8", "every_min": 45})
                self.assertEqual((code, doc["config"]["mode"], doc["config"]["every_min"]),
                                 (200, "js8", 45))
                self.assertEqual(self.api(srv, "POST", "/api/bands", {"mode": "ft8"})[0], 400)
                # a band picked by hand puts the plan on Manual
                code, _ = self.api(srv, "POST", "/api/profile", {"name": "wspr-17m"})
                self.assertEqual((code, spawned), (202, ["wspr-17m"]))
                self.assertEqual(rx.bands.status()["plan"], "manual")
                rx.bands = None
                self.assertEqual(self.api(srv, "GET", "/api/bands")[0], 503)
            finally:
                rxd.list_profiles, rxd.spawn_switch = old
                srv.shutdown()


class Tui(unittest.TestCase):
    """rxd-tui's Digital view: the band plan line and the a, m and s keys."""

    def setUp(self):
        import test_rxd_tui as tt
        self.tt = tt
        self.t = tt.t
        bp = {"plan": "auto", "mode": "wspr", "phase": "hold", "profile": "wspr",
              "reason": "20 m: best by conditions (SFI 124, K 2, day)", "next_at": DAY + 1800,
              "sun": {"part": "day", "words": "day"},
              "conditions": {"source": "hamqsl", "sfi": 124, "a": 6, "k": 2}}
        self.app = tt.app_on("wspr")
        self.app.p.status["band_plan"] = bp
        self.posts = []
        self.app.p.bands = self.posts.append

    def test_words(self):
        g = self.tt.U
        text, role = self.t.band_plan_words(self.app.p.status["band_plan"], g)
        self.assertIn("Band plan Auto", text)
        self.assertIn("20 m: best by conditions (SFI 124, K 2, day)", text)
        self.assertEqual(role, "ok")
        extra = self.t.band_plan_extra(self.app.p.status["band_plan"], g)
        self.assertIn("N0NBH, hamqsl.com", extra)
        self.assertIn("next look", extra)
        man = self.t.band_plan_words({"plan": "manual", "reason": "Manual: staying on wspr"}, g)
        self.assertEqual(man[1], "label")
        self.assertIn("Band plan Manual: staying on wspr", man[0])
        self.assertIsNone(self.t.band_plan_words(None, g))
        self.assertIn("No band conditions", self.t.band_plan_extra({"sun": {}}, g))

    def test_drawn_and_keys(self):
        text = self.tt.frame(self.app, 30, 100).text()
        self.assertIn("Band plan Auto", text)
        self.assertIn("a auto", text.split("\n")[-1])
        self.app.key(ord("a"))
        self.app.key(ord("m"))
        self.app.key(ord("s"))
        self.assertEqual(self.posts, [{"plan": "manual"}, {"mode": "js8"}, {"scan": True}])
        self.app.p.status["band_plan"] = None
        self.app.key(ord("a"))
        self.assertIn("no band plan", self.app.message)
        self.assertNotIn("a auto", self.tt.frame(self.app, 30, 100).text().split("\n")[-1])


    def test_poller_posts_bands(self):
        class Link:
            def __init__(self):
                self.posts = []

            def post(self, path, body, timeout=None):
                self.posts.append((path, body))
                if body.get("mode") == "ft8":
                    return 400, {"error": "mode must be wspr or js8"}
                return 200, {"status": {"plan": body.get("plan", "auto")}}
        link = Link()
        p = self.t.Poller(link)
        p.bands({"plan": "manual"})
        p._bands(p.bands_req)
        p._bands({"mode": "ft8"})
        self.assertEqual(link.posts[0], ("/api/v1/bands", {"plan": "manual"}))
        self.assertEqual(p.status["band_plan"], {"plan": "manual"})
        acks = p.take_bands_acks()
        self.assertEqual(acks[0], ({"plan": "manual"}, None))
        self.assertIn("mode must be wspr or js8", acks[1][1])



class Js8Decoding(unittest.TestCase):
    def test_rxd_decoder_counts_as_decoding(self):
        class Dec:
            def __init__(self, err): self.err = err
            def status(self): return {"engine": "rxd", "error": self.err}
        class Rx: pass
        rx = Rx()
        rx.js8dec = Dec(None)
        self.assertTrue(rxd.js8_decoding(rx))
        rx.js8dec = Dec("decoder process died")
        self.assertFalse(rxd.js8_decoding(rx))

if __name__ == "__main__":
    unittest.main()
