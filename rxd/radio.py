#!/usr/bin/env python3
"""Managed by rx/setup.sh — switchable, resumable receiver profiles.

  radio                list profiles; * marks the active one
  radio NAME           switch to NAME. rxd, the receiver, restarts on NAME; on
                       a JS8 or WSPR profile it decodes that mode itself.
                       The profile being left is saved first, so coming back
                       to it resumes where it was. NAME when it is already up
                       changes nothing.
  radio off            save the active profile and stop rxd
  radio resume         bring the active profile back up (i3 runs it at login)
  radio save [NAME]    save rxd's current tuning as NAME (default: active)
  radio reset NAME     forget your saved state for NAME; back to its default
  radio show NAME      print a profile as JSON
  radio help           this text

  radio meshtastic     put the LoRa radio on Meshtastic, North Texas settings
  radio meshcore       put the LoRa radio on MeshCore, North Texas settings
  radio mesh           start the active mesh and open its client
  radio mesh nodes     who has been heard
  radio mesh send TEXT          to the primary (Meshtastic) or public channel
  radio mesh dm NAME TEXT       direct message; Tab completes names
  radio mesh join URL           Meshtastic: add channels from a shared URL
  radio mesh join NAME [KEY]    MeshCore: add a channel (#name needs no key)
  radio mesh info      identity, radio settings, channels -- without starting it
  radio mesh on | off  start / stop the active mesh

  Every mesh command but info starts the mesh if it is not running. The two
  networks share one radio, so choosing one stops the other. Neither starts at
  boot.

  --no-focus           leave focus where it is (radio mesh opens its client)

Tab completes profile names (/usr/share/bash-completion/completions/radio).

Every amateur band has a JS8 and a WSPR profile on its standard dial:
js8-160m ... js8-2m and wspr-2190m ... wspr-2m. On 40 m they keep their old
names, js8 and wspr (js8-40m and wspr-40m mean the same). 6 m and 2 m need
the Ham It Up on BYPASS. rxd's band plan (Auto in its Digital view) picks
among them by band conditions, time of day and what it has heard.

A profile is tuning only: the dial frequency, offset mode, demodulator and
its settings, audio sink and volume, the RTL-SDR's settings, and what rxd
decodes ("decoder": "js8" or "wspr"). A WSPR profile also says whether rxd
reports its spots to WSPRnet ("wspr_report", on by default). Both decoders
work on the audio before the volume, so the volume is only the speaker's.
The defaults below are starting points; saved state lives in
~/.config/rx/radio/ and wins.

rxd (systemd user unit rxd@NAME) reads only ~/.config/rx/radio/NAME.json, so
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
import socket
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".config", "rx", "radio")
ACTIVE = os.path.join(STATE, "active")

# rxd's HTTP control port; module-level so the tests can point it elsewhere.
RXD_URL = "http://127.0.0.1:8073"
RXD_START_SECS = 15

NOOELEC = "57818712"  # NESDR SMArt v5, behind the Ham It Up
SPEAKER = "Built-in Audio"


def rtl(gain):
    # gain is an index into the tuner's gain table, not dB. On the R820T:
    # 4 = 3.7 dB, 10 = 16.6 dB, 20 = 37.2 dB. A saved gain_db wins over it.
    return {"serial": NOOELEC, "gain": gain, "biasT": False, "ppm": 0}


# USB as set by hand for JS8 on 2026-11-04.
USB_JS8 = {"bandwidth": 3000.0, "agcAttack": 47.916229248046875,
           "agcDecay": 4.896891117095947}
NFM = {"bandwidth": 12500.0}
WFM = {"bandwidth": 150000.0}
AM = {"bandwidth": 10000.0}


def js8(desc, dial):
    # rxd decodes each slot itself, from the audio before the volume; the
    # speaker plays it quietly, and the browser and rxd-tui can listen too.
    return {"description": desc, "frequency": float(dial), "offset": "auto",
            "demod": "USB", "demod_settings": USB_JS8, "sink": SPEAKER,
            "volume": 0.2, "rtl": rtl(4), "decoder": "js8"}


def wspr(desc, dial):
    # rxd records each two-minute slot and decodes it with wsprd; the
    # speaker stays silent (volume 0) but the browser and rxd-tui can listen.
    return {"description": desc, "frequency": dial, "offset": "auto",
            "demod": "USB", "demod_settings": {"bandwidth": 2800.0},
            "sink": SPEAKER, "volume": 0.0, "rtl": rtl(4),
            "decoder": "wspr", "wspr_report": True}


def vhf(desc, hz, demod="FM", settings=NFM, gain=20):
    return {"description": desc, "frequency": hz,
            "offset": "auto", "demod": demod, "demod_settings": settings,
            "sink": SPEAKER, "volume": 0.5, "rtl": rtl(gain)}


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

PROFILES = {
    "ghostnet": js8("JS8 GhostNet, 40 m", 7107000),
    **BAND_PLAN,
    "vhf":      vhf("2 m calling", 146520000.0),
    "dcara":    vhf("DCARA", 146920000.0),
    "uhf":      vhf("70 cm calling", 446000000.0),
    "noaa":     vhf("NOAA weather", 162550000.0),  # strongest of the seven at EM13
    "fm-93.3":  vhf("93.3 FM", 93300000.0, "WFM", WFM, gain=10),
    "fm-105.3": vhf("105.3 FM", 105300000.0, "WFM", WFM, gain=10),
    # Below the R820T's range, so through the Ham It Up like HF: "auto"
    # upconverts anything under 30 MHz.
    "am-820":   vhf("820 AM", 820000.0, "AM", AM, gain=4),
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
    """Whether rxd puts p through the Ham It Up's 125 MHz upconversion."""
    # "auto" (the default) follows the frequency, as rxd does: the R820T
    # cannot tune below about 24 MHz, so HF and AM broadcast go through the
    # upconverter. "Ham-It-Up" and "None" pin it on or off.
    mode = p.get("offset", "auto")
    if mode == "auto":
        return float(p["frequency"]) < float(p.get("upconvert_below_hz", 30e6))
    return mode != "None"


