#!/usr/bin/env python3
"""rxd: a small RTL-SDR receiver daemon, the receiver behind `radio`.

  rxd PROFILE                 receive with a radio profile (a name under
                              ~/.config/rxd/radio/ or a path to its JSON)
  rxd PROFILE --audio stdout  raw s16le mono 48 kHz on stdout instead
  rxd PROFILE --iq FILE|-     read cu8 IQ from a file or stdin, not a dongle
  rxd PROFILE --iq synth      a synthetic signal: noise plus a test tone
                              1500 Hz above the dial, for testing anything
                              but the radio itself

It reads the same profile JSON as `radio`, opens the dongle by serial,
demodulates one channel (USB, LSB, AM, FM or WFM) and plays it to a
PipeWire/PulseAudio sink. Control is over HTTP, on 127.0.0.1 by default:

  8073  HTTP JSON (docs/API.md): GET /api/v1/status, POST /api/v1/tune {"freq": 7078000,
        "mode": "USB", "bandwidth": 2800, "gain_db": 3.7, "volume": 0.2}
        ("gain_db" turns automatic gain off; "gain_auto": true turns it on;
        "audio_auto", "audio_nr", "audio_filter", "audio_norm", "audio_limit"
        and "audio_squelch" switch the listening clean-up)

A JS8 profile ("decoder": "js8") also decodes JS8 itself, with js8.py beside
rxd; GET /api/v1/js8 lists the messages. A WSPR
profile ("decoder": "wspr") decodes WSPR with wsprd and reports the spots to
WSPRnet as the station's call at its grid (/etc/rxd/station.json, see
docs/station.md); GET /api/v1/wspr lists them. Both decode
the audio before squelch and volume, so the volume is only the speaker's.

A profile's "frequency" is the dial: for USB, where a signal 1500 Hz up in
the audio is 1500 Hz above it.

SIGHUP re-reads the profile; SIGINT or SIGTERM stops.
"""
import argparse
import collections
import ctypes
import ctypes.util
import json
import os
import queue
import re
import signal
import socket
import shutil
import sqlite3
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

# rxd's version: the one place it is set. GET /api/v1/status reports it, and
# the release workflow refuses a tag that doesn't match it.
VERSION = "0.3.0"
# The HTTP API's version: the N in /api/vN/. It goes up only when a client
# written for the old one would misread the new one (docs/API.md).
API_VERSION = 1

STATE = os.path.expanduser("~/.config/rxd/radio")
# Where this file really is: bin/rxd is a link to it, and js8.py and the
# browser UI are installed beside it.
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import station as station_conf  # noqa: E402
import location as location_conf  # noqa: E402
AUDIO_RATE = 48000
# A profile's "offset": "auto" (the default) upconverts below
# upconvert_below_hz when the station has an upconverter (station.json), since
# the R820T tunes no lower than about 24 MHz and an upconverter such as the
# Ham It Up is how HF gets in; "Ham-It-Up" always upconverts and "None" (or
# SDR++'s "Manual") never does. A profile's own upconverter_hz and
# upconvert_below_hz win over the station's. An upconverter's UPCONVERT/BYPASS
# switch may be physical: rxd can only report which way it should be.
OFFSET_MODES = ("auto", "Ham-It-Up", "None", "Manual")
# Profile sink descriptions -> PipeWire node names (None: default sink).
SINKS = {"Built-in Audio": None}
MODES = ("USB", "LSB", "AM", "FM", "WFM")
DECODERS = ("js8", "wspr", None)
DEFAULT_BW = {"USB": 2800.0, "LSB": 2800.0, "AM": 10000.0, "FM": 12500.0,
              "WFM": 150000.0}


def log(msg):
    print("rxd: " + msg, file=sys.stderr, flush=True)


_station = {"key": None, "st": None}


def station():
    """The station file (docs/station.md), read again whenever it changes."""
    path = station_conf.path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    key = (path, mtime)
    if _station["key"] != key:
        st = station_conf.load(path)
        for p in st.problems:
            log("station: " + p)
        _station.update(key=key, st=st)
    return _station["st"]


# The station's grid: a live GPS fix, else the last one saved, else the
# station file's (location.py). Everything that needs the station's grid gets
# it from station_id(), which asks this.
_location = {"loc": None}


def location():
    if _location["loc"] is None:
        _location["loc"] = location_conf.Location(
            lambda: station().identity()[1], location_conf.SavedGrid(log=log),
            off_reason="GPS is off" if station().gpsd() is None else "rxd isn't reading the GPS")
    return _location["loc"]


