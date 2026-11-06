#!/usr/bin/env python3
"""rxd-tui: a terminal client for rxd, the RTL-SDR receiver daemon.

It talks only to rxd's HTTP API (GET /api/status, GET /api/spectrum and
POST /api/tune), by default at http://127.0.0.1:8073.

Keys:
  Left/Right  tune down/up by the step     Up/Down  larger/smaller step
  f           type a frequency             m        next mode
  [ ]         narrower/wider bandwidth     g G      gain down/up
  - +         volume down/up               ?        help      q  quit

A typed frequency is MHz when it is under 2000 (7.078, 146.52), kHz under
100000 (14074) and Hz above that (7078000); a unit (k, M, Hz) overrides this.

Status is shown with a symbol and a word as well as colour, and NO_COLOR is
honoured. --ascii draws without Unicode block characters.
"""
import argparse
import collections
import curses
import json
import locale
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8073"
STEPS = (10, 100, 1000, 5000, 12500, 100000, 1000000)  # same set as the web UI
DEFAULT_STEP = 1000
MODES = ("USB", "LSB", "AM", "FM", "WFM")
# Bandwidth presets for [ and ]. rxd's SSB filter runs from 200 Hz to 200 Hz
# plus the bandwidth, so 2800 is SDR++'s 3000 Hz filter.
BANDWIDTHS = {"USB": (500, 1000, 1800, 2400, 2800), "LSB": (500, 1000, 1800, 2400, 2800),
              "AM": (3000, 5000, 6000, 8000, 10000), "FM": (6250, 10000, 12500, 25000),
              "WFM": (100000, 150000, 200000)}
FREQ_MIN, FREQ_MAX = 100e3, 1.7e9      # rxd's tuning range
VOLUME_MAX = 4.0                       # rxd accepts 0 to 4
GAIN_FALLBACK_MAX = 50.0               # 1 dB steps when rxd reports no gain table
LEVEL_LO, LEVEL_HI = -110.0, -10.0     # signal bar span, dBFS

STATUS_EVERY = 0.5      # seconds: about 2 a second
SPECTRUM_EVERY = 0.25   # about 4 a second
RETRY_EVERY = 1.0       # while rxd is unreachable
NO_SPECTRUM_EVERY = 5.0  # while rxd has no /api/spectrum
TIMEOUT = 0.8           # per request
DROPPING_HOLD = 3.0     # "DROPPING" stays up this long after the count last rose
WATERFALL_MAX = 64


# ------------------------------------------------------------ pure logic

def parse_freq(text):
    """A typed frequency in Hz (an int). Raises ValueError with a readable message."""
    t = text.strip().lower().replace(" ", "").replace("_", "")
    m = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)(hz|khz|k|mhz|m|ghz|g)?", t)
    if not m:
        raise ValueError("not a frequency: %r (try 7.078, 14074 or 7078000)" % text.strip())
    v = float(m.group(1))
    unit = m.group(2)
    if unit:
        hz = v * {"hz": 1, "k": 1e3, "khz": 1e3, "m": 1e6, "mhz": 1e6,
                  "g": 1e9, "ghz": 1e9}[unit]
    elif v < 2000:
        hz = v * 1e6
    elif v < 100000:
        hz = v * 1e3
    else:
        hz = v
    hz = int(round(hz))
    if not FREQ_MIN <= hz <= FREQ_MAX:
        raise ValueError("%s MHz is outside rxd's range, 0.1 to 1700 MHz" % format_mhz(hz))
    return hz


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def step_tune(freq, step, direction):
    """Tune to the next multiple of step up (direction > 0) or down, within range."""
    n = freq / step
    k = math.floor(n + 1e-9) + 1 if direction > 0 else math.ceil(n - 1e-9) - 1
    return int(clamp(k * step, FREQ_MIN, FREQ_MAX))


def next_step(step, direction):
    i = min(range(len(STEPS)), key=lambda j: abs(STEPS[j] - step))
    return STEPS[clamp(i + (1 if direction > 0 else -1), 0, len(STEPS) - 1)]


def next_mode(mode):
    return MODES[(MODES.index(mode) + 1) % len(MODES)] if mode in MODES else MODES[0]


def next_bandwidth(mode, bw, direction):
    """The next preset wider or narrower than bw; bw itself at either end."""
    presets = BANDWIDTHS.get(mode, BANDWIDTHS["USB"])
    bw = float(bw or presets[0])
    if direction > 0:
        wider = [p for p in presets if p > bw + 0.5]
        return float(wider[0]) if wider else bw
    narrower = [p for p in presets if p < bw - 0.5]
    return float(narrower[-1]) if narrower else bw


