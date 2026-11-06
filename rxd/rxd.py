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
        ("gain_db" turns automatic gain off; "gain_auto": true turns it on;
        "audio_auto", "audio_nr", "audio_filter", "audio_norm", "audio_limit"
        and "audio_squelch" switch the listening clean-up)

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
# A JS8 profile's sink is JS8Call's input: silence there means no decodes
# (2026-11-06, a saved volume 0 cost 18 minutes of them). Below this rxd
# refuses a volume; a profile saved with less loads at JS8_VOLUME.
DECODER_MIN_VOLUME = 0.05
JS8_VOLUME = 0.2
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
            # Automatic RF gain (AutoGain), on unless the profile says false;
            # gain_db is then only where it starts.
            self.gain_auto = rtl.get("gain_auto", True) is not False
            self.audio = audio_settings(p.get("audio"))
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
            if self.decoder == "js8" and self.volume < DECODER_MIN_VOLUME:
                log("volume %g would silence JS8Call: using %g" % (self.volume, JS8_VOLUME))
                self.volume = JS8_VOLUME
            self.version = getattr(self, "version", 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(mode=self.mode, bandwidth=self.bandwidth, dial=self.dial,
                        cal=self.cal, upconverter=self.upconverter(),
                        offset_mode=self.offset_mode,
                        upconvert_below=self.upconvert_below,
                        agc_attack=self.agc_attack, agc_decay=self.agc_decay,
                        volume=self.volume, gain_db=self.gain_db, squelch=self.squelch,
                        wspr_report=self.wspr_report, gain_auto=self.gain_auto,
                        audio=dict(self.audio), version=self.version)

    def upconverter(self):
        """The offset in effect for the current dial (call with the lock held)."""
        if self.offset_mode == "Ham-It-Up":
            return self.upconverter_hz
        if self.offset_mode == "auto" and self.dial < self.upconvert_below:
            return self.upconverter_hz
        return 0.0

    def update(self, freq=None, mode=None, bandwidth=None, gain_db=None,
               volume=None, squelch=Ellipsis, wspr_report=None, gain_auto=None, **audio):
        with self.lock:
            if gain_auto is not None and not isinstance(gain_auto, bool):
                raise ValueError("gain_auto must be true or false")
            new_audio = audio_update(self.audio, audio)
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
                if gain_auto is None:  # a gain set by hand turns the automatic off
                    self.gain_auto = False
            if gain_auto is not None:
                self.gain_auto = gain_auto
            self.audio = new_audio
            if volume is not None:
                volume = float(volume)
                if not 0 <= volume <= 4:
                    raise ValueError("volume must be between 0 and 4")
                if self.decoder == "js8" and volume < DECODER_MIN_VOLUME:
                    raise ValueError("JS8Call decodes this audio: volume can't go below %g"
                                     % DECODER_MIN_VOLUME)
                self.volume = volume
            if squelch is not Ellipsis:  # None (JSON null) turns it off
                if squelch is not None:
                    squelch = float(squelch)
                    if not -150 <= squelch <= 0:
                        raise ValueError("squelch must be in dBFS, -150 to 0, or null")
                self.squelch = squelch
            self.version += 1

    def set_auto_gain(self, db):
        """AutoGain's choice, kept unless gain_db was set by hand meanwhile."""
        with self.lock:
            if self.gain_auto:
                self.gain_db = float(db)
            return self.gain_auto


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


# ------------------------------------------------------- automatic RF gain

def db10(p):
    return float(10 * np.log10(p + 1e-12))


class AutoGain:
    """Picks the dongle's gain step from the raw IQ, a block at a time.

    The aim is the best signal-to-noise without overload. With an 8-bit ADC
    that means: enough gain that the band's noise floor (from the spectrum
    rxd already makes) sits well above the ADC's own quantisation noise, so
    the antenna sets the floor and not the ADC, and no more, since more gain
    only spends headroom. Overload shows as clipped samples (bytes 0 or 255)
    or an RMS near full scale; on the uConsole's R820T a crowded band
    compresses (the RMS stops rising with gain) before much clips.

    Measured 2026-11-06 on the box, 820 kHz AM through the Ham It Up: the
    carrier's SNR was best (47 dB) at 3.7-7.7 dB of gain, where the floor is
    7-11 dB over the ADC's, and fell to 34 dB at 30+ dB as the band compressed.

    After a start or a retune it steps once a second until it settles, then
    at most once every SLOW seconds. Under a WSPR or JS8 profile it then moves
    only in the gap between slots, so a decode never sees a step, unless the
    signal overloads or is far too low.
    """

    ADC_FLOOR_DB = -50.0   # 8-bit quantisation noise on make_spectrum's scale
    MARGIN_LO, MARGIN_HI, MARGIN_FAR = 6.0, 13.0, 2.0  # floor over the ADC's, dB
    RMS_HI, RMS_UP, RMS_URGENT = -10.0, -15.0, -5.0    # dBFS
    CLIP_HI, CLIP_UP, CLIP_URGENT = 1e-4, 1e-6, 1e-3   # fraction of I/Q values
    URGENT_DROP = 6.0      # dB: an overload steps down at least this far
    FAST, SLOW = 1.0, 5.0  # seconds between steps while settling / after
    SETTLE_FOR = 30.0      # seconds of fast steps at most
    # The floor doesn't always move dB for dB: on WFM at 93.3 MHz the R820T's
    # floor rose 11 dB for its 0 -> 3.7 dB step, so neither was in the band
    # and the loop flipped between them every second (2026-11-06). A step
    # that would undo the last one within REVERSAL_S is refused, the loop
    # holds the higher gain, and that move waits BAR_S. Overload isn't barred.
    REVERSAL_S, BAR_S = 120.0, 600.0
    # Decoder -> (slot period, gap start, gap end), seconds into the period.
    # WSPR records 114 s from the even minute; a JS8 Normal frame ends 13.1 s
    # into its 15 s slot.
    SLOTS = {"wspr": (120.0, 114.5, 119.5), "js8": (15.0, 13.3, 14.8)}

    def __init__(self, gains, decoder=None):
        self.gains = sorted(set(float(g) for g in gains))
        self.decoder = decoder
        self.key = None          # what it settled for: (mode, tuner MHz)
        self.spec_seq = None     # the last spectrum it took a floor from
        self.steps = 0
        self.state = "settling"
        self.reason = None
        self.last = {}
        self.cpu = 0.0
        self.reset(0.0)

    def reset(self, now):
        """Start settling again (a start, a retune or auto turned back on)."""
        self.t_dec = self.t_short = self.t_start = now
        self.acc = [0, 0, 0.0, []]   # clipped values, samples, sum |x|^2, floors
        self.short = [0, 0, 0.0]
        self.skip = 0
        self.settling = True
        self.holds = 0
        self.state = "settling"
        self.last_step = None    # (from, to, when) of the last non-overload step
        self.barred = {}         # (index, direction) -> until when

    @staticmethod
    def measure(x):
        """(clipped I/Q values, samples, sum of |x|^2) for a block from the dongle."""
        v = x.view(np.float32)
        # cu8 bytes 0 and 255 are -1.0 and +1.0; 1 and 254 are 0.992.
        return int(np.count_nonzero(np.abs(v) > 0.995)), len(x), float(np.dot(v, v))

    @staticmethod
    def floor_of(bins):
        """The band's noise floor: the 20th percentile of the spectrum, without
        the outer tenths (the dongle's filter rolls off) or the DC spike."""
        b = np.asarray(bins, np.float32)
        n = len(b)
        if n < 40:
            return None
        e, mid = n // 10, n // 2
        return float(np.percentile(np.concatenate([b[e:mid - 6], b[mid + 6:n - e]]), 20))

    def index(self, gain):
        g = self.gains
        return int(np.argmin([abs(x - (gain or 0.0)) for x in g]))

    def in_gap(self, wall):
        period, a, b = self.SLOTS[self.decoder]
        return a <= wall % period < b

    def decide(self, clip, rms, margin, i):
        """(-1, 0 or +1, why) for one window of measurements at gain step i."""
        g = self.gains
        down = None
        if clip > self.CLIP_HI:
            down = "clipping"
        elif rms > self.RMS_HI:
            down = "near full scale"
        elif margin is not None and margin > self.MARGIN_HI:
            down = "more gain than the noise needs"
        if down:
            return (-1, down) if i > 0 else (0, down + ", at the lowest gain")
        if margin is not None and margin < self.MARGIN_LO:
            if i + 1 >= len(g):
                return 0, "noise near the ADC's, at the highest gain"
            if clip > self.CLIP_UP or rms + (g[i + 1] - g[i]) > self.RMS_UP:
                return 0, "noise near the ADC's, but no headroom for more"
            return 1, "noise near the ADC's"
        return 0, "the antenna sets the noise floor, with headroom"

    def target(self, i, d, urgent=False):
        g = self.gains
        if d < 0 and urgent:
            lower = [j for j in range(i) if g[j] <= g[i] - self.URGENT_DROP]
            return lower[-1] if lower else 0
        return max(0, min(len(g) - 1, i + d))

    def jump(self, i, d, rms, margin):
        """While settling: the step nearest the middle of the target, at least
        one step in direction d."""
        g = self.gains
        mid = (self.MARGIN_LO + self.MARGIN_HI) / 2
        if d < 0:
            move = max(rms - self.RMS_UP, (margin - mid) if margin is not None else 0.0)
            want = g[i] - 0.75 * move  # the floor seldom falls dB for dB
            j = min(range(i), key=lambda k: abs(g[k] - want)) if i else 0
        else:
            move = min(mid - margin, self.RMS_UP - rms)
            want = g[i] + 0.75 * move
            j = min(range(i + 1, len(g)), key=lambda k: abs(g[k] - want)) \
                if i + 1 < len(g) else i
        return j

    def observe(self, x, gain, now, wall, floor=None):
        """One block of IQ in (with a new noise floor, if there is one); the
        gain to step to, or None to stay where it is."""
        if not self.gains:
            return None
        if self.skip > 0:      # blocks read before the last step
            self.skip -= 1
            return None
        c, n, pw = self.measure(x)
        for a in (self.acc, self.short):
            a[0] += c
            a[1] += n
            a[2] += pw
        if floor is not None:
            self.acc[3].append(floor)
        i = self.index(gain)
        if now - self.t_short >= self.FAST and self.short[1]:
            sc, sn, sp = self.short
            self.short = [0, 0, 0.0]
            self.t_short = now
            clip, rms = sc / (2.0 * sn), db10(sp / sn)
            fl = self.acc[3][-3:]
            margin = float(np.median(fl)) - self.ADC_FLOOR_DB if fl else None
            if (clip > self.CLIP_URGENT or rms > self.RMS_URGENT) and i > 0:
                return self.step(i, self.target(i, -1, urgent=True), now, "overload")
            if not self.settling and margin is not None and margin < self.MARGIN_FAR:
                d, why = self.decide(clip, rms, margin, i)
                if d > 0:
                    return self.step(i, i + 1, now, "noise far too near the ADC's")
        if now - self.t_dec < (self.FAST if self.settling else self.SLOW):
            return None
        cl, n, pw, fl = self.acc
        if not n:
            return None
        clip, rms = cl / (2.0 * n), db10(pw / n)
        floor_db = float(np.median(fl)) if fl else None
        margin = floor_db - self.ADC_FLOOR_DB if floor_db is not None else None
        if not self.settling and self.decoder in self.SLOTS and not self.in_gap(wall):
            d, why = self.decide(clip, rms, margin, i)
            if d:  # keep measuring; step in the gap
                self.state, self.reason = "waiting for the gap between slots", why
            return None
        self.last = {"clip": clip, "rms_dbfs": round(rms, 1),
                     "floor_db": None if floor_db is None else round(floor_db, 1),
                     "margin_db": None if margin is None else round(margin, 1)}
        self.acc = [0, 0, 0.0, []]
        self.t_dec = now
        d, why = self.decide(clip, rms, margin, i)
        self.reason = why
        if d and why not in ("clipping", "near full scale"):
            if self.barred.get((i, d), -1.0) > now:
                d, why = 0, why + "; held, as the step flip-flopped"
                self.reason = why
            elif self.last_step and now - self.last_step[2] < self.REVERSAL_S and \
                    (self.last_step[1] - self.last_step[0]) * d < 0:
                self.barred[(i, d) if d < 0 else (self.last_step[0], -1)] = now + self.BAR_S
                if d < 0:  # stay up here
                    d, why = 0, why + "; held, as the step flip-flopped"
                    self.reason = why
        if d:
            self.holds = 0
            j = self.jump(i, d, rms, margin) if self.settling else i + d
            if why not in ("clipping", "near full scale"):
                self.last_step = (i, j, now)
            return self.step(i, j, now, why)
        self.holds += 1
        if self.settling and (self.holds >= 2 or now - self.t_start > self.SETTLE_FOR):
            self.settling = False
        self.state = "settling" if self.settling else "holding"
        return None

    def step(self, i, j, now, why):
        self.reason = why
        self.acc = [0, 0, 0.0, []]
        self.short = [0, 0, 0.0]
        self.t_dec = self.t_short = now
        self.holds = 0
        if j == i:
            return None
        self.steps += 1
        self.state = "settling" if self.settling else "stepped"
        return self.gains[j]

    def status(self):
        return dict(self.last, state=self.state, reason=self.reason, steps=self.steps,
                    cpu=round(self.cpu, 4))


# ------------------------------------------------------- listening audio

# audio_* in /api/tune and the profile's "audio": {...}. nr is None for "by
# mode": on for voice (AM, FM, USB, LSB), off for WFM, whose music the
# noise estimate would eat into on long notes.
AUDIO_KEYS = ("auto", "nr", "filter", "norm", "limit", "squelch")


def audio_settings(p):
    a = {k: True for k in AUDIO_KEYS}
    a["nr"] = None
    return audio_update(a, {"audio_" + k: v for k, v in (p or {}).items()})


def audio_update(cur, fields):
    out = dict(cur)
    for name, v in fields.items():
        k = name[len("audio_"):] if name.startswith("audio_") else None
        if k not in AUDIO_KEYS:
            raise ValueError("unknown audio setting %s" % name)
        if not (isinstance(v, bool) or (k == "nr" and v is None)):
            raise ValueError("%s must be true or false%s" % (name, " or null" if k == "nr" else ""))
        out[k] = v
    return out


class NoiseReducer:
    """Spectral subtraction on 48 kHz audio: 512-point frames, half overlap.

    The noise is each bin's smoothed power at its quietest, let rise
    RISE_DB_S a second so it follows a changing band; a bin keeps
    1 - OVER * noise / power of itself, but never less than FLOOR. About
    11 ms of delay.
    """

    N, H = 512, 256
    RISE_DB_S = 3.0        # a held note 40 dB over the noise lasts ~13 s untouched
    OVER = 4.0             # the quietest of a smoothed power reads low: make up
    FLOOR = 0.18           # -15 dB
    SMOOTH = 0.2

    def __init__(self):
        n = np.arange(self.N)
        self.win = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * n / self.N)).astype(np.float32)
        self.buf = np.zeros(self.H, np.float32)       # input not yet framed
        self.fifo = np.zeros(self.H, np.float32)      # output not yet given out
        self.tail = np.zeros(self.H, np.float32)      # overlap from the last frame
        self.rise = 10 ** (self.RISE_DB_S * self.H / AUDIO_RATE / 10)
        self.s = None          # smoothed power per bin
        self.noise = None

    def __call__(self, a):
        N, H = self.N, self.H
        data = np.concatenate([self.buf, a.astype(np.float32)])
        m = (len(data) - H) // H
        if m > 0:
            fr = np.lib.stride_tricks.sliding_window_view(data, N)[::H][:m]
            X = np.fft.rfft(fr * self.win, axis=1)
            P = (X.real ** 2 + X.imag ** 2).astype(np.float64)
            if self.s is None:  # start from the typical bin, so a tone isn't "noise"
                self.s = P[0].copy()
                self.noise = np.minimum(P[0], np.median(P[0]))
            S, _ = sps.lfilter([self.SMOOTH], [1, self.SMOOTH - 1], P, axis=0,
                               zi=(1 - self.SMOOTH) * self.s[None, :])
            # noise_k = min(noise_{k-1} * rise, S_k), all frames at once
            r = self.rise ** np.arange(1, m + 1)[:, None]
            runs = np.minimum.accumulate(np.vstack([self.noise[None, :], S / r]), axis=0)[1:]
            Nk = runs * r
            self.s, self.noise = S[-1], Nk[-1]
            G = np.maximum(1 - self.OVER * Nk / (S + 1e-20), self.FLOOR)
            y = np.fft.irfft(X * G, n=N, axis=1).astype(np.float32) * self.win
            out = (np.vstack([self.tail[None, :], y[:-1, H:]]) + y[:, :H]).ravel()
            self.tail = y[-1, H:].copy()
            self.buf = data[m * H:]
            self.fifo = np.concatenate([self.fifo, out])
        else:
            self.buf = data
        n = len(a)
        res, self.fifo = self.fifo[:n], self.fifo[n:]
        if len(res) < n:   # can't happen with the H samples banked first
            res = np.concatenate([res, np.zeros(n - len(res), np.float32)])
        return res


