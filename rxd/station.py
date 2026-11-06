"""station: what this station is and what hardware it has, for rxd and radio.

One JSON file, $RXD_STATION or /etc/rxd/station.json, written by whoever sets
the machine up (rx's setup.sh, or you by hand). Every key is optional, and
with no file at all rxd and radio use the generic defaults below: no callsign
or grid (so no WSPRnet reports), the first RTL-SDR found, no upconverter, and
the default audio sink. See docs/station.md.

    {
      "call": "KA1PID",
      "grid": "EN52lk",
      "sdr": {"serial": "57818712"},
      "upconverter": {"name": "Ham It Up", "hz": 125000000,
                      "below_hz": 30000000, "switch": true},
      "sink": "Built-in Audio",
      "gps": {"enabled": true, "gpsd": "127.0.0.1:2947"},
      "profiles": {"dcara": {"description": "DCARA", "frequency": 146920000,
                             "demod": "FM", "demod_settings": {"bandwidth": 12500}}}
    }

$RXD_CALL and $RXD_GRID, when set, win over "call" and "grid", and $RXD_GPSD
("HOST:PORT" or "off") over "gps". rxd's GPS (location.py) wins over "grid"
when it has a fix.
"""
import json
import os
import re
import threading
import time

PROFILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
MODES = ("USB", "LSB", "AM", "FM", "WFM")

DEFAULT_PATH = "/etc/rxd/station.json"
GRID = re.compile(r"^[A-Ra-r]{2}[0-9]{2}(?:[A-Xa-x]{2})?$")
HOSTPORT = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9_.-]+):[0-9]{1,5}$")
GPS_DEFAULTS = {"enabled": True, "gpsd": "127.0.0.1:2947"}
UPCONVERTER_DEFAULTS = {"name": "upconverter", "hz": 125e6, "below_hz": 30e6, "switch": True}


def path():
    return os.environ.get("RXD_STATION") or DEFAULT_PATH


