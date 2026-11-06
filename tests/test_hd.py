"""Tests for HD Radio (rxd/hd.py and its wiring into rxd): the resampler, the
HD-capable WFM chain, sideband detection, nrsc5's log, the blend's state
machine, alignment, and the whole path against a fake nrsc5
(tests/fixtures/fake_nrsc5.py). No network and no real nrsc5.

    python3 -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import numpy as np
from scipy import signal as sps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402
import hd  # noqa: E402
import radio  # noqa: E402
import rxd  # noqa: E402
sys.path.insert(0, os.path.join(_paths.TESTS, "fixtures"))
import fake_nrsc5  # noqa: E402

FAKE = [sys.executable, _paths.FAKE_NRSC5]
R = hd.HD_RATE
BLOCK = int(R // 10) // 256 * 256
WFM = {"description": "test FM", "frequency": 99900000, "demod": "WFM",
       "demod_settings": {"bandwidth": 150000}, "rtl": {"gain": 10}}


# ------------------------------------------------------------------ helpers

class FmStation:
    """IQ of an FM station at 1488375 S/s centred on it, carrying
    fake_nrsc5.program_audio, with HD-like noise sidebands if hd_db is set."""

    def __init__(self, hd_db=-20.0, snr_db=40.0, seed=3):
        self.n = 0
        self.phase = 0.0
        self.hd_db = hd_db
        self.noise = 10 ** (-snr_db / 20)
        self.rng = np.random.default_rng(seed)
        self.tau = 75e-6

    def block(self, n=BLOCK):
        t = (self.n + np.arange(n)) / R
        m = fake_nrsc5.program_audio(t).astype(np.float64)
        m1 = fake_nrsc5.program_audio(t - 1.0 / R).astype(np.float64)
        pre = m + self.tau * R * (m - m1)          # pre-emphasis, undone by the chain
        dphi = 2 * np.pi * 75000.0 * 0.5 * pre / R   # half deviation: no clipping
        ph = self.phase + np.cumsum(dphi)
        self.phase = float(ph[-1] % (2 * np.pi))
        x = 0.5 * np.exp(1j * ph)
        if self.hd_db is not None:
            w = self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)
            f = np.fft.fftfreq(n, 1.0 / R)
            mask = (np.abs(f) > 129e3) & (np.abs(f) < 199e3)
            band = np.fft.ifft(np.fft.fft(w) * mask)
            band *= 0.5 * 10 ** (self.hd_db / 20) / (np.std(band) + 1e-12)
            x = x + band
        x = x + self.noise * 0.5 * (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        self.n += n
        return x.astype(np.complex64)


def spectrum_of(x, dial=0.0, bins=512):
    """rxd's spectrum shape for a block, as make_spectrum builds it."""
    n = 2 * bins
    segs = min(len(x) // n, 4)
    w = np.hanning(n)
    p = (np.abs(np.fft.fft(x[: segs * n].reshape(segs, n) * w, axis=1)) ** 2).mean(axis=0)
    p = np.fft.fftshift(p).reshape(bins, -1).mean(axis=1)
    db = 10 * np.log10(p + 1e-12)
    return {"bins": list(db), "start_hz": dial - R / 2, "stop_hz": dial + R / 2, "dial_hz": dial}


class Log:
    """A stand-in Nrsc5Log for the blend: just the fields it reads."""

    def __init__(self, synced=True, ber=1e-5, mer=(12.0, 12.0), syncs=1):
        self.synced, self.ber, self.mer, self.syncs = synced, ber, mer, syncs


def fake_env(**cfg):
    return dict(os.environ, FAKE_NRSC5=json.dumps(cfg))


class FakeEnv:
    """FAKE_NRSC5 set for the duration of a with block."""

    def __init__(self, **cfg):
        self.cfg = cfg

    def __enter__(self):
        self.old = os.environ.get("FAKE_NRSC5")
        os.environ["FAKE_NRSC5"] = json.dumps(self.cfg)
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("FAKE_NRSC5", None)
        else:
            os.environ["FAKE_NRSC5"] = self.old


def run_hd(h, station, seconds, chain=None, on_block=None):
    """Drive an Hd with station's IQ for seconds of simulated time; the output audio.

    Each block waits for the fake nrsc5 to have answered the IQ fed so far,
    so the run is as if paced in real time.
    """
    chain = chain or rxd.Chain("WFM", 150000.0, 0, 0, hd=True)
    out = []
    blocks = int(seconds * 10)
    for b in range(blocks):
        x = station.block()
        raw = rxd.complex_to_cu8(x)
        now = (b + 1) * BLOCK / R
        if b % 2 == 0:
            h.spectrum(spectrum_of(x))
        a = chain(x, 0.0)
        out.append(h.process(a, raw, now))
        p = h.proc
        if p is not None:
            # until its output stops growing: it has answered this block
            deadline = time.monotonic() + 2.0
            last, still = -1, 0
            while p.alive() and time.monotonic() < deadline and still < 2:
                end = p.audio.end
                still = still + 1 if end == last else 0
                last = end
                time.sleep(0.003)
            deadline = time.monotonic() + 3.0
            while h.checking and time.monotonic() < deadline:
                time.sleep(0.002)
        if on_block:
            on_block(b, now)
    return np.concatenate(out)


# ------------------------------------------------------------------ resampling

class Resampler(unittest.TestCase):
    def test_rate_and_accuracy(self):
        # 1 kHz through 44.1k -> 48k: right count, right frequency, clean
        rs = hd.PolyResampler(160, 147, 20000.0, 44100, 32)
        t = np.arange(44100 * 2) / 44100
        y = rs(np.sin(2 * np.pi * 1000 * t))
        self.assertAlmostEqual(len(y), 96000, delta=2)
        ref = np.sin(2 * np.pi * 1000 * (np.arange(len(y)) / 48000.0) + 0)
        # past the filter's start-up, the error is tiny once its delay is taken out
        d = (32 // 2) / 44100.0
        ref = np.sin(2 * np.pi * 1000 * (np.arange(len(y)) / 48000.0 - d + 0.5 / (44100 * 160)))
        err = y[2000:-10] - ref[2000:-10]
        lag = np.argmax(np.correlate(y[2000:4000], ref[2000:4000], "full")) - 1999
        self.assertLessEqual(abs(lag), 1)
        sp = np.abs(np.fft.rfft(y[4800:4800 + 48000] * np.hanning(48000)))
        peak = np.argmax(sp)
        self.assertEqual(peak, 1000)                    # 1 Hz bins
        spur = np.max(np.delete(sp, range(peak - 30, peak + 31)))
        self.assertLess(20 * np.log10(spur / sp[peak]), -60)
        self.assertLess(np.std(err[np.abs(err) < 1]), 0.5)  # sane, not a wild signal

    def test_block_invariant(self):
        rng = np.random.default_rng(5)
        x = rng.standard_normal(30000).astype(np.float32)
        for up, down, fs in ((160, 147, 44100), (640, 1323, 99225)):
            whole = hd.PolyResampler(up, down, 15000.0, fs)(x)
            rs = hd.PolyResampler(up, down, 15000.0, fs)
            parts, i = [], 0
            for n in (1, 7, 1000, 3, 4801, 12345, 17, 30000):
                parts.append(rs(x[i:i + n]))
                i += n
                if i >= len(x):
                    break
            np.testing.assert_allclose(np.concatenate(parts), whole, atol=1e-5)

    def test_hd_chain_exactly_48k(self):
        c = rxd.Chain("WFM", 150000.0, 0, 0, hd=True)
        self.assertEqual(c.rate, 1488375)
        self.assertEqual(c.offset, 0.0)
        n = 0
        st = FmStation(hd_db=None)
        for _ in range(30):              # 3 s
            n += len(c(st.block(), 0.0))
        self.assertAlmostEqual(n / (30 * BLOCK / R), 48000.0, delta=20)

    def test_hd_chain_demodulates(self):
        # The program's tones come out: correlate with the audio the IQ carries.
        c = rxd.Chain("WFM", 150000.0, 0, 0, hd=True)
        st = FmStation(hd_db=-20)
        a = np.concatenate([c(st.block(), 0.0) for _ in range(20)])
        ref = fake_nrsc5.program_audio(np.arange(len(a)) / 48000.0)
        bp = sps.firwin(255, [200, 3500], fs=48000, pass_zero=False)
        aa, rr = sps.lfilter(bp, 1, a)[4800:], sps.lfilter(bp, 1, ref)[4800:]
        c0 = np.correlate(aa[:48000 + 2000], rr[:48000], "valid")   # the audio trails the IQ
        lag = int(np.argmax(c0))
        r = c0[lag] / np.sqrt(np.sum(aa[lag:lag + 48000] ** 2) * np.sum(rr[:48000] ** 2))
        self.assertGreater(r, 0.9)

    def test_hd_chain_block_invariant(self):
        st = FmStation(hd_db=-20)
        x = np.concatenate([st.block() for _ in range(6)])
        whole = rxd.Chain("WFM", 150000.0, 0, 0, hd=True)(x, 0.0)
        c = rxd.Chain("WFM", 150000.0, 0, 0, hd=True)
        parts, i = [], 0
        for n in (BLOCK, 1000, BLOCK - 1000, 333, 2 * BLOCK, len(x)):
            parts.append(c(x[i:i + n], 0.0))
            i += n
        np.testing.assert_allclose(np.concatenate(parts), whole, atol=2e-4)

    def test_cu8_round_trip(self):
        x = (np.random.default_rng(1).standard_normal(1000) * 0.3).astype(np.complex64)
        y = rxd.cu8_to_complex(rxd.complex_to_cu8(x))
        self.assertLess(np.max(np.abs(np.clip(x.real, -1, 1) - y.real)), 1 / 127)


# ------------------------------------------------------------------ detection

class Sidebands(unittest.TestCase):
    def test_hd_station(self):
        st = FmStation(hd_db=-18)
        s = hd.sidebands(spectrum_of(st.block()))
        self.assertTrue(s["present"], s)
        self.assertGreater(s["lower_db"], 10)

    def test_analog_only(self):
        st = FmStation(hd_db=None)
        s = hd.sidebands(spectrum_of(st.block()))
        self.assertFalse(s["present"], s)

    def test_one_side_is_a_neighbour(self):
        # A station 200 kHz up lights the upper sideband's bins only.
        st = FmStation(hd_db=None)
        x = st.block()
        t = np.arange(len(x)) / R
        x = x + 0.3 * np.exp(1j * (2 * np.pi * 180e3 * t + 40 * np.sin(2 * np.pi * 900 * t)))
        s = hd.sidebands(spectrum_of(x))
        self.assertFalse(s["present"], s)

    def test_off_centre_dial(self):
        # Sidebands are found around the dial, wherever it sits in the band.
        st = FmStation(hd_db=-18)
        sp = spectrum_of(st.block(), dial=99.9e6)
        self.assertTrue(hd.sidebands(sp)["present"])
        sp["dial_hz"] += 300e3
        self.assertFalse(hd.sidebands(sp)["present"])


# ------------------------------------------------------------------ nrsc5's log

class Nrsc5Log(unittest.TestCase):
    LINES = """13:04:07 Synchronized
13:04:07 Frequency offset: 99 Hz
13:04:07 Primary service mode: 1
13:04:07 Station name: TEST
13:04:07 Audio service 0: public, type: None, codec: 0, blend: 2, gain: 0 dB, delay: 96, latency: 8
13:04:07 Title: A Song
13:04:07 Artist: The Band
13:04:07 Audio service 1: public, type: Jazz, codec: 0, blend: 0, gain: 0 dB, delay: 0, latency: 8
13:04:07 Slogan: The test station
13:04:08 Audio bit rate: 63.7 kbps
13:04:09 MER: 13.4 dB (lower), 12.3 dB (upper)
13:04:09 BER: 0.000060, avg: 0.022001, min: 0.000060, max: 0.197004
13:04:09 Audio packet CRC mismatches: 3
"""

    def test_parse(self):
        lg = hd.Nrsc5Log()
        for ln in self.LINES.splitlines():
            lg.parse(ln, 5.0)
        self.assertTrue(lg.synced)
        self.assertEqual(lg.syncs, 1)
        self.assertEqual(lg.station, "TEST")
        self.assertEqual(lg.slogan, "The test station")
        self.assertEqual((lg.title, lg.artist), ("A Song", "The Band"))
        self.assertEqual(lg.mer, (13.4, 12.3))
        self.assertAlmostEqual(lg.ber, 6e-5)
        self.assertEqual(lg.crc_errors, 3)
        self.assertEqual(lg.bitrate, 63.7)
        self.assertEqual(lg.program_list(), [
            {"program": 0, "name": "HD1", "type": None},
            {"program": 1, "name": "HD2", "type": "Jazz"}])
        lg.parse("13:04:10 Lost synchronization", 6.0)
        self.assertFalse(lg.synced)
        self.assertIsNone(lg.mer)
        lg.parse("13:04:12 Synchronized", 8.0)
        self.assertEqual(lg.syncs, 2)

    def test_junk(self):
        lg = hd.Nrsc5Log()
        for ln in ("", "Found Rafael Micro R820T tuner", "13:00:00 MER: x", "BER:", "\xff\xfe"):
            lg.parse(ln, 0)
        self.assertFalse(lg.synced)
        self.assertIsNone(lg.mer)


# ------------------------------------------------------------------ the blend

class BlendMachine(unittest.TestCase):
    def run_script(self, b, steps, dt=0.1):
        """steps: [(seconds, kwargs for tick)]; the states seen, in order, deduplicated.
        Time carries on from the last call on the same Blend."""
        t, seen = getattr(b, "_test_t", 0.0), []
        for secs, kw in steps:
            for _ in range(int(round(secs / dt))):
                t += dt
                st = b.tick(t, **kw)
                if not seen or seen[-1] != st:
                    seen.append(st)
        b._test_t = t
        return seen, t

    def test_acquire_needs_hold_and_alignment(self):
        b = hd.Blend()
        good = dict(running=True, log=Log(), aligned=True, detected=True)
        # Clean but not lined up yet: the hold runs, but it waits for alignment.
        seen, _ = self.run_script(b, [(0.5, dict(running=False, detected=True)),
                                      (6.0, dict(good, aligned=False))])
        self.assertEqual(seen, ["no signal", "acquiring"])
        self.assertIn("lining HD up", b.reason)
        seen, _ = self.run_script(b, [(0.1, good)])
        self.assertEqual(b.state, "digital")
        # Lined up at once: still the whole hold of clean signal first.
        b = hd.Blend()
        seen, _ = self.run_script(b, [(3.85, good)])
        self.assertEqual(seen, ["acquiring"])
        self.assertIn("checking it stays clean", b.reason)
        seen, _ = self.run_script(b, [(0.3, good)])
        self.assertEqual(b.state, "digital")
        self.assertEqual(b.reason, "Playing HD Radio.")

    def test_dirty_signal_never_enters(self):
        b = hd.Blend()
        seen, _ = self.run_script(b, [(30, dict(running=True, log=Log(ber=5e-3), aligned=True))])
        self.assertEqual(seen, ["acquiring"])
        self.assertIn("isn't clean enough", b.reason)
        b = hd.Blend()
        seen, _ = self.run_script(b, [(30, dict(running=True, log=Log(mer=(4.0, 5.0)),
                                                aligned=True))])
        self.assertEqual(seen, ["acquiring"])

    def test_drops_fast_returns_slowly(self):
        b = hd.Blend()
        good = dict(running=True, log=Log(), aligned=True)
        self.run_script(b, [(4.1, good)])
        self.assertEqual(b.state, "digital")
        # one lost frame: straight back to analog
        b.tick(100.0, bad_audio=True, **good)
        self.assertEqual(b.state, "analog fallback")
        self.assertIn("frames were lost", b.reason)
        self.assertEqual(b.hold, 8.0)
        # lost sync, bad BER, too-low MER each drop it too
        for log, why in ((Log(synced=False), "lost sync"), (Log(ber=0.05), "bit error"),
                         (Log(mer=(1.0, 2.0)), "too weak")):
            b2 = hd.Blend()
            self.run_script(b2, [(4.1, good)])
            b2.tick(50.0, running=True, log=log, aligned=True)
            self.assertEqual(b2.state, "analog fallback", why)
            self.assertIn(why, b2.reason)

    def test_fringe_station_does_not_flap(self):
        # Clean for 5 s, a dropout, clean 5 s, dropout...: digital at most once
        # or twice, then the hold outgrows the clean spells.
        b = hd.Blend()
        good = dict(running=True, log=Log(), aligned=True)
        bad = dict(good, bad_audio=True)
        script = [(5.0, good), (0.1, bad)] * 30
        seen, _ = self.run_script(b, script)
        entries = sum(1 for s in seen if s == "digital")
        self.assertEqual(entries, 1, seen)     # once, then 5 s spells never reach the 8 s hold
        self.assertEqual(b.state, "analog fallback")
        self.assertEqual(b.hold, 8.0)
        # Spells longer than the hold come back, each drop doubling it.
        b = hd.Blend()
        seen, _ = self.run_script(b, [(9.0, good), (0.1, bad)] * 6)
        self.assertEqual(sum(1 for s in seen if s == "digital"), 2, seen)
        self.assertEqual(b.hold, 16.0)

    def test_hold_relaxes_after_long_digital(self):
        b = hd.Blend()
        good = dict(running=True, log=Log(), aligned=True)
        self.run_script(b, [(4.1, good), (0.1, dict(good, bad_audio=True)), (8.3, good)])
        self.assertEqual(b.state, "digital")
        self.assertEqual(b.hold, 8.0)
        self.run_script(b, [(hd.Blend.HOLD_RESET + 1, good)])
        self.assertEqual(b.hold, hd.Blend.HOLD_MIN)

    def test_hold_is_capped(self):
        b = hd.Blend()
        for _ in range(20):
            b._drop(0)
        self.assertEqual(b.hold, hd.Blend.HOLD_MAX)

    def test_not_running(self):
        b = hd.Blend()
        b.tick(1.0, running=False, detected=False)
        self.assertEqual((b.state, b.reason), ("no signal", "No HD Radio signal."))
        b.tick(1.1, running=False, detected=True, backoff=True)
        self.assertIn("waiting to try nrsc5 again", b.reason)


# ------------------------------------------------------------------ alignment

class Alignment(unittest.TestCase):
    def test_find_offset(self):
        rng = np.random.default_rng(2)
        s = sps.lfilter(*sps.butter(4, [200, 3500], "bandpass", fs=8000), rng.standard_normal(8000 * 30))
        # analog holds s[0:24 s] from index 1000; hd holds s[10 s:18 s] * 0.5 from index 50
        analog, a0 = s[:8000 * 24] + 0.05 * rng.standard_normal(8000 * 24), 1000
        hdx, h0 = 0.5 * s[8000 * 10:8000 * 18], 50
        k, score, gain = hd.find_offset(analog, a0, hdx, h0)
        # hd index h carries analog index h + k: h0 <-> a0 + 80000
        self.assertEqual(k, a0 + 80000 - h0)
        self.assertGreater(score, 0.9)
        self.assertAlmostEqual(gain, 2.0, delta=0.1)

    def test_partial_overlap_hd_ahead(self):
        # HD runs ahead: its newest audio isn't in the analog yet.
        rng = np.random.default_rng(4)
        s = sps.lfilter(*sps.butter(4, [200, 3500], "bandpass", fs=8000), rng.standard_normal(8000 * 30))
        analog = s[:8000 * 20]
        hdx = s[8000 * 14:8000 * 26]   # 6 s overlap
        k, score, _ = hd.find_offset(analog, 0, hdx, 0)
        self.assertEqual(k, 8000 * 14)

    def test_nothing_in_common(self):
        rng = np.random.default_rng(6)
        self.assertIsNone(hd.find_offset(rng.standard_normal(8000 * 20), 0,
                                         rng.standard_normal(8000 * 8), 0))
        self.assertIsNone(hd.find_offset(np.zeros(100), 0, np.zeros(100), 0))  # too short

    def test_ring(self):
        r = hd.Ring(1.0, 10)
        r.write(np.arange(7))
        r.write(np.arange(7, 15))
        self.assertEqual(r.end, 15)
        self.assertEqual(list(r.read(5, 10)), list(range(5, 15)))
        self.assertIsNone(r.read(4, 2))   # fallen out
        self.assertIsNone(r.read(10, 6))  # not there yet


# ------------------------------------------------------------------ against the fake nrsc5

class FakeNrsc5(unittest.TestCase):
    """Hd driving tests/fixtures/fake_nrsc5.py as nrsc5, on synthetic FM IQ."""

    def make(self, program=0, **cfg):
        os.environ["FAKE_NRSC5"] = json.dumps(cfg)
        self.addCleanup(os.environ.pop, "FAKE_NRSC5", None)
        h = hd.Hd(FAKE, program, clock=time.monotonic)
        self.addCleanup(h.close)
        return h

    def states(self, h):
        seen = []

        def on_block(b, now):
            st = h.blend.state
            if not seen or seen[-1][1] != st:
                seen.append((round(now, 1), st))
        return seen, on_block

    def check_aligned(self, out, start_s, lead):
        """The output from start_s on is the program audio, lined up with analog's."""
        a = out[int(start_s * 48000):]
        t0 = start_s
        ref = fake_nrsc5.program_audio(t0 + np.arange(len(a)) / 48000.0)
        bp = sps.firwin(255, [200, 3500], fs=48000, pass_zero=False)
        aa, rr = sps.lfilter(bp, 1, a), sps.lfilter(bp, 1, ref)
        n = min(len(aa), 48000)
        c = np.correlate(aa[2000:2000 + n], rr[:n + 4000], "valid")
        return int(np.argmax(c))

    def test_acquire_align_blend(self):
        h = self.make(lead=2.0, station="TEST", title="A Song", artist="The Band",
                      programs=[[0, "None"], [1, "Jazz"]])
        seen, cb = self.states(h)
        out = run_hd(h, FmStation(), 16.0, on_block=cb)
        names = [s for _, s in seen]
        self.assertEqual(names[:3], ["no signal", "acquiring", "digital"], seen)
        self.assertEqual(h.blend.state, "digital")
        st = h.status(16.0)
        self.assertEqual(st["state"], "digital")
        self.assertEqual(st["station"], "TEST")
        self.assertEqual((st["title"], st["artist"]), ("A Song", "The Band"))
        self.assertEqual([p["name"] for p in st["programs"]], ["HD1", "HD2"])
        self.assertEqual(st["program"], 0)
        self.assertEqual(st["mer_db"], {"lower": 12.0, "upper": 13.0})
        self.assertEqual(st["analog_delay_s"], 0.0)       # HD ahead: the analog isn't delayed
        # 2 s less the fake's own pipe latency
        self.assertGreater(st["hd_lead_s"], 1.0)
        self.assertLess(st["hd_lead_s"], 2.05)
        self.assertGreater(st["clean_s"], 0)
        # The HD lined up with the analog: same content, no echo. Analog's own
        # chain delay is the reference; HD (lead 2 s) is played to match it.
        dig_at = next(t for t, s in seen if s == "digital")
        analog_lag = self.check_aligned(out[:int((dig_at - 0.5) * 48000)], 3.0, 0)
        hd_lag = self.check_aligned(out, dig_at + 1.0, 0)
        self.assertLessEqual(abs(hd_lag - analog_lag), 3, (hd_lag, analog_lag))
        # and it is the HD that plays: the fake's audio has no FM noise, but
        # mostly this: the output keeps going smoothly across the crossfade
        i = int(dig_at * 48000)
        seg = out[i - 24000:i + 24000]
        self.assertLess(np.max(np.abs(np.diff(seg))), 0.5)

    def test_lost_sync_falls_back_and_returns(self):
        h = self.make(lead=2.0, lost=[[9.0, 10.0]])
        seen, cb = self.states(h)
        run_hd(h, FmStation(), 24.0, on_block=cb)
        names = [s for _, s in seen]
        self.assertIn("analog fallback", names)
        i = names.index("analog fallback")
        self.assertEqual(names[i - 1], "digital")
        self.assertEqual(names[-1], "digital", seen)    # back, after a longer hold
        back = [t for t, s in seen][-1]
        fell = [t for t, s in seen][i]
        self.assertGreaterEqual(back - fell, 8.0)

    def test_bad_frames_leave_before_they_play(self):
        h = self.make(lead=2.0, bad=[[10.0, 10.2]])
        seen, cb = self.states(h)
        out = run_hd(h, FmStation(), 14.0, on_block=cb)
        fell = next(t for t, s in seen if s == "analog fallback")
        # HD audio from IQ time 10 s carries content 12 s, played at ~12 s;
        # the blend leaves when the frames arrive, about 2 s before.
        self.assertLess(fell, 11.0)
        # no silent hole in the output where the bad frames would have played
        seg = out[int(11.5 * 48000):int(12.8 * 48000)]
        env = np.sqrt(np.convolve(seg ** 2, np.ones(480) / 480, "valid"))
        self.assertGreater(env.min(), 0.02)

    def test_hd_behind_delays_analog(self):
        h = self.make(lead=-3.0)
        seen, cb = self.states(h)
        run_hd(h, FmStation(), 22.0, on_block=cb)
        self.assertEqual(h.blend.state, "digital", seen)
        st = h.status(22.0)
        self.assertGreater(st["hd_lead_s"], -4.0)
        self.assertLess(st["hd_lead_s"], -2.95)
        self.assertGreater(st["analog_delay_s"], 3.0)
        self.assertLess(st["analog_delay_s"], 4.0)

    def test_follows_a_small_move(self):
        # The HD shifts 50 ms against the analog mid-run: rxd follows it in
        # place, staying digital, instead of falling back to analog.
        h = self.make(lead=2.0, shift=[[9.0, 0.05]])
        h.CHECK_EVERY = 2.0
        seen, cb = self.states(h)
        run_hd(h, FmStation(), 20.0, on_block=cb)
        names = [s for _, s in seen]
        self.assertEqual(names, ["no signal", "acquiring", "digital"], seen)
        st = h.status(20.0)
        self.assertEqual(st["realigned"], 1)
        self.assertAlmostEqual(st["hd_lead_s"], 2.05, delta=0.6)

    def test_hd2_is_digital_only(self):
        h = self.make(program=1, lead=2.0, lost=[[8.0, 9.0]])
        seen, cb = self.states(h)
        out = run_hd(h, FmStation(), 12.0, on_block=cb)
        # nrsc5 runs from the start: no waiting for sidebands with HD2 picked
        self.assertAlmostEqual(h.started_at, BLOCK / R)
        # silence before digital and while sync is lost
        dig = next(t for t, s in seen if s == "digital")
        self.assertLess(np.max(np.abs(out[:int((dig - 0.2) * 48000)])), 1e-6)
        fb = next(t for t, s in seen if s == "analog fallback")
        self.assertLess(np.max(np.abs(out[int((fb + 0.4) * 48000):int((fb + 0.9) * 48000)])), 1e-6)
        self.assertIn("HD2 is digital-only", h.status(fb + 0.5)["reason"])
        # and HD2's own audio while digital
        seg = out[int((dig + 0.5) * 48000):int(fb * 48000)]
        self.assertGreater(np.sqrt(np.mean(seg ** 2)), 0.05)

    def test_program_change_restarts_nrsc5(self):
        with tempfile.TemporaryDirectory() as d:
            argv = os.path.join(d, "argv")
            h = self.make(lead=2.0, log=argv)
            run_hd(h, FmStation(), 3.0)
            h.set_program(2)
            run_hd(h, FmStation(), 0.5)
            time.sleep(0.3)
            with open(argv) as fh:
                runs = [json.loads(x) for x in fh]
        self.assertEqual([r[-1] for r in runs], ["0", "2"])
        self.assertEqual(runs[0][:6], ["-r", "-", "-o", "-", "-t", "raw"])

    def test_no_sidebands_no_nrsc5(self):
        h = self.make()
        run_hd(h, FmStation(hd_db=None), 3.0)
        self.assertIsNone(h.proc)
        self.assertEqual(h.status(3.0)["state"], "no signal")
        self.assertEqual(h.status(3.0)["reason"], "No HD Radio signal.")

    def test_no_sync_backs_off(self):
        h = self.make(sync_at=1e9)
        h.ACQUIRE_TIMEOUT = 2.0
        run_hd(h, FmStation(), 4.0)
        self.assertIsNone(h.proc)
        st = h.status(4.0)
        self.assertEqual(st["state"], "no signal")
        self.assertIn("no HD sync", st["reason"])
        self.assertIn("trying again later", st["reason"])

    def test_nrsc5_exits(self):
        h = self.make(exit_at=2.0)
        run_hd(h, FmStation(), 4.0)
        self.assertIsNone(h.proc)
        self.assertIn("nrsc5 exited", h.status(4.0)["reason"])


# ------------------------------------------------------------------ rxd wiring

class Source:
    """FmStation as an rxd source, paced at speed x real time."""

    paced = False

    def __init__(self, station, speed=4.0):
        self.st = station
        self.center = None
        self.rate = None
        self.speed = speed
        self.t_next = None
        self.reads = 0

    def gains(self):
        return []

    def configure(self, rate, ppm, bias_t):
        self.rate = rate

    def set_rate(self, rate):
        self.rate = rate

    def set_gain(self, db):
        pass

    def set_center(self, hz):
        self.center = float(hz)

    def read_raw(self, n):
        now = time.monotonic()
        if self.t_next is None:
            self.t_next = now
        if self.t_next > now:
            time.sleep(self.t_next - now)
        self.t_next += n / self.rate / self.speed
        self.reads += 1
        return rxd.complex_to_cu8(self.st.block(n))

    def read(self, n):
        return rxd.cu8_to_complex(self.read_raw(n))

    def close(self):
        pass


class Capture:
    def __init__(self):
        self.chunks = []

    def write(self, pcm):
        self.chunks.append(pcm)

    def close(self):
        pass


class Wiring(unittest.TestCase):
    def receiver(self, p=None, nrsc5=FAKE, speed=4.0, station_obj=None):
        rx = rxd.Receiver(rxd.Settings(json.loads(json.dumps(p or WFM)), "fm-test"),
                          Source(station_obj or FmStation(), speed), Capture())
        rx.nrsc5 = lambda conf: nrsc5
        t0 = time.monotonic()
        rx.clock = lambda: t0 + speed * (time.monotonic() - t0)
        return rx

    def test_not_installed(self):
        rx = self.receiver(nrsc5=None)
        rx.build(rx.s.snapshot())
        self.assertFalse(rx.chain.hd)
        self.assertEqual(rx.chain.rate, 1200000)          # FM exactly as before
        self.assertIsNone(rx.hd)
        st = rx.hd_status(rx.s.snapshot())
        self.assertEqual(st["state"], "off")
        self.assertEqual(st["reason"], "HD Radio: nrsc5 not installed")

    def test_off_for_profile_and_non_wfm(self):
        rx = self.receiver(p=dict(WFM, hd=False))
        rx.build(rx.s.snapshot())
        self.assertIsNone(rx.hd)
        self.assertEqual(rx.hd_status(rx.s.snapshot())["reason"], "HD Radio is off for this profile.")
        self.assertFalse(rx.hd_status(rx.s.snapshot())["enabled"])
        rx = self.receiver(p=dict(WFM, demod="FM", demod_settings={"bandwidth": 12500}))
        rx.build(rx.s.snapshot())
        self.assertIsNone(rx.hd)
        self.assertIn("WFM", rx.hd_status(rx.s.snapshot())["reason"])

    def test_off_for_station(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "station.json")
            with open(path, "w") as fh:
                json.dump({"hd": False}, fh)
            old = os.environ["RXD_STATION"]
            os.environ["RXD_STATION"] = path
            try:
                rx = self.receiver()
                rx.build(rx.s.snapshot())
                self.assertIsNone(rx.hd)
                st = rx.hd_status(rx.s.snapshot())
                self.assertEqual(st["reason"], "HD Radio is off for this station.")
                self.assertFalse(st["station_enabled"])
            finally:
                os.environ["RXD_STATION"] = old

    def test_station_file(self):
        import station
        self.assertEqual(station.Station({}).hd, {"enabled": True, "nrsc5": None})
        self.assertEqual(station.Station({"hd": False}).hd["enabled"], False)
        s = station.Station({"hd": {"enabled": "yes", "nrsc5": 5}})
        self.assertEqual(s.hd, {"enabled": True, "nrsc5": None})
        self.assertEqual(len(s.problems), 2)

    def test_find_nrsc5(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "nrsc5")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            old = os.environ.pop("RXD_NRSC5")
            try:
                self.assertEqual(hd.find_nrsc5(exe), exe)
                self.assertIsNone(hd.find_nrsc5(os.path.join(d, "missing")))
                os.environ["RXD_NRSC5"] = "off"
                self.assertIsNone(hd.find_nrsc5(exe))
            finally:
                os.environ["RXD_NRSC5"] = old

    def test_profile_settings(self):
        s = rxd.Settings(dict(WFM, hd={"enabled": True, "program": 2}))
        self.assertEqual((s.hd_enabled, s.hd_program), (True, 2))
        s.update(hd_program=1)
        self.assertEqual(s.snapshot()["hd_program"], 1)
        for bad in (8, -1, "1", True, 1.5):
            with self.assertRaises(ValueError):
                s.update(hd_program=bad)
        with self.assertRaises(ValueError):
            s.update(hd_enabled="no")
        with self.assertRaises(ValueError):
            rxd.Settings(dict(WFM, hd={"program": 9}))
        self.assertEqual(rxd.hd_profile(None), (True, 0))
        self.assertEqual(rxd.hd_profile(False), (False, 0))

    def test_capture_keeps_program(self):
        st = {"freq": 99.9e6, "mode": "WFM", "bandwidth": 150000.0,
              "hd": {"program": 2, "enabled": True, "state": "digital"}}
        p = radio.capture(dict(WFM), st)
        self.assertEqual(p["hd"], {"enabled": True, "program": 2})
        # back through rxd: the same choice
        s = rxd.Settings(p)
        self.assertEqual((s.hd_enabled, s.hd_program), (True, 2))
        st["hd"] = {"program": 0, "enabled": False}
        self.assertEqual(radio.capture(dict(WFM), st)["hd"], {"enabled": False, "program": 0})
        # an older rxd without the block leaves the profile's alone
        p = radio.capture(dict(WFM, hd={"program": 1}), {"freq": 99.9e6})
        self.assertEqual(p["hd"], {"program": 1})

    def test_live_blend_through_the_api(self):
        """rxd on the fake: status, tune, and /api/v1/audio carrying the HD."""
        os.environ["FAKE_NRSC5"] = json.dumps({"lead": 2.0, "station": "TEST", "marker_hz": 5000,
                                               "programs": [[0, "None"], [1, "Jazz"]]})
        self.addCleanup(os.environ.pop, "FAKE_NRSC5", None)
        rx = self.receiver(speed=4.0)
        rx.start()
        self.addCleanup(rx.close)
        self.addCleanup(rx.stop.set)
        srv = rxd.ThreadingHTTPServer(("127.0.0.1", 0), rxd.Api)
        srv.daemon_threads = True
        srv.rx = rx
        srv.js8 = rxd.Js8Feed(None)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = "http://127.0.0.1:%d/api/v1" % srv.server_address[1]
        self.assertEqual(rx.chain.rate, hd.HD_RATE)
        self.assertEqual(rx.src.center, 99.9e6)            # centred, for nrsc5
        st = json.load(urllib.request.urlopen(base + "/status"))
        self.assertIn(st["hd"]["state"], ("no signal", "acquiring"))
        # Listen as the browser does while it acquires and blends.
        got, stop = [], threading.Event()

        def listen():
            with urllib.request.urlopen(base + "/audio?rate=48000", timeout=10) as r:
                self.assertEqual(r.headers["X-Audio-Rate"], "48000")
                while not stop.is_set():
                    b = r.read(9600)
                    if not b:
                        return
                    got.append(b)
        threading.Thread(target=listen, daemon=True).start()
        deadline = time.monotonic() + 30
        before = None
        while time.monotonic() < deadline:
            st = json.load(urllib.request.urlopen(base + "/status"))
            if st["hd"]["state"] == "acquiring" and sum(len(b) for b in got) > 4 * 48000:
                before = sum(len(b) for b in got)       # the stream so far was analog
            if st["hd"]["state"] == "digital":
                break
            time.sleep(0.05)
        self.assertEqual(st["hd"]["state"], "digital", st["hd"])
        self.assertEqual(st["hd"]["station"], "TEST")
        self.assertEqual(st["hd"]["reason"], "Playing HD Radio.")
        time.sleep(0.5)                                  # past the crossfade
        mark = sum(len(b) for b in got)
        time.sleep(0.6)
        # HD2 by the API: status says so at once, and rxd restarts nrsc5 on program 1
        req = urllib.request.Request(base + "/tune", data=json.dumps({"hd_program": 1}).encode(),
                                     headers={"Content-Type": "application/json"})
        st = json.load(urllib.request.urlopen(req))
        self.assertEqual(st["hd"]["program"], 1)
        time.sleep(0.5)
        stop.set()
        pcm = np.frombuffer(b"".join(got), "<i2").astype(np.float32) / 32767

        def marker(i):
            seg = pcm[i // 2: i // 2 + 24000]
            sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            f = np.fft.rfftfreq(len(seg), 1 / 48000)
            at = sp[np.abs(f - 5000) < 20].max()
            near = np.median(sp[(np.abs(f - 5000) > 200) & (np.abs(f - 5000) < 1500)])
            return 20 * np.log10(at / near)
        # The stream carries the HD once digital (its marker tone), and didn't before.
        self.assertIsNotNone(before)
        self.assertLess(marker(before - 48000), 40)     # the half second up to then: analog
        self.assertGreater(marker(mark), 50)            # HD: its marker 60+ dB up
        bad = urllib.request.Request(base + "/tune", data=json.dumps({"hd_program": 9}).encode())
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        self.assertEqual(e.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
