"""Tests for rxd: python3 -m unittest discover rxd  (numpy and scipy needed)."""
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
import rxd  # noqa: E402

JS8 = {"description": "JS8, 40 m", "frequency": 7077900.0, "vfo_offset": -168100.0,
       "offset": "Ham-It-Up", "demod": "USB",
       "demod_settings": {"bandwidth": 3000.0, "agcAttack": 47.9, "agcDecay": 4.9},
       "sink": "SDR to JS8 (virtual cable)", "volume": 0.2,
       "rtl": {"serial": "57818712", "gain": 4, "ppm": 0}, "js8call_dial": 7078000}


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
        self.src = FastSynth(7077900 + 125e6 + 1500)
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

    def test_profile_dial_calibration(self):
        snap = self.settings.snapshot()
        self.assertEqual(snap["dial"], 7078000)
        self.assertEqual(snap["cal"], 100)
        self.assertEqual(self.rx.rf(snap), 7077900 + 125e6)
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
        self.assertEqual(center, 7077900 + 125e6 - 50000)
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
        self.assertTrue(self.wait(lambda: self.src.center == 50.2e6 - 100 - 50000))
        # Changing mode rebuilds the chain at its own rate.
        self.settings.update(mode="WFM", freq=105.3e6)
        self.assertTrue(self.wait(lambda: self.rx.chain.mode == "WFM"))
        self.assertEqual(self.src.rate, 1200000)
        st = self.rx.status()
        self.assertEqual(st["mode"], "WFM")
        self.assertTrue(st["running"])

    def test_test_tone_is_heard(self):
        self.rx.start()
        self.assertTrue(self.wait(lambda: len(self.sink.chunks) > 40))
        audio = np.concatenate(self.sink.chunks).astype(np.float32)
        # The synth carrier sits 1500 Hz above the VFO (not the dial).
        spec = np.abs(np.fft.rfft(audio[len(audio) // 2:]))
        peak = np.fft.rfftfreq(len(audio) - len(audio) // 2, 1 / 48000)[spec.argmax()]
        self.assertAlmostEqual(peak, 1500, delta=10)


class Control(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = rxd.Settings(dict(JS8))
        cls.rx = rxd.Receiver(cls.settings, FastSynth(7077900 + 125e6 + 1500), Capture())
        cls.rx.start()
        cls.rig = rxd.RigctlServer(("127.0.0.1", 0), rxd.RigctlHandler)
        cls.rig.rx = cls.rx
        cls.http = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
        cls.http.rx = cls.rx
        for s in (cls.rig, cls.http):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.rig.shutdown()
        cls.http.shutdown()
        cls.rx.stop.set()

    def rigctl(self, *cmds):
        with socket.create_connection(self.rig.server_address, timeout=2) as s:
            s.sendall(("\n".join(cmds) + "\nq\n").encode())
            out = b""
            while True:
                d = s.recv(65536)
                if not d:
                    return out.decode()
                out += d

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

    def test_rigctl_handshake(self):
        out = self.rigctl("\\chk_vfo", "\\dump_state")
        self.assertTrue(out.startswith("0\n1\n2\n0\n"))
        self.assertTrue(out.endswith("done\n"))

    def test_rigctl_freq_and_mode(self):
        self.assertEqual(self.rigctl("F 7078000", "f"), "RPRT 0\n7078000\n")
        self.assertEqual(self.rigctl("M PKTUSB 2400", "m"), "RPRT 0\nUSB\n2400\n")
        self.assertEqual(self.rigctl("M USB 2800"), "RPRT 0\n")
        self.assertEqual(self.rigctl("F nonsense"), "RPRT -1\n")
        self.assertEqual(self.rigctl("t", "T 1", "T 0"), "0\nRPRT -11\nRPRT 0\n")
        self.assertEqual(self.rigctl("\\get_powerstat", "v", "s"), "1\nVFOA\n0\nVFOA\n")
        self.assertRegex(self.rigctl("l STRENGTH"), r"^-?\d+\n$")
        self.assertEqual(self.rigctl("\\no_such_thing"), "RPRT -11\n")

    def test_http(self):
        code, st = self.api("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertIn("level_dbfs", st)
        code, st = self.api("POST", "/api/tune", {"freq": 7079000, "mode": "usb"})
        self.assertEqual((code, st["freq"], st["mode"]), (200, 7079000, "USB"))
        self.assertEqual(self.api("POST", "/api/tune", {"squelch": 1})[0], 400)
        self.assertEqual(self.api("POST", "/api/tune", {"mode": "CW"})[0], 400)
        self.assertEqual(self.api("POST", "/api/tune", {"mode": 5})[0], 400)
        self.assertEqual(self.api("POST", "/api/tune", {"freq": 1})[0], 400)
        self.assertEqual(self.api("GET", "/nope")[0], 404)
        code, st = self.api("POST", "/api/tune", {"volume": 0.4})
        self.assertEqual((code, st["volume"]), (200, 0.4))
        self.assertEqual(self.api("POST", "/api/tune", {"volume": 9})[0], 400)
        self.assertEqual((st["vfo_hz"], st["cal_hz"]), (7078900, 100))
        self.api("POST", "/api/tune", {"freq": 7078000})


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
        p = dict(JS8, offset="Ham-It-Up", frequency=146.52e6, js8call_dial=146.52e6)
        self.assertEqual(rxd.Settings(p).snapshot()["upconverter"], 125e6)
        p = {k: v for k, v in JS8.items() if k != "offset"}
        self.assertEqual(rxd.Settings(p).snapshot()["offset_mode"], "auto")
        p = dict(JS8, offset="auto", upconvert_below_hz=5e6)
        self.assertEqual(rxd.Settings(p).snapshot()["upconverter"], 0.0)
        with self.assertRaises(ValueError):
            rxd.Settings(dict(JS8, offset="Sideways"))

    def test_unsupported_mode(self):
        with self.assertRaises(ValueError):
            rxd.Settings(dict(JS8, demod="CW"))


if __name__ == "__main__":
    unittest.main()