def start_gps():
    """Read the GPS through gpsd, unless the station file or $RXD_GPSD says off."""
    addr = station().gpsd()
    loc = location()
    if addr is None:
        loc.off_reason = "GPS is off (station file or $RXD_GPSD)"
        log("gps: off")
        return None
    gps = location_conf.GpsLocation(addr, log=log)
    loc.attach(gps)
    return gps.start()


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
            self.dial = float(p["frequency"])
            self.offset_mode = p.get("offset", "auto")
            if self.offset_mode not in OFFSET_MODES:
                raise ValueError("offset must be one of " + ", ".join(OFFSET_MODES))
            st = station()
            self.upconverter_hz = float(p.get("upconverter_hz", st.upconverter_hz))
            self.upconvert_below = float(p.get("upconvert_below_hz", st.upconvert_below_hz))
            rtl = p.get("rtl", {})
            self.serial = str(rtl.get("serial") or st.serial)
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
            sink = p["sink"] if "sink" in p else st.sink
            self.sink = SINKS.get(sink, sink)
            # What rxd decodes in this profile's audio: JS8, WSPR or nothing.
            self.decoder = p.get("decoder")
            if self.decoder not in DECODERS:
                raise ValueError("decoder must be one of " + ", ".join(d for d in DECODERS if d))
            self.wspr_report = bool(p.get("wspr_report", True))
            self.version = getattr(self, "version", 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(mode=self.mode, bandwidth=self.bandwidth, dial=self.dial,
                        upconverter=self.upconverter(),
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
        self.bands = None            # the BandPlanner, on a live rxd
        self.js8dec = None           # a js8.SlotDecoder, for JS8 decoded by rxd

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
        return snap["dial"] + snap["upconverter"]

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
            if self.js8dec is not None:  # likewise
                self.js8dec.push(audio, snap["dial"])
            # Listening clean-up, for the speaker and the stream alike; the
            # decoders above have had the audio as it was.
            heard = self.listening(audio, snap)
            audio = self.gate(heard, snap["squelch"])
            pcm = np.clip(audio * snap["volume"] * 32767, -32768, 32767).astype("<i2")
            self.sink.write(pcm)
            if self.listeners:
                self.stream(heard)
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
        # RF -> dial scale: take off the upconverter.
        to_dial = -snap["upconverter"]
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
                "mode": snap["mode"], "bandwidth": snap["bandwidth"],
                "gain_db": snap["gain_db"], "volume": snap["volume"],
                "squelch": snap["squelch"],
                "squelch_open": self.squelch_open and (self.fx is None or self.fx.sq_open),
                "gain_auto": snap["gain_auto"],
                "gain_loop": self.autogain.status() if self.autogain is not None else
                {"state": "unavailable", "reason": "this source has no gain control"},
                "audio": self.fx.status(snap["audio"]) if self.fx is not None else
                # Before the first block: the same shape, nothing active yet.
                dict(snap["audio"], active=[], nr_setting=snap["audio"]["nr"],
                     nr=snap["audio"]["nr"] if snap["audio"]["nr"] is not None
                     else snap["mode"] != "WFM"),
                "offset_mode": snap["offset_mode"], "upconverter_hz": snap["upconverter"],
                "upconvert_below_hz": snap["upconvert_below"],
                "level_dbfs": round(self.chain.level_db, 1) if self.chain else None,
                "tuner_hz": self.src.center, "offset_hz": self.offset,
                "sample_rate": self.chain.rate if self.chain else None,
                "dsp_cpu": round(self.cpu, 3), "blocks": self.blocks,
                "dropped": self.dropped, "error": self.error,
                "running": not self.stop.is_set(), "gains": self.src.gains(),
                "decoder": self.s.decoder, "station": station_status(),
                "wspr": self.wspr.status() if self.wspr is not None else None,
                "js8_decoder": self.js8dec.status() if self.js8dec is not None else None,
                "clock": clock_status(),
                "band_plan": self.bands.status() if self.bands is not None else None,
                "listeners": len(self.listeners),
                "api_version": API_VERSION, "version": VERSION}


# ---------------------------------------------------------------------- HTTP

# ------------------------------------------------------- profiles via radio

# radio from the same install as this rxd (beside it), else the one on PATH.
RADIO = os.environ.get("RXD_RADIO") or \
    (os.path.join(HERE, "radio.py") if os.path.isfile(os.path.join(HERE, "radio.py")) else
     shutil.which("radio") or "/usr/local/bin/radio")
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

    systemd-run --scope puts radio in a scope of its own, so it doesn't die
    with rxd@OLD when it stops it.
    """
    env = dict(os.environ)
    argv = [RADIO, name]
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


# ------------------------------------------------------------- JS8 messages

# A callsign: a 1-3 character prefix, a digit, a 1-4 letter suffix, and an
# optional /P-style part. Not "INFO", "HELLO" or a group like "@HB".
CALLSIGN = re.compile(r"^(?:[A-Z0-9]{1,4}/)?[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(?:/[A-Z0-9]{1,4})?$")


JS8_DIR = os.path.expanduser(os.environ.get("RXD_JS8_DIR", "~/.local/share/rxd/js8"))
JS8_LOG = os.path.join(JS8_DIR, "DIRECTED.TXT")


class Js8Feed:
    """New lines of the JS8 decoder's DIRECTED.TXT (JS8's usual message log
    format), parsed and numbered, read as they're asked for. It outlives the
    profile, so /api/js8 keeps its history on a WSPR profile and after a
    restart."""

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
        text = f[4].replace("\u2662", "").strip()  # JS8's end-of-message diamond
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


# ------------------------------------------------------------ JS8 decoder

# A JS8 profile ("decoder": "js8") decodes JS8 with js8.py. It records slots
# of this audio, before squelch and volume, and writes ALL.TXT (frames) and
# DIRECTED.TXT (messages) under JS8_DIR, which Js8Feed tails for /api/js8.
# "js8_submodes" picks the speeds to decode (default normal, fast and slow,
# about a third of one CM4 core; turbo adds about a fifth).
JS8_SUBMODES = ("normal", "fast", "turbo", "slow")
JS8_DEFAULT_SUBMODES = ["normal", "fast", "slow"]
JS8_PY = (os.environ.get("RXD_JS8_PY", ""),
          os.path.join(HERE, "js8.py"),
          "/usr/local/lib/rxd/js8.py")


def load_js8():
    import importlib.util
    for path in JS8_PY:
        if path and os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("js8", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def make_js8(settings, p):
    """The SlotDecoder for a JS8 profile decoded by rxd, or None."""
    js8 = load_js8()
    if js8 is None:
        log("no js8.py at %s: JS8 won't decode" % ", ".join(x for x in JS8_PY if x))
        return None
    subs = p.get("js8_submodes") or JS8_DEFAULT_SUBMODES
    bad = [x for x in subs if x not in JS8_SUBMODES]
    if bad:
        raise ValueError("js8_submodes: unknown %s (have %s)" % (", ".join(bad),
                                                                ", ".join(JS8_SUBMODES)))
    return js8.SlotDecoder(settings.snapshot()["dial"], subs, rate_in=AUDIO_RATE)


# --------------------------------------------------------------------- WSPR

# rxd decodes WSPR itself rather than through WSJT-X: it records each
# two-minute slot from its own audio, before the volume and squelch, and runs
# wsprd (WSJT-X's command-line decoder, from the wsjtx package) on it. Spots
# go to the feed at /api/wspr and, when reporting is on, to WSPRnet.
WSPRD = os.environ.get("RXD_WSPRD") or shutil.which("wsprd") or "/usr/bin/wsprd"
WSPR_DIR = os.path.expanduser(os.environ.get("RXD_WSPR_DIR", "~/.local/share/rxd/wspr"))
# WSJT-X's own upload endpoint (Network/wsprnet.cpp): one form POST per spot.
WSPRNET_URL = os.environ.get("RXD_WSPRNET", "http://wsprnet.org/post/")
RXD_VERSION = "rxd-" + VERSION
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


def upconverter_name():
    return station().upconverter_name or "upconverter"


def station_missing():
    return "set call and grid in %s" % station_conf.path()


def station_id():
    """(call, grid): this station. The call is station.json's ($RXD_CALL wins);
    the grid is the GPS's when it has an accepted fix, else the last one saved,
    else station.json's ($RXD_GRID wins). See location()."""
    return station().identity()[0], location().grid()[0]


