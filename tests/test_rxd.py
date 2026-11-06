"""Tests for rxd: python3 -m unittest discover rxd  (numpy and scipy needed)."""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  rxd/ and clients/tui/ on sys.path
import rxd  # noqa: E402

JS8 = {"description": "JS8, 40 m", "frequency": 7078000.0, "vfo_offset": -168100.0,
       "offset": "Ham-It-Up", "demod": "USB",
       "demod_settings": {"bandwidth": 3000.0, "agcAttack": 47.9, "agcDecay": 4.9},
       "sink": "Built-in Audio", "volume": 0.2,
       "rtl": {"serial": "57818712", "gain": 4, "ppm": 0}, "decoder": "js8"}


def tone(rate, freq, n, amp=0.5):
    return (amp * np.exp(2j * np.pi * freq * np.arange(n) / rate)).astype(np.complex64)


def level_at(audio, freq, rate=rxd.AUDIO_RATE):
    """Magnitude (dB) of audio at freq, from a windowed FFT."""
    a = audio[len(audio) // 4:]  # past filter and AGC settling
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    f = np.fft.rfftfreq(len(a), 1 / rate)
    i = np.argmin(np.abs(f - freq))
    return 20 * np.log10(spec[max(i - 2, 0):i + 3].max() + 1e-12)


def run(chain, x, offset, block=23808):
    return np.concatenate([chain(x[i:i + block], offset) for i in range(0, len(x), block)])


class Dsp(unittest.TestCase):
    def test_decimator_is_block_invariant(self):
        rng = np.random.default_rng(1)
        x = (rng.standard_normal(10007) + 1j * rng.standard_normal(10007)).astype(np.complex64)
        taps = np.hanning(41)
        whole = rxd.FirDecimator(taps, 5)(x)
        d = rxd.FirDecimator(taps, 5)
        parts = np.concatenate([d(x[i:i + 997]) for i in range(0, len(x), 997)])
        np.testing.assert_allclose(parts, whole, rtol=1e-4, atol=1e-5)

    def test_cu8_lookup_matches_arithmetic(self):
        b = np.random.default_rng(3).integers(0, 256, 20000, dtype=np.uint8)
        f = (b.astype(np.float32) - 127.5) / 127.5
        np.testing.assert_allclose(rxd.cu8_to_complex(b), f[0::2] + 1j * f[1::2], atol=1e-6)
        self.assertEqual(len(rxd.cu8_to_complex(b[:-1])), 9999)  # odd: last byte dropped

    def test_shift_is_block_invariant(self):
        # The shift folded into stage1 must give the same audio however the
        # input is cut into blocks, or the phase bookkeeping is wrong.
        for mode, rate, off in (("USB", 240000, 50000), ("WFM", 1200000, 250000)):
            x = tone(rate, off + 1234, rate // 2) + tone(rate, off - 40000, rate // 2, 0.2)
            whole = rxd.Chain(mode, 2800 if mode == "USB" else 150000, 0, 0)
            parts = rxd.Chain(mode, 2800 if mode == "USB" else 150000, 0, 0)
            a = whole.shift_decimate(x, off)
            b = np.concatenate([parts.shift_decimate(x[i:i + 7777], off)
                                for i in range(0, len(x), 7777)])
            np.testing.assert_allclose(b, a, rtol=1e-3, atol=1e-4)

    def check_sideband(self, mode, sign):
        # One tone 1000 Hz into the wanted sideband and an equal one 1700 Hz
        # into the other, in the same run, so the AGC treats both alike.
        n = 240000 * 2
        x = tone(240000, 50000 + sign * 1000, n) + tone(240000, 50000 - sign * 1700, n)
        a = run(rxd.Chain(mode, 2800, 47.9, 4.9), x, 50000)
        self.assertEqual(len(a), n // 5)
        spec = np.abs(np.fft.rfft(a[len(a) // 2:]))
        peak = np.fft.rfftfreq(len(a) - len(a) // 2, 1 / 48000)[spec.argmax()]
        self.assertAlmostEqual(peak, 1000, delta=5)
        rej = level_at(a, 1000) - level_at(a, 1700)
        self.assertGreater(rej, 40, "opposite sideband only %.1f dB down" % rej)

    def test_usb(self):
        self.check_sideband("USB", +1)

    def test_lsb(self):
        self.check_sideband("LSB", -1)

    def test_usb_passband_edges(self):
        # 200-3000 Hz passes; 4 kHz is filtered away.
        chain = rxd.Chain("USB", 2800, 47.9, 4.9)
        x = tone(240000, 51500, 240000) + tone(240000, 54000, 240000)
        a = run(chain, x, 50000)
        self.assertGreater(level_at(a, 1500) - level_at(a, 4000), 40)

    def test_am(self):
        n = 240000 * 2
        t = np.arange(n) / 240000
        x = ((1 + 0.5 * np.cos(2 * np.pi * 1000 * t)) *
             np.exp(2j * np.pi * 50000 * t)).astype(np.complex64) * 0.3
        a = run(rxd.Chain("AM", 10000, 47.9, 4.9), x, 50000)
        spec = np.abs(np.fft.rfft(a[len(a) // 2:]))
        peak = np.fft.rfftfreq(len(a) - len(a) // 2, 1 / 48000)[spec.argmax()]
        self.assertAlmostEqual(peak, 1000, delta=5)

    def test_wfm(self):
        rate, n = 1200000, 1200000
        t = np.arange(n) / rate
        dev = 75000 * 0.5
        phase = 2 * np.pi * 250000 * t + dev / 1000 * np.sin(2 * np.pi * 1000 * t)
        x = (0.5 * np.exp(1j * phase)).astype(np.complex64)
        a = run(rxd.Chain("WFM", 150000, 0, 0), x, 250000, block=119808)
        self.assertEqual(len(a), n // 25)
        spec = np.abs(np.fft.rfft(a[len(a) // 2:]))
        peak = np.fft.rfftfreq(len(a) - len(a) // 2, 1 / 48000)[spec.argmax()]
        self.assertAlmostEqual(peak, 1000, delta=10)

    def test_agc_levels_quiet_and_loud(self):
        outs = []
        for amp in (0.003, 0.3):
            chain = rxd.Chain("USB", 2800, 47.9, 4.9)
            a = run(chain, tone(240000, 51000, 240000 * 3, amp), 50000)
            outs.append(np.abs(a[-24000:]).max())
        self.assertLess(abs(20 * np.log10(outs[0] / outs[1])), 3)
        self.assertLess(max(outs), 1.0)


class Capture:
    def __init__(self):
        self.chunks = []

    def write(self, pcm):
        self.chunks.append(pcm)

    def close(self):
        pass


class FastSynth(rxd.SynthSource):
    """SynthSource without the real-time pacing."""

    def read(self, n):
        self.t_next = None
        return super().read(n)


class Engine(unittest.TestCase):
    def setUp(self):
        self.settings = rxd.Settings(dict(JS8))
        self.src = FastSynth(7078000 + 125e6 + 1500)
        self.sink = Capture()
        self.rx = rxd.Receiver(self.settings, self.src, self.sink)

    def tearDown(self):
        self.rx.stop.set()

    def wait(self, cond, secs=5):
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_profile_frequency_is_the_dial(self):
        snap = self.settings.snapshot()
        self.assertEqual(snap["dial"], 7078000)
        self.assertNotIn("cal", snap)
        self.assertEqual(self.rx.rf(snap), 7078000 + 125e6)
        self.assertEqual(self.settings.bandwidth, 2800)

    def test_gain_index_maps_to_db(self):
        self.rx.start()
        # The synth has four gains, so the profile's index 4 clamps to the last.
        self.assertEqual(self.settings.gain_db, 37.2)
        self.assertEqual(self.rx.gain_db_for(1), 3.7)
        self.assertEqual(self.rx.gain_db_for(99), 37.2)

    def test_runs_and_retunes(self):
        self.rx.start()
        self.assertTrue(self.wait(lambda: len(self.sink.chunks) > 5))
        center = self.src.center
        self.assertEqual(center, 7078000 + 125e6 - 50000)
        # A small step moves only the software mixer.
        self.settings.update(freq=7080000)
        self.assertTrue(self.wait(lambda: self.rx.offset == 52000))
        self.assertEqual(self.src.center, center)
        # 7.107 is still within the mixer's 20-100 kHz window...
        self.settings.update(freq=7107000)
        self.assertTrue(self.wait(lambda: self.rx.offset == 79000))
        self.assertEqual(self.src.center, center)
        # ...a bigger step retunes the dongle and puts the signal 50 kHz up.
        self.settings.update(freq=7200000)
        self.assertTrue(self.wait(lambda: self.src.center != center))
        self.assertEqual(self.rx.offset, 50000)
        # Crossing 30 MHz drops the upconverter offset under "auto".
        self.settings.offset_mode = "auto"
        self.settings.update(freq=50.2e6)
        self.assertTrue(self.wait(lambda: self.src.center == 50.2e6 - 50000))
        # Changing mode rebuilds the chain at its own rate.
        self.settings.update(mode="WFM", freq=105.3e6)
        self.assertTrue(self.wait(lambda: self.rx.chain.mode == "WFM"))
        self.assertEqual(self.src.rate, 1200000)
        st = self.rx.status()
        self.assertEqual(st["mode"], "WFM")
        self.assertTrue(st["running"])

    def test_spectrum_shows_the_band_on_the_dial_scale(self):
        self.rx.start()
        self.assertTrue(self.wait(lambda: self.rx.spectrum is not None))
        sp = self.rx.spectrum
        self.assertEqual(len(sp["bins"]), 512)
        self.assertEqual(sp["stop_hz"] - sp["start_hz"], 240000)
        self.assertEqual(sp["dial_hz"], 7078000)
        self.assertEqual(sp["passband"], [7078200, 7081000])
        # The synth carrier is 1500 Hz over the dial.
        width = (sp["stop_hz"] - sp["start_hz"]) / 512
        peak = sp["start_hz"] + (int(np.argmax(sp["bins"])) + 0.5) * width
        self.assertAlmostEqual(peak, 7079500, delta=width)
        seq = sp["seq"]
        self.assertTrue(self.wait(lambda: self.rx.spectrum["seq"] > seq))

    def test_test_tone_is_heard(self):
        self.rx.start()
        self.assertTrue(self.wait(lambda: len(self.sink.chunks) > 40))
        audio = np.concatenate(self.sink.chunks).astype(np.float32)
        # The synth carrier sits 1500 Hz above the VFO (not the dial).
        spec = np.abs(np.fft.rfft(audio[len(audio) // 2:]))
        peak = np.fft.rfftfreq(len(audio) - len(audio) // 2, 1 / 48000)[spec.argmax()]
        self.assertAlmostEqual(peak, 1500, delta=10)


JS8_LINES = [
    "2026-11-06 03:25:59\t7.078000\t1500\t-16\tW0WQQ: 9A3QXZ HEARTBEAT SNR -04 \u2662 \n",
    "2026-11-06 03:26:14\t7.078000\t1180\t+03\tKQ4FQZ: @HB HEARTBEAT EM75 \u2662 \n",
    "2026-11-06 03:27:01\t7.078000\t900\t-09\tKD4EQ: KA1PID SNR -12 \u2662 \n",
]


TRACKING_GPS = """Reference ID    : 50505300 (PPS)
Stratum         : 1
System time     : 0.000000017 seconds slow of NTP time
Leap status     : Normal
"""


class Clock(unittest.TestCase):
    def test_gps_locked(self):
        c = rxd.parse_tracking(TRACKING_GPS)
        self.assertEqual((c["synced"], c["source"]), (True, "GPS"))
        self.assertAlmostEqual(c["offset_s"], -1.7e-8)

    def test_network_and_failures(self):
        net = TRACKING_GPS.replace("50505300 (PPS)", "C0A80001 (192.168.0.1)")
        self.assertEqual(rxd.parse_tracking(net)["source"], "network")
        off = TRACKING_GPS.replace("0.000000017 seconds slow", "2.5 seconds fast")
        self.assertEqual((rxd.parse_tracking(off)["synced"], rxd.parse_tracking(off)["offset_s"]),
                         (False, 2.5))
        lost = TRACKING_GPS.replace("Normal", "Not synchronised").replace("50505300 (PPS)", "00000000 ()")
        self.assertEqual(rxd.parse_tracking(lost), {"synced": False, "source": None, "offset_s": -1.7e-8})
        self.assertEqual(rxd.parse_tracking("")["synced"], False)


class Js8(unittest.TestCase):
    def test_parse(self):
        line = rxd.Js8Feed.parse(JS8_LINES[0])
        self.assertEqual(line, {"time": "2026-11-06 03:25:59", "freq": 7078000.0, "offset": 1500,
                                "snr": -16, "from": "W0WQQ", "to": "9A3QXZ",
                                "text": "W0WQQ: 9A3QXZ HEARTBEAT SNR -04"})
        self.assertIsNone(rxd.Js8Feed.parse(JS8_LINES[1])["to"])  # @HB is a group
        self.assertIsNone(rxd.Js8Feed.parse("garbage\n"))
        for word, call in (("HELLO", False), ("INFO", False), ("SNR", False), ("@HB", False),
                           ("KA1PID", True), ("9A3QXZ", True), ("VE6/LB9YH", True),
                           ("N5DQQ/P", True), ("W1AW", True), ("K1", False)):
            line = JS8_LINES[0].replace("9A3QXZ HEARTBEAT", word + " HEARTBEAT")
            self.assertEqual(rxd.Js8Feed.parse(line)["to"], word if call else None, word)

    def test_follows_appends_partial_lines_and_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "DIRECTED.TXT")
            with open(path, "w") as fh:
                fh.write(JS8_LINES[0])
            feed = rxd.Js8Feed(path)
            first = feed.since(0)
            self.assertEqual((first["seq"], [x["from"] for x in first["lines"]]), (1, ["W0WQQ"]))
            with open(path, "a") as fh:
                fh.write(JS8_LINES[1] + JS8_LINES[2][:20])  # the last one half-written
            got = feed.since(first["seq"])
            self.assertEqual([x["from"] for x in got["lines"]], ["KQ4FQZ"])
            with open(path, "a") as fh:
                fh.write(JS8_LINES[2][20:])
            self.assertEqual([x["from"] for x in feed.since(got["seq"])["lines"]], ["KD4EQ"])
            self.assertEqual(feed.since(feed.seq)["lines"], [])
            # logrotate renames it and JS8Call starts a new file.
            os.rename(path, path + "-20261106")
            with open(path, "w") as fh:
                fh.write(JS8_LINES[0])
            again = feed.since(feed.seq)
            self.assertEqual([x["from"] for x in again["lines"]], ["W0WQQ"])
            self.assertEqual(again["seq"], 4)
        self.assertEqual(rxd.Js8Feed("/nonexistent/DIRECTED.TXT").since(0), {"seq": 0, "lines": []})


class Control(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = rxd.Settings(dict(JS8))
        cls.rx = rxd.Receiver(cls.settings, FastSynth(7078000 + 125e6 + 1500), Capture())
        cls.rx.start()
        cls.http = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
        cls.http.rx = cls.rx
        cls.js8dir = tempfile.mkdtemp()
        cls.js8log = os.path.join(cls.js8dir, "DIRECTED.TXT")
        with open(cls.js8log, "w") as fh:
            fh.write(JS8_LINES[0] + JS8_LINES[1])
        cls.http.js8 = rxd.Js8Feed(cls.js8log)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.rx.stop.set()

    def api(self, method, path, doc=None):
        req = urllib.request.Request(
            "http://%s:%d%s" % (*self.http.server_address, path), method=method,
            data=None if doc is None else json.dumps(doc).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_http(self):
        code, st = self.api("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertIn("level_dbfs", st)
        code, st = self.api("POST", "/api/tune", {"freq": 7079000, "mode": "usb"})
        self.assertEqual((code, st["freq"], st["mode"]), (200, 7079000, "USB"))
        self.assertEqual(self.api("POST", "/api/tune", {"squelch": 1})[0], 400)
        self.assertEqual(self.api("POST", "/api/tune", {"nonsense": 1})[0], 400)
        code, st = self.api("POST", "/api/tune", {"squelch": -60})
        self.assertEqual((code, st["squelch"]), (200, -60.0))
        code, st = self.api("POST", "/api/tune", {"squelch": None})
        self.assertEqual((code, st["squelch"]), (200, None))
        self.assertEqual(self.api("POST", "/api/tune", {"mode": "CW"})[0], 400)
        self.assertEqual(self.api("POST", "/api/tune", {"mode": 5})[0], 400)
        self.assertEqual(self.api("POST", "/api/tune", {"freq": 1})[0], 400)
        self.assertEqual(self.api("GET", "/nope")[0], 404)
        code, st = self.api("POST", "/api/tune", {"volume": 0.4})
        self.assertEqual((code, st["volume"]), (200, 0.4))
        self.assertEqual(self.api("POST", "/api/tune", {"volume": 9})[0], 400)
        self.assertNotIn("vfo_hz", st)
        self.assertEqual(self.api("POST", "/api/tune", {"volume": 0})[1]["volume"], 0.0)
        self.api("POST", "/api/tune", {"freq": 7078000})

    def test_status_lists_gains(self):
        self.assertEqual(self.api("GET", "/api/status")[1]["gains"], [0.0, 3.7, 16.6, 37.2])
        self.assertEqual(self.api("GET", "/api/status")[1]["upconvert_below_hz"], 30e6)

    def test_spectrum_endpoint(self):
        end = time.time() + 5
        while time.time() < end:
            code, sp = self.api("GET", "/api/spectrum")
            if code == 200:
                break
            time.sleep(0.05)
        self.assertEqual((code, len(sp["bins"])), (200, 512))

    def page(self, path="/"):
        url = "http://%s:%d%s" % (*self.http.server_address, path)
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_serves_the_web_ui(self):
        old = rxd.WEB_PATHS
        try:
            with tempfile.TemporaryDirectory() as d:
                page = os.path.join(d, "index.html")
                with open(page, "w") as fh:
                    fh.write("<!doctype html><title>rxd</title>")
                rxd.WEB_PATHS = (page,)
                code, ctype, body = self.page("/")
                self.assertEqual((code, ctype.split(";")[0]), (200, "text/html"))
                self.assertIn(b"<title>rxd</title>", body)
                self.assertEqual(self.page("/index.html?x=1")[0], 200)
            rxd.WEB_PATHS = ("/nonexistent/index.html",)
            self.assertEqual(self.page("/")[0], 404)
        finally:
            rxd.WEB_PATHS = old


class Clients(Control):
    """The endpoints the browser UI added: profiles, switching, audio, JS8."""

    def test_js8_feed_over_http(self):
        code, doc = self.api("GET", "/api/js8?after=0")
        self.assertEqual((code, [x["from"] for x in doc["lines"]]), (200, ["W0WQQ", "KQ4FQZ"]))
        with open(self.js8log, "a") as fh:
            fh.write(JS8_LINES[2])
        code, doc2 = self.api("GET", "/api/js8?after=%d" % doc["seq"])
        self.assertEqual([x["to"] for x in doc2["lines"]], ["KA1PID"])
        self.assertEqual(self.api("GET", "/api/js8?after=x")[0], 400)
        st = self.api("GET", "/api/status")[1]
        self.assertEqual(st["decoder"], "js8")
        self.assertIn("station", st)
        self.assertEqual(set(st["clock"]), {"synced", "source", "offset_s"})

    def fake_radio(self, d):
        path = os.path.join(d, "radio")
        doc = {"active": "js8", "profiles": [
            {"name": "js8", "description": "JS8, 40 m", "freq": 7078000.0, "mode": "USB",
             "upconvert": True, "js8": True, "saved": True},
            {"name": "fm-105.3", "description": "105.3 FM", "freq": 105.3e6, "mode": "WFM",
             "upconvert": False, "js8": False, "saved": True}]}
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n[ \"$1\" = --json ] && echo '%s'\n" % json.dumps(doc))
        os.chmod(path, 0o755)
        return path

    def test_profiles_and_switch(self):
        old_radio, old_spawn = rxd.RADIO, rxd.spawn_switch
        spawned = []
        try:
            with tempfile.TemporaryDirectory() as d:
                rxd.RADIO = self.fake_radio(d)
                rxd._profiles["doc"] = None
                rxd.spawn_switch = spawned.append
                code, doc = self.api("GET", "/api/profiles")
                self.assertEqual((code, doc["active"], len(doc["profiles"])), (200, "js8", 2))
                code, doc = self.api("POST", "/api/profile", {"name": "fm-105.3"})
                self.assertEqual((code, doc, spawned), (202, {"switching": "fm-105.3"}, ["fm-105.3"]))
                self.assertEqual(self.api("POST", "/api/profile", {"name": "nope"})[0], 400)
                self.assertEqual(self.api("POST", "/api/profile", {"nom": "js8"})[0], 400)
                self.assertEqual(spawned, ["fm-105.3"])
            rxd.RADIO = "/nonexistent/radio"
            rxd._profiles["doc"] = None
            self.assertEqual(self.api("GET", "/api/profiles")[0], 503)
        finally:
            rxd.RADIO, rxd.spawn_switch = old_radio, old_spawn
            rxd._profiles["doc"] = None

    def test_audio_stream(self):
        s = socket.create_connection(self.http.server_address, timeout=5)
        s.sendall(b"GET /api/audio?rate=12000 HTTP/1.0\r\n\r\n")
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += s.recv(4096)
        head, data = raw.split(b"\r\n\r\n", 1)
        self.assertIn(b"X-Audio-Rate: 12000", head)
        while len(data) < 12000 * 2 * 2:  # two seconds
            data += s.recv(4096)
        self.assertEqual(self.rx.status()["listeners"], 1)
        s.close()  # really hang up
        a = np.frombuffer(data[: len(data) // 2 * 2], "<i2").astype(float)[6000:]
        spec = np.abs(np.fft.rfft(a))
        peak = np.fft.rfftfreq(len(a), 1 / 12000)[spec.argmax()]
        self.assertAlmostEqual(peak, 1500, delta=10)  # the synth's tone, at 12 kHz
        # The server only learns the socket closed when a later write fails,
        # which can take a few chunks once the kernel buffer has drained.
        deadline = time.time() + 10
        while self.rx.status()["listeners"] and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.rx.status()["listeners"], 0)
        for bad in ("rate=44100", "rate=x"):
            self.assertEqual(self.page("/api/audio?" + bad)[0], 400)


class Profiles(unittest.TestCase):
    def test_load_by_path_and_name(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "js8.json")
            with open(path, "w") as fh:
                json.dump(JS8, fh)
            p, got = rxd.load_profile(path)
            self.assertEqual((p["demod"], got), ("USB", path))
            old = rxd.STATE
            rxd.STATE = d
            try:
                self.assertEqual(rxd.load_profile("js8")[1], path)
                with self.assertRaises(SystemExit):
                    rxd.load_profile("missing")
            finally:
                rxd.STATE = old

    def test_saved_gain_db_wins_over_index(self):
        s = rxd.Settings(dict(JS8, rtl=dict(JS8["rtl"], gain_db=16.6)), "js8")
        self.assertEqual((s.gain_db, s.name), (16.6, "js8"))
        rx = rxd.Receiver(s, FastSynth(7077900 + 125e6), Capture())
        rx.start()
        try:
            self.assertEqual(s.gain_db, 16.6)
            self.assertEqual(rx.status()["profile"], "js8")
        finally:
            rx.stop.set()

    def test_offset_auto_follows_frequency(self):
        s = rxd.Settings(dict(JS8, offset="auto"), "js8")
        self.assertEqual(s.snapshot()["upconverter"], 125e6)   # 40 m: through it
        s.update(freq=146.52e6, mode="FM")
        self.assertEqual(s.snapshot()["upconverter"], 0.0)     # 2 m: bypass
        s.update(freq=820e3, mode="AM")
        self.assertEqual(s.snapshot()["upconverter"], 125e6)   # below the R820T
        # Pinned modes ignore the frequency; a missing offset means auto.
        self.assertEqual(rxd.Settings(dict(JS8, offset="None")).snapshot()["upconverter"], 0.0)
        p = dict(JS8, offset="Ham-It-Up", frequency=146.52e6)
        self.assertEqual(rxd.Settings(p).snapshot()["upconverter"], 125e6)
        p = {k: v for k, v in JS8.items() if k != "offset"}
        self.assertEqual(rxd.Settings(p).snapshot()["offset_mode"], "auto")
        p = dict(JS8, offset="auto", upconvert_below_hz=5e6)
        self.assertEqual(rxd.Settings(p).snapshot()["upconverter"], 0.0)
        with self.assertRaises(ValueError):
            rxd.Settings(dict(JS8, offset="Sideways"))

    def test_listen_address_from_the_environment(self):
        old = {k: os.environ.get(k) for k in ("RXD_HTTP",)}
        try:
            os.environ["RXD_HTTP"] = "0.0.0.0:8073"
            self.assertEqual(rxd.parse_args(["js8"]).http, "0.0.0.0:8073")
            self.assertEqual(rxd.parse_args(["js8", "--http", "127.0.0.1:9"]).http, "127.0.0.1:9")
            del os.environ["RXD_HTTP"]
            self.assertEqual(rxd.parse_args(["js8"]).http, "127.0.0.1:8073")
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                rxd.parse_args(["js8", "--rigctl", "127.0.0.1:4532"])  # gone
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_station_from_the_environment(self):
        old = {k: os.environ.get(k) for k in ("RXD_CALL", "RXD_GRID")}
        try:
            os.environ["RXD_CALL"], os.environ["RXD_GRID"] = "ka1pid", "en52LK"
            doc = rxd.station_status()
            self.assertEqual((doc["call"], doc["grid"]), ("KA1PID", "EN52lk"))
            self.assertNotIn("missing", doc)
            self.assertEqual(doc["upconverter"]["name"], "Ham It Up")
            del os.environ["RXD_GRID"]
            doc = rxd.station_status()
            self.assertEqual((doc["call"], doc["grid"]), ("KA1PID", None))
            self.assertIn("no grid", doc["missing"])
            self.assertIn("set call and grid in " + os.environ["RXD_STATION"], doc["missing"])
            del os.environ["RXD_CALL"]
            self.assertIn("no callsign or grid", rxd.station_status()["missing"])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_squelch_settings(self):
        s = rxd.Settings(dict(JS8, demod="FM", demod_settings={"squelchEnabled": True,
                                                                "squelchLevel": -60.0}))
        self.assertEqual(s.squelch, -60.0)
        self.assertIsNone(rxd.Settings(dict(JS8, demod="FM", demod_settings={
            "squelchEnabled": False, "squelchLevel": -60.0})).squelch)
        s.update(freq=146.52e6)            # leaving squelch out keeps it
        self.assertEqual(s.squelch, -60.0)
        s.update(squelch=None)             # null turns it off
        self.assertIsNone(s.squelch)
        with self.assertRaises(ValueError):
            s.update(squelch=20)

    def test_squelch_gate_hysteresis(self):
        rx = rxd.Receiver(rxd.Settings(dict(JS8)), FastSynth(0), Capture())
        rx.chain = rxd.Chain("FM", 12500, 0, 0)
        a = np.ones(10, np.float32)
        for level, want in ((-70, 0), (-50, 1), (-52, 1), (-54, 0), (-51, 0), (-49, 1)):
            rx.chain.level_db = level
            self.assertEqual(rx.gate(a, -50.0).max(), want, "at %d dBFS" % level)
        self.assertEqual(rx.gate(a, None).max(), 1)
        rx.chain.level_db = np.float64(-40.0)  # what the chain really produces
        rx.gate(a, -50.0)
        json.dumps(rx.status())  # status must stay plain JSON with squelch on

    def test_unsupported_mode(self):
        with self.assertRaises(ValueError):
            rxd.Settings(dict(JS8, demod="CW"))


if __name__ == "__main__":
    unittest.main()
