"""Tests for rxd-tui: python3 -m unittest rxd.test_rxd_tui  (stdlib only).

The logic is tested directly; then the real program runs in a pseudo-terminal
against a fake rxd, and a small VT100 emulator turns its output back into a
screen so the layout can be checked.
"""
import curses
import json
import math
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import fcntl
import threading
import time
import unittest
import urllib.parse
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rxd_tui as t  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
U = t.Glyphs(False)
A = t.Glyphs(True)
GAINS = (0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 12.5, 19.7, 29.7, 49.6)

# The profiles the fake rxd offers, as `radio --json` lists them.
PROFILES = [
    {"name": "ghostnet", "description": "Ghostnet", "freq": 7078000, "mode": "USB",
     "upconvert": True, "js8": True, "saved": True},
    {"name": "js8", "description": "JS8Call 20 m", "freq": 14078000, "mode": "USB",
     "upconvert": True, "js8": True, "saved": True},
    {"name": "fm-105.3", "description": "WBUR Boston", "freq": 105300000, "mode": "WFM",
     "upconvert": False, "js8": False, "saved": True},
    {"name": "noaa", "description": "NOAA Weather", "freq": 162550000, "mode": "FM",
     "upconvert": False, "js8": False, "saved": True},
    {"name": "2m", "description": "2 m calling", "freq": 146520000, "mode": "FM",
     "upconvert": False, "js8": False, "saved": True},
    {"name": "am-1030", "description": "WBZ", "freq": 1030000, "mode": "AM",
     "upconvert": True, "js8": False, "saved": True},
    {"name": "40m", "description": "40 m SSB", "freq": 7200000, "mode": "LSB",
     "upconvert": True, "js8": False, "saved": True},
]
BW = {"USB": 2800.0, "LSB": 2800.0, "AM": 10000.0, "FM": 12500.0, "WFM": 150000.0}


def state_for(name):
    """The status a fake rxd on profile name reports."""
    p = next(x for x in PROFILES if x["name"] == name)
    return {"profile": name, "freq": float(p["freq"]), "vfo_hz": p["freq"] - 100.0,
            "cal_hz": 100.0, "mode": p["mode"], "bandwidth": BW[p["mode"]], "gain_db": 3.7,
            "volume": 0.2, "squelch": -70.0 if name == "noaa" else None,
            "squelch_open": name != "2m", "offset_mode": "auto",
            "upconverter_hz": 125e6 if p["upconvert"] else 0.0, "upconvert_below_hz": 30e6,
            "level_dbfs": -62.4, "tuner_hz": 132028000.0, "offset_hz": 50000.0,
            "sample_rate": 240000, "dsp_cpu": 0.25, "blocks": 100, "dropped": 0,
            "error": None, "running": True, "gains": list(GAINS), "js8": p["js8"],
            "js8_call": "KA1PID" if p["js8"] else None,
            # ghostnet's clock is off, to show the warning; the rest are on GPS
            "clock": {"synced": False, "source": None, "offset_s": 3.2} if name == "ghostnet"
            else {"synced": True, "source": "GPS", "offset_s": 0.0},
            "listeners": 0}


