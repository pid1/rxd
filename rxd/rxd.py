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

A WSPR profile ("decoder": "wspr") also decodes WSPR with wsprd and reports
the spots to WSPRnet; GET /api/wspr lists them.

Frequencies on both ports are the dial, as JS8Call means it. A JS8 profile
whose VFO sits off its js8call_dial (7077900 vs 7078000, tuned by hand)
keeps that difference as a calibration, so the dial still reads 7078000.

SIGHUP re-reads the profile; SIGINT or SIGTERM stops.
"""
import argparse
import collections
import configparser
import ctypes
import ctypes.util
import json
import os
import queue
import re
import signal
import socket
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
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
DECODERS = ("js8", "wspr", None)
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
            # SDR++'s squelchEnabled/squelchLevel, carried in demod_settings.
            self.squelch = float(ds["squelchLevel"]) if ds.get("squelchEnabled") and \
                ds.get("squelchLevel") is not None else None
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
            self.js8 = p.get("js8call_dial") is not None
            # What decodes this profile's audio: JS8Call (any profile with a
            # js8call_dial), rxd's own WSPR decoder, or nothing.
            self.decoder = p.get("decoder") or ("js8" if self.js8 else None)
            if self.decoder not in DECODERS:
                raise ValueError("decoder must be one of " + ", ".join(d for d in DECODERS if d))
            self.wspr_report = bool(p.get("wspr_report", True))
            self.version = getattr(self, "version", 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(mode=self.mode, bandwidth=self.bandwidth, dial=self.dial,
                        cal=self.cal, upconverter=self.upconverter(),
                        offset_mode=self.offset_mode,
                        upconvert_below=self.upconvert_below,
                        agc_attack=self.agc_attack, agc_decay=self.agc_decay,
                        volume=self.volume, gain_db=self.gain_db, squelch=self.squelch,
                        wspr_report=self.wspr_report, version=self.version)

    def upconverter(self):
        """The offset in effect for the current dial (call with the lock held)."""
        if self.offset_mode == "Ham-It-Up":
            return self.upconverter_hz
        if self.offset_mode == "auto" and self.dial < self.upconvert_below:
            return self.upconverter_hz
        return 0.0

    def update(self, freq=None, mode=None, bandwidth=None, gain_db=None,
               volume=None, squelch=Ellipsis, wspr_report=None):
        with self.lock:
            if wspr_report is not None:
                if not isinstance(wspr_report, bool):
                    raise ValueError("wspr_report must be true or false")
                self.wspr_report = wspr_report
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
            if squelch is not Ellipsis:  # None (JSON null) turns it off
                if squelch is not None:
                    squelch = float(squelch)
                    if not -150 <= squelch <= 0:
                        raise ValueError("squelch must be in dBFS, -150 to 0, or null")
                self.squelch = squelch
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
            self.level_db = float(10 * np.log10(p + 1e-12))
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
        self.squelch_open = True
        self.listeners = []          # (rate, queue) per streaming client
        self.listen_lock = threading.Lock()
        self.resamplers = {}         # rate -> FirDecimator from 48 kHz
        self.wspr = None             # a WsprDecoder, for a WSPR profile
        self.bands = None            # the BandPlanner, on a live rxd

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
            if self.wspr is not None:  # before squelch and volume
                self.wspr.push(audio, snap["dial"])
            audio = self.gate(audio, snap["squelch"])
            pcm = np.clip(audio * snap["volume"] * 32767, -32768, 32767).astype("<i2")
            self.sink.write(pcm)
            if self.listeners:
                self.stream(audio)
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

    def gate(self, audio, level):
        """Squelch: silence below level dBFS; opens at it, closes 3 dB under."""
        if level is None:
            self.squelch_open = True
            return audio
        lv = self.chain.level_db
        # bool(): lv is numpy's, and a numpy bool_ in status breaks json.dumps.
        self.squelch_open = bool(lv >= (level - 3 if self.squelch_open else level))
        return audio if self.squelch_open else np.zeros_like(audio)

    # ------------------------------------------------ audio to the clients

    STREAM_RATES = (12000, 24000, 48000)
    MAX_LISTENERS = 4

    def listen(self, rate):
        """A queue of s16le chunks at rate, or None if too many are listening."""
        q = queue.Queue(maxsize=30)  # about 3 s; a slow client loses chunks, never the DSP
        with self.listen_lock:
            if len(self.listeners) >= self.MAX_LISTENERS:
                return None
            self.listeners.append((rate, q))
        return q

    def unlisten(self, q):
        with self.listen_lock:
            self.listeners = [(r, x) for r, x in self.listeners if x is not q]

    def stream(self, audio):
        # Before the uConsole's own volume, so muting its speaker doesn't
        # silence the phone; the client has its own volume.
        with self.listen_lock:
            listeners = list(self.listeners)
        chunks = {}
        for rate, q in listeners:
            if rate not in chunks:
                d = AUDIO_RATE // rate
                if d > 1:
                    if rate not in self.resamplers:
                        self.resamplers[rate] = FirDecimator(
                            sps.firwin(16 * d + 1, rate * 0.45, fs=AUDIO_RATE), d)
                    a = self.resamplers[rate](audio)
                else:
                    a = audio
                chunks[rate] = np.clip(a * 0.8 * 32767, -32768, 32767).astype("<i2").tobytes()
            try:
                q.put_nowait(chunks[rate])
            except queue.Full:
                pass

    def status(self):
        snap = self.s.snapshot()
        return {"profile": self.s.name, "freq": snap["dial"],
                "vfo_hz": snap["dial"] - snap["cal"], "cal_hz": snap["cal"],
                "mode": snap["mode"], "bandwidth": snap["bandwidth"],
                "gain_db": snap["gain_db"], "volume": snap["volume"],
                "squelch": snap["squelch"], "squelch_open": self.squelch_open,
                "offset_mode": snap["offset_mode"], "upconverter_hz": snap["upconverter"],
                "upconvert_below_hz": snap["upconvert_below"],
                "level_dbfs": round(self.chain.level_db, 1) if self.chain else None,
                "tuner_hz": self.src.center, "offset_hz": self.offset,
                "sample_rate": self.chain.rate if self.chain else None,
                "dsp_cpu": round(self.cpu, 3), "blocks": self.blocks,
                "dropped": self.dropped, "error": self.error,
                "running": not self.stop.is_set(), "gains": self.src.gains(),
                "js8": self.s.js8, "js8_call": js8_mycall() if self.s.js8 else None,
                "decoder": self.s.decoder,
                "wspr": self.wspr.status() if self.wspr is not None else None,
                "clock": clock_status(),
                "band_plan": self.bands.status() if self.bands is not None else None,
                "listeners": len(self.listeners)}


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

# ------------------------------------------------------- profiles via radio

RADIO = os.environ.get("RXD_RADIO") or shutil.which("radio") or "/usr/local/bin/radio"
_profiles = {"at": 0.0, "doc": None}


def list_profiles():
    """radio's profiles and the active one ({"active", "profiles"}), cached briefly."""
    if _profiles["doc"] is not None and time.monotonic() - _profiles["at"] < 5:
        return _profiles["doc"]
    out = subprocess.run([RADIO, "--json"], capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise OSError((out.stderr or out.stdout).strip() or "radio --json failed")
    _profiles.update(at=time.monotonic(), doc=json.loads(out.stdout))
    return _profiles["doc"]


def spawn_switch(name):
    """Run `radio NAME` outside this unit: it stops this very rxd to start the next.

    systemd-run --scope puts radio (and any JS8Call it starts) in a scope of
    its own, so neither dies with rxd@OLD when radio stops it.
    """
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")  # radio drives JS8Call's window through i3
    argv = [RADIO, "--no-focus", name]
    if shutil.which("systemd-run"):
        argv = ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                "--unit=radio-switch-%d" % int(time.time() * 1000)] + argv
    subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    _profiles["doc"] = None


# ------------------------------------------------------------------ clock

_clock = {"at": 0.0, "doc": None}


def parse_tracking(text):
    """chronyc -n tracking -> {"synced", "source", "offset_s"}.

    JS8's 15 s slots tolerate about 2 s of error and fail silently past it,
    so "synced" means chrony is locked (leap status Normal) and within 1 s.
    """
    def field(name):
        m = re.search(r"^%s\s*:\s*(.*)$" % re.escape(name), text, re.M)
        return m.group(1).strip() if m else None
    ref, leap, sys_t = field("Reference ID"), field("Leap status"), field("System time")
    offset = None
    if sys_t:
        m = re.match(r"([0-9.]+) seconds (fast|slow)", sys_t)
        if m:
            offset = float(m.group(1)) * (1 if m.group(2) == "fast" else -1)
    name = re.search(r"\((.*)\)", ref or "")
    name = name.group(1) if name else None
    if name in ("PPS", "NMEA", "GPS", "GPS1", "SHM0", "SOCK"):
        source = "GPS"
    elif name and name not in ("", "0.0.0.0"):
        source = "network"
    else:
        source = None
    synced = leap == "Normal" and offset is not None and abs(offset) < 1.0
    return {"synced": synced, "source": source if synced or source else None,
            "offset_s": offset}