def decoder(p):
    return p.get("decoder")


def summary(name, p):
    path = "Ham It Up upconvert" if upconverting(p) else "Ham It Up bypass"
    return "%-10s %11.4f MHz %-4s %-19s %s" % (
        name, p["frequency"] / 1e6, p["demod"], path, p.get("description", ""))


def unit_name(name):
    # The unit passes %i to rxd verbatim, so keep to characters that need no
    # systemd escaping; anything else would reach rxd mangled.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        die("profile name '%s': use letters, digits, '.', '-' or '_'" % name)
    return "rxd@%s.service" % name


# ------------------------------------------------------------------ apps

def running(proc):
    return subprocess.run(["pgrep", "-x", proc],
                          stdout=subprocess.DEVNULL).returncode == 0


_desktop = {}


def desktop():
    """The environment for i3-msg (radio mesh moves its client's window): the
    desktop's, even when radio runs from SSH, a service or rxd (band plan
    switches), where DISPLAY and I3SOCK are unset. i3's socket is found the way i3 --get-socketpath would
    be asked, or failing that from i3's runtime directory."""
    if _desktop:
        return _desktop
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    if not env.get("XAUTHORITY") and os.path.isfile(os.path.join(HOME, ".Xauthority")):
        env["XAUTHORITY"] = os.path.join(HOME, ".Xauthority")
    if not env.get("I3SOCK"):
        sock = ""
        try:
            sock = subprocess.run(["i3", "--get-socketpath"], env=env, capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        if not sock:
            import glob
            found = glob.glob("/run/user/%d/i3/ipc-socket.*" % os.getuid())
            sock = max(found, key=os.path.getmtime) if found else ""
        if sock:
            env["I3SOCK"] = sock
    _desktop.update(env)
    return _desktop


def i3(cmd):
    subprocess.run(["i3-msg", "-q", cmd], env=desktop(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check_clock(mode):
    # JS8's 15 s and WSPR's 2 min slots tolerate about 2 s of clock error.
    # Outside that nothing decodes and nothing says why, so say it here.
    out = subprocess.run(["chronyc", "-n", "tracking"], capture_output=True,
                         text=True).stdout
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
    if prev and prev != name:
        was = upconverting(profile(prev))
        now = upconverting(target)
        if was != now:
            say("flip the Ham It Up to %s" % ("UPCONVERT" if now else "BYPASS"),
                "warning")


# --------------------------------------------------------------------- mesh

# Both networks share the AIO's SX1262, so one runs at a time. rx-mesh (root,
# through sudo) is the only thing that starts or stops their daemons.
MESHES = ("meshtastic", "meshcore")
MESH_FILE = os.path.join(STATE, "mesh")
MESH_UNITS = {"meshtastic": "meshtasticd", "meshcore": "raspberrypimc-companion"}
MESH_PORTS = {"meshtastic": 4403, "meshcore": 5000}
RX_MESH = "/usr/local/sbin/rx-mesh"
MT_PY = "/opt/meshtastic-cli/bin/python"
MT_OPS = "/usr/local/lib/rx/meshtastic_ops.py"
MESHCLI = "/opt/meshcore-cli/bin/meshcli"
NAMES_CACHE = os.path.join(HOME, ".cache", "rx", "mesh-names-%s")
MC_TYPES = {1: "companion", 2: "repeater", 3: "room", 4: "sensor"}


def mesh_conf():
    conf = {}
    try:
        with open("/etc/rx/mesh.conf", encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    conf[key] = value
    except OSError:
        die("no /etc/rx/mesh.conf — run setup.sh --only lora")
    return conf


def mesh_network():
    try:
        with open(MESH_FILE, encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        name = ""
    return name if name in MESHES else "meshtastic"


def mesh_running():
    for net in MESHES:
        if subprocess.run(["systemctl", "is-active", "--quiet", MESH_UNITS[net]]).returncode == 0:
            return net
    return None


def wait_port(port, secs=60):
    for _ in range(secs * 2):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def mesh_up(net, apply=False):
    """Make net the running mesh, applying its settings when it (re)starts."""
    started = False
    if mesh_running() != net:
        if subprocess.run(["sudo", "-n", RX_MESH, "start", net]).returncode != 0:
            die("could not start %s (rx-mesh via sudo; re-run setup.sh --only lora)" % net)
        started = True
    if not wait_port(MESH_PORTS[net]):
        die("%s started but is not answering on port %d" % (net, MESH_PORTS[net]))
    if apply or started:
        mesh_apply(net, started)


def mt_ops(*args, check=True):
    if not os.path.exists(MT_OPS):
        die("no %s — run setup.sh --only lora" % MT_OPS)
    rc = subprocess.run([MT_PY, MT_OPS, *args]).returncode
    if check and rc != 0:
        sys.exit(rc)
    return rc


def meshcli(*args, as_json=False):
    cmd = [MESHCLI, "-q", "-t", "127.0.0.1", "-p", str(MESH_PORTS["meshcore"])]
    if as_json:
        cmd.append("-j")
    out = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        die("meshcore-cli %s failed: %s" % (args[0], (out.stderr or out.stdout).strip()[-200:]))
    if not as_json:
        return out.stdout
    text = out.stdout.strip()
    try:
        return json.loads(text) if text else {}
    except ValueError:
        die("meshcore-cli %s returned something unexpected" % args[0])


def mesh_apply(net, started):
    conf = mesh_conf()
    if net == "meshtastic":
        mt_ops("apply")  # waits out the node's own reboot
        wait_port(MESH_PORTS[net])
        return
    # MeshCore's radio settings live in companion.env and are applied at
    # every start; only the identity is set through the client.
    info = meshcli("infos", as_json=True)
    if info.get("name") != conf["NODE_NAME"]:
        meshcli("set", "name", conf["NODE_NAME"])
        print("meshcore: name set to %s" % conf["NODE_NAME"])
    # One flood advert per start, so the mesh learns this node exists --
    # MeshCore nodes only appear in each other's contacts from adverts.
    if started:
        meshcli("floodadv")
        print("meshcore: sent an advert")


def age(epoch):
    if not epoch:
        return "-"
    secs = int(time.time() - epoch)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return "%d%s ago" % (secs // n, unit)
    return "%ds ago" % secs


def mc_nodes():
    contacts = meshcli("contacts", as_json=True) or {}
    rows = sorted(contacts.values(), key=lambda c: c.get("last_advert", 0), reverse=True)
    os.makedirs(os.path.dirname(NAMES_CACHE), exist_ok=True)
    with open(NAMES_CACHE % "meshcore", "w", encoding="utf-8") as fh:
        fh.write("".join(c["adv_name"] + "\n" for c in rows if c.get("adv_name") and " " not in c["adv_name"]))
    if not rows:
        print("no contacts yet — MeshCore learns nodes from their adverts")
        return
    print("%-24s %-10s %-6s %s" % ("NAME", "TYPE", "HOPS", "LAST ADVERT"))
    for c in rows:
        hops = c.get("out_path_len", -1)
        print("%-24s %-10s %-6s %s" % (c.get("adv_name", "?")[:24], MC_TYPES.get(c.get("type"), "?"),
                                       "flood" if hops < 0 else hops, age(c.get("last_advert"))))


def mc_info():
    me = meshcli("infos", as_json=True)
    print("network   MeshCore")
    print("identity  %s %s" % (me.get("name"), me.get("public_key", "")[:16]))
    print("radio     %.3f MHz, %.1f kHz, SF %s, CR %s, %s dBm" % (
        me.get("radio_freq", 0), me.get("radio_bw", 0), me.get("radio_sf"), me.get("radio_cr"),
        me.get("tx_power")))
    print("position  %s" % ("never sent" if not me.get("adv_lat") and not me.get("adv_lon") else "in adverts"))
    chans = meshcli("get_channels", as_json=True) or []
    for ch in chans if isinstance(chans, list) else []:
        print("channel   %s %s" % (ch.get("channel_idx"), ch.get("channel_name")))
    contacts = meshcli("contacts", as_json=True) or {}
    print("heard     %d contacts" % len(contacts))


def mesh_main(argv, focus):
    net = mesh_network()
    sub, args = (argv[0] if argv else None), argv[1:]

    if sub == "info":
        if mesh_running() == net:
            mt_ops("info") if net == "meshtastic" else mc_info()
        else:
            print("network   %s (off; 'radio mesh on' starts it)" % ("Meshtastic" if net == "meshtastic" else "MeshCore"))
        return
    if sub == "off":
        subprocess.run(["sudo", "-n", RX_MESH, "stop"], check=False)
        print("mesh off")
        return
    if sub not in (None, "on", "nodes", "send", "dm", "join"):
        die("unknown mesh command '%s' (see: radio help)" % sub)

    mesh_up(net)
    if sub == "on":
        print("%s on" % net)
    elif sub is None:
        if net == "meshtastic":
            # It sets no WM_CLASS, so there is no window to wait for by class;
            # i3's title rule moves it to workspace 3.
            if not running("meshtastic-ui"):
                subprocess.Popen(["meshtastic-ui"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            if focus:
                i3("workspace number 3")
        elif sys.stdin.isatty():
            os.execv(MESHCLI, [MESHCLI, "-t", "127.0.0.1", "-p", str(MESH_PORTS["meshcore"])])
        else:
            print("MeshCore is up; for its chat client run: meshcore-cli -t 127.0.0.1")
    elif net == "meshtastic":
        mt_ops(sub, *args)
    elif sub == "nodes":
        mc_nodes()
    elif sub == "send" and args:
        meshcli("public", " ".join(args))
        print("sent to the public channel")
    elif sub == "dm" and len(args) >= 2:
        sys.stdout.write(meshcli("msg", args[0], " ".join(args[1:])))
    elif sub == "join" and args:
        meshcli("add_channel", *args[:2])
        mc_info()
    else:
        die("usage: radio mesh %s ... (see: radio help)" % sub)


def choose_mesh(net, focus):
    os.makedirs(STATE, exist_ok=True)
    with open(MESH_FILE, "w", encoding="utf-8") as fh:
        fh.write(net + "\n")
    mesh_up(net, apply=True)
    mt_ops("info") if net == "meshtastic" else mc_info()


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
    if rest[:1] == ["mesh"]:
        # radio [--no-focus] mesh SUB ARGS...: free text follows, so take the
        # rest verbatim rather than through argparse.
        mesh_main(rest[1:], focus_flag)
        return
    a = ap.parse_args()
    focus = not a.no_focus

    if a.cmd in ALIASES:  # js8-40m and wspr-40m are js8 and wspr
        a.cmd = ALIASES[a.cmd]
    if a.arg in ALIASES and a.cmd in ("show", "reset", "save"):
        a.arg = ALIASES[a.arg]

    if a.names:  # for tab completion
        print("\n".join(known() + sorted(ALIASES)))
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
        print("mesh: radio %s (active) · radio mesh nodes|send|dm|join|info|on|off" % mesh_network())
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
    elif a.cmd in MESHES:
        choose_mesh(a.cmd, focus)
    elif a.cmd == "show":
        print(json.dumps(profile(a.arg or active() or "ghostnet"), indent=2))
    else:
        switch(a.cmd)


if __name__ == "__main__":
    main()
