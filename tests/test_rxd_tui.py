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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rxd_tui as t  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
U = t.Glyphs(False)
A = t.Glyphs(True)


# ------------------------------------------------------------- fake rxd

class FakeRxd:
    """An http.server speaking rxd's API on a random port. Records every POST."""

    def __init__(self, spectrum=True, gains=(0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 12.5, 19.7, 29.7, 49.6)):
        self.state = {"profile": "js8", "freq": 7078000.0, "vfo_hz": 7077900.0, "cal_hz": 100.0,
                      "mode": "USB", "bandwidth": 2800.0, "gain_db": 3.7, "volume": 0.2,
                      "offset_mode": "auto", "upconverter_hz": 125e6, "level_dbfs": -62.4,
                      "tuner_hz": 132028000.0, "offset_hz": 50000.0, "sample_rate": 240000,
                      "dsp_cpu": 0.25, "blocks": 100, "dropped": 0, "error": None,
                      "running": True, "gains": list(gains)}
        self.spectrum = spectrum
        self.posts = []
        self.seq = 0
        self.lock = threading.Lock()
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
                if self.path == "/api/status":
                    with fake.lock:
                        return self.send(200, dict(fake.state))
                if self.path == "/api/spectrum" and fake.spectrum:
                    return self.send(200, fake.make_spectrum())
                self.send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                doc = json.loads(self.rfile.read(n))
                with fake.lock:
                    fake.posts.append(doc)
                    if "freq" in doc and not 100e3 <= doc["freq"] <= 1.7e9:
                        return self.send(400, {"error": "frequency out of range"})
                    if "mode" in doc and doc["mode"] != fake.state["mode"]:
                        fake.state["bandwidth"] = {"USB": 2800.0, "LSB": 2800.0, "AM": 10000.0,
                                                   "FM": 12500.0, "WFM": 150000.0}[doc["mode"]]
                    fake.state.update(doc)
                    fake.state["upconverter_hz"] = 125e6 if fake.state["freq"] < 30e6 else 0.0
                    return self.send(200, dict(fake.state))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def make_spectrum(self):
        with self.lock:
            dial, bw = self.state["freq"], self.state["bandwidth"]
            self.seq += 1
            seq = self.seq
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

    def close(self):
        self.server.shutdown()
        self.server.server_close()


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
    the terminal's settings (stty -g) before it starts and after it exits."""

    SCRIPT = ('trap ":" INT; stty -g > "$1/before"; "$2" "$3" --url "$4" $5; '
              'echo $? > "$1/code"; stty -g > "$1/after"')

    def __init__(self, url, rows=24, cols=80, env=None, args=""):
        self.vt = VT(rows, cols)
        self.dir = tempfile.mkdtemp(prefix="rxd-tui-test-")
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        e = dict(os.environ, TERM="xterm-256color", LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
        for k in ("NO_COLOR", "LINES", "COLUMNS"):
            e.pop(k, None)
        e.update(env or {})
        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", self.SCRIPT, "sh", self.dir, sys.executable,
             os.path.join(HERE, "rxd_tui.py"), url, args],
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
KEYS = {"left": b"\x1bOD", "right": b"\x1bOC", "up": b"\x1bOA", "down": b"\x1bOB"}


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

    def test_no_color(self):
        self.assertFalse(t.use_color({"NO_COLOR": "1"}))
        self.assertTrue(t.use_color({"NO_COLOR": ""}))
        self.assertTrue(t.use_color({}))

    def test_monochrome_attrs_still_mark_status(self):
        # Without colour every status role still has bold or reverse.
        a = t.CursesScreen.make_attrs(False)
        for role in ("ok", "warn", "bad", "err", "key", "bar", "upconv"):
            self.assertTrue(a[role] & (curses.A_BOLD | curses.A_REVERSE), role)

    def test_big_font(self):
        for scale, g, rows in ((1, U, 3), (2, U, 5), (1, A, 5), (2, A, 5)):
            lines = t.big_text("7.078 000", scale, g)
            self.assertEqual(len(lines), rows)
            self.assertEqual(len({len(s) for s in lines}), 1)
            if g is A:
                self.assertTrue(all(s.isascii() for s in lines))
        self.assertEqual(t.big_text("0", 1, U), ["▄▄▄", "█ █", "█▄█"])
        self.assertEqual(len(t.big_text("7.078 000", 1, U)[0]), 31)

    def test_help_line_fits(self):
        for g in (U, A):
            for w in (60, 78, 100, 158):
                parts = t.help_line(w, g)
                keys = [s for s, r in parts if r == "key"]
                self.assertEqual(len(keys), 9)
                if w >= 78:
                    self.assertLessEqual(sum(len(s) for s, _ in parts), w)
        self.assertIn(" bandwidth", [s for s, _ in t.help_line(158, U)])