def clock_status():
    """The system clock as JS8 sees it; synced None when chrony can't say."""
    if _clock["doc"] is not None and time.monotonic() - _clock["at"] < 10:
        return _clock["doc"]
    try:
        out = subprocess.run(["chronyc", "-n", "tracking"], capture_output=True,
                             text=True, timeout=5)
        doc = parse_tracking(out.stdout) if out.returncode == 0 else \
            {"synced": False, "source": None, "offset_s": None}
    except (OSError, subprocess.SubprocessError):
        doc = {"synced": None, "source": None, "offset_s": None}
    _clock.update(at=time.monotonic(), doc=doc)
    return doc


# ----------------------------------------------------------- JS8Call's log

JS8_LOG = os.path.expanduser(os.environ.get("RXD_JS8_LOG",
                                            "~/.local/share/JS8Call/DIRECTED.TXT"))
JS8_INI = os.path.expanduser("~/.config/JS8Call.ini")


def js8_mycall():
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",),
                                   comment_prefixes=())
    cp.optionxform = str
    try:
        cp.read(JS8_INI, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    for sec in ("Configuration", "Common"):
        if cp.has_option(sec, "MyCall"):
            return cp.get(sec, "MyCall").strip() or None
    return None


# A callsign: a 1-3 character prefix, a digit, a 1-4 letter suffix, and an
# optional /P-style part. Not "INFO", "HELLO" or a group like "@HB".
CALLSIGN = re.compile(r"^(?:[A-Z0-9]{1,4}/)?[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(?:/[A-Z0-9]{1,4})?$")


class Js8Feed:
    """New lines of DIRECTED.TXT, parsed and numbered, read as they're asked for."""

    KEEP = 500

    def __init__(self, path=None):
        self.path = path or JS8_LOG
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=self.KEEP)
        self.seq = 0
        self.pos = None
        self.inode = None
        self.partial = b""

    @staticmethod
    def parse(raw):
        f = raw.rstrip("\n").split("\t")
        if len(f) < 5:
            return None
        text = f[4].replace("\u2662", "").strip()  # JS8Call's end-of-message diamond
        frm, _, body = text.partition(":")
        words = body.split()
        to = words[0] if words and CALLSIGN.match(words[0]) else None
        try:
            freq = float(f[1]) * 1e6 if float(f[1]) < 1000 else float(f[1])
            off, snr = int(f[2]), int(f[3])
        except ValueError:
            return None
        return {"time": f[0], "freq": freq, "offset": off, "snr": snr,
                "from": frm.strip() or None, "to": to, "text": text}

    def refresh(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return
        if self.inode != st.st_ino or (self.pos or 0) > st.st_size:
            # First read, or logrotate moved it: start over, from the last 64 KB.
            self.inode, self.partial = st.st_ino, b""
            self.pos = max(st.st_size - 65536, 0)
            skip_first = self.pos > 0
        else:
            skip_first = False
        if st.st_size == self.pos:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            data = self.partial + fh.read()
            self.pos = fh.tell()
        rows = data.split(b"\n")
        self.partial = rows.pop()
        if skip_first and rows:
            rows.pop(0)
        for r in rows:
            line = self.parse(r.decode("utf-8", "replace"))
            if line:
                self.seq += 1
                line["seq"] = self.seq
                self.lines.append(line)

    def since(self, after):
        with self.lock:
            self.refresh()
            if after <= 0:
                out = list(self.lines)[-200:]
            else:
                out = [x for x in self.lines if x["seq"] > after]
            return {"seq": self.seq, "lines": out}


# --------------------------------------------------------------------- WSPR

# rxd decodes WSPR itself rather than through WSJT-X: it records each
# two-minute slot from its own audio, before the volume and squelch, and runs
# wsprd (WSJT-X's command-line decoder, from the wsjtx package) on it. Spots
# go to the feed at /api/wspr and, when reporting is on, to WSPRnet.
WSPRD = os.environ.get("RXD_WSPRD") or shutil.which("wsprd") or "/usr/bin/wsprd"
WSPR_DIR = os.path.expanduser(os.environ.get("RXD_WSPR_DIR", "~/.local/share/rxd/wspr"))
# WSJT-X's own upload endpoint (Network/wsprnet.cpp): one form POST per spot.
WSPRNET_URL = os.environ.get("RXD_WSPRNET", "http://wsprnet.org/post/")
RXD_VERSION = "rxd-0.2"
GRID = re.compile(r"^[A-Ra-r]{2}[0-9]{2}(?:[A-Xa-x]{2})?$")


def grid_latlon(grid):
    """A Maidenhead locator's centre as (lat, lon), or None."""
    if not grid or not GRID.match(grid):
        return None
    g = grid.upper()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * (2 / 24) + 1 / 24
        lat += (ord(g[5]) - 65) * (1 / 24) + 1 / 48
    else:
        lon, lat = lon + 1, lat + 0.5
    return lat, lon


def grid_km(a, b):
    pa, pb = grid_latlon(a), grid_latlon(b)
    if not pa or not pb:
        return None
    la1, lo1, la2, lo2 = map(np.radians, (pa[0], pa[1], pb[0], pb[1]))
    h = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    return int(round(float(2 * 6371 * np.arcsin(np.sqrt(h)))))


def station_id():
    """(call, grid) to report as: $RXD_CALL/$RXD_GRID, else JS8Call's MyCall/MyGrid."""
    call = os.environ.get("RXD_CALL") or js8_mycall()
    grid = os.environ.get("RXD_GRID")
    if not grid:
        cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",),
                                       comment_prefixes=())
        cp.optionxform = str
        try:
            cp.read(JS8_INI, encoding="utf-8")
        except (OSError, configparser.Error):
            pass
        for sec in ("Configuration", "Common"):
            if cp.has_option(sec, "MyGrid"):
                grid = cp.get(sec, "MyGrid").strip()
                break
    call = call.strip().upper() if call else None
    if grid and GRID.match(grid.strip()):
        g = grid.strip()
        grid = g[:4].upper() + g[4:].lower()
    else:
        grid = None
    return call or None, grid


# wspr_spots.txt, which wsprd rewrites each run: date time sync snr dt freq
# message drift cycles jitter. WSJT-X parses it with this same expression.
SPOT_LINE = re.compile(r"^(\d{6})\s+(\d{4})\s+(-?\d+)\s+([+-]?\d+)\s+([+-]?\d+\.\d+)\s+"
                       r"(\d+\.\d+)\s+(\S.*\S)\s+([+-]?\d+)\s+([+-]?\d+)\s+([+-]?\d+)\s*$")
# ALL_WSPR.TXT, wsprd's running log: date time snr dt freq message drift ...
ALL_LINE = re.compile(r"^(\d{6})\s+(\d{4})\s+([+-]?\d+)\s+([+-]?\d+\.\d+)\s+(\d+\.\d+)\s+"
                      r"(\S.*?\S)\s+([+-]?\d+)\s+[+-]?\d+\.\d+\s")


def parse_wspr_message(msg):
    """'K1ABC FN42 37', 'PJ4/K1ABC 37' or '<PJ4/K1ABC> FK52UD 37' -> (call, grid, dbm)."""
    w = msg.split()
    try:
        dbm = int(w[-1])
    except (ValueError, IndexError):
        return None
    if len(w) == 3 and GRID.match(w[1]):
        call, grid = w[0].strip("<>"), w[1]
    elif len(w) == 2:
        call, grid = w[0].strip("<>"), None
    else:
        return None
    if not call or call == "...":
        return None
    return call, grid, dbm


def spot_from(date, hhmm, snr, dt, mhz, msg, drift, mygrid=None):
    parsed = parse_wspr_message(msg)
    if not parsed:
        return None
    call, grid, dbm = parsed
    if grid:  # DM33vq, as WSPRnet and everyone else writes it
        grid = grid[:4].upper() + grid[4:].lower()
    iso = "20%s-%s-%sT%s:%s:00Z" % (date[:2], date[2:4], date[4:6], hhmm[:2], hhmm[2:])
    return {"time": iso, "snr": int(snr), "dt": float(dt), "freq": round(float(mhz) * 1e6),
            "drift": int(drift), "call": call, "grid": grid, "dbm": dbm,
            "km": grid_km(mygrid, grid) if grid else None, "text": msg.strip(),
            "date": date, "hhmm": hhmm, "mhz": mhz}