def station_status():
    """{"call", "grid", "grid_source", "grid_reason", "default_grid",
    "saved_grid", "gps", "file", "upconverter", "problems"}, and "missing"
    saying what to set when the call or grid is."""
    call = station().identity()[0]
    loc = location().status()
    grid = loc["grid"]
    doc = dict({"call": call}, **station().describe())
    if loc.pop("saved_problem"):
        doc["problems"].append(location().saved.problem)
    doc.update(loc)
    if not call or not grid:
        doc["missing"] = "no %s: %s" % (" or ".join(
            x for x, v in (("callsign", call), ("grid", grid)) if not v), station_missing())
    return doc


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

    def __init__(self, call=None, grid=None, url=None, post=None, ident=None):
        # ident() -> (call, grid), read at each report so a new grid (the
        # GPS's, say) is used without a restart.
        self.ident = ident or (lambda: (call, grid))
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

    @property
    def call(self):
        return self.ident()[0]

    @property
    def grid(self):
        return self.ident()[1]

    def queue_slot(self, spots, dial_hz):
        call, grid = self.ident()
        if not call or not grid:
            return 0
        rqrg = "%.6f" % (dial_hz / 1e6)
        common = {"version": RXD_VERSION, "rcall": call, "rgrid": grid,
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
        call, grid = self.ident()
        if not had_spots and call and grid:
            beat = {"function": "wsprstat", "rcall": call, "rgrid": grid,
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
        call, grid = self.ident()
        return {"enabled": True, "call": call, "grid": grid, "sent": self.sent,
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
        self.load_history()
        threading.Thread(target=self.worker, name="wspr", daemon=True).start()

    # The station, read live: the GPS may move the grid while rxd runs.
    @property
    def call(self):
        return station_id()[0]

    @property
    def grid(self):
        return station_id()[1]

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
        call, grid = self.reporter.ident() if self.reporter is not None else station_id()
        if self.reporter is None or not call or not grid:
            report = {"enabled": False, "call": call, "grid": grid,
                      "reason": "no callsign and grid to report as: " + station_missing()}
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
#   ~/.config/rxd/bands.json            its settings, as the UI sets them
#   ~/.local/share/rxd/bands.json       what was heard, per profile and UTC hour
#   ~/.local/share/rxd/bandplan.json    where it is: scanning or holding, and why
#   ~/.local/share/rxd/conditions.json  the last band conditions fetched
BANDS_CONF = os.path.expanduser(os.environ.get("RXD_BANDS_CONF", "~/.config/rxd/bands.json"))
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
    """(sunrise, sunset) Unix times on the station's solar day holding t, either None if it doesn't
    happen (or no location)."""
    if not latlon:
        return None, None
    # The station's own solar day (local mean time from its longitude), not
    # the machine's time zone: on a UTC clock, an American sunset falls after
    # midnight and would come before that day's sunrise.
    off = latlon[1] / 15.0 * 3600
    start = (t + off) // 86400 * 86400 - off
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
    """Whether rxd's JS8 decoder (rx.js8dec) is running without an error."""
    dec = getattr(rx, "js8dec", None)
    if dec is None or not hasattr(dec, "status"):
        return False
    try:
        st = dec.status()
        return bool(st.get("running", not st.get("error")))
    except Exception:  # noqa: BLE001
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
        self._latlon = latlon   # Ellipsis: the station's grid centre, read live
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

    @property
    def latlon(self):
        """Where the sun is worked out for: the station's grid centre, live
        (so a GPS fix moves it), or the location the planner was given."""
        return grid_latlon(station_id()[1]) if self._latlon is Ellipsis else self._latlon

    @latlon.setter
    def latlon(self, v):
        self._latlon = v

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
        if self.fresh:
            st["after_scan"] = True  # the very first choice: the best band, no margin
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

    def free_side(self):
        """True when Auto may pick a band on the other side of the upconverter:
        cross_side is on, or no person has to flip a switch to get there."""
        return self.conf["cross_side"] or not station().manual_switch

    def side_up(self):
        """True if the upconverter is (as far as rxd knows) on UPCONVERT: the
        side the running profile needs, which is how its switch was last set."""
        with self.rx.s.lock:
            return self.rx.s.upconverter() > 0

    def enabled(self, up):
        b = self.conf.get("bands")
        if b is None:  # the default: every band (Win's call, 2026-11-06); the side
            return list(BAND_ORDER)  # rule still keeps Auto to the switch's side
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
            elif bool(r.get("upconvert")) != up and not self.free_side():
                why[r["band"]] = "needs the %s on %s" % (
                    upconverter_name(), "UPCONVERT" if r.get("upconvert") else "BYPASS")
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
            st.update(phase="idle", reason="Auto: no %s bands enabled on the %s's %s side" % (
                "WSPR" if self.conf["mode"] == "wspr" else "JS8", upconverter_name(),
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
                    elif bool(r.get("upconvert")) != up and not self.free_side():
                        why = "needs the %s on %s" % (
                            upconverter_name(), "UPCONVERT" if r.get("upconvert") else "BYPASS")
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


# ---------------------------------------------------------------------- map
# The web UI's station map. Two endpoints, both read-only:
#   GET /api/v1/tiles/Z/X/Y.png  offline raster tiles (z/x/y, Web Mercator),
#                         from $RXD_TILES or the tree meshtastic-ui keeps
#                         (rx installs one): ~/.portduino/default/maps/<style>/
#                         (OSM standard style, zoom 1-6 worldwide). Nothing
#                         is ever fetched from the internet.
#   GET /api/v1/tiles/info.json  whether there are tiles, and which zooms
#   GET /api/v1/stations  stations heard by JS8 and WSPR that have a grid,
#                         one per mode and call, at their grid square's centre
# Grids for JS8 stations come from their own heartbeats and CQs ("Q1ABC: @HB
# HEARTBEAT FN31"), in rxd's JS8 logs: DIRECTED.TXT keeps the grid of those,
# and ALL.TXT beside it has every frame, including those of messages that
# never finished. Learned grids are kept in $RXD_GRIDS
# (~/.local/share/rxd/grids.json) so a station that heartbeats once stays on
# the map when it's heard again later. No online callsign lookups.

TILE_DIRS = (os.environ.get("RXD_TILES", ""), "~/.portduino/default/maps", "/maps")
TILE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
TILE_PATH = re.compile(r"^/tiles/(\d{1,2})/(\d{1,7})/(\d{1,7})\.(png|jpg|jpeg|webp)$")
GRIDS_FILE = os.path.expanduser(os.environ.get("RXD_GRIDS", "~/.local/share/rxd/grids.json"))
TILE_ATTRIBUTION = "\u00a9 OpenStreetMap contributors"


def tile_root(dirs=TILE_DIRS):
    """The first directory holding a z/x/y tree, as (path, sorted zooms), else (None, [])."""
    for d in dirs:
        if not d:
            continue
        d = os.path.expanduser(d)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        zooms = sorted(int(n) for n in names if n.isdigit() and os.path.isdir(os.path.join(d, n)))
        if zooms:
            return os.path.realpath(d), zooms
        # meshtastic-ui's layout: maps/<style>/z/x/y, OSM's style first
        for style in sorted(names, key=lambda n: (n not in ("openstreetmap", "osm"), n)):
            sub = os.path.join(d, style)
            try:
                zs = sorted(int(n) for n in os.listdir(sub) if n.isdigit())
            except OSError:
                continue
            if zs:
                return os.path.realpath(sub), zs
    return None, []


def tile_file(root, path):
    """The file for /tiles/Z/X/Y.ext under root, or None for anything else."""
    m = TILE_PATH.match(path)
    if not root or not m:
        return None, None
    z, x, y = int(m[1]), int(m[2]), int(m[3])
    if z > 22 or x >= 1 << z or y >= 1 << z:
        return None, None
    f = os.path.realpath(os.path.join(root, str(z), str(x), "%d.%s" % (y, m[4])))
    if not f.startswith(root + os.sep) or not os.path.isfile(f):
        return None, None
    return f, TILE_TYPES[m[4]]


def serve_tile(h, path):
    root, zooms = getattr(h.server, "tile_root", None) or tile_root()
    if path == "/tiles/info.json":
        return h.send(200, {"available": bool(root), "zooms": zooms,
                            "min": zooms[0] if zooms else None, "max": zooms[-1] if zooms else None,
                            "attribution": TILE_ATTRIBUTION if root else None})
    f, ctype = tile_file(root, path)
    if not f:
        return h.send(404, {"error": "no such tile"})
    with open(f, "rb") as fh:
        body = fh.read()
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "max-age=86400")
    h.end_headers()
    h.wfile.write(body)


def js8_grid(text):
    """(call, grid) from a JS8 heartbeat or CQ that carries one, else None."""
    frm, _, body = (text or "").replace("\u2662", "").partition(":")
    frm, words = frm.strip(), body.split()
    if len(words) < 2 or not CALLSIGN.match(frm) or not GRID.match(words[-1]):
        return None
    if not {"HEARTBEAT", "CQ", "GRID"} & set(words[:-1]):
        return None
    g = words[-1]
    return frm, g[:4].upper() + g[4:].lower()


# ALL.TXT, one line per JS8 frame:
# "2026-11-06 10:47:00-11  0.3  605 B  3+pVJzNoOkJW         3    WD4UQ: @HB HEARTBEAT EM88"
JS8_ALL_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*([+-]?\d+)\s+[+-]?\d+\.\d+\s+\d+\s+"
                          r"\S\s+\S+\s+\d+\s+(.*\S)\s*$")


def iso_utc(t):
    """'2026-11-06 10:47:03' or '2026-11-06T12:20:00Z' (both UTC) -> '2026-11-06T10:47:03Z'."""
    m = re.match(r"^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d)(:\d\d)?", t or "")
    return "%sT%s%sZ" % (m[1], m[2], m[3] or ":00") if m else None


# The offline callsign database rxd/callsign_db.py builds (FCC licences
# geocoded to 6-character grids, ZIP centroids, and cty.dat prefixes for
# everyone else). Read-only here; reopened when the updater replaces it.
CALLSIGNS_DB = os.path.expanduser(os.environ.get("RXD_CALLSIGNS", "~/.local/share/rxd/callsigns.sqlite"))
# call suffixes that say how, not where: /P portable, /M mobile, /QRP ...
CALL_SUFFIXES = {"P", "M", "QRP", "QRPP", "A", "LH", "R", "B", "J", "Y"}


class CallBook:
    """call -> {"grid", "prec": "addr" | "zip" | "country", "region"}, or None."""

    def __init__(self, path=None):
        self.path = path or CALLSIGNS_DB
        self.con = self.stamp = None
        self.memo = {}

    def db(self):
        try:
            st = os.stat(self.path)
        except OSError:
            st = None
        stamp = st and (st.st_ino, st.st_mtime_ns)
        if stamp != self.stamp:
            if self.con:
                self.con.close()
            self.con, self.stamp, self.memo = None, stamp, {}
            if st:
                try:
                    self.con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(self.path),
                                               uri=True, check_same_thread=False)
                    self.con.execute("SELECT 1 FROM calls LIMIT 1")
                except sqlite3.Error as e:
                    log("callsign database %s unusable: %s" % (self.path, e))
                    self.con = None
        return self.con

    def country(self, con, s):
        keys = ["=" + s] + [s[:n] for n in range(len(s), 0, -1)]
        rows = dict((k, (g, e)) for k, g, e in con.execute(
            "SELECT prefix, grid, entity FROM prefixes WHERE prefix IN (%s)" % ",".join("?" * len(keys)),
            keys))
        for k in keys:
            if k in rows:
                return {"grid": rows[k][0], "prec": "country", "region": rows[k][1]}
        return None

    def lookup(self, call):
        con = self.db()
        call = (call or "").strip().upper()
        if not con or not call:
            return None
        if call in self.memo:
            return self.memo[call]
        try:
            self.memo[call] = r = self.find(con, call)
        except sqlite3.Error:
            r = None
        return r

    def find(self, con, call):
        parts = [p for p in call.split("/") if p]
        if not parts or any(p in ("MM", "AM") for p in parts[1:]):
            return None  # at sea or in the air
        r = con.execute("SELECT grid, entity FROM prefixes WHERE prefix=?", ("=" + call,)).fetchone()
        if r:
            return {"grid": r[0], "prec": "country", "region": r[1]}
        full = [p for p in parts if CALLSIGN.match(p)]
        home = max(full, key=len) if full else parts[0]
        rest = [p for p in parts if p != home and p not in CALL_SUFFIXES]
        if rest:  # VE3/KA1PID, EA8/K1ABC, KA1PID/4: operating away from home
            where = rest[0]
            if where.isdigit():
                where = home
            return self.country(con, where)
        r = con.execute("SELECT grid, prec, region FROM calls WHERE call=?", (home,)).fetchone()
        if r:
            return {"grid": r[0], "prec": r[1], "region": r[2]}
        return self.country(con, home)


class StationMap:
    """Heard stations with a grid, merged per mode and call, for /api/stations."""

    ALL_TAIL = 262144

    def __init__(self, js8=None, wspr=None, all_path=None, grids_path=None, clock=time.time,
                 me=None, book=None):
        self.js8 = js8                # a Js8Feed (or anything with since())
        self.book = book if book is not None else CallBook()  # the offline callsign database
        self.wspr = wspr              # a callable returning the WsprDecoder or None
        self.all_path = all_path if all_path is not None else \
            os.path.join(os.path.dirname(JS8_LOG), "ALL.TXT")
        self.grids_path = grids_path if grids_path is not None else GRIDS_FILE
        self.clock = clock
        self._me = me                 # (call, grid), or None: this station, read live
        self.lock = threading.Lock()
        self.grids = {}               # call -> {"grid", "time"}
        self.heard = {}               # call -> JS8 frames from ALL.TXT: {"time", "snr", "freq"}
        self.all_pos = self.all_inode = None
        self.dirty = False
        try:
            with open(self.grids_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.grids = {c: v for c, v in doc.items()
                          if isinstance(v, dict) and GRID.match(str(v.get("grid", "")))}
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def lines(src):
        if src is None:
            return []
        lk = getattr(src, "lock", None)
        if isinstance(getattr(src, "lines", None), collections.deque) and lk is not None:
            if hasattr(src, "refresh"):
                src.since(1 << 62)  # reads anything new in the log
            with lk:
                return list(src.lines)
        return src.since(0).get("lines", [])

    @property
    def me(self):
        return self._me if self._me is not None else station_id()

    def learn(self, call, grid, t):
        old = self.grids.get(call)
        if old and (t or "") < (old.get("time") or ""):
            return  # an older frame: keep the newer grid
        if not old or old["grid"] != grid:
            self.dirty = True
        self.grids[call] = {"grid": grid, "time": t}

    def read_all(self):
        """New heartbeat and CQ frames with grids from ALL.TXT."""
        try:
            st = os.stat(self.all_path)
        except OSError:
            return
        if self.all_inode != st.st_ino or (self.all_pos or 0) > st.st_size:
            self.all_inode, self.all_pos = st.st_ino, max(st.st_size - self.ALL_TAIL, 0)
        if self.all_pos == st.st_size:
            return
        with open(self.all_path, "rb") as fh:
            fh.seek(self.all_pos)
            data = fh.read()
        cut = data.rfind(b"\n") + 1
        self.all_pos += cut
        for raw in data[:cut].decode("utf-8", "replace").split("\n"):
            m = JS8_ALL_LINE.match(raw)
            cg = m and js8_grid(m[3])
            if cg:
                t = iso_utc(m[1])
                self.learn(cg[0], cg[1], t)
                self.heard.setdefault(cg[0], []).append({"time": t, "snr": int(m[2])})
                del self.heard[cg[0]][:-50]

    def save(self):
        if not self.dirty:
            return
        self.dirty = False
        try:
            os.makedirs(os.path.dirname(self.grids_path), exist_ok=True)
            tmp = self.grids_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.grids, fh, sort_keys=True)
            os.replace(tmp, self.grids_path)
        except OSError as e:
            log("can't save grids to %s: %s" % (self.grids_path, e))

    def snapshot(self, hours=24.0):
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() - hours * 3600))
        out, nogrid = {}, set()
        me_call, me_grid = self.me     # once, so every distance uses the same grid

        def add(mode, call, grid, t, snr, freq=None, dbm=None):
            if not t or t < cutoff:
                return
            src, region = "heard", None
            if not grid:
                # never heard with a grid: the callsign database, if it knows
                found = self.book.lookup(call) if self.book else None
                if not found:
                    nogrid.add((mode, call))
                    return
                grid, src, region = found["grid"], found["prec"], found.get("region")
            k = (mode, call)
            s = out.get(k)
            if s is None:
                ll = grid_latlon(grid)
                s = out[k] = {"call": call, "mode": mode, "grid": grid, "lat": round(ll[0], 4),
                              "lon": round(ll[1], 4),
                              "km": grid_km(me_grid, grid) if src != "country" else None,
                              "snr": snr, "last": t, "last_snr": snr, "freq": freq, "dbm": dbm,
                              "src": src, "region": region}
            if snr is not None and (s["snr"] is None or snr > s["snr"]):
                s["snr"] = snr
            if t >= s["last"]:
                s["last"], s["last_snr"] = t, snr
                if freq is not None:
                    s["freq"] = freq
                if dbm is not None:
                    s["dbm"] = dbm
                if grid != s["grid"] and src == "heard" == s["src"]:  # moved: plot it where it is now
                    ll = grid_latlon(grid)
                    s.update(grid=grid, lat=round(ll[0], 4), lon=round(ll[1], 4),
                             km=grid_km(me_grid, grid))

        with self.lock:
            self.read_all()
            js8 = self.lines(self.js8)
            for ln in js8:
                cg = js8_grid(ln.get("text"))
                if cg:
                    self.learn(cg[0], cg[1], iso_utc(ln.get("time")))
            wspr = self.wspr() if self.wspr else None
            spots = self.lines(wspr)
            for sp in spots:
                if sp.get("grid") and sp.get("call"):
                    self.learn(sp["call"], sp["grid"], sp.get("time"))
            self.save()
            grids = self.grids
            for call, frames in self.heard.items():
                for f in frames:
                    add("js8", call, grids[call]["grid"], f["time"], f["snr"])
            for ln in js8:
                call = ln.get("from")
                t = iso_utc(ln.get("time"))
                if not call or not CALLSIGN.match(call):
                    continue
                g = grids.get(call)
                add("js8", call, g and g["grid"], t, ln.get("snr"), ln.get("freq"))
            for sp in spots:
                call = sp.get("call")
                g = sp.get("grid") or (grids.get(call) or {}).get("grid")
                add("wspr", call, g, sp.get("time"), sp.get("snr"), sp.get("freq"), sp.get("dbm"))
        stations = sorted(out.values(), key=lambda s: s["last"], reverse=True)
        ll = grid_latlon(me_grid)
        me = {"call": me_call, "grid": me_grid, "lat": ll[0], "lon": ll[1]} if ll else None
        return {"me": me, "hours": hours, "stations": stations,
                "no_grid": {m: sum(1 for x in nogrid if x[0] == m and x not in out)
                            for m in ("js8", "wspr")}}