def next_gain(gains, gain_db, direction):
    """The next of the dongle's gain steps, or 1 dB steps when there is no table."""
    gains = sorted(float(g) for g in (gains or []))
    if not gains:
        g = 0.0 if gain_db is None else float(gain_db)
        return round(clamp(g + (1 if direction > 0 else -1), 0.0, GAIN_FALLBACK_MAX), 1)
    if gain_db is None:
        return gains[len(gains) // 2]
    if direction > 0:
        up = [g for g in gains if g > gain_db + 0.05]
        return up[0] if up else float(gain_db)
    down = [g for g in gains if g < gain_db - 0.05]
    return down[-1] if down else float(gain_db)


def next_volume(volume, direction):
    """0.05 steps up to 1, 0.25 steps above it, within rxd's 0 to 4."""
    v = float(volume or 0)
    if direction > 0:
        v += 0.05 if v < 1 - 1e-9 else 0.25
    else:
        v -= 0.05 if v <= 1 + 1e-9 else 0.25
    return round(clamp(v, 0.0, VOLUME_MAX), 2)


def format_mhz(hz):
    """7078000 -> '7.078 000' (MHz, grouped in threes)."""
    hz = int(round(hz))
    whole, frac = divmod(abs(hz), 1000000)
    return "%s%d.%03d %03d" % ("-" if hz < 0 else "", whole, frac // 1000, frac % 1000)


def format_hz(hz):
    """A size in Hz with a unit: 10 Hz, 12.5 kHz, 1 MHz."""
    hz = float(hz)
    for div, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if abs(hz) >= div:
            return "%s %s" % (("%.3f" % (hz / div)).rstrip("0").rstrip("."), unit)
    return "%d Hz" % round(hz)


class Glyphs:
    """Characters for Unicode terminals, or plain ASCII."""

    def __init__(self, ascii_only=False):
        self.ascii = ascii_only
        if ascii_only:
            self.bars = " .-=#"
            self.shade = " .:-=+*#%@"
            self.level = ("#", "-", "")
            self.dial, self.band, self.left, self.right = "^", "=", "<", ">"
            self.rx, self.drop, self.bad, self.stop, self.wait = "*", "!", "x", "#", "~"
            self.keys = {"left": "<-", "right": "->", "up": "Up", "down": "Dn", "sep": "/"}
            self.ellipsis = "..."
        else:
            self.bars = " ▁▂▃▄▅▆▇█"
            self.shade = " ░▒▓█"
            self.level = ("█", "░", " ▏▎▍▌▋▊▉")
            self.dial, self.band, self.left, self.right = "▲", "━", "◀", "▶"
            self.rx, self.drop, self.bad, self.stop, self.wait = "●", "▲", "✕", "■", "…"
            self.keys = {"left": "←", "right": "→", "up": "↑", "down": "↓", "sep": ""}
            self.ellipsis = "…"


def resample(bins, start, stop, width, view_start=None, view_stop=None):
    """Bins spanning start..stop Hz -> width columns spanning the view (peak per column).

    Columns outside the bins' span are None, so an old waterfall row from
    before a retune lines up by frequency rather than by position."""
    n = len(bins)
    if not n or width <= 0 or stop <= start:
        return [None] * max(width, 0)
    vs = start if view_start is None else view_start
    ve = stop if view_stop is None else view_stop
    binw = (stop - start) / n
    colw = (ve - vs) / width
    out = []
    for c in range(width):
        f0 = vs + c * colw
        i0 = int(math.floor((f0 - start) / binw + 1e-9))
        i1 = int(math.ceil((f0 + colw - start) / binw - 1e-9))
        i1 = max(i1, i0 + 1)
        if i1 <= 0 or i0 >= n:
            out.append(None)
            continue
        out.append(max(bins[max(i0, 0):min(i1, n)]))
    return out


def db_range(values):
    """A display range: the noise floor (20th percentile) up to the strongest bin."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return -120.0, -60.0
    lo = v[len(v) // 5] - 3
    hi = max(v[-1] + 3, lo + 30)
    return lo, hi


def _frac(v, lo, hi):
    return clamp((v - lo) / (hi - lo), 0.0, 1.0) if hi > lo else 0.0


def render_spectrum(values, rows, lo, hi, g):
    """Columns of dB -> rows of bar characters, top row first."""
    chars = g.bars
    per_row = len(chars) - 1
    lines = [[] for _ in range(rows)]
    for v in values:
        if v is None:
            for line in lines:
                line.append(" ")
            continue
        total = max(1, int(round(_frac(v, lo, hi) * rows * per_row)))
        for r in range(rows):
            base = (rows - 1 - r) * per_row
            lines[r].append(chars[clamp(total - base, 0, per_row)])
    return ["".join(line) for line in lines]


def render_waterfall_row(values, lo, hi, g):
    top = len(g.shade) - 1
    return "".join(" " if v is None else g.shade[int(round(_frac(v, lo, hi) * top))]
                   for v in values)


def column_of(hz, start, stop, width):
    return int(math.floor((hz - start) / (stop - start) * width)) if stop > start else -1


def marker_row(width, start, stop, dial, passband, g):
    """The passband as a bar and the dial as a pointer, under the spectrum."""
    row = [" "] * width
    if stop <= start or width <= 0:
        return "".join(row)
    if passband:
        a = column_of(min(passband), start, stop, width)
        b = column_of(max(passband), start, stop, width)
        for c in range(max(a, 0), min(b, width - 1) + 1):
            row[c] = g.band
    c = column_of(dial, start, stop, width)
    if c < 0:
        row[0] = g.left
    elif c >= width:
        row[-1] = g.right
    else:
        row[c] = g.dial
    return "".join(row)


def axis_row(width, start, stop):
    """Frequency labels (MHz) centred on round values across the spectrum."""
    row = [" "] * width
    span = stop - start
    if span <= 0 or width < 8:
        return "".join(row)
    raw = span / max(1, width // 14)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    decimals = max(0, -int(math.floor(math.log10(step / 1e6))))
    f = math.ceil(start / step) * step
    last_end = -1
    while f <= stop:
        label = "%.*f" % (decimals, f / 1e6)
        c = column_of(f, start, stop, width) - len(label) // 2
        if c > last_end and c >= 0 and c + len(label) <= width:
            row[c:c + len(label)] = label
            last_end = c + len(label)
        f += step
    return "".join(row)


def level_bar(dbfs, width, g):
    """The signal level as a filled bar from LEVEL_LO to LEVEL_HI dBFS."""
    if width <= 0:
        return ""
    full, empty, parts = g.level
    if dbfs is None:
        return empty * width
    cells = _frac(dbfs, LEVEL_LO, LEVEL_HI) * width
    n = int(cells)
    bar = full * n
    if parts and n < width:
        p = int((cells - n) * len(parts))
        if p:
            bar += parts[p]
    return bar + empty * (width - len(bar))


# 3x5 digits; '#' is lit. Drawn with half blocks (3 rows) or full cells.
FONT = {
    "0": ("###", "#.#", "#.#", "#.#", "###"), "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"), "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"), "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"), "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"), "9": ("###", "#.#", "###", "..#", "###"),
    "-": ("...", "...", "###", "...", "..."), ".": (".", ".", ".", ".", "#"),
    " ": (".", ".", ".", ".", "."),
}


def big_text(text, scale, g):
    """Text in the big font: scale 1 is 3 rows (half blocks; 5 in ASCII), scale 2 is 5 rows."""
    cols = []  # one 5-pixel column at a time, with a blank column between glyphs
    for i, ch in enumerate(text):
        glyph = FONT.get(ch, FONT[" "])
        for x in range(len(glyph[0])):
            cols.append([glyph[y][x] == "#" for y in range(5)])
        if i < len(text) - 1:
            cols.append([False] * 5)
    if scale >= 2:
        on = "#" if g.ascii else "█"
        return ["".join((on * 2) if col[y] else "  " for col in cols) for y in range(5)]
    if g.ascii:
        return ["".join("#" if col[y] else " " for col in cols) for y in range(5)]
    pix = [[False] + col for col in cols]  # a blank pixel row on top: 6 rows -> 3
    half = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return ["".join(half[(p[2 * r], p[2 * r + 1])] for p in pix) for r in range(3)]


def badge(reachable, status, dropping, g):
    """(symbol, word, role) for the receiver's state. Never colour alone."""
    if reachable is None:
        return g.wait, "CONNECTING", "warn"
    if not reachable:
        return g.bad, "NO RXD", "bad"
    if status.get("error"):
        return g.bad, "ERROR", "bad"
    if status.get("running") is False:
        return g.stop, "STOPPED", "bad"
    if dropping:
        return g.drop, "DROPPING", "warn"
    return g.rx, "RX", "ok"


def help_items(g):
    k = g.keys
    return [(k["left"] + k["sep"] + k["right"], "tune"), (k["up"] + k["sep"] + k["down"], "step"),
            ("f", "freq"), ("m", "mode"), ("[ ]", "bandwidth"), ("g G", "gain"),
            ("- +", "volume"), ("?", "help"), ("q", "quit")]


def help_line(width, g):
    """[(text, role)] for the bottom line: full labels if they fit, then shorter."""
    items = help_items(g)
    short = {"bandwidth": "bw", "volume": "vol", "freq": "freq"}
    for labels, gap in (("full", "  "), ("short", "  "), ("short", " ")):
        parts = []
        for key, label in items:
            if labels == "short":
                label = short.get(label, label)
            if parts:
                parts.append((gap, "help"))
            parts += [(key, "key"), (" " + label, "help")]
        if sum(len(t) for t, _ in parts) <= width:
            return parts
    return parts  # the caller clips; '?' still opens the full list


def use_color(environ=os.environ):
    """NO_COLOR (any non-empty value) turns colour off: https://no-color.org."""
    return not environ.get("NO_COLOR")


def unicode_ok():
    enc = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
    return enc in ("utf8", "utf16", "utf32")


# ------------------------------------------------------------- talking to rxd

class Link:
    """rxd's HTTP API. Every call returns (code, doc) or raises OSError."""

    def __init__(self, url, timeout=TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                doc = json.loads(e.read() or b"{}")
            except ValueError:
                doc = {}
            return e.code, doc if isinstance(doc, dict) else {}
        except ValueError as e:  # a reply that isn't JSON
            raise OSError("bad reply from rxd: %s" % e)

    def get(self, path):
        return self.request(path)

    def post(self, path, body):
        return self.request(path, body)


def reason(exc):
    """A short why for an unreachable rxd."""
    r = getattr(exc, "reason", exc)
    if isinstance(r, ConnectionRefusedError):
        return "connection refused"
    if isinstance(r, (TimeoutError, OSError)) and "timed out" in str(r):
        return "no answer"
    text = getattr(r, "strerror", None) or str(r)
    return text.strip() or r.__class__.__name__


class Poller(threading.Thread):
    """Polls status and spectrum and sends tunes, off the curses thread."""

    def __init__(self, link, clock=time.monotonic):
        super().__init__(name="rxd-poller", daemon=True)
        self.link = link
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.changed = threading.Event()
        self.halt = threading.Event()
        self.reachable = None      # None until the first answer or failure
        self.why = ""
        self.status = {}
        self.spectrum = None
        self.spec_state = "waiting"  # waiting, ok or missing (no /api/spectrum)
        self.history = collections.deque(maxlen=WATERFALL_MAX)
        self.dropping_until = 0.0
        self.pending = {}
        self.acks = []             # (sent, error or None), drained by the UI

    # called from the UI thread
    def tune(self, **fields):
        with self.lock:
            self.pending.update(fields)
        self.wake.set()

    def take_acks(self):
        with self.lock:
            acks, self.acks = self.acks, []
        return acks

    def dropping(self):
        return self.clock() < self.dropping_until

    def stop(self):
        self.halt.set()
        self.wake.set()

    # the thread
    def run(self):
        next_status = next_spec = 0.0
        while not self.halt.is_set():
            self.wake.clear()
            with self.lock:
                req, self.pending = self.pending, {}
            if req:
                self._tune(req)
                next_status = self.clock() + STATUS_EVERY
                continue
            now = self.clock()
            if now >= next_status:
                ok = self._status()
                next_status = now + (STATUS_EVERY if ok else RETRY_EVERY)
            if now >= next_spec:
                if self.reachable:
                    self._spectrum()
                    next_spec = now + (NO_SPECTRUM_EVERY if self.spec_state == "missing"
                                       else SPECTRUM_EVERY)
                else:
                    next_spec = next_status
            self.wake.wait(max(0.01, min(next_status, next_spec) - self.clock()))

    def _offline(self, exc):
        with self.lock:
            self.reachable = False
            self.why = reason(exc)
            self.spectrum = None
        self.changed.set()

    def _set_status(self, doc):
        with self.lock:
            old = self.status.get("dropped")
            if old is not None and (doc.get("dropped") or 0) > old:
                self.dropping_until = self.clock() + DROPPING_HOLD
            self.status = doc
            self.reachable = True
            self.why = ""

    def _status(self):
        try:
            code, doc = self.link.get("/api/status")
        except OSError as e:
            self._offline(e)
            return False
        if code != 200:
            self._offline(OSError("HTTP %d from /api/status" % code))
            return False
        self._set_status(doc)
        self.changed.set()
        return True

    def _spectrum(self):
        try:
            code, doc = self.link.get("/api/spectrum")
        except OSError as e:
            self._offline(e)
            return
        with self.lock:
            if code == 404:
                self.spec_state, self.spectrum = "missing", None
            elif code != 200 or not doc.get("bins"):
                self.spec_state = "waiting"
            else:
                self.spec_state = "ok"
                if not self.spectrum or doc.get("seq") != self.spectrum.get("seq"):
                    self.history.appendleft((float(doc["start_hz"]), float(doc["stop_hz"]),
                                             doc["bins"]))
                self.spectrum = doc
        self.changed.set()

    def _tune(self, req):
        try:
            code, doc = self.link.post("/api/tune", req)
        except OSError as e:
            with self.lock:
                self.acks.append((req, "not sent: can't reach rxd (%s)" % reason(e)))
            self._offline(e)
            return
        with self.lock:
            if code == 200:
                self.acks.append((req, None))
            else:
                self.acks.append((req, doc.get("error") or "rxd refused it (HTTP %d)" % code))
        if code == 200:
            self._set_status(doc)
        else:
            self._status()
        self.changed.set()


# ------------------------------------------------------------------ drawing

class Canvas:
    """A plain-text screen, for tests and --snapshot."""

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.cells = [[" "] * w for _ in range(h)]
        self.roles = [[""] * w for _ in range(h)]

    def put(self, y, x, text, role=""):
        if not 0 <= y < self.h:
            return
        for i, ch in enumerate(text):
            if 0 <= x + i < self.w:
                self.cells[y][x + i] = ch
                self.roles[y][x + i] = role

    def text(self):
        return "\n".join("".join(r).rstrip() for r in self.cells)


class CursesScreen:
    ROLES = ("bar", "big", "dim", "label", "value", "ok", "warn", "bad", "spec", "wf",
             "marker", "key", "help", "msg", "err", "upconv")

    def __init__(self, win, color):
        self.win = win
        self.h, self.w = win.getmaxyx()
        self.attrs = self.make_attrs(color)

    @staticmethod
    def make_attrs(color):
        b, r, d = curses.A_BOLD, curses.A_REVERSE, getattr(curses, "A_DIM", 0)
        a = {"bar": r, "big": b, "dim": d, "label": 0, "value": b, "ok": b, "warn": b,
             "bad": b | r, "spec": 0, "wf": 0, "marker": b, "key": r, "help": 0,
             "msg": 0, "err": b, "upconv": b}
        if color:
            pairs = {"ok": curses.COLOR_GREEN, "warn": curses.COLOR_YELLOW,
                     "bad": curses.COLOR_RED, "err": curses.COLOR_RED,
                     "spec": curses.COLOR_CYAN, "marker": curses.COLOR_YELLOW,
                     "big": curses.COLOR_WHITE, "upconv": curses.COLOR_YELLOW}
            for n, (role, fg) in enumerate(sorted(pairs.items()), start=1):
                try:
                    curses.init_pair(n, fg, -1)
                    a[role] |= curses.color_pair(n)
                except curses.error:
                    pass
        return a

    def put(self, y, x, text, role=""):
        if not 0 <= y < self.h or x >= self.w:
            return
        text = text[:self.w - x]
        try:
            self.win.addstr(y, x, text, self.attrs.get(role, 0))
        except curses.error:
            pass  # writing the bottom-right cell moves the cursor off screen: harmless


class App:
    def __init__(self, poller, glyphs, url=DEFAULT_URL):
        self.p = poller
        self.g = glyphs
        self.url = url
        self.step = DEFAULT_STEP
        self.overrides = {}     # values sent but not yet confirmed by rxd
        self.message = ""       # the result of the last action
        self.message_role = "msg"
        self.entry = None       # the frequency being typed, or None
        self.show_help = False
        self.lo = self.hi = None  # smoothed spectrum range
        self.wf_cache = {}      # waterfall rows already resampled to the screen

    # ---- state
    def view(self):
        with self.p.lock:
            st = dict(self.p.status)
            spec = self.p.spectrum
            hist = list(self.p.history)
            reachable, why, spec_state = self.p.reachable, self.p.why, self.p.spec_state
        st.update(self.overrides)
        return st, spec, hist, reachable, why, spec_state

    def drain(self):
        for sent, err in self.p.take_acks():
            for k, v in sent.items():
                if self.overrides.get(k) == v:
                    del self.overrides[k]
            if err:
                self.say(err, "err")

    def say(self, text, role="msg"):
        self.message, self.message_role = text, role

    def send(self, **fields):
        self.overrides.update(fields)
        self.p.tune(**fields)

    # ---- keys; returns False to quit
    def key(self, ch):
        if self.show_help:
            self.show_help = False
            return True
        if self.entry is not None:
            return self.entry_key(ch)
        st = self.view()[0]
        if ch in (ord("q"), ord("Q")):
            return False
        self.say("")
        if ch == ord("?"):
            self.show_help = True
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            if st.get("freq") is None:
                return self.not_yet()
            d = 1 if ch == curses.KEY_RIGHT else -1
            f = step_tune(float(st["freq"]), self.step, d)
            if f == st["freq"]:
                self.say("At the %s of rxd's range" % ("top" if d > 0 else "bottom"))
            else:
                self.send(freq=f)
        elif ch in (curses.KEY_UP, curses.KEY_DOWN):
            self.step = next_step(self.step, 1 if ch == curses.KEY_UP else -1)
        elif ch == ord("f"):
            self.entry = ""
        elif ch == ord("m"):
            if st.get("mode") is None:
                return self.not_yet()
            self.send(mode=next_mode(st["mode"]))
            self.overrides.pop("bandwidth", None)
        elif ch in (ord("["), ord("]")):
            if st.get("mode") is None:
                return self.not_yet()
            d = 1 if ch == ord("]") else -1
            bw = next_bandwidth(st["mode"], st.get("bandwidth"), d)
            if bw == st.get("bandwidth"):
                self.say("Bandwidth at its %s for %s: %s" % (
                    "widest" if d > 0 else "narrowest", st["mode"], format_hz(bw)))
            else:
                self.send(bandwidth=bw)
        elif ch in (ord("g"), ord("G")):
            if not st:
                return self.not_yet()
            d = 1 if ch == ord("G") else -1
            g = next_gain(st.get("gains"), st.get("gain_db"), d)
            if g == st.get("gain_db"):
                self.say("Gain at its %s: %.1f dB" % ("maximum" if d > 0 else "minimum", g))
            else:
                self.send(gain_db=g)
        elif ch in (ord("-"), ord("_"), ord("+"), ord("=")):
            if st.get("volume") is None:
                return self.not_yet()
            d = 1 if ch in (ord("+"), ord("=")) else -1
            v = next_volume(st["volume"], d)
            if v == st["volume"]:
                self.say("Volume at its %s: %.2f" % ("maximum" if d > 0 else "minimum", v))
            else:
                self.send(volume=v)
        return True

    def not_yet(self):
        self.say("Not sent: no status from rxd yet", "err")
        return True

    def entry_key(self, ch):
        if ch in (27, 7):  # Esc, Ctrl-G
            self.entry = None
        elif ch in (10, 13, curses.KEY_ENTER):
            text, self.entry = self.entry, None
            if text.strip():
                try:
                    self.send(freq=parse_freq(text))
                except ValueError as e:
                    self.say(str(e), "err")
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.entry = self.entry[:-1]
        elif ch == 21:  # Ctrl-U
            self.entry = ""
        elif 32 <= ch < 127 and len(self.entry) < 20:
            self.entry += chr(ch)
        return True

    # ---- drawing
    def draw(self, scr):
        self.drain()
        st, spec, hist, reachable, why, spec_state = self.view()
        g, h, w = self.g, scr.h, scr.w
        if h < 10 or w < 30:
            scr.put(0, 0, "rxd-tui: too small", "err")
            scr.put(1, 0, "Needs 30x10; q quits")
            return
        stale = not reachable
        sym, word, role = badge(reachable, st, self.p.dropping(), g)

        # top bar
        scr.put(0, 0, " " * w, "bar")
        title = " rxd  " + (st.get("profile") or "")
        if self.url.rstrip("/") != DEFAULT_URL:
            title += "  @ " + self.url.split("//", 1)[-1]
        scr.put(0, 0, title[:w - 14], "bar")
        b = " %s %s " % (sym, word)
        scr.put(0, w - len(b) - 1, b, role)

        # big frequency, with mode, bandwidth and step beside it
        freq = st.get("freq")
        ftext = format_mhz(freq) if freq is not None else "-.--- ---"
        scale = 2 if (w >= 120 and h >= 36) else 1
        big = big_text(ftext, scale, g)
        y = 2 if h >= 20 else 1
        side = ["%s" % (st.get("mode") or "---"),
                "Bandwidth " + (format_hz(st["bandwidth"]) if st.get("bandwidth") else "---"),
                "Step " + format_hz(self.step)]
        bigw = len(big[0])
        if h >= 20 and bigw + 26 <= w:
            x0 = 2
            for i, line in enumerate(big):
                scr.put(y + i, x0, line, "dim" if stale else "big")
            scr.put(y + len(big) - 1, x0 + bigw + 2, "MHz", "label")
            sx = x0 + bigw + 8
            rows = [0, 1, 2] if len(big) == 3 else [0, 2, 4]
            scr.put(y + rows[0], sx, " %s " % side[0], "key")
            scr.put(y + rows[1], sx, side[1], "value")
            scr.put(y + rows[2], sx, side[2], "value")
            y += len(big) + 1
        else:
            scr.put(y, 1, ftext + " MHz", "dim" if stale else "big")
            scr.put(y, len(ftext) + 7, "  ".join([" %s " % side[0]] + side[1:]), "value")
            y += 2 if h >= 16 else 1

        # Ham It Up, gain, volume
        up = st.get("upconverter_hz")
        x = 1
        scr.put(y, x, "Ham It Up switch:", "label")
        x += 18
        if up is None:
            sw, srole = "---", "value"
        elif up > 0:
            sw, srole = "UPCONVERT (%s)" % format_hz(up), "upconv"
        else:
            sw, srole = "BYPASS", "value"
        scr.put(y, x, sw, srole)
        x += len(sw) + 4
        gain = st.get("gain_db")
        for label, val in (("Gain", "%.1f dB" % gain if gain is not None else "---"),
                           ("Volume", "%.2f" % st["volume"] if st.get("volume") is not None
                            else "---")):
            if x + len(label) + len(val) + 2 > w:
                break
            scr.put(y, x, label, "label")
            scr.put(y, x + len(label) + 1, val, "value")
            x += len(label) + len(val) + 5
        y += 1

        # signal level, and the receiver's health when there is room
        lvl = st.get("level_dbfs")
        scr.put(y, 1, "Signal", "label")
        ltext = "%6.1f dBFS" % lvl if lvl is not None else "  --- dBFS"
        scr.put(y, 8, ltext, "value")
        extra = ""
        if w >= 110 and st:
            extra = "DSP %d%%   Dropped %d" % (round((st.get("dsp_cpu") or 0) * 100),
                                              st.get("dropped") or 0)
        barw = clamp(w - 21 - (len(extra) + 4 if extra else 0), 4, 60)
        scr.put(y, 20, level_bar(lvl, barw, g), "spec")
        if extra:
            scr.put(y, 20 + barw + 4, extra, "label")
        y += 2 if h >= 30 else 1

        # spectrum, markers and waterfall
        bottom = h - 2           # message line; help on h - 1
        avail = bottom - y
        if avail >= 3:
            self.draw_spectrum(scr, y, avail, w, spec, hist, reachable, spec_state)

        # message line
        if self.entry is not None:
            for prompt in ("Frequency (7.078 MHz, 14074 kHz or 7078000 Hz), Enter tunes, "
                           "Esc cancels: ", "Frequency (MHz, kHz or Hz), Enter tunes, Esc cancels: ",
                           "Frequency: "):
                if len(prompt) + 14 <= w:
                    break
            scr.put(bottom, 0, prompt, "value")
            scr.put(bottom, len(prompt), self.entry + "_", "key")
        elif self.message:
            scr.put(bottom, 1, self.message, self.message_role)
        elif reachable is False:
            scr.put(bottom, 1, "%s Can't reach rxd at %s (%s). Retrying." % (
                g.bad, self.url, why), "err")
        elif st.get("error"):
            scr.put(bottom, 1, "rxd: %s" % st["error"], "err")

        # help line
        x = 1
        for text, r in help_line(w - 2, g):
            scr.put(h - 1, x, text, r)
            x += len(text)

        if self.show_help:
            self.draw_help(scr)

    def draw_spectrum(self, scr, y, avail, w, spec, hist, reachable, spec_state):
        g = self.g
        if not spec:
            text = {"missing": "No spectrum: this rxd has no /api/spectrum",
                    "waiting": "Waiting for the spectrum from rxd"}.get(spec_state)
            if not reachable:
                text = "No spectrum: rxd not reachable"
            scr.put(y + avail // 2, max(0, (w - len(text)) // 2), text, "dim")
            return
        start, stop = float(spec["start_hz"]), float(spec["stop_hz"])
        cols = resample(spec["bins"], start, stop, w)
        lo, hi = db_range(cols)
        if self.lo is None:
            self.lo, self.hi = lo, hi
        else:  # ease the scale so the picture doesn't jump
            self.lo += (lo - self.lo) * 0.2
            self.hi += (hi - self.hi) * 0.2
        rows = clamp(avail // 3, 1, 12)
        wf_rows = avail - 2 - rows
        scr.put(y, 0, axis_row(w, start, stop), "label")
        for i, line in enumerate(render_spectrum(cols, rows, self.lo, self.hi, g)):
            scr.put(y + 1 + i, 0, line, "spec")
        scr.put(y + 1 + rows, 0, marker_row(w, start, stop, spec.get("dial_hz"),
                                            spec.get("passband"), g), "marker")
        # Resampling is most of the drawing time; each row is done once per size.
        cache = {}
        for i, (s0, s1, bins) in enumerate(hist[:max(wf_rows, 0)]):
            key = (id(bins), w, start, stop)
            hit = self.wf_cache.get(key)
            cols_i = hit[1] if hit and hit[0] is bins else resample(bins, s0, s1, w, start, stop)
            cache[key] = (bins, cols_i)
            scr.put(y + 2 + rows + i, 0, render_waterfall_row(cols_i, self.lo, self.hi, g), "wf")
        self.wf_cache = cache

    def draw_help(self, scr):
        k = self.g.keys
        lines = [
            "rxd-tui keys",
            "",
            "%-9s tune down / up by the step" % (k["left"] + " " + k["right"]),
            "%-9s larger / smaller step: 10 Hz to 1 MHz" % (k["up"] + " " + k["down"]),
            "f         type a frequency and press Enter:",
            "            7.078 or 146.52 is MHz, 14074 is kHz,",
            "            7078000 is Hz; 7078k and 7.078M work too",
            "m         next mode: USB LSB AM FM WFM",
            "[ ]       narrower / wider bandwidth",
            "g G       gain down / up through the dongle's steps",
            "- +       volume down / up (0 to 4)",
            "?         this help",
            "q         quit (Ctrl-C works too)",
            "",
            "Ham It Up: the UPCONVERT/BYPASS switch on the box is",
            "physical. rxd only says which way it should be; the",
            "wrong way gives silence.",
            "",
            "Any key closes this.",
        ]
        bw = max(len(s) for s in lines) + 4
        bh = len(lines) + 2
        if bh > scr.h:
            lines = [s for s in lines if s][:scr.h - 2]
            bh = len(lines) + 2
        y0 = max(0, (scr.h - bh) // 2)
        x0 = max(0, (scr.w - bw) // 2)
        for i in range(bh):
            scr.put(y0 + i, x0, " " * bw, "bar")
        for i, s in enumerate(lines):
            scr.put(y0 + 1 + i, x0 + 2, s, "bar")


# --------------------------------------------------------------------- main

ESCAPES = {"[A": curses.KEY_UP, "OA": curses.KEY_UP, "[B": curses.KEY_DOWN,
           "OB": curses.KEY_DOWN, "[C": curses.KEY_RIGHT, "OC": curses.KEY_RIGHT,
           "[D": curses.KEY_LEFT, "OD": curses.KEY_LEFT}


def read_escape(win):
    """An arrow key whose sequence curses didn't recognise (the terminal not in the
    keypad mode terminfo expects), Esc on its own, or None for anything else."""
    win.nodelay(True)
    seq = ""
    try:
        while len(seq) < 8:
            c = win.getch()
            if c == -1:
                break
            seq += chr(c) if c < 256 else "?"
            if len(seq) >= 2 and (seq[-1].isalpha() or seq[-1] == "~"):
                break
    finally:
        win.nodelay(False)
        win.timeout(100)
    if not seq:
        return 27
    return ESCAPES.get(seq)


def run(stdscr, app, color):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if curses.has_colors():
        # curses.wrapper has started colour; keep the terminal's own
        # foreground and background either way, so NO_COLOR draws no colour.
        try:
            curses.use_default_colors()
        except curses.error:
            color = False
    else:
        color = False
    stdscr.keypad(True)
    stdscr.timeout(100)
    app.p.start()
    dirty = True
    while True:
        if dirty or app.p.changed.is_set():
            app.p.changed.clear()
            stdscr.erase()
            app.draw(CursesScreen(stdscr, color))
            stdscr.noutrefresh()
            curses.doupdate()
            dirty = False
        ch = stdscr.getch()
        if ch == -1:
            continue
        dirty = True
        if ch == 27:
            ch = read_escape(stdscr)
            if ch is None:
                continue
        if ch == curses.KEY_RESIZE:
            curses.update_lines_cols()
            stdscr.clear()
            continue
        if not app.key(ch):
            return


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rxd-tui", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("RXD_URL", DEFAULT_URL),
                    help="rxd's HTTP API (default $RXD_URL or %s)" % DEFAULT_URL)
    ap.add_argument("--ascii", action="store_true",
                    help="plain ASCII, no Unicode block characters")
    a = ap.parse_args(argv)
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # Esc cancels typing without a pause
    url = a.url if "//" in a.url else "http://" + a.url
    poller = Poller(Link(url))
    app = App(poller, Glyphs(a.ascii or not unicode_ok()), url)
    try:
        curses.wrapper(run, app, use_color())
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