class WsprNet:
    """Uploads spots to WSPRnet the way WSJT-X does: one form POST each, and a
    'wsprstat' heartbeat for a slot with none, so the station shows as on air.

    A spot that can't be sent (no internet) waits and goes with the next slot;
    one the server answers without "spot(s) added" is dropped, not retried.
    """

    KEEP = 2000  # about a day of a busy band, offline

    def __init__(self, call, grid, url=None, post=None):
        self.call, self.grid = call, grid
        self.url = url or WSPRNET_URL
        self.post = post or self._post
        self.pending = collections.deque(maxlen=self.KEEP)
        self.sent = 0
        self.error = None
        self.online = None   # None until the first upload says either way

    def _post(self, fields):
        import urllib.request
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(4096).decode("utf-8", "replace")

    def queue_slot(self, spots, dial_hz):
        rqrg = "%.6f" % (dial_hz / 1e6)
        common = {"version": RXD_VERSION, "rcall": self.call, "rgrid": self.grid,
                  "rqrg": rqrg, "mode": "2"}
        n = 0
        for s in spots:
            # WSJT-X's guard against reporting a spot off the band it's on.
            if abs(float(s["mhz"]) - dial_hz / 1e6) >= 0.01:
                continue
            f = {"function": "wspr", "date": s["date"], "time": s["hhmm"],
                 "sig": str(s["snr"]), "dt": "%.1f" % s["dt"], "drift": str(s["drift"]),
                 "tqrg": s["mhz"], "tcall": s["call"], "tgrid": s["grid"] or "",
                 "dbm": str(s["dbm"])}
            f.update(common)
            self.pending.append(f)
            n += 1
        return n

    def flush(self, dial_hz, had_spots):
        """Send what's waiting (and a heartbeat for an empty slot)."""
        if not had_spots:
            beat = {"function": "wsprstat", "rcall": self.call, "rgrid": self.grid,
                    "rqrg": "%.6f" % (dial_hz / 1e6), "tpct": "0",
                    "tqrg": "%.6f" % (dial_hz / 1e6), "dbm": "0",
                    "version": RXD_VERSION, "mode": "2"}
            try:
                self.post(beat)
                self.online, self.error = True, None
            except OSError as e:
                self.online, self.error = False, "can't reach WSPRnet: %s" % e
        while self.pending:
            f = self.pending[0]
            try:
                code, text = self.post(f)
            except OSError as e:
                self.online, self.error = False, "can't reach WSPRnet: %s" % e
                return
            self.online = True
            self.pending.popleft()
            if code == 200 and "spot(s) added" in text:
                self.sent += 1
                self.error = None
            else:
                self.error = "WSPRnet refused a spot: %s" % (text.strip()[:120] or code)
            time.sleep(0.2)  # WSJT-X's pace

    def status(self):
        return {"enabled": True, "call": self.call, "grid": self.grid, "sent": self.sent,
                "pending": len(self.pending), "online": self.online, "error": self.error}


class WsprDecoder:
    """Two-minute slots of the receiver's audio -> wsprd -> spots.

    A slot starts on an even UTC minute and a transmission 1 s into it; wsprd
    reads 114 s at 12 kHz. A slot is joined at most 2 s late (padded with
    silence) and dropped if the dial moves during it.
    """

    RATE = 12000
    SLOT = 120
    SAMPLES = 114 * 12000
    LATE = 2.0
    KEEP = 1000

    def __init__(self, settings, datadir=None, wsprd=None, reporter=None, clock=time.time):
        self.s = settings
        self.dir = datadir or WSPR_DIR
        os.makedirs(self.dir, exist_ok=True)
        self.wsprd = wsprd or WSPRD
        self.reporter = reporter
        self.clock = clock
        self.resample = FirDecimator(sps.firwin(129, 5000, fs=AUDIO_RATE).astype(np.float32),
                                     AUDIO_RATE // self.RATE)
        self.chunks, self.n, self.slot, self.dial = [], 0, None, None
        self.jobs = queue.Queue(maxsize=2)
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=self.KEEP)
        self.seq = 0
        self.decoding = None   # the slot being decoded
        self.last = None       # {"slot", "spots"} for the last decoded slot
        self.error = None
        self.call, self.grid = station_id()
        self.load_history()
        threading.Thread(target=self.worker, name="wspr", daemon=True).start()

    def iso(self, t):
        return time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(t))

    # -------------------------------------------------------- recording

    def push(self, audio, dial, now=None):
        """A block of 48 kHz audio that ended at now (Unix time)."""
        a = self.resample(audio)
        now = self.clock() if now is None else now
        t0 = now - len(a) / self.RATE
        if self.slot is not None and (dial != self.dial or t0 > self.slot + self.SLOT):
            self.slot = None  # retuned, or a gap (the dongle stalled)
        if self.slot is None:
            slot = (t0 // self.SLOT) * self.SLOT
            late = t0 - slot
            if late > self.LATE:
                return
            self.slot, self.dial = slot, dial
            pad = int(round(late * self.RATE))
            self.chunks, self.n = [np.zeros(pad, np.float32)], pad
        self.chunks.append(a)
        self.n += len(a)
        if self.n >= self.SAMPLES:
            x = np.concatenate(self.chunks)[: self.SAMPLES]
            try:
                self.jobs.put_nowait((self.slot, self.dial, x))
            except queue.Full:
                self.error = "decoding is falling behind; a slot was skipped"
                log(self.error)
            self.slot, self.chunks, self.n = None, [], 0

    # --------------------------------------------------------- decoding

    def worker(self):
        while True:
            slot, dial, x = self.jobs.get()
            self.decoding = slot
            try:
                spots = self.decode(slot, dial, x)
                self.error = None
            except (OSError, subprocess.SubprocessError, RuntimeError) as e:
                self.error = "wsprd failed: %s" % e
                log(self.error)
                spots = None
            finally:
                self.decoding = None
            if spots is None:
                continue
            with self.lock:
                for sp in spots:
                    self.seq += 1
                    self.lines.append(dict(sp, seq=self.seq))
                self.last = {"slot": self.iso(slot), "spots": len(spots)}
            if self.reporter and self.s.snapshot().get("wspr_report"):
                self.reporter.queue_slot(spots, dial)
                self.reporter.flush(dial, bool(spots))

    def decode(self, slot, dial, x):
        import wave
        name = time.strftime("%y%m%d_%H%M", time.gmtime(slot))
        wav = os.path.join(self.dir, name + ".wav")
        # The audio is AGC'd to about 0.5 peak; wsprd wants 16-bit PCM.
        pcm = np.clip(x * 16000, -32768, 32767).astype("<i2")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(pcm.tobytes())
        spots_file = os.path.join(self.dir, "wspr_spots.txt")
        try:
            os.remove(spots_file)
        except OSError:
            pass
        argv = [self.wsprd, "-a", self.dir, "-f", "%.6f" % (dial / 1e6), wav]
        if shutil.which("nice"):
            argv = ["nice", "-n", "10"] + argv
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=110,
                                 cwd=self.dir)
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass
        if "<DecodeFinished>" not in out.stdout:
            raise RuntimeError((out.stderr or out.stdout).strip()[-200:] or
                               "exit %d" % out.returncode)
        spots = []
        try:
            with open(spots_file, encoding="utf-8", errors="replace") as fh:
                for row in fh:
                    m = SPOT_LINE.match(row.rstrip("\n"))
                    if m:
                        sp = spot_from(m[1], m[2], m[4], m[5], m[6], m[7], m[8], self.grid)
                        if sp:
                            spots.append(sp)
        except FileNotFoundError:
            pass  # no spots this slot
        return spots

    def load_history(self):
        """The tail of ALL_WSPR.TXT, so a restart doesn't empty the feed."""
        path = os.path.join(self.dir, "ALL_WSPR.TXT")
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(size - 131072, 0))
                rows = fh.read().decode("utf-8", "replace").split("\n")
        except OSError:
            return
        if size > 131072:
            rows = rows[1:]
        for row in rows:
            m = ALL_LINE.match(row)
            if m:
                sp = spot_from(m[1], m[2], m[3], m[4], m[5], m[6], m[7], self.grid)
                if sp:
                    self.seq += 1
                    self.lines.append(dict(sp, seq=self.seq))

    # ----------------------------------------------------------- status

    def since(self, after):
        with self.lock:
            out = list(self.lines)[-200:] if after <= 0 else \
                [x for x in self.lines if x["seq"] > after]
            return {"seq": self.seq, "lines": out}

    def status(self):
        now = self.clock()
        if self.decoding is not None:
            state, slot = "decoding", self.decoding
        elif self.slot is not None:
            state, slot = "recording", self.slot
        else:
            state, slot = "waiting", (now // self.SLOT + 1) * self.SLOT
        snap = self.s.snapshot()
        if self.reporter is None:
            report = {"enabled": False, "call": self.call, "grid": self.grid,
                      "reason": "no callsign and grid: set MyCall and MyGrid in JS8Call, "
                                "or RXD_CALL and RXD_GRID in /etc/rx/rxd.env"}
        else:
            report = self.reporter.status()
            report["enabled"] = bool(snap.get("wspr_report"))
        return {"state": state, "slot": self.iso(slot),
                "decoding": self.iso(self.decoding) if self.decoding is not None else None,
                "last": self.last, "error": self.error, "report": report}


# ---------------------------------------------------------------- band plan

# With the band plan on Auto, rxd picks the JS8 or WSPR band itself: from the
# day's band conditions (N0NBH's, from hamqsl.com, or NOAA SWPC's indices
# when hamqsl can't be had), the sun at the station, and what rxd has heard
# on each band at this hour. With no conditions (no internet) it scans the
# bands it has not yet heard at this hour, a couple of WSPR slots or a few
# minutes of JS8 each, and learns. Band changes go through `radio NAME`, which
# restarts rxd, so the planner keeps everything on disk and picks up where it
# was in the next rxd:
#   ~/.config/rx/bands.json             its settings, as the UI sets them
#   ~/.local/share/rxd/bands.json       what was heard, per profile and UTC hour
#   ~/.local/share/rxd/bandplan.json    where it is: scanning or holding, and why
#   ~/.local/share/rxd/conditions.json  the last band conditions fetched
BANDS_CONF = os.path.expanduser(os.environ.get("RXD_BANDS_CONF", "~/.config/rx/bands.json"))
BANDS_DIR = os.path.expanduser(os.environ.get("RXD_BANDS_DIR", "~/.local/share/rxd"))
HAMQSL_URL = os.environ.get("RXD_HAMQSL", "https://www.hamqsl.com/solarxml.php")
SWPC_FLUX_URL = os.environ.get("RXD_SWPC_FLUX",
                               "https://services.swpc.noaa.gov/json/f107_cm_flux.json")
SWPC_KP_URL = os.environ.get("RXD_SWPC_KP",
                             "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json")
HF_BANDS = ("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m")
BAND_ORDER = ("2190m", "630m") + HF_BANDS + ("6m", "2m")
PLAN_MODES = ("wspr", "js8")
# N0NBH's calculated conditions come in four groups of HF bands.
HAMQSL_GROUP = {"160m": "80m-40m", "80m": "80m-40m", "60m": "80m-40m", "40m": "80m-40m",
                "30m": "30m-20m", "20m": "30m-20m", "17m": "17m-15m", "15m": "17m-15m",
                "12m": "12m-10m", "10m": "12m-10m"}
COND_VALUE = {"good": 1.0, "fair": 0.6, "poor": 0.2}
# What a mid-latitude station usually hears, by the sun: low bands at night,
# high bands by day, the middle ones around the gray line. 0 to 1.
BAND_PRIOR = {
    "day": {"2190m": .05, "630m": .1, "160m": .05, "80m": .25, "60m": .45, "40m": .6,
            "30m": .8, "20m": 1.0, "17m": .9, "15m": .8, "12m": .6, "10m": .5,
            "6m": .15, "2m": .05},
    "gray": {"2190m": .3, "630m": .4, "160m": .5, "80m": .75, "60m": .75, "40m": .9,
             "30m": .85, "20m": .85, "17m": .6, "15m": .45, "12m": .3, "10m": .25,
             "6m": .1, "2m": .05},
    "night": {"2190m": .5, "630m": .7, "160m": .7, "80m": .9, "60m": .8, "40m": 1.0,
              "30m": .8, "20m": .45, "17m": .25, "15m": .15, "12m": .1, "10m": .08,
              "6m": .05, "2m": .05},
}
PART_WORDS = {"day": "day", "night": "night", "gray": "gray line"}
# What counts as a lot heard: WSPR spots per two-minute slot, JS8 decodes a minute.
HEARD_SCALE = {"wspr": 10.0, "js8": 2.0}
# How much listening makes one good look at a band: two WSPR slots, six minutes of JS8.
HEARD_PER = {"wspr": 2.0, "js8": 6.0}
PLAN_DEFAULTS = {"plan": "auto", "mode": "wspr", "bands": None, "dwell_min": 6,
                 "every_min": 30, "cross_side": False}
CREDITS = {"hamqsl": "Band conditions courtesy of N0NBH, www.hamqsl.com",
           "swpc": "Solar indices from NOAA SWPC; band conditions estimated by rxd"}


def band_label(band):
    """"20m" -> "20 m"."""
    return band[:-1] + " m" if band and band.endswith("m") and not band.endswith("cm") else \
        (band or "?")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)