def js8_lines(n=30):
    calls = ["W1AW", "K1ABC", "N0XQX", "VE3QXZ", "G4ABC", "WB2OQX"]
    out = []
    for i in range(n):
        fr = calls[i % len(calls)]
        to = "KA1PID" if i % 7 == 3 else ("@HB" if i % 2 else calls[(i + 2) % len(calls)])
        text = "%s: %s %s" % (fr, to, "HEARTBEAT FN42" if to == "@HB" else "SNR -%02d" % i)
        out.append({"seq": i + 1, "time": "2026-11-05 14:%02d:%02d" % (i // 4, (i * 15) % 60),
                    "freq": 7078000.0, "offset": 1000 + i, "snr": -i % 20 - 10,
                    "from": fr, "to": None if to.startswith("@") else to, "text": text})
    return out


# ------------------------------------------------------------- fake rxd

class FakeRxd:
    """An http.server speaking rxd's API on a random port. Records every POST.

    A profile switch really drops: the server stops listening for drop
    seconds, then comes back on the same port on the new profile. The audio
    stream is a 440 Hz tone."""

    def __init__(self, profile="40m", spectrum=True, profiles=True, gains=GAINS, drop=2.0,
                 port=0):
        self.state = state_for(profile)
        self.state["gains"] = list(gains)
        self.spectrum = spectrum
        self.has_profiles = profiles
        self.drop = drop
        self.posts = []          # POST /api/tune bodies
        self.switches = []       # POST /api/profile names
        self.audio_rates = []    # each GET /api/audio's rate
        self.js8 = js8_lines()
        self.listeners = 0
        self.seq = 0
        self.gen = 0             # bumped when the server goes down, to end streams
        self.lock = threading.Lock()
        self.closed = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, doc):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                q = urllib.parse.parse_qs(query)
                if path == "/api/status":
                    with fake.lock:
                        return self.send(200, dict(fake.state))
                if path == "/api/spectrum" and fake.spectrum:
                    return self.send(200, fake.make_spectrum())
                if path == "/api/profiles" and fake.has_profiles:
                    with fake.lock:
                        return self.send(200, {"active": fake.state["profile"],
                                               "profiles": [dict(p) for p in PROFILES]})
                if path == "/api/js8":
                    after = int(q.get("after", ["0"])[0])
                    with fake.lock:
                        lines = [x for x in fake.js8 if x["seq"] > after]
                        return self.send(200, {"seq": len(fake.js8), "lines": lines})
                if path == "/api/audio":
                    return self.audio(int(q.get("rate", ["24000"])[0]))
                self.send(404, {"error": "not found"})

            def audio(self, rate):
                if rate not in (12000, 24000, 48000):
                    return self.send(400, {"error": "rate must be one of (12000, 24000, 48000)"})
                with fake.lock:
                    if fake.listeners >= 4:
                        return self.send(429, {"error": "4 clients are already listening"})
                    fake.listeners += 1
                    fake.audio_rates.append(rate)
                    gen = fake.gen
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("X-Audio-Rate", str(rate))
                    self.end_headers()
                    n, k = rate // 10, 0
                    while gen == fake.gen:
                        chunk = array("h", (int(8000 * math.sin(2 * math.pi * 440 * (k + i) / rate))
                                            for i in range(n)))
                        k += n
                        self.wfile.write(chunk.tobytes())
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass
                finally:
                    with fake.lock:
                        fake.listeners -= 1

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                doc = json.loads(self.rfile.read(n))
                if self.path == "/api/profile":
                    if not fake.has_profiles:
                        return self.send(404, {"error": "not found"})
                    name = doc.get("name")
                    if name not in [p["name"] for p in PROFILES]:
                        return self.send(400, {"error": "no profile '%s'" % name})
                    with fake.lock:
                        fake.switches.append(name)
                    threading.Thread(target=fake.switch_to, args=(name,), daemon=True).start()
                    return self.send(202, {"switching": name})
                with fake.lock:
                    fake.posts.append(doc)
                    if "freq" in doc and not 100e3 <= doc["freq"] <= 1.7e9:
                        return self.send(400, {"error": "frequency out of range"})
                    if "mode" in doc and doc["mode"] != fake.state["mode"]:
                        fake.state["bandwidth"] = BW[doc["mode"]]
                    fake.state.update(doc)
                    fake.state["upconverter_hz"] = 125e6 if fake.state["freq"] < 30e6 else 0.0
                    return self.send(200, dict(fake.state))

        self.handler = H
        self.server = self.serve(port)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port

    def serve(self, port):
        srv = ThreadingHTTPServer(("127.0.0.1", port), self.handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def down(self):
        with self.lock:
            self.gen += 1
            srv, self.server = self.server, None
        if srv:
            srv.shutdown()
            srv.server_close()

    def switch_to(self, name):
        """What `radio NAME` does to rxd: it stops, then starts on the new profile."""
        time.sleep(0.3)
        self.down()
        time.sleep(self.drop)
        with self.lock:
            if self.closed:
                return
            gains = self.state["gains"]
            self.state = state_for(name)
            self.state["gains"] = gains
            self.server = self.serve(self.port)

    def make_spectrum(self):
        with self.lock:
            self.seq += 1
            return spectrum_for(self.state, self.seq)

    def close(self):
        self.closed = True
        self.down()


def spectrum_for(state, seq):
    """rxd's /api/spectrum around state's dial: noise, a signal in the passband and one beside."""
    dial, bw = state["freq"], state["bandwidth"]
    start, stop = dial - 50000 - 120000, dial - 50000 + 120000
    bins = []
    for i in range(512):
        f = start + (i + 0.5) * (stop - start) / 512
        v = -95 + 2 * math.sin(i * 1.7 + seq)            # noise
        v += 40 * math.exp(-((f - dial - 1500) / 600) ** 2)  # a signal in the passband
        v += 25 * math.exp(-((f - start - 30000) / 900) ** 2)
        bins.append(round(v, 1))
    return {"bins": bins, "start_hz": start, "stop_hz": stop, "dial_hz": dial,
            "passband": [dial + 200, dial + 200 + bw], "seq": seq}


# ------------------------------------------------- a small VT100 emulator

class VT:
    """Enough of xterm to replay what ncurses writes into a grid of characters."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.y = self.x = 0
        self.top, self.bot = 0, rows - 1
        self.wrap = False
        self.saved = (0, 0)
        self.buf = b""
        self.alt = False
        self.last = " "

    def text(self):
        return "\n".join("".join(r).rstrip() for r in self.grid)

    def scroll_up(self, n=1):
        for _ in range(n):
            del self.grid[self.top]
            self.grid.insert(self.bot, [" "] * self.cols)

    def scroll_down(self, n=1):
        for _ in range(n):
            del self.grid[self.bot]
            self.grid.insert(self.top, [" "] * self.cols)

    def feed(self, data):
        self.buf += data
        s = self.buf.decode("utf-8", "ignore") if self.buf else ""
        # keep a trailing incomplete UTF-8 or escape sequence for next time
        cut = len(self.buf)
        while cut > 0 and (self.buf[cut - 1] & 0xC0) == 0x80:
            cut -= 1
        if cut > 0 and self.buf[cut - 1] >= 0xC0:
            s = self.buf[:cut - 1].decode("utf-8", "ignore")
            self.buf = self.buf[cut - 1:]
        else:
            s = self.buf.decode("utf-8", "ignore")
            self.buf = b""
        m = re.search(r"\x1b(\[[0-9;?>=]*|[()*+])?$", s)
        if m:
            self.buf = s[m.start():].encode() + self.buf
            s = s[:m.start()]
        i = 0
        while i < len(s):
            c = s[i]
            if c == "\x1b":
                i = self.escape(s, i)
                continue
            self.char(c)
            i += 1

    def char(self, c):
        if c == "\r":
            self.x, self.wrap = 0, False
        elif c == "\n":
            self.index()
        elif c == "\b":
            self.x, self.wrap = max(0, self.x - 1), False
        elif c == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
        elif c < " " or c == "\x7f":
            pass
        else:
            if self.wrap:
                self.x, self.wrap = 0, False
                self.index()
            self.grid[self.y][self.x] = c
            self.last = c
            if self.x == self.cols - 1:
                self.wrap = True
            else:
                self.x += 1

    def index(self):
        self.wrap = False
        if self.y == self.bot:
            self.scroll_up()
        elif self.y < self.rows - 1:
            self.y += 1

    def escape(self, s, i):
        if i + 1 >= len(s):
            return i + 1
        c = s[i + 1]
        if c == "[":
            m = re.match(r"\x1b\[([0-9;?>=]*)([ -/]*)([@-~])", s[i:])
            if not m:
                return i + 2
            self.csi(m.group(1), m.group(3))
            return i + m.end()
        if c in "()*+":
            return i + 3
        if c == "]":  # OSC ... BEL or ST
            end = s.find("\x07", i)
            st = s.find("\x1b\\", i)
            ends = [e for e in (end, st) if e >= 0]
            return (min(ends) + (1 if min(ends) == end else 2)) if ends else len(s)
        if c == "7":
            self.saved = (self.y, self.x)
        elif c == "8":
            self.y, self.x = self.saved
        elif c == "M":
            if self.y == self.top:
                self.scroll_down()
            else:
                self.y = max(0, self.y - 1)
        elif c == "D":
            self.index()
        elif c == "E":
            self.x = 0
            self.index()
        return i + 2

    def csi(self, params, final):
        private = params.startswith("?")
        ps = [int(p) if p.isdigit() else None for p in params.lstrip("?>=").split(";")] \
            if params.lstrip("?>=") else []

        def p(n=0, d=1):
            return ps[n] if len(ps) > n and ps[n] is not None and ps[n] != 0 else d
        self.wrap = False
        if private:
            if final in "hl" and 1049 in ps:
                self.alt = final == "h"
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
            return
        if final in "Hf":
            self.y, self.x = min(p(0) - 1, self.rows - 1), min(p(1) - 1, self.cols - 1)
        elif final == "A":
            self.y = max(self.top if self.y >= self.top else 0, self.y - p())
        elif final == "B":
            self.y = min(self.bot if self.y <= self.bot else self.rows - 1, self.y + p())
        elif final == "C":
            self.x = min(self.cols - 1, self.x + p())
        elif final == "D":
            self.x = max(0, self.x - p())
        elif final == "G" or final == "`":
            self.x = min(self.cols - 1, p() - 1)
        elif final == "d":
            self.y = min(self.rows - 1, p() - 1)
        elif final == "K":
            mode = ps[0] if ps and ps[0] is not None else 0
            row = self.grid[self.y]
            rng = {0: range(self.x, self.cols), 1: range(0, self.x + 1),
                   2: range(self.cols)}.get(mode, [])
            for k in rng:
                row[k] = " "
        elif final == "J":
            mode = ps[0] if ps and ps[0] is not None else 0
            if mode == 0:
                for k in range(self.x, self.cols):
                    self.grid[self.y][k] = " "
                for r in range(self.y + 1, self.rows):
                    self.grid[r] = [" "] * self.cols
            elif mode == 1:
                for r in range(self.y):
                    self.grid[r] = [" "] * self.cols
                for k in range(self.x + 1):
                    self.grid[self.y][k] = " "
            else:
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
        elif final == "X":
            for k in range(self.x, min(self.cols, self.x + p())):
                self.grid[self.y][k] = " "
        elif final == "P":
            row = self.grid[self.y]
            n = min(p(), self.cols - self.x)
            del row[self.x:self.x + n]
            row.extend([" "] * n)
        elif final == "@":
            row = self.grid[self.y]
            n = min(p(), self.cols - self.x)
            for _ in range(n):
                row.insert(self.x, " ")
            del row[self.cols:]
        elif final == "L" and self.top <= self.y <= self.bot:
            for _ in range(p()):
                del self.grid[self.bot]
                self.grid.insert(self.y, [" "] * self.cols)
        elif final == "M" and self.top <= self.y <= self.bot:
            for _ in range(p()):
                del self.grid[self.y]
                self.grid.insert(self.bot, [" "] * self.cols)
        elif final == "S":
            self.scroll_up(p())
        elif final == "T":
            self.scroll_down(p())
        elif final == "r":
            self.top = p(0, 1) - 1
            self.bot = p(1, self.rows) - 1
            self.y = self.x = 0
        elif final == "b":
            for _ in range(p()):
                self.char(self.last)




# ------------------------------------------------ running the real program

class Pty:
    """rxd_tui.py in a pseudo-terminal of a given size, under a shell that records
    the terminal's settings (stty -g) before it starts and after it exits.
    TUI_PATH, if given, is the program's PATH (the shell keeps its own)."""

    SCRIPT = ('trap ":" INT; d="$1"; shift; stty -g > "$d/before"; '
              'PATH="${TUI_PATH:-$PATH}" "$@"; echo $? > "$d/code"; stty -g > "$d/after"')

    def __init__(self, url, rows=24, cols=80, env=None, args=()):
        self.vt = VT(rows, cols)
        self.dir = tempfile.mkdtemp(prefix="rxd-tui-test-")
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        e = dict(os.environ, TERM="xterm-256color", LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
        for k in ("NO_COLOR", "LINES", "COLUMNS", "TUI_PATH"):
            e.pop(k, None)
        e.update(env or {})
        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", self.SCRIPT, "sh", self.dir, sys.executable,
             os.path.join(HERE, "rxd_tui.py"), "--url", url] + list(args),
            stdin=slave, stdout=slave, stderr=slave, env=e,
            start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        self.slave = slave
        self.raw = b""

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            r, _, _ = select.select([self.master], [], [], left)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.raw += data
                self.vt.feed(data)

    def wait_for(self, text, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.pump(0.1)
            if text in self.vt.text():
                return True
        return False

    def send(self, data):
        os.write(self.master, data)

    def keys(self, *seq, gap=0.35):
        for k in seq:
            self.send(k)
            self.pump(gap)

    def resize(self, rows, cols):
        # The kernel sends SIGWINCH to the foreground process group.
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.vt = VT(rows, cols)

    def finish(self, timeout=5.0):
        """(exit code, stty before, stty after)."""
        end = time.monotonic() + timeout
        while self.proc.poll() is None and time.monotonic() < end:
            self.pump(0.1)
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        os.close(self.master)
        os.close(self.slave)

        def read(name):
            with open(os.path.join(self.dir, name)) as fh:
                return fh.read().strip()
        return int(read("code")), settings(read("before")), settings(read("after"))


def settings(stty):
    """stty -g output without macOS's PENDIN, a kernel flag for pending input
    that a program neither sets nor restores."""
    if not stty.startswith("gfmt1:"):
        return stty  # Linux: colon-separated hex, no PENDIN problem
    fields = dict(f.split("=", 1) for f in stty[6:].split(":"))
    fields["lflag"] = "%x" % (int(fields["lflag"], 16) & ~termios.PENDIN)
    return fields


# What a terminal sends once ncurses has switched it to keypad (application) mode.
KEYS = {"left": b"\x1bOD", "right": b"\x1bOC", "up": b"\x1bOA", "down": b"\x1bOB",
        "pgup": b"\x1b[5~", "pgdn": b"\x1b[6~", "enter": b"\r", "esc": b"\x1b"}


def fake_player(dirname, name="ffplay"):
    """A player script that records its arguments and pid and keeps what it's sent.

    ffplay's -h (the channel-layout probe) answers like ffmpeg 7."""
    os.makedirs(dirname, exist_ok=True)
    path = os.path.join(dirname, name)
    with open(path, "w") as fh:
        fh.write('#!/bin/sh\n'
                 'case "$*" in *-h*) echo "  -ch_layout  <channel_layout>"; exit 0;; esac\n'
                 'echo "$@" > "%(d)s/args"\n'
                 'echo $$ > "%(d)s/pid"\n'
                 'exec /bin/cat > "%(d)s/pcm"\n' % {"d": dirname})
    os.chmod(path, 0o755)
    return path


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ------------------------------------------------------------------ tests

class ParseFreq(unittest.TestCase):
    def test_mhz_khz_hz(self):
        self.assertEqual(t.parse_freq("7.078"), 7078000)
        self.assertEqual(t.parse_freq("146.52"), 146520000)
        self.assertEqual(t.parse_freq("7"), 7000000)
        self.assertEqual(t.parse_freq("14074"), 14074000)
        self.assertEqual(t.parse_freq("7078000"), 7078000)
        self.assertEqual(t.parse_freq(" 7.078 000 "), 7078000)
        self.assertEqual(t.parse_freq(".540"), 540000)

    def test_units(self):
        self.assertEqual(t.parse_freq("7078k"), 7078000)
        self.assertEqual(t.parse_freq("7078 kHz"), 7078000)
        self.assertEqual(t.parse_freq("7.078M"), 7078000)
        self.assertEqual(t.parse_freq("162.55mhz"), 162550000)
        self.assertEqual(t.parse_freq("1.2G"), 1200000000)
        self.assertEqual(t.parse_freq("540000Hz"), 540000)

    def test_rejects(self):
        for bad in ("", "abc", "7.0.7", "-7", "7,078", "50Hz", "2G", "1e6"):
            with self.assertRaises(ValueError, msg=bad):
                t.parse_freq(bad)
        with self.assertRaisesRegex(ValueError, "outside"):
            t.parse_freq("50k")


class Tuning(unittest.TestCase):
    def test_step_tune_snaps_to_the_grid(self):
        self.assertEqual(t.step_tune(7078000, 1000, 1), 7079000)
        self.assertEqual(t.step_tune(7078000, 1000, -1), 7077000)
        self.assertEqual(t.step_tune(7078123, 1000, 1), 7079000)
        self.assertEqual(t.step_tune(7078123, 1000, -1), 7078000)
        self.assertEqual(t.step_tune(146520000, 12500, 1), 146525000)
        self.assertEqual(t.step_tune(146520000, 12500, -1), 146512500)
        self.assertEqual(t.step_tune(7078000, 10, 1), 7078010)

    def test_bounds(self):
        self.assertEqual(t.step_tune(100000, 1000, -1), 100000)
        self.assertEqual(t.step_tune(150000, 1000000, -1), 100000)
        self.assertEqual(t.step_tune(1.7e9, 1000, 1), 1700000000)
        self.assertEqual(t.step_tune(1699999000, 1000000, 1), 1700000000)

    def test_steps(self):
        self.assertEqual(t.STEPS, (10, 100, 1000, 5000, 12500, 100000, 1000000))
        self.assertEqual(t.next_step(1000, 1), 5000)
        self.assertEqual(t.next_step(5000, 1), 12500)
        self.assertEqual(t.next_step(1000000, 1), 1000000)
        self.assertEqual(t.next_step(10, -1), 10)
        self.assertEqual(t.next_step(12500, -1), 5000)

    def test_mode_bandwidth_gain_volume(self):
        self.assertEqual(t.next_mode("USB"), "LSB")
        self.assertEqual(t.next_mode("WFM"), "USB")
        self.assertEqual(t.next_bandwidth("USB", 2800, -1), 2400)
        self.assertEqual(t.next_bandwidth("USB", 2800, 1), 2800)   # widest
        self.assertEqual(t.next_bandwidth("USB", 2600, 1), 2800)   # off-preset
        self.assertEqual(t.next_bandwidth("FM", 12500, 1), 25000)
        self.assertEqual(t.next_bandwidth("AM", 3000, -1), 3000)
        gains = [0.0, 0.9, 3.7, 49.6]
        self.assertEqual(t.next_gain(gains, 3.7, 1), 49.6)
        self.assertEqual(t.next_gain(gains, 3.7, -1), 0.9)
        self.assertEqual(t.next_gain(gains, 49.6, 1), 49.6)
        self.assertEqual(t.next_gain(gains, 2.0, 1), 3.7)          # off-table
        self.assertEqual(t.next_gain([], 3.7, 1), 4.7)             # no table: 1 dB
        self.assertEqual(t.next_gain([], 0.0, -1), 0.0)
        self.assertEqual(t.next_volume(0.2, 1), 0.25)
        self.assertEqual(t.next_volume(0.0, -1), 0.0)
        self.assertEqual(t.next_volume(1.0, 1), 1.25)
        self.assertEqual(t.next_volume(1.25, -1), 1.0)
        self.assertEqual(t.next_volume(4.0, 1), 4.0)


class Formatting(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(t.format_mhz(7078000), "7.078 000")
        self.assertEqual(t.format_mhz(146520000), "146.520 000")
        self.assertEqual(t.format_mhz(540000), "0.540 000")
        self.assertEqual(t.format_hz(10), "10 Hz")
        self.assertEqual(t.format_hz(12500), "12.5 kHz")
        self.assertEqual(t.format_hz(2800), "2.8 kHz")
        self.assertEqual(t.format_hz(1e6), "1 MHz")
        self.assertEqual(t.format_hz(125e6), "125 MHz")
        self.assertEqual(t.short_mhz(105300000), "105.3 MHz")
        self.assertEqual(t.short_mhz(7078000), "7.078 MHz")
        self.assertEqual(t.station_freq(105300000, "WFM"), ("105.3", "MHz"))
        self.assertEqual(t.station_freq(1030000, "AM"), ("1030", "kHz"))
        self.assertEqual(t.station_freq(162550000, "FM"), ("162.55", "MHz"))
        self.assertEqual(t.percent(0.2), "20 %")
        self.assertEqual(t.clip("abcdef", 4, U), "abc…")
        self.assertEqual(t.clip("abcdef", 5, A), "ab...")
        self.assertEqual(t.clip("abc", 4, U), "abc")

    def test_badges_have_a_symbol_and_a_word(self):
        cases = [((None, {}, False), "CONNECTING"), ((False, {}, False), "NO RXD"),
                 ((True, {"error": "dongle gone"}, False), "ERROR"),
                 ((True, {"running": False}, False), "STOPPED"),
                 ((True, {"running": True}, True), "DROPPING"),
                 ((True, {"running": True}, False), "RX")]
        for g in (U, A):
            for args, word in cases:
                sym, w, role = t.badge(*args, g)
                self.assertEqual(w, word)
                self.assertTrue(sym.strip())
                if g is A:
                    self.assertTrue(sym.isascii())
        self.assertEqual(t.badge(False, {}, False, U)[:2], ("✕", "NO RXD"))
        self.assertEqual(t.badge(True, {}, True, U)[:2], ("▲", "DROPPING"))
        self.assertEqual(t.badge(True, {}, False, U)[:2], ("●", "RX"))
        self.assertEqual(t.badge(False, {}, False, U, {"name": "js8"})[:2], ("■", "SWITCHING"))

    def test_no_color(self):
        self.assertFalse(t.use_color({"NO_COLOR": "1"}))
        self.assertTrue(t.use_color({"NO_COLOR": ""}))
        self.assertTrue(t.use_color({}))

    def test_monochrome_attrs_still_mark_status(self):
        # Without colour every status role still has bold or reverse.
        a = t.CursesScreen.make_attrs(False)
        for role in ("ok", "warn", "bad", "err", "key", "bar", "upconv", "sel", "mine",
                     "tabon", "head"):
            self.assertTrue(a[role] & (curses.A_BOLD | curses.A_REVERSE), role)
        self.assertTrue(a["mine"] & curses.A_REVERSE)

    def test_big_font(self):
        for scale, g, rows in ((1, U, 3), (2, U, 5), (1, A, 5), (2, A, 5)):
            lines = t.big_text("7.078 000", scale, g)
            self.assertEqual(len(lines), rows)
            self.assertEqual(len({len(s) for s in lines}), 1)
            if g is A:
                self.assertTrue(all(s.isascii() for s in lines))
        self.assertEqual(t.big_text("0", 1, U), ["▄▄▄", "█ █", "█▄█"])
        self.assertEqual(len(t.big_text("7.078 000", 1, U)[0]), 31)

    def test_help_line_fits_and_keeps_view_help_and_quit(self):
        for g in (U, A):
            for view, mode in (("js8", "USB"), ("listen", "FM"), ("listen", "WFM"),
                               ("manual", "USB")):
                for listening in (False, True):
                    items = t.help_items(g, view, mode, listening)
                    for w in (60, 78, 100, 158):
                        parts = t.help_line(w, g, items)
                        keys = [s for s, r in parts if r == "key"]
                        self.assertEqual(keys[-3:], ["1 2 3", "?", "q"], (view, w))
                        self.assertLessEqual(sum(len(s) for s, _ in parts), w, (view, w))
                    full = [s for s, r in t.help_line(158, g, items) if r == "key"]
                    self.assertEqual(len(full), len(items))
        listen = [k for k, _ in t.help_items(U, "listen", "FM", True)]
        self.assertIn("s , .", listen)
        self.assertIn("[ ]", listen)
        self.assertNotIn("s , .", [k for k, _ in t.help_items(U, "listen", "WFM")])
        self.assertIn(" bandwidth", [s for s, _ in t.help_line(158, U)])


class Views(unittest.TestCase):
    def test_profile_and_status_kinds(self):
        kinds = {p["name"]: t.profile_kind(p) for p in PROFILES}
        self.assertEqual(kinds, {"ghostnet": "js8", "js8": "js8", "fm-105.3": "listen",
                                 "noaa": "listen", "2m": "listen", "am-1030": "listen",
                                 "40m": "manual"})
        self.assertEqual(t.status_kind(state_for("ghostnet")), "js8")
        self.assertEqual(t.status_kind(state_for("noaa")), "listen")
        self.assertEqual(t.status_kind(state_for("40m")), "manual")
        self.assertIsNone(t.status_kind({}))

    def test_grouping(self):
        groups = t.group_stations(PROFILES)
        self.assertEqual([(title, [p["name"] for p in members]) for title, members in groups],
                         [("Broadcast FM", ["fm-105.3"]), ("VHF · UHF", ["noaa", "2m"]),
                          ("AM", ["am-1030"])])
        self.assertEqual([p["name"] for p in t.picker_list("listen", PROFILES)],
                         ["fm-105.3", "noaa", "2m", "am-1030"])
        self.assertEqual([p["name"] for p in t.picker_list("js8", PROFILES)],
                         ["ghostnet", "js8"])
        self.assertEqual(t.picker_list("manual", PROFILES), [])
        only_am = [p for p in PROFILES if p["mode"] == "AM"]
        self.assertEqual([title for title, _ in t.group_stations(only_am)], ["AM"])
        self.assertEqual(t.group_stations([]), [])

    def test_bandwidth_presets(self):
        self.assertEqual(t.LISTEN_BW, {"WFM": (100e3, 150e3, 200e3), "FM": (8e3, 12.5e3, 16e3),
                                       "AM": (6e3, 10e3, 15e3)})
        self.assertEqual(t.cycle_bandwidth("WFM", 150000), 200000)
        self.assertEqual(t.cycle_bandwidth("WFM", 200000), 100000)   # round again
        self.assertEqual(t.cycle_bandwidth("FM", 8000), 12500)
        self.assertEqual(t.cycle_bandwidth("FM", 12500), 16000)
        self.assertEqual(t.cycle_bandwidth("AM", 10000), 15000)
        self.assertEqual(t.cycle_bandwidth("AM", 7000), 10000)       # off a preset: wider
        self.assertEqual(t.cycle_bandwidth("AM", 20000), 6000)
        self.assertIsNone(t.cycle_bandwidth("USB", 2800))
        self.assertEqual(t.bandwidth_name("WFM", 100000), "Narrow")
        self.assertEqual(t.bandwidth_name("FM", 12500), "Normal")
        self.assertEqual(t.bandwidth_name("AM", 15000), "Wide")
        self.assertEqual(t.bandwidth_name("AM", 7000), "Custom")

    def test_squelch_keys(self):
        # s toggles; off remembers the level; , and . move it 2 dB
        self.assertEqual(t.squelch_key("s", None, -60.0), (-60.0, -60.0, ""))
        self.assertEqual(t.squelch_key("s", -70.0, -60.0), (None, -70.0, ""))
        self.assertEqual(t.squelch_key(".", -70.0, -70.0), (-68.0, -68.0, ""))
        self.assertEqual(t.squelch_key(",", -70.0, -70.0), (-72.0, -72.0, ""))
        send, level, msg = t.squelch_key(".", None, -60.0)
        self.assertIs(send, t.NOSEND)
        self.assertIn("s turns it on", msg)
        send, level, msg = t.squelch_key(".", 0.0, 0.0)
        self.assertIs(send, t.NOSEND)
        self.assertIn("highest", msg)
        self.assertEqual(t.squelch_key(",", -119.0, -119.0)[0], -120.0)

    def test_squelch_words(self):
        self.assertEqual(t.squelch_words({"squelch": None}, U), ("Off, always open", "value"))
        self.assertEqual(t.squelch_words({"squelch": -60.0, "squelch_open": True}, U),
                         ("-60 dBFS  ● Open (signal)", "ok"))
        text, role = t.squelch_words({"squelch": -60.0, "squelch_open": False}, A)
        self.assertEqual(text, "-60 dBFS  # Closed (quiet)")

    def test_clock_words(self):
        self.assertEqual(t.clock_words({"synced": True, "source": "GPS"}, U),
                         ("● Clock synced (GPS)", "ok"))
        self.assertEqual(t.clock_words({"synced": True, "source": "network"}, U)[0],
                         "● Clock synced (network)")
        text, role = t.clock_words({"synced": False, "source": None, "offset_s": -3.24}, U)
        self.assertTrue(text.startswith("✕ Clock not synced: JS8 won't decode"), text)
        self.assertIn("3.2 s off", text)
        self.assertEqual(role, "bad")
        self.assertEqual(t.clock_words({"synced": False}, U)[0],
                         "✕ Clock not synced: JS8 won't decode")
        for c in ({"synced": None}, None, {}):
            self.assertEqual(t.clock_words(c, U), ("? Clock unknown", "warn"))
        for c in ({"synced": True, "source": "GPS"}, {"synced": False}, None):
            self.assertTrue(t.clock_words(c, A)[0].isascii())

    def test_flip_needed(self):
        fm = PROFILES[2]
        self.assertTrue(t.flip_needed(fm, {"upconverter_hz": 125e6}))
        self.assertFalse(t.flip_needed(fm, {"upconverter_hz": 0.0}))
        self.assertFalse(t.flip_needed(fm, {}))


class Js8Lines(unittest.TestCase):
    LINE = {"seq": 1, "time": "2026-11-05 14:03:15", "snr": -7, "from": "W1AW", "to": "KA1PID",
            "text": "W1AW: KA1PID SNR -07"}

    def test_format(self):
        text, mine = t.format_js8(self.LINE, "KA1PID", 78, U)
        self.assertEqual(text, "14:03:15  -7  W1AW      ◆ KA1PID SNR -07")
        self.assertTrue(mine)
        text, mine = t.format_js8(dict(self.LINE, to="@HB", text="W1AW: @HB HEARTBEAT"), "KA1PID",
                                  78, A)
        self.assertEqual(text, "14:03:15  -7  W1AW      @HB HEARTBEAT")
        self.assertFalse(mine)
        text, _ = t.format_js8(dict(self.LINE, snr=None), "KA1PID", 20, A)
        self.assertEqual(len(text), 20)
        self.assertTrue(text.endswith("..."))
        self.assertTrue(t.JS8_HEADER.startswith("UTC      SNR  From"))

    def test_mine(self):
        self.assertTrue(t.js8_mine({"to": "ka1pid"}, "KA1PID"))
        self.assertTrue(t.js8_mine({"from": "W1AW", "text": "W1AW: @ALLCALL KA1PID/P QRV?"},
                                   "KA1PID/P"))
        self.assertTrue(t.js8_mine({"from": "W1AW", "text": "W1AW: GE KA1PID 73"}, "KA1PID"))
        self.assertFalse(t.js8_mine({"from": "W1AW", "text": "W1AW: GE KA1PIDX"}, "KA1PID"))
        # the owner's own transmissions aren't to them
        self.assertFalse(t.js8_mine({"from": "KA1PID", "text": "KA1PID: @HB HEARTBEAT"}, "KA1PID"))
        self.assertFalse(t.js8_mine(self.LINE, None))
        self.assertTrue(t.js8_mine({"mycall": True}, None))

    def test_filter_and_count(self):
        lines = js8_lines(30)
        self.assertEqual(t.js8_filter(lines, ""), lines)
        w1 = t.js8_filter(lines, " w1aw ")
        self.assertTrue(w1)
        self.assertTrue(all("W1AW" in t.js8_key(x) for x in w1))
        self.assertLess(len(w1), len(lines))
        self.assertEqual(t.js8_filter(lines, "NOBODY"), [])
        self.assertEqual(t.stations_heard(lines), 6)
        self.assertEqual(t.stations_heard([]), 0)


class Audio(unittest.TestCase):
    def test_rates(self):
        self.assertEqual(t.rate_for("WFM"), 24000)
        for m in ("FM", "AM", "USB", "LSB", None):
            self.assertEqual(t.rate_for(m), 12000)

    def test_find_player_in_order(self):
        have = {"aplay": "/usr/bin/aplay", "ffplay": "/opt/homebrew/bin/ffplay",
                "play": "/opt/homebrew/bin/play"}
        self.assertEqual(t.find_player(have.get), ("aplay", "/usr/bin/aplay"))
        del have["aplay"]
        self.assertEqual(t.find_player(have.get), ("ffplay", "/opt/homebrew/bin/ffplay"))
        self.assertEqual(t.find_player({"pw-cat": "/p", "play": "/q"}.get), ("pw-cat", "/p"))
        self.assertIsNone(t.find_player({}.get))
        self.assertIn("brew install ffmpeg", t.no_player_hint("darwin"))
        self.assertIn("ffmpeg or sox", t.no_player_hint("linux"))

    def test_player_commands(self):
        r = 12000
        self.assertEqual(t.player_command("pw-cat", "pw-cat", r),
                         ["pw-cat", "--playback", "--raw", "--format", "s16", "--rate", "12000",
                          "--channels", "1", "-"])
        self.assertEqual(t.player_command("paplay", "paplay", r),
                         ["paplay", "--raw", "--format=s16le", "--rate=12000", "--channels=1"])
        self.assertEqual(t.player_command("aplay", "aplay", r),
                         ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", "12000", "-c", "1"])
        self.assertEqual(t.player_command("ffplay", "ffplay", 24000, ["-ch_layout", "mono"]),
                         ["ffplay", "-nodisp", "-loglevel", "quiet", "-f", "s16le", "-ar", "24000",
                          "-ch_layout", "mono", "-"])
        self.assertEqual(t.player_command("ffplay", "ffplay", r)[-3:], ["-ac", "1", "-"])
        self.assertEqual(t.player_command("play", "play", r),
                         ["play", "-q", "-t", "raw", "-e", "signed", "-b", "16", "-c", "1",
                          "-r", "12000", "-"])
        with self.assertRaises(ValueError):
            t.player_command("vlc", "vlc", r)

    def test_ffplay_channel_probe(self):
        self.assertEqual(t.ffplay_channels("  -ch_layout  <channel_layout> (default mono)"),
                         ["-ch_layout", "mono"])
        self.assertEqual(t.ffplay_channels("  -channels  <int>"), ["-ac", "1"])
        d = tempfile.mkdtemp(prefix="rxd-tui-ffplay-")
        path = fake_player(d)
        p = t.Player.found("ffplay", path)
        self.assertEqual(p.name, "ffplay")
        self.assertEqual(p.build(12000), [path, "-nodisp", "-loglevel", "quiet", "-f", "s16le",
                                          "-ar", "12000", "-ch_layout", "mono", "-"])
        self.assertEqual(t.probe_ffplay(os.path.join(d, "missing")), ["-ac", "1"])

    def test_custom_player(self):
        p = t.Player.custom("ffmpeg -f s16le -ar {rate} -i - out.wav")
        self.assertEqual(p.name, "ffmpeg")
        self.assertEqual(p.build(24000), ["ffmpeg", "-f", "s16le", "-ar", "24000", "-i", "-",
                                          "out.wav"])
        self.assertEqual(t.custom_command("'/my dir/player' --rate={rate}", 12000),
                         ["/my dir/player", "--rate=12000"])
        with self.assertRaises(ValueError):
            t.Player.custom("  ")

    def test_volume_scaling(self):
        def pcm(*v):
            return array("h", v).tobytes()

        def samples(b):
            a = array("h")
            a.frombytes(b)
            return list(a)
        data = pcm(0, 1000, -1000, 20000, -20000, 32767, -32768)
        self.assertIs(t.scale_pcm(data, 1.0), data)                     # untouched
        self.assertEqual(samples(t.scale_pcm(data, 0.5)), [0, 500, -500, 10000, -10000, 16383,
                                                           -16384])
        self.assertEqual(samples(t.scale_pcm(data, 2.0)), [0, 2000, -2000, 32767, -32768, 32767,
                                                           -32768])     # clamped
        self.assertEqual(t.scale_pcm(data, 0.0), bytes(len(data)))
        self.assertEqual(len(t.scale_pcm(data, 1.5)), len(data))
        self.assertEqual(t.scale_pcm(b"", 0.5), b"")


class FakePoller:
    """Stands in for Poller when drawing without a network."""

    def __init__(self, status, spectrum=None, reachable=True, profiles=PROFILES, js8=None):
        self.lock = threading.Lock()
        self.status, self.spectrum, self.reachable = status, spectrum, reachable
        self.history = [(spectrum["start_hz"], spectrum["stop_hz"], spectrum["bins"])] * 30 \
            if spectrum else []
        self.why, self.spec_state = "connection refused", "ok" if spectrum else "missing"
        self.profiles = {"active": status.get("profile"), "profiles": [dict(p) for p in profiles]} \
            if profiles is not None else None
        self.profiles_state = "ok" if profiles is not None else "missing"
        self.profiles_error = ""
        self.js8_lines = list(js8 if js8 is not None else (js8_lines() if status.get("js8") else []))
        self.js8_state = "ok"
        self.want_spectrum = True
        self.changed = threading.Event()
        self.sent = []
        self.switched = []
        self.reloads = 0
        self.switch_acks = []

    def tune(self, **f):
        self.sent.append(f)
        self.status.update(f)

    def take_acks(self):
        return []

    def switch(self, name):
        self.switched.append(name)

    def take_switch_acks(self):
        acks, self.switch_acks = self.switch_acks, []
        return acks

    def reload_profiles(self):
        self.reloads += 1

    def dropping(self):
        return False


class FakeAudio:
    """Stands in for AudioPlayer in App tests."""

    def __init__(self):
        self.on, self.rate, self.gain = False, None, 1.0
        self.player = t.Player("ffplay", lambda r: ["ffplay"])

    def start(self, rate):
        self.on, self.rate = True, rate

    def stop(self):
        self.on = False

    def set_rate(self, rate):
        self.rate = rate

    def words(self, g):
        if not self.on:
            return g.stop, "Not listening here", "dim"
        return g.rx, "Listening on this machine (ffplay, %g kHz)" % (self.rate / 1000), "ok"


def app_on(profile, audio=None, g=U, clock=None, **kw):
    st = state_for(profile)
    spec = spectrum_for(st, 1)
    kwargs = {"clock": clock} if clock else {}
    app = t.App(FakePoller(st, spec, **kw), g, audio=audio, **kwargs)
    return app


def frame(app, h=24, w=80):
    c = t.Canvas(h, w)
    app.draw(c)
    return c


class Drawing(unittest.TestCase):
    def test_view_follows_the_profile(self):
        for name, view in (("ghostnet", "js8"), ("fm-105.3", "listen"), ("noaa", "listen"),
                           ("am-1030", "listen"), ("40m", "manual")):
            app = app_on(name)
            frame(app)
            self.assertEqual(app.view, view, name)

    def test_js8_80x24(self):
        app = app_on("ghostnet")
        c = frame(app)
        text = c.text()
        lines = text.split("\n")
        self.assertEqual(len(lines), 24)
        self.assertIn("1 ● JS8", lines[0])
        self.assertIn("● RX", lines[0])
        self.assertIn("▸ ghostnet", text)
        self.assertIn("● Active", text)
        self.assertIn("Ham It Up UPCONVERT", text)
        self.assertIn("6 stations heard", text)
        clock = next(i for i, s in enumerate(lines) if "Clock not synced" in s)
        self.assertEqual(c.role_at(clock, 0), "bad")              # the whole row, reversed
        self.assertIn(t.JS8_HEADER, text)
        mine = [i for i, s in enumerate(lines) if "◆ KA1PID" in s]
        self.assertTrue(mine)
        self.assertEqual(c.role_at(mine[0], 1), "mine")
        self.assertIn("WB2OQX", lines[-3])                         # newest at the bottom
        for word in ("Step", "Bandwidth", "Gain", "Speaker", "Squelch"):
            self.assertNotIn(word, text)                           # nothing else
        self.assertTrue(all(len(s) <= 80 for s in lines))
        self.assertIn("/ filter", lines[-1])

    def test_js8_filter_and_scroll(self):
        app = app_on("ghostnet")
        frame(app)
        for k in b"/w1aw\r":
            app.key(k)
        text = frame(app).text()
        self.assertEqual(app.js8_filter, "W1AW")
        n = len(t.js8_filter(js8_lines(), "W1AW"))
        self.assertEqual(n, 9)
        self.assertIn("Filter W1AW: 9 of 30", text)
        body = [s for s in text.split("\n") if re.match(r" \d\d:\d\d:\d\d", s)]
        self.assertEqual(len(body), n)
        self.assertTrue(all("W1AW" in s for s in body))
        app.key(27)
        self.assertEqual(app.js8_filter, "")
        frame(app)
        app.key(curses.KEY_PPAGE)
        text = frame(app).text()
        self.assertIn("back, PgDn for newest", text)
        self.assertNotIn("WB2OQX    HEARTBEAT", text.split("\n")[-3])
        app.key(curses.KEY_NPAGE)
        self.assertEqual(app.js8_scroll, 0)
        # new lines while scrolled back don't move what's on screen
        app.key(curses.KEY_PPAGE)
        before = frame(app).text()
        app.p.js8_lines.append(dict(js8_lines(31)[-1], seq=31, text="N0XQX: NEW LINE"))
        self.assertEqual(frame(app).text().split("\n")[8:20], before.split("\n")[8:20])

    def test_js8_clock_ok_and_unknown(self):
        app = app_on("js8")
        self.assertIn("● Clock synced (GPS)", frame(app).text())
        app.p.status["clock"] = {"synced": None}
        self.assertIn("? Clock unknown", frame(app).text())

    def test_listen_80x24(self):
        app = app_on("noaa", audio=FakeAudio())
        c = frame(app)
        text = c.text()
        lines = text.split("\n")
        self.assertIn("2 ● Listen", lines[0])
        self.assertIn("NOAA Weather", lines[2])
        self.assertIn("NFM", text)
        self.assertIn("Ham It Up BYPASS", text)
        self.assertIn("Gain 3.7 dB", text)
        self.assertIn("Bandwidth Normal 12.5 kHz", text)
        self.assertIn("Speaker 20 %", text)
        self.assertIn("Squelch -70 dBFS  ● Open (signal)", text)
        self.assertIn("■ Not listening here: l plays the audio here", text)
        for title in ("Broadcast FM", "VHF · UHF", "AM"):
            self.assertIn(title, text)
        self.assertIn("1030 kHz", text)
        self.assertIn("▸ NOAA Weather", text)
        self.assertIn("▲ Flip Ham It Up to UPCONVERT", text)      # WBZ needs it
        self.assertNotIn("Step", text)
        self.assertTrue(all(len(s) <= 80 for s in lines))

    def test_listen_wfm_has_no_squelch(self):
        text = frame(app_on("fm-105.3")).text()
        self.assertIn("WBUR Boston", text)
        self.assertNotIn("Squelch", text)
        self.assertIn("No audio player", text)                    # audio=None

    def test_manual_80x24_has_everything(self):
        app = app_on("40m", audio=FakeAudio())
        text = frame(app).text()
        lines = text.split("\n")
        self.assertEqual(len(lines), 24)
        self.assertIn("● RX", lines[0])
        self.assertIn("3 ● Manual", lines[0])
        self.assertIn("40m", lines[0])
        self.assertIn("LSB", text)
        self.assertIn("MHz", text)
        self.assertIn("Ham It Up switch: UPCONVERT (125 MHz)", text)
        self.assertIn("-62.4 dBFS", text)
        self.assertIn("▲", text)
        self.assertIn("quit", lines[-1])
        self.assertIn("l listen", lines[-1])
        self.assertTrue(all(len(s) <= 80 for s in lines))
        app.key(ord("l"))
        self.assertIn("● Listening on this machine (ffplay, 12 kHz)", frame(app).text())

    def test_picking_another_kind_shows_its_picker_and_never_switches(self):
        app = app_on("fm-105.3")
        frame(app)
        app.key(ord("1"))
        text = frame(app).text()
        self.assertEqual(app.view, "js8")
        self.assertIn("Receiver is on fm-105.3 (WBUR Boston). Pick a JS8 frequency to switch.",
                      text)
        self.assertIn("ghostnet", text)
        self.assertNotIn(t.JS8_HEADER, text)
        for _ in range(3):
            frame(app)
        self.assertEqual(app.view, "js8")
        self.assertEqual(app.p.switched, [])
        app.key(ord("g"))
        self.assertEqual(app.p.sent, [])                         # no tuning in JS8
        app.key(ord("2"))
        self.assertEqual(app.view, "listen")
        self.assertNotIn("Receiver is on", frame(app).text())
        app = app_on("ghostnet")
        frame(app)
        app.key(ord("2"))
        self.assertIn("Pick a station to switch.", frame(app).text())
        app.key(ord("G"))
        self.assertEqual(app.p.sent, [])
        self.assertIn("pick a station first", app.message)

    def test_manual_stays_until_left(self):
        app = app_on("ghostnet")
        frame(app)
        app.key(ord("3"))
        frame(app)
        self.assertEqual(app.view, "manual")
        app.p.status.update(state_for("noaa"))               # switched elsewhere
        frame(app)
        self.assertEqual(app.view, "manual")
        app.key(ord("2"))
        frame(app)
        self.assertEqual(app.view, "listen")
        app.p.status.update(state_for("ghostnet"))
        frame(app)
        self.assertEqual(app.view, "js8")                        # follows again

    def test_no_profiles_is_manual_only(self):
        app = app_on("noaa", profiles=None)
        frame(app)
        self.assertEqual(app.view, "manual")
        app.key(ord("1"))
        self.assertEqual(app.view, "manual")
        self.assertIn("can't switch profiles", app.message)

    def test_bypass_and_ascii(self):
        for name in ("ghostnet", "noaa", "am-1030", "40m"):
            app = app_on(name, g=A)
            text = frame(app).text()
            self.assertTrue(text.isascii(), (name, [c for c in text if not c.isascii()][:5]))
        app = app_on("40m", g=A)
        app.p.status["upconverter_hz"] = 0.0
        text = frame(app).text()
        self.assertIn("Ham It Up switch: BYPASS", text)
        self.assertIn("* RX", text)
        app = app_on("ghostnet", g=A)
        app.key(ord("?"))
        self.assertTrue(frame(app).text().isascii())

    def test_wide_uses_more_space(self):
        small = frame(app_on("40m")).text()
        big = frame(app_on("40m"), 45, 160).text()
        self.assertNotIn("██  ██", small)      # 3-row half-block digits
        self.assertIn("██  ██", big)           # 5-row digits at double size

        def spec_rows(text):  # lines between the axis labels and the dial marker
            lines = text.split("\n")
            axis = next(i for i, s in enumerate(lines) if "7.15" in s and "MHz" not in s)
            marker = next(i for i, s in enumerate(lines) if "▲━" in s)
            return marker - axis - 1
        self.assertEqual(spec_rows(small), 4)
        self.assertEqual(spec_rows(big), 10)
        self.assertIn("Dropped 0", big)

    def test_unreachable_says_so(self):
        text = frame(app_on("40m", reachable=False)).text()
        self.assertIn("✕ NO RXD", text)
        self.assertIn("Can't reach rxd", text)

    def test_tiny_and_odd_sizes(self):
        self.assertIn("too small", frame(app_on("40m"), 8, 20).text())
        for name in ("ghostnet", "noaa", "fm-105.3", "40m"):
            for h, w in ((10, 30), (16, 50), (24, 80), (30, 100), (45, 160), (60, 250)):
                for g in (U, A):
                    app = app_on(name, g=g, audio=FakeAudio())
                    frame(app, h, w)
                    app.key(ord("?"))
                    frame(app, h, w)  # no exceptions anywhere in between

    def test_manual_keys(self):
        app = app_on("40m")
        frame(app)
        p = app.p
        app.key(curses.KEY_RIGHT)
        app.key(curses.KEY_UP)
        self.assertEqual(app.step, 5000)
        app.key(curses.KEY_DOWN)
        app.key(curses.KEY_LEFT)
        for k in "[]gG-+":
            app.key(ord(k))
        app.key(ord("m"))
        self.assertEqual(p.sent, [{"freq": 7201000}, {"freq": 7200000},
                                  {"bandwidth": 2400.0}, {"bandwidth": 2800.0},
                                  {"gain_db": 2.7}, {"gain_db": 3.7},
                                  {"volume": 0.15}, {"volume": 0.2}, {"mode": "AM"}])
        app.key(ord("f"))
        for k in b"14.074\n":
            app.key(k)
        self.assertEqual(p.sent[-1], {"freq": 14074000})
        app.key(ord("f"))
        for k in b"zzz\n":
            app.key(k)
        self.assertIn("not a frequency", app.message)
        app.key(ord("?"))
        self.assertTrue(app.show_help)
        app.key(ord("x"))
        self.assertFalse(app.show_help)
        app.key(ord("x"))
        self.assertIn("isn't a key in Manual", app.message)       # never silent
        self.assertFalse(app.key(ord("q")))

    def test_listen_keys(self):
        app = app_on("noaa", audio=FakeAudio())
        frame(app)
        p = app.p
        for k in "Gbss.,-+":
            app.key(ord(k))
            frame(app)
        self.assertEqual(p.sent, [{"gain_db": 7.7}, {"bandwidth": 16000.0}, {"squelch": None},
                                  {"squelch": -70.0}, {"squelch": -68.0}, {"squelch": -70.0},
                                  {"volume": 0.15}, {"volume": 0.2}])
        app.key(ord("M"))
        self.assertEqual(p.sent[-1], {"volume": 0.0})
        self.assertIn("Speaker Muted (M restores 20 %)", frame(app).text())
        app.key(ord("M"))
        self.assertEqual(p.sent[-1], {"volume": 0.2})
        # local volume, scaled here, not sent to rxd
        n = len(p.sent)
        app.key(ord("l"))
        self.assertTrue(app.audio.on)
        self.assertEqual(app.audio.rate, 12000)
        app.key(ord("["))
        app.key(ord("["))
        frame(app)
        self.assertEqual(app.audio.gain, 0.8)
        self.assertIn("Volume here 80 %", frame(app).text())
        self.assertEqual(len(p.sent), n)
        app.key(ord("l"))
        self.assertFalse(app.audio.on)

    def test_squelch_is_nfm_only(self):
        app = app_on("fm-105.3")
        frame(app)
        app.key(ord("s"))
        self.assertEqual(app.p.sent, [])
        self.assertIn("NFM only", app.message)
        app.key(ord("b"))
        self.assertEqual(app.p.sent, [{"bandwidth": 200000.0}])

    def test_l_without_a_player_explains(self):
        app = app_on("noaa", audio=None)
        frame(app)
        app.key(ord("l"))
        self.assertIn("No audio player found", app.message)
        self.assertEqual(app.message_role, "err")
        app = app_on("ghostnet", audio=FakeAudio())
        frame(app)
        app.key(ord("l"))
        self.assertFalse(app.audio.on)
        self.assertIn("No audio in JS8", app.message)
        app.key(ord("M"))
        self.assertEqual(app.p.sent, [])                          # JS8Call's feed stays up

    def test_audio_rate_follows_the_mode(self):
        app = app_on("noaa", audio=FakeAudio())
        frame(app)
        app.key(ord("l"))
        app.p.status.update(state_for("fm-105.3"))
        frame(app)
        self.assertEqual(app.audio.rate, 24000)

    def test_switch_confirm_and_finish(self):
        now = [100.0]
        app = app_on("ghostnet", clock=lambda: now[0])
        frame(app)
        app.key(curses.KEY_DOWN)
        app.key(10)
        self.assertEqual(app.confirm["name"], "js8")
        text = frame(app).text()
        self.assertIn("Switch to js8 (JS8Call 20 m)?", text)
        self.assertIn("Leaving ghostnet closes JS8Call", text)
        self.assertIn("y switches; any other key stays.", text)
        app.key(ord("n"))
        self.assertIsNone(app.confirm)
        self.assertEqual(app.p.switched, [])
        self.assertIn("Stayed on ghostnet", app.message)
        app.key(10)
        app.key(ord("y"))
        self.assertEqual(app.p.switched, ["js8"])
        app.p.reachable = False
        text = frame(app).text()
        self.assertIn("■ SWITCHING", text)
        self.assertIn("■ Switching to js8…", text)
        app.p.reachable = True
        app.p.status.update(state_for("js8"))
        frame(app)
        self.assertIsNone(app.switching)
        self.assertIn("Now on JS8Call 20 m", app.message)
        self.assertEqual(app.p.reloads, 1)

    def test_switch_between_stations_asks_nothing_and_warns_about_the_flip(self):
        now = [100.0]
        app = app_on("fm-105.3", clock=lambda: now[0])
        frame(app)
        for _ in range(3):
            app.key(curses.KEY_DOWN)
        app.key(10)
        self.assertIsNone(app.confirm)
        self.assertEqual(app.p.switched, ["am-1030"])
        app.p.reachable = False
        self.assertIn("▲ Flip the Ham It Up to UPCONVERT", frame(app).text())   # before
        app.p.reachable = True
        app.p.status.update(state_for("am-1030"))
        frame(app)
        self.assertIn("Flip the Ham It Up to UPCONVERT for WBZ", app.message)    # after
        self.assertEqual(app.message_role, "warn")

    def test_switch_times_out(self):
        now = [100.0]
        app = app_on("fm-105.3", clock=lambda: now[0])
        frame(app)
        app.key(curses.KEY_DOWN)
        app.key(10)
        self.assertTrue(app.switching)
        app.p.reachable = False
        now[0] += 26
        frame(app)
        self.assertIsNone(app.switching)
        self.assertIn("did not finish", app.message)
        self.assertIn("has not come back after 25 seconds", app.message)
        self.assertEqual(app.p.reloads, 1)

    def test_mute_survives_a_switch(self):
        now = [100.0]
        app = app_on("fm-105.3", clock=lambda: now[0])
        frame(app)
        app.key(ord("M"))
        self.assertEqual(app.p.sent, [{"volume": 0.0}])
        app.key(curses.KEY_DOWN)
        app.key(10)
        app.p.status.update(state_for("noaa"))                   # volume 0.2 again
        frame(app)
        self.assertEqual(app.p.sent[-1], {"volume": 0.0})
        self.assertEqual(app.mute_prev, 0.2)


class AgainstFakeRxd(unittest.TestCase):
    def wait(self, cond, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_poller_status_spectrum_and_tune(self):
        fake = FakeRxd()
        self.addCleanup(fake.close)
        p = t.Poller(t.Link(fake.url))
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(self.wait(lambda: len(p.history) >= 3, 3))
        self.assertTrue(p.reachable)
        self.assertEqual(p.status["freq"], 7200000.0)
        self.assertEqual(p.spec_state, "ok")
        p.tune(freq=50)
        acks = []
        self.assertTrue(self.wait(lambda: acks.extend(p.take_acks()) or acks, 3))
        self.assertEqual(acks, [({"freq": 50}, "frequency out of range")])
        self.assertTrue(self.wait(lambda: p.profiles_state == "ok"))
        self.assertEqual(p.profiles["active"], "40m")
        # no spectrum polling outside Manual
        p.want_spectrum = False
        time.sleep(0.3)
        n = fake.seq
        time.sleep(0.6)
        self.assertEqual(fake.seq, n)

    def test_poller_js8_feed_and_switch(self):
        fake = FakeRxd("ghostnet", drop=1.0)
        self.addCleanup(fake.close)
        p = t.Poller(t.Link(fake.url))
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(self.wait(lambda: len(p.js8_lines) == 30))
        with fake.lock:
            fake.js8.append(dict(js8_lines(31)[-1]))
        self.assertTrue(self.wait(lambda: len(p.js8_lines) == 31, 4))
        self.assertEqual([x["seq"] for x in p.js8_lines], list(range(1, 32)))
        p.switch("fm-105.3")
        self.assertTrue(self.wait(lambda: p.reachable is False, 3))
        self.assertTrue(self.wait(lambda: p.status.get("profile") == "fm-105.3", 5))
        self.assertEqual(p.take_switch_acks(), [("fm-105.3", None)])
        self.assertTrue(self.wait(lambda: not p.js8_lines))          # not a JS8 profile
        p.switch("nope")
        self.assertTrue(self.wait(lambda: p.switch_acks, 3))
        self.assertIn("no profile 'nope'", p.take_switch_acks()[0][1])

    def test_poller_unreachable_and_no_spectrum(self):
        fake = FakeRxd(spectrum=False)
        url = fake.url
        p = t.Poller(t.Link(url))
        p._status()
        p._spectrum()
        self.assertEqual(p.spec_state, "missing")
        fake.close()
        self.assertFalse(p._status())
        self.assertFalse(p.reachable)
        self.assertEqual(p.why, "connection refused")

    def test_no_profiles_endpoint(self):
        fake = FakeRxd(profiles=False)
        self.addCleanup(fake.close)
        p = t.Poller(t.Link(fake.url))
        p._status()
        p._load_profiles(p.status)
        self.assertEqual(p.profiles_state, "missing")

    def test_audio_player_plays_reconnects_and_restarts_on_rate(self):
        fake = FakeRxd("noaa", drop=1.0)
        self.addCleanup(fake.close)
        d = tempfile.mkdtemp(prefix="rxd-tui-audio-")
        script = os.path.join(d, "rec")
        with open(script, "w") as fh:
            fh.write('#!/bin/sh\necho $$ > "%s/pid.$1"\nexec /bin/cat > "%s/pcm.$1"\n' % (d, d))
        os.chmod(script, 0o755)
        ap = t.AudioPlayer(fake.url, t.Player.custom(script + " {rate}"))
        self.addCleanup(ap.stop)

        def size(rate):
            try:
                return os.path.getsize(os.path.join(d, "pcm.%d" % rate))
            except OSError:
                return 0

        def pid(rate):
            with open(os.path.join(d, "pid.%d" % rate)) as fh:
                return int(fh.read())
        ap.start(12000)
        self.assertTrue(self.wait(lambda: size(12000) > 4800))
        self.assertEqual(ap.words(U)[1], "Listening on this machine (rec, 12 kHz)")
        # the samples arrive as sent, a 440 Hz tone at 8000
        with open(os.path.join(d, "pcm.12000"), "rb") as fh:
            a = array("h")
            a.frombytes(fh.read(2400))
        self.assertEqual(max(a), 8000)
        # rxd restarts (a profile switch): reconnect, same player
        first = pid(12000)
        fake.down()
        self.assertTrue(self.wait(lambda: ap.words(U)[1] == "Reconnecting", 4))
        fake.server = fake.serve(fake.port)
        n = size(12000)
        self.assertTrue(self.wait(lambda: size(12000) > n + 4800, 6))
        self.assertEqual(pid(12000), first)
        # local volume scales what the player gets
        ap.gain = 0.5
        time.sleep(0.6)
        n = size(12000)
        time.sleep(0.5)
        with open(os.path.join(d, "pcm.12000"), "rb") as fh:
            fh.seek(n & ~1)
            a = array("h")
            a.frombytes(fh.read(2400))
        self.assertEqual(max(a), 4000)
        # the mode changes to WFM: a new stream and a new player at 24 kHz
        ap.set_rate(24000)
        self.assertTrue(self.wait(lambda: size(24000) > 9600))
        self.assertTrue(self.wait(lambda: not alive(first), 3))
        self.assertEqual(fake.audio_rates[-1], 24000)
        second = pid(24000)
        ap.stop()
        self.assertFalse(alive(second))
        self.assertEqual(ap.words(U), ("■", "Not listening here", "dim"))
        self.assertTrue(self.wait(lambda: fake.listeners == 0, 3))

    def test_audio_refused(self):
        fake = FakeRxd("noaa")
        self.addCleanup(fake.close)
        fake.listeners = 4
        ap = t.AudioPlayer(fake.url, t.Player.custom("cat"))
        self.addCleanup(ap.stop)
        ap.start(12000)
        self.assertTrue(self.wait(lambda: ap.state == "error"))
        sym, text, role = ap.words(U)
        self.assertEqual((sym, role), ("✕", "bad"))
        self.assertIn("4 clients are already listening", text)


@unittest.skipUnless(hasattr(pty, "openpty"), "needs a pty")
class RealProgram(unittest.TestCase):
    def run_tui(self, fake_url, rows=24, cols=80, env=None, args=()):
        tui = Pty(fake_url, rows, cols, env, args)
        self.addCleanup(lambda: tui.proc.poll() is None and os.killpg(tui.proc.pid, signal.SIGKILL))
        return tui

    def quit_cleanly(self, tui):
        tui.send(b"q")
        code, before, after = tui.finish()
        self.assertEqual(code, 0)
        self.assertEqual(after, before)                             # terminal restored
        self.assertIn("\x1b[?1049l", tui.raw.decode("utf-8", "ignore"))

    def fake(self, *a, **k):
        fake = FakeRxd(*a, **k)
        self.addCleanup(fake.close)
        return fake

    def test_manual_draws_tunes_and_quits(self):
        fake = self.fake("40m")
        tui = self.run_tui(fake.url)
        self.assertTrue(tui.wait_for("● RX"), tui.vt.text())
        self.assertTrue(tui.wait_for("▲"), tui.vt.text())     # spectrum drawn
        screen = tui.vt.text()
        self.assertIn("3 ● Manual", screen)
        self.assertIn("Ham It Up switch: UPCONVERT", screen)
        self.assertIn("quit", screen.split("\n")[-1])
        self.assertIn("\x1b[?1049h", tui.raw.decode("utf-8", "ignore"))  # alternate screen
        tui.keys(KEYS["right"], KEYS["up"], b"\x1b[C", b"m", b"]", b"G", b"+")
        tui.send(b"f146.52\r")
        tui.pump(0.6)
        self.assertTrue(tui.wait_for("BYPASS"), tui.vt.text())  # 146.52 MHz: no upconverter
        self.assertEqual(fake.posts, [{"freq": 7201000}, {"freq": 7205000}, {"mode": "AM"},
                                      {"gain_db": 7.7}, {"volume": 0.25},
                                      {"freq": 146520000}])
        self.assertIn("3 ● Manual", tui.vt.text())               # tuning to FM didn't move it
        tui.send(b"?")
        self.assertTrue(tui.wait_for("Any key closes this"), tui.vt.text())
        tui.send(b" ")
        tui.pump(0.3)
        self.quit_cleanly(tui)

    def test_js8_view_filter_and_scroll(self):
        fake = self.fake("ghostnet")
        tui = self.run_tui(fake.url)
        self.assertTrue(tui.wait_for("WB2OQX", 6), tui.vt.text())
        screen = tui.vt.text()
        self.assertIn("1 ● JS8", screen)
        self.assertIn("✕ Clock not synced: JS8 won't decode", screen)
        self.assertIn("◆ KA1PID", screen)
        self.assertIn("6 stations heard", screen)
        self.assertNotIn("Bandwidth", screen)
        tui.send(b"/W1AW\r")
        self.assertTrue(tui.wait_for("Filter W1AW: 9 of 30"), tui.vt.text())
        body = [s for s in tui.vt.text().split("\n") if re.match(r" \d\d:\d\d:\d\d", s)]
        self.assertEqual(len(body), 9)
        tui.send(KEYS["esc"])
        tui.pump(0.4)
        tui.send(KEYS["pgup"])
        self.assertTrue(tui.wait_for("PgDn for newest"), tui.vt.text())
        tui.send(KEYS["pgdn"])
        tui.pump(0.4)
        self.assertNotIn("PgDn for newest", tui.vt.text())
        tui.send(b"g")
        tui.pump(0.3)
        self.assertIn("isn't a key in JS8", tui.vt.text())
        self.assertEqual(fake.posts, [])
        self.quit_cleanly(tui)

    def test_listen_keys_send_the_right_posts(self):
        fake = self.fake("noaa")
        tui = self.run_tui(fake.url)
        self.assertTrue(tui.wait_for("Squelch -70 dBFS"), tui.vt.text())
        self.assertTrue(tui.wait_for("▸ NOAA Weather"), tui.vt.text())
        screen = tui.vt.text()
        self.assertIn("2 ● Listen", screen)
        self.assertIn("NOAA Weather", screen)
        self.assertIn("VHF · UHF", screen)
        tui.keys(*[bytes([c]) for c in b"Gbss.,-+MM"])
        tui.pump(0.4)
        self.assertEqual(fake.posts, [{"gain_db": 7.7}, {"bandwidth": 16000.0}, {"squelch": None},
                                      {"squelch": -70.0}, {"squelch": -68.0}, {"squelch": -70.0},
                                      {"volume": 0.15}, {"volume": 0.2}, {"volume": 0.0},
                                      {"volume": 0.2}])
        self.assertIn("Bandwidth Wide 16 kHz", tui.vt.text())
        tui.keys(b"M")
        self.assertIn("Speaker Muted (M restores 20 %)", tui.vt.text())
        self.quit_cleanly(tui)
        self.assertEqual(fake.posts[-2:], [{"volume": 0.0}, {"volume": 0.2}])  # given back on quit

    def test_confirm_then_switch_through_a_real_drop(self):
        fake = self.fake("ghostnet", drop=2.0)
        tui = self.run_tui(fake.url)
        self.assertTrue(tui.wait_for("WB2OQX", 6), tui.vt.text())
        tui.keys(KEYS["down"], KEYS["enter"])
        self.assertTrue(tui.wait_for("closes JS8Call"), tui.vt.text())
        tui.send(b"n")
        self.assertTrue(tui.wait_for("Stayed on ghostnet"), tui.vt.text())
        self.assertEqual(fake.switches, [])
        tui.keys(KEYS["enter"])
        self.assertTrue(tui.wait_for("closes JS8Call"), tui.vt.text())
        tui.send(b"y")
        self.assertTrue(tui.wait_for("Switching to js8"), tui.vt.text())
        self.assertTrue(tui.wait_for("■ SWITCHING", 3), tui.vt.text())
        self.assertTrue(tui.wait_for("Now on JS8Call 20 m", 8), tui.vt.text())
        self.assertEqual(fake.switches, ["js8"])
        self.assertTrue(tui.wait_for("● Clock synced (GPS)"), tui.vt.text())
        # js8 -> a station: asks again, and the Ham It Up flips
        tui.send(b"2")
        self.assertTrue(tui.wait_for("Pick a station to switch"), tui.vt.text())
        tui.keys(KEYS["enter"])
        self.assertTrue(tui.wait_for("Then flip the Ham It Up to BYPASS"), tui.vt.text())
        tui.send(b"y")
        self.assertTrue(tui.wait_for("Flip the Ham It Up to BYPASS for WBUR Boston", 8),
                        tui.vt.text())
        self.assertIn("2 ● Listen", tui.vt.text())
        self.assertEqual(fake.switches, ["js8", "fm-105.3"])
        self.quit_cleanly(tui)

    def test_l_plays_through_a_player_on_path_and_quit_kills_it(self):
        fake = self.fake("fm-105.3")
        d = tempfile.mkdtemp(prefix="rxd-tui-bin-")
        fake_player(d, "ffplay")
        tui = self.run_tui(fake.url, env={"TUI_PATH": d})
        self.assertTrue(tui.wait_for("WBUR Boston"), tui.vt.text())
        tui.send(b"l")
        self.assertTrue(tui.wait_for("● Listening on this machine (ffplay, 24 kHz)", 6),
                        tui.vt.text())
        time.sleep(0.5)
        self.assertGreater(os.path.getsize(os.path.join(d, "pcm")), 9600)
        with open(os.path.join(d, "args")) as fh:
            self.assertEqual(fh.read().split(), ["-nodisp", "-loglevel", "quiet", "-f", "s16le",
                                                 "-ar", "24000", "-ch_layout", "mono", "-"])
        with open(os.path.join(d, "pid")) as fh:
            pid = int(fh.read())
        self.assertTrue(alive(pid))
        tui.keys(b"]", b"]")
        self.assertIn("Volume here 120 %", tui.vt.text())
        self.quit_cleanly(tui)
        self.assertFalse(alive(pid))
        self.assertTrue(AgainstFakeRxd.wait(self, lambda: fake.listeners == 0, 3))

    def test_listen_flag_and_no_player(self):
        fake = self.fake("noaa")
        empty = tempfile.mkdtemp(prefix="rxd-tui-nobin-")
        tui = self.run_tui(fake.url, env={"TUI_PATH": empty}, args=["--listen"])
        self.assertTrue(tui.wait_for("No audio player"), tui.vt.text())
        tui.send(b"l")
        hint = t.no_player_hint()
        self.assertTrue(tui.wait_for(hint[:40]), tui.vt.text())
        self.assertEqual(fake.audio_rates, [])
        self.quit_cleanly(tui)

    def test_listen_flag_starts_audio(self):
        fake = self.fake("noaa")
        d = tempfile.mkdtemp(prefix="rxd-tui-bin-")
        fake_player(d, "ffplay")
        tui = self.run_tui(fake.url, env={"TUI_PATH": d}, args=["--listen"])
        self.assertTrue(tui.wait_for("● Listening on this machine (ffplay, 12 kHz)", 6),
                        tui.vt.text())
        self.assertEqual(fake.audio_rates, [12000])
        self.quit_cleanly(tui)

    def test_ctrl_c_resize_no_color_and_no_rxd(self):
        fake = FakeRxd()
        url = fake.url
        fake.close()        # nothing listening
        tui = self.run_tui(url, env={"NO_COLOR": "1"})
        self.assertTrue(tui.wait_for("✕ NO RXD"), tui.vt.text())
        self.assertIn("Can't reach rxd", tui.vt.text())
        # No colour at all: only the terminal's defaults (39, 49) are ever set.
        sgr = re.findall(r"\x1b\[([0-9;]*)m", tui.raw.decode("utf-8", "ignore"))
        codes = {c for p in sgr for c in p.split(";") if c}
        self.assertFalse(codes & {str(n) for n in list(range(30, 38)) + list(range(40, 48))
                                  + [38, 48]}, codes)
        tui.resize(45, 160)
        self.assertTrue(tui.wait_for("✕ NO RXD"), tui.vt.text())
        self.assertGreater(max(len(s) for s in tui.vt.text().split("\n")), 100)
        tui.send(b"\x03")           # Ctrl-C
        code, before, after = tui.finish()
        self.assertEqual(code, 0)
        self.assertEqual(after, before)

    def test_every_view_resizes(self):
        fake = self.fake("ghostnet")
        tui = self.run_tui(fake.url, 30, 100)
        self.assertTrue(tui.wait_for("WB2OQX", 6), tui.vt.text())
        for key, word in ((b"2", "Pick a station"), (b"3", "Ham It Up switch"), (b"1", "Clock")):
            tui.send(key)
            self.assertTrue(tui.wait_for(word), tui.vt.text())
            for rows, cols in ((24, 80), (12, 40), (45, 160)):
                tui.resize(rows, cols)
                self.assertTrue(tui.wait_for("rxd", 3), (rows, cols))
        self.quit_cleanly(tui)

    def test_reconnects_quietly(self):
        fake = FakeRxd()
        port = fake.port
        fake.close()
        tui = self.run_tui(fake.url)
        self.assertTrue(tui.wait_for("NO RXD"))
        fake2 = FakeRxd(port=port)
        self.addCleanup(fake2.close)
        self.assertTrue(tui.wait_for("● RX", 5), tui.vt.text())
        self.quit_cleanly(tui)


if __name__ == "__main__":
    unittest.main()
