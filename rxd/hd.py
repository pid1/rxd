"""hd: HD Radio (NRSC-5) for rxd's FM broadcast (WFM) profiles.

rxd plays the analog signal as soon as a WFM profile starts. When the
spectrum shows HD Radio's digital sidebands (OFDM about 129-199 kHz either
side of the carrier), it starts nrsc5 as a separate process and pipes it the
dongle's raw IQ, untouched: on an HD-capable profile the dongle runs at
nrsc5's 1488375 S/s centred on the station, so the bytes rxd reads are
exactly what nrsc5 wants. nrsc5 (https://github.com/theori-io/nrsc5, GPL-3)
is never linked, vendored or copied: rxd only runs the `nrsc5` program and
reads what it prints, its audio on stdout and its log on stderr.

    nrsc5 -r - -o - -t raw PROGRAM    IQ on stdin, 44.1 kHz s16 stereo out

Once nrsc5 has held sync with clean audio for a few seconds, rxd crossfades
from analog to HD1, lined up with the analog by cross-correlating the two
(both come from the same IQ clock, so the offset holds while nrsc5 stays in
sync). nrsc5's HD audio usually comes out ahead of the same audio on analog,
so rxd delays the HD; if it ever comes out behind, rxd delays the analog to
match, as a real receiver does. Losing sync, a bad audio frame or a poor BER
fades back to analog at once; coming back waits longer each time, so a
fringe station doesn't flap. HD2, HD3 ... are digital-only: picking one
plays it or, without HD, silence.

Everything here is driven from rxd's DSP thread (Hd.process, once per IQ
block) plus three small threads per nrsc5 process for its pipes. The pure
parts (PolyResampler, sidebands, Nrsc5Log, Blend, find_offset) take their
inputs as arguments so the tests can drive them with scripted data.
"""
import collections
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
from scipy import signal as sps

HD_RATE = 1488375            # nrsc5's cu8 input rate, S/s
NRSC5_RATE = 44100           # nrsc5's audio
AUDIO_RATE = 48000           # rxd's audio
FRAME = 2048                 # nrsc5 audio frame, samples per channel
FRAME_BYTES = FRAME * 4      # s16 stereo
CORR_RATE = 8000             # the rate alignment correlates at
CORR_DECIM = AUDIO_RATE // CORR_RATE

STATES = ("off", "no signal", "acquiring", "digital", "analog fallback")
NOT_INSTALLED = "HD Radio: nrsc5 not installed"


def find_nrsc5(configured=None):
    """The nrsc5 to run: $RXD_NRSC5, the station's, or nrsc5 on PATH. None if none."""
    for cand in (os.environ.get("RXD_NRSC5"), configured):
        if cand:
            if cand == "off":
                return None
            return cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None
    return shutil.which("nrsc5") or next(
        (p for p in ("/usr/local/bin/nrsc5", "/usr/bin/nrsc5") if os.access(p, os.X_OK)), None)


# ------------------------------------------------------------------ resampling