class Station:
    """The station file, read and checked. A bad value is reported in
    .problems and replaced by its default; it never stops rxd."""

    def __init__(self, doc=None, source=None, problems=()):
        self.source = source          # the file read, or None
        self.problems = list(problems)
        doc = doc if isinstance(doc, dict) else {}
        self.call = self._call(doc.get("call"))
        self.grid = self._grid(doc.get("grid"))
        sdr = doc.get("sdr") or {}
        if not isinstance(sdr, dict):
            self.problems.append("sdr must be an object")
            sdr = {}
        serial = sdr.get("serial", "")
        if isinstance(serial, int) and not isinstance(serial, bool):
            serial = str(serial)
        if not isinstance(serial, str):
            self.problems.append("sdr.serial must be a string")
            serial = ""
        self.serial = serial.strip()
        self.upconverter = self._upconverter(doc.get("upconverter"))
        sink = doc.get("sink")
        if sink is not None and not isinstance(sink, str):
            self.problems.append("sink must be a string or null")
            sink = None
        self.sink = sink or None
        self.profiles = self._profiles(doc.get("profiles"))
        self.gps = self._gps(doc.get("gps"))

    def _call(self, v):
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            self.problems.append("call must be a callsign")
            return None
        return v.strip().upper()

    def _grid(self, v):
        if v is None:
            return None
        if not isinstance(v, str) or not GRID.match(v.strip()):
            self.problems.append("grid must be a Maidenhead locator, like EN52lk")
            return None
        g = v.strip()
        return g[:4].upper() + g[4:].lower()

    def _profiles(self, v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            self.problems.append("profiles must be an object of name: profile")
            return {}
        out = {}
        for name, p in v.items():
            if not PROFILE_NAME.match(name):
                self.problems.append("profile name %r: use letters, digits, '.', '-' or '_'" % name)
            elif not isinstance(p, dict):
                self.problems.append("profiles.%s must be an object" % name)
            elif isinstance(p.get("frequency"), bool) or \
                    not isinstance(p.get("frequency"), (int, float)) or p["frequency"] <= 0:
                self.problems.append("profiles.%s needs a frequency in Hz" % name)
            elif p.get("demod", "USB") not in MODES:
                self.problems.append("profiles.%s: demod must be one of %s" % (name, ", ".join(MODES)))
            else:
                q = dict(p)
                q["frequency"] = float(q["frequency"])
                q.setdefault("description", name)
                q.setdefault("demod", "USB")
                out[name] = q
        return out

    def _gps(self, v):
        out = dict(GPS_DEFAULTS)
        if v is None:
            return out
        if v is False:
            return dict(out, enabled=False)
        if not isinstance(v, dict):
            self.problems.append("gps must be an object, or false")
            return out
        if "enabled" in v:
            if isinstance(v["enabled"], bool):
                out["enabled"] = v["enabled"]
            else:
                self.problems.append("gps.enabled must be true or false")
        if "gpsd" in v:
            if isinstance(v["gpsd"], str) and HOSTPORT.match(v["gpsd"].strip()):
                out["gpsd"] = v["gpsd"].strip()
            else:
                self.problems.append("gps.gpsd must be HOST:PORT, like 127.0.0.1:2947")
        return out

    def _upconverter(self, v):
        if v is None or v is False:
            return None
        if not isinstance(v, dict):
            self.problems.append("upconverter must be an object or null")
            return None
        out = dict(UPCONVERTER_DEFAULTS)
        for k in ("hz", "below_hz"):
            if k in v:
                x = v[k]
                if isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0:
                    self.problems.append("upconverter.%s must be a number of Hz" % k)
                else:
                    out[k] = float(x)
        if "name" in v:
            if isinstance(v["name"], str) and v["name"].strip():
                out["name"] = v["name"].strip()
            else:
                self.problems.append("upconverter.name must be a string")
        if "switch" in v:
            if isinstance(v["switch"], bool):
                out["switch"] = v["switch"]
            else:
                self.problems.append("upconverter.switch must be true or false")
        out["hz"] = float(out["hz"])
        out["below_hz"] = float(out["below_hz"])
        return out if out["hz"] > 0 else None

    # what rxd and radio use -------------------------------------------------

    @property
    def upconverter_hz(self):
        return self.upconverter["hz"] if self.upconverter else 0.0

    @property
    def upconvert_below_hz(self):
        return self.upconverter["below_hz"] if self.upconverter else 0.0

    @property
    def upconverter_name(self):
        return self.upconverter["name"] if self.upconverter else None

    @property
    def manual_switch(self):
        """True when the upconverter has a switch only a person can flip, so a
        profile on the other side of it can't be reached by software."""
        return bool(self.upconverter and self.upconverter["switch"])

    def identity(self):
        """(call, grid), with $RXD_CALL and $RXD_GRID winning when set."""
        call, grid = self.call, self.grid
        env_call, env_grid = os.environ.get("RXD_CALL"), os.environ.get("RXD_GRID")
        if env_call is not None:
            call = env_call.strip().upper() or None
        if env_grid is not None:
            g = env_grid.strip()
            grid = g[:4].upper() + g[4:].lower() if GRID.match(g) else None
        return call, grid

    def gpsd(self):
        """gpsd's HOST:PORT, or None when the GPS is off ($RXD_GPSD wins)."""
        env = os.environ.get("RXD_GPSD")
        if env is not None:
            env = env.strip()
            if env.lower() in ("", "off", "0", "false", "no"):
                return None
            if HOSTPORT.match(env):
                return env
        return self.gps["gpsd"] if self.gps["enabled"] else None

    def describe(self):
        """For rxd's /api/v1/status: what the clients need to know."""
        up = None
        if self.upconverter:
            up = {"name": self.upconverter["name"], "hz": self.upconverter["hz"],
                  "below_hz": self.upconverter["below_hz"], "switch": self.upconverter["switch"]}
        return {"file": self.source, "upconverter": up, "problems": list(self.problems)}


# ------------------------------------------------- the upconverter's switch
# An upconverter with "switch": true has an UPCONVERT/BYPASS switch only a
# person can flip, which rxd can't see. So a person says which way it is, and
# rxd and radio keep that here with when it was said, and the boot it was said
# in: after a reboot or a power-up nobody knows (it may have been flipped while
# the machine was off), so a position from another boot reads as "unknown".
# A plain rxd restart or a profile switch keeps it. See docs/station.md.

SWITCH_PATH = "~/.local/state/rxd/switch.json"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
POSITIONS = ("upconvert", "bypass")


def switch_path():
    return os.path.expanduser(os.environ.get("RXD_SWITCH") or SWITCH_PATH)


def boot_id():
    """This boot's ID (Linux's boot_id, or the file $RXD_BOOT_ID names), or
    None where there's none: then a confirmed position lasts until changed."""
    try:
        with open(os.environ.get("RXD_BOOT_ID") or BOOT_ID_PATH, encoding="ascii") as fh:
            return fh.read().strip() or None
    except (OSError, ValueError):
        return None


def side(upconverting):
    """The switch position a profile needs: "upconvert" or "bypass"."""
    return "upconvert" if upconverting else "bypass"


def side_word(position):
    """"UPCONVERT", "BYPASS", or "not confirmed", as the clients say it."""
    return position.upper() if position in POSITIONS else "not confirmed"


def read_switch(path=None):
    """{"position": "upconvert", "bypass" or "unknown", "confirmed_at": epoch
    seconds or None, "why": why it's unknown, or None}."""
    path = path or switch_path()
    unknown = {"position": "unknown", "confirmed_at": None}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return dict(unknown, why="not confirmed yet")
    except (OSError, ValueError) as e:
        return dict(unknown, why="can't read %s: %s" % (path, e))
    if not isinstance(doc, dict) or doc.get("position") not in POSITIONS or \
            isinstance(doc.get("confirmed_at"), bool) or \
            not isinstance(doc.get("confirmed_at"), (int, float)):
        return dict(unknown, why="%s isn't a confirmed position" % path)
    if doc.get("boot_id") != boot_id():
        return dict(unknown, why="the machine has started up since it was confirmed")
    return {"position": doc["position"], "confirmed_at": float(doc["confirmed_at"]), "why": None}


def confirm_switch(position, path=None, now=None):
    """Keep position as the switch's, confirmed now, for this boot. ValueError
    for a position that isn't one; OSError when it can't be saved."""
    if position not in POSITIONS:
        raise ValueError('the switch is "upconvert" or "bypass"')
    path = path or switch_path()
    doc = {"position": position, "confirmed_at": round(time.time() if now is None else now, 3),
           "boot_id": boot_id()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)
    return read_switch(path)


def load(file=None):
    """The station at file (default: $RXD_STATION or /etc/rxd/station.json).
    A missing file is the generic station; an unreadable one is too, with the
    reason in .problems."""
    file = file or path()
    try:
        with open(file, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return Station()
    except (OSError, ValueError) as e:
        return Station(source=file, problems=["can't read %s: %s" % (file, e)])
    if not isinstance(doc, dict):
        return Station(source=file, problems=["%s must hold a JSON object" % file])
    return Station(doc, source=file)