def http_get(url, timeout=10.0, limit=262144):
    import urllib.request  # here, not at the top: only the band plan fetches
    req = urllib.request.Request(url, headers={"User-Agent": RXD_VERSION + " (band plan)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


# ------------------------------------------------------- band conditions

def _num(text):
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


def parse_hamqsl(xml):
    """N0NBH's solarxml.php -> {"source", "updated", "sfi", "a", "k", "bands", "vhf", ...}.

    "bands" is {"80m-40m": {"day": "Fair", "night": "Good"}, ...}, as they
    calculate it; "vhf" is their E-skip and aurora words by location."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    sd = root.find("solardata") if root.tag == "solar" else root
    if sd is None:
        raise ValueError("no solardata in the hamqsl XML")

    def text(tag):
        el = sd.find(tag)
        return el.text.strip() if el is not None and el.text else ""
    bands = {}
    for b in sd.iter("band"):
        name, when = b.get("name"), (b.get("time") or "").lower()
        if name and when in ("day", "night") and b.text:
            bands.setdefault(name, {})[when] = b.text.strip()
    vhf = {}
    for ph in sd.iter("phenomenon"):
        if ph.text:
            vhf["%s %s" % (ph.get("name"), ph.get("location"))] = ph.text.strip()
    sfi = _num(text("solarflux"))
    if sfi is None or not bands:
        raise ValueError("the hamqsl XML has no solar flux or band conditions")
    k = _num(text("kindex"))
    a = _num(text("aindex"))
    return {"source": "hamqsl", "updated": text("updated"), "sfi": int(sfi),
            "a": int(a) if a is not None else None, "k": int(k) if k is not None else None,
            "bands": bands, "vhf": vhf, "geomag": text("geomagfield") or None,
            "noise": text("signalnoise") or None}


def model_conditions(sfi, k):
    """Day and night words for hamqsl's four HF groups from SFI and K alone: a
    rough stand-in for N0NBH's calculation, used when only NOAA answers."""
    hi = min(max((sfi - 65) / 100.0, 0.0), 1.0)      # SFI 65 -> 0, 165 -> 1
    storm = min(max(((k or 0) - 3) / 4.0, 0.0), 1.0)  # K 3 -> 0, K 7 -> 1

    def word(v):
        v *= 1 - 0.7 * storm
        return "Good" if v >= 0.7 else "Fair" if v >= 0.4 else "Poor"
    day = {"80m-40m": .55, "30m-20m": .6 + .4 * hi, "17m-15m": .25 + .7 * hi,
           "12m-10m": .1 + .8 * hi}
    night = {"80m-40m": .85, "30m-20m": .4 + .4 * hi, "17m-15m": .1 + .5 * hi,
             "12m-10m": .05 + .3 * hi}
    return {g: {"day": word(day[g]), "night": word(night[g])} for g in day}


def parse_swpc(flux_json, kp_json):
    """NOAA SWPC's 10.7 cm flux and planetary K JSON -> the same shape as parse_hamqsl."""
    flux = json.loads(flux_json)
    rows = [r for r in flux if isinstance(r, dict) and r.get("flux") is not None]
    if not rows:
        raise ValueError("no solar flux in SWPC's JSON")
    latest = max(rows, key=lambda r: r.get("time_tag") or "")
    sfi = float(latest["flux"])
    kp = json.loads(kp_json)
    k = a = None
    if kp and isinstance(kp[0], list):  # the older shape: a header row, then rows
        head = kp[0]
        last = kp[-1]
        k = float(last[head.index("Kp")]) if "Kp" in head else None
        a = float(last[head.index("a_running")]) if "a_running" in head else None
    elif kp:
        last = max((r for r in kp if isinstance(r, dict)), key=lambda r: r.get("time_tag") or "")
        k, a = last.get("Kp"), last.get("a_running")
    k = int(round(float(k))) if k is not None else None
    return {"source": "swpc", "updated": latest.get("time_tag"), "sfi": int(round(sfi)),
            "a": int(a) if a is not None else None, "k": k,
            "bands": model_conditions(sfi, k), "vhf": {}, "geomag": None, "noise": None}


class Conditions:
    """Band conditions, fetched at most every 45 minutes and kept on disk, so
    the rxd that starts after each band switch doesn't fetch them again."""

    MAX_AGE = 45 * 60    # fetch again after this
    RETRY = 15 * 60      # after a failure, wait this long
    STALE = 3 * 3600     # older than this, they're no guide

    def __init__(self, path, fetch=None, clock=time.time):
        self.path = path
        self.fetch = fetch or http_get
        self.clock = clock
        self.doc = read_json(path, {}) or {}

    def due(self, now=None):
        now = self.clock() if now is None else now
        d = self.doc
        return now - (d.get("fetched") or 0) >= self.MAX_AGE and \
            now - (d.get("tried") or 0) >= self.RETRY

    def refresh(self, now=None):
        """Fetch them (blocking, up to about 20 s); True if they came."""
        now = self.clock() if now is None else now
        errors = []
        cond = None
        try:
            cond = parse_hamqsl(self.fetch(HAMQSL_URL))
        except Exception as e:  # noqa: BLE001 - any failure falls back to NOAA
            errors.append("hamqsl: %s" % (getattr(e, "reason", None) or e))
        if cond is None:
            try:
                cond = parse_swpc(self.fetch(SWPC_FLUX_URL), self.fetch(SWPC_KP_URL))
            except Exception as e:  # noqa: BLE001
                errors.append("NOAA: %s" % (getattr(e, "reason", None) or e))
        d = dict(self.doc, tried=now)
        if cond is not None:
            d.update(fetched=now, cond=cond, error=None)
        else:
            d["error"] = "; ".join(str(x) for x in errors)[:200]
            log("band conditions: %s" % d["error"])
        self.doc = d
        try:
            write_json(self.path, d)
        except OSError as e:
            log("can't keep band conditions in %s: %s" % (self.path, e))
        return cond is not None

    def current(self, now=None):
        """The conditions if they're fresh enough to steer by, else None."""
        now = self.clock() if now is None else now
        if self.doc.get("cond") and now - (self.doc.get("fetched") or 0) < self.STALE:
            return self.doc["cond"]
        return None

    def error(self):
        return self.doc.get("error")


# ------------------------------------------------------------- the sun

def sun_elevation(lat, lon, t):
    """The sun's elevation in degrees at (lat, lon) at Unix time t; good to a
    fraction of a degree, which is plenty for day, night and gray line."""
    import math
    d = t / 86400.0 + 2440587.5 - 2451545.0  # days since J2000
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians(gmst * 15 + lon) - ra
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(dec) +
                                  math.cos(la) * math.cos(dec) * math.cos(ha)))


