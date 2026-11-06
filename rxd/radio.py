#!/usr/bin/env python3
"""radio: switchable, resumable receiver profiles for rxd.

  radio                list profiles; * marks the active one
  radio NAME           switch to NAME. rxd, the receiver, restarts on NAME; on
                       a JS8 or WSPR profile it decodes that mode itself.
                       The profile being left is saved first, so coming back
                       to it resumes where it was. NAME when it is already up
                       changes nothing.
  radio off            save the active profile and stop rxd
  radio resume         bring the active profile back up (run it at login)
  radio save [NAME]    save rxd's current tuning as NAME (default: active)
  radio reset NAME     forget your saved state for NAME; back to its default
  radio show NAME      print a profile as JSON
  radio help           this text
  radio CMD ARGS...    run radio-CMD ARGS... when CMD is not a profile and a
                       radio-CMD is on PATH: how a machine adds commands of its
                       own (rx adds 'radio mesh' this way)

  --no-focus           passed on to a radio-CMD (it may open a window and
                       focus it otherwise)

Tab completes profile names (share/bash-completion/completions/radio).

Every amateur band has a JS8 and a WSPR profile on its standard dial:
js8-160m ... js8-2m and wspr-2190m ... wspr-2m. On 40 m they keep their old
names, js8 and wspr (js8-40m and wspr-40m mean the same). With an
upconverter that has a switch (a Ham It Up), 6 m and 2 m need it on BYPASS.
rxd's band plan (Auto in its Digital view) picks among them by band
conditions, time of day and what it has heard.

The station file (/etc/rxd/station.json, or $RXD_STATION; see
docs/station.md) says which RTL-SDR to open, the upconverter, the audio
sink, and any profiles of this station's own (local repeaters, broadcast
stations). Without one, radio uses the first RTL-SDR, no upconverter and the
default sink.

A profile is tuning only: the dial frequency, offset mode, demodulator and
its settings, audio sink and volume, the RTL-SDR's settings, and what rxd
decodes ("decoder": "js8" or "wspr"). A WSPR profile also says whether rxd
reports its spots to WSPRnet ("wspr_report", on by default). Both decoders
work on the audio before the volume, so the volume is only the speaker's.
The defaults below are starting points; saved state lives in
~/.config/rxd/radio/ and wins.

rxd (systemd user unit rxd@NAME) reads only ~/.config/rxd/radio/NAME.json, so
the merged profile is written there before it starts. Its HTTP status on
127.0.0.1:8073 is where the tuning is saved from. One rxd runs at a time: it
holds the dongle. Edit NAME.json while it plays and
'systemctl --user kill -s HUP rxd@NAME' makes it re-read the file.
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import station as station_conf  # noqa: E402

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".config", "rxd", "radio")
ACTIVE = os.path.join(STATE, "active")

# rxd's HTTP control port; module-level so the tests can point it elsewhere.
RXD_URL = "http://127.0.0.1:8073"
RXD_START_SECS = 15

# This station's hardware (docs/station.md); generic when there is no file.
STATION = station_conf.load()
for _p in STATION.problems:
    print("radio: station: " + _p, file=sys.stderr)


def rtl(gain):
    # gain is an index into the tuner's gain table, not dB. On the R820T:
    # 4 = 3.7 dB, 10 = 16.6 dB, 20 = 37.2 dB. A saved gain_db wins over it.
    # No serial: rxd opens the first RTL-SDR it finds.
    out = {"gain": gain, "biasT": False, "ppm": 0}
    if STATION.serial:
        out = dict({"serial": STATION.serial}, **out)
    return out


def speaker(p):
    """p with the station's audio sink, when it names one (otherwise rxd
    plays to the default sink)."""
    if STATION.sink and "sink" not in p:
        p = dict(p)
        keys = list(p)
        at = keys.index("volume") if "volume" in keys else len(keys)
        p = dict(list(p.items())[:at] + [("sink", STATION.sink)] + list(p.items())[at:])
    return p


# USB as set by hand for JS8 on 2026-11-04.
USB_JS8 = {"bandwidth": 3000.0, "agcAttack": 47.916229248046875,
           "agcDecay": 4.896891117095947}
NFM = {"bandwidth": 12500.0}
WFM = {"bandwidth": 150000.0}
AM = {"bandwidth": 10000.0}


def js8(desc, dial):
    # rxd decodes each slot itself, from the audio before the volume; the
    # speaker plays it quietly, and the browser and rxd-tui can listen too.
    return speaker({"description": desc, "frequency": float(dial), "offset": "auto",
                    "demod": "USB", "demod_settings": USB_JS8,
                    "volume": 0.2, "rtl": rtl(4), "decoder": "js8"})


def wspr(desc, dial):
    # rxd records each two-minute slot and decodes it with wsprd; the
    # speaker stays silent (volume 0) but the browser and rxd-tui can listen.
    return speaker({"description": desc, "frequency": dial, "offset": "auto",
                    "demod": "USB", "demod_settings": {"bandwidth": 2800.0},
                    "volume": 0.0, "rtl": rtl(4),
                    "decoder": "wspr", "wspr_report": True})


def vhf(desc, hz, demod="FM", settings=NFM, gain=20):
    return speaker({"description": desc, "frequency": hz,
                    "offset": "auto", "demod": demod, "demod_settings": settings,
                    "volume": 0.5, "rtl": rtl(gain)})


def station_profile(p):
    """One of the station file's own profiles, filled out like the built-in
    ones: offset auto, the station's dongle and sink."""
    out = speaker(dict({"offset": "auto", "volume": 0.5}, **p))
    out["rtl"] = dict(rtl(20), **(p.get("rtl") or {}))
    return out


