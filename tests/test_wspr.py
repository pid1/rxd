"""Tests for rxd's WSPR decoding and WSPRnet reporting.

python3 -m unittest discover rxd. Nothing here talks to WSPRnet: uploads go
to a fake. test_real_wsprd_decodes_a_slot runs only where wsprd is installed
(the uConsole), and decodes a synthesized transmission end to end.
"""
import json
import os
import shutil
import stat
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

WSPR = {"description": "WSPR, 40 m", "frequency": 7038600.0, "offset": "auto",
        "demod": "USB", "demod_settings": {"bandwidth": 2800.0},
        "sink": "Built-in Audio", "volume": 0.0,
        "rtl": {"serial": "57818712", "gain": 4, "ppm": 0},
        "decoder": "wspr", "wspr_report": True}

# wsprcode "KA1PID EM12 23": the 162 channel symbols (tones 0-3).
SYMBOLS = [int(c) for c in (
    "312200203002133200122121133220"
    "222230232122002010332011212221"
    "321200013030101012030010110221"
    "121010223202023223221310310211"
    "230203332200212322112220022310"
    "323322031002")]
SLOT = 1793967600.0  # 2026-11-06 12:20:00 UTC, an even minute

SPOTS = ("261106 1220   7 -10  0.1   7.040120  KA1PID EM12 23          0     1    0\n"
         "261106 1220   3 -24 -0.4   7.040188  PJ4/K1ABC 37            -1     1    0\n"
         "261106 1220   2 -27  1.2   7.040030  <PJ4/K1ABC> FK52UD 33    0     1    0\n")


def wspr_audio(symbols=SYMBOLS, base=1500.0, rate=rxd.AUDIO_RATE, start=1.0, length=116.0,
               snr_db=None, seed=0):
    """A WSPR transmission as receiver audio: 4-FSK at 12000/8192 baud and spacing."""
    sps_ = int(round(rate * 8192 / 12000))
    df = 12000 / 8192
    f = np.repeat(base + (np.array(symbols) - 1.5) * df, sps_)
    ph = 2 * np.pi * np.cumsum(f) / rate
    x = np.zeros(int(length * rate), np.float32)
    i0 = int(start * rate)
    x[i0:i0 + len(ph)] = 0.3 * np.sin(ph)
    if snr_db is not None:  # WSPR's SNR is in 2500 Hz
        noise_power = 0.3 ** 2 / 2 / 10 ** (snr_db / 10) * (rate / 2) / 2500
        x += np.random.default_rng(seed).standard_normal(len(x)).astype(np.float32) * \
            np.float32(np.sqrt(noise_power))
    return x


class FakeSettings:
    def __init__(self, report=True):
        self.report = report

    def snapshot(self):
        return {"wspr_report": self.report}


def feed(dec, audio, t_start, dial=7038600.0, block=4800):
    for i in range(0, len(audio), block):
        a = audio[i:i + block]
        dec.push(a, dial, now=t_start + (i + len(a)) / rxd.AUDIO_RATE)


class Parsing(unittest.TestCase):
    def test_spot_lines(self):
        rows = [rxd.SPOT_LINE.match(r) for r in SPOTS.splitlines()]
        spots = [rxd.spot_from(m[1], m[2], m[4], m[5], m[6], m[7], m[8], "EM13lc") for m in rows]
        a, b, c = spots
        self.assertEqual((a["call"], a["grid"], a["dbm"], a["snr"], a["freq"], a["time"]),
                         ("KA1PID", "EM12", 23, -10, 7040120, "2026-11-06T12:20:00Z"))
        self.assertEqual((b["call"], b["grid"], b["dbm"], b["drift"]), ("PJ4/K1ABC", None, 37, -1))
        self.assertEqual((c["call"], c["grid"], c["dbm"]), ("PJ4/K1ABC", "FK52ud", 33))
        self.assertIsNone(b["km"])
        self.assertTrue(0 < a["km"] < 200)  # EM12 is next door to EM13

    def test_all_wspr_line(self):
        row = "261106 1048 -22  0.02  10.1402200  KA1PID EM12 23          0  0.42  1  1    0  0   0     1   737"
        m = rxd.ALL_LINE.match(row)
        sp = rxd.spot_from(m[1], m[2], m[3], m[4], m[5], m[6], m[7])
        self.assertEqual((sp["call"], sp["snr"], sp["freq"]), ("KA1PID", -22, 10140220))

    def test_grid(self):
        lat, lon = rxd.grid_latlon("EM13lc")
        self.assertAlmostEqual(lat, 33.10, places=1)
        self.assertAlmostEqual(lon, -97.04, places=1)
        self.assertIsNone(rxd.grid_latlon("ZZ99"))
        # FN42 (Boston) to EM13 (Dallas): about 2500 km.
        self.assertTrue(2300 < rxd.grid_km("FN42", "EM13") < 2700)

    def test_bad_messages(self):
        for msg in ("", "KA1PID", "KA1PID EM12", "<...> EM12 23 extra"):
            self.assertIsNone(rxd.parse_wspr_message(msg), msg)


