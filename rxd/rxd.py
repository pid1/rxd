#!/usr/bin/env python3
"""rxd: a small RTL-SDR receiver daemon, the receiver behind `radio`.

  rxd PROFILE                 receive with a radio profile (a name under
                              ~/.config/rx/radio/ or a path to its JSON)
  rxd PROFILE --audio stdout  raw s16le mono 48 kHz on stdout instead
  rxd PROFILE --iq FILE|-     read cu8 IQ from a file or stdin, not a dongle
  rxd PROFILE --iq synth      a synthetic signal: noise plus a test tone
                              1500 Hz above the dial, for testing anything
                              but the radio itself

It reads the same profile JSON as `radio`, opens the dongle by serial,
demodulates one channel (USB, LSB, AM, FM or WFM) and plays it to a
PipeWire/PulseAudio sink. Two control ports, both on 127.0.0.1 by default:

  4532  Hamlib NET rigctl (rig model 2), so JS8Call or `rigctl -m 2` can
        read and set the frequency and mode
  8073  HTTP JSON: GET /api/status, POST /api/tune {"freq": 7078000,
        "mode": "USB", "bandwidth": 2800, "gain_db": 3.7, "volume": 0.2}

Frequencies on both ports are the dial, as JS8Call means it. A JS8 profile
whose VFO sits off its js8call_dial (7077900 vs 7078000, tuned by hand)
keeps that difference as a calibration, so the dial still reads 7078000.

SIGHUP re-reads the profile; SIGINT or SIGTERM stops.
"""
import argparse
import ctypes
import ctypes.util
import json
import os
import queue
import signal
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# One BLAS thread. The filters are small matrix products, and OpenBLAS's
# default pool of one thread per core busy-waits between them: measured on
# the uConsole, a live run sat at 131% CPU with the pool and the DSP needed
# about 16% of one core without it. Set before numpy loads, or it is ignored.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import numpy as np  # noqa: E402
from scipy import signal as sps  # noqa: E402

STATE = os.path.expanduser("~/.config/rx/radio")
AUDIO_RATE = 48000
# A profile's "offset": "auto" (the default) upconverts below
# upconvert_below_hz, since the R820T tunes no lower than about 24 MHz and
# the Ham It Up is how HF gets in; "Ham-It-Up" always upconverts and "None"
# (or SDR++'s "Manual") never does. The Ham It Up's own UPCONVERT/BYPASS
# switch is physical: rxd can only report which way it should be.
OFFSET_MODES = ("auto", "Ham-It-Up", "None", "Manual")
UPCONVERTER_HZ = 125e6
UPCONVERT_BELOW_HZ = 30e6
# Profile sink descriptions -> PipeWire node names (None: default sink).
SINKS = {"SDR to JS8 (virtual cable)": "SDR_to_JS8", "Built-in Audio": None}
MODES = ("USB", "LSB", "AM", "FM", "WFM")
DEFAULT_BW = {"USB": 2800.0, "LSB": 2800.0, "AM": 10000.0, "FM": 12500.0,
              "WFM": 150000.0}