# Amateur bands, ITU Region 2 edges in Hz, lowest first: what band a
# frequency is in, for `radio --json` and rxd's band plan.
BANDS = (("2190m", 135700, 137800), ("630m", 472000, 479000),
         ("160m", 1800000, 2000000), ("80m", 3500000, 4000000),
         ("60m", 5250000, 5450000), ("40m", 7000000, 7300000),
         ("30m", 10100000, 10150000), ("20m", 14000000, 14350000),
         ("17m", 18068000, 18168000), ("15m", 21000000, 21450000),
         ("12m", 24890000, 24990000), ("10m", 28000000, 29700000),
         ("6m", 50000000, 54000000), ("2m", 144000000, 148000000),
         ("1.25m", 222000000, 225000000), ("70cm", 420000000, 450000000))

# Standard dial frequencies (USB), checked 2026-11-06. JS8: JS8Call's default
# list (JS8Call-improved 3.0.3, JS8_Main/FrequencyList.cpp; the original
# JS8Call 2.2 had 1842 kHz on 160 m and nothing on 60 m). WSPR: WSJT-X's
# default list (models/FrequencyList.cpp), which has no 60 m; 5364.7 kHz is
# where WSPRnet's 60 m spots are (wspr.live, 552,000 a week there against
# 190,000 on the older 5287.2).
JS8_DIALS = (("160m", 1843500), ("80m", 3578000), ("60m", 5363000), ("40m", 7078000),
             ("30m", 10130000), ("20m", 14078000), ("17m", 18104000), ("15m", 21078000),
             ("12m", 24922000), ("10m", 28078000), ("6m", 50318000), ("2m", 144178000))
WSPR_DIALS = (("2190m", 136000), ("630m", 474200), ("160m", 1836600), ("80m", 3568600),
              ("60m", 5364700), ("40m", 7038600), ("30m", 10138700), ("20m", 14095600),
              ("17m", 18104600), ("15m", 21094600), ("12m", 24924600), ("10m", 28124600),
              ("6m", 50293000), ("2m", 144489000))
# 40 m keeps the names it always had; js8-40m and wspr-40m are aliases.
BAND_NAMES = {("js8", "40m"): "js8", ("wspr", "40m"): "wspr"}


def band_profile_name(mode, band):
    return BAND_NAMES.get((mode, band), "%s-%s" % (mode, band))


ALIASES = {"%s-40m" % m: n for (m, _), n in BAND_NAMES.items()}


def band_of(hz):
    """The amateur band hz is in ("20m"), or None."""
    for name, lo, hi in BANDS:
        if lo <= hz <= hi:
            return name
    return None


def band_label(band):
    return band[:-1] + " m" if band.endswith("m") and not band.endswith("cm") else band


def band_profiles():
    """The band plan's profiles, one per mode and band: {name: profile}."""
    out = {}
    for mode, dials, make, title in (("js8", JS8_DIALS, js8, "JS8"),
                                     ("wspr", WSPR_DIALS, wspr, "WSPR")):
        for band, dial in dials:
            out[band_profile_name(mode, band)] = make(
                "%s, %s" % (title, band_label(band)), float(dial))
    return out


BAND_PLAN = band_profiles()

# Built in: the band plan and the calling frequencies every station shares.
# Local repeaters and broadcast stations come from the station file.
PROFILES = {
    "ghostnet": js8("JS8 GhostNet, 40 m", 7107000),
    **BAND_PLAN,
    "vhf":      vhf("2 m calling", 146520000.0),
    "uhf":      vhf("70 cm calling", 446000000.0),
    "noaa":     vhf("NOAA weather", 162550000.0),  # one of the seven US channels
    **{n: station_profile(p) for n, p in STATION.profiles.items()},
}