def day_part(t, latlon=None):
    """"day", "gray" (the sun within 6 degrees of the horizon) or "night".
    With no location, this machine's local clock stands in for the sun."""
    if latlon:
        e = sun_elevation(latlon[0], latlon[1], t)
        return "day" if e > 6 else "night" if e < -6 else "gray"
    h = time.localtime(t).tm_hour
    return "day" if 8 <= h < 17 else "gray" if h in (6, 7, 17, 18) else "night"


def sun_times(t, latlon):
    """(sunrise, sunset) Unix times on t's local day, either None if it doesn't
    happen (or no location)."""
    if not latlon:
        return None, None
    lt = time.localtime(t)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    rise = sset = None
    prev = sun_elevation(latlon[0], latlon[1], start)
    for m in range(5, 24 * 60 + 1, 5):
        e = sun_elevation(latlon[0], latlon[1], start + m * 60)
        if prev < -0.833 <= e and rise is None:
            rise = start + m * 60
        if prev >= -0.833 > e and sset is None:
            sset = start + m * 60
        prev = e
    return rise, sset


# ----------------------------------------------------- scoring a band

def cond_value(band, cond, part):
    """0 to 1: how good the conditions say band is now, or None if they don't say."""
    if not cond:
        return None
    if band in HAMQSL_GROUP:
        g = (cond.get("bands") or {}).get(HAMQSL_GROUP[band]) or {}
        vals = [COND_VALUE.get((g.get(w) or "").lower()) for w in
                (("day", "night") if part == "gray" else (part,))]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        v = sum(vals) / len(vals)
        if band == "160m" and part == "day":
            v *= 0.3  # the 80-40 group says little about 160 m by day
        return v
    vhf = " ".join((cond.get("vhf") or {}).values()).lower()
    if band == "6m":
        if "50mhz" in vhf or "6m es" in vhf:
            return 1.0
        sfi = cond.get("sfi") or 0
        return 0.6 if part == "day" and sfi >= 180 else 0.1
    if band == "2m":
        return 1.0 if "144mhz" in vhf else 0.05
    return None  # 2190 m and 630 m: the sun and what's heard are the guide


def band_score(band, part, cond, heard):
    """(score 0..1, what it rests on: "conditions", "heard" or "usual").

    heard is (how much was heard 0..1, how many good looks that rests on).
    The conditions and the usual pattern for the time of day make the base;
    what rxd has actually heard here at this hour soon outweighs them, since
    hearing the most is the point: 72 % after one good look, up to 90 %."""
    prior = BAND_PRIOR[part].get(band, 0.1)
    c = cond_value(band, cond, part)
    base = prior if c is None else 0.4 * prior + 0.6 * c
    h, n = heard or (0.0, 0.0)
    w = 0.9 * n / (n + 0.25) if n > 0 else 0.0
    return round((1 - w) * base + w * h, 4), ("conditions" if c is not None else "usual"), w


class BandHistory:
    """What rxd heard on each profile, by UTC hour, kept on disk.

    A sample is one visit's worth in one hour: units (WSPR slots decoded, or
    minutes of JS8), count (spots or decodes), calls (distinct stations) and
    the median signal level. Old samples count for less: half every 14 days."""

    KEEP_DAYS = 60
    PER_HOUR = 40
    HALF_LIFE = 14 * 86400.0

    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        doc = read_json(path, {}) or {}
        self.bands = doc.get("bands") if isinstance(doc.get("bands"), dict) else {}

    def save(self):
        try:
            write_json(self.path, {"version": 1, "bands": self.bands})
        except OSError as e:
            log("can't keep the band history in %s: %s" % (self.path, e))

    def add(self, name, t, units, count, calls, visit, level=None):
        """Add to the sample for this visit and hour (made if it's new)."""
        hour = "%02d" % time.gmtime(t).tm_hour
        rows = self.bands.setdefault(name, {}).setdefault(hour, [])
        s = rows[-1] if rows and rows[-1].get("v") == visit else None
        if s is None:
            s = {"t": round(t), "v": visit, "units": 0, "count": 0, "calls": 0}
            rows.append(s)
        s["units"] += units
        s["count"] += count
        s["calls"] += calls
        s["t"] = round(t)
        if level is not None:
            s["level"] = round(level, 1)
        cut = self.clock() - self.KEEP_DAYS * 86400
        rows[:] = [x for x in rows if x.get("t", 0) >= cut][-self.PER_HOUR:]
        self.save()

    def heard(self, name, t, scale, per=1.0):
        """(0..1, good looks it rests on, rate) for name around t's UTC hour;
        per is how many units make one good look."""
        hour = time.gmtime(t).tm_hour
        now = self.clock()
        wsum = rsum = 0.0
        for dh, wh in ((0, 1.0), (-1, 0.5), (1, 0.5)):
            for s in (self.bands.get(name) or {}).get("%02d" % ((hour + dh) % 24), []):
                if not s.get("units"):
                    continue
                w = wh * 0.5 ** (max(0.0, now - s.get("t", now)) / self.HALF_LIFE) * \
                    min(s["units"] / per, 3.0)
                rate = (s.get("count", 0) + s.get("calls", 0)) / 2.0 / s["units"]
                wsum += w
                rsum += w * rate
        if wsum <= 0:
            return 0.0, 0.0, None
        import math
        rate = rsum / wsum
        return 1 - math.exp(-rate / scale), wsum, rate