def log(msg):
    print("rxd: " + msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ profile

def load_profile(spec):
    path = spec if os.sep in spec or spec.endswith(".json") else \
        os.path.join(STATE, spec + ".json")
    if not os.path.isfile(path):
        sys.exit("rxd: no profile at %s (save one with: radio show NAME > FILE)"
                 % path)
    with open(path, encoding="utf-8") as fh:
        p = json.load(fh)
    return p, path


class Settings:
    """What to receive. Shared between the control ports and the DSP thread."""

    def __init__(self, p, name=None):
        self.lock = threading.Lock()
        self.name = name
        self.apply_profile(p)

    def apply_profile(self, p):
        with self.lock:
            mode = p.get("demod", "USB")
            if mode not in MODES:
                raise ValueError("rxd does not demodulate %s" % mode)
            self.mode = mode
            ds = p.get("demod_settings", {})
            bw = float(ds.get("bandwidth", DEFAULT_BW[mode]))
            # SDR++'s 3000 Hz USB filter starts at 0; ours starts at 200 Hz.
            self.bandwidth = min(bw, 2800.0) if mode in ("USB", "LSB") else bw
            self.agc_attack = float(ds.get("agcAttack", 50.0))
            self.agc_decay = float(ds.get("agcDecay", 5.0))
            self.volume = float(p.get("volume", 0.5))
            carrier = float(p["frequency"])  # SDR++'s VFO: the carrier
            dial = float(p.get("js8call_dial", carrier))
            self.cal = dial - carrier
            self.dial = dial
            self.offset_mode = p.get("offset", "auto")
            if self.offset_mode not in OFFSET_MODES:
                raise ValueError("offset must be one of " + ", ".join(OFFSET_MODES))
            self.upconverter_hz = float(p.get("upconverter_hz", UPCONVERTER_HZ))
            self.upconvert_below = float(p.get("upconvert_below_hz", UPCONVERT_BELOW_HZ))
            rtl = p.get("rtl", {})
            self.serial = str(rtl.get("serial", ""))
            self.gain_index = rtl.get("gain")
            # A saved gain_db wins; otherwise the index (SDR++'s convention)
            # is looked up in the dongle's gain table once it is open.
            self.gain_db = float(rtl["gain_db"]) if rtl.get("gain_db") is not None else None
            self.ppm = int(rtl.get("ppm", 0))
            self.bias_t = bool(rtl.get("biasT", False))
            self.sink = SINKS.get(p.get("sink"), p.get("sink"))
            self.version = getattr(self, "version", 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(mode=self.mode, bandwidth=self.bandwidth, dial=self.dial,
                        cal=self.cal, upconverter=self.upconverter(),
                        offset_mode=self.offset_mode,
                        upconvert_below=self.upconvert_below,
                        agc_attack=self.agc_attack, agc_decay=self.agc_decay,
                        volume=self.volume, gain_db=self.gain_db,
                        version=self.version)

    def upconverter(self):
        """The offset in effect for the current dial (call with the lock held)."""
        if self.offset_mode == "Ham-It-Up":
            return self.upconverter_hz
        if self.offset_mode == "auto" and self.dial < self.upconvert_below:
            return self.upconverter_hz
        return 0.0

    def update(self, freq=None, mode=None, bandwidth=None, gain_db=None,
               volume=None):
        with self.lock:
            if mode is not None:
                mode = mode.upper()
                if mode not in MODES:
                    raise ValueError("mode must be one of " + ", ".join(MODES))
                if mode != self.mode and bandwidth is None:
                    self.bandwidth = DEFAULT_BW[mode]
                self.mode = mode
            if freq is not None:
                freq = float(freq)
                if not 100e3 <= freq <= 1.7e9:
                    raise ValueError("frequency out of range")
                self.dial = freq
            if bandwidth is not None and float(bandwidth) > 0:
                self.bandwidth = float(bandwidth)
            if gain_db is not None:
                self.gain_db = float(gain_db)
            if volume is not None:
                volume = float(volume)
                if not 0 <= volume <= 4:
                    raise ValueError("volume must be between 0 and 4")
                self.volume = volume
            self.version += 1


# ------------------------------------------------------------------ sources

class RtlSource:
    """An RTL-SDR dongle through librtlsdr (the library radio already uses)."""

    def __init__(self, serial):
        path = ctypes.util.find_library("rtlsdr")
        if not path:
            sys.exit("rxd: librtlsdr not found")
        lib = self.lib = ctypes.CDLL(path)
        lib.rtlsdr_get_device_usb_strings.argtypes = [
            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        for fn in ("set_tuner_gain_mode", "set_tuner_gain", "set_freq_correction",
                   "set_bias_tee", "set_direct_sampling", "set_agc_mode"):
            if hasattr(lib, "rtlsdr_" + fn):  # bias-T is rtl-sdr-blog's
                getattr(lib, "rtlsdr_" + fn).argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.rtlsdr_get_tuner_gains.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_int)]
        lib.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_read_sync.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        index = None
        for i in range(lib.rtlsdr_get_device_count()):
            bufs = [ctypes.create_string_buffer(256) for _ in range(3)]
            if lib.rtlsdr_get_device_usb_strings(i, *bufs) == 0 and \
                    (not serial or bufs[2].value.decode(errors="replace") == serial):
                index = i
                break
        if index is None:
            sys.exit("rxd: dongle SN %s is not plugged in" % serial)
        self.dev = ctypes.c_void_p()
        if lib.rtlsdr_open(ctypes.byref(self.dev), index) != 0:
            sys.exit("rxd: could not open dongle SN %s (is another rxd holding it? sudo fuser -v /dev/bus/usb/*/*)"
                     % serial)
        self.center = None
        self.rate = None
        self.lock = threading.Lock()

    def gains(self):
        n = self.lib.rtlsdr_get_tuner_gains(self.dev, None)
        arr = (ctypes.c_int * max(n, 0))()
        self.lib.rtlsdr_get_tuner_gains(self.dev, arr)
        return [g / 10.0 for g in arr]

    def configure(self, rate, ppm, bias_t):
        lib, dev = self.lib, self.dev
        lib.rtlsdr_set_direct_sampling(dev, 0)
        lib.rtlsdr_set_agc_mode(dev, 0)
        lib.rtlsdr_set_tuner_gain_mode(dev, 1)
        if ppm:
            lib.rtlsdr_set_freq_correction(dev, ppm)
        if hasattr(lib, "rtlsdr_set_bias_tee"):
            lib.rtlsdr_set_bias_tee(dev, int(bias_t))
        self.set_rate(rate)

    def set_rate(self, rate):
        with self.lock:
            if rate != self.rate:
                self.lib.rtlsdr_set_sample_rate(self.dev, int(rate))
                self.lib.rtlsdr_reset_buffer(self.dev)
                self.rate = rate

    def set_gain(self, db):
        self.lib.rtlsdr_set_tuner_gain(self.dev, int(round(db * 10)))

    def set_center(self, hz):
        with self.lock:
            self.lib.rtlsdr_set_center_freq(self.dev, int(hz))
            self.center = float(hz)

    def read(self, n):
        buf = (ctypes.c_uint8 * (2 * n))()
        got = ctypes.c_int()
        with self.lock:
            r = self.lib.rtlsdr_read_sync(self.dev, buf, 2 * n, ctypes.byref(got))
        if r != 0:
            raise IOError("rtlsdr_read_sync returned %d" % r)
        return cu8_to_complex(np.frombuffer(buf, np.uint8, got.value))

    def close(self):
        self.lib.rtlsdr_close(self.dev)


class FileSource:
    """cu8 IQ from a file or stdin. Retuning only moves the software mixer."""

    paced = False  # nothing is lost by waiting: block rather than drop

    def __init__(self, path, center=None):
        self.fh = sys.stdin.buffer if path == "-" else open(path, "rb")
        self.center = center
        self.rate = None
        self.fixed_center = center is not None

    def gains(self):
        return []

    def configure(self, rate, ppm, bias_t):
        self.rate = rate

    def set_rate(self, rate):
        self.rate = rate

    def set_gain(self, db):
        pass

    def set_center(self, hz):
        if not self.fixed_center:
            self.center = float(hz)

    def read(self, n):
        raw = self.fh.read(2 * n)
        if len(raw) < 2:
            raise EOFError
        return cu8_to_complex(np.frombuffer(raw[: len(raw) // 2 * 2], np.uint8))

    def close(self):
        if self.fh is not sys.stdin.buffer:
            self.fh.close()


class SynthSource:
    """Noise plus a carrier at a fixed RF, paced in real time."""

    def __init__(self, tone_rf, snr_db=20.0):
        self.tone_rf = tone_rf
        self.amp = 10 ** (snr_db / 20) * 0.01
        self.center = None
        self.rate = None
        self.phase = 0.0
        self.t_next = None
        self.rng = np.random.default_rng(8)

    def gains(self):
        return [0.0, 3.7, 16.6, 37.2]

    def configure(self, rate, ppm, bias_t):
        self.rate = rate

    def set_rate(self, rate):
        self.rate = rate

    def set_gain(self, db):
        pass

    def set_center(self, hz):
        self.center = float(hz)

    def read(self, n):
        now = time.monotonic()
        if self.t_next is None:
            self.t_next = now
        if self.t_next > now:
            time.sleep(self.t_next - now)
        self.t_next += n / self.rate
        w = 2 * np.pi * (self.tone_rf - self.center) / self.rate
        ph = self.phase + w * np.arange(n)
        self.phase = (self.phase + w * n) % (2 * np.pi)
        noise = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        return (self.amp * np.exp(1j * ph) + 0.01 * noise).astype(np.complex64)

    def close(self):
        pass


# Every (I, Q) byte pair as one little-endian uint16 -> its complex sample.
_CU8 = (np.arange(256, dtype=np.float32) - 127.5) / 127.5
CU8_LUT = (_CU8[np.arange(65536) & 0xFF] + 1j * _CU8[np.arange(65536) >> 8]).astype(np.complex64)


def cu8_to_complex(b):
    b = b[: len(b) // 2 * 2]
    return CU8_LUT[np.ascontiguousarray(b).view("<u2")]


# --------------------------------------------------------------------- sinks

class PulseSink:
    """Playback through libpulse-simple (PipeWire's Pulse server)."""

    class Spec(ctypes.Structure):
        _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32),
                    ("channels", ctypes.c_uint8)]

    class Attr(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32) for n in
                    ("maxlength", "tlength", "prebuf", "minreq", "fragsize")]

    def __init__(self, device):
        path = ctypes.util.find_library("pulse-simple")
        if not path:
            sys.exit("rxd: libpulse-simple not found")
        lib = self.lib = ctypes.CDLL(path)
        lib.pa_simple_new.restype = ctypes.c_void_p
        lib.pa_simple_new.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int)]
        lib.pa_simple_write.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_int)]
        lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        spec = self.Spec(3, AUDIO_RATE, 1)  # PA_SAMPLE_S16LE, mono
        # About 300 ms of buffer: enough to ride out a slow block.
        tl = AUDIO_RATE * 2 * 3 // 10
        attr = self.Attr(0xFFFFFFFF, tl, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
        err = ctypes.c_int()
        self.s = lib.pa_simple_new(None, b"rxd", 1, device.encode() if device else None,
                                   b"receiver", ctypes.byref(spec), None,
                                   ctypes.byref(attr), ctypes.byref(err))
        if not self.s:
            sys.exit("rxd: could not open audio sink %s (pulse error %d)"
                     % (device or "default", err.value))

    def write(self, pcm):
        err = ctypes.c_int()
        b = pcm.tobytes()
        if self.lib.pa_simple_write(self.s, b, len(b), ctypes.byref(err)) < 0:
            raise IOError("pulse write failed (%d)" % err.value)

    def close(self):
        self.lib.pa_simple_free(self.s)


class StdoutSink:
    def write(self, pcm):
        sys.stdout.buffer.write(pcm.tobytes())
        sys.stdout.buffer.flush()

    def close(self):
        pass


class NullSink:
    def write(self, pcm):
        pass

    def close(self):
        pass


# ----------------------------------------------------------------------- DSP

class FirDecimator:
    """FIR filter keeping only every D-th output, with history across blocks."""

    def __init__(self, taps, decim):
        self.taps = np.asarray(taps)[::-1].copy()
        self.decim = decim
        self.hist = np.zeros(len(taps) - 1, np.complex64 if np.iscomplexobj(taps)
                             else np.float32)
        self.phase = 0  # index into the next block of the next output
        self.count = 0  # samples taken in before this block

    def set_taps(self, taps):
        """New taps of the same length, keeping history and phase."""
        self.taps = np.asarray(taps)[::-1].copy()

    def __call__(self, x):
        n = len(self.taps)
        if np.iscomplexobj(x) and not np.iscomplexobj(self.hist):
            self.hist = self.hist.astype(np.complex64)
        xx = np.concatenate([self.hist, x])
        win = np.lib.stride_tricks.sliding_window_view(xx, n)
        out = win[self.phase::self.decim] @ self.taps
        consumed = len(x) - self.phase
        self.phase = (-consumed) % self.decim
        self.hist = xx[len(xx) - (n - 1):]
        self.count += len(x)
        return out.astype(np.complex64 if np.iscomplexobj(out) else np.float32)


class Agc:
    """Envelope-following gain, computed per 5 ms and interpolated between."""

    STEP = 240

    def __init__(self, attack, decay, rate=AUDIO_RATE, target=0.5):
        # SDR++'s attack and decay are per-second rates: a = rate / fs.
        self.a = 1 - (1 - min(attack / rate, 1.0)) ** self.STEP
        self.d = 1 - (1 - min(decay / rate, 1.0)) ** self.STEP
        self.env = 1e-4
        self.gain = target / self.env
        self.target = target

    def __call__(self, x):
        n = len(x)
        steps = max(n // self.STEP, 1)
        pk = np.abs(x[: steps * self.STEP]).reshape(steps, -1).max(axis=1) \
            if n >= self.STEP else np.array([np.abs(x).max() if n else 0.0])
        gains = np.empty(steps + 1)
        gains[0] = self.gain
        for i, e in enumerate(pk):
            k = self.a if e > self.env else self.d
            self.env += (e - self.env) * k
            self.env = max(self.env, e if e > 4 * self.env else 0.0, 1e-6)
            gains[i + 1] = self.target / self.env
        self.gain = gains[-1]
        g = np.interp(np.arange(n), np.linspace(0, n, steps + 1), gains)
        return x * g.astype(np.float32)


class Chain:
    """Shift and decimate -> channel filter -> demodulator -> 48 kHz audio.

    The frequency shift is folded into the first filter: its taps are the
    low-pass prototype moved up to the signal, and the output is turned back
    down at the decimated rate. Mixing every input sample first costs a
    complex exponential per sample, which at WFM's 1.2 MS/s alone was more
    than the CM4 has.
    """

    def __init__(self, mode, bandwidth, attack, decay):
        self.mode = mode
        self.bandwidth = bandwidth
        if mode == "WFM":
            self.rate, self.if_rate = 1200000, 240000
            self.offset = 250000.0
            cutoff = min(bandwidth / 2 + 15000, 100000)
        else:
            self.rate, self.if_rate = 240000, AUDIO_RATE
            self.offset = 50000.0
            cutoff = 20000.0 if mode in ("USB", "LSB") else min(bandwidth / 2 + 2000, 20000)
        d1 = self.rate // self.if_rate
        self.proto1 = sps.firwin(16 * d1 + 1, cutoff, fs=self.rate)
        self.stage1 = FirDecimator(self.proto1.astype(np.complex64), d1)
        self.shift = None  # the offset stage1's taps are built for
        fs = self.if_rate
        if mode == "WFM":
            # stage1 already cuts at 90 kHz; a second filter adds nothing.
            self.chan = None
        elif mode in ("USB", "LSB"):
            lo, hi = 200.0, 200.0 + bandwidth
            half = (hi - lo) / 2
            proto = sps.firwin(255, half, fs=fs)
            fc = (lo + hi) / 2 * (1 if mode == "USB" else -1)
            taps = proto * np.exp(2j * np.pi * fc * np.arange(len(proto)) / fs)
            self.chan = FirDecimator(taps.astype(np.complex64), 1)
        else:
            self.chan = FirDecimator(sps.firwin(127, min(bandwidth / 2, fs / 2 * 0.9),
                                                fs=fs), 1)
        self.agc = Agc(attack, decay) if mode in ("USB", "LSB", "AM") else None
        self.prev = np.complex64(1)
        self.dc = 0.0
        if mode == "WFM":
            tau = 75e-6
            a = np.exp(-1 / (fs * tau))
            self.deemph = ([1 - a], [1, -a])
            self.deemph_zi = np.zeros(1)
            self.stage2 = FirDecimator(sps.firwin(63, 15000, fs=fs), fs // AUDIO_RATE)
        self.level_db = -120.0

    def shift_decimate(self, x, offset):
        w = 2 * np.pi * offset / self.rate
        if offset != self.shift:
            n = np.arange(len(self.proto1))
            self.stage1.set_taps((self.proto1 * np.exp(1j * w * n)).astype(np.complex64))
            self.shift = offset
        first = self.stage1.count + self.stage1.phase
        y = self.stage1(x)
        m = first + self.stage1.decim * np.arange(len(y), dtype=np.int64)
        return y * np.exp(-1j * ((w * m) % (2 * np.pi))).astype(np.complex64)

    def fm(self, y):
        z = np.concatenate([[self.prev], y])
        self.prev = y[-1] if len(y) else self.prev
        return np.angle(z[1:] * np.conj(z[:-1])).astype(np.float32)

    def __call__(self, iq, offset):
        y = self.shift_decimate(iq, offset)
        if self.chan is not None:
            y = self.chan(y)
        if len(y):
            p = float(np.mean(np.abs(y) ** 2))
            self.level_db = 10 * np.log10(p + 1e-12)
        if self.mode in ("USB", "LSB"):
            audio = self.agc(y.real.astype(np.float32))
        elif self.mode == "AM":
            env = np.abs(y).astype(np.float32)
            self.dc += (float(env.mean()) - self.dc) * 0.1 if len(env) else 0
            audio = self.agc(env - self.dc)
        elif self.mode == "FM":
            audio = self.fm(y) * (self.if_rate / (2 * np.pi * 5000.0))
        else:  # WFM
            d = self.fm(y) * (self.if_rate / (2 * np.pi * 75000.0))
            d, self.deemph_zi = sps.lfilter(*self.deemph, d, zi=self.deemph_zi)
            audio = self.stage2(d.astype(np.float32))
        return audio.astype(np.float32)


# -------------------------------------------------------------------- engine

class Receiver:
    """Reader thread -> queue -> DSP thread -> sink."""

    MIN_OFF, MAX_OFF = 20000.0, 100000.0  # keep clear of the DC spike

    def __init__(self, settings, source, sink):
        self.s = settings
        self.src = source
        self.sink = sink
        self.q = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.dropped = 0
        self.blocks = 0
        self.chain = None
        self.version = None
        self.error = None
        self.cpu = 0.0
        self.offset = 0.0
        self.spectrum = None
        self.spectrum_at = 0.0

    def gain_db_for(self, index):
        g = self.src.gains()
        if not g:
            return 0.0
        return g[max(0, min(int(index), len(g) - 1))] if index is not None else g[len(g) // 2]

    def start(self):
        snap = self.s.snapshot()
        self.build(snap)
        self.src.configure(self.chain.rate, self.s.ppm, self.s.bias_t)
        with self.s.lock:
            if self.s.gain_db is None:
                self.s.gain_db = self.gain_db_for(self.s.gain_index)
        self.src.set_gain(self.s.gain_db)
        self.retune(self.s.snapshot(), force=True)
        self.version = self.s.snapshot()["version"]
        threading.Thread(target=self.reader, name="reader", daemon=True).start()
        threading.Thread(target=self.dsp, name="dsp", daemon=True).start()

    def build(self, snap):
        self.chain = Chain(snap["mode"], snap["bandwidth"], snap["agc_attack"],
                           snap["agc_decay"])

    def rf(self, snap):
        return snap["dial"] - snap["cal"] + snap["upconverter"]

    def retune(self, snap, force=False):
        rf = self.rf(snap)
        off = rf - (self.src.center or 0.0)
        lo = self.MIN_OFF if self.chain.mode != "WFM" else 150000.0
        hi = self.MAX_OFF if self.chain.mode != "WFM" else 400000.0
        if force or self.src.center is None or not lo <= abs(off) <= hi:
            self.src.set_center(rf - self.chain.offset)
        self.offset = rf - self.src.center

    def block_len(self):
        return int(self.chain.rate // 10) // 256 * 256  # ~100 ms, 512-byte multiple

    def reader(self):
        while not self.stop.is_set():
            try:
                x = self.src.read(self.block_len())
            except EOFError:
                self.q.put(None)
                return
            except Exception as e:  # dongle unplugged and the like
                self.error = str(e)
                log("read failed: %s" % e)
                self.q.put(None)
                return
            if not getattr(self.src, "paced", True):
                self.q.put(x)
                continue
            try:
                self.q.put_nowait(x)
            except queue.Full:
                self.dropped += 1

    def dsp(self):
        while not self.stop.is_set():
            x = self.q.get()
            if x is None:
                self.stop.set()
                return
            snap = self.s.snapshot()
            if snap["version"] != self.version:
                rebuild = (snap["mode"] != self.chain.mode or
                           snap["bandwidth"] != self.chain.bandwidth)
                if rebuild:
                    self.build(snap)
                    self.src.set_rate(self.chain.rate)
                    while not self.q.empty():  # blocks read at the old rate
                        try:
                            self.q.get_nowait()
                        except queue.Empty:
                            break
                if snap["gain_db"] is None:  # a reloaded profile: index -> dB
                    with self.s.lock:
                        self.s.gain_db = self.gain_db_for(self.s.gain_index)
                    snap = self.s.snapshot()
                self.src.set_gain(snap["gain_db"])
                self.retune(snap, force=rebuild)
                self.version = snap["version"]
                if rebuild:
                    continue  # the block in hand was at the old rate
            t0 = time.thread_time()
            audio = self.chain(x, self.offset)
            pcm = np.clip(audio * snap["volume"] * 32767, -32768, 32767).astype("<i2")
            self.sink.write(pcm)
            dt = time.thread_time() - t0
            self.cpu += (dt / (len(x) / self.chain.rate) - self.cpu) * 0.05
            self.blocks += 1
            now = time.monotonic()
            if now - self.spectrum_at >= self.SPECTRUM_EVERY:
                self.spectrum_at = now
                self.spectrum = self.make_spectrum(x, snap)

    SPECTRUM_EVERY = 0.2   # seconds: about 5 a second for the clients
    SPECTRUM_BINS = 512

    def make_spectrum(self, x, snap):
        """The whole captured band, averaged over a few FFTs, on the dial's scale."""
        n = 2 * self.SPECTRUM_BINS
        segs = min(len(x) // n, 4)
        if not segs:
            return self.spectrum
        w = np.hanning(n).astype(np.float32)
        p = (np.abs(np.fft.fft(x[: segs * n].reshape(segs, n) * w, axis=1)) ** 2).mean(axis=0)
        p = np.fft.fftshift(p) / (n * float(np.sum(w ** 2)) / n)
        p = p.reshape(self.SPECTRUM_BINS, -1).mean(axis=1)
        db = 10 * np.log10(p + 1e-12)
        rate = self.chain.rate
        # RF -> dial scale: take off the upconverter, put back the calibration.
        to_dial = -snap["upconverter"] + snap["cal"]
        center = self.src.center + to_dial
        dial, bw, mode = snap["dial"], snap["bandwidth"], snap["mode"]
        if mode == "USB":
            band = [dial + 200, dial + 200 + bw]
        elif mode == "LSB":
            band = [dial - 200 - bw, dial - 200]
        else:
            band = [dial - bw / 2, dial + bw / 2]
        return {"bins": [round(float(v), 1) for v in db], "start_hz": center - rate / 2,
                "stop_hz": center + rate / 2, "dial_hz": dial, "passband": band,
                "seq": (self.spectrum or {}).get("seq", 0) + 1}

    def status(self):
        snap = self.s.snapshot()
        return {"profile": self.s.name, "freq": snap["dial"],
                "vfo_hz": snap["dial"] - snap["cal"], "cal_hz": snap["cal"],
                "mode": snap["mode"], "bandwidth": snap["bandwidth"],
                "gain_db": snap["gain_db"], "volume": snap["volume"],
                "offset_mode": snap["offset_mode"], "upconverter_hz": snap["upconverter"],
                "upconvert_below_hz": snap["upconvert_below"],
                "level_dbfs": round(self.chain.level_db, 1) if self.chain else None,
                "tuner_hz": self.src.center, "offset_hz": self.offset,
                "sample_rate": self.chain.rate if self.chain else None,
                "dsp_cpu": round(self.cpu, 3), "blocks": self.blocks,
                "dropped": self.dropped, "error": self.error,
                "running": not self.stop.is_set(), "gains": self.src.gains()}


# ----------------------------------------------------------- Hamlib rigctl

# Hamlib mode bits (rig.h): AM 0x1, USB 0x4, LSB 0x8, FM 0x20, WFM 0x40.
MODE_BITS = 0x1 | 0x4 | 0x8 | 0x20 | 0x40

DUMP_STATE = "\n".join([
    "1", "2", "0",
    # rx range: start end modes low_power high_power vfo ant
    "100000.000000 1700000000.000000 0x%x -1 -1 0x1 0x1" % MODE_BITS,
    "0 0 0 0 0 0 0",
    "0 0 0 0 0 0 0",          # no tx ranges: receive only
    "0x%x 1" % MODE_BITS,     # tuning step 1 Hz
    "0 0",
    "0xc 2800", "0xc 2400", "0xc 3000", "0x1 10000", "0x20 12500",
    "0x40 150000", "0 0",
    "0", "0", "0", "0",       # max rit, xit, if shift, announces
    "",                       # preamps
    "",                       # attenuators
    "0x0", "0x0",             # has get/set func
    "0x40000000", "0x0",      # get level: STRENGTH; set level: none
    "0x0", "0x0",             # get/set parm
    "vfo_ops=0x0", "ptt_type=0x0", "targetable_vfo=0x0",
    "has_set_vfo=0", "has_get_vfo=1", "has_set_freq=1", "has_get_freq=1",
    "has_set_conf=0", "has_get_conf=0", "has_power2mW=0", "has_mW2power=0",
    "timeout=0", "rig_model=2", "rigctld_version=rxd 0.1",
    "done", ""])


def rigctl_reply(cmd, rx):
    """One rigctl command line -> the reply text, or None to hang up."""
    parts = cmd.strip().split()
    if not parts:
        return ""
    head, args = parts[0], parts[1:]
    ok, nimpl, einval = "RPRT 0\n", "RPRT -11\n", "RPRT -1\n"
    s = rx.s
    if head in ("q", "Q", "\\quit"):
        return None
    if head in ("\\chk_vfo", "chk_vfo"):
        return "0\n"
    if head in ("\\dump_state", "dump_state"):
        return DUMP_STATE
    if head in ("f", "\\get_freq"):
        return "%d\n" % round(s.snapshot()["dial"])
    if head in ("F", "\\set_freq"):
        try:
            s.update(freq=float(args[0]))
        except (IndexError, ValueError):
            return einval
        return ok
    if head in ("m", "\\get_mode"):
        snap = s.snapshot()
        return "%s\n%d\n" % (snap["mode"], snap["bandwidth"])
    if head in ("M", "\\set_mode"):
        try:
            bw = float(args[1]) if len(args) > 1 and float(args[1]) > 0 else None
            # JS8Call asks for PKTUSB; data modes are plain SSB here.
            s.update(mode=args[0].upper().replace("PKT", ""), bandwidth=bw)
        except (IndexError, ValueError):
            return einval
        return ok
    if head in ("v", "\\get_vfo"):
        return "VFOA\n"
    if head in ("V", "\\set_vfo"):
        return ok
    if head in ("t", "\\get_ptt"):
        return "0\n"
    if head in ("T", "\\set_ptt"):
        # Receive only. Accept "off" so a client releasing PTT is not an error.
        return ok if args and args[0] == "0" else nimpl
    if head in ("s", "\\get_split_vfo"):
        return "0\nVFOA\n"
    if head in ("S", "\\set_split_vfo"):
        return ok if args and args[0] == "0" else nimpl
    if head == "\\get_powerstat":
        return "1\n"
    if head in ("l", "\\get_level"):
        if args and args[0] == "STRENGTH":
            # dB relative to S9 (-73 dBm). Uncalibrated: dBFS + 73 is only a guess.
            return "%d\n" % round(rx.chain.level_db + 73)
        return nimpl
    return nimpl


class RigctlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        rx = self.server.rx
        for raw in self.rfile:
            line = raw.decode(errors="replace")
            # Several commands may share a line ("f\nm\n" arrives either way).
            reply = rigctl_reply(line, rx)
            if reply is None:
                return
            self.wfile.write(reply.encode())


class RigctlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------- HTTP

# The browser UI: $RXD_WEB, where setup.sh installs it, or beside this file in
# a checkout. Read on every request so a reinstall needs no restart.
WEB_PATHS = (os.environ.get("RXD_WEB", ""), "/usr/local/share/rxd/index.html",
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html"))


def web_page():
    for path in WEB_PATHS:
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
    return None


class Api(BaseHTTPRequestHandler):
    server_version = "rxd/0.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, doc):
        body = (json.dumps(doc) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            return self.send(200, self.server.rx.status())
        if path == "/api/spectrum":
            spec = self.server.rx.spectrum
            return self.send(200, spec) if spec else self.send(503, {"error": "no spectrum yet"})
        if path in ("/", "/index.html"):
            page = web_page()
            if page is None:
                return self.send(404, {"error": "no web UI at " + ", ".join(p for p in WEB_PATHS if p)})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/tune":
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            doc = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(doc, dict):
                raise ValueError("body must be a JSON object")
            unknown = set(doc) - {"freq", "mode", "bandwidth", "gain_db", "volume"}
            if unknown:
                raise ValueError("unknown field(s): " + ", ".join(sorted(unknown)))
            self.server.rx.s.update(**doc)
        except (ValueError, TypeError, AttributeError) as e:
            return self.send(400, {"error": str(e)})
        self.send(200, self.server.rx.status())


# ---------------------------------------------------------------------- main

def hostport(text, default_port):
    host, _, port = text.rpartition(":")
    return (host or "127.0.0.1", int(port or default_port))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="rxd", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile")
    ap.add_argument("--iq", help="cu8 file, - for stdin, or synth (default: the dongle)")
    ap.add_argument("--iq-center", type=float,
                    help="tuner frequency the IQ file was recorded at")
    ap.add_argument("--audio", default="pulse",
                    help="pulse, pulse:SINK, stdout or null (default: the profile's sink)")
    # Defaults from the environment, so the systemd unit's EnvironmentFile
    # (/etc/rx/rxd.env) can open HTTP to the LAN without editing the unit.
    ap.add_argument("--rigctl", default=os.environ.get("RXD_RIGCTL", "127.0.0.1:4532"),
                    help="HOST:PORT, or off (default $RXD_RIGCTL or 127.0.0.1:4532)")
    ap.add_argument("--http", default=os.environ.get("RXD_HTTP", "127.0.0.1:8073"),
                    help="HOST:PORT, or off (default $RXD_HTTP or 127.0.0.1:8073)")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    p, path = load_profile(a.profile)
    try:
        settings = Settings(p, os.path.basename(path)[:-5])
    except ValueError as e:
        sys.exit("rxd: " + str(e))
    snap = settings.snapshot()

    if a.iq == "synth":
        src = SynthSource(snap["dial"] - snap["cal"] + snap["upconverter"] + 1500)
    elif a.iq:
        src = FileSource(a.iq, a.iq_center)
    else:
        src = RtlSource(settings.serial)

    if a.audio == "stdout":
        sink = StdoutSink()
    elif a.audio == "null":
        sink = NullSink()
    elif a.audio.startswith("pulse"):
        sink = PulseSink(a.audio.partition(":")[2] or settings.sink)
    else:
        sys.exit("rxd: --audio must be pulse, pulse:SINK, stdout or null")

    rx = Receiver(settings, src, sink)
    rx.start()
    servers = []
    if a.rigctl != "off":
        rs = RigctlServer(hostport(a.rigctl, 4532), RigctlHandler)
        rs.rx = rx
        servers.append(rs)
    if a.http != "off":
        hs = ThreadingHTTPServer(hostport(a.http, 8073), Api)
        hs.daemon_threads = True
        hs.rx = rx
        servers.append(hs)
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def reload(*_):
        try:
            settings.apply_profile(load_profile(path)[0])
            log("reloaded %s" % path)
        except (OSError, ValueError, KeyError) as e:
            log("reload failed: %s" % e)

    signal.signal(signal.SIGHUP, reload)
    signal.signal(signal.SIGTERM, lambda *_: rx.stop.set())
    st = rx.status()
    log("%s %.4f MHz %s, tuner %.4f MHz, rigctl %s, http %s"
        % (os.path.basename(path), st["freq"] / 1e6, st["mode"],
           (st["tuner_hz"] or 0) / 1e6, a.rigctl, a.http))
    try:
        while not rx.stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        rx.stop.set()
    for srv in servers:
        srv.shutdown()
    src.close()
    sink.close()
    return 1 if rx.error else 0


if __name__ == "__main__":
    sys.exit(main())
