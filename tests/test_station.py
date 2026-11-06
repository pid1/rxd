"""Tests for the station file (rxd/station.py, docs/station.md) and how rxd
and radio use it: missing, partial and full files, bad values, and the
environment overriding the call and grid.

    python3 -m unittest discover tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402  rxd/ and clients/tui/ on sys.path
import station  # noqa: E402
import rxd  # noqa: E402
from test_radio import load_radio  # noqa: E402

FULL = {
    "call": "ka1pid", "grid": "em13LC",
    "sdr": {"serial": "57818712"},
    "upconverter": {"name": "Ham It Up", "hz": 125000000, "below_hz": 30000000, "switch": True},
    "sink": "Built-in Audio",
    "profiles": {"dcara": {"description": "DCARA", "frequency": 146920000, "demod": "FM"}},
}
JS8 = {"frequency": 7078000.0, "offset": "auto", "demod": "USB", "rtl": {"gain": 4}}


class StationEnv(unittest.TestCase):
    """Each test gets its own station file (or none) and no RXD_CALL/RXD_GRID."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rxd-station-")
        self.path = os.path.join(self.dir, "station.json")
        self.saved = {k: os.environ.get(k) for k in ("RXD_STATION", "RXD_CALL", "RXD_GRID")}
        os.environ["RXD_STATION"] = self.path
        os.environ.pop("RXD_CALL", None)
        os.environ.pop("RXD_GRID", None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rxd._station.update(key=None, st=None)

    def write(self, doc):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        # a rewrite within one mtime tick must still be read
        rxd._station.update(key=None, st=None)


class Loading(StationEnv):
    def test_missing_is_the_generic_station(self):
        st = station.load()
        self.assertEqual((st.call, st.grid, st.serial, st.upconverter, st.sink, st.profiles),
                         (None, None, "", None, None, {}))
        self.assertEqual((st.upconverter_hz, st.upconvert_below_hz, st.manual_switch),
                         (0.0, 0.0, False))
        self.assertIsNone(st.source)
        self.assertEqual(st.problems, [])

    def test_full(self):
        self.write(FULL)
        st = station.load()
        self.assertEqual((st.call, st.grid, st.serial, st.sink),
                         ("KA1PID", "EM13lc", "57818712", "Built-in Audio"))
        self.assertEqual(st.upconverter, {"name": "Ham It Up", "hz": 125e6,
                                          "below_hz": 30e6, "switch": True})
        self.assertTrue(st.manual_switch)
        self.assertEqual(st.profiles["dcara"]["frequency"], 146920000.0)
        self.assertEqual(st.problems, [])
        self.assertEqual(st.source, self.path)

    def test_partial(self):
        self.write({"call": "KA1PID", "upconverter": {}})
        st = station.load()
        self.assertEqual((st.call, st.grid, st.serial, st.sink), ("KA1PID", None, "", None))
        # an empty upconverter is the common 125 MHz kind, with a switch
        self.assertEqual(st.upconverter, {"name": "upconverter", "hz": 125e6,
                                          "below_hz": 30e6, "switch": True})
        self.write({"upconverter": {"hz": 120e6, "switch": False, "name": "SpyVerter"}})
        st = station.load()
        self.assertEqual((st.upconverter_hz, st.manual_switch, st.upconverter_name),
                         (120e6, False, "SpyVerter"))
        self.write({"upconverter": None, "sdr": {"serial": 12345}})
        st = station.load()
        self.assertEqual((st.upconverter, st.serial), (None, "12345"))

    def test_bad_values_fall_back_and_say_why(self):
        self.write({"call": 7, "grid": "nowhere", "sdr": {"serial": []},
                    "upconverter": {"hz": "lots", "switch": "yes"}, "sink": 3,
                    "profiles": {"bad name": {}, "nofreq": {"demod": "FM"},
                                 "odd": {"frequency": 1e6, "demod": "CW"}, "ok": {"frequency": 1e6}}})
        st = station.load()
        self.assertEqual((st.call, st.grid, st.serial, st.sink), (None, None, "", None))
        self.assertEqual(st.upconverter_hz, 125e6)   # the bad hz is dropped, not the converter
        self.assertTrue(st.manual_switch)
        self.assertEqual(list(st.profiles), ["ok"])
        self.assertEqual(st.profiles["ok"], {"frequency": 1e6, "description": "ok", "demod": "USB"})
        for word in ("call", "grid", "sdr.serial", "upconverter.hz", "upconverter.switch", "sink",
                     "'bad name'", "nofreq", "odd"):
            self.assertTrue(any(word in p for p in st.problems), (word, st.problems))

    def test_unreadable_is_generic_with_a_problem(self):
        self.write("{not json")
        st = station.load()
        self.assertIsNone(st.call)
        self.assertEqual(st.source, self.path)
        self.assertIn("can't read", st.problems[0])
        self.write("[1, 2]")
        self.assertIn("JSON object", station.load().problems[0])

    def test_environment_wins_for_call_and_grid(self):
        self.write(FULL)
        os.environ["RXD_CALL"], os.environ["RXD_GRID"] = "w1aw", "fn31pr"
        self.assertEqual(station.load().identity(), ("W1AW", "FN31pr"))
        os.environ["RXD_GRID"] = "bad"
        self.assertEqual(station.load().identity(), ("W1AW", None))


class InRxd(StationEnv):
    def test_no_station_never_upconverts(self):
        s = rxd.Settings(dict(JS8))
        self.assertEqual(s.snapshot()["upconverter"], 0.0)
        self.assertEqual(s.serial, "")
        self.assertIsNone(s.sink)
        doc = rxd.station_status()
        self.assertIsNone(doc["upconverter"])
        self.assertIn("no callsign or grid: set call and grid in " + self.path, doc["missing"])

    def test_station_supplies_the_hardware(self):
        self.write(FULL)
        s = rxd.Settings(dict(JS8))
        self.assertEqual(s.snapshot()["upconverter"], 125e6)
        self.assertEqual(s.serial, "57818712")
        self.assertIsNone(s.sink)  # "Built-in Audio" is the default sink
        self.assertEqual(rxd.station_id(), ("KA1PID", "EM13lc"))
        doc = rxd.station_status()
        self.assertNotIn("missing", doc)
        self.assertEqual(doc["upconverter"]["name"], "Ham It Up")
        # the profile's own values win over the station's
        p = dict(JS8, upconverter_hz=120e6, rtl={"serial": "1"}, sink="other")
        s = rxd.Settings(p)
        self.assertEqual((s.snapshot()["upconverter"], s.serial, s.sink), (120e6, "1", "other"))

    def test_station_is_read_again_when_it_changes(self):
        self.write({"call": "KA1PID", "grid": "EM13lc"})
        self.assertEqual(rxd.station_id(), ("KA1PID", "EM13lc"))
        self.write({"call": "W1AW", "grid": "FN31pr"})
        self.assertEqual(rxd.station_id(), ("W1AW", "FN31pr"))


class InRadio(StationEnv):
    def radio(self):
        home = tempfile.mkdtemp(dir=self.dir)
        return load_radio(home)

    def test_generic_radio(self):
        r = self.radio()
        p = r.PROFILES["js8"]
        self.assertNotIn("serial", p["rtl"])
        self.assertNotIn("sink", p)
        self.assertFalse(r.upconverting(p))
        self.assertIn("direct", r.summary("js8", p))
        self.assertNotIn("dcara", r.PROFILES)
        self.assertIn("vhf", r.PROFILES)

    def test_radio_reads_the_station_file(self):
        self.write(FULL)
        r = self.radio()
        p = r.PROFILES["js8"]
        self.assertEqual(p["rtl"]["serial"], "57818712")
        self.assertEqual(p["sink"], "Built-in Audio")
        self.assertTrue(r.upconverting(p))
        self.assertFalse(r.upconverting(r.PROFILES["vhf"]))
        self.assertIn("Ham It Up upconvert", r.summary("js8", p))
        d = r.PROFILES["dcara"]
        self.assertEqual((d["frequency"], d["demod"], d["offset"], d["sink"], d["rtl"]["serial"]),
                         (146920000.0, "FM", "auto", "Built-in Audio", "57818712"))

    def test_no_switch_no_flip_message(self):
        self.write(dict(FULL, upconverter={"name": "Auto-switching upconverter", "switch": False}))
        r = self.radio()
        self.assertFalse(r.STATION.manual_switch)
        self.assertIn("Auto-switching upconverter upconvert", r.summary("js8", r.PROFILES["js8"]))

    def test_extensions_on_path(self):
        self.write(FULL)
        r = self.radio()
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        for name, mode in (("radio-mesh", 0o755), ("radio-notexec", 0o644), ("radio-Bad", 0o755)):
            with open(os.path.join(bindir, name), "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(os.path.join(bindir, name), mode)
        old = os.environ["PATH"]
        os.environ["PATH"] = bindir  # only ours: the box has real radio-* commands
        try:
            self.assertEqual(list(r.extensions()), ["mesh"])
            ran = []
            r.os.execv = lambda path, argv: ran.append(argv)
            try:
                r.run_extension("mesh", ["dm", "W1AW", "hi"], focus=False)
                self.assertEqual(ran[-1][1:], ["--no-focus", "dm", "W1AW", "hi"])
                self.assertFalse(r.run_extension("js8", [], True))   # a profile wins
                self.assertFalse(r.run_extension("help", [], True))  # so does a builtin
                self.assertFalse(r.run_extension("nothing", [], True))
            finally:
                r.os.execv = os.execv
            out = io.StringIO()
            saved = sys.argv
            sys.argv = ["radio", "--names"]
            try:
                with contextlib.redirect_stdout(out):
                    r.main()
            finally:
                sys.argv = saved
            self.assertIn("mesh", out.getvalue().split())
        finally:
            os.environ["PATH"] = old


if __name__ == "__main__":
    unittest.main()