class Recording(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dec = rxd.WsprDecoder(FakeSettings(), datadir=self.dir, wsprd="/nonexistent",
                                   clock=lambda: SLOT)
        self.dec.worker = None
        self.jobs = []
        self.dec.jobs.put_nowait = self.jobs.append

    def test_one_slot_from_an_even_minute(self):
        feed(self.dec, np.zeros(int(130 * rxd.AUDIO_RATE), np.float32), SLOT - 5)
        self.assertEqual(len(self.jobs), 1)
        slot, dial, x = self.jobs[0]
        self.assertEqual((slot, dial, len(x)), (SLOT, 7038600.0, 114 * 12000))

    def test_joins_late_with_padding_but_not_too_late(self):
        feed(self.dec, wspr_audio(start=0.0, length=116), SLOT + 1.5)
        self.assertEqual(len(self.jobs), 1)
        x = self.jobs[0][2]
        # 1.5 s of silence first, then the signal straight away.
        self.assertLess(np.abs(x[:17000]).max(), 1e-3)
        self.assertGreater(np.abs(x[19000:20000]).max(), 0.1)
        self.jobs.clear()
        feed(self.dec, np.zeros(int(100 * rxd.AUDIO_RATE), np.float32), SLOT + 120 + 3)
        self.assertEqual(self.jobs, [])  # 3 s late: wait for the slot after

    def test_retune_drops_the_slot(self):
        n = int(60 * rxd.AUDIO_RATE)
        feed(self.dec, np.zeros(n, np.float32), SLOT)
        feed(self.dec, np.zeros(n, np.float32), SLOT + 60, dial=7040000.0)
        self.assertEqual(self.jobs, [])

    def test_status_states(self):
        st = self.dec.status()
        self.assertEqual((st["state"], st["slot"]), ("waiting", "2026-11-06T12:22:00Z"))
        self.assertFalse(st["report"]["enabled"] and st["report"].get("call") is None)
        feed(self.dec, np.zeros(48000, np.float32), SLOT)
        self.assertEqual(self.dec.status()["state"], "recording")
        json.dumps(self.dec.status())


FAKE_WSPRD = """#!/bin/sh
# argv: -a DIR -f MHZ WAV
echo "$@" > "$2/argv"
cp "$2/spots.in" "$2/wspr_spots.txt" 2>/dev/null
echo "<DecodeFinished>"
"""


class Decoding(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.wsprd = os.path.join(self.dir, "wsprd")
        with open(self.wsprd, "w") as fh:
            fh.write(FAKE_WSPRD)
        os.chmod(self.wsprd, stat.S_IRWXU)
        self.posts = []
        self.net = rxd.WsprNet("KA1PID", "EM13lc", post=self.fake_post)
        self.reply = (200, "1 spot(s) added")
        self.dec = rxd.WsprDecoder(FakeSettings(), datadir=self.dir, wsprd=self.wsprd,
                                   reporter=self.net, clock=lambda: SLOT)

    def fake_post(self, fields):
        if isinstance(self.reply, Exception):
            raise self.reply
        self.posts.append(fields)
        return self.reply

    def wait(self, seq):
        for _ in range(200):
            if self.dec.seq >= seq and self.dec.decoding is None and self.dec.jobs.empty():
                return
            time.sleep(0.05)
        self.fail("no decode")

    def test_decodes_and_reports_a_slot(self):
        with open(os.path.join(self.dir, "spots.in"), "w") as fh:
            fh.write(SPOTS)
        self.dec.jobs.put((SLOT, 7038600.0, np.zeros(114 * 12000, np.float32)))
        self.wait(3)
        time.sleep(0.8)  # the uploads are paced 0.2 s apart
        argv = open(os.path.join(self.dir, "argv")).read().split()
        self.assertEqual(argv[:3], ["-a", self.dir, "-f"])
        self.assertEqual(argv[3], "7.038600")
        self.assertTrue(argv[4].endswith("261106_1220.wav"))
        self.assertFalse(os.path.exists(argv[4]))  # the recording is removed
        feed_ = self.dec.since(0)
        self.assertEqual([x["call"] for x in feed_["lines"]], ["KA1PID", "PJ4/K1ABC", "PJ4/K1ABC"])
        self.assertEqual(self.dec.status()["last"], {"slot": "2026-11-06T12:20:00Z", "spots": 3})
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(self.posts[0], {
            "function": "wspr", "date": "261106", "time": "1220", "sig": "-10", "dt": "0.1",
            "drift": "0", "tqrg": "7.040120", "tcall": "KA1PID", "tgrid": "EM12", "dbm": "23",
            "version": rxd.RXD_VERSION, "rcall": "KA1PID", "rgrid": "EM13lc",
            "rqrg": "7.038600", "mode": "2"})
        self.assertEqual(self.posts[1]["tgrid"], "")
        self.assertEqual(self.net.status()["sent"], 3)

    def test_empty_slot_sends_a_heartbeat(self):
        self.net.flush(7038600.0, had_spots=False)
        self.assertEqual(self.posts[0]["function"], "wsprstat")
        self.assertEqual(self.posts[0]["rqrg"], "7.038600")

    def test_offline_spots_wait_for_the_next_slot(self):
        spot = rxd.spot_from("261106", "1220", "-10", "0.1", "7.040120", "KA1PID EM12 23", "0")
        self.net.queue_slot([spot], 7038600.0)
        self.reply = OSError("network is unreachable")
        self.net.flush(7038600.0, True)
        st = self.net.status()
        self.assertEqual((st["pending"], st["online"], st["sent"]), (1, False, 0))
        self.reply = (200, "1 spot(s) added")
        self.net.flush(7038600.0, True)
        self.assertEqual((self.net.status()["pending"], self.net.status()["sent"]), (0, 1))

    def test_refused_spot_is_dropped(self):
        spot = rxd.spot_from("261106", "1220", "-10", "0.1", "7.040120", "KA1PID EM12 23", "0")
        self.net.queue_slot([spot], 7038600.0)
        self.reply = (200, "Error: bad grid")
        self.net.flush(7038600.0, True)
        st = self.net.status()
        self.assertEqual((st["pending"], st["sent"]), (0, 0))
        self.assertIn("bad grid", st["error"])

    def test_off_band_spot_is_not_reported(self):
        spot = rxd.spot_from("261106", "1220", "-10", "0.1", "14.097100", "KA1PID EM12 23", "0")
        self.assertEqual(self.net.queue_slot([spot], 7038600.0), 0)

    def test_reporting_off_sends_nothing(self):
        self.dec.s = FakeSettings(report=False)
        with open(os.path.join(self.dir, "spots.in"), "w") as fh:
            fh.write(SPOTS)
        self.dec.jobs.put((SLOT, 7038600.0, np.zeros(114 * 12000, np.float32)))
        self.wait(3)
        time.sleep(0.3)
        self.assertEqual(self.posts, [])
        self.assertFalse(self.dec.status()["report"]["enabled"])

    def test_history_survives_a_restart(self):
        with open(os.path.join(self.dir, "ALL_WSPR.TXT"), "w") as fh:
            fh.write("261106 1048 -22  0.02   7.0401200  KA1PID EM12 23          0  0.42  1  1    0  0   0     1   737\n")
        dec = rxd.WsprDecoder(FakeSettings(), datadir=self.dir, wsprd=self.wsprd)
        self.assertEqual([x["call"] for x in dec.since(0)["lines"]], ["KA1PID"])

    @unittest.skipUnless(shutil.which("wsprd"), "wsprd not installed")
    def test_real_wsprd_decodes_a_slot(self):
        # Through the recorder (resampler, padding, WAV) and the real wsprd.
        dec = rxd.WsprDecoder(FakeSettings(report=False), datadir=self.dir,
                              wsprd=shutil.which("wsprd"), clock=lambda: SLOT)
        feed(dec, wspr_audio(snr_db=-15), SLOT)
        for _ in range(1200):
            if dec.last:
                break
            time.sleep(0.1)
        lines = dec.since(0)["lines"]
        self.assertEqual([(x["call"], x["grid"], x["dbm"]) for x in lines], [("KA1PID", "EM12", 23)])
        self.assertAlmostEqual(lines[0]["freq"], 7038600 + 1500, delta=3)
        self.assertLess(abs(lines[0]["snr"] + 15), 5)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.settings = rxd.Settings(dict(WSPR), "wspr")
        cls.rx = rxd.Receiver(cls.settings, rxd.SynthSource(7038600 + 125e6 + 1500), rxd.NullSink())
        cls.rx.wspr = rxd.WsprDecoder(cls.settings, datadir=cls.dir, wsprd="/nonexistent",
                                      reporter=rxd.WsprNet("KA1PID", "EM13lc", post=lambda f: (200, "")))
        cls.rx.start()
        cls.http = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
        cls.http.rx = cls.rx
        cls.http.js8 = rxd.Js8Feed("/nonexistent")
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.rx.stop.set()

    def call(self, path, body=None):
        url = "http://%s:%d%s" % (*self.http.server_address, path)
        req = urllib.request.Request(url, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def test_status_and_feed(self):
        st = self.call("/api/status")
        self.assertEqual(st["decoder"], "wspr")
        self.assertIn(st["wspr"]["state"], ("waiting", "recording"))
        self.assertEqual(st["wspr"]["report"]["call"], "KA1PID")
        self.assertEqual(self.call("/api/wspr?after=0"), {"seq": 0, "lines": []})

    def test_report_toggle(self):
        st = self.call("/api/tune", {"wspr_report": False})
        self.assertFalse(st["wspr"]["report"]["enabled"])
        st = self.call("/api/tune", {"wspr_report": True})
        self.assertTrue(st["wspr"]["report"]["enabled"])
        with self.assertRaises(urllib.error.HTTPError):
            self.call("/api/tune", {"wspr_report": "yes"})

    def test_bad_decoder(self):
        with self.assertRaises(ValueError):
            rxd.Settings(dict(WSPR, decoder="ft8"))


if __name__ == "__main__":
    unittest.main()