def js8_decoding(rx):
    """Whether something is decoding JS8 for rxd: rxd's own decoder when it has
    one (rx.js8_decoder with a status() saying "running"), else JS8Call."""
    dec = getattr(rx, "js8_decoder", None)
    if dec is not None and hasattr(dec, "status"):
        try:
            return bool(dec.status().get("running", True))
        except Exception:  # noqa: BLE001
            return False
    try:
        return subprocess.run(["pgrep", "-x", "js8call"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class BandPlanner:
    """Auto: choose and hop bands; Manual: stay put. Runs in rxd, one tick
    every few seconds, and keeps its state on disk across rxd's restarts."""

    TICK = 5.0
    SCAN_SLOTS = 2          # WSPR slots a band needs, scanning
    SETTLE = 90.0           # a switch asked for this recently is still under way
    STAY = 0.85             # stay unless another band scores this much better
    RESCAN = 20 * 3600.0    # a band scanned at this hour this recently counts as heard
    ROWS_EVERY = 60.0       # how often radio's profile list is re-read

    def __init__(self, rx, js8=None, conf_path=None, data_dir=None, profiles=None,
                 switch=None, conditions=None, clock=time.time, latlon=Ellipsis,
                 synced=None, js8_alive=None):
        self.rx = rx
        self.js8 = js8
        self.conf_path = conf_path or BANDS_CONF
        d = data_dir or BANDS_DIR
        self.state_path = os.path.join(d, "bandplan.json")
        self.profiles = profiles or list_profiles
        self.switch_fn = switch or spawn_switch
        self.clock = clock
        self.cond = conditions or Conditions(os.path.join(d, "conditions.json"), clock=clock)
        self.history = BandHistory(os.path.join(d, "bands.json"), clock=clock)
        if latlon is Ellipsis:
            latlon = grid_latlon(station_id()[1])
        self.latlon = latlon
        self.synced = synced or (lambda: clock_status().get("synced") is not False)
        self.js8_alive = js8_alive or (lambda: js8_decoding(rx))
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.conf = self.load_conf()
        self.state = read_json(self.state_path, None)
        self.fresh = self.state is None
        self.state = self.state if isinstance(self.state, dict) else {}
        self.rows, self.rows_at, self.rows_error = None, -1e18, None
        self.tried_at = -1e18
        self.scores = {}
        self.doc = None
        now = clock()
        w = rx.wspr
        self.visit = {"name": rx.s.name, "t": now, "id": int(now), "slots": 0, "units": 0.0,
                      "count": 0, "levels": [], "wspr_last": w.last if w else None,
                      "wspr_seq": w.seq if w else 0, "js8_seq": None, "js8_at": now}
        self.arrive(now)

    # ----------------------------------------------------- settings, state

    def load_conf(self):
        c = dict(PLAN_DEFAULTS)
        doc = read_json(self.conf_path, {})
        if isinstance(doc, dict):
            try:
                c.update(self.check(doc))
            except ValueError as e:
                log("ignoring %s: %s" % (self.conf_path, e))
        return c

    @staticmethod
    def check(doc):
        """The valid settings in doc; ValueError for one that isn't."""
        out = {}
        for k, v in doc.items():
            if k == "plan":
                if v not in ("auto", "manual"):
                    raise ValueError("plan must be auto or manual")
            elif k == "mode":
                if v not in PLAN_MODES:
                    raise ValueError("mode must be wspr or js8")
            elif k == "bands":
                if v is not None and (not isinstance(v, list) or
                                      any(b not in BAND_ORDER for b in v)):
                    raise ValueError("bands must be null (the default) or a list of: " +
                                     ", ".join(BAND_ORDER))
                v = None if v is None else [b for b in BAND_ORDER if b in v]
            elif k == "dwell_min":
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 2 <= v <= 60:
                    raise ValueError("dwell_min must be 2 to 60 minutes")
            elif k == "every_min":
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 10 <= v <= 240:
                    raise ValueError("every_min must be 10 to 240 minutes")
            elif k == "cross_side":
                if not isinstance(v, bool):
                    raise ValueError("cross_side must be true or false")
            else:
                continue  # scan and anything unknown are not settings
            out[k] = v
        return out

    def save_conf(self):
        try:
            write_json(self.conf_path, self.conf)
        except OSError as e:
            log("can't save the band plan's settings in %s: %s" % (self.conf_path, e))

    def save_state(self):
        try:
            write_json(self.state_path, self.state)
        except OSError as e:
            log("can't keep the band plan's state in %s: %s" % (self.state_path, e))

    def arrive(self, now):
        """A new rxd on rx.s.name: carry on if the plan sent it here, or go
        Manual if someone switched to it by hand (radio NAME, a picker)."""
        name = self.rx.s.name
        st = self.state
        if not self.fresh and self.conf["plan"] == "auto" and \
                name not in (st.get("on"), st.get("requested")):
            self.conf["plan"] = "manual"
            self.save_conf()
            st["reason"] = "Manual: %s was chosen outside the band plan" % name
            log("band plan: Manual, since %s was switched to by hand" % name)
        st["on"], st["requested"], st["requested_at"] = name, None, None
        self.save_state()

    # ------------------------------------------------------------- inputs

    def profile_rows(self, now, force=False):
        if force or self.rows is None or now - self.rows_at >= self.ROWS_EVERY:
            try:
                self.rows = self.profiles()["profiles"]
                self.rows_error = None
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
                self.rows_error = str(e)
            self.rows_at = now
        return self.rows or []

    def side_up(self):
        """True if the Ham It Up is (as far as rxd knows) on UPCONVERT: the side
        the running profile needs, which is how Win last set the switch."""
        with self.rx.s.lock:
            return self.rx.s.upconverter() > 0

    def enabled(self, up):
        b = self.conf.get("bands")
        if b is None:  # the default: every band on the switch's current side
            return list(HF_BANDS) if up else ["6m", "2m"]
        return list(b)

    def plan_rows(self, now, mode=None):
        mode = mode or self.conf["mode"]
        return [r for r in self.profile_rows(now)
                if r.get("plan") and r.get("decoder") == mode and r.get("band")]

    def candidates(self, now):
        """(the profiles Auto may pick from, {band: why not} for the others)."""
        up = self.side_up()
        en = self.enabled(up)
        out, why = [], {}
        for r in self.plan_rows(now):
            if r["band"] not in en:
                why[r["band"]] = "not enabled"
            elif bool(r.get("upconvert")) != up and not self.conf["cross_side"]:
                why[r["band"]] = "needs the Ham It Up on %s" % (
                    "UPCONVERT" if r.get("upconvert") else "BYPASS")
            else:
                out.append(r)
        return out, why

    def current_row(self, now):
        name = self.rx.s.name
        return next((r for r in self.profile_rows(now) if r.get("name") == name), None)

    # ------------------------------------------------------------ learning

    def sample(self, now):
        """Count what was heard on this band since the last tick."""
        v = self.visit
        row = self.current_row(now)
        if not row or not row.get("band") or not row.get("plan"):
            return
        snap = self.rx.s.snapshot()
        if abs(snap["dial"] - float(row.get("freq") or 0)) > 5000:
            return  # retuned by hand within the profile: not this band's dial
        chain = self.rx.chain
        if chain is not None and snap.get("gain_db") is not None:
            v["levels"].append(chain.level_db)
            del v["levels"][:-240]
        if not self.synced():
            return  # with the clock off nothing decodes; that says nothing of the band
        w = self.rx.wspr
        if row.get("decoder") == "wspr" and w is not None:
            last = w.last
            if last and last != v["wspr_last"]:
                v["wspr_last"] = last
                got = w.since(v["wspr_seq"])
                v["wspr_seq"] = got["seq"]
                calls = {x.get("call") for x in got["lines"] if x.get("call")}
                try:
                    import calendar
                    t = calendar.timegm(time.strptime(last["slot"], "%Y-%m-%dT%H:%M:00Z"))
                except (ValueError, KeyError, TypeError):
                    t = now
                v["slots"] += 1
                v["units"] += 1
                v["count"] += int(last.get("spots") or 0)
                self.history.add(row["name"], t, 1, int(last.get("spots") or 0), len(calls),
                                 v["id"], self.level())
        elif row.get("decoder") == "js8" and self.js8 is not None:
            if v["js8_seq"] is None:
                v["js8_seq"] = self.js8.since(0)["seq"]
                v["js8_at"] = now
                return
            if now - v["js8_at"] < 60:
                return
            minutes = (now - v["js8_at"]) / 60.0
            got = self.js8.since(v["js8_seq"])
            v["js8_seq"], v["js8_at"] = got["seq"], now
            if not self.js8_alive():
                return
            dial = snap["dial"]
            lines = [x for x in got["lines"]
                     if abs(float(x.get("freq") or 0) - dial) < 3000]
            calls = {x.get("from") for x in lines if x.get("from")}
            v["units"] += minutes
            v["count"] += len(lines)
            self.history.add(row["name"], now, round(minutes, 2), len(lines), len(calls),
                             v["id"], self.level())

    def level(self):
        lv = sorted(self.visit["levels"])
        return lv[len(lv) // 2] if lv else None

    def heard(self, name, mode, now):
        return self.history.heard(name, now, HEARD_SCALE.get(mode, 3.0),
                                  HEARD_PER.get(mode, 1.0))

    def visit_done(self, now):
        v = self.visit
        dwell = max(float(self.conf["dwell_min"]), 2.0) * 60
        if now - v["t"] < dwell:
            return False
        if self.conf["mode"] == "wspr" and v["slots"] < self.SCAN_SLOTS:
            # no slots at all for long past the dwell: wsprd or the audio is
            # broken here; move on rather than wait forever
            return now - v["t"] > dwell + 6 * 60
        return True

    def quiet(self, now):
        """Not in the middle of decoding a WSPR slot, whose spots a switch would lose."""
        w = self.rx.wspr
        if w is None:
            return True
        if w.decoding is not None or not w.jobs.empty():
            return False
        return w.slot is None or now - w.slot < 60 or self.visit["slots"] == 0

    # ------------------------------------------------------------ deciding

    def part(self, now):
        return day_part(now, self.latlon)

    def score_all(self, rows, now, cond):
        part = self.part(now)
        out = {}
        for r in rows:
            h, n, rate = self.heard(r["name"], r["decoder"], now)
            s, basis, w = band_score(r["band"], part, cond, (h, n))
            out[r["name"]] = {"score": s, "basis": basis, "heard_w": w, "visits": round(n, 2),
                              "rate": round(rate, 2) if rate is not None else None}
        return out

    def because(self, row, sc, cond, now, stay=False, rival=None):
        """Why row is the one, in words. rival: the better-scoring band stayed off."""
        b = band_label(row["band"])
        if rival is not None:
            return "%s: staying; about as good as %s %s" % (
                b, band_label(rival["band"]),
                ("by conditions (SFI %s, K %s, %s)" % (cond.get("sfi"), cond.get("k"),
                                                       PART_WORDS[self.part(now)]))
                if cond else "by what rxd has heard at this hour")
        part = PART_WORDS[self.part(now)]
        unit = "spots a slot" if row["decoder"] == "wspr" else "decodes a minute"
        heard = sc.get("heard_w", 0) > 0 and sc.get("rate") is not None
        if cond:
            idx = "SFI %s, K %s, %s" % (cond.get("sfi"), cond.get("k"), part)
            what = "conditions and what rxd has heard here" if heard else "conditions"
            return "%s: %s by %s (%s)" % (b, "still best" if stay else "best", what, idx)
        if heard:
            return "%s: %s heard at this hour (%.1f %s); no band conditions" % (
                b, "still best" if stay else "best", sc["rate"], unit)
        return "%s: usually best by %s; no band conditions and nothing heard yet" % (b, part)

    def switch(self, row, reason, now):
        st = self.state
        st["reason"] = reason
        if row["name"] == self.rx.s.name:
            self.save_state()
            return
        if st.get("requested") == row["name"] and now - (st.get("requested_at") or 0) < self.SETTLE:
            return  # asked already; radio is on its way
        if now - self.tried_at < 60:
            return
        self.tried_at = now
        st["requested"], st["requested_at"] = row["name"], now
        self.save_state()
        log("band plan: switching to %s: %s" % (row["name"], reason))
        try:
            self.switch_fn(row["name"])
        except OSError as e:
            st["reason"] = "Can't switch to %s: %s" % (row["name"], e)
            self.save_state()

    def start_scan(self, rows, now, why):
        cur = self.rx.s.name
        part = self.part(now)
        order = sorted(rows, key=lambda r: (r["name"] != cur, -BAND_PRIOR[part].get(r["band"], 0)))
        st = self.state
        st.update(phase="scan", scan=[r["name"] for r in order], scan_i=0, scan_why=why,
                  next_eval=None)
        self.scan_step(order[0], now)

    def scan_step(self, row, now):
        st = self.state
        n, i = len(st["scan"]), st["scan_i"]
        self.switch(row, "Scanning: %s, %d of %d (%s)" % (band_label(row["band"]), i + 1, n,
                                                          st.get("scan_why") or "learning"),
                    now)

    def evaluate(self, now, cands, fresh=False):
        """Choose among cands. Staying put wins a near tie (a switch costs a slot),
        except straight after a scan, whose point was to compare them."""
        st = self.state
        cond = self.cond.current(now)
        sc = self.score_all(cands, now, cond)
        self.scores = sc
        if cond is None and self.synced():
            scanned = st.setdefault("scanned", {})
            hour = time.gmtime(now).tm_hour
            unknown = [r for r in cands if sc[r["name"]]["visits"] < 0.5 and
                       now - scanned.get("%s@%02d" % (r["name"], hour), 0) > self.RESCAN]
            if unknown:
                self.start_scan(unknown, now, "no band conditions; learning this hour")
                return
        best = max(cands, key=lambda r: sc[r["name"]]["score"])
        cur = next((r for r in cands if r["name"] == self.rx.s.name), None)
        st.update(phase="hold", scan=None, scan_i=None,
                  next_eval=now + float(self.conf["every_min"]) * 60)
        stay = sc[best["name"]]["score"] * (1.0 if fresh else self.STAY)
        if cur is not None and sc[cur["name"]]["score"] >= stay:
            rival = None if sc[cur["name"]]["score"] >= sc[best["name"]]["score"] else best
            self.switch(cur, self.because(cur, sc[cur["name"]], cond, now, stay=True,
                                          rival=rival), now)
        else:
            self.switch(best, self.because(best, sc[best["name"]], cond, now), now)

    def decide(self, now):
        st = self.state
        cur = self.rx.s.name
        if self.conf["plan"] != "auto":
            st["phase"] = "manual"
            if not (st.get("reason") or "").startswith("Manual"):
                st["reason"] = "Manual: staying on %s" % cur
            return
        if st.get("phase") == "manual":
            st["phase"] = None
            st["reason"] = None
        cands, _ = self.candidates(now)
        if not cands:
            st.update(phase="idle", reason="Auto: no %s bands enabled on the Ham It Up's %s side" % (
                "WSPR" if self.conf["mode"] == "wspr" else "JS8",
                "UPCONVERT" if self.side_up() else "BYPASS") if not self.rows_error else
                "Auto: can't list profiles (%s)" % self.rows_error)
            return
        names = [r["name"] for r in cands]
        if st.get("phase") == "scan" and (st.get("scan_why") or "").startswith("no band") and \
                self.cond.current(now) and self.quiet(now):
            st["phase"] = None  # scanning for want of conditions, and now there are some
        if st.get("phase") == "scan" and st.get("scan"):
            scan = [n for n in st["scan"] if n in names]
            if not scan:
                st["phase"] = None
            else:
                if scan != st["scan"]:
                    i = st.get("scan_i") or 0
                    keep = st["scan"][i] if i < len(st["scan"]) else None
                    st["scan"] = scan
                    st["scan_i"] = scan.index(keep) if keep in scan else min(i, len(scan) - 1)
                i = st["scan_i"]
                target = next(r for r in cands if r["name"] == st["scan"][i])
                if cur != target["name"]:
                    self.scan_step(target, now)
                    return
                if not self.visit_done(now) or not self.quiet(now):
                    v = self.visit
                    extra = " · slot %d of %d" % (min(v["slots"] + 1, self.SCAN_SLOTS),
                                                   self.SCAN_SLOTS) \
                        if self.conf["mode"] == "wspr" else ""
                    st["reason"] = "Scanning: %s, %d of %d (%s)%s" % (
                        band_label(target["band"]), i + 1, len(st["scan"]),
                        st.get("scan_why") or "learning", extra)
                    return
                scanned = st.setdefault("scanned", {})
                scanned["%s@%02d" % (cur, time.gmtime(now).tm_hour)] = now
                for k in [k for k, t in scanned.items() if now - t > self.RESCAN]:
                    del scanned[k]
                if i + 1 < len(st["scan"]):
                    st["scan_i"] = i + 1
                    nxt = next(r for r in cands if r["name"] == st["scan"][i + 1])
                    self.scan_step(nxt, now)
                    return
                st["phase"], st["after_scan"] = None, True  # the scan is done: choose
        due = st.get("phase") != "hold" or cur not in names or \
            now >= (st.get("next_eval") or 0)
        if due and self.quiet(now):
            self.evaluate(now, cands, fresh=bool(st.pop("after_scan", False)))

    # ---------------------------------------------------------- the loop

    def tick(self, now=None):
        now = self.clock() if now is None else now
        if self.cond.due(now):
            self.cond.refresh(now)  # outside the lock: it can take seconds
        with self.lock:
            self.sample(now)
            self.decide(now)
            self.save_state()
            self.doc = self.build_status(now)

    def run(self):
        while not self.rx.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the plan must never take rxd down
                log("band plan: %s: %s" % (type(e).__name__, e))
            self.wake.wait(self.TICK)
            self.wake.clear()

    def start(self):
        threading.Thread(target=self.run, name="bands", daemon=True).start()

    # --------------------------------------------------------------- API

    def configure(self, doc):
        """POST /api/bands: settings, and {"scan": true} to scan now."""
        if not isinstance(doc, dict):
            raise ValueError("body must be a JSON object")
        unknown = set(doc) - set(PLAN_DEFAULTS) - {"scan"}
        if unknown:
            raise ValueError("unknown field(s): " + ", ".join(sorted(unknown)))
        new = self.check(doc)
        if "scan" in doc and not isinstance(doc["scan"], bool):
            raise ValueError("scan must be true or false")
        now = self.clock()
        with self.lock:
            old = dict(self.conf)
            self.conf.update(new)
            if doc.get("scan"):
                self.conf["plan"] = "auto"
            self.save_conf()
            st = self.state
            changed = any(old.get(k) != self.conf.get(k) for k in
                          ("plan", "mode", "bands", "cross_side"))
            if doc.get("scan"):
                cands, _ = self.candidates(now)
                if cands:
                    self.start_scan(cands, now, "asked for")
            elif changed and self.conf["plan"] == "auto":
                st.update(phase=None, scan=None, scan_i=None, next_eval=None)
            elif self.conf["plan"] == "auto" and old.get("every_min") != self.conf["every_min"] \
                    and st.get("phase") == "hold":
                st["next_eval"] = now + float(self.conf["every_min"]) * 60
            if self.conf["plan"] == "manual":
                st["reason"] = "Manual: staying on %s" % self.rx.s.name
            self.save_state()
            self.doc = self.build_status(now)
        self.wake.set()
        return self.describe()

    def manual_pick(self, name):
        """A profile picked by hand (POST /api/profile): the plan goes Manual."""
        with self.lock:
            if self.conf["plan"] == "auto":
                self.conf["plan"] = "manual"
                self.save_conf()
            self.state["reason"] = "Manual: %s picked by hand" % name
            self.state["on"] = name  # so the next rxd doesn't take it for a surprise
            self.save_state()
            self.doc = self.build_status(self.clock())

    def build_status(self, now):
        st = self.state
        cond = self.cond.current(now)
        up = self.side_up()
        row = self.current_row(now)
        scan = None
        if st.get("phase") == "scan" and st.get("scan"):
            i = st.get("scan_i") or 0
            nm = st["scan"][min(i, len(st["scan"]) - 1)]
            r = next((x for x in self.profile_rows(now) if x.get("name") == nm), None)
            scan = {"i": i + 1, "n": len(st["scan"]), "profile": nm,
                    "band": r.get("band") if r else None}
        rise, sset = sun_times(now, self.latlon)
        part = self.part(now)
        return {"plan": self.conf["plan"], "mode": self.conf["mode"],
                "phase": st.get("phase") or ("manual" if self.conf["plan"] != "auto" else "idle"),
                "profile": self.rx.s.name, "band": row.get("band") if row else None,
                "reason": st.get("reason") or ("Auto: choosing a band" if
                                               self.conf["plan"] == "auto" else
                                               "Manual: staying on %s" % self.rx.s.name),
                "scan": scan, "next_at": st.get("next_eval") if st.get("phase") == "hold" else None,
                "switching_to": st.get("requested"),
                "bands": self.enabled(up), "default_bands": self.conf.get("bands") is None,
                "dwell_min": self.conf["dwell_min"], "every_min": self.conf["every_min"],
                "cross_side": self.conf["cross_side"], "side": "UPCONVERT" if up else "BYPASS",
                "sun": {"part": part, "words": PART_WORDS[part], "sunrise": rise, "sunset": sset,
                        "located": bool(self.latlon)},
                "conditions": None if not cond else {
                    "source": cond.get("source"), "sfi": cond.get("sfi"), "a": cond.get("a"),
                    "k": cond.get("k"), "updated": cond.get("updated"),
                    "fetched": self.cond.doc.get("fetched"), "bands": cond.get("bands"),
                    "credit": CREDITS.get(cond.get("source"), "")},
                "conditions_error": None if cond else self.cond.error()}

    def status(self):
        doc = self.doc
        if doc is None:
            with self.lock:
                doc = self.doc = self.build_status(self.clock())
        return doc

    def describe(self):
        """GET /api/bands: settings, status, and every band profile with how it scores."""
        now = self.clock()
        with self.lock:
            cond = self.cond.current(now)
            up = self.side_up()
            en = self.enabled(up)
            rows = []
            for mode in PLAN_MODES:
                prs = self.plan_rows(now, mode)
                sc = self.score_all(prs, now, cond)
                for r in sorted(prs, key=lambda r: BAND_ORDER.index(r["band"])
                                if r["band"] in BAND_ORDER else 99):
                    why = None
                    if r["band"] not in en:
                        why = "not enabled"
                    elif bool(r.get("upconvert")) != up and not self.conf["cross_side"]:
                        why = "needs the Ham It Up on %s" % (
                            "UPCONVERT" if r.get("upconvert") else "BYPASS")
                    s = sc[r["name"]]
                    rows.append({"name": r["name"], "mode": mode, "band": r["band"],
                                 "freq": r.get("freq"), "upconvert": bool(r.get("upconvert")),
                                 "enabled": r["band"] in en, "eligible": why is None,
                                 "why_not": why, "score": s["score"], "basis": s["basis"],
                                 "heard": {"rate": s["rate"], "visits": s["visits"]}})
            return {"config": dict(self.conf), "status": self.build_status(now),
                    "all_bands": list(BAND_ORDER), "profiles": rows}


def make_bands(rx, js8):
    """The band plan for a live rxd, or None (RXD_BANDS=off, or not the dongle)."""
    env = os.environ.get("RXD_BANDS", "")
    if env == "off" or (env != "on" and not isinstance(rx.src, RtlSource)):
        return None
    try:
        bp = BandPlanner(rx, js8)
    except Exception as e:  # noqa: BLE001 - never stop rxd for the plan
        log("band plan off: %s" % e)
        return None
    bp.start()
    return bp


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
        if path == "/api/profiles":
            try:
                return self.send(200, list_profiles())
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                return self.send(503, {"error": "can't list profiles: %s" % e})
        if path == "/api/js8":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                after = int(q.get("after", ["0"])[0])
            except ValueError:
                return self.send(400, {"error": "after must be a number"})
            return self.send(200, self.server.js8.since(after))
        if path == "/api/wspr":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                after = int(q.get("after", ["0"])[0])
            except ValueError:
                return self.send(400, {"error": "after must be a number"})
            wspr = self.server.rx.wspr
            if wspr is None:
                return self.send(200, {"seq": 0, "lines": [], "decoder": self.server.rx.s.decoder})
            return self.send(200, wspr.since(after))
        if path == "/api/audio":
            return self.audio()
        if path == "/api/bands":
            return self.bands_get()
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

    def audio(self):
        rx = self.server.rx
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            rate = int(q.get("rate", ["24000"])[0])
        except ValueError:
            rate = 0
        if rate not in rx.STREAM_RATES:
            return self.send(400, {"error": "rate must be one of %s" % (rx.STREAM_RATES,)})
        chunks = rx.listen(rate)
        # A client that stops reading without closing (a phone off the Wi-Fi)
        # would block the write forever and hold a listener slot: give up.
        self.connection.settimeout(10)
        if chunks is None:
            return self.send(429, {"error": "%d clients are already listening" % rx.MAX_LISTENERS})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Audio-Rate", str(rate))
            self.send_header("X-Audio-Format", "s16le; channels=1")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Expose-Headers", "X-Audio-Rate")
            self.end_headers()
            # HTTP/1.0: the stream simply runs until either side closes it.
            while not rx.stop.is_set():
                try:
                    data = chunks.get(timeout=5)
                except queue.Empty:
                    continue
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            rx.unlisten(chunks)

    def do_POST(self):
        if self.path == "/api/profile":
            return self.switch()
        if self.path == "/api/bands":
            return self.bands_post()
        if self.path != "/api/tune":
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            doc = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(doc, dict):
                raise ValueError("body must be a JSON object")
            unknown = set(doc) - {"freq", "mode", "bandwidth", "gain_db", "volume", "squelch",
                                  "wspr_report"}
            if unknown:
                raise ValueError("unknown field(s): " + ", ".join(sorted(unknown)))
            self.server.rx.s.update(**doc)
        except (ValueError, TypeError, AttributeError) as e:
            return self.send(400, {"error": str(e)})
        self.send(200, self.server.rx.status())

    def switch(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            doc = json.loads(self.rfile.read(n) or b"{}")
            name = doc.get("name") if isinstance(doc, dict) else None
            if not isinstance(name, str):
                raise ValueError('body must be {"name": PROFILE}')
            names = [p["name"] for p in list_profiles()["profiles"]]
            if name not in names:
                raise ValueError("no profile '%s' (have: %s)" % (name, ", ".join(names)))
        except (ValueError, TypeError) as e:
            return self.send(400, {"error": str(e)})
        except (OSError, subprocess.SubprocessError) as e:
            return self.send(503, {"error": "can't list profiles: %s" % e})
        try:
            spawn_switch(name)
        except OSError as e:
            return self.send(503, {"error": "can't run radio: %s" % e})
        log("switching to %s for an HTTP client" % name)
        if self.server.rx.bands is not None:
            self.server.rx.bands.manual_pick(name)
        self.send(202, {"switching": name})

    # the band plan (BandPlanner)
    def bands_get(self):
        bp = self.server.rx.bands
        if bp is None:
            return self.send(503, {"error": "no band plan: this rxd isn't on the radio "
                                            "(or RXD_BANDS=off)"})
        return self.send(200, bp.describe())

    def bands_post(self):
        bp = self.server.rx.bands
        if bp is None:
            return self.send(503, {"error": "no band plan: this rxd isn't on the radio "
                                            "(or RXD_BANDS=off)"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            doc = json.loads(self.rfile.read(n) or b"{}")
            return self.send(200, bp.configure(doc))
        except (ValueError, TypeError) as e:
            return self.send(400, {"error": str(e)})


# ---------------------------------------------------------------------- main

def make_wspr(settings):
    if not os.path.isfile(WSPRD):
        log("no wsprd at %s (sudo apt install wsjtx): WSPR won't decode" % WSPRD)
    call, grid = station_id()
    reporter = WsprNet(call, grid) if call and grid else None
    return WsprDecoder(settings, reporter=reporter)


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
    if settings.decoder == "wspr":
        rx.wspr = make_wspr(settings)
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
        hs.js8 = Js8Feed()
        servers.append(hs)
        rx.bands = make_bands(rx, hs.js8)
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
