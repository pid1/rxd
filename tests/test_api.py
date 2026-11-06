"""Tests for rxd's API versioning (docs/API.md): every endpoint under /api/v1/,
the old unversioned /api/ paths as aliases, another version refused plainly,
and the version fields in the status.

    python3 -m unittest discover tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402  rxd/ and clients/tui/ on sys.path
import rxd  # noqa: E402
import test_rxd  # noqa: E402

GETS = ("status", "spectrum", "profiles", "js8?after=0", "wspr?after=0", "bands", "stations")


class Versions(unittest.TestCase):
    def test_one_version_everywhere(self):
        self.assertRegex(rxd.VERSION, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual(rxd.RXD_VERSION, "rxd-" + rxd.VERSION)
        self.assertEqual(rxd.Api.server_version, "rxd/" + rxd.VERSION)
        # the release script reads the same line
        out = subprocess.run(["sh", os.path.join(_paths.ROOT, "scripts", "version.sh")],
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), rxd.VERSION)

    def test_version_flag(self):
        out = subprocess.run([sys.executable, os.path.join(_paths.RXD, "rxd.py"), "--version"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "rxd " + rxd.VERSION, out.stderr)

    def test_route(self):
        self.assertEqual(rxd.route("/api/v1/status"), "/api/status")
        self.assertEqual(rxd.route("/api/status"), "/api/status")          # the old alias
        self.assertEqual(rxd.route("/api/v1/tiles/3/1/2.png"), "/tiles/3/1/2.png")
        self.assertEqual(rxd.route("/tiles/3/1/2.png"), "/tiles/3/1/2.png")
        self.assertIsNone(rxd.route("/api/v2/status"))
        self.assertIsNone(rxd.route("/api/v0/status"))
        self.assertEqual(rxd.route("/"), "/")

    def test_clients_speak_v1(self):
        with open(_paths.WEB, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn('const API = "/api/v1", API_VERSION = 1;', page)
        # no request goes to an unversioned path
        self.assertEqual(re.findall(r'(?:api|fetch)\("/(?:api|tiles)/', page), [])
        with open(os.path.join(_paths.TUI, "rxd_tui.py"), encoding="utf-8") as fh:
            tui = fh.read()
        code = [ln for ln in tui.splitlines() if not ln.lstrip().startswith("#")]
        self.assertEqual([ln for ln in code if re.search(r'link\.(get|post)\("/api/(?!v1/)', ln)], [])
        self.assertIn("API_VERSION = 1", tui)


class Http(unittest.TestCase):
    """A live rxd Api on a synthetic source, as test_rxd.Control starts it."""
    setUpClass = classmethod(test_rxd.Control.setUpClass.__func__)
    tearDownClass = classmethod(test_rxd.Control.tearDownClass.__func__)
    api = test_rxd.Control.api

    def raw(self, method, path, doc=None):
        req = urllib.request.Request(
            "http://%s:%d%s" % (*self.http.server_address, path), method=method,
            data=None if doc is None else json.dumps(doc).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_status_says_its_versions(self):
        for path in ("/api/v1/status", "/api/status"):
            code, st = self.api("GET", path)
            self.assertEqual(code, 200)
            self.assertEqual((st["api_version"], st["version"]), (1, rxd.VERSION))
            self.assertIn("upconverter", st["station"])

    def test_every_get_answers_the_same_on_both(self):
        # The first spectrum comes a block or two after start: until then both
        # paths say 503, but one could be asked just before it and one after.
        end = time.time() + 5
        while time.time() < end and self.raw("GET", "/api/v1/spectrum")[0] != 200:
            time.sleep(0.05)
        for name in GETS:
            v1 = self.raw("GET", "/api/v1/" + name)
            old = self.raw("GET", "/api/" + name)
            self.assertEqual(v1[0], old[0], name)
            self.assertNotEqual(v1[0], 404, name)
        self.assertEqual(self.raw("GET", "/api/v1/tiles/info.json")[0],
                         self.raw("GET", "/tiles/info.json")[0])

    def test_posts_on_both(self):
        for path in ("/api/v1/tune", "/api/tune"):
            code, st = self.api("POST", path, {"volume": 0.3})
            self.assertEqual((code, st["volume"]), (200, 0.3), path)
            self.assertEqual(self.api("POST", path, {"nonsense": 1})[0], 400, path)
        for path in ("/api/v1/profile", "/api/profile"):
            self.assertEqual(self.api("POST", path, {})[0], 400, path)

    def test_another_version_is_refused_plainly(self):
        for method in ("GET", "POST"):
            code, doc = self.api(method, "/api/v2/status", {} if method == "POST" else None)
            self.assertEqual(code, 404)
            self.assertEqual(doc["api_version"], 1)
            self.assertIn("API version 1", doc["error"])
        self.assertEqual(self.api("GET", "/api/v1/nope")[0], 404)
        self.assertEqual(self.api("POST", "/api/v1/nope", {})[0], 404)

    def test_tiles_under_v1_stay_inside_the_tree(self):
        with tempfile.TemporaryDirectory() as top:
            d = os.path.join(os.path.realpath(top), "tiles")
            os.makedirs(os.path.join(d, "3", "1"))
            with open(os.path.join(d, "3", "1", "2.png"), "wb") as fh:
                fh.write(b"\x89PNG")
            with open(os.path.join(os.path.dirname(d), "secret.png"), "wb") as fh:
                fh.write(b"no")
            self.http.tile_root = (d, [3])
            try:
                self.assertEqual(self.raw("GET", "/api/v1/tiles/3/1/2.png"), (200, b"\x89PNG"))
                for bad in ("/api/v1/tiles/../secret.png", "/api/v1/tiles/3/../../secret.png",
                            "/api/v1/tiles/%2e%2e/secret.png"):
                    self.assertEqual(self.raw("GET", bad)[0], 404, bad)
            finally:
                del self.http.tile_root


class Switch(unittest.TestCase):
    """The upconverter's switch: POST /api/v1/switch, the status's
    upconverter, and POST /api/v1/profile across the switch (docs/API.md).
    The live rxd is on JS8, 40 m: through the Ham It Up, on UPCONVERT."""
    setUpClass = classmethod(test_rxd.Control.setUpClass.__func__)
    tearDownClass = classmethod(test_rxd.Control.tearDownClass.__func__)
    api = test_rxd.Control.api
    fake_radio = test_rxd.Clients.fake_radio

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        for k, v in (("RXD_SWITCH", os.path.join(self.dir, "switch.json")),
                     ("RXD_BOOT_ID", os.path.join(self.dir, "boot_id"))):
            self.addCleanup(os.environ.__setitem__, k, os.environ[k])
            os.environ[k] = v
        self.boot("first-boot")
        old = rxd.RADIO, rxd.spawn_switch
        self.spawned = []
        rxd.RADIO = self.fake_radio(self.dir)
        rxd._profiles["doc"] = None
        rxd.spawn_switch = self.spawned.append

        def restore():
            rxd.RADIO, rxd.spawn_switch = old
            rxd._profiles["doc"] = None
        self.addCleanup(restore)

    def boot(self, bid):
        with open(os.environ["RXD_BOOT_ID"], "w") as fh:
            fh.write(bid + "\n")

    def upconverter(self):
        return self.api("GET", "/api/v1/status")[1]["upconverter"]

    def test_unknown_until_confirmed(self):
        up = self.upconverter()
        self.assertEqual((up["name"], up["switch"], up["needs"], up["position"], up["ok"]),
                         ("Ham It Up", True, "upconvert", "unknown", False))
        self.assertIsNone(up["confirmed_at"])
        self.assertEqual(up["why"], "not confirmed yet")

    def test_confirm(self):
        code, st = self.api("POST", "/api/v1/switch", {"position": "upconvert"})
        self.assertEqual(code, 200)
        up = st["upconverter"]
        self.assertEqual((up["position"], up["ok"], up["why"]), ("upconvert", True, None))
        self.assertRegex(up["confirmed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        code, st = self.api("POST", "/api/v1/switch", {"position": "bypass"})
        self.assertEqual((st["upconverter"]["position"], st["upconverter"]["ok"]), ("bypass", False))
        # kept on disk, so the next rxd (a restart, a profile switch) knows it
        with open(os.environ["RXD_SWITCH"]) as fh:
            self.assertEqual(json.load(fh)["position"], "bypass")
        self.assertEqual(self.upconverter()["position"], "bypass")
        # the machine starts up again: nobody knows any more
        self.boot("second-boot")
        up = self.upconverter()
        self.assertEqual((up["position"], up["confirmed_at"]), ("unknown", None))
        self.assertIn("started up since", up["why"])
        for bad in ({}, {"position": "up"}, {"position": "bypass", "x": 1}, [], {"position": None}):
            self.assertEqual(self.api("POST", "/api/v1/switch", bad)[0], 400, bad)
        self.assertEqual(self.upconverter()["position"], "unknown")

    def test_no_switch_nothing_to_confirm(self):
        st = os.path.join(self.dir, "station.json")
        with open(st, "w") as fh:
            json.dump({"upconverter": {"name": "Ham It Up", "switch": False}}, fh)
        self.addCleanup(os.environ.__setitem__, "RXD_STATION", os.environ["RXD_STATION"])
        os.environ["RXD_STATION"] = st
        up = self.upconverter()
        self.assertEqual((up["switch"], up["position"], up["ok"]), (False, None, True))
        code, doc = self.api("POST", "/api/v1/switch", {"position": "bypass"})
        self.assertEqual(code, 409)
        self.assertIn("no switch to confirm", doc["error"])
        # and a profile switch needs no confirmation
        self.assertEqual(self.api("POST", "/api/v1/profile", {"name": "fm-105.3"})[0], 202)
        self.assertEqual(self.spawned, ["fm-105.3"])
        with open(st, "w") as fh:
            json.dump({}, fh)
        self.assertIsNone(self.upconverter())

    def test_profile_across_the_switch(self):
        self.api("POST", "/api/v1/switch", {"position": "upconvert"})
        code, doc = self.api("POST", "/api/v1/profile", {"name": "fm-105.3"})
        self.assertEqual(code, 409)
        self.assertEqual(doc["error"], "fm-105.3 needs the Ham It Up on BYPASS, and it's on "
                                       "UPCONVERT. Flip the switch, then send \"switch\": "
                                       "\"bypass\" to confirm it.")
        self.assertEqual((doc["needs"], doc["position"]), ("bypass", "upconvert"))
        self.assertEqual(self.spawned, [])
        # the wrong side, or not a side, is refused and changes nothing
        for bad in ("upconvert", "up", True):
            self.assertEqual(self.api("POST", "/api/v1/profile",
                                      {"name": "fm-105.3", "switch": bad})[0], 400, bad)
        self.assertEqual(self.api("POST", "/api/v1/profile", {"name": "fm-105.3", "x": 1})[0], 400)
        self.assertEqual((self.spawned, self.upconverter()["position"]), ([], "upconvert"))
        # confirmed: it switches, and the switch's position is the new side
        code, doc = self.api("POST", "/api/v1/profile", {"name": "fm-105.3", "switch": "bypass"})
        self.assertEqual((code, doc, self.spawned), (202, {"switching": "fm-105.3"}, ["fm-105.3"]))
        self.assertEqual(self.upconverter()["position"], "bypass")

    def test_profile_while_unknown_needs_the_switch_too(self):
        code, doc = self.api("POST", "/api/v1/profile", {"name": "fm-105.3"})
        self.assertEqual((code, doc["position"]), (409, "unknown"))
        self.assertIn("and it's not confirmed", doc["error"])
        code, _ = self.api("POST", "/api/v1/profile", {"name": "fm-105.3", "switch": "bypass"})
        self.assertEqual((code, self.spawned), (202, ["fm-105.3"]))

    def test_same_side_and_same_profile_need_nothing(self):
        self.api("POST", "/api/v1/switch", {"position": "bypass"})
        self.assertEqual(self.api("POST", "/api/v1/profile", {"name": "fm-105.3"})[0], 202)
        # the profile already up (js8) asks nothing, whatever the switch
        self.addCleanup(setattr, self.rx.s, "name", self.rx.s.name)
        self.rx.s.name = "js8"
        self.assertEqual(self.api("POST", "/api/v1/profile", {"name": "js8"})[0], 202)
        self.assertEqual(self.spawned, ["fm-105.3", "js8"])


if __name__ == "__main__":
    unittest.main()