class Spectrum(unittest.TestCase):
    def test_resample_widths(self):
        bins = [float(i) for i in range(512)]
        for w in (20, 80, 158, 512, 700):
            cols = t.resample(bins, 0, 512, w)
            self.assertEqual(len(cols), w)
            self.assertNotIn(None, cols)
            self.assertEqual(cols, sorted(cols))
        self.assertEqual(t.resample(bins, 0, 512, 4), [127.0, 255.0, 383.0, 511.0])  # peaks

    def test_peak_survives_downsampling(self):
        bins = [-100.0] * 512
        bins[300] = -20.0
        cols = t.resample(bins, 0, 512, 80)
        self.assertEqual(max(cols), -20.0)
        self.assertEqual(cols.index(-20.0), 300 * 80 // 512)

    def test_resample_view_outside_is_none(self):
        cols = t.resample([1.0] * 100, 1000, 2000, 10, 500, 1500)
        self.assertEqual(cols[:5], [None] * 5)
        self.assertEqual(cols[5:], [1.0] * 5)

    def test_render_rows_and_widths(self):
        for g in (U, A):
            for w in (40, 80, 160):
                vals = [-100 + 80 * i / (w - 1) for i in range(w)]
                for rows in (1, 4):
                    lines = t.render_spectrum(vals, rows, -100, -20, g)
                    self.assertEqual(len(lines), rows)
                    self.assertTrue(all(len(s) == w for s in lines))
                    self.assertTrue(set("".join(lines)) <= set(g.bars))
                    self.assertEqual(lines[-1][-1], g.bars[-1])   # strongest: full
                    self.assertIn(lines[0][0], (" ", g.bars[1]))  # weakest: lowest
        self.assertEqual(t.render_spectrum([-100, -60, -20], 1, -100, -20, U), ["▁▄█"])
        self.assertEqual(t.render_spectrum([-20], 2, -100, -20, U), ["█", "█"])
        self.assertEqual(t.render_spectrum([-60], 2, -100, -20, U), [" ", "█"])
        self.assertEqual(t.render_spectrum([-100, None], 1, -100, -20, A), [". "])

    def test_waterfall_shading(self):
        self.assertEqual(t.render_waterfall_row([-100, -80, -60, -40, -20, None], -100, -20, U),
                         " ░▒▓█ ")
        self.assertEqual(t.render_waterfall_row([-200, 0], -100, -20, U), " █")  # clamped
        row = t.render_waterfall_row([-100 + i for i in range(81)], -100, -20, A)
        self.assertTrue(row.isascii())
        self.assertEqual((row[0], row[-1]), (" ", "@"))

    def test_markers_and_axis(self):
        row = t.marker_row(80, 0, 80000, 40000, [40200, 43000], U)
        self.assertEqual(len(row), 80)
        self.assertEqual(row[40], "▲")
        self.assertEqual(row[41:44], "━━━")
        self.assertEqual(t.marker_row(10, 0, 100, 500, None, A)[-1], ">")
        self.assertEqual(t.marker_row(10, 0, 100, -5, None, A)[0], "<")
        ax = t.axis_row(80, 6.908e6, 7.148e6)
        self.assertEqual(len(ax), 80)
        self.assertIn("7.00", ax)
        self.assertIn("7.10", ax)
        c = ax.index("7.00") + 2
        self.assertEqual(c, t.column_of(7.0e6, 6.908e6, 7.148e6, 80))

    def test_level_bar(self):
        self.assertEqual(t.level_bar(-110, 10, U), "░" * 10)
        self.assertEqual(t.level_bar(-10, 10, U), "█" * 10)
        self.assertEqual(t.level_bar(-60, 10, A), "#####-----")
        self.assertEqual(len(t.level_bar(-63.3, 37, U)), 37)
        self.assertEqual(t.level_bar(None, 5, A), "-----")


class FakePoller:
    """Stands in for Poller when drawing without a network."""

    def __init__(self, status, spectrum=None, reachable=True):
        self.lock = threading.Lock()
        self.status, self.spectrum, self.reachable = status, spectrum, reachable
        self.history = [(spectrum["start_hz"], spectrum["stop_hz"], spectrum["bins"])] * 30 \
            if spectrum else []
        self.why, self.spec_state = "connection refused", "ok" if spectrum else "missing"
        self.sent = []

    def tune(self, **f):
        self.sent.append(f)

    def take_acks(self):
        return []

    def dropping(self):
        return False


class Drawing(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRxd()
        self.addCleanup(self.fake.close)
        self.status = dict(self.fake.state)
        self.spec = self.fake.make_spectrum()

    def frame(self, h, w, g=U, **kw):
        app = t.App(FakePoller(self.status, self.spec, **kw), g)
        c = t.Canvas(h, w)
        app.draw(c)
        return app, c.text()

    def test_80x24_has_everything(self):
        _, text = self.frame(24, 80)
        lines = text.split("\n")
        self.assertEqual(len(lines), 24)
        self.assertIn("● RX", lines[0])
        self.assertIn("js8", lines[0])
        self.assertIn("USB", text)
        self.assertIn("MHz", text)
        self.assertIn("Ham It Up switch: UPCONVERT (125 MHz)", text)
        self.assertIn("-62.4 dBFS", text)
        self.assertIn("▲", text)
        self.assertIn("quit", lines[-1])
        self.assertTrue(all(len(s) <= 80 for s in lines))

    def test_bypass_and_ascii(self):
        self.status["upconverter_hz"] = 0.0
        _, text = self.frame(24, 80, A)
        self.assertIn("Ham It Up switch: BYPASS", text)
        self.assertTrue(text.isascii(), [c for c in text if not c.isascii()][:5])
        self.assertIn("* RX", text)

    def test_wide_uses_more_space(self):
        _, small = self.frame(24, 80)
        _, big = self.frame(45, 160)
        self.assertNotIn("██  ██", small)      # 3-row half-block digits
        self.assertIn("██  ██", big)           # 5-row digits at double size
        def spec_rows(text):  # lines between the axis labels and the dial marker
            lines = text.split("\n")
            axis = next(i for i, s in enumerate(lines) if "7.05" in s and "MHz" not in s)
            marker = next(i for i, s in enumerate(lines) if "▲━" in s)
            return marker - axis - 1
        self.assertEqual(spec_rows(small), 4)
        self.assertEqual(spec_rows(big), 10)
        self.assertIn("Dropped 0", big)

    def test_unreachable_says_so(self):
        _, text = self.frame(24, 80, reachable=False)
        self.assertIn("✕ NO RXD", text)
        self.assertIn("Can't reach rxd", text)

    def test_tiny_terminal(self):
        _, text = self.frame(8, 20)
        self.assertIn("too small", text)
        for h, w in ((10, 30), (16, 50), (24, 80), (30, 100), (45, 160), (60, 250)):
            self.frame(h, w)  # no exceptions anywhere in between

    def test_every_help_key_does_something(self):
        app = t.App(FakePoller(self.status, self.spec), U)
        p = app.p
        app.key(curses.KEY_RIGHT)
        app.key(curses.KEY_UP)
        self.assertEqual(app.step, 5000)
        app.key(curses.KEY_DOWN)
        app.key(curses.KEY_LEFT)
        for k in "m[]gG-+":
            app.key(ord(k))
        self.assertEqual(p.sent, [{"freq": 7079000}, {"freq": 7078000}, {"mode": "LSB"},
                                  {"bandwidth": 2400.0}, {"bandwidth": 2800.0},
                                  {"gain_db": 2.7}, {"gain_db": 3.7},
                                  {"volume": 0.15}, {"volume": 0.2}])
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
        self.assertFalse(app.key(ord("q")))


class AgainstFakeRxd(unittest.TestCase):
    def test_poller_status_spectrum_and_tune(self):
        fake = FakeRxd()
        self.addCleanup(fake.close)
        p = t.Poller(t.Link(fake.url))
        p.start()
        self.addCleanup(p.stop)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(p.history) < 3:
            time.sleep(0.05)
        self.assertTrue(p.reachable)
        self.assertEqual(p.status["freq"], 7078000.0)
        self.assertEqual(p.spec_state, "ok")
        self.assertGreaterEqual(len(p.history), 3)
        p.tune(freq=50)
        deadline = time.monotonic() + 3
        acks = []
        while time.monotonic() < deadline and not acks:
            acks = p.take_acks()
            time.sleep(0.02)
        self.assertEqual(acks, [({"freq": 50}, "frequency out of range")])

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


@unittest.skipUnless(hasattr(pty, "openpty"), "needs a pty")
class RealProgram(unittest.TestCase):
    def run_tui(self, fake_url, rows=24, cols=80, env=None):
        tui = Pty(fake_url, rows, cols, env)
        self.addCleanup(lambda: tui.proc.poll() is None and os.killpg(tui.proc.pid, signal.SIGKILL))
        return tui

    def test_starts_draws_tunes_and_quits(self):
        fake = FakeRxd()
        self.addCleanup(fake.close)
        tui = self.run_tui(fake.url)
        self.assertTrue(tui.wait_for("● RX"), tui.vt.text())
        self.assertTrue(tui.wait_for("▲"), tui.vt.text())     # spectrum drawn
        screen = tui.vt.text()
        self.assertIn("Ham It Up switch: UPCONVERT", screen)
        self.assertIn("quit", screen.split("\n")[-1])
        self.assertIn("\x1b[?1049h", tui.raw.decode("utf-8", "ignore"))  # alternate screen

        tui.send(KEYS["right"])
        tui.pump(0.4)
        tui.send(KEYS["up"])          # step 5 kHz
        tui.pump(0.2)
        tui.send(b"\x1b[C")          # right arrow in normal cursor mode works too
        tui.pump(0.4)
        tui.send(b"m")
        tui.pump(0.4)
        tui.send(b"]")
        tui.pump(0.4)
        tui.send(b"G")
        tui.pump(0.4)
        tui.send(b"+")
        tui.pump(0.4)
        tui.send(b"f146.52\r")
        tui.pump(0.6)
        self.assertTrue(tui.wait_for("BYPASS"), tui.vt.text())  # 146.52 MHz: no upconverter
        self.assertEqual(fake.posts, [{"freq": 7079000}, {"freq": 7080000}, {"mode": "LSB"},
                                      {"gain_db": 7.7}, {"volume": 0.25},
                                      {"freq": 146520000}])
        tui.send(b"?")
        self.assertTrue(tui.wait_for("Any key closes this"), tui.vt.text())
        tui.send(b" ")
        tui.pump(0.3)
        tui.send(b"q")
        code, before, after = tui.finish()
        self.assertEqual(code, 0)
        self.assertEqual(after, before)                             # terminal restored
        self.assertIn("\x1b[?1049l", tui.raw.decode("utf-8", "ignore"))

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

    def test_reconnects_quietly(self):
        fake = FakeRxd()
        port = fake.server.server_address[1]
        fake.close()
        tui = self.run_tui(fake.url)
        self.assertTrue(tui.wait_for("NO RXD"))
        fake2 = _fake_on_port(port)
        self.addCleanup(fake2.close)
        self.assertTrue(tui.wait_for("● RX", 5), tui.vt.text())
        tui.send(b"q")
        self.assertEqual(tui.finish()[0], 0)


def _fake_on_port(port):
    """A FakeRxd listening on a given port."""
    orig = ThreadingHTTPServer.__init__

    def init(self, addr, handler, *a, **k):
        orig(self, ("127.0.0.1", port), handler, *a, **k)
    ThreadingHTTPServer.__init__ = init
    try:
        return FakeRxd()
    finally:
        ThreadingHTTPServer.__init__ = orig


if __name__ == "__main__":
    unittest.main()
