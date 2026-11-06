#!/usr/bin/env python3
"""rxd-tui: a terminal client for rxd, the RTL-SDR receiver daemon.

It talks only to rxd's HTTP API, by default at http://127.0.0.1:8073, and
has three views, like rxd's browser UI. The view follows the receiver's
profile; 1, 2 and 3 pick one.

  1 Digital  the digital modes, with a picker for their profiles grouped
             by mode (JS8: GhostNet or the band plan's; WSPR). On JS8 it
             shows rxd's decodes and the clock; on WSPR, its spots, the
             two-minute cycle and the reporting to WSPRnet, which r turns on
             and off. Nothing to tune.
  2 Listen   a picker of broadcast FM, VHF/UHF and AM stations, with gain,
             bandwidth, squelch (NFM) and the uConsole speaker's volume.
  3 Manual   the full instrument: tuning, spectrum and waterfall.

Picking a station or frequency switches rxd's profile; rxd restarts, which
takes a few seconds.

Times are shown in this machine's time zone ($TZ), 24-hour.

l plays the receiver's audio on this machine, through the first of pw-cat,
paplay, aplay, ffplay or sox's play on PATH (or --player), so the TUI works
as a remote listening station, on any profile.
[ ] set its volume here; M mutes the uConsole's own speaker meanwhile.
--listen starts with it on.

? shows each view's keys. A typed frequency (f, in Manual) is MHz when it is
under 2000 (7.078, 146.52), kHz under 100000 (14074) and Hz above that;
a unit (k, M, Hz) overrides this.

Status is shown with a symbol and a word as well as colour, and NO_COLOR is
honoured. --ascii draws without Unicode block characters.
"""
import argparse
import calendar
import collections
import curses
import json
import locale
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array

DEFAULT_URL = "http://127.0.0.1:8073"
STEPS = (10, 100, 1000, 5000, 12500, 100000, 1000000)  # same set as the web UI
DEFAULT_STEP = 1000
MODES = ("USB", "LSB", "AM", "FM", "WFM")
# Bandwidth presets for [ and ] in Manual. rxd's SSB filter runs from 200 Hz
# to 200 Hz plus the bandwidth, so 2800 is SDR++'s 3000 Hz filter.
BANDWIDTHS = {"USB": (500, 1000, 1800, 2400, 2800), "LSB": (500, 1000, 1800, 2400, 2800),
              "AM": (3000, 5000, 6000, 8000, 10000), "FM": (6250, 10000, 12500, 25000),
              "WFM": (100000, 150000, 200000)}
FREQ_MIN, FREQ_MAX = 100e3, 1.7e9      # rxd's tuning range
VOLUME_MAX = 4.0                       # rxd accepts 0 to 4
GAIN_FALLBACK_MAX = 50.0               # 1 dB steps when rxd reports no gain table
LEVEL_LO, LEVEL_HI = -110.0, -10.0     # signal bar span, dBFS

# The views, and which profiles belong to which (as in the browser UI).
VIEWS = (("digital", "Digital"), ("listen", "Listen"), ("manual", "Manual"))
# The Digital view's picker groups its profiles by decoder, in this order.
DECODERS = (("js8", "JS8"), ("wspr", "WSPR"))
LISTEN_MODES = ("WFM", "FM", "AM")
GROUPS = (("WFM", "Broadcast FM"), ("FM", "VHF · UHF"), ("AM", "AM"))
# Listen's b cycles these: Narrow, Normal, Wide.
LISTEN_BW = {"WFM": (100e3, 150e3, 200e3), "FM": (8e3, 12.5e3, 16e3), "AM": (6e3, 10e3, 15e3)}
BW_NAMES = ("Narrow", "Normal", "Wide")
SQUELCH_DEFAULT, SQUELCH_STEP = -60.0, 2.0       # dBFS
SQUELCH_MIN, SQUELCH_MAX = -120.0, 0.0
LOCAL_VOL_STEP, LOCAL_VOL_MAX = 10, 200          # percent, for audio played here
PLAYERS = ("pw-cat", "paplay", "aplay", "ffplay", "play")

STATUS_EVERY = 0.5      # seconds: about 2 a second
SPECTRUM_EVERY = 0.25   # about 4 a second, in Manual only
RETRY_EVERY = 1.0       # while rxd is unreachable
NO_SPECTRUM_EVERY = 5.0  # while rxd has no /api/spectrum
JS8_EVERY = 2.5         # the decode feed, on a JS8 profile
WSPR_EVERY = 3.0        # the spot feed, on a WSPR profile (spots come every 2 minutes)
PROFILES_RETRY = 10.0   # after /api/profiles failed
TIMEOUT = 0.8           # per request
SLOW_TIMEOUT = 20.0     # /api/profiles and the switch run `radio --json`
DROPPING_HOLD = 3.0     # "DROPPING" stays up this long after the count last rose
SWITCH_TIMEOUT = 25.0   # a profile switch that hasn't finished by then failed
WATERFALL_MAX = 64
JS8_KEEP = 500
AUDIO_STALL = 3.0       # seconds without audio before the stream is reopened
AUDIO_FRESH = 1.5       # audio this recent counts as playing


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


def short_mhz(hz):
    """105300000 -> '105.3 MHz'; 7078000 -> '7.078 MHz'."""
    return ("%.6f" % (float(hz) / 1e6)).rstrip("0").rstrip(".") + " MHz"


def station_freq(hz, mode):
    """The number and unit a station is known by: 105.3 MHz, or 1030 kHz on AM."""
    hz = float(hz)
    if mode == "AM" and hz < 30e6:
        return ("%.3f" % (hz / 1e3)).rstrip("0").rstrip("."), "kHz"
    return ("%.6f" % (hz / 1e6)).rstrip("0").rstrip("."), "MHz"


def percent(volume):
    return "%d %%" % round(float(volume) * 100)


def clip(text, width, g):
    """text cut to width, with an ellipsis if anything was cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    e = g.ellipsis if width > len(g.ellipsis) else ""
    return text[:width - len(e)] + e


# ---- profiles and views

def mode_label(mode):
    """rxd's FM is narrow FM; the browser UI calls it NFM too."""
    return "NFM" if mode == "FM" else (mode or "---")


def decoder_of(d):
    """What rxd decodes in a profile's (or a status's) audio: "js8", "wspr" or None."""
    return d.get("decoder") if d.get("decoder") in dict(DECODERS) else None


def profile_kind(p):
    """Which view a profile belongs to: digital, listen or manual."""
    if decoder_of(p):
        return "digital"
    return "listen" if p.get("mode") in LISTEN_MODES else "manual"


def status_kind(st):
    """The same for a status, when the profile list isn't available."""
    if not st or not st.get("mode"):
        return None
    if decoder_of(st):
        return "digital"
    return "listen" if st["mode"] in LISTEN_MODES else "manual"


def group_digital(profiles):
    """The Digital view's picker: [(mode title, [profiles])], JS8 then WSPR."""
    out = []
    for dec, title in DECODERS:
        members = [p for p in profiles if decoder_of(p) == dec]
        if members:
            out.append((title, members))
    return out


def group_stations(profiles):
    """Listen's picker: [(group title, [profiles])], in the browser UI's order."""
    ls = [p for p in profiles if profile_kind(p) == "listen"]
    out = []
    for mode, title in GROUPS:
        members = [p for p in ls if p.get("mode") == mode]
        if members:
            out.append((title, members))
    return out


def picker_list(view, profiles):
    """The profiles a view's picker offers, in order."""
    if view == "digital":
        return [p for _, members in group_digital(profiles) for p in members]
    if view == "listen":
        return [p for _, members in group_stations(profiles) for p in members]
    return []


def cycle_bandwidth(mode, bw):
    """Listen's b: the next of Narrow, Normal and Wide, round again after Wide.
    Off a preset, the next wider one. None for a mode without presets."""
    presets = LISTEN_BW.get(mode)
    if not presets:
        return None
    bw = float(bw or 0)
    for i, p in enumerate(presets):
        if abs(p - bw) < 1:
            return presets[(i + 1) % len(presets)]
    wider = [p for p in presets if p > bw]
    return wider[0] if wider else presets[0]


def bandwidth_name(mode, bw):
    for name, p in zip(BW_NAMES, LISTEN_BW.get(mode, ())):
        if bw is not None and abs(p - float(bw)) < 1:
            return name
    return "Custom"


NOSEND = object()


def squelch_key(key, squelch, level):
    """Listen's squelch keys: s toggles, , lowers and . raises the level.

    Returns (what to send as squelch, or NOSEND; the level to remember;
    a message, or "")."""
    if key == "s":
        if squelch is None:
            return level, level, ""
        return None, float(squelch), ""
    if squelch is None:
        return NOSEND, level, "Squelch is off: s turns it on"
    d = SQUELCH_STEP if key == "." else -SQUELCH_STEP
    new = clamp(float(squelch) + d, SQUELCH_MIN, SQUELCH_MAX)
    if new == squelch:
        return NOSEND, new, "Squelch at its %s: %d dBFS" % (
            "highest" if d > 0 else "lowest", new)
    return new, new, ""


def squelch_words(st, g):
    """(text, role) for the squelch: Off, or the level and whether it's open."""
    sq = st.get("squelch")
    a = audio_of(st)
    if sq is None and a and a["auto"] and a.get("squelch"):
        if st.get("squelch_open") is False:
            return "Auto  %s Closed (quiet)" % g.stop, "label"
        return "Auto  %s Open (signal)" % g.rx, "ok"
    if sq is None:
        return "Off, always open", "value"
    lvl = "%d dBFS" % round(sq)
    if st.get("squelch_open") is False:
        return "%s  %s Closed (quiet)" % (lvl, g.stop), "label"
    return "%s  %s Open (signal)" % (lvl, g.rx), "ok"


# ---- gain: auto RF gain and audio clean-up

# Manual's keys for each stage of rxd's audio clean-up: (key, field, words)
AUDIO_STAGES = (("n", "nr", "noise reduction"), ("h", "filter", "hum filter"),
                ("e", "norm", "even loudness"), ("c", "limit", "limiter"),
                ("S", "squelch", "NFM squelch"))
AUDIO_SHORT = {"nr": "NR", "filter": "hum", "norm": "loudness", "limit": "limiter",
               "squelch": "squelch"}


def audio_of(st):
    """rxd's audio settings with anything sent but not yet confirmed, or None
    for an rxd without them. nr is the effective state; nr_setting None is
    "by mode" (on except for WFM)."""
    a = st.get("audio") if st else None
    if not isinstance(a, dict) or not isinstance(a.get("auto"), bool):
        return None
    a = dict(a)
    for k in ("auto",) + tuple(f for _, f, _ in AUDIO_STAGES):
        if "audio_" + k in st:
            a[k] = st["audio_" + k]
    if "audio_nr" in st:
        a["nr_setting"] = st["audio_nr"]
        a["nr"] = st["mode"] != "WFM" if st["audio_nr"] is None else st["audio_nr"]
    return a


def gain_words(st):
    """The gain in use, and whether rxd picks it: "Auto 7.7 dB" or "7.7 dB"."""
    gain = st.get("gain_db") if st else None
    if gain is None:
        return "---"
    return ("Auto %.1f dB" if st.get("gain_auto") else "%.1f dB") % gain


def audio_words(st, short=False):
    """What the clean-up is doing: "Auto: NR, hum, loudness, limiter" or "Off"."""
    a = audio_of(st)
    if a is None:
        return "---"
    if not a["auto"]:
        return "Off, untouched"
    names = AUDIO_SHORT if short else {f: w for _, f, w in AUDIO_STAGES}
    on = [names[f] for _, f, _ in AUDIO_STAGES
          if a.get(f) and (f != "squelch" or st.get("mode") == "FM")]
    return "Auto: " + (", ".join(on) if on else "nothing on")