def soft_limit(a, knee=0.6, ceil=0.97):
    """Unchanged below knee; above it, a tanh curve up to ceil."""
    m = np.abs(a) > knee
    if not m.any():
        return a
    y = a.copy()
    v = np.abs(a[m])
    y[m] = np.sign(a[m]) * (knee + (ceil - knee) * np.tanh((v - knee) / (ceil - knee)))
    return y


class AudioFx:
    """Clean-up for listening: never on the audio a decoder gets.

    In order: an automatic noise squelch (NFM), a hum and DC filter (on NFM a
    300-3500 Hz voice band, so sub-audible tones and the discriminator's hiss
    go too), the noise reducer, a loudness normaliser so quiet and loud
    stations come out alike, and a soft limiter.
    """

    # mode -> (high-pass Hz, its order, low-pass Hz or None)
    FILTERS = {"USB": (100.0, 2, None), "LSB": (100.0, 2, None), "AM": (100.0, 4, None),
               "FM": (300.0, 6, 3500.0), "WFM": (30.0, 2, None)}
    TARGET_DB = -20.0                  # RMS the normaliser aims at, dBFS
    GAIN_MIN_DB, GAIN_MAX_DB = -20.0, 12.0  # +12: dead air stays quiet
    NORM_GATE_DB = -60.0               # quieter than this doesn't move it
    SQ_BAND = (8000.0, 16000.0)        # NFM noise squelch measures here
    SQ_OPEN_DB, SQ_CLOSE_DB = 6.0, 3.0  # quieter than pure noise by, to open/close
    SQ_HANG = 3                        # blocks it stays open after the signal goes

    def __init__(self, mode, bandwidth):
        self.mode, self.bandwidth = mode, bandwidth
        hp, order, lp = self.FILTERS[mode]
        sos = sps.butter(order, hp, "highpass", fs=AUDIO_RATE, output="sos")
        if lp:
            sos = np.vstack([sos, sps.butter(6, lp, "lowpass", fs=AUDIO_RATE, output="sos")])
        self.sos = sos
        self.zi = np.zeros((sos.shape[0], 2))
        self.nr = NoiseReducer()
        self.level = None      # the normaliser's loudness estimate, dBFS
        self.gain = 1.0
        self.sq_open, self.sq_hang = True, 0
        self.sq_ref = self.noise_ref(bandwidth) if mode == "FM" else None
        self.sq_quiet = None   # dB quieter than pure noise, last block
        self.active = []
        self.cpu = 0.0

    @classmethod
    def hf_power(cls, a):
        if len(a) < 64:
            return None
        X = np.fft.rfft(a)
        f = np.fft.rfftfreq(len(a), 1.0 / AUDIO_RATE)
        sel = (f >= cls.SQ_BAND[0]) & (f < cls.SQ_BAND[1])
        return float(np.mean(X.real[sel] ** 2 + X.imag[sel] ** 2)) / len(a)

    @classmethod
    def noise_ref(cls, bandwidth):
        """What the NFM discriminator gives for noise alone, in SQ_BAND: it
        depends only on the channel filter, not on the noise's level."""
        rng = np.random.default_rng(5)
        ch = Chain("FM", bandwidth, 0, 0)
        x = (rng.standard_normal(72000) + 1j * rng.standard_normal(72000)).astype(np.complex64)
        a = ch(x * 0.01, 50000.0)
        return cls.hf_power(a[len(a) // 3:])

    def nr_on(self, opt):
        return opt["nr"] if opt["nr"] is not None else self.mode != "WFM"

    def squelch(self, a):
        p = self.hf_power(a)
        if p is None or not self.sq_ref:
            return
        self.sq_quiet = db10(self.sq_ref) - db10(p)
        if self.sq_quiet >= (self.SQ_CLOSE_DB if self.sq_open else self.SQ_OPEN_DB):
            self.sq_open, self.sq_hang = True, self.SQ_HANG
        elif self.sq_hang > 0:
            self.sq_hang -= 1
        else:
            self.sq_open = False

    def normalise(self, a, adapt):
        lv = db10(float(np.mean(a * a))) if len(a) else -120.0
        if adapt and lv > self.NORM_GATE_DB:
            if self.level is None:
                self.level = lv
            self.level += (lv - self.level) * (0.35 if lv > self.level else 0.03)
        g_db = 0.0 if self.level is None else \
            min(max(self.TARGET_DB - self.level, self.GAIN_MIN_DB), self.GAIN_MAX_DB)
        g = 10 ** (g_db / 20)
        ramp = np.linspace(self.gain, g, len(a), endpoint=False, dtype=np.float32) \
            if len(a) else 1.0
        self.gain = g
        return a * ramp

    def __call__(self, a, opt, manual_squelch=False):
        """Audio for the speaker and the phone. opt is Settings.audio."""
        t0 = time.thread_time()
        act = []
        if not opt["auto"]:
            self.sq_open = True
            self.active = act
            return a
        if self.mode == "FM" and opt["squelch"] and not manual_squelch:
            self.squelch(a)
            act.append("squelch")
        else:
            self.sq_open = True
        if opt["filter"]:
            a, self.zi = sps.sosfilt(self.sos, a, zi=self.zi)
            a = a.astype(np.float32)
            act.append("filter")
        if self.nr_on(opt):
            a = self.nr(a)
            act.append("nr")
        if not self.sq_open:
            a = np.zeros_like(a)
        if opt["norm"]:
            a = self.normalise(a, adapt=self.sq_open)
            act.append("norm")
        if opt["limit"]:
            a = soft_limit(a)
            act.append("limit")
        self.active = act
        dt = time.thread_time() - t0
        self.cpu += (dt / (len(a) / AUDIO_RATE) - self.cpu) * 0.05 if len(a) else 0
        return a.astype(np.float32)

    def status(self, opt):
        return {"auto": opt["auto"], "nr": bool(self.nr_on(opt)), "nr_setting": opt["nr"],
                "filter": opt["filter"], "norm": opt["norm"], "limit": opt["limit"],
                "squelch": opt["squelch"], "active": list(self.active),
                "squelch_open": self.sq_open if "squelch" in self.active else None,
                "norm_gain_db": round(db10(self.gain ** 2), 1) if "norm" in self.active else None,
                "cpu": round(self.cpu, 4)}


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
        self.autogain = None         # an AutoGain, once the dongle's gains are known
        self.fx = None               # AudioFx for what's listened to
        self.clock, self.wallclock = time.monotonic, time.time  # tests run on their own

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
        gains = self.src.gains()
        self.autogain = AutoGain(gains, self.s.decoder) if gains else None
        self.retune(self.s.snapshot(), force=True)
        # Again after tuning: the R820T can lose a gain set before its first
        # tune (measured 2026-11-06: the band's floor 22 dB high at 3.7 dB
        # until the gain was set once more).
        self.src.set_gain(self.s.gain_db)
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
                self.retune(snap, force=rebuild)
                self.src.set_gain(snap["gain_db"])  # after a retune, as in start()
                self.version = snap["version"]
                if rebuild:
                    continue  # the block in hand was at the old rate
            t0 = time.thread_time()
            audio = self.chain(x, self.offset)
            self.auto_gain(x, snap)
            if self.wspr is not None:  # before squelch and volume
                self.wspr.push(audio, snap["dial"])
            # Listening clean-up; a decoder's sink (JS8Call's cable) stays clean.
            heard = self.listening(audio, snap)
            audio = self.gate(audio if self.s.decoder else heard, snap["squelch"])
            pcm = np.clip(audio * snap["volume"] * 32767, -32768, 32767).astype("<i2")
            self.sink.write(pcm)
            if self.listeners:
                self.stream(heard if self.s.decoder else audio)
            dt = time.thread_time() - t0
            self.cpu += (dt / (len(x) / self.chain.rate) - self.cpu) * 0.05
            self.blocks += 1
            now = self.clock()
            if now - self.spectrum_at >= self.SPECTRUM_EVERY:
                self.spectrum_at = now
                self.spectrum = self.make_spectrum(x, snap)

    def auto_gain(self, x, snap):
        """Feed AutoGain a block and make the step it asks for."""
        ag = self.autogain
        if ag is None:
            return
        if not snap["gain_auto"]:
            ag.key, ag.state = None, "off"
            return
        t0 = time.thread_time()
        now = self.clock()
        key = (self.chain.mode, round((self.src.center or 0.0) / 1e6))
        if key != ag.key:  # started, retuned by a MHz or more, or turned back on
            ag.key = key
            ag.reset(now)
        ag.decoder = self.s.decoder
        floor, sp = None, self.spectrum
        if sp is not None and sp.get("seq") != ag.spec_seq:
            ag.spec_seq = sp.get("seq")
            floor = ag.floor_of(sp["bins"])
        new = ag.observe(x, snap["gain_db"], now, self.wallclock(), floor)
        if new is not None and self.s.set_auto_gain(new):
            self.src.set_gain(new)
            ag.skip = self.q.qsize() + 2   # blocks already read at the old gain
            log("auto gain %.1f dB (%s)" % (new, ag.reason))
        ag.cpu += ((time.thread_time() - t0) / (len(x) / self.chain.rate) - ag.cpu) * 0.05

    def listening(self, audio, snap):
        """The audio for the speaker and the phone, cleaned up by AudioFx."""
        if self.fx is None or (self.fx.mode, self.fx.bandwidth) != (self.chain.mode,
                                                                   self.chain.bandwidth):
            self.fx = AudioFx(self.chain.mode, self.chain.bandwidth)
        return self.fx(audio, snap["audio"], manual_squelch=snap["squelch"] is not None)

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
                "squelch": snap["squelch"],
                "volume_min": DECODER_MIN_VOLUME if self.s.decoder == "js8" else 0.0,
                "squelch_open": self.squelch_open and (self.fx is None or self.fx.sq_open),
                "gain_auto": snap["gain_auto"],
                "gain_loop": self.autogain.status() if self.autogain is not None else
                {"state": "unavailable", "reason": "this source has no gain control"},
                "audio": self.fx.status(snap["audio"]) if self.fx is not None else
                dict(snap["audio"], active=[]),
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
        if self.path != "/api/tune":
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            doc = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(doc, dict):
                raise ValueError("body must be a JSON object")
            unknown = set(doc) - {"freq", "mode", "bandwidth", "gain_db", "volume", "squelch",
                                  "wspr_report", "gain_auto"} - {"audio_" + k for k in AUDIO_KEYS}
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
        self.send(202, {"switching": name})


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