class PolyResampler:
    """A rational up/down resampler: one polyphase FIR, state across blocks.

    Output m is at input position m * down / up. Feeding a signal in blocks
    of any size gives the same samples as feeding it whole.
    """

    def __init__(self, up, down, cutoff, fs_in, taps_per_phase=32):
        self.up, self.down, self.k = int(up), int(down), int(taps_per_phase)
        h = sps.firwin(self.k * self.up, cutoff, fs=fs_in * self.up) * self.up
        # Row p is phase p's taps, oldest input first.
        self.h = h.reshape(self.k, self.up).T[:, ::-1].astype(np.float32).copy()
        self.hist = np.zeros(self.k - 1, np.float32)
        self.t = 0  # the next output's position, in up-rate units from the block's start

    def __call__(self, x):
        x = np.asarray(x, np.float32)
        xx = np.concatenate([self.hist, x])
        end = len(x) * self.up
        ts = np.arange(self.t, end, self.down, dtype=np.int64)
        self.hist = xx[len(xx) - (self.k - 1):]
        if not len(ts):
            self.t -= end
            return np.zeros(0, np.float32)
        win = np.lib.stride_tricks.sliding_window_view(xx, self.k)
        y = np.einsum("ij,ij->i", win[ts // self.up], self.h[ts % self.up])
        self.t = int(ts[-1] + self.down - end)
        return y.astype(np.float32)


class FirDecim:
    """A real FIR keeping every d-th output, with history across blocks."""

    def __init__(self, taps, d):
        self.taps = np.asarray(taps, np.float32)[::-1].copy()
        self.d = d
        self.hist = np.zeros(len(taps) - 1, np.float32)
        self.phase = 0

    def __call__(self, x):
        xx = np.concatenate([self.hist, np.asarray(x, np.float32)])
        win = np.lib.stride_tricks.sliding_window_view(xx, len(self.taps))
        out = win[self.phase::self.d] @ self.taps
        self.phase = (-(len(x) - self.phase)) % self.d
        self.hist = xx[len(xx) - (len(self.taps) - 1):]
        return out.astype(np.float32)


def corr_filter():
    """200-3500 Hz then down to CORR_RATE: what alignment compares."""
    return FirDecim(sps.firwin(97, [200, 3500], fs=AUDIO_RATE, pass_zero=False), CORR_DECIM)


class Ring:
    """The last cap samples of a stream, addressed by absolute sample index."""

    def __init__(self, seconds, rate):
        self.cap = int(seconds * rate)
        self.buf = np.zeros(self.cap, np.float32)
        self.end = 0  # samples written ever
        self.lock = threading.Lock()

    def write(self, x):
        x = np.asarray(x, np.float32)[-self.cap:]
        with self.lock:
            n = len(x)
            i = self.end % self.cap
            first = min(n, self.cap - i)
            self.buf[i:i + first] = x[:first]
            self.buf[:n - first] = x[first:]
            self.end += n

    @property
    def start(self):
        return max(0, self.end - self.cap)

    def read(self, start, n):
        """Samples start..start+n; None unless all of them are held."""
        with self.lock:
            if n < 0 or start < max(0, self.end - self.cap) or start + n > self.end:
                return None
            idx = (np.arange(start, start + n) % self.cap)
            return self.buf[idx].copy()


# ------------------------------------------------------------------ detection

SIDEBAND = (135e3, 195e3)    # inside the 129-199 kHz OFDM, clear of its edges
SIDEBAND_DB = 5.0            # both sidebands this far over the floor


def sidebands(spec, threshold=SIDEBAND_DB):
    """Whether a spectrum (rxd's /api/v1/spectrum shape) shows HD sidebands.

    {"present", "lower_db", "upper_db", "floor_db"}: each sideband's median
    level and the band's floor (its 20th percentile). Both sidebands have to
    stand clear of the floor and be within 8 dB of each other: one strong
    side alone is usually a neighbouring station, not HD.
    """
    bins = np.asarray(spec["bins"], float)
    n = len(bins)
    f = spec["start_hz"] + (np.arange(n) + 0.5) * (spec["stop_hz"] - spec["start_hz"]) / n
    d = f - spec["dial_hz"]
    lo = bins[(d > -SIDEBAND[1]) & (d < -SIDEBAND[0])]
    hi = bins[(d > SIDEBAND[0]) & (d < SIDEBAND[1])]
    if len(lo) < 3 or len(hi) < 3:
        return {"present": False, "lower_db": None, "upper_db": None, "floor_db": None}
    floor = float(np.percentile(bins, 20))
    lo_db, hi_db = float(np.median(lo)) - floor, float(np.median(hi)) - floor
    present = min(lo_db, hi_db) >= threshold and abs(lo_db - hi_db) <= 8.0
    return {"present": bool(present), "lower_db": round(lo_db, 1),
            "upper_db": round(hi_db, 1), "floor_db": round(floor, 1)}


# ------------------------------------------------------------------ nrsc5's log

_LINE = re.compile(r"^(?:\d\d:\d\d:\d\d )?(.*)$")


class Nrsc5Log:
    """What nrsc5's stderr has said: sync, quality, station and programs.

    nrsc5 prints "HH:MM:SS message" lines (src/main.c's callback); the
    messages parsed here are the ones its README and source document.
    """

    def __init__(self):
        self.synced = False
        self.syncs = 0               # how many times it has synchronised
        self.sync_at = None
        self.mer = None              # (lower, upper) dB
        self.ber = None
        self.station = None
        self.slogan = None
        self.title = self.artist = self.album = None
        self.programs = {}           # program -> {"type": ...}
        self.crc_errors = 0          # audio packets with CRC errors, total
        self.crc_at = None
        self.bitrate = None
        self.lines = 0
        self.last = None             # the last line, for "why did it exit"

    def parse(self, line, now):
        m = _LINE.match(line.rstrip("\r\n"))
        msg = m.group(1).strip() if m else line.strip()
        self.lines += 1
        self.last = msg
        if msg == "Synchronized":
            self.synced, self.sync_at = True, now
            self.syncs += 1
        elif msg == "Lost synchronization":
            self.synced = False
            self.mer = self.ber = None
        elif msg.startswith("MER:"):
            v = re.findall(r"(-?[\d.]+) dB", msg)
            if len(v) == 2:
                self.mer = (float(v[0]), float(v[1]))
        elif msg.startswith("BER:"):
            v = re.match(r"BER: ([\d.eE+-]+)", msg)
            if v:
                self.ber = float(v.group(1))
        elif msg.startswith("Station name:"):
            self.station = msg.split(":", 1)[1].strip() or None
        elif msg.startswith("Slogan:"):
            self.slogan = msg.split(":", 1)[1].strip() or None
        elif msg.startswith("Title:"):
            self.title = msg.split(":", 1)[1].strip() or None
        elif msg.startswith("Artist:"):
            self.artist = msg.split(":", 1)[1].strip() or None
        elif msg.startswith("Album:"):
            self.album = msg.split(":", 1)[1].strip() or None
        elif msg.startswith("Audio service "):
            v = re.match(r"Audio service (\d+): \w+, type: ([^,]*)", msg)
            if v:
                self.programs[int(v.group(1))] = {"type": v.group(2).strip()}
        elif msg.startswith("Audio packet CRC mismatches:"):
            v = re.findall(r"\d+", msg)
            if v:
                self.crc_errors += int(v[-1])
                self.crc_at = now
        elif msg.startswith("Audio bit rate:"):
            v = re.findall(r"[\d.]+", msg)
            if v:
                self.bitrate = float(v[0])

    def program_list(self):
        out = []
        for n in sorted(self.programs):
            t = self.programs[n]["type"]
            out.append({"program": n, "name": "HD%d" % (n + 1),
                        "type": None if t in ("", "None") else t})
        return out


# ------------------------------------------------------------------ the process

class Nrsc5:
    """One nrsc5 process: IQ in, 48 kHz mono HD audio out, its log parsed.

    feed() never blocks: a block that doesn't fit the pipe's queue is
    dropped and counted (nrsc5 then loses sync, which the blend sees).
    The HD audio lands in .audio (a Ring) as it arrives, and in .corr at
    CORR_RATE for alignment; .bad holds the ring positions of frames nrsc5
    filled with silence because their packets were missing or corrupt.
    """

    QUEUE = 12   # IQ blocks waiting for nrsc5's stdin: about 1.2 s

    def __init__(self, cmd, program=0, clock=time.monotonic, ring_seconds=30.0):
        self.cmd = list(cmd) + ["-r", "-", "-o", "-", "-t", "raw", str(int(program))]
        self.program = int(program)
        self.clock = clock
        self.log = Nrsc5Log()
        self.audio = Ring(ring_seconds, AUDIO_RATE)
        self.corr = Ring(ring_seconds, CORR_RATE)
        self.bad = collections.deque(maxlen=256)   # (start, end) in .audio
        self.frames = 0
        self.bad_frames = 0
        self.dropped = 0
        self.started = clock()
        self.q = queue.Queue(maxsize=self.QUEUE)
        self.rs = PolyResampler(160, 147, 20000.0, NRSC5_RATE, 32)
        self.cf = corr_filter()
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        self.threads = [threading.Thread(target=f, name="nrsc5-" + f.__name__.strip("_"),
                                         daemon=True)
                        for f in (self._writer, self._reader, self._logger)]
        for t in self.threads:
            t.start()

    def feed(self, raw):
        try:
            self.q.put_nowait(raw)
        except queue.Full:
            self.dropped += 1

    def alive(self):
        return self.proc.poll() is None

    def _writer(self):
        fd = self.proc.stdin.fileno()
        while True:
            raw = self.q.get()
            if raw is None:
                break
            mv = memoryview(raw).cast("B")
            try:
                while mv:
                    mv = mv[os.write(fd, mv):]
            except OSError:  # nrsc5 has gone
                break
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def _reader(self):
        fd = self.proc.stdout.fileno()
        pend = b""
        while True:
            try:
                b = os.read(fd, 65536)
            except OSError:
                break
            if not b:
                break
            pend += b
            whole = len(pend) // FRAME_BYTES * FRAME_BYTES
            if whole:
                self._frames(pend[:whole])
                pend = pend[whole:]

    def _frames(self, data):
        s = np.frombuffer(data, "<i2").reshape(-1, FRAME, 2)
        zero = ~s.reshape(len(s), -1).any(axis=1)   # frames nrsc5 filled with silence
        mono = (s.astype(np.float32).mean(axis=2) / 32768.0).reshape(-1)
        a = self.rs(mono)
        with self.lock:
            start = self.audio.end
            # Each 44.1 kHz frame is 2048 * 160/147 ~ 2229 samples here.
            per = len(a) / len(s)
            for i in np.flatnonzero(zero):
                self.bad.append((start + int(i * per), start + int((i + 1) * per) + 1))
            self.frames += len(s)
            self.bad_frames += int(zero.sum())
            self.audio.write(a)
            self.corr.write(self.cf(a))

    def _logger(self):
        for line in iter(self.proc.stderr.readline, b""):
            self.log.parse(line.decode("utf-8", "replace"), self.clock())

    def bad_between(self, start, end):
        with self.lock:
            return any(s < end and e > start for s, e in self.bad)

    def stop(self):
        try:
            self.q.put_nowait(None)
        except queue.Full:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        except OSError:
            pass


# ------------------------------------------------------------------ alignment

def find_offset(analog, a0, hd, h0, min_overlap=3.0, rate=CORR_RATE):
    """Where hd's audio sits in analog's: (k, score, gain) or None.

    analog starts at index a0 of its stream and hd at h0 of its own, both at
    rate. k is the offset that lines them up, in those indices: hd sample h
    carries the audio of analog sample h + k. score is the normalised
    correlation at k (1 is identical), and gain what hd needs to match
    analog's level. None when nothing stands out.
    """
    x = np.asarray(analog, np.float64)
    y = np.asarray(hd, np.float64)
    nx, ny = len(x), len(y)
    need = int(min_overlap * rate)
    if nx < need or ny < need:
        return None
    nfft = 1 << int(np.ceil(np.log2(nx + ny)))
    c = np.fft.irfft(np.fft.rfft(x, nfft) * np.conj(np.fft.rfft(y, nfft)), nfft)
    # c[L] = sum_i x[i + L] y[i]: lag L from -(ny-1) to nx-1
    lags = np.concatenate([np.arange(0, nx), np.arange(-(ny - 1), 0)])
    c = np.concatenate([c[:nx], c[nfft - (ny - 1):]])
    # Energy of each signal over the overlap at every lag, by cumulative sums.
    cx = np.concatenate([[0], np.cumsum(x * x)])
    cy = np.concatenate([[0], np.cumsum(y * y)])
    i0 = np.maximum(0, -lags)                 # first y index overlapping
    i1 = np.minimum(ny, nx - lags)            # one past the last
    ov = i1 - i0
    ok = ov >= need
    ex = cx[np.clip(i1 + lags, 0, nx)] - cx[np.clip(i0 + lags, 0, nx)]
    ey = cy[i1] - cy[i0]
    den = np.sqrt(ex * ey) + 1e-20
    r = np.where(ok, c / den, 0.0)
    best = int(np.argmax(r))
    score = float(r[best])
    # Something else close to as good (away from the peak) means no lock.
    away = np.abs(lags - lags[best]) > rate // 20
    rival = float(np.max(r[away & ok])) if np.any(away & ok) else 0.0
    if score < 0.35 or rival > 0.8 * score:
        return None
    gain = float(np.sqrt(ex[best] / (ey[best] + 1e-20)))
    return (a0 - h0) + int(lags[best]), score, gain


def refine(analog_ring, hd_ring, k, a_end, span, seconds=1.5):
    """k (48 kHz) refined to the sample within +-span, on the latest audio both hold."""
    n = int(seconds * AUDIO_RATE)
    a_start = min(a_end, hd_ring.end + k) - n - span
    y = hd_ring.read(a_start - k, n)
    x = analog_ring.read(a_start - span, n + 2 * span)
    if x is None or y is None:
        return k
    win = np.lib.stride_tricks.sliding_window_view(x, n)
    c = win @ y
    return k + int(np.argmax(c)) - span


# ------------------------------------------------------------------ blending

class Blend:
    """The HD state machine. tick() once a block; .state is one of STATES.

    Digital only after nrsc5 has stayed clean (in sync, audio frames good,
    BER and MER past their thresholds, and lined up) for hold seconds; any
    fault drops straight back. Each drop doubles hold, up to HOLD_MAX, and
    HOLD_RESET seconds of unbroken digital bring it back to HOLD_MIN.
    """

    HOLD_MIN, HOLD_MAX, HOLD_RESET = 4.0, 64.0, 300.0
    BER_ENTER, BER_DROP = 2e-3, 2e-2        # nrsc5's pre-FEC bit error rate
    MER_ENTER, MER_DROP = 6.0, 3.0          # dB, the better of the two sidebands

    def __init__(self):
        self.state = "no signal"
        self.hold = self.HOLD_MIN
        self.clean_since = None
        self.digital_since = None
        self.reason = "No HD Radio signal."
        self.drops = 0

    def quality(self, log):
        """(fault, clean): fault says why to leave digital, clean whether to enter."""
        if not log.synced:
            return "nrsc5 lost sync", False
        best_mer = max(log.mer) if log.mer else None
        if log.ber is not None and log.ber > self.BER_DROP:
            return "the bit error rate is too high", False
        if best_mer is not None and best_mer < self.MER_DROP:
            return "the signal is too weak (MER %.1f dB)" % best_mer, False
        clean = (log.ber is not None and log.ber <= self.BER_ENTER and
                 best_mer is not None and best_mer >= self.MER_ENTER)
        return None, clean

    def tick(self, now, running, log=None, bad_audio=False, aligned=False, detected=False,
             backoff=False):
        prev = self.state
        if not running:
            self.clean_since = None
            if prev == "digital":
                self._drop(now)
            self.state = "no signal"
            self.reason = ("HD sidebands seen; waiting to try nrsc5 again." if backoff and detected
                           else "No HD Radio signal." if not detected
                           else "HD sidebands seen; starting nrsc5.")
            return self.state
        fault, clean = self.quality(log)
        if bad_audio and fault is None:
            fault = "HD audio frames were lost"
        if prev == "digital":
            if fault:
                self._drop(now)
                self.state = "analog fallback"
                self.reason = "Back on analog: %s." % fault
            elif not aligned:
                self._drop(now)
                self.state = "analog fallback"
                self.reason = "Back on analog: lining HD up with analog again."
            else:
                if now - self.digital_since >= self.HOLD_RESET:
                    self.hold = self.HOLD_MIN
                self.reason = "Playing HD Radio."
            return self.state
        # acquiring or analog fallback: count clean time toward the hold
        if fault or not clean:
            self.clean_since = None
        elif self.clean_since is None:
            self.clean_since = now
        if self.clean_since is not None and aligned and now - self.clean_since >= self.hold:
            self.state, self.digital_since = "digital", now
            self.reason = "Playing HD Radio."
            return self.state
        if prev != "analog fallback":
            self.state = "acquiring"
        if fault:
            why = fault if log.syncs else "nrsc5 is looking for the digital signal"
        elif not clean:
            why = "the digital signal isn't clean enough yet"
        elif not aligned:
            why = "lining HD up with analog"
        else:
            why = "checking it stays clean for %.0f s" % self.hold
        self.reason = ("Analog: %s." if self.state == "acquiring" else "Back on analog: %s.") % why
        return self.state

    def clean_for(self, now):
        if self.state == "digital":
            return now - self.digital_since
        return now - self.clean_since if self.clean_since is not None else 0.0

    def _drop(self, now):
        self.drops += 1
        self.hold = min(self.hold * 2, self.HOLD_MAX)
        self.digital_since = None
        self.clean_since = None


# ------------------------------------------------------------------ the glue

class Hd:
    """HD Radio for one WFM receiver: detect, run nrsc5, align and blend.

    process(analog, raw, now) takes the analog chain's 48 kHz audio for one
    IQ block and that block's raw cu8 bytes, and returns the audio to play:
    analog, HD, or a crossfade between them.
    """

    FADE = 0.3                # seconds, each way
    MARGIN = 0.3              # seconds of HD kept in hand ahead of the play point
    DETECT_N, DETECT_OF = 3, 5   # sidebands in 3 of the last 5 spectra
    ACQUIRE_TIMEOUT = 30.0    # no sync this long after starting: stop, back off
    FALLBACK_TIMEOUT = 90.0   # in fallback without sync this long: stop, back off
    BACKOFF_MIN, BACKOFF_MAX = 60.0, 600.0
    CHECK_EVERY = 15.0        # re-check the alignment while running
    RETRY_EVERY = 1.0         # try again this often until it lines up
    START_GRACE = 2.0         # before believing "no sync" after a start

    def __init__(self, cmd, program=0, clock=time.monotonic, spawn=None, analog_seconds=30.0):
        self.cmd = cmd
        self.program = int(program)
        self.clock = clock
        self.spawn = spawn or (lambda prog: Nrsc5(self.cmd, prog, clock=self.clock))
        self.proc = None
        self.blend = Blend()
        self.analog = Ring(analog_seconds, AUDIO_RATE)    # analog as demodulated
        self.analog_corr = Ring(analog_seconds, CORR_RATE)
        self.acf = corr_filter()
        self.detect = collections.deque(maxlen=self.DETECT_OF)
        self.side = {"present": False, "lower_db": None, "upper_db": None, "floor_db": None}
        self.backoff = self.BACKOFF_MIN
        self.retry_at = None
        self.started_at = None
        self.k = None             # hd index + k = analog index (48 kHz), this sync
        self.k_sync = None        # the nrsc5 sync count k was found in
        self.k_at = None
        self.gain = 1.0
        self.delay = 0            # samples the analog is delayed by (0 unless HD lags)
        self.play_lag = None      # HD2+: samples behind the HD ring's end the play point runs
        self.w = 0.0              # HD's share of the output, 0..1
        self.checking = False
        self.lead = None          # seconds HD arrives ahead of analog (negative: behind)
        self.k_cand = None        # a moved k seen once, waiting for a second look
        self.k_next = None        # a moved k confirmed, to cross over to
        self.moves = 0
        self.programs = []        # this station's programs, kept across nrsc5 restarts
        self.lock = threading.Lock()
        self.ended = None         # why the last nrsc5 run ended

    # --- detection, from the spectrum rxd makes every 0.2 s
    def spectrum(self, spec):
        self.side = sidebands(spec)
        self.detect.append(self.side["present"])

    @property
    def detected(self):
        return sum(self.detect) >= self.DETECT_N

    def set_program(self, program):
        program = int(program)
        if program != self.program:
            self.program = program
            self._stop("program changed")
            self.retry_at = None
            self.w = 0.0
            self.blend = Blend()   # a new start, not a fallback: no longer hold

    def retuned(self):
        """A new station: forget this one's nrsc5, detection, alignment and delay."""
        self._stop("retuned")
        self.detect.clear()
        self.retry_at = None
        self.backoff = self.BACKOFF_MIN
        self.blend = Blend()
        self.delay = 0
        self.w = 0.0
        self.lead = None
        self.ended = None
        self.programs = []

    # --- per block
    def process(self, analog, raw, now):
        n = len(analog)
        a0 = self.analog.end
        self.analog.write(analog)
        self.analog_corr.write(self.acf(analog))
        self._manage(now)
        p = self.proc
        if p is not None:
            if raw is not None:
                p.feed(raw)
            if self.program == 0:
                self._align(now)
        running = p is not None and p.alive()
        if p is not None and p.log.programs and len(p.log.programs) != len(self.programs):
            self.programs = p.log.program_list()
        aligned = self._aligned()
        # The analog delay the HD would need: HD coming out behind the analog
        # needs the analog delayed to match, which happens only on the way
        # into digital, so analog-first stays immediate.
        delay = self.delay
        if self.program == 0 and self.blend.state != "digital":
            delay = max(self.delay, self._delay_needed())
        bad = False
        hd = None
        moved = None
        if self.k_next is not None and self.program == 0:
            old_k, self.k = self.k, self.k_next
            self.k_next = None
            if running and aligned and self.blend.state == "digital":
                moved = p.audio.read(a0 - self.delay - old_k, n)
        if running and aligned:
            start = self._hd_start(a0, delay)
            fade = int(self.FADE * AUDIO_RATE)
            # A bad frame anywhere not yet played (HD usually runs seconds
            # ahead): leave now, so the fade is over long before it plays.
            bad = p.bad_between(start - fade, max(p.audio.end, start + n + fade))
            hd = p.audio.read(start, n)
            if hd is None:
                bad = True
            elif moved is not None:
                r = np.linspace(0, 1, n, dtype=np.float32)
                hd = (1 - r) * moved + r * hd
        st = self.blend.tick(now, running, p.log if p else None, bad_audio=bad,
                             aligned=aligned, detected=self.detected,
                             backoff=self.retry_at is not None and now < self.retry_at)
        target = 1.0 if st == "digital" and hd is not None else 0.0
        if self.program == 0:
            base = analog if self.delay == 0 else self.analog.read(a0 - self.delay, n)
            if base is None:
                base = np.zeros(n, np.float32)
            if target and delay > self.delay:
                # The analog steps back to meet the HD: one crossfade, over
                # this block, from the analog as it was to the HD.
                self.delay = delay
                self.w = 1.0
                ramp = np.linspace(0, 1, n, dtype=np.float32)
                return ((1 - ramp) * base + ramp * self.gain * hd).astype(np.float32)
        ramp = self.w + np.arange(1, n + 1) / (self.FADE * AUDIO_RATE) * \
            (1 if target > self.w else -1)
        ramp = np.clip(ramp, min(self.w, target), max(self.w, target)).astype(np.float32)
        self.w = float(ramp[-1]) if n else self.w
        if self.program == 0:
            if hd is None or not ramp.any():
                return base.astype(np.float32)
            return ((1 - ramp) * base + ramp * self.gain * hd).astype(np.float32)
        # HD2, HD3 ...: digital only
        if hd is None:
            return np.zeros(n, np.float32)
        return (ramp * hd).astype(np.float32)

    def _hd_start(self, a0, delay=None):
        if self.program == 0:
            return a0 - (self.delay if delay is None else delay) - self.k
        return self.proc.audio.end - self.play_lag

    def _delay_needed(self):
        """Analog delay (samples) so the HD the play point needs has arrived."""
        if self.k is None or self.lead is None:
            return self.delay
        behind = -self.lead * AUDIO_RATE + self.MARGIN * AUDIO_RATE + 4800
        return max(0, int(np.ceil(behind)))

    def _aligned(self):
        p = self.proc
        if p is None or not p.log.syncs:
            return False
        if self.program != 0:
            if self.play_lag is None and p.audio.end >= int(self.MARGIN * AUDIO_RATE) + FRAME * 2:
                self.play_lag = int(self.MARGIN * AUDIO_RATE) + 4800
            return self.play_lag is not None
        return self.k is not None and self.k_sync == p.log.syncs

    def _align(self, now):
        p = self.proc
        if self.checking or not p.log.synced or p.corr.end < 3 * CORR_RATE:
            return
        if self.k is None or self.k_sync != p.log.syncs:
            due = self.k_at is None or now - self.k_at >= self.RETRY_EVERY
        else:
            due = now - self.k_at >= self.CHECK_EVERY
        if not due:
            return
        self.checking = True
        sync = p.log.syncs
        a_end = self.analog.end
        h_end = p.audio.end
        if self.k is not None and self.k_sync == sync:
            # A recheck: 3 s of HD the analog already holds, searched +-0.25 s
            # around where it was, a few times cheaper than the first search.
            k8 = self.k // CORR_DECIM
            hn, span = 3 * CORR_RATE, CORR_RATE // 4
            hc_end = min(p.corr.end, self.analog_corr.end - k8 - span)
            h0 = hc_end - hn
            a0, an = h0 + k8 - span, hn + 2 * span
        else:
            # Only HD since nrsc5 last synchronised: older audio was another sync.
            hn = min(p.corr.end - p.corr.start, 12 * CORR_RATE)
            an = min(self.analog_corr.end - self.analog_corr.start, 24 * CORR_RATE)
            h0, a0 = p.corr.end - hn, self.analog_corr.end - an
        y, x = p.corr.read(h0, hn), self.analog_corr.read(a0, an)

        def work():
            try:
                got = find_offset(x, a0, y, h0) if x is not None and y is not None else None
                if got is None:
                    return
                k8, score, gain = got
                k = refine(self.analog, p.audio, k8 * CORR_DECIM, a_end, CORR_DECIM * 2)
                with self.lock:
                    if self.proc is not p or p.log.syncs != sync:
                        return
                    if self.k is not None and self.k_sync == sync and abs(k - self.k) > 2:
                        # The HD moved against the analog. Follow it once a
                        # second look agrees, with a short HD-to-HD crossfade.
                        if self.k_cand is not None and abs(k - self.k_cand) <= 2:
                            self.k_next, self.k_cand = k, None
                            self.moves += 1
                            sys.stderr.write("rxd: HD alignment moved %+d samples\n" % (k - self.k))
                            self.k_at = now
                        else:
                            self.k_cand = k
                            self.k_at = now - self.CHECK_EVERY + self.RETRY_EVERY
                        return
                    self.k_cand = None
                    self.k, self.k_sync, self.k_at = k, sync, now
                    self.gain = float(np.clip(gain, 0.1, 10.0))
                    # HD audio h is played at analog time h + k, and has
                    # arrived by analog time h + (a_end - h_end).
                    self.lead = (k - (a_end - h_end)) / AUDIO_RATE
            finally:
                if self.k is None:
                    self.k_at = now   # when it last tried
                self.checking = False

        threading.Thread(target=work, name="hd-align", daemon=True).start()

    def _manage(self, now):
        p = self.proc
        if p is None:
            if self.retry_at is not None and now < self.retry_at:
                return
            if self.program != 0 or self.detected:
                self._start(now)
            return
        if not p.alive():
            self._stop("nrsc5 exited%s" % (": " + p.log.last if p.log.last else ""))
            self._backoff(now)
            return
        if self.program != 0:
            return
        st = self.blend.state
        if st == "acquiring" and not p.log.syncs and now - self.started_at > self.ACQUIRE_TIMEOUT:
            self._stop("no HD sync in %.0f s" % self.ACQUIRE_TIMEOUT)
            self._backoff(now)
        elif st == "analog fallback" and not p.log.synced and \
                now - (p.log.sync_at or self.started_at) > self.FALLBACK_TIMEOUT:
            self._stop("HD lost for %.0f s" % self.FALLBACK_TIMEOUT)
            self._backoff(now)
        elif st == "digital":
            self.backoff = self.BACKOFF_MIN

    def _start(self, now):
        try:
            self.proc = self.spawn(self.program)
        except OSError as e:
            self.ended = "nrsc5 would not start: %s" % e
            self._backoff(now)
            return
        self.started_at = now
        self.retry_at = None
        self.k = self.k_sync = self.k_at = None
        self.k_cand = self.k_next = None
        self.play_lag = None
        self.lead = None

    def _stop(self, why):
        p, self.proc = self.proc, None
        if p is not None:
            p.stop()
            self.ended = why
        self.k = self.k_sync = None
        self.play_lag = None

    def _backoff(self, now):
        self.retry_at = now + self.backoff
        self.backoff = min(self.backoff * 2, self.BACKOFF_MAX)

    def close(self):
        self._stop("stopped")

    def status(self, now):
        p = self.proc
        log = p.log if p is not None else Nrsc5Log()
        st = self.blend.state
        reason = self.blend.reason
        if p is None and self.ended and self.retry_at is not None and now < self.retry_at:
            reason = "Analog only: %s; trying again later." % self.ended
        if self.program != 0:
            why = re.sub(r"^(Analog|Back on analog|Analog only): ", "", reason)
            why = why.replace("lining HD up with analog", "waiting for its audio")
            if not why.startswith("nrsc5"):
                why = why[:1].upper() + why[1:]
            reason = {"digital": "Playing HD%d." % (self.program + 1)}.get(
                st, "HD%d is digital-only: silent until HD Radio comes in. %s"
                % (self.program + 1, why))
        mer = {"lower": log.mer[0], "upper": log.mer[1]} if log.mer else None
        return {"state": st, "available": True, "reason": reason,
                "program": self.program, "programs": list(self.programs),
                "station": log.station, "slogan": log.slogan,
                "title": log.title, "artist": log.artist, "album": log.album,
                "mer_db": mer, "ber": log.ber, "synced": bool(p is not None and log.synced),
                "clean_s": round(self.blend.clean_for(now), 1),
                "hold_s": self.blend.hold,
                "sidebands": self.side, "running": p is not None,
                "analog_delay_s": round(self.delay / AUDIO_RATE, 3),
                "hd_lead_s": round(self.lead, 3) if self.lead is not None else None,
                "dropped": p.dropped if p is not None else 0,
                "realigned": self.moves,
                "bad_frames": p.bad_frames if p is not None else 0}


def off_status(reason, program=0):
    """The hd block when HD isn't running at all: not installed, turned off, not WFM."""
    return {"state": "off", "available": False, "reason": reason, "program": program,
            "programs": [], "station": None, "slogan": None, "title": None, "artist": None,
            "album": None, "mer_db": None, "ber": None, "synced": False, "clean_s": 0.0,
            "hold_s": Blend.HOLD_MIN, "sidebands": None, "running": False,
            "analog_delay_s": 0.0, "hd_lead_s": None, "dropped": 0, "realigned": 0,
            "bad_frames": 0}