def audio_toggle(st, key):
    """(fields to send, message) for A (all of it) or a stage's key in Manual."""
    a = audio_of(st)
    if a is None:
        return None, "This rxd has no audio clean-up"
    if key == "A":
        on = not a["auto"]
        return {"audio_auto": on}, "Audio clean-up %s" % ("on" if on else "off, untouched")
    _, field, words = next(x for x in AUDIO_STAGES if x[0] == key)
    if not a["auto"]:
        return None, "Audio clean-up is off; A turns it on"
    if field == "squelch" and st.get("mode") != "FM":
        return None, "The noise squelch is for NFM only"
    on = not a.get(field)
    v = on
    if field == "nr" and on == (st.get("mode") != "WFM"):
        v = None   # what the mode does anyway: back to "by mode"
    return {"audio_" + field: v}, "%s %s" % (words[0].upper() + words[1:], "on" if on else "off")


def squelch_cycle(st, level):
    """Listen's s on an rxd with the noise squelch: Off, Auto, Level, Off..."""
    a = audio_of(st)
    auto = bool(a and a["auto"] and a.get("squelch"))
    if st.get("squelch") is not None:
        return {"squelch": None, "audio_squelch": False}, "Squelch off"
    if auto:
        return {"squelch": level}, "Squelch opens above %d dBFS" % round(level)
    return {"audio_squelch": True, "audio_auto": True}, "Squelch auto: opens for a signal"


def clock_words(clock, g, mode="JS8"):
    """(text, role) for the clock, which JS8 and WSPR need within about a second."""
    c = clock if isinstance(clock, dict) else {}
    if c.get("synced") is True:
        src = c.get("source")
        return "%s Clock synced%s" % (g.rx, " (%s)" % src if src else ""), "ok"
    if c.get("synced") is False:
        off = c.get("offset_s")
        extra = " (%.1f s off)" % abs(off) if isinstance(off, (int, float)) else ""
        return "%s Clock not synced: %s won't decode%s" % (g.bad, mode, extra), "bad"
    return "%s Clock unknown" % g.unknown, "warn"


def hiu_word(up):
    return "UPCONVERT" if up else "BYPASS"


def upconverter(st):
    """The station's upconverter as rxd reports it ({"name", "hz", "below_hz",
    "switch"}), or None when it has none (docs/station.md)."""
    return ((st or {}).get("station") or {}).get("upconverter")


def up_name(st):
    u = upconverter(st)
    return u["name"] if u else "Upconverter"


def manual_switch(st):
    """Whether the upconverter has a switch only a person can flip."""
    u = upconverter(st)
    return bool(u and u.get("switch"))


def flip_needed(p, st):
    """Whether switching to profile p means flipping the upconverter's switch."""
    if not manual_switch(st) or st.get("upconverter_hz") is None:
        return False
    return bool(p.get("upconvert")) != (st["upconverter_hz"] > 0)


# ---- JS8 decodes

def js8_mine(line, call):
    """Whether a decode is to (or mentions) the owner's callsign."""
    if line.get("mycall") is True:
        return True
    c = (call or "").upper()
    if not c:
        return False
    if (line.get("to") or "").upper() == c:
        return True
    text = str(line.get("text") or "").upper()
    sender = str(line.get("from") or "").upper()
    if sender and text.startswith(sender + ":"):
        text = text[len(sender) + 1:]  # the sender's own name isn't a mention
    return c in re.split(r"[^A-Z0-9/]+", text)


def js8_key(line):
    return " ".join([str(line.get("from") or ""), str(line.get("to") or ""),
                     str(line.get("text") or "")]).upper()


def js8_filter(lines, text):
    """The lines whose callsigns or text contain text (any case)."""
    return line_filter(lines, text, js8_key)


UTC_TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?")


def utc_epoch(t):
    """A UTC time from rxd ("2026-11-05 14:03:15" or "2026-11-06T12:20:00Z") as Unix
    time, or None."""
    m = UTC_TIME.match(str(t or "").strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(x or 0) for x in m.groups())
    try:
        return calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0))
    except (ValueError, OverflowError):
        return None


def local_time(t, seconds=True):
    """A UTC time from rxd in this machine's time zone, 24-hour: 08:03:15 (or 08:03)."""
    e = utc_epoch(t)
    if e is None:
        return None
    return time.strftime("%H:%M:%S" if seconds else "%H:%M", time.localtime(e))


def js8_time(t):
    """A decode's time, local. A bare time of day (no date) can't be converted and is
    shown as it came."""
    lt = local_time(t)
    if lt:
        return lt
    m = re.search(r"\d\d:\d\d:\d\d", str(t or ""))
    return m.group(0) if m else str(t or "--:--:--")[:8]


JS8_HEADER = "Time     SNR  From      Message"


def format_js8(line, call, width, g):
    """(text, mine) for one decode: time, SNR, sender, a mark if it's to us, text."""
    fr = str(line.get("from") or "?")
    text = str(line.get("text") or "")
    if text.upper().startswith(fr.upper() + ":"):
        text = text[len(fr) + 1:].strip()
    snr = line.get("snr")
    snr_t = "%+3d" % snr if isinstance(snr, int) else "  ?"
    mine = js8_mine(line, call)
    s = "%s %s  %-9s %s%s" % (js8_time(line.get("time")), snr_t, fr,
                              (g.mine + " ") if mine else "", text)
    return clip(s, width, g), mine


def stations_heard(lines, key="from"):
    return len({x.get(key) for x in lines if x.get(key)})


# ---- WSPR spots

KM_PER_MILE = 1.609344
WSPR_FORMAT = "%-5s  %-10s %-6s %5s %4s %4s %8s"
WSPR_HEADER = WSPR_FORMAT % ("Time", "Call", "Grid", "Miles", "SNR", "dBm", "Audio Hz")


def wspr_key(line):
    return " ".join(str(line.get(k) or "") for k in ("call", "grid", "text")).upper()


def line_filter(lines, text, key=js8_key):
    """The lines whose callsigns or text contain text (any case)."""
    f = (text or "").strip().upper()
    return [x for x in lines if f in key(x)] if f else list(lines)


def format_wspr(line, dial, width, g):
    """One spot: local time, call, grid, miles, SNR, dBm and the audio offset."""
    km = line.get("km")
    miles = "%d" % round(km / KM_PER_MILE) if isinstance(km, (int, float)) else ""
    snr, dbm, f = line.get("snr"), line.get("dbm"), line.get("freq")
    off = "%d" % round(f - dial) if isinstance(f, (int, float)) and \
        isinstance(dial, (int, float)) else ""
    s = WSPR_FORMAT % (local_time(line.get("time"), False) or "--:--",
                       str(line.get("call") or "?"), str(line.get("grid") or ""), miles,
                       "%+d" % snr if isinstance(snr, int) else "?",
                       "%d" % dbm if isinstance(dbm, int) else "?", off)
    return clip(s.rstrip(), width, g)


def slot_time(iso):
    return local_time(iso, False) or "--:--"


def wspr_cycle(w, g):
    """(text, role) for where the two-minute cycle is."""
    state = w.get("state")
    if state == "recording":
        return "%s Recording the %s slot" % (g.rx, slot_time(w.get("slot"))), "ok"
    if state == "decoding":
        slot = slot_time(w.get("decoding") or w.get("slot"))
        return "%s Decoding the %s slot" % (g.wait, slot), "value"
    if state == "waiting":
        return "%s Next slot %s" % (g.wait, slot_time(w.get("slot"))), "label"
    return "%s Cycle unknown" % g.unknown, "warn"


def wspr_last(w):
    last = w.get("last")
    if not isinstance(last, dict) or not last.get("slot"):
        return "No slot decoded yet"
    n = last.get("spots") or 0
    t = slot_time(last["slot"])
    if not n:
        return "No spots in the %s slot" % t
    return "%d spot%s in the %s slot" % (n, "" if n == 1 else "s", t)


def wspr_can_report(w):
    """Whether r can turn reporting on and off: rxd has a callsign and grid."""
    r = (w or {}).get("report")
    return isinstance(r, dict) and not r.get("reason")


def wspr_report_words(w, g):
    """(text, role) for the reporting to WSPRnet, in words as well as colour."""
    r = w.get("report")
    if not isinstance(r, dict):
        return "%s Reporting unknown" % g.unknown, "warn"
    if r.get("reason"):
        return "%s Not reporting: %s" % (g.stop, r["reason"]), "warn"
    who = " ".join(x for x in (r.get("call"), r.get("grid")) if x)
    if not r.get("enabled"):
        return "%s Not reporting to WSPRnet (r reports as %s)" % (g.stop, who), "label"
    pending = r.get("pending") or 0
    if r.get("online") is False:
        return "%s Offline: %d spot%s waiting" % (g.drop, pending, "" if pending == 1 else "s"), \
            "warn"
    if r.get("error"):
        return "%s %s" % (g.bad, r["error"]), "bad"
    text = "%s Reporting to WSPRnet as %s %s %d sent" % (g.rx, who, g.dot, r.get("sent") or 0)
    if pending:
        text += ", %d waiting" % pending
    return text, "ok"


# ---- the band plan (rxd's status band_plan; POST /api/bands)

def band_plan_words(bp, g):
    """(text, role) for the band plan line: Auto or Manual, and why this band."""
    if not isinstance(bp, dict):
        return None
    auto = bp.get("plan") == "auto"
    mode = "WSPR" if bp.get("mode") == "wspr" else "JS8"
    head = "Band plan Auto %s %s" % (g.dot, mode) if auto else "Band plan Manual"
    phase = bp.get("phase")
    sym = g.stop if not auto else g.wait if phase == "scan" else g.drop if phase == "idle" else g.rx
    reason = bp.get("reason") or ""
    if reason.startswith("Manual: "):
        reason = reason[len("Manual: "):]
    role = "warn" if auto and phase == "idle" else "ok" if auto else "label"
    return "%s %s: %s" % (sym, head, reason), role


def station_words(st, g):
    """(text, role) for the station line: its grid and where it comes from,
    "Station EN52lk from GPS (10 satellites)", or None from an older rxd."""
    s = (st or {}).get("station") or {}
    reason = s.get("grid_reason")
    if not reason:
        return None
    src = s.get("grid_source")
    gps = (s.get("gps") or {}).get("state")
    sym = g.rx if src == "gps" else g.bad if not src else g.drop if gps == "rejected" else g.dot
    role = "ok" if src == "gps" else "bad" if not src else "warn" if gps == "rejected" else "label"
    return "%s Station %s" % (sym, reason), role


def band_plan_extra(bp, g):
    """The second band plan line: conditions (credited) and the sun, or why there are none."""
    if not isinstance(bp, dict):
        return None
    sun = bp.get("sun") or {}
    words = (sun.get("words") or "").capitalize()
    c = bp.get("conditions")
    if c:
        who = "N0NBH, hamqsl.com" if c.get("source") == "hamqsl" else "NOAA SWPC"
        text = "SFI %s %s K %s (%s)" % (c.get("sfi"), g.dot, c.get("k"), who)
    else:
        text = "No band conditions (offline?)"
    if words:
        text += "   %s" % words
    if bp.get("plan") == "auto" and bp.get("phase") == "hold" and bp.get("next_at"):
        text += "   next look %s" % time.strftime("%H:%M", time.localtime(bp["next_at"]))
    return text


# ---- audio here

def rate_for(mode):
    """The stream rate: 24 kHz carries broadcast FM's audio, 12 kHz is plenty for voice."""
    return 24000 if mode == "WFM" else 12000


def find_player(which=shutil.which):
    """(name, path) of the first raw-PCM player on PATH, or None."""
    for name in PLAYERS:
        path = which(name)
        if path:
            return name, path
    return None


def ffplay_channels(help_text):
    """ffmpeg 7 dropped the raw demuxers' -ac for -ch_layout; older ones lack it."""
    return ["-ch_layout", "mono"] if "ch_layout" in help_text else ["-ac", "1"]


