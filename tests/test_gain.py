"""Tests for rxd's automatic RF gain and listening audio clean-up:
python3 -m unittest discover rxd  (numpy and scipy needed)."""
import json
import os
import queue
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rxd  # noqa: E402

# The uConsole dongle's R820T gain table, as rxd reports it.
R820T = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9,
         25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5,
         48.0, 49.6]

USB = {"description": "test", "frequency": 7038600.0, "offset": "auto", "demod": "USB",
       "demod_settings": {"bandwidth": 2800.0}, "volume": 0.5,
       "rtl": {"serial": "", "gain_db": 3.7}}
JS8 = dict(USB, frequency=7078000.0, js8call_dial=7078000)


class GainSynth:
    """A dongle with gain: band noise and an optional carrier, both scaled by
    the gain, through an 8-bit ADC that clips at full scale. The clock is the
    samples read, so a test runs faster than real time."""

    paced = False

    def __init__(self, noise_db, tone_db=None, tone_hz=1500.0):
        self.noise_db = noise_db   # band noise per spectrum bin at 0 dB gain
        self.tone_db = tone_db     # carrier power at 0 dB gain, dBFS
        self.tone_hz = tone_hz     # above the VFO
        self.gain = 0.0
        self.center = None
        self.rate = None
        self.t = 0.0
        self.phase = 0.0
        self.rng = np.random.default_rng(11)
        self.history = []          # (t, dB) for every set_gain

    def gains(self):
        return list(R820T)

    def configure(self, rate, ppm, bias_t):
        self.rate = rate

    def set_rate(self, rate):
        self.rate = rate

    def set_gain(self, db):
        self.gain = db
        self.history.append((self.t, db))

    def set_center(self, hz):
        self.center = float(hz)

    def now(self):
        return self.t

    def read(self, n):
        g = 10 ** (self.gain / 20)
        sigma = np.sqrt(10 ** (self.noise_db / 10) / 2) * g
        x = sigma * (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        if self.tone_db is not None:
            # the carrier sits tone_hz over the VFO, 50 kHz over the tuner
            w = 2 * np.pi * (50000.0 + self.tone_hz) / self.rate
            ph = self.phase + w * np.arange(n)
            self.phase = (self.phase + w * n) % (2 * np.pi)
            x = x + np.sqrt(10 ** (self.tone_db / 10)) * g * np.exp(1j * ph)
        b = np.empty(2 * n, np.uint8)
        b[0::2] = np.clip(np.round(127.5 + 127.5 * x.real), 0, 255)
        b[1::2] = np.clip(np.round(127.5 + 127.5 * x.imag), 0, 255)
        self.t += n / self.rate
        return rxd.cu8_to_complex(b)

    def close(self):
        pass


class Capture:
    def __init__(self):
        self.chunks = []

    def write(self, pcm):
        self.chunks.append(pcm)

    def close(self):
        pass


WALL0 = 1_800_000_000.0  # a multiple of 120: sim time 0 is a slot start


class Loop(unittest.TestCase):
    """The loop against a simulated dongle, in simulated time."""

    def make(self, src, profile=USB, gain=None):
        p = json.loads(json.dumps(profile))
        if gain is not None:
            p["rtl"]["gain_db"] = gain
        self.settings = rxd.Settings(p)
        self.src = src
        self.rx = rxd.Receiver(self.settings, src, Capture())
        self.rx.clock = src.now
        self.rx.wallclock = lambda: WALL0 + src.now()
        self.decisions = []   # (wall, dB) for each step AutoGain asked for
        self.rx.start()
        ag = self.rx.autogain
        observe = ag.observe

        def spy(x, gain, now, wall, floor=None):
            new = observe(x, gain, now, wall, floor)
            if new is not None:
                self.decisions.append((wall, new))
            return new
        ag.observe = spy
        return self.rx

    def tearDown(self):
        self.rx.stop.set()

    def run_to(self, t, secs=60):
        end = time.time() + secs
        while self.src.t < t and time.time() < end and not self.rx.stop.is_set():
            time.sleep(0.01)
        self.assertGreaterEqual(self.src.t, t, "the receiver stalled")

    def steps_after(self, t):
        return [d for d in self.decisions if d[0] - WALL0 > t]

    def settled(self):
        st = self.rx.autogain.status()
        return st, self.settings.snapshot()["gain_db"]

    def test_strong_signal_that_clips_steps_down_and_holds(self):
        # A carrier at +17 dBFS at the profile's 37.2 dB: hard clipping.
        self.make(GainSynth(noise_db=-80, tone_db=-20), gain=37.2)
        self.run_to(60)
        st, g = self.settled()
        self.assertLess(g, 37.2)
        self.assertEqual(st["clip"], 0.0)
        self.assertLessEqual(st["rms_dbfs"], rxd.AutoGain.RMS_HI)
        self.assertEqual(self.steps_after(30), [], "still stepping: %s" % self.decisions)
        self.assertIn(st["state"], ("holding",))

    def test_weak_signal_steps_up_until_the_antenna_sets_the_floor(self):
        self.make(GainSynth(noise_db=-85, tone_db=-70), gain=3.7)
        self.run_to(60)
        st, g = self.settled()
        self.assertGreater(g, 30)
        self.assertGreaterEqual(st["margin_db"], rxd.AutoGain.MARGIN_LO)
        self.assertLessEqual(st["margin_db"], rxd.AutoGain.MARGIN_HI)
        self.assertEqual(self.steps_after(30), [])
        # it settled fast: a few proportional jumps, not one step a second
        self.assertLessEqual(len(self.decisions), 6, self.decisions)

    def test_noise_only_converges_and_holds(self):
        self.make(GainSynth(noise_db=-70), gain=49.6)
        self.run_to(60)
        st, g = self.settled()
        self.assertGreaterEqual(st["margin_db"], rxd.AutoGain.MARGIN_LO)
        self.assertLessEqual(st["margin_db"], rxd.AutoGain.MARGIN_HI)
        self.assertEqual(self.steps_after(30), [])
        self.assertEqual(rxd.AutoGain.index(self.rx.autogain, g),
                         rxd.AutoGain.index(self.rx.autogain, self.src.gain))

    def test_digital_steps_only_between_slots(self):
        # JS8: 15 s slots, so a step may come only 13.3-14.8 s into one.
        self.make(GainSynth(noise_db=-70), profile=JS8, gain=29.7)
        self.run_to(40)
        self.assertFalse(self.rx.autogain.settling)
        n = len(self.decisions)
        self.src.noise_db += 14   # the band gets noisier: too much gain now
        self.run_to(130)
        later = self.decisions[n:]
        self.assertTrue(later, "never stepped down for the noisier band")
        for wall, _ in later:
            self.assertTrue(13.3 <= wall % 15 < 14.8, "stepped %.2f s into a slot" % (wall % 15))

    def test_overload_steps_at_once_even_mid_slot(self):
        self.make(GainSynth(noise_db=-70, tone_db=-90), profile=JS8, gain=29.7)
        self.run_to(40)
        n = len(self.decisions)
        t = self.src.t
        while (WALL0 + t) % 15 > 5:   # wait until early in a slot
            self.run_to(self.src.t + 0.5)
            t = self.src.t
        self.src.tone_db = -25        # a strong station comes up: clips
        self.run_to(t + 3)
        later = self.decisions[n:]
        self.assertTrue(later, "didn't step for the overload")
        wall, db = later[0]
        self.assertLess(wall - WALL0 - t, 2.5)
        self.assertLessEqual(db, 29.7 - rxd.AutoGain.URGENT_DROP)

    def test_a_step_that_flip_flops_is_held(self):
        # A dongle whose floor jumps 11 dB between 0 and 3.7 dB, as the R820T
        # did on WFM: neither step is in the band.
        class Jumpy(GainSynth):
            def read(self, n):
                real = self.gain
                self.gain = 0.0 if real < 1 else real + 12.3
                try:
                    return super().read(n)
                finally:
                    self.gain = real
        self.make(Jumpy(noise_db=-48), gain=0.0)
        self.run_to(90)
        self.assertLessEqual(len(self.decisions), 2, self.decisions)
        self.assertEqual(self.settings.snapshot()["gain_db"], 3.7)  # holds the higher one
        self.assertIn("flip-flopped", self.rx.autogain.status()["reason"])

    def test_manual_gain_stops_the_loop_and_gain_auto_restarts_it(self):
        rx = self.make(GainSynth(noise_db=-70), gain=49.6)
        self.run_to(5)
        self.settings.update(gain_db=48.0)
        st = rx.status()
        self.assertFalse(st["gain_auto"])
        self.assertEqual(st["gain_db"], 48.0)
        t = self.src.t
        self.run_to(t + 20)
        self.assertEqual(self.steps_after(t + 1), [])
        self.assertEqual(self.src.gain, 48.0)
        self.assertEqual(rx.status()["gain_loop"]["state"], "off")
        self.settings.update(gain_auto=True)
        self.run_to(self.src.t + 15)
        self.assertTrue(rx.status()["gain_auto"])
        self.assertLess(self.src.gain, 48.0)


class Decide(unittest.TestCase):
    def setUp(self):
        self.ag = rxd.AutoGain(R820T)

    def test_hysteresis_band(self):
        i = R820T.index(20.7)
        self.assertEqual(self.ag.decide(0, -30, 9.0, i)[0], 0)
        self.assertEqual(self.ag.decide(0, -30, 5.0, i)[0], 1)
        self.assertEqual(self.ag.decide(0, -30, 14.0, i)[0], -1)
        # the band is wider than the biggest step, so one step can't flip it
        widest = max(b - a for a, b in zip(R820T, R820T[1:]))
        self.assertGreater(rxd.AutoGain.MARGIN_HI - rxd.AutoGain.MARGIN_LO, widest)
        self.assertGreater(rxd.AutoGain.RMS_HI - rxd.AutoGain.RMS_UP, widest)

    def test_overload_wins_and_headroom_blocks_a_step_up(self):
        i = R820T.index(20.7)
        self.assertEqual(self.ag.decide(1e-3, -30, 2.0, i), (-1, "clipping"))
        self.assertEqual(self.ag.decide(0, -8, 2.0, i), (-1, "near full scale"))
        d, why = self.ag.decide(0, -14, 2.0, i)
        self.assertEqual(d, 0)
        self.assertIn("headroom", why)
        self.assertEqual(self.ag.decide(0, -30, 2.0, len(R820T) - 1)[0], 0)
        self.assertEqual(self.ag.decide(1e-2, -3, 30, 0)[0], 0)

    def test_measure_counts_clipped_bytes(self):
        b = np.full(2000, 128, np.uint8)
        b[:10] = 255
        b[10:15] = 0
        b[15:30] = 254   # near, not at, full scale
        c, n, pw = rxd.AutoGain.measure(rxd.cu8_to_complex(b))
        self.assertEqual((c, n), (15, 1000))

    def test_floor_ignores_signals_and_the_dc_spike(self):
        bins = np.full(512, -40.0)
        bins[100:130] = 0       # a few strong stations
        bins[250:262] = 10      # DC
        bins[:40] = -90         # the filter's roll-off
        self.assertAlmostEqual(rxd.AutoGain.floor_of(bins), -40.0)


class Settings(unittest.TestCase):
    def test_profile_and_tune_fields(self):
        s = rxd.Settings(dict(USB))
        self.assertTrue(s.gain_auto)
        self.assertEqual(s.audio, {"auto": True, "nr": None, "filter": True, "norm": True,
                                   "limit": True, "squelch": True})
        p = json.loads(json.dumps(USB))
        p["rtl"]["gain_auto"] = False
        p["audio"] = {"nr": False, "limit": False}
        s = rxd.Settings(p)
        self.assertFalse(s.gain_auto)
        self.assertEqual((s.audio["nr"], s.audio["limit"], s.audio["norm"]), (False, False, True))
        s.update(gain_auto=True, audio_nr=None, audio_auto=False)
        self.assertTrue(s.gain_auto)
        self.assertEqual((s.audio["nr"], s.audio["auto"]), (None, False))
        with self.assertRaises(ValueError):
            s.update(gain_auto="yes")
        with self.assertRaises(ValueError):
            s.update(audio_norm=None)
        with self.assertRaises(ValueError):
            s.update(audio_bass=True)
        # a gain set by hand turns the loop off, unless gain_auto comes with it
        s.update(gain_db=20.7)
        self.assertFalse(s.gain_auto)
        s.update(gain_db=3.7, gain_auto=True)
        self.assertTrue(s.gain_auto)
        self.assertTrue(s.set_auto_gain(8.7))
        self.assertEqual(s.gain_db, 8.7)
        s.update(gain_db=1.4)
        self.assertFalse(s.set_auto_gain(8.7))   # too late: set by hand
        self.assertEqual(s.gain_db, 1.4)


class DecoderVolume(unittest.TestCase):
    """A JS8 profile's audio is JS8Call's input: it is never silenced."""

    def test_refuses_silence_on_js8(self):
        s = rxd.Settings(json.loads(json.dumps(JS8)))
        for v in (0.0, 0.01):
            with self.assertRaises(ValueError):
                s.update(volume=v)
        self.assertEqual(s.volume, 0.5)
        s.update(volume=rxd.DECODER_MIN_VOLUME)
        self.assertEqual(s.volume, rxd.DECODER_MIN_VOLUME)
        # a listening profile and WSPR (decoded before the volume) may go to 0
        for p in (USB, dict(USB, decoder="wspr")):
            s = rxd.Settings(json.loads(json.dumps(p)))
            s.update(volume=0.0)
            self.assertEqual(s.volume, 0.0)

    def test_a_profile_saved_silent_loads_audible(self):
        s = rxd.Settings(dict(json.loads(json.dumps(JS8)), volume=0.0))
        self.assertEqual(s.volume, rxd.JS8_VOLUME)
        s = rxd.Settings(dict(json.loads(json.dumps(USB)), volume=0.0))
        self.assertEqual(s.volume, 0.0)

    def test_status_and_http(self):
        rx = rxd.Receiver(rxd.Settings(json.loads(json.dumps(JS8))), GainSynth(-70), Capture())
        self.assertEqual(rx.status()["volume_min"], rxd.DECODER_MIN_VOLUME)
        rx2 = rxd.Receiver(rxd.Settings(json.loads(json.dumps(USB))), GainSynth(-70), Capture())
        self.assertEqual(rx2.status()["volume_min"], 0.0)
        http = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
        http.rx = rx
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.shutdown)
        req = urllib.request.Request("http://%s:%d/api/tune" % http.server_address, method="POST",
                                     data=b'{"volume": 0}', headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(e.exception.code, 400)
        self.assertIn("JS8Call", json.load(e.exception)["error"])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = GainSynth(noise_db=-70)
        cls.settings = rxd.Settings(json.loads(json.dumps(USB)))
        cls.rx = rxd.Receiver(cls.settings, cls.src, Capture())
        cls.rx.clock = cls.src.now
        cls.rx.start()
        cls.http = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
        cls.http.rx = cls.rx
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

    def test_gain_override_and_back(self):
        code, st = self.api("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(st["gain_auto"])
        self.assertIn("state", st["gain_loop"])
        code, st = self.api("POST", "/api/tune", {"gain_db": 12.5})
        self.assertEqual((code, st["gain_auto"], st["gain_db"]), (200, False, 12.5))
        code, st = self.api("POST", "/api/tune", {"gain_auto": True})
        self.assertEqual((code, st["gain_auto"]), (200, True))
        code, st = self.api("POST", "/api/tune", {"gain_auto": 1})
        self.assertEqual(code, 400)

    def test_audio_switches(self):
        code, st = self.api("POST", "/api/tune", {"audio_nr": False, "audio_limit": False})
        self.assertEqual(code, 200)
        self.assertEqual((st["audio"]["nr"], st["audio"]["limit"]), (False, False))
        end = time.time() + 3
        while time.time() < end and "limit" in self.rx.status()["audio"]["active"]:
            time.sleep(0.02)
        st = self.rx.status()
        self.assertNotIn("limit", st["audio"]["active"])
        self.assertNotIn("nr", st["audio"]["active"])
        code, st = self.api("POST", "/api/tune", {"audio_auto": False})
        end = time.time() + 3
        while time.time() < end and self.rx.status()["audio"]["active"]:
            time.sleep(0.02)
        self.assertEqual(self.rx.status()["audio"]["active"], [])
        code, st = self.api("POST", "/api/tune", {"audio_auto": True, "audio_nr": None,
                                                  "audio_limit": True})
        self.assertEqual((code, st["audio"]["nr"], st["audio"]["nr_setting"]), (200, True, None))
        self.assertEqual(self.api("POST", "/api/tune", {"audio_bass": True})[0], 400)


# ------------------------------------------------------------- audio clean-up

FS = rxd.AUDIO_RATE


def blocks(fx, a, opt, n=4800):
    return np.concatenate([fx(a[i:i + n], opt) for i in range(0, len(a), n)])


def rms_db(a):
    return 10 * np.log10(np.mean(np.asarray(a, np.float64) ** 2) + 1e-20)


def band_db(a, f0, f1):
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    f = np.fft.rfftfreq(len(a), 1 / FS)
    return 10 * np.log10(spec[(f >= f0) & (f < f1)].sum() + 1e-20)


def only(**on):
    opt = {k: False for k in rxd.AUDIO_KEYS}
    opt.update(auto=True, **on)
    return opt


def fm_iq(dev_hz, n, snr_db=None, tone=1000.0, rate=240000, seed=3):
    t = np.arange(n) / rate
    ph = 2 * np.pi * 50000 * t + dev_hz / tone * np.sin(2 * np.pi * tone * t)
    x = 0.3 * np.exp(1j * ph)
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        s = 0.3 * 10 ** (-snr_db / 20) / np.sqrt(2) * np.sqrt(rate / 12500)
        x = x + s * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return x.astype(np.complex64)


def chain_out(mode, x, bw=12500.0, block=24064):
    ch = rxd.Chain(mode, bw, 50.0, 5.0)
    return np.concatenate([ch(x[i:i + block], 50000.0) for i in range(0, len(x), block)])


class Audio(unittest.TestCase):
    def test_normaliser_evens_quiet_and_loud_stations(self):
        # NFM has no AGC: 1 kHz against 4 kHz deviation is 12 dB apart raw.
        outs, raws = [], []
        for dev in (1000.0, 4000.0):
            a = chain_out("FM", fm_iq(dev, 240000 * 8))
            raws.append(rms_db(a[-48000:]))
            fx = rxd.AudioFx("FM", 12500.0)
            outs.append(rms_db(blocks(fx, a, only(norm=True))[-48000:]))
        self.assertGreater(abs(raws[1] - raws[0]), 10)
        self.assertLess(abs(outs[1] - outs[0]), 1.5, outs)
        self.assertAlmostEqual(outs[0], rxd.AudioFx.TARGET_DB, delta=1.5)

    def test_normaliser_limits_its_gain_and_ignores_silence(self):
        fx = rxd.AudioFx("AM", 10000.0)
        out = blocks(fx, np.zeros(48000, np.float32), only(norm=True))
        self.assertEqual(float(np.abs(out).max()), 0.0)
        self.assertIsNone(fx.level)
        quiet = (3e-3 * np.sin(2 * np.pi * 440 * np.arange(48000 * 6) / FS)).astype(np.float32)
        out = blocks(fx, quiet, only(norm=True))
        self.assertAlmostEqual(20 * np.log10(fx.gain), rxd.AudioFx.GAIN_MAX_DB, delta=0.1)
        self.assertLess(rms_db(out[-4800:]), rxd.AudioFx.TARGET_DB - 5)

    def test_limiter(self):
        a = np.linspace(-3, 3, 10001).astype(np.float32)
        y = rxd.soft_limit(a)
        self.assertLessEqual(float(np.abs(y).max()), 0.97)
        small = np.abs(a) <= 0.6
        np.testing.assert_array_equal(y[small], a[small])
        self.assertTrue(np.all(np.diff(y) >= 0))    # still monotonic: no fold-over

    def test_filter_takes_out_hum_and_ctcss(self):
        t = np.arange(FS * 2) / FS
        voice = 0.2 * np.sin(2 * np.pi * 1000 * t)
        for mode, low, cut in (("AM", 60.0, 15), ("FM", 100.0, 40), ("FM", 0.0, 40)):
            a = (voice + (0.2 * np.sin(2 * np.pi * low * t) if low else 0.2)).astype(np.float32)
            out = blocks(rxd.AudioFx(mode, 10000.0), a, only(filter=True))[FS:]
            ref = a[FS:]
            lo_in, lo_out = (band_db(ref, low - 5, low + 5), band_db(out, low - 5, low + 5)) \
                if low else (rms_db(ref - voice[FS:]), rms_db(np.full(10, out.mean())))
            self.assertGreater(lo_in - lo_out, cut, (mode, low))
            self.assertAlmostEqual(band_db(out, 990, 1010), band_db(ref, 990, 1010), delta=1.0)

    def test_nfm_filter_cuts_the_hiss_above_the_voice(self):
        a = chain_out("FM", fm_iq(3000.0, 240000 * 2, snr_db=12))
        out = blocks(rxd.AudioFx("FM", 12500.0), a, only(filter=True))[24000:]
        a = a[24000:]
        self.assertGreater(band_db(a, 4000, 20000) - band_db(out, 4000, 20000), 15)
        self.assertAlmostEqual(band_db(out, 950, 1050), band_db(a, 950, 1050), delta=1.0)

    def test_noise_reducer(self):
        # 0.5 s bursts of a 1 kHz tone in steady white noise, like speech.
        rng = np.random.default_rng(2)
        n = FS * 8
        t = np.arange(n) / FS
        tone = 0.3 * np.sin(2 * np.pi * 1000 * t) * ((t % 1.0) < 0.5)
        noise = 0.03 * rng.standard_normal(n)
        a = (tone + noise).astype(np.float32)
        fx = rxd.AudioFx("AM", 10000.0)
        out = blocks(fx, a, only(nr=True))
        d = rxd.NoiseReducer.H * 2  # its delay
        out = out[d:]
        gaps = [(k + 0.6, k + 0.95) for k in range(4, 7)]
        bursts = [(k + 0.1, k + 0.45) for k in range(4, 7)]
        cut = lambda x, spans: np.concatenate([x[int(a * FS):int(b * FS)] for a, b in spans])
        reduced = rms_db(cut(a, gaps)) - rms_db(cut(out, gaps))
        self.assertGreater(reduced, 10, "noise only %.1f dB down" % reduced)
        kept = band_db(cut(out, bursts), 980, 1020) - band_db(cut(a, bursts), 980, 1020)
        self.assertGreater(kept, -1.5, "tone %.1f dB down" % kept)
        # off for WFM unless asked for, since it would eat into music
        self.assertFalse(rxd.AudioFx("WFM", 150000.0).nr_on(only(nr=None)))
        self.assertTrue(rxd.AudioFx("WFM", 150000.0).nr_on(only(nr=True)))
        self.assertTrue(fx.nr_on(only(nr=None)))

    def test_noise_reducer_rebuilds_the_signal_exactly(self):
        class Bypass(rxd.NoiseReducer):
            OVER = 0.0   # every bin kept whole: only the framing is left
        a = np.random.default_rng(7).standard_normal(FS * 2).astype(np.float32)
        nr = Bypass()
        out = np.concatenate([nr(a[i:i + 4813]) for i in range(0, len(a), 4813)])
        self.assertEqual(len(out), len(a))
        d = rxd.NoiseReducer.H * 2
        np.testing.assert_allclose(out[d + 512:], a[512:len(a) - d], atol=1e-4)

    def test_noise_reducer_keeps_a_steady_tone(self):
        # a tone there from the start isn't taken for noise; a held note
        # 40 dB over the noise is untouched for 10 s
        rng = np.random.default_rng(8)
        t = np.arange(FS * 10) / FS
        a = (0.3 * np.sin(2 * np.pi * 700 * t) + 3e-3 * rng.standard_normal(len(t))).astype(np.float32)
        out = rxd.AudioFx("AM", 10000.0)
        out = blocks(out, a, only(nr=True))
        for s in (0.5, 5, 9):
            seg = slice(int(s * FS), int((s + 0.5) * FS))
            self.assertAlmostEqual(rms_db(out[seg]), rms_db(a[seg]), delta=0.5)

    def test_nfm_auto_squelch(self):
        fx = rxd.AudioFx("FM", 12500.0)
        opt = only(squelch=True)
        noise = chain_out("FM", (0.01 * (np.random.default_rng(4).standard_normal(240000 * 2) + 1j *
                                         np.random.default_rng(5).standard_normal(240000 * 2))
                                 ).astype(np.complex64))
        out = blocks(fx, noise, opt)
        self.assertFalse(fx.sq_open)
        self.assertEqual(float(np.abs(out[-4800:]).max()), 0.0)
        for snr in (25, 10):   # strong, and weak but readable
            fx = rxd.AudioFx("FM", 12500.0)
            sig = chain_out("FM", fm_iq(3000.0, 240000 * 2, snr_db=snr))
            out = blocks(fx, sig, opt)
            self.assertTrue(fx.sq_open, "closed at %d dB SNR (%.1f dB quiet)" % (snr, fx.sq_quiet))
            self.assertGreater(np.abs(out[-4800:]).max(), 0.1)
        # and a manual squelch takes over from it
        fx = rxd.AudioFx("FM", 12500.0)
        blocks(fx, noise, opt)
        out = fx(noise[:4800], opt, manual_squelch=True)
        self.assertTrue(fx.sq_open)
        self.assertNotIn("squelch", fx.active)

    def test_off_means_untouched(self):
        a = np.random.default_rng(6).standard_normal(4800).astype(np.float32)
        fx = rxd.AudioFx("FM", 12500.0)
        opt = {k: True for k in rxd.AUDIO_KEYS}
        opt["auto"] = False
        self.assertIs(fx(a, opt), a)
        self.assertEqual(fx.active, [])


class Routing(unittest.TestCase):
    """The clean-up reaches the speaker and the phone, never a decoder."""

    def run_rx(self, profile):
        settings = rxd.Settings(json.loads(json.dumps(profile)))
        src = GainSynth(noise_db=-80, tone_db=-40)
        sink = Capture()
        rx = rxd.Receiver(settings, src, sink)
        rx.clock = src.now
        q = rx.listen(48000)
        rx.start()
        got = []
        end = time.time() + 30
        while src.t < 12 and time.time() < end:
            try:
                got.append(q.get(timeout=1))
            except queue.Empty:
                pass
        rx.stop.set()
        stream = np.frombuffer(b"".join(got), "<i2").astype(np.float32) / 32767 / 0.8
        speaker = np.concatenate(sink.chunks).astype(np.float32) / 32767 / settings.volume
        return rx, stream, speaker

    def test_listening_profile_gets_the_clean_up_on_both(self):
        rx, stream, speaker = self.run_rx(USB)
        self.assertIn("norm", rx.status()["audio"]["active"])
        # the USB tone is AGC'd to 0.5 peak (-9 dBFS RMS); normalised to -20
        for a in (stream, speaker):
            self.assertAlmostEqual(rms_db(a[-3 * FS:]), rxd.AudioFx.TARGET_DB, delta=2.0)

    def test_decoder_audio_stays_clean(self):
        rx, stream, speaker = self.run_rx(JS8)
        # JS8Call's cable gets the AGC'd audio as before; the phone gets the clean-up
        self.assertAlmostEqual(rms_db(speaker[-3 * FS:]), -9.0, delta=1.5)
        self.assertAlmostEqual(rms_db(stream[-3 * FS:]), rxd.AudioFx.TARGET_DB, delta=2.0)


# ------------------------------------------------------------------ rxd-tui

AUDIO_ON = {"auto": True, "nr": True, "nr_setting": None, "filter": True, "norm": True,
            "limit": True, "squelch": True, "active": ["filter", "nr", "norm", "limit"]}


class Tui(unittest.TestCase):
    def app(self, profile, **st):
        import test_rxd_tui as tt
        app = tt.app_on(profile)
        app.p.status.update({"gain_auto": True, "gain_db": 7.7, "audio": dict(AUDIO_ON)}, **st)
        tt.frame(app)
        self.tt = tt
        return app

    def test_words(self):
        import rxd_tui as t
        st = {"gain_auto": True, "gain_db": 7.7, "mode": "AM", "audio": dict(AUDIO_ON)}
        self.assertEqual(t.gain_words(st), "Auto 7.7 dB")
        self.assertEqual(t.gain_words(dict(st, gain_auto=False)), "7.7 dB")
        self.assertEqual(t.audio_words(st, short=True), "Auto: NR, hum, loudness, limiter")
        self.assertIn("squelch", t.audio_words(dict(st, mode="FM"), short=True))
        self.assertEqual(t.audio_words(dict(st, audio_auto=False)), "Off, untouched")  # sent, unconfirmed
        self.assertEqual(t.audio_words({"mode": "AM"}), "---")   # an rxd without it

    def test_toggles(self):
        import rxd_tui as t
        st = {"mode": "AM", "audio": dict(AUDIO_ON)}
        self.assertEqual(t.audio_toggle(st, "A")[0], {"audio_auto": False})
        self.assertEqual(t.audio_toggle(st, "c")[0], {"audio_limit": False})
        self.assertEqual(t.audio_toggle(st, "n")[0], {"audio_nr": False})
        # turning NR back on where the mode does anyway returns it to "by mode"
        off = dict(st, audio=dict(AUDIO_ON, nr=False, nr_setting=False))
        self.assertEqual(t.audio_toggle(off, "n")[0], {"audio_nr": None})
        wfm = {"mode": "WFM", "audio": dict(AUDIO_ON, nr=False)}
        self.assertEqual(t.audio_toggle(wfm, "n")[0], {"audio_nr": True})
        self.assertIsNone(t.audio_toggle(st, "S")[0])             # NFM only
        self.assertIsNone(t.audio_toggle(dict(st, audio=dict(AUDIO_ON, auto=False)), "n")[0])
        fm = {"mode": "FM", "squelch": None, "audio": dict(AUDIO_ON)}
        self.assertEqual(t.squelch_cycle(fm, -60)[0], {"squelch": -60})
        self.assertEqual(t.squelch_cycle(dict(fm, squelch=-60.0), -60)[0],
                         {"squelch": None, "audio_squelch": False})
        self.assertEqual(t.squelch_cycle(dict(fm, audio=dict(AUDIO_ON, squelch=False)), -60)[0],
                         {"audio_squelch": True, "audio_auto": True})

    def test_listen_keys_and_screen(self):
        app = self.app("noaa", squelch=None)
        text = self.tt.frame(app).text()
        self.assertIn("Gain Auto 7.7 dB", text)
        self.assertIn("Squelch Auto", text)
        self.assertIn("Audio Auto: noise reduction", text)
        app.key(ord("a"))
        self.assertEqual(app.p.sent[-1], {"gain_db": 7.7, "gain_auto": False})
        self.assertIn("Gain by hand", app.message)
        app.key(ord("s"))
        self.assertEqual(app.p.sent[-1], {"squelch": app.sq_level})  # auto -> a level
        app.key(ord("A"))
        self.assertEqual(app.p.sent[-1], {"audio_auto": False})
        help_text = "\n".join(app.help_lines())
        self.assertIn("auto gain on / off", help_text)

    def test_manual_keys(self):
        app = self.app("40m")
        self.assertIn("Gain Auto 7.7 dB", self.tt.frame(app, 24, 120).text())
        app.key(ord("S"))
        self.assertIn("NFM only", app.message)
        for k in "nhecA":
            app.key(ord(k))
        self.assertEqual(app.p.sent, [{"audio_nr": False}, {"audio_filter": False},
                                      {"audio_norm": False}, {"audio_limit": False},
                                      {"audio_auto": False}])
        app.p.status["gain_auto"] = False
        app.overrides.clear()
        self.tt.frame(app)
        app.key(ord("a"))
        self.assertEqual(app.p.sent[-1], {"gain_auto": True})

    def test_no_speaker_volume_or_mute_on_js8(self):
        import rxd_tui as t
        app = self.app("ghostnet")   # JS8: its audio is JS8Call's input
        app.view = "manual"
        for k in "M-+":
            app.key(ord(k))
            self.assertIn("feeds JS8Call", app.message)
        self.assertEqual(app.p.sent, [])
        keys = [k for k, _ in t.help_items(app.g, "manual", "USB", decoder="js8")]
        self.assertNotIn("- +", keys)
        # a mute made on a station isn't carried over into JS8
        st = dict(app.p.status)
        app.switching = {"name": "ghostnet", "desc": "GhostNet", "prev_up": True, "muted": 0.3}
        app.finish_switch(st)
        self.assertEqual(app.p.sent, [])
        self.assertIsNone(app.mute_prev)


if __name__ == "__main__":
    unittest.main()