def say(msg, kind=None):
    print("radio: " + msg, file=sys.stderr)
    # From dmenu or a launcher there is no terminal; surface what matters.
    if kind and os.environ.get("DISPLAY") and shutil.which("i3-nagbar"):
        subprocess.Popen(["i3-nagbar", "-t", kind, "-m", "radio: " + msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def die(msg):
    say(msg, "error")
    sys.exit(1)


# ------------------------------------------------------------------ files

def dump(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=4)
    os.replace(tmp, path)


# --------------------------------------------------------------- profiles

def known():
    saved = []
    if os.path.isdir(STATE):
        saved = sorted(f[:-5] for f in os.listdir(STATE) if f.endswith(".json"))
    return list(PROFILES) + [n for n in saved if n not in PROFILES]


def profile(name):
    p = copy.deepcopy(PROFILES.get(name, {}))
    path = os.path.join(STATE, name + ".json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            p.update(json.load(fh))
    if not p:
        die("no profile '%s' (have: %s)" % (name, ", ".join(known())))
    return p


def store(name, p):
    os.makedirs(STATE, exist_ok=True)
    dump(os.path.join(STATE, name + ".json"), p)


def ensure_stored(name, p):
    # rxd knows nothing of PROFILES: it reads only NAME.json, so a default
    # that was never saved has to be written out before rxd can play it.
    path = os.path.join(STATE, name + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            if json.load(fh) == p:
                return
    except (OSError, ValueError):
        pass
    store(name, p)


def active():
    try:
        with open(ACTIVE, encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        return None
    return name if name in known() else None


def set_active(name):
    os.makedirs(STATE, exist_ok=True)
    with open(ACTIVE, "w", encoding="utf-8") as fh:
        fh.write(name + "\n")


def upconverting(p):
    """Whether rxd puts p through the station's upconverter (station.json)."""
    # "auto" (the default) follows the frequency, as rxd does: the R820T
    # cannot tune below about 24 MHz, so HF and AM broadcast go through the
    # upconverter. "Ham-It-Up" and "None" pin it on or off. A station with no
    # upconverter never upconverts.
    if not float(p.get("upconverter_hz", STATION.upconverter_hz)):
        return False
    mode = p.get("offset", "auto")
    if mode == "auto":
        return float(p["frequency"]) < float(p.get("upconvert_below_hz",
                                                   STATION.upconvert_below_hz))
    return mode not in ("None", "Manual")


def decoder(p):
    return p.get("decoder")


def summary(name, p):
    if STATION.upconverter_name:
        path = "%s %s" % (STATION.upconverter_name,
                          "upconvert" if upconverting(p) else "bypass")
    else:
        path = "direct"
    return "%-10s %11.4f MHz %-4s %-19s %s" % (
        name, p["frequency"] / 1e6, p["demod"], path, p.get("description", ""))


def unit_name(name):
    # The unit passes %i to rxd verbatim, so keep to characters that need no
    # systemd escaping; anything else would reach rxd mangled.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        die("profile name '%s': use letters, digits, '.', '-' or '_'" % name)
    return "rxd@%s.service" % name


# ----------------------------------------------------------------- clock

def check_clock(mode):
    # JS8's 15 s and WSPR's 2 min slots tolerate about 2 s of clock error.
    # Outside that nothing decodes and nothing says why, so say it here.
    # Only chrony can say; without it there is nothing to check.
    try:
        out = subprocess.run(["chronyc", "-n", "tracking"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    if not re.search(r"^Leap status\s*: Normal", out, re.M):
        say("clock not synchronised (no GPS fix and no network?) — %s will "
            "not decode until it is. See: chronyc sources" % mode, "warning")


# ------------------------------------------------------------------- rxd

def systemctl(*args, **kw):
    return subprocess.run(["systemctl", "--user", *args], **kw)


def rxd_status():
    """rxd's /api/status as a dict, or None when nothing answers."""
    try:
        with urllib.request.urlopen(RXD_URL + "/api/status", timeout=1) as r:
            return json.load(r)
    except (OSError, ValueError):  # refused, timed out, HTTP error, not JSON
        return None


def rxd_stop():
    # Every instance, not just the active profile's: whichever one runs holds
    # the dongle. systemctl matches the pattern against loaded units.
    systemctl("stop", "rxd@*.service", stderr=subprocess.DEVNULL)


def rxd_fail(name, why):
    unit = unit_name(name)
    log = subprocess.run(["journalctl", "--user", "-u", unit, "-n", "15", "--no-pager"],
                         capture_output=True, text=True).stdout.rstrip()
    # Stopped so Restart=on-failure does not keep hammering a missing dongle.
    systemctl("stop", unit, stderr=subprocess.DEVNULL)
    if log:
        print(log, file=sys.stderr)
    # rxd says plainly when the dongle is missing or busy; put its own last
    # word in the message, which is all a nagbar shows.
    lines = [ln for ln in log.splitlines() if "rxd: " in ln] or log.splitlines() or [""]
    last = lines[-1].split("rxd: ", 1)[-1].strip()
    die("%s %s%s" % (unit, why, ": " + last if last else ""))


def rxd_start(name):
    unit = unit_name(name)
    # After a run of failures systemd refuses to start the unit again until
    # its counter is cleared; an explicit 'radio NAME' is a fresh attempt.
    systemctl("reset-failed", unit, stderr=subprocess.DEVNULL)
    if systemctl("start", unit).returncode != 0:
        rxd_fail(name, "did not start")
    deadline = time.monotonic() + RXD_START_SECS
    while time.monotonic() < deadline:
        if rxd_status() is not None:
            return
        state = systemctl("is-active", unit, capture_output=True, text=True).stdout.strip()
        if state in ("failed", "inactive"):
            rxd_fail(name, "exited")
        time.sleep(0.25)
    rxd_fail(name, "is not answering on %s" % RXD_URL)


# ------------------------------------------------------ capture and apply

def capture(p, st):
    """The profile p, updated with rxd's current tuning from status st."""
    p = copy.deepcopy(p)
    p.pop("vfo_offset", None)  # SDR++'s; rxd ignores it
    # offset is left as it was: status reports the upconverter now in
    # effect, and saving that would pin an "auto" profile to one side.
    if st.get("freq") is not None:
        p["frequency"] = float(st["freq"])  # the dial
    if st.get("mode"):
        p["demod"] = st["mode"]
    if st.get("bandwidth") is not None:
        p["demod_settings"] = dict(p.get("demod_settings") or {},
                                   bandwidth=float(st["bandwidth"]))
    if st.get("volume") is not None:
        p["volume"] = float(st["volume"])
    if "squelch" in st:  # in SDR++'s keys, which rxd reads back
        ds = dict(p.get("demod_settings") or {}, squelchEnabled=st["squelch"] is not None)
        if st["squelch"] is not None:
            ds["squelchLevel"] = float(st["squelch"])
        p["demod_settings"] = ds
    if st.get("gain_db") is not None:
        p["rtl"] = dict(p.get("rtl") or {}, gain_db=float(st["gain_db"]))
    if "gain_auto" in st:  # with it on, gain_db is only where rxd starts
        p["rtl"] = dict(p.get("rtl") or {}, gain_auto=bool(st["gain_auto"]))
    au = st.get("audio")
    if isinstance(au, dict):
        saved = {k: au[k] for k in ("auto", "filter", "norm", "limit", "squelch") if k in au}
        if "nr_setting" in au:  # the setting (None: by mode), not whether it's on now
            saved["nr"] = au["nr_setting"]
        if saved:
            p["audio"] = saved
    report = (st.get("wspr") or {}).get("report") or {}
    if p.get("decoder") == "wspr" and "reason" not in report and "enabled" in report:
        p["wspr_report"] = bool(report["enabled"])
    return p


def save_running(name):
    """Save rxd's tuning into name; False if no rxd answers to save from."""
    st = rxd_status()
    if st is None:
        return False
    store(name, capture(profile(name), st))
    return True


def switch(name, resume=False):
    prev = active()
    target = profile(name)
    unit_name(name)  # a name rxd cannot be started on fails before anything stops

    # Re-running the profile already playing changes nothing: a restart would
    # throw away tuning done live over HTTP. rxd's own idea of its profile
    # guards against an instance started for some other name.
    st = rxd_status()
    same = prev == name and st is not None and st.get("profile", name) == name

    # Save the profile being left, so coming back resumes it. Resuming at
    # login has nothing current to save.
    if prev and st is not None and not resume and not same:
        store(prev, capture(profile(prev), st))
        target = profile(name)

    set_active(name)
    if not same:
        ensure_stored(name, target)
        rxd_stop()
        rxd_start(name)

    if decoder(target) == "js8":
        check_clock("JS8")
    elif decoder(target) == "wspr":
        check_clock("WSPR")

    print(summary(name, target))
    # The one setting no file can change.
    if prev and prev != name and STATION.manual_switch:
        was = upconverting(profile(prev))
        now = upconverting(target)
        if was != now:
            say("flip the %s to %s" % (STATION.upconverter_name,
                                       "UPCONVERT" if now else "BYPASS"), "warning")


# ------------------------------------------------------------- extensions

def extensions():
    """The radio-CMD commands on PATH: {CMD: path}."""
    out = {}
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            cmd = f[len("radio-"):]
            if f.startswith("radio-") and re.fullmatch(r"[a-z][a-z0-9-]*", cmd) \
                    and cmd not in out and os.access(os.path.join(d, f), os.X_OK):
                out[cmd] = os.path.join(d, f)
    return out


BUILTINS = ("off", "resume", "save", "reset", "show", "help")


def run_extension(cmd, args, focus):
    """Hand radio CMD ARGS... to radio-CMD, if CMD is no profile of ours."""
    if cmd is None or cmd in BUILTINS or cmd in known() or cmd in ALIASES:
        return False
    path = extensions().get(cmd)
    if not path:
        return False
    os.execv(path, [path] + ([] if focus else ["--no-focus"]) + list(args))


def main():
    ap = argparse.ArgumentParser(prog="radio", add_help=True)
    ap.add_argument("--no-focus", action="store_true")
    ap.add_argument("--names", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("cmd", nargs="?")
    ap.add_argument("arg", nargs="?")
    rest = sys.argv[1:]
    if rest[:1] == ["--no-focus"]:
        rest, focus_flag = rest[1:], False
    else:
        focus_flag = True
    if rest and not rest[0].startswith("-"):
        # radio [--no-focus] CMD ARGS... for a radio-CMD: free text may follow,
        # so the rest goes over verbatim rather than through argparse.
        run_extension(rest[0], rest[1:], focus_flag)
    a = ap.parse_args()

    if a.cmd in ALIASES:  # js8-40m and wspr-40m are js8 and wspr
        a.cmd = ALIASES[a.cmd]
    if a.arg in ALIASES and a.cmd in ("show", "reset", "save"):
        a.arg = ALIASES[a.arg]

    if a.names:  # for tab completion
        print("\n".join(known() + sorted(ALIASES) + sorted(extensions())))
    elif a.json:  # for rxd's browser UI: the profiles and which is active
        rows = []
        for n in known():
            p = profile(n)
            rows.append({"name": n, "description": p.get("description", ""),
                         "freq": float(p["frequency"]),
                         "mode": p["demod"], "upconvert": upconverting(p),
                         "js8": decoder(p) == "js8",
                         "decoder": decoder(p),
                         "band": band_of(float(p["frequency"])),
                         "plan": n in BAND_PLAN,
                         "saved": os.path.isfile(os.path.join(STATE, n + ".json"))})
        print(json.dumps({"active": active(), "profiles": rows}))
    elif a.cmd is None:
        cur = active()
        for n in known():
            saved = os.path.isfile(os.path.join(STATE, n + ".json"))
            print("%s %s%s" % ("*" if n == cur else " ", summary(n, profile(n)),
                               "  (saved)" if saved else ""))
        print("\nradio NAME switches · radio off · radio help · Tab completes names")
        more = sorted(extensions())
        if more:
            print("more: " + " · ".join("radio " + c for c in more))
    elif a.cmd == "help":
        print(__doc__.split("\n\n", 1)[1].split("\nA profile is")[0])
    elif a.cmd == "off":
        name = active()
        if name:
            save_running(name)
        rxd_stop()
        print("rxd stopped" + ("; 'radio resume' brings back %s" % name if name else ""))
    elif a.cmd == "resume":
        name = active()
        if name:
            switch(name, resume=True)
        else:
            print("no active profile; 'radio NAME' picks one")
    elif a.cmd == "save":
        name = a.arg or active() or die("nothing active; name the profile: radio save NAME")
        unit_name(name)  # a profile rxd could not be started on is no use
        st = rxd_status()
        if st is None:
            die("rxd is not running; nothing to save")
        base = profile(name) if name in known() else dict(profile(active() or "ghostnet"),
                                                          description=name)
        store(name, capture(base, st))
        set_active(name)
        print("saved " + summary(name, profile(name)))
    elif a.cmd == "reset":
        if not a.arg:
            die("usage: radio reset NAME")
        path = os.path.join(STATE, a.arg + ".json")
        if os.path.isfile(path):
            os.remove(path)
        print("reset %s to its default" % a.arg if a.arg in PROFILES
              else "removed %s" % a.arg)
    elif a.cmd == "show":
        print(json.dumps(profile(a.arg or active() or "ghostnet"), indent=2))
    else:
        switch(a.cmd)


if __name__ == "__main__":
    main()