def serve_stations(h):
    srv = h.server
    if getattr(srv, "stations", None) is None:
        srv.stations = StationMap(js8=getattr(srv, "js8", None), wspr=lambda: srv.rx.wspr)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
    try:
        hours = float(q.get("hours", ["24"])[0])
        if not 0 < hours <= 24 * 30:
            raise ValueError
    except ValueError:
        return h.send(400, {"error": "hours must be a number from 0 to 720"})
    doc = srv.stations.snapshot(hours)
    doc["decoder"] = srv.rx.s.decoder
    return h.send(200, doc)


# The browser UI: $RXD_WEB, where install.py puts it (beside this file, which
# bin/rxd links to), or clients/web/ in a checkout. Read on every request so a
# reinstall needs no restart.
WEB_PATHS = (os.environ.get("RXD_WEB", ""), os.path.join(HERE, "web", "index.html"),
             os.path.join(os.path.dirname(HERE), "clients", "web", "index.html"))


def web_page():
    for path in WEB_PATHS:
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
    return None


API_PREFIX = "/api/v%d/" % API_VERSION


def route(path):
    """The handler's name for a request path: /api/v1/X is /api/X, and so is
    the old unversioned /api/X, kept as an alias; /api/v1/tiles/... is
    /tiles/.... None for another API version."""
    if path.startswith(API_PREFIX):
        rest = path[len(API_PREFIX) - 1:]
        return rest if rest.startswith("/tiles/") else "/api" + rest
    if re.match(r"^/api/v[0-9]+/", path):
        return None
    return path