def probe_ffplay(path):
    try:
        out = subprocess.run([path, "-hide_banner", "-h", "demuxer=s16le"],
                             capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ["-ac", "1"]
    return ffplay_channels(out.stdout + out.stderr)


def player_command(name, path, rate, channels=None):
    """The argv that plays raw s16le mono at rate from stdin."""
    r = str(int(rate))
    if name == "pw-cat":
        return [path, "--playback", "--raw", "--format", "s16", "--rate", r, "--channels", "1", "-"]
    if name == "paplay":
        return [path, "--raw", "--format=s16le", "--rate=" + r, "--channels=1"]
    if name == "aplay":
        return [path, "-q", "-t", "raw", "-f", "S16_LE", "-r", r, "-c", "1"]
    if name == "ffplay":
        return [path, "-nodisp", "-loglevel", "quiet", "-f", "s16le", "-ar", r] + \
            list(channels or ["-ac", "1"]) + ["-"]
    if name == "play":
        return [path, "-q", "-t", "raw", "-e", "signed", "-b", "16", "-c", "1", "-r", r, "-"]
    raise ValueError("no such player: %s" % name)


def custom_command(cmd, rate):
    """--player's command, with {rate} replaced by the sample rate."""
    return [a.replace("{rate}", str(int(rate))) for a in shlex.split(cmd)]


def no_player_hint(platform=sys.platform):
    if platform == "darwin":
        return "No audio player found. Install one: brew install ffmpeg (or sox), or use --player"
    return "No audio player found. Install pipewire-bin, alsa-utils, ffmpeg or sox"



WAITING = "Waiting for rxd; 3 opens Manual meanwhile"

class Player:
    """How to start the local player: a name for the status and an argv per rate."""

    def __init__(self, name, build):
        self.name = name
        self.build = build

    @classmethod
    def found(cls, name, path):
        chans = []

        def build(rate):
            if name == "ffplay" and not chans:
                chans.extend(probe_ffplay(path))
            return player_command(name, path, rate, chans)
        return cls(name, build)

    @classmethod
    def custom(cls, cmd):
        argv = shlex.split(cmd)
        if not argv:
            raise ValueError("--player needs a command")
        return cls(os.path.basename(argv[0]), lambda rate: custom_command(cmd, rate))


def scale_pcm(data, gain):
    """s16le samples times gain, clamped to 16 bits. data must be whole samples."""
    if gain == 1.0:
        return data
    if gain <= 0:
        return bytes(len(data))
    a = array("h")
    a.frombytes(data)
    if sys.byteorder == "big":
        a.byteswap()
    out = array("h", [-32768 if v < -32768 else 32767 if v > 32767 else v
                      for v in (int(s * gain) for s in a)])
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


# ---- glyphs and drawing helpers

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
            self.unknown, self.mine, self.cursor, self.dot = "?", ">", ">", "/"
            self.keys = {"left": "<-", "right": "->", "up": "Up", "down": "Dn", "sep": "/",
                         "updown": "Up/Dn"}
            self.ellipsis = "..."
        else:
            self.bars = " ▁▂▃▄▅▆▇█"
            self.shade = " ░▒▓█"
            self.level = ("█", "░", " ▏▎▍▌▋▊▉")
            self.dial, self.band, self.left, self.right = "▲", "━", "◀", "▶"
            self.rx, self.drop, self.bad, self.stop, self.wait = "●", "▲", "✕", "■", "…"
            self.unknown, self.mine, self.cursor, self.dot = "?", "◆", "▸", "·"
            self.keys = {"left": "←", "right": "→", "up": "↑", "down": "↓", "sep": "",
                         "updown": "↑↓"}
            self.ellipsis = "…"

    def text(self, s):
        """Text written with Unicode punctuation, made ASCII when it must be."""
        if not self.ascii:
            return s
        for a, b in (("·", "/"), ("…", "..."), ("●", "*"), ("▲", "!"), ("✕", "x"),
                     ("■", "#"), ("◆", ">"), ("▸", ">"), ("↑↓", "Up/Dn"), ("—", "-")):
            s = s.replace(a, b)
        return s


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


def badge(reachable, status, dropping, g, switching=None):
    """(symbol, word, role) for the receiver's state. Never colour alone."""
    if switching:
        return g.stop, "SWITCHING", "warn"
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


SHORT_LABELS = {"bandwidth": "bw", "volume": "vol", "speaker": "spkr", "station": "pick",
                "switch": "pick", "squelch": "sq", "listen": "listen", "filter": "filter",
                "reporting": "report", "auto gain": "auto g", "auto audio": "auto a",
                "clean-up": "clean"}


def help_items(g, view="manual", mode=None, listening=False, decoder=None, report=False,
               plan=False):
    """[(keys, label)] for a view's bottom line; ? and q always come last.
    In Digital, decoder is the receiver's ("js8", "wspr" or None) and report
    whether r can switch WSPR reporting, plan whether rxd has a band plan."""
    k = g.keys
    ud = k["updown"]
    if view == "digital":
        # When they don't all fit, the ones just before the view keys go first.
        items = [(ud + " Enter", "switch")] + ([("a", "auto")] if plan else [])
        if decoder == "wspr" and report:
            items.append(("r", "reporting"))
        items += [("l", "listen")] + ([("[ ]", "volume")] if listening else [])
        items += [("PgUp PgDn", "scroll"), ("/", "filter"), ("1 2 3", "view")]
    elif view == "listen":
        items = [(ud + " Enter", "station"), ("g G", "gain"), ("a", "auto gain"),
                 ("A", "auto audio"), ("b", "bandwidth")]
        if mode == "FM":
            items.append(("s , .", "squelch"))
        items += [("l", "listen")] + ([("[ ]", "volume")] if listening else []) + \
            [("- +", "speaker"), ("M", "mute"), ("1 2 3", "view")]
    else:
        items = [(k["left"] + k["sep"] + k["right"], "tune"), (k["up"] + k["sep"] + k["down"], "step"),
                 ("f", "freq"), ("m", "mode"), ("l", "listen"), ("[ ]", "bandwidth"),
                 ("g G", "gain"), ("a", "auto gain"), ("A n h e c", "clean-up")] + \
            [("- +", "volume"), ("1 2 3", "view")]
    return items + [("?", "help"), ("q", "quit")]


def help_line(width, g, items=None):
    """[(text, role)] for the bottom line: full labels if they fit, then shorter,
    then fewer items; ? help and q quit always stay."""
    items = list(items or help_items(g))

    def build(its, short, gap):
        parts = []
        for key, label in its:
            if short:
                label = SHORT_LABELS.get(label, label)
            if parts:
                parts.append((gap, "help"))
            parts += [(key, "key"), (" " + label, "help")]
        return parts

    def size(parts):
        return sum(len(t) for t, _ in parts)
    for short, gap in ((False, "  "), (True, "  "), (True, " ")):
        parts = build(items, short, gap)
        if size(parts) <= width:
            return parts
    while len(items) > 3:
        items.pop(-4)  # the view keys, ? and q always stay
        parts = build(items, True, " ")
        if size(parts) <= width:
            return parts
    return parts  # the caller clips


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

    def request(self, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                doc = json.loads(e.read() or b"{}")
            except ValueError:
                doc = {}
            return e.code, doc if isinstance(doc, dict) else {}
        except ValueError as e:  # a reply that isn't JSON
            raise OSError("bad reply from rxd: %s" % e)

    def get(self, path, timeout=None):
        return self.request(path, timeout=timeout)

    def post(self, path, body, timeout=None):
        return self.request(path, body, timeout=timeout)


# The rxd HTTP API this client speaks: /api/v1/ (rxd's docs/API.md).
API_VERSION = 1


def api_mismatch(code, doc):
    """Why this rxd can't be talked to, when it speaks another API: None if it
    speaks ours."""
    has = doc.get("api_version") if isinstance(doc, dict) else None
    if code == 404 and has is None:
        return "this rxd has no API v1; rxd-tui needs rxd 0.3.0 or later"
    if has is not None and has != API_VERSION:
        return "this rxd speaks API version %s; this rxd-tui speaks %d" % (has, API_VERSION)
    if code == 200 and has is None:
        return "this rxd doesn't say its API version; rxd-tui needs rxd 0.3.0 or later"
    return None


def reason(exc):
    """A short why for an unreachable rxd."""
    r = getattr(exc, "reason", exc)
    if isinstance(r, ConnectionRefusedError):
        return "connection refused"
    if isinstance(r, (TimeoutError, OSError)) and "timed out" in str(r):
        return "no answer"
    text = getattr(r, "strerror", None) or str(r)
    return text.strip() or r.__class__.__name__


class Poller:
    """Talks to rxd off the curses thread.

    One thread polls status (and the spectrum, in Manual) and sends tunes;
    another does the slow things: the profile list, profile switches and
    the decoder's feed (JS8 decodes or WSPR spots)."""

    def __init__(self, link, clock=time.monotonic):
        self.link = link
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.side_wake = threading.Event()
        self.changed = threading.Event()
        self.halt = threading.Event()
        self.reachable = None      # None until the first answer or failure
        self.why = ""
        self.status = {}
        self.spectrum = None
        self.spec_state = "waiting"  # waiting, ok or missing (no /api/spectrum)
        self.want_spectrum = True
        self.history = collections.deque(maxlen=WATERFALL_MAX)
        self.dropping_until = 0.0
        self.pending = {}
        self.acks = []             # (sent, error or None), drained by the UI
        self.profiles = None       # rxd's {"active", "profiles"}
        self.profiles_state = "loading"  # loading, ok, missing (old rxd) or error
        self.profiles_error = ""
        self.want_profiles = True
        self.profiles_asked_for = None
        self.profiles_retry_at = 0.0
        self.switch_req = None
        self.switch_acks = []      # (name, error or None)
        self.bands_req = None      # a POST /api/bands body, for the side thread
        self.bands_acks = []       # (body, error or None)
        # The decoder's feed: one of these fills at a time, the other is empty.
        self.js8_lines = collections.deque(maxlen=JS8_KEEP)
        self.wspr_lines = collections.deque(maxlen=JS8_KEEP)
        self.js8_state = self.wspr_state = "waiting"  # waiting, ok, missing or error
        self.feed_seq = 0
        self.feed_for = None       # (profile, decoder) the lines are from
        self.threads = []

    # called from the UI thread
    def start(self):
        for name, fn in (("rxd-poller", self.run), ("rxd-side", self.side)):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self.threads.append(th)

    def tune(self, **fields):
        with self.lock:
            self.pending.update(fields)
        self.wake.set()

    def take_acks(self):
        with self.lock:
            acks, self.acks = self.acks, []
        return acks

    def switch(self, name):
        with self.lock:
            self.switch_req = name
        self.side_wake.set()

    def take_switch_acks(self):
        with self.lock:
            acks, self.switch_acks = self.switch_acks, []
        return acks

    def bands(self, body):
        """The band plan's settings (Auto/Manual, mode, scan now), sent by the side thread."""
        with self.lock:
            self.bands_req = dict(self.bands_req or {}, **body)
        self.side_wake.set()

    def take_bands_acks(self):
        with self.lock:
            acks, self.bands_acks = self.bands_acks, []
        return acks

    def reload_profiles(self):
        with self.lock:
            self.want_profiles = True
            self.profiles_retry_at = 0.0
        self.side_wake.set()

    def dropping(self):
        return self.clock() < self.dropping_until

    def stop(self):
        self.halt.set()
        self.wake.set()
        self.side_wake.set()

    # the status thread
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
            if not self.want_spectrum:
                next_spec = next_status
            elif now >= next_spec:
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
            code, doc = self.link.get("/api/v1/status")
        except OSError as e:
            self._offline(e)
            return False
        why = api_mismatch(code, doc)
        if why:
            self._offline(OSError(why))
            return False
        if code != 200:
            self._offline(OSError("HTTP %d from /api/v1/status" % code))
            return False
        self._set_status(doc)
        self.changed.set()
        return True

    def _spectrum(self):
        try:
            code, doc = self.link.get("/api/v1/spectrum")
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
            code, doc = self.link.post("/api/v1/tune", req)
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

    # the side thread: profiles, switches and the decoder's feed
    def side(self):
        next_feed = 0.0
        while not self.halt.is_set():
            self.side_wake.clear()
            with self.lock:
                name, self.switch_req = self.switch_req, None
                breq, self.bands_req = self.bands_req, None
                st, reachable = dict(self.status), self.reachable
            if breq:
                self._bands(breq)
                continue
            if name:
                self._switch(name)
                continue
            if reachable and self._profiles_due(st):
                self._load_profiles(st)
                continue
            now = self.clock()
            dec = decoder_of(st)
            if reachable and dec:
                if (st.get("profile"), dec) != self.feed_for:
                    self._feed_reset((st.get("profile"), dec))
                    next_feed = 0.0
                if now >= next_feed:
                    self._feed(dec)
                    next_feed = self.clock() + (WSPR_EVERY if dec == "wspr" else JS8_EVERY)
            elif reachable and self.feed_for is not None:
                self._feed_reset(None)
            self.side_wake.wait(clamp(next_feed - self.clock(), 0.05, 0.5) if dec else 0.5)

    def _profiles_due(self, st):
        with self.lock:
            if self.clock() < self.profiles_retry_at:
                return False
            if self.want_profiles:
                return True
            # rxd moved to another profile (switched by `radio` or the web UI)
            act = self.profiles.get("active") if self.profiles else None
            name = st.get("profile")
            return bool(self.profiles and name and name != act and name != self.profiles_asked_for)

    def _load_profiles(self, st):
        with self.lock:
            self.profiles_asked_for = st.get("profile")
        try:
            code, doc = self.link.get("/api/v1/profiles", timeout=SLOW_TIMEOUT)
        except OSError as e:
            with self.lock:
                self.profiles_error = "can't reach rxd (%s)" % reason(e)
                self.profiles_retry_at = self.clock() + RETRY_EVERY
            return
        with self.lock:
            self.want_profiles = False
            if code == 200 and isinstance(doc.get("profiles"), list):
                self.profiles, self.profiles_state, self.profiles_error = doc, "ok", ""
            elif code == 404:
                self.profiles, self.profiles_state = None, "missing"
            else:
                self.profiles_state = "error" if self.profiles is None else "ok"
                self.profiles_error = doc.get("error") or "HTTP %d" % code
                self.want_profiles = True
                self.profiles_retry_at = self.clock() + PROFILES_RETRY
        self.changed.set()

    def _switch(self, name):
        try:
            code, doc = self.link.post("/api/v1/profile", {"name": name}, timeout=SLOW_TIMEOUT)
            err = None if code in (200, 202) else \
                "rxd refused: %s" % (doc.get("error") or "HTTP %d" % code)
        except OSError as e:
            err = "Not switched: can't reach rxd (%s)" % reason(e)
        with self.lock:
            self.switch_acks.append((name, err))
        self.changed.set()

    def _bands(self, body):
        try:
            code, doc = self.link.post("/api/v1/bands", body, timeout=SLOW_TIMEOUT)
            err = None if code == 200 else \
                "rxd refused: %s" % (doc.get("error") or "HTTP %d" % code)
        except OSError as e:
            err = "Not sent: can't reach rxd (%s)" % reason(e)
            doc = {}
        with self.lock:
            self.bands_acks.append((body, err))
            if err is None and isinstance(doc.get("status"), dict):
                self.status = dict(self.status, band_plan=doc["status"])
        self.changed.set()

    def _feed_reset(self, key):
        with self.lock:
            self.js8_lines.clear()
            self.wspr_lines.clear()
            self.feed_seq = 0
            self.feed_for = key
            self.js8_state = self.wspr_state = "waiting"
        self.changed.set()

    def _feed(self, dec):
        """New lines from /api/js8 or /api/wspr, which number them the same way."""
        lines = self.wspr_lines if dec == "wspr" else self.js8_lines
        try:
            code, doc = self.link.get("/api/v1/%s?after=%d" % (dec, self.feed_seq), timeout=3.0)
        except OSError:
            return  # the status thread reports rxd's absence
        with self.lock:
            if code == 200 and isinstance(doc.get("lines"), list):
                seq = doc.get("seq") if isinstance(doc.get("seq"), int) else self.feed_seq
                if seq < self.feed_seq:  # rxd restarted and numbers from 1 again
                    lines.clear()
                    self.feed_seq = 0
                    self.side_wake.set()
                    return
                new = sorted((x for x in doc["lines"] if isinstance(x, dict)
                              and (x.get("seq") or 0) > self.feed_seq),
                             key=lambda x: x.get("seq") or 0)
                lines.extend(new)
                self.feed_seq = max(self.feed_seq, seq)
                state = "ok"
            elif code == 404:
                state = "missing"
            else:
                state = "error: " + (doc.get("error") or "HTTP %d" % code)
            setattr(self, dec + "_state", state)
        self.changed.set()


# ------------------------------------------------------------ audio here

class Refused(Exception):
    """rxd answered the audio request with an error: retry slowly."""


class AudioPlayer:
    """rxd's /api/audio piped into a local player, in a background thread.

    It reconnects when the stream drops (as it does across a profile switch),
    restarts the player when the rate changes with the mode, scales the
    samples for the volume here, and kills the player when it stops."""

    def __init__(self, url, player, changed=None, clock=time.monotonic):
        u = urllib.parse.urlsplit(url)
        self.scheme = u.scheme
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 80
        self.base = u.path.rstrip("/")
        self.player = player
        self.changed = changed or threading.Event()
        self.clock = clock
        self.lock = threading.Lock()
        self.on = False
        self.rate = 12000
        self.gain = 1.0
        self.state = "off"     # off, connecting, playing, reconnecting, error
        self.error = ""
        self.play_rate = None
        self.last_data = 0.0
        self.bytes = 0
        self.proc = None
        self.sock = None
        self.halt = None
        self.thread = None

    # called from the UI thread
    def start(self, rate):
        if self.on:
            return
        self.rate = rate
        self.on = True
        self.halt = threading.Event()
        self._set("connecting")
        self.thread = threading.Thread(target=self.run, args=(self.halt,), name="rxd-audio",
                                       daemon=True)
        self.thread.start()

    def stop(self, wait=2.0):
        if not self.on:
            return
        self.on = False
        self.halt.set()
        self._close_sock()
        self._kill(self.proc)
        if self.thread:
            self.thread.join(wait)
        self._set("off")

    def set_rate(self, rate):
        self.rate = rate

    def words(self, g):
        """(symbol, text, role) for the audio's state."""
        with self.lock:
            state, err, rate, last = self.state, self.error, self.play_rate, self.last_data
        if not self.on:
            if err:
                return g.bad, "Audio stopped: " + err, "bad"
            return g.stop, "Not listening here", "dim"
        if state == "error":
            return g.bad, "Audio: %s Retrying." % err, "bad"
        if state == "playing" and self.clock() - last < AUDIO_FRESH:
            return g.rx, "Listening on this machine (%s, %s kHz)" % (
                self.player.name, ("%.1f" % (rate / 1000)).rstrip("0").rstrip(".")), "ok"
        if state == "connecting":
            return g.drop, "Connecting audio", "warn"
        return g.drop, "Reconnecting", "warn"

    # the thread
    def _set(self, state, error=""):
        with self.lock:
            changed = (state, error) != (self.state, self.error)
            self.state, self.error = state, error
        if changed:
            self.changed.set()

    def _close_sock(self):
        s, self.sock = self.sock, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    @staticmethod
    def _kill(proc):
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(1.0)

    def connect(self, rate):
        """A socket positioned at the PCM, and the stream's rate. Raises Refused or OSError."""
        if self.scheme not in ("http", ""):
            raise Refused("audio needs an http:// rxd URL.")
        s = socket.create_connection((self.host, self.port), timeout=3.0)
        try:
            s.sendall(("GET %s/api/v1/audio?rate=%d HTTP/1.0\r\nHost: %s\r\n\r\n"
                       % (self.base, rate, self.host)).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = s.recv(4096)
                if not data:
                    raise OSError("rxd closed the audio stream")
                buf += data
                if len(buf) > 65536:
                    raise OSError("bad audio reply from rxd")
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            m = re.match(r"HTTP/\d\.\d (\d+)", lines[0])
            if not m:
                raise OSError("bad audio reply from rxd")
            code = int(m.group(1))
            headers = dict((k.strip().lower(), v.strip()) for k, _, v in
                           (x.partition(":") for x in lines[1:]))
            if code != 200:
                body = rest
                try:
                    while len(body) < 65536:
                        data = s.recv(4096)
                        if not data:
                            break
                        body += data
                except OSError:
                    pass
                try:
                    err = json.loads(body or b"{}").get("error")
                except (ValueError, AttributeError):
                    err = None
                if code == 404:
                    raise Refused("this rxd has no audio stream.")
                raise Refused("rxd refused: %s." % (err or "HTTP %d" % code))
            try:
                got = int(headers.get("x-audio-rate", rate))
            except ValueError:
                got = rate
            return s, got, rest
        except BaseException:
            s.close()
            raise

    def run(self, halt):
        proc, proc_rate, retries = None, None, 0
        try:
            while not halt.is_set():
                rate = self.rate
                try:
                    s, got, first = self.connect(rate)
                except Refused as e:
                    self._set("error", str(e))
                    halt.wait(5.0)
                    continue
                except OSError:
                    retries += 1
                    self._set("reconnecting" if self.bytes or retries > 1 else "connecting")
                    halt.wait(min(0.5 * retries, 3.0))
                    continue
                self.sock = s
                if halt.is_set():
                    break
                if proc is None or proc_rate != got or proc.poll() is not None:
                    self._kill(proc)
                    proc = None
                    try:
                        argv = self.player.build(got)
                        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, bufsize=0,
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL,
                                                start_new_session=True)
                    except (OSError, ValueError) as e:
                        self._close_sock()
                        self._set("error", "can't start %s (%s)." % (self.player.name, e))
                        halt.wait(5.0)
                        continue
                    self.proc, proc_rate = proc, got
                with self.lock:
                    self.play_rate = got
                ended = self.pump(s, proc, first, rate, halt)
                self._close_sock()
                if ended == "player":
                    self._kill(proc)
                    proc = None
                    self._set("error", "%s stopped." % self.player.name)
                    halt.wait(2.0)
                elif ended == "rate":
                    retries = 0
                elif not halt.is_set():
                    retries += 1
                    self._set("reconnecting")
                    halt.wait(min(0.5 * retries, 3.0))
        finally:
            self._close_sock()
            self._kill(proc)
            self.proc = None

    def pump(self, s, proc, first, rate, halt):
        """Copy PCM to the player until something ends it; says what did."""
        s.settimeout(0.5)
        carry = b""
        last = self.clock()
        data = first
        while not halt.is_set():
            if self.rate != rate:
                return "rate"
            if data:
                data = carry + data
                n = len(data) & ~1
                carry, data = data[n:], data[:n]
                try:
                    proc.stdin.write(scale_pcm(data, self.gain))
                except (BrokenPipeError, OSError, ValueError):
                    return "player"
                now = self.clock()
                last = now
                self.bytes += n
                with self.lock:
                    self.last_data = now
                    was = self.state
                    self.state, self.error = "playing", ""
                if was != "playing":
                    self.changed.set()
            elif self.clock() - last > AUDIO_STALL:
                return "stall"
            if proc.poll() is not None:
                return "player"
            try:
                data = s.recv(8192)
            except socket.timeout:
                data = b""
                continue
            except OSError:
                return "closed"
            if not data:
                return "closed"
        return "halt"


# ------------------------------------------------------------------ drawing

class Canvas:
    """A plain-text screen, for tests."""

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

    def role_at(self, y, x):
        return self.roles[y][x]


class CursesScreen:
    ROLES = ("bar", "tabon", "big", "dim", "label", "value", "ok", "warn", "bad", "spec", "wf",
             "marker", "key", "help", "msg", "err", "upconv", "sel", "mine", "head")

    def __init__(self, win, color):
        self.win = win
        self.h, self.w = win.getmaxyx()
        self.attrs = self.make_attrs(color)

    @staticmethod
    def make_attrs(color):
        b, r, d = curses.A_BOLD, curses.A_REVERSE, getattr(curses, "A_DIM", 0)
        a = {"bar": r, "tabon": b, "big": b, "dim": d, "label": 0, "value": b, "ok": b,
             "warn": b, "bad": b | r, "spec": 0, "wf": 0, "marker": b, "key": r, "help": 0,
             "msg": 0, "err": b, "upconv": b, "sel": r, "mine": b | r, "head": b}
        if color:
            pairs = {"ok": curses.COLOR_GREEN, "warn": curses.COLOR_YELLOW,
                     "bad": curses.COLOR_RED, "err": curses.COLOR_RED,
                     "spec": curses.COLOR_CYAN, "marker": curses.COLOR_YELLOW,
                     "big": curses.COLOR_WHITE, "upconv": curses.COLOR_YELLOW,
                     "mine": curses.COLOR_CYAN}
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
    def __init__(self, poller, glyphs, url=DEFAULT_URL, audio=None, player_hint="",
                 clock=time.monotonic, listen=False):
        self.p = poller
        self.g = glyphs
        self.url = url
        self.audio = audio          # an AudioPlayer, or None when no player was found
        self.player_hint = player_hint or no_player_hint()
        self.clock = clock
        self.step = DEFAULT_STEP
        self.overrides = {}     # values sent but not yet confirmed by rxd
        self.message = ""       # the result of the last action
        self.message_role = "msg"
        self.entry = None       # text being typed, or None
        self.entry_kind = None  # "freq" or "filter"
        self.show_help = False
        self.lo = self.hi = None  # smoothed spectrum range
        self.wf_cache = {}      # waterfall rows already resampled to the screen
        # views
        self.view = None        # digital, listen or manual; None until rxd says
        self.view_for = None    # the profile the view was chosen for
        self.pinned = False     # Manual, chosen by the user, stays
        self.cursor = 0
        self.cursor_key = None
        self.switching = None   # {"name", "desc", "prev_up", "want_up", "since"}
        self.sq_level = SQUELCH_DEFAULT
        self.mute_prev = None   # the uConsole volume M will restore
        self.local_vol = 100    # percent, for audio played here
        # the Digital view's list (JS8 decodes or WSPR spots)
        self.js8_filter = ""
        self.js8_scroll = 0     # lines back from the newest
        self.js8_seen = None    # (profile, matching lines) at the last draw
        self.js8_page = 10      # rows on screen, for PgUp and PgDn
        self.want_listen = listen  # --listen: start once rxd says which mode

    # ---- state
    def snap(self):
        with self.p.lock:
            s = dict(status=dict(self.p.status), spectrum=self.p.spectrum,
                     history=list(self.p.history), reachable=self.p.reachable, why=self.p.why,
                     spec_state=self.p.spec_state, profiles=self.p.profiles,
                     profiles_state=self.p.profiles_state,
                     profiles_error=getattr(self.p, "profiles_error", ""),
                     js8=list(self.p.js8_lines), js8_state=self.p.js8_state,
                     wspr=list(getattr(self.p, "wspr_lines", ())),
                     wspr_state=getattr(self.p, "wspr_state", "waiting"))
        st = s["status"]
        st.update(self.overrides)
        w = st.get("wspr")
        if "wspr_report" in self.overrides and isinstance(w, dict) and \
                isinstance(w.get("report"), dict):  # sent, not yet confirmed
            st["wspr"] = dict(w, report=dict(w["report"], enabled=self.overrides["wspr_report"]))
        return s

    def decoder(self, s):
        """The receiver's decoder: "js8", "wspr" or None."""
        return decoder_of(s["status"]) if s["status"] else None

    def profile_list(self, s):
        return (s["profiles"] or {}).get("profiles") or []

    def active_name(self, s):
        return s["status"].get("profile") or (s["profiles"] or {}).get("active")

    def active_profile(self, s):
        n = self.active_name(s)
        return next((p for p in self.profile_list(s) if p.get("name") == n), None)

    def kind(self, s):
        p = self.active_profile(s)
        return profile_kind(p) if p else status_kind(s["status"])

    def mismatch(self, s):
        k = self.kind(s)
        return self.view in ("digital", "listen") and k is not None and k != self.view

    def drain(self):
        for sent, err in self.p.take_acks():
            for k, v in sent.items():
                if self.overrides.get(k) == v:
                    del self.overrides[k]
            if err:
                self.say(err, "err")
        for name, err in self.p.take_switch_acks():
            if err and self.switching and self.switching["name"] == name:
                self.switching = None
                self.say(err, "err")
        for body, err in (self.p.take_bands_acks() if hasattr(self.p, "take_bands_acks") else ()):
            if err:
                self.say(err, "err")

    def update(self):
        """Everything that follows from rxd's state: views, switches, the audio rate."""
        self.drain()
        s = self.snap()
        st = s["status"]
        self.p.want_spectrum = self.view == "manual"
        sw = self.switching
        if sw:
            if s["reachable"] and st.get("profile") == sw["name"]:
                self.finish_switch(st)
            elif self.clock() - sw["since"] > SWITCH_TIMEOUT:
                self.switching = None
                self.say("%s The switch to %s did not finish. %s" % (
                    self.g.bad, sw["name"],
                    "rxd is still on %s." % st.get("profile") if s["reachable"]
                    else "rxd has not come back after %d seconds." % SWITCH_TIMEOUT), "err")
                self.p.reload_profiles()
        if s["profiles_state"] == "missing":
            self.view = "manual"  # an rxd that can't switch profiles: the instrument only
        elif self.pinned:
            self.view = "manual"
            self.view_for = self.active_name(s)
        else:
            n, k = self.active_name(s), self.kind(s)
            if n and k and n != self.view_for:
                self.view_for, self.view = n, k
                self.js8_scroll = 0
        if self.mute_prev is not None and st.get("volume") and not self.switching \
                and "volume" not in self.overrides and s["reachable"]:
            self.mute_prev = None  # the speaker was turned up elsewhere
        if self.audio and self.want_listen and st.get("mode"):
            self.want_listen = False
            self.audio.start(rate_for(st["mode"]))
        if self.audio and self.audio.on and st.get("mode"):
            self.audio.set_rate(rate_for(st["mode"]))
        if self.audio:
            self.audio.gain = self.local_vol / 100.0
        return s

    def finish_switch(self, st):
        sw, self.switching = self.switching, None
        up = (st.get("upconverter_hz") or 0) > 0
        if up != sw["prev_up"] and sw["manual"]:
            self.say("%s Flip the %s to %s for %s; the wrong way is silent."
                     % (self.g.drop, sw["up_name"], hiu_word(up), sw["desc"]), "warn")
        else:
            self.say("%s Now on %s" % (self.g.rx, sw["desc"]), "ok")
        if sw.get("muted") is not None and st.get("volume"):
            self.mute_prev = st["volume"]
            self.send(volume=0.0)
        self.js8_scroll = 0
        self.p.reload_profiles()

    def say(self, text, role="msg"):
        self.message, self.message_role = text, role

    def send(self, **fields):
        self.overrides.update(fields)
        self.p.tune(**fields)

    def close(self):
        """On quit: stop the audio here, and give the uConsole speaker back if M muted it."""
        if self.audio:
            self.audio.stop()
        if self.mute_prev is not None and getattr(self.p, "link", None):
            try:
                self.p.link.post("/api/v1/tune", {"volume": self.mute_prev})
            except OSError:
                pass
            self.mute_prev = None

    # ---- keys; returns False to quit
    def key(self, ch):
        s = self.update()
        if self.show_help:
            self.show_help = False
            return True
        if self.entry is not None:
            return self.entry_key(ch)
        if ch in (ord("q"), ord("Q")):
            return False
        self.say("")
        if ch == ord("?"):
            self.show_help = True
        elif ch in (ord("1"), ord("2"), ord("3")):
            self.pick_view(VIEWS[ch - ord("1")][0], s)
        elif ch == ord("l"):
            self.toggle_audio(s)
        elif self.view == "digital":
            self.digital_key(ch, s)
        elif self.view == "listen":
            self.listen_key(ch, s)
        elif self.view == "manual":
            self.manual_key(ch, s)
        elif s["reachable"] is not True:
            self.say(WAITING)
        return True

    def pick_view(self, v, s):
        if v != "manual" and s["profiles_state"] == "missing":
            self.say("This rxd can't switch profiles (no /api/profiles), so it's Manual only",
                     "err")
            return
        self.view, self.pinned = v, v == "manual"
        self.view_for = self.active_name(s)
        self.cursor_key = None
        self.js8_scroll = 0

    def picker(self, s):
        """(the current view's picker list, the cursor, kept on the list)."""
        items = picker_list(self.view, self.profile_list(s))
        key = (self.view, self.active_name(s), tuple(p.get("name") for p in items))
        if key != self.cursor_key:
            self.cursor_key = key
            names = [p.get("name") for p in items]
            self.cursor = names.index(key[1]) if key[1] in names else 0
        self.cursor = clamp(self.cursor, 0, max(len(items) - 1, 0))
        return items

    def picker_key(self, ch, s):
        """↑/↓ and Enter in a picker. True if the key was the picker's."""
        if ch not in (curses.KEY_UP, curses.KEY_DOWN, 10, 13, curses.KEY_ENTER):
            return False
        items = self.picker(s)
        if not items:
            self.say(self.picker_empty(s), "err")
            return True
        if ch == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
        elif ch == curses.KEY_DOWN:
            self.cursor = min(len(items) - 1, self.cursor + 1)
        else:
            self.select(items[self.cursor], s)
        return True

    def picker_empty(self, s):
        if s["profiles_state"] == "error" and not s["profiles"]:
            return "%s Can't list profiles: %s" % (self.g.bad, s["profiles_error"])
        if not s["profiles"]:
            return "Loading profiles from rxd" + self.g.ellipsis
        return "No %s profiles are saved." % ("JS8 or WSPR" if self.view == "digital"
                                              else "station")

    def select(self, p, s):
        st = s["status"]
        if self.switching:
            self.say("Already switching to %s" % self.switching["name"])
        elif p.get("name") == self.active_name(s):
            self.say("Already on %s" % p.get("name"))
        elif not s["reachable"]:
            self.say("Not switched: can't reach rxd", "err")
        else:
            self.do_switch(p, s)

    def do_switch(self, p, s):
        st = s["status"]
        self.switching = {"name": p["name"], "desc": p.get("description") or p["name"],
                          "prev_up": (st.get("upconverter_hz") or 0) > 0,
                          "want_up": bool(p.get("upconvert")), "since": self.clock(),
                          "manual": manual_switch(st), "up_name": up_name(st),
                          "muted": self.mute_prev}
        self.say("")
        self.p.switch(p["name"])

    def toggle_audio(self, s):
        if self.audio is None:
            self.say("%s %s" % (self.g.bad, self.player_hint), "err")
        elif self.audio.on:
            self.audio.stop()
            self.say("%s Stopped listening here" % self.g.stop)
        else:
            self.audio.start(rate_for(s["status"].get("mode")))

    def local_volume_key(self, ch):
        d = LOCAL_VOL_STEP if ch in (ord("]"), ord("}")) else -LOCAL_VOL_STEP
        v = clamp(self.local_vol + d, 0, LOCAL_VOL_MAX)
        if v == self.local_vol:
            self.say("Volume here at its %s: %d %%" % ("maximum" if d > 0 else "minimum", v))
        self.local_vol = v
        if self.audio:
            self.audio.gain = v / 100.0
        if not (self.audio and self.audio.on):
            self.say("Volume here %d %%, for when l plays the audio" % v)

    def mute_key(self, s):
        st = s["status"]
        if st.get("volume") is None:
            self.not_yet()
        elif self.mute_prev is not None:
            v, self.mute_prev = self.mute_prev, None
            self.send(volume=v)
        elif st["volume"] <= 0:
            self.say("The uConsole speaker is already at 0 %; + turns it up")
        else:
            self.mute_prev = st["volume"]
            self.send(volume=0.0)

    def speaker_key(self, ch, st):
        if st.get("volume") is None:
            return self.not_yet()
        d = 1 if ch in (ord("+"), ord("=")) else -1
        base = self.mute_prev if (self.mute_prev is not None and d > 0) else st["volume"]
        v = next_volume(base, d)
        self.mute_prev = None
        if v == st["volume"]:
            self.say("Speaker at its %s: %s" % ("maximum" if d > 0 else "minimum", percent(v)))
        else:
            self.send(volume=v)

    def gain_key(self, ch, st):
        if not st:
            return self.not_yet()
        d = 1 if ch == ord("G") else -1
        g = next_gain(st.get("gains"), st.get("gain_db"), d)
        if g == st.get("gain_db"):
            self.say("Gain at its %s: %.1f dB" % ("maximum" if d > 0 else "minimum", g))
        else:
            self.send(gain_db=g)

    def gain_auto_key(self, st):
        """a: automatic RF gain on / off (off keeps the gain in use)."""
        if not st or "gain_auto" not in st:
            return self.say("This rxd has no automatic gain", "err") if st else self.not_yet()
        if st.get("gain_auto"):
            self.send(gain_db=st.get("gain_db"), gain_auto=False)
            self.say("Gain by hand, %.1f dB: g G change it" % st.get("gain_db"))
        else:
            self.send(gain_auto=True)
            self.say("Auto gain: rxd picks the best signal-to-noise")

    def audio_key(self, ch, st):
        if not st:
            return self.not_yet()
        fields, msg = audio_toggle(st, chr(ch))
        if fields:
            self.send(**fields)
        self.say(msg, "msg" if fields else "err")

    def digital_key(self, ch, s):
        if self.picker_key(ch, s):
            return
        if ch in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            page = max(1, self.js8_page - 1)
            self.js8_scroll = max(0, self.js8_scroll + (page if ch == curses.KEY_PPAGE else -page))
        elif ch == ord("/"):
            self.entry, self.entry_kind = self.js8_filter, "filter"
        elif ch == 27:
            if self.js8_filter:
                self.js8_filter, self.js8_scroll = "", 0
        elif ch == ord("M"):
            self.mute_key(s)
        elif ch == ord("r"):
            self.report_key(s)
        elif ch in (ord("a"), ord("m"), ord("s")):
            self.band_plan_key(ch, s)
        elif ch in (ord("["), ord("]")) and self.decoder(s) != "js8":
            self.local_volume_key(ch)
        else:
            self.other_key(ch)

    def band_plan_key(self, ch, s):
        """a: Auto / Manual; m: Auto listens for WSPR / JS8; s: scan the bands now."""
        bp = s["status"].get("band_plan")
        if not isinstance(bp, dict):
            self.say("This rxd has no band plan (it isn't on the radio)", "err")
        elif not s["reachable"]:
            self.say("Not sent: can't reach rxd", "err")
        elif ch == ord("a"):
            auto = bp.get("plan") != "auto"
            self.p.bands({"plan": "auto" if auto else "manual"})
            self.say("%s Band plan %s" % ((self.g.rx, "Auto: rxd picks the band") if auto
                                          else (self.g.stop, "Manual: staying on this band")))
        elif ch == ord("m"):
            mode = "js8" if bp.get("mode") == "wspr" else "wspr"
            self.p.bands({"mode": mode})
            self.say("Auto listens for %s%s" % ("JS8" if mode == "js8" else "WSPR",
                                                "" if bp.get("plan") == "auto"
                                                else " (once a turns Auto on)"))
        else:
            self.p.bands({"scan": True})
            self.say("%s Scanning the bands; Auto" % self.g.wait)

    def report_key(self, s):
        """r: WSPR reporting to WSPRnet on / off."""
        st = s["status"]
        w = st.get("wspr")
        if self.decoder(s) != "wspr" or self.mismatch(s):
            self.say("r turns WSPR reporting on and off; the receiver isn't on WSPR")
        elif not isinstance(w, dict) or not isinstance(w.get("report"), dict):
            self.not_yet()
        elif not wspr_can_report(w):
            self.say("%s Can't report: %s" % (self.g.stop, w["report"]["reason"]), "err")
        else:
            on = not w["report"].get("enabled")
            self.send(wspr_report=on)
            self.say("%s %s" % ((self.g.rx, "Reporting spots to WSPRnet") if on else
                                (self.g.stop, "Stopped reporting to WSPRnet")))

    def listen_key(self, ch, s):
        st = s["status"]
        if self.picker_key(ch, s):
            return
        controls = (ord("g"), ord("G"), ord("b"), ord("s"), ord(","), ord("."), ord("a"), ord("A"))
        if ch in controls and self.mismatch(s):
            self.say("Receiver is on %s; pick a station first (%s Enter)" % (
                st.get("profile"), self.g.keys["updown"]))
        elif ch in (ord("g"), ord("G")):
            self.gain_key(ch, st)
        elif ch == ord("a"):
            self.gain_auto_key(st)
        elif ch == ord("A"):
            self.audio_key(ch, st)
        elif ch == ord("s") and audio_of(st) is not None and st.get("mode") == "FM":
            fields, msg = squelch_cycle(st, self.sq_level)
            self.send(**fields)
            self.say(msg)
        elif ch == ord("b"):
            if st.get("mode") is None:
                return self.not_yet()
            bw = cycle_bandwidth(st["mode"], st.get("bandwidth"))
            if bw is None:
                self.say("No bandwidth presets for %s" % st["mode"])
            else:
                self.send(bandwidth=bw)
        elif ch in (ord("s"), ord(","), ord(".")):
            if st.get("mode") != "FM":
                self.say("Squelch is for NFM only; this is %s" % mode_label(st.get("mode")))
            elif "squelch" not in st:
                self.say("This rxd has no squelch", "err")
            else:
                send, self.sq_level, msg = squelch_key(chr(ch), st.get("squelch"), self.sq_level)
                if send is not NOSEND:
                    self.send(squelch=send)
                if msg:
                    self.say(msg)
        elif ch in (ord("-"), ord("_"), ord("+"), ord("=")):
            self.speaker_key(ch, st)
        elif ch == ord("M"):
            self.mute_key(s)
        elif ch in (ord("["), ord("]")):
            self.local_volume_key(ch)
        else:
            self.other_key(ch)

    def manual_key(self, ch, s):
        st = s["status"]
        if ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
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
            self.entry, self.entry_kind = "", "freq"
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
        elif ch in (ord("{"), ord("}")):
            self.local_volume_key(ch)
        elif ch in (ord("g"), ord("G")):
            self.gain_key(ch, st)
        elif ch == ord("a"):
            self.gain_auto_key(st)
        elif ch in [ord(c) for c in "AnhecS"]:
            self.audio_key(ch, st)
        elif ch in (ord("-"), ord("_"), ord("+"), ord("=")):
            self.speaker_key(ch, st)
        elif ch == ord("M"):
            self.mute_key(s)
        else:
            self.other_key(ch)

    def other_key(self, ch):
        if 32 < ch < 127:
            self.say("%s isn't a key in %s; ? lists them" % (
                chr(ch), dict(VIEWS)[self.view]))

    def not_yet(self):
        self.say("Not sent: no status from rxd yet", "err")
        return True

    def entry_key(self, ch):
        if ch in (27, 7):  # Esc, Ctrl-G
            self.entry = None
        elif ch in (10, 13, curses.KEY_ENTER):
            text, self.entry = self.entry, None
            if self.entry_kind == "filter":
                self.js8_filter, self.js8_scroll = text.strip().upper(), 0
            elif text.strip():
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
        s = self.update()
        g, h, w = self.g, scr.h, scr.w
        if h < 10 or w < 30:
            scr.put(0, 0, "rxd-tui: too small", "err")
            scr.put(1, 0, "Needs 30x10; q quits")
            return
        st = s["status"]
        self.draw_bar(scr, s)
        bottom = h - 2           # message line; help on h - 1
        if self.view == "digital":
            self.draw_digital(scr, s, 1, bottom)
        elif self.view == "listen":
            self.draw_listen(scr, s, 1, bottom)
        elif self.view == "manual":
            self.draw_manual(scr, s, bottom)
        elif s["reachable"]:
            scr.put(h // 2, 2, "Loading profiles from rxd" + g.ellipsis, "dim")

        self.draw_message(scr, s, bottom)
        x = 1
        listening = bool(self.audio and self.audio.on)
        items = help_items(g, self.view or "manual", st.get("mode"), listening,
                           None if self.mismatch(s) else self.decoder(s),
                           wspr_can_report(st.get("wspr")),
                           isinstance(st.get("band_plan"), dict))
        for text, r in help_line(w - 2, g, items):
            scr.put(h - 1, x, text, r)
            x += len(text)
        if self.show_help:
            self.draw_box(scr, self.help_lines())

    def draw_bar(self, scr, s):
        g, w = self.g, scr.w
        st = s["status"]
        sym, word, role = badge(s["reachable"], st, self.p.dropping(), g, self.switching)
        b = " %s %s " % (sym, word)
        scr.put(0, 0, " " * w, "bar")
        tabs = []
        for n, (v, name) in enumerate(VIEWS, start=1):
            on = v == self.view
            tabs.append((" %d %s%s " % (n, (g.rx + " ") if on else "", name), on))
        tabw = sum(len(t) for t, _ in tabs)
        title = " rxd  " + (self.active_name(s) or "")
        if self.url.rstrip("/") != DEFAULT_URL:
            title += "  @ " + self.url.split("//", 1)[-1]
        room = w - len(b) - 2 - tabw - 1
        if room < 6:
            tabs = [(t.replace(name, name[:1]) if not on else t, on)
                    for (t, on), (_, name) in zip(tabs, VIEWS)]
            tabw = sum(len(t) for t, _ in tabs)
            room = w - len(b) - 2 - tabw - 1
        scr.put(0, 0, clip(title, max(room, 4), g), "bar")
        x = max(room, 4) + 1
        if x + tabw <= w - len(b) - 1:
            for t, on in tabs:
                scr.put(0, x, t, "tabon" if on else "bar")
                x += len(t)
        scr.put(0, w - len(b) - 1, b, role)

    def draw_message(self, scr, s, y):
        g, w = self.g, scr.w
        st = s["status"]
        if self.entry is not None:
            if self.entry_kind == "filter":
                prompts = ("Show lines with (a callsign, or nothing for all), Enter shows, "
                           "Esc cancels: ", "Callsign, Enter filters, Esc cancels: ", "Filter: ")
            else:
                prompts = ("Frequency (7.078 MHz, 14074 kHz or 7078000 Hz), Enter tunes, "
                           "Esc cancels: ", "Frequency (MHz, kHz or Hz), Enter tunes, "
                           "Esc cancels: ", "Frequency: ")
            for prompt in prompts:
                if len(prompt) + 14 <= w:
                    break
            scr.put(y, 0, prompt, "value")
            scr.put(y, len(prompt), self.entry + "_", "key")
        elif self.message and not (self.message == WAITING and s["reachable"] is True):
            # "Waiting for rxd" is over once it answers; don't leave it up.
            scr.put(y, 1, clip(g.text(self.message), w - 2, g), self.message_role)
        elif self.switching:
            sw = self.switching
            text = "%s Switching to %s%s rxd restarts, which takes a few seconds." % (
                g.stop, sw["name"], g.ellipsis)
            if sw["want_up"] != sw["prev_up"] and sw["manual"]:
                text = "%s Switching to %s%s  %s Flip the %s to %s" % (
                    g.stop, sw["name"], g.ellipsis, g.drop, sw["up_name"], hiu_word(sw["want_up"]))
            scr.put(y, 1, clip(text, w - 2, g), "warn")
        elif s["reachable"] is False and s["why"].startswith("this rxd "):
            # it answers, but in an API this client doesn't speak
            scr.put(y, 1, clip("%s %s: %s." % (g.bad, self.url, s["why"][0].upper() + s["why"][1:]),
                               w - 2, g), "err")
        elif s["reachable"] is False:
            scr.put(y, 1, clip("%s Can't reach rxd at %s (%s). Retrying." % (
                g.bad, self.url, s["why"]), w - 2, g), "err")
        elif st.get("error"):
            scr.put(y, 1, clip("rxd: %s" % st["error"], w - 2, g), "err")

    def draw_note(self, scr, s, y, what):
        """The note above a picker for a view the receiver isn't in."""
        g = self.g
        st = s["status"]
        p = self.active_profile(s)
        text = "Receiver is on %s%s. Pick %s to switch." % (
            st.get("profile") or "another profile",
            " (%s)" % p["description"] if p and p.get("description") else "", what)
        scr.put(y, 1, clip(g.text(text), scr.w - 2, g), "warn")

    def picker_row(self, p, s, i, top, sub):
        """(text, role, state text, state role) for one picker entry."""
        g = self.g
        st = s["status"]
        active = p.get("name") == self.active_name(s) and not self.switching
        mark = g.cursor if i == self.cursor else " "
        if active:
            state, srole = g.rx + " Active", "ok"
        elif self.switching and self.switching["name"] == p.get("name"):
            state, srole = g.stop + " Switching" + g.ellipsis, "warn"
        elif flip_needed(p, st):
            state, srole = "%s Flip %s to %s" % (g.drop, up_name(st), hiu_word(p.get("upconvert"))), \
                "upconv"
        else:
            state, srole = "", "label"
        text = "%s %s %s" % (mark, top, sub)
        return text, ("sel" if i == self.cursor else "value"), state, srole

    def draw_picker_rows(self, scr, rows, y0, y1):
        """rows: [(text, role, state, srole)] or ("group", title); kept in view of the cursor."""
        w = scr.w
        n = y1 - y0
        if n <= 0:
            return
        cur = next((i for i, r in enumerate(rows) if r[0] != "group" and r[1] == "sel"), 0)
        first = 0
        if len(rows) > n:
            first = clamp(cur - n // 2, 0, len(rows) - n)
            if first > 0 and rows[first - 1][0] == "group" and cur - first < n - 1:
                first -= 1
        for k, r in enumerate(rows[first:first + n]):
            y = y0 + k
            if r[0] == "group":
                scr.put(y, 1, self.g.text(r[1]), "head")
                continue
            text, role, state, srole = r
            state = self.g.text(state)
            sx = max(w - len(state) - 2, w // 2) if state else w - 1
            body = clip(text, (sx - 2 if state else w - 3), self.g)
            scr.put(y, 1, body.ljust(min(sx - 2, w - 3)) if role == "sel" else body, role)
            if state and sx < w - 2:
                scr.put(y, sx, clip(self.g.text(state), w - sx - 1, self.g), srole)
        if first > 0:
            scr.put(y0, w - 2, self.g.keys["up"][:1], "label")
        if first + n < len(rows):
            scr.put(y1 - 1, w - 2, self.g.keys["down"][:1], "label")

    def draw_digital(self, scr, s, y, bottom):
        """The Digital view: the picker, the receiver and clock, then JS8's decodes
        or WSPR's cycle, reporting and spots, whichever the receiver decodes."""
        g, w = self.g, scr.w
        st = s["status"]
        mis = self.mismatch(s)
        if mis:
            self.draw_note(scr, s, y, "a JS8 or WSPR frequency")
            y += 2
        items = self.picker(s)
        if not items:
            scr.put(y, 2, g.text(self.picker_empty(s)), "dim")
            y += 2
        else:
            rows, i = [], 0
            for title, members in group_digital(self.profile_list(s)):
                rows.append(("group", title))
                for p in members:
                    sub = "%s%s %s" % (p.get("description") or "",
                                       " " + g.dot if p.get("description") else "",
                                       short_mhz(p.get("freq") or 0))
                    rows.append(self.picker_row(p, s, i, "%-10s" % p.get("name"), sub.strip()))
                    i += 1
            room = bottom - y
            n = min(len(rows), room - 1 if mis else
                    min(max(2, room // 3), 8 if room >= 30 else 5))
            self.draw_picker_rows(scr, rows, y, y + n)
            y += n + 1
        if mis:
            return
        dec = self.decoder(s)
        wspr = dec == "wspr"
        lines = s["wspr"] if wspr else s["js8"]

        def put(y, x, text, role=""):
            if y < bottom:
                scr.put(y, x, text, role)
        # status: the upconverter, receiver, level, stations heard
        sym, word, role = badge(s["reachable"], st, self.p.dropping(), g, self.switching)
        x = 1
        up = st.get("upconverter_hz")
        parts = [(up_name(st) + " ", "label"),
                 (hiu_word(up > 0) if up is not None else "---",
                  "upconv" if up else "value"),
                 ("   ", "")] if upconverter(st) else []
        parts += [("%s %s" % (sym, word), role), ("   Signal ", "label")]
        lvl = st.get("level_dbfs")
        parts.append(("%.1f dBFS" % lvl if lvl is not None else "--- dBFS", "value"))
        heard = stations_heard(lines, "call" if wspr else "from")
        tail = "%d station%s heard" % (heard, "" if heard == 1 else "s")
        used = sum(len(t) for t, _ in parts)
        barw = clamp(w - used - len(tail) - 8, 0, 20)
        if barw >= 4:
            parts += [(" ", ""), (level_bar(lvl, barw, g), "spec")]
        parts += [("   ", ""), (tail, "value")]
        for t, r in parts:
            put(y, x, t[:max(0, w - 1 - x)], r)
            x += len(t)
        y += 1
        ctext, crole = clock_words(st.get("clock"), g, "WSPR" if wspr else "JS8")
        if crole == "bad":
            put(y, 0, " " * w, "bad")
            put(y, 1, clip(ctext + ". Check chrony and the GPS.", w - 2, g), "bad")
        else:
            put(y, 1, clip(ctext, w - 2, g), crole)
        y += 1
        sw = station_words(st, g)
        if sw and bottom - y > 8:
            put(y, 1, clip(g.text(sw[0]), w - 2, g), sw[1])
            y += 1
        bpw = band_plan_words(st.get("band_plan"), g)
        if bpw:
            put(y, 1, clip(g.text(bpw[0]), w - 2, g), bpw[1])
            y += 1
            if bottom - y > 10:
                put(y, 3, clip(g.text(band_plan_extra(st["band_plan"], g)), w - 4, g), "label")
                y += 1
        if wspr:
            y = self.draw_wspr_status(scr, st.get("wspr"), y, bottom)
        if self.audio and self.audio.on and y < bottom:
            y = self.draw_audio_row(scr, y)
        y += 1 if bottom - y > 8 else 0
        # decodes or spots
        if wspr:
            header, key = WSPR_HEADER, wspr_key
        else:
            header, key = JS8_HEADER, js8_key
        shown = line_filter(lines, self.js8_filter, key)
        area = bottom - y - 1
        self.js8_page = max(area, 1)
        prof = st.get("profile")
        if self.js8_seen and self.js8_seen[0] == prof and self.js8_scroll:
            self.js8_scroll += max(0, len(shown) - self.js8_seen[1])  # stay put as lines arrive
        self.js8_seen = (prof, len(shown))
        self.js8_scroll = clamp(self.js8_scroll, 0, max(0, len(shown) - area))
        if y >= bottom:
            return
        info = ""
        if self.js8_filter:
            info = "Filter %s: %d of %d  (Esc clears)" % (
                self.js8_filter, len(shown), len(lines))
        if self.js8_scroll:
            info = ("%s %d back, PgDn for newest" % (g.drop, self.js8_scroll)) + \
                ("   " + info if info else "")
        scr.put(y, 1, clip(header, w - 2, g), "head")
        if info:
            scr.put(y, max(len(header) + 3, w - len(info) - 2),
                    clip(info, w - len(header) - 5, g), "warn" if self.js8_scroll else "label")
        y += 1
        if area <= 0:
            return
        if not shown:
            state = s["wspr_state"] if wspr else s["js8_state"]
            what = "spots" if wspr else "decodes"
            if state == "missing":
                text = "%s This rxd has no %s feed." % (g.bad, "WSPR" if wspr else "JS8")
            elif str(state).startswith("error"):
                text = "%s No %s: %s" % (g.bad, what, state[7:])
            elif self.js8_filter and lines:
                text = "No lines with %s. / changes the filter, Esc clears it." % self.js8_filter
            elif wspr:
                text = "No spots yet. They appear here as each two-minute slot is decoded."
            else:
                text = "No decodes yet. They appear here as rxd decodes each slot."
            scr.put(y, 2, clip(text, w - 3, g), "dim")
            return
        end = len(shown) - self.js8_scroll
        view = shown[max(0, end - area):end]
        call = (st.get("station") or {}).get("call")
        for i, line in enumerate(view):
            if wspr:
                scr.put(y + i, 1, format_wspr(line, st.get("freq"), w - 2, g))
                continue
            text, mine = format_js8(line, call, w - 2, g)
            scr.put(y + i, 1, text.ljust(w - 2) if mine else text, "mine" if mine else "")

    def draw_wspr_status(self, scr, wsp, y, bottom):
        """WSPR's cycle, last slot, reporting and any error; returns the next row."""
        g, w = self.g, scr.w

        def put(text, role):
            if y < bottom:
                scr.put(y, 1, clip(g.text(text), w - 2, g), role)
        if not isinstance(wsp, dict):
            put("%s Waiting for the WSPR decoder" % g.wait, "dim")
            return y + 1
        ctext, crole = wspr_cycle(wsp, g)
        last = wspr_last(wsp)
        if len(ctext) + 3 + len(last) <= w - 2:
            put(ctext, crole)
            if y < bottom:
                scr.put(y, 1 + len(ctext) + 3, last, "value")
            y += 1
        else:
            put(ctext, crole)
            y += 1
            put(last, "value")
            y += 1
        put(*wspr_report_words(wsp, g))
        y += 1
        if wsp.get("error"):
            put("%s %s" % (g.bad, wsp["error"]), "err")
            y += 1
        return y

    def draw_listen(self, scr, s, y, bottom):
        g, w, h = self.g, scr.w, scr.h
        st = s["status"]
        stale = not s["reachable"]
        mis = self.mismatch(s)
        if mis:
            self.draw_note(scr, s, y, "a station")
            y += 2
        else:
            p = self.active_profile(s)
            name = (p.get("description") if p else None) or st.get("profile") or "---"
            num, unit = station_freq(st["freq"], st.get("mode")) if st.get("freq") else ("-.-", "MHz")
            scale = 2 if (w >= 120 and h >= 36) else 1
            big = big_text(num, scale, g)
            bigw = len(big[0])
            y += 1 if h >= 22 else 0
            sub = "%s   %s" % (unit, mode_label(st.get("mode")))
            grp = dict(GROUPS).get(st.get("mode"))
            if grp:
                sub += "   " + g.text(grp)
            if h >= 20 and bigw + 30 <= w:
                for i, line in enumerate(big):
                    scr.put(y + i, 2, line, "dim" if stale else "big")
                sx = 2 + bigw + 3
                rows = [0, 1, 2] if len(big) == 3 else [0, 2, 4]
                scr.put(y + rows[0], sx, clip(name, w - sx - 1, g), "value")
                scr.put(y + rows[1], sx, clip(sub, w - sx - 1, g), "label")
                scr.put(y + rows[2], sx, clip(st.get("profile") or "", w - sx - 1, g), "dim")
                y += len(big) + 1
            else:
                scr.put(y, 1, clip("%s  %s %s  %s" % (name, num, unit, mode_label(st.get("mode"))),
                                   w - 2, g), "dim" if stale else "big")
                y += 1
            # signal and Ham It Up
            lvl = st.get("level_dbfs")
            scr.put(y, 1, "Signal", "label")
            scr.put(y, 8, "%6.1f dBFS" % lvl if lvl is not None else "  --- dBFS", "value")
            up = st.get("upconverter_hz")
            label = up_name(st) + " " if upconverter(st) else ""
            hiu = label + (hiu_word(up > 0) if up is not None else "---") if label else ""
            barw = clamp(w - 20 - len(hiu) - 4, 4, 40)
            scr.put(y, 20, level_bar(lvl, barw, g), "spec")
            if label:
                scr.put(y, 20 + barw + 3, label, "label")
                scr.put(y, 20 + barw + 3 + len(label), hiu[len(label):], "upconv" if up else "value")
            y += 1
            # gain, bandwidth, speaker
            gain = st.get("gain_db")
            bw = st.get("bandwidth")
            vol = st.get("volume")
            if self.mute_prev is not None:
                spk = "Muted (M restores %s)" % percent(self.mute_prev)
            else:
                spk = percent(vol) if vol is not None else "---"
            fields = [("Gain", gain_words(st)),
                      ("Bandwidth", "%s %s" % (bandwidth_name(st.get("mode"), bw), format_hz(bw))
                       if bw else "---"),
                      ("Speaker", spk)]
            x = 1
            for label, val in fields:
                if x + len(label) + len(val) + 1 > w - 1:
                    break
                scr.put(y, x, label, "label")
                scr.put(y, x + len(label) + 1, val, "warn" if label == "Speaker" and
                        self.mute_prev is not None else "value")
                x += len(label) + len(val) + 5
            y += 1
            if st.get("mode") == "FM" and "squelch" in st:
                text, role = squelch_words(st, g)
                scr.put(y, 1, "Squelch", "label")
                scr.put(y, 9, text, role)
                y += 1
            if audio_of(st) is not None:
                scr.put(y, 1, "Audio", "label")
                scr.put(y, 7, clip(audio_words(st), w - 8, g), "value")
                y += 1
            y = self.draw_audio_row(scr, y)
            y += 1
        # the station picker
        items = self.picker(s)
        if not items:
            scr.put(y, 2, g.text(self.picker_empty(s)), "dim")
            return
        rows, i = [], 0
        for title, members in group_stations(self.profile_list(s)):
            rows.append(("group", title))
            for p in members:
                desc = p.get("description") or p.get("name")
                num, unit = station_freq(p.get("freq") or 0, p.get("mode"))
                rows.append(self.picker_row(p, s, i, "%-22s" % clip(desc, 22, g),
                                            "%7s %-3s  %s" % (num, unit, mode_label(p.get("mode")))))
                i += 1
        self.draw_picker_rows(scr, rows, y, bottom)

    def draw_audio_row(self, scr, y, x=1):
        g = self.g
        if self.audio is None:
            scr.put(y, x, clip("%s No audio player: %s" % (g.bad, self.player_hint.split(". ", 1)[-1]),
                               scr.w - x - 1, g), "bad")
            return y + 1
        sym, text, role = self.audio.words(g)
        text = "%s %s" % (sym, text)
        if self.audio.on:
            text += "   Volume here %d %%" % self.local_vol
        else:
            text += ": l plays the audio here"
        scr.put(y, x, clip(text, scr.w - x - 1, g), role)
        return y + 1

    def draw_manual(self, scr, s, bottom):
        st, spec, hist = s["status"], s["spectrum"], s["history"]
        reachable, spec_state = s["reachable"], s["spec_state"]
        g, h, w = self.g, scr.h, scr.w
        stale = not reachable
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

        # the upconverter, gain, volume
        up = st.get("upconverter_hz")
        x = 1
        if upconverter(st):
            label = "%s%s:" % (up_name(st), " switch" if manual_switch(st) else "")
            scr.put(y, x, label, "label")
            x += len(label) + 1
            if up is None:
                sw, srole = "---", "value"
            elif up > 0:
                sw, srole = "UPCONVERT (%s)" % format_hz(up), "upconv"
            else:
                sw, srole = "BYPASS", "value"
            scr.put(y, x, sw, srole)
            x += len(sw) + 4
        gain = st.get("gain_db")
        if self.mute_prev is not None:
            spk = "Muted"
        else:
            spk = percent(st["volume"]) if st.get("volume") is not None else "---"
        for label, val in (("Gain", gain_words(st)), ("Speaker", spk),
                           ("Audio", audio_words(st, short=True))):
            if label == "Audio" and x + len(label) + len(val) + 2 > w:
                val = val.split(":")[0]   # just Auto or Off when the list won't fit
            if x + len(label) + len(val) + 2 > w:
                break
            scr.put(y, x, label, "label")
            scr.put(y, x + len(label) + 1, val, "value")
            x += len(label) + len(val) + 5
        y += 1

        # signal level, the audio here when it's on, and the receiver's health when there is room
        lvl = st.get("level_dbfs")
        scr.put(y, 1, "Signal", "label")
        ltext = "%6.1f dBFS" % lvl if lvl is not None else "  --- dBFS"
        scr.put(y, 8, ltext, "value")
        extra, erole = "", "label"
        if self.audio and self.audio.on:
            sym, text, erole = self.audio.words(g)
            extra = "%s %s" % (sym, text)
        elif w >= 110 and st:
            extra = "DSP %d%%   Dropped %d" % (round((st.get("dsp_cpu") or 0) * 100),
                                              st.get("dropped") or 0)
        barw = clamp(w - 21 - (len(extra) + 4 if extra else 0), 4, 60)
        scr.put(y, 20, level_bar(lvl, barw, g), "spec")
        if extra:
            scr.put(y, 20 + barw + 3, clip(extra, w - barw - 24, g), erole)
        y += 2 if h >= 30 else 1

        # spectrum, markers and waterfall
        avail = bottom - y
        if avail >= 3:
            self.draw_spectrum(scr, y, avail, w, spec, hist, reachable, spec_state)

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

    def help_lines(self):
        k = self.g.keys
        ud = k["updown"]
        views = ["1 2 3     Digital, Listen or Manual view"]
        if self.view == "digital":
            body = [
                "rxd-tui: Digital (JS8 and WSPR)",
                "",
                "%-9s pick a JS8 or WSPR frequency; Enter switches" % ud,
                "PgUp PgDn scroll back through the decodes or spots",
                "          / forward",
                "/         show only lines with a callsign; Esc",
                "          shows them all again",
                "r         WSPR: report spots to WSPRnet / stop",
                "a         band plan Auto (rxd picks the band) / Manual",
                "m         Auto listens for WSPR / JS8",
                "s         scan the bands now (and Auto)",
                "l         play the audio on this machine / stop",
                "[ ]       volume of the audio here",
            ] + views + [
                "",
                "%s marks JS8 lines to your callsign. JS8 and WSPR" % self.g.mine,
                "need the clock right to about a second, so the",
                "clock's state is shown under the receiver's.",
                "Times are this machine's local time.",
            ]
        elif self.view == "listen":
            body = [
                "rxd-tui: Listen",
                "",
                "%-9s pick a station; Enter switches" % ud,
                "g G       gain down / up through the dongle's steps",
                "          (sets it by hand: auto gain goes off)",
                "a         auto gain on / off",
                "A         auto audio (the clean-up) on / off",
                "b         bandwidth: Narrow, Normal, Wide",
                "s         squelch: off, auto, at a level (NFM only)",
                ", .       squelch level down / up, 2 dB a press",
                "- +       uConsole speaker volume down / up",
                "M         mute the uConsole speaker / restore it",
                "l         play the audio on this machine / stop",
                "[ ]       volume of the audio here",
            ] + views
        else:
            body = [
                "rxd-tui: Manual",
                "",
                "%-9s tune down / up by the step" % (k["left"] + " " + k["right"]),
                "%-9s larger / smaller step: 10 Hz to 1 MHz" % (k["up"] + " " + k["down"]),
                "f         type a frequency and press Enter:",
                "            7.078 or 146.52 is MHz, 14074 is kHz,",
                "            7078000 is Hz; 7078k and 7.078M work too",
                "m         next mode: USB LSB AM FM WFM",
                "[ ]       narrower / wider bandwidth",
                "g G       gain down / up through the dongle's steps",
                "a         auto gain on / off",
                "A         audio clean-up on / off",
                "n h e c S its parts on / off: noise reduction, hum",
                "          filter, even loudness, limiter, NFM squelch",
                "- +       uConsole speaker volume down / up (0-400 %)",
                "M         mute the uConsole speaker / restore it",
                "l         play the audio on this machine / stop",
                "{ }       volume of the audio here",
            ] + views
        return body + [
            "?         this help",
            "q         quit (Ctrl-C works too)",
            "",
            "An upconverter's UPCONVERT/BYPASS switch (a Ham It Up's)",
            "may be physical. rxd only says which way it should be;",
            "the wrong way gives silence.",
            "",
            "Any key closes this.",
        ]

    def draw_box(self, scr, lines):
        lines = [self.g.text(x) for x in lines]
        bw = min(max(len(x) for x in lines) + 4, scr.w)
        bh = len(lines) + 2
        if bh > scr.h:
            lines = [x for x in lines if x]
            if len(lines) + 2 > scr.h:  # keep the last line: how to close it
                lines = lines[:max(scr.h - 3, 0)] + lines[-1:]
            bh = len(lines) + 2
        y0 = max(0, (scr.h - bh) // 2)
        x0 = max(0, (scr.w - bw) // 2)
        for i in range(bh):
            scr.put(y0 + i, x0, " " * bw, "bar")
        for i, x in enumerate(lines):
            scr.put(y0 + 1 + i, x0 + 2, x[:bw - 4], "bar")


# --------------------------------------------------------------------- main

ESCAPES = {"[A": curses.KEY_UP, "OA": curses.KEY_UP, "[B": curses.KEY_DOWN,
           "OB": curses.KEY_DOWN, "[C": curses.KEY_RIGHT, "OC": curses.KEY_RIGHT,
           "[D": curses.KEY_LEFT, "OD": curses.KEY_LEFT, "[5~": curses.KEY_PPAGE,
           "[6~": curses.KEY_NPAGE, "OM": 10}


def read_escape(win):
    """A key whose sequence curses didn't recognise (the terminal not in the
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
            if app.switching or (app.audio and app.audio.on):
                dirty = True  # the switch's timeout and the audio's state move on their own
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
    ap.add_argument("--listen", action="store_true",
                    help="start with the audio playing on this machine")
    ap.add_argument("--player", metavar="CMD",
                    help="the command that plays raw s16le mono PCM from stdin; "
                         "{rate} becomes the sample rate (default: the first of %s on PATH)"
                         % ", ".join(PLAYERS))
    a = ap.parse_args(argv)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        # A locale the machine doesn't have, usually forwarded over SSH
        # (macOS sends LC_CTYPE=UTF-8). curses still needs UTF-8 to draw.
        for name in ("C.UTF-8", "C.utf8"):
            try:
                locale.setlocale(locale.LC_ALL, name)
                break
            except locale.Error:
                pass
    os.environ.setdefault("ESCDELAY", "25")  # Esc cancels typing without a pause
    url = a.url if "//" in a.url else "http://" + a.url
    poller = Poller(Link(url))
    if a.player:
        try:
            player = Player.custom(a.player)
        except ValueError as e:
            ap.error(str(e))
    else:
        found = find_player()
        player = Player.found(*found) if found else None
    audio = AudioPlayer(url, player, poller.changed) if player else None
    app = App(poller, Glyphs(a.ascii or not unicode_ok()), url, audio, listen=a.listen)
    if a.listen and not audio:
        app.say("%s %s" % (app.g.bad, app.player_hint), "err")
    try:
        curses.wrapper(run, app, use_color())
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