class Api(BaseHTTPRequestHandler):
    server_version = "rxd/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def send(self, code, doc):
        body = (json.dumps(doc) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def no_version(self):
        return self.send(404, {"error": "this rxd speaks API version %d (%s)"
                                        % (API_VERSION, API_PREFIX), "api_version": API_VERSION})

    def do_GET(self):
        path = route(self.path.split("?", 1)[0])
        if path is None:
            return self.no_version()
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
        if path.startswith("/tiles/"):
            return serve_tile(self, path)
        if path == "/api/stations":
            return serve_stations(self)
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
        path = route(self.path.split("?", 1)[0])
        if path is None:
            return self.no_version()
        if path == "/api/profile":
            return self.switch()
        if path == "/api/bands":
            return self.bands_post()
        if path != "/api/tune":
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
    # Always a reporter: it reports only while there's a call and a grid,
    # read at each slot, so a GPS fix or an edited station file takes effect
    # without a restart.
    return WsprDecoder(settings, reporter=WsprNet(ident=station_id))


def hostport(text, default_port):
    host, _, port = text.rpartition(":")
    return (host or "127.0.0.1", int(port or default_port))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="rxd", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="rxd " + VERSION)
    ap.add_argument("profile")
    ap.add_argument("--iq", help="cu8 file, - for stdin, or synth (default: the dongle)")
    ap.add_argument("--iq-center", type=float,
                    help="tuner frequency the IQ file was recorded at")
    ap.add_argument("--audio", default="pulse",
                    help="pulse, pulse:SINK, stdout or null (default: the profile's sink)")
    # From the environment by default, so the systemd unit's EnvironmentFile
    # (/etc/rxd/rxd.env) can open HTTP to the LAN without editing the unit.
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
        src = SynthSource(snap["dial"] + snap["upconverter"] + 1500)
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
    try:
        if settings.decoder == "js8":
            rx.js8dec = make_js8(settings, p)
    except ValueError as e:
        sys.exit("rxd: " + str(e))
    start_gps()
    if not all(station_id()):
        log("no station identity: " + station_missing())
    rx.start()
    servers = []
    if a.http != "off":
        hs = ThreadingHTTPServer(hostport(a.http, 8073), Api)
        hs.daemon_threads = True
        hs.rx = rx
        hs.js8 = Js8Feed(rx.js8dec.directed_path if rx.js8dec is not None else None)
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
    log("%s %.4f MHz %s, tuner %.4f MHz, http %s"
        % (os.path.basename(path), st["freq"] / 1e6, st["mode"],
           (st["tuner_hz"] or 0) / 1e6, a.http))
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
