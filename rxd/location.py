"""location: where the station is, from its GPS, for rxd.

The station's grid comes from, in order:

  1. a live GPS fix rxd has accepted ("gps"),
  2. the last GPS grid rxd accepted, saved on disk ("gps-saved"),
  3. the station file's "grid" (/etc/rxd/station.json, or $RXD_GRID) ("station"),
  4. nothing.

The GPS is read through gpsd (127.0.0.1:2947 by default; station.json's
"gps" or $RXD_GPSD change it, and "off" turns it off). rxd asks gpsd for the
receiver's own bytes as well as its JSON ("raw": 2), because some receivers'
NMEA is not to be trusted: a u-blox clone was seen reporting a placeholder
position (3000.0000 N, 11960.0000 E: minutes of 60, no satellites in view) in
NMEA while its UBX binary NAV-SOL held a real fix. So:

  - UBX NAV-SOL (or NAV-POSLLH) is read from the raw stream and preferred;
  - gpsd's TPV and SKY are the second source, for receivers that only speak
    NMEA, and a TPV position must pass placeholder checks first.

A fix is accepted only when it is believable (a 2D or 3D fix, 5 or more
satellites, accurate to better than 100 m) and steady: the same 6-character
grid for 3 minutes, with a margin at the subsquare's edges so a fix jittering
across one doesn't flap between two grids.

PRIVACY: only the 6-character grid ever leaves this module. Precise latitude
and longitude are never stored, logged, saved or put in a status; everything
downstream uses the grid's centre. The saved file holds the grid, its source
and when it was accepted, nothing else.
"""
import json
import math
import os
import re
import socket
import struct
import threading
import time

DEFAULT_GPSD = "127.0.0.1:2947"
STATE_FILE = "~/.local/state/rxd/location.json"
GRID6 = re.compile(r"^[A-R]{2}[0-9]{2}[a-x]{2}$")
SOURCES = ("gps/ubx", "gps/tpv")

# What makes a fix believable.
MIN_SATS = 5
MAX_ACC_M = 100.0
HOLD_S = 180.0      # the same grid this long before it's accepted
GAP_S = 20.0        # no good fix for this long: the fix is lost
MARGIN = 0.15       # of a subsquare: how far past its edge a fix may wander
FRESH_S = 5.0       # a source heard from this recently is "present"

# Positions receivers report before they have a fix (or forever, if they're
# clones): factory defaults, as (lat, lon). Any whole-degree pair is caught
# too; these are the ones seen in the wild.
PLACEHOLDERS = {(0.0, 0.0), (30.0, 120.0)}

# WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_B = _A * (1 - _F)


def state_path():
    return os.path.expanduser(os.environ.get("RXD_LOCATION") or STATE_FILE)


# ------------------------------------------------------------ geometry

def ecef_to_geodetic(x, y, z):
    """WGS84 ECEF metres -> (lat, lon) degrees."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-6:
        return (90.0 if z >= 0 else -90.0), math.degrees(lon)
    lat = math.atan2(z, p * (1 - _E2))
    for _ in range(6):
        s = math.sin(lat)
        n = _A / math.sqrt(1 - _E2 * s * s)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon)


def geodetic_to_ecef(lat, lon, h=0.0):
    """(lat, lon) degrees and height in metres -> WGS84 ECEF metres."""
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    return ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
            (n * (1 - _E2) + h) * math.sin(la))


def maidenhead(lat, lon):
    """(lat, lon) -> the 6-character grid, EM13lc style."""
    lon = min(max(lon + 180.0, 0.0), 360.0 - 1e-9)
    lat = min(max(lat + 90.0, 0.0), 180.0 - 1e-9)
    return (chr(65 + int(lon // 20)) + chr(65 + int(lat // 10)) +
            str(int(lon % 20 // 2)) + str(int(lat % 10)) +
            chr(97 + int(lon % 2 * 12)) + chr(97 + int(lat % 1 * 24)))


def grid_box(grid):
    """A 6-character grid's (lat0, lon0, height, width) in degrees."""
    g = grid[:4].upper() + grid[4:].lower()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2 + (ord(g[4]) - 97) * (2 / 24)
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3]) + (ord(g[5]) - 97) * (1 / 24)
    return lat, lon, 1 / 24, 2 / 24


def near(lat, lon, grid, margin=MARGIN):
    """Whether (lat, lon) is in grid or within margin (of its size) of it."""
    lat0, lon0, h, w = grid_box(grid)
    return (lat0 - margin * h <= lat <= lat0 + h + margin * h and
            lon0 - margin * w <= lon <= lon0 + w + margin * w)


# ---------------------------------------------------------------- UBX

def ubx_checksum(body):
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def ubx_frame(cls, mid, payload):
    """A UBX frame (for tests and fakes)."""
    body = bytes((cls, mid)) + struct.pack("<H", len(payload)) + payload
    return b"\xb5\x62" + body + ubx_checksum(body)


def parse_nav_sol(p):
    """UBX NAV-SOL (0x01 0x06) -> {"fix", "ok", "sv", "acc_m", "ecef"}, or None."""
    if len(p) < 52:
        return None
    fix, flags = p[10], p[11]
    x, y, z, pacc = struct.unpack_from("<iiiI", p, 12)
    return {"fix": fix, "ok": bool(flags & 1), "sv": p[47], "acc_m": pacc / 100.0,
            "ecef": (x / 100.0, y / 100.0, z / 100.0)}


def parse_nav_posllh(p):
    """UBX NAV-POSLLH (0x01 0x02) -> {"lat", "lon", "acc_m"}, or None."""
    if len(p) < 28:
        return None
    lon, lat = struct.unpack_from("<ii", p, 4)
    hacc = struct.unpack_from("<I", p, 20)[0]
    return {"lat": lat * 1e-7, "lon": lon * 1e-7, "acc_m": hacc / 1000.0}


def parse_nav_status(p):
    """UBX NAV-STATUS (0x01 0x03) -> {"fix", "ok"}, or None."""
    if len(p) < 16:
        return None
    return {"fix": p[4], "ok": bool(p[5] & 1)}


# --------------------------------------------------------------- NMEA

def nmea_ok(sentence):
    """Whether an NMEA sentence's checksum (if it has one) is right."""
    s = sentence.strip()
    if not s.startswith("$"):
        return False
    body, star, ck = s[1:].partition("*")
    if not star:
        return True
    x = 0
    for c in body.encode("ascii", "replace"):
        x ^= c
    return ck[:2].upper() == "%02X" % x


def nmea_placeholder(sentence):
    """Why an NMEA GGA or RMC sentence's position is a placeholder, or None.
    Minutes of 60 or more can't be real: 11960.0000 is 119 degrees 60 minutes."""
    s = sentence.strip()
    if not nmea_ok(s):
        return None
    f = s.split("*")[0].split(",")
    kind = f[0][-3:]
    if kind == "GGA" and len(f) > 5:
        lat, lon = f[2], f[4]
    elif kind == "RMC" and len(f) > 6:
        lat, lon = f[3], f[5]
    else:
        return None
    try:
        lat_min, lon_min = float(lat[2:]), float(lon[3:])
    except ValueError:
        return None
    if lat_min >= 60 or lon_min >= 60:
        return "its NMEA position has minutes of 60 or more"
    return None


def is_placeholder(lat, lon):
    """Whether a position is one receivers report when they have no real fix."""
    if (round(lat, 6), round(lon, 6)) in PLACEHOLDERS:
        return True
    return lat == round(lat) and lon == round(lon)


def tpv_problem(tpv, used, nmea_fake=False):
    """Why gpsd's TPV position isn't believable, or None. used is SKY's count
    of satellites used (None when there's no SKY)."""
    mode = tpv.get("mode") or 0
    if mode < 2:
        return "no fix"
    lat, lon = tpv.get("lat"), tpv.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return "no fix"
    if nmea_fake or is_placeholder(lat, lon):
        return "placeholder"
    if used == 0:
        return "no satellites"
    if used is None or used < MIN_SATS:
        return "satellites"
    eph = tpv.get("eph")
    if not isinstance(eph, (int, float)) or eph >= MAX_ACC_M:
        return "accuracy"
    return None


def ubx_problem(sol, lat, lon):
    if sol["fix"] not in (2, 3) or not sol["ok"]:
        return "no fix"
    if is_placeholder(lat, lon):
        return "placeholder"
    if sol["sv"] is None or sol["sv"] < MIN_SATS:
        return "satellites"
    if sol["acc_m"] is None or sol["acc_m"] >= MAX_ACC_M:
        return "accuracy"
    return None


# ----------------------------------------------------------- steadiness

class Steady:
    """Accepts a grid once fixes hold it for hold seconds without a gap.

    A fix within margin of the accepted grid (or of the grid being timed)
    counts as that grid, so a position jittering over a subsquare's edge
    keeps the grid it has. Only the grid is kept: positions are used and
    dropped.
    """

    def __init__(self, hold=HOLD_S, gap=GAP_S, margin=MARGIN):
        self.hold, self.gap, self.margin = hold, gap, margin
        self.accepted = None
        self.cand = None
        self.since = None
        self.last = None

    def offer(self, lat, lon, t):
        """A believable fix at time t; returns the grid if it's newly accepted."""
        if self.last is not None and t - self.last > self.gap:
            self.cand = None
        self.last = t
        if self.accepted and near(lat, lon, self.accepted, self.margin):
            self.cand = None
            return None
        g = maidenhead(lat, lon)
        if self.cand is None or not near(lat, lon, self.cand, self.margin):
            self.cand, self.since = g, t
        if t - self.since >= self.hold:
            self.accepted, self.cand = self.cand, None
            return self.accepted
        return None

    def live(self, t):
        return self.last is not None and t - self.last <= self.gap

    def settling(self, t):
        """Seconds until the grid being timed is accepted, or None."""
        if self.cand is None or not self.live(t):
            return None
        return max(0.0, self.hold - (t - self.since))


# --------------------------------------------------------------- gpsd

REASONS = {
    "placeholder": "the receiver reports a placeholder position, not a real fix",
    "no satellites": "the receiver reports a position but uses no satellites",
    "no fix": "the receiver has no fix",
}


def _problem_words(problem, sv=None, acc=None):
    if problem == "satellites":
        return "only %s satellite%s (needs %d)" % (
            sv if sv is not None else "an unknown number of", "" if sv == 1 else "s", MIN_SATS)
    if problem == "accuracy":
        return ("accurate only to about %d m (needs better than %d m)" % (acc, MAX_ACC_M)
                if acc is not None else "its accuracy is unknown")
    return REASONS.get(problem, problem)


def parse_hostport(text, default=DEFAULT_GPSD):
    host, _, port = (text or default).rpartition(":")
    return host.strip("[]") or "127.0.0.1", int(port or 2947)


class GpsLocation:
    """A thread reading gpsd: UBX from the raw stream, TPV and SKY from its
    JSON. on_accept(grid, source) is called when a grid is accepted."""

    RETRY_S = 30.0
    BUF_MAX = 65536

    def __init__(self, gpsd=DEFAULT_GPSD, on_accept=None, log=None, clock=time.monotonic,
                 hold=HOLD_S, gap=GAP_S, margin=MARGIN):
        self.gpsd = gpsd
        self.addr = parse_hostport(gpsd)
        self.on_accept = on_accept or (lambda g, s: None)
        self.log = log or (lambda m: None)
        self.clock = clock
        self.steady = Steady(hold, gap, margin)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.buf = b""
        self.connected = False
        self.conn_error = None
        self.devices = None          # gpsd's DEVICES list length, once seen
        self.heard = None            # when anything last came from gpsd
        self.ubx = None              # {"t", "problem", "sv", "acc"} of the last UBX position
        self.ubx_status = None       # {"t", "fix", "ok"} from NAV-STATUS or NAV-SOL
        self.ubx_sol_t = None
        self.tpv = None              # {"t", "problem", "sv", "acc"} of the last TPV
        self.sky_used = None
        self.sky_t = None
        self.nmea_fake_t = None      # when the raw NMEA last held a placeholder
        self.source = None           # "ubx" or "tpv": what the steady fixes come from
        self._said = None            # the last state logged
        self.since = None            # when the connection was made

    # --------------------------------------------------------- the stream

    def start(self):
        threading.Thread(target=self.run, name="gps", daemon=True).start()
        return self

    def run(self):
        while not self.stop.is_set():
            try:
                with socket.create_connection(self.addr, timeout=10) as s:
                    s.sendall(b'?WATCH={"enable":true,"json":true,"raw":2};\n')
                    with self.lock:
                        self.connected, self.conn_error, self.buf = True, None, b""
                        self.heard = self.clock()   # a grace period before "sending nothing"
                        self.since = self.heard
                    self._say()
                    s.settimeout(GAP_S)
                    while not self.stop.is_set():
                        data = s.recv(8192)
                        if not data:
                            raise OSError("gpsd closed the connection")
                        self.feed(data)
            except OSError as e:
                with self.lock:
                    self.connected = False
                    self.conn_error = str(e) or e.__class__.__name__
                self._say()
            self.stop.wait(self.RETRY_S)

    def feed(self, data):
        """Bytes from gpsd: UBX frames, NMEA sentences and JSON lines, mixed."""
        with self.lock:
            self.heard = self.clock()
            self.buf = (self.buf + data)[-self.BUF_MAX:]
            while True:
                if not self._next():
                    break
        self._say()

    def _next(self):
        """Take one item from the front of the buffer; False when it needs more."""
        b = self.buf
        cands = [i for i in (b.find(b"\xb5\x62"), b.find(b"{"), b.find(b"$")) if i >= 0]
        if not cands:
            self.buf = b""
            return False
        i = min(cands)
        b = self.buf = b[i:]
        if b.startswith(b"\xb5\x62"):
            if len(b) < 6:
                return False
            n = struct.unpack_from("<H", b, 4)[0]
            if n > 1024:
                self.buf = b[2:]
                return True
            if len(b) < 8 + n:
                return False
            if ubx_checksum(b[2:6 + n]) != b[6 + n:8 + n]:
                self.buf = b[2:]
                return True
            self.buf = b[8 + n:]
            self._ubx(b[2], b[3], b[6:6 + n])
            return True
        j = b.find(b"\n")
        if j < 0:
            if len(b) > 4096:
                self.buf = b[1:]
                return True
            return False
        line, self.buf = b[:j].strip(), b[j + 1:]
        if line.startswith(b"{"):
            try:
                doc = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                return True
            if isinstance(doc, dict):
                self._json(doc)
        else:
            self._nmea(line.decode("ascii", "replace"))
        return True

    # ---------------------------------------------------------- messages

    def _ubx(self, cls, mid, p):
        now = self.clock()
        if cls != 0x01:
            return
        if mid == 0x03:
            st = parse_nav_status(p)
            if st:
                self.ubx_status = dict(st, t=now)
        elif mid == 0x06:
            sol = parse_nav_sol(p)
            if not sol:
                return
            self.ubx_sol_t = now
            self.ubx_status = {"t": now, "fix": sol["fix"], "ok": sol["ok"]}
            lat, lon = ecef_to_geodetic(*sol["ecef"]) if any(sol["ecef"]) else (0.0, 0.0)
            self._position("ubx", lat, lon, ubx_problem(sol, lat, lon), sol["sv"], sol["acc_m"], now)
        elif mid == 0x02:
            if self.ubx_sol_t is not None and now - self.ubx_sol_t < FRESH_S:
                return  # NAV-SOL is here: one position per epoch
            pos = parse_nav_posllh(p)
            st = self.ubx_status
            if not pos or not st or now - st["t"] > FRESH_S:
                return
            sv = self.sky_used if self.sky_t is not None and now - self.sky_t < FRESH_S else None
            sol = {"fix": st["fix"], "ok": st["ok"], "sv": sv, "acc_m": pos["acc_m"]}
            self._position("ubx", pos["lat"], pos["lon"], ubx_problem(sol, pos["lat"], pos["lon"]),
                           sv, pos["acc_m"], now)

    def _json(self, doc):
        now = self.clock()
        kind = doc.get("class")
        if kind == "DEVICES":
            self.devices = len(doc.get("devices") or [])
        elif kind == "SKY":
            used = doc.get("uSat")
            if used is None and isinstance(doc.get("satellites"), list):
                used = sum(1 for s in doc["satellites"] if isinstance(s, dict) and s.get("used"))
            self.sky_used, self.sky_t = used, now
        elif kind == "TPV":
            sv = self.sky_used if self.sky_t is not None and now - self.sky_t < 3 * FRESH_S else None
            fake = self.nmea_fake_t is not None and now - self.nmea_fake_t < FRESH_S
            problem = tpv_problem(doc, sv, fake)
            eph = doc.get("eph") if isinstance(doc.get("eph"), (int, float)) else None
            if doc.get("lat") is None or doc.get("lon") is None:
                self.tpv = {"t": now, "problem": problem, "sv": sv, "acc": eph}
                return
            self._position("tpv", doc["lat"], doc["lon"], problem, sv, eph, now)

    def _nmea(self, line):
        j = line.find("$")
        if j < 0:
            return
        if nmea_placeholder(line[j:]):
            self.nmea_fake_t = self.clock()

    def _position(self, src, lat, lon, problem, sv, acc, now):
        rec = {"t": now, "problem": problem, "sv": sv, "acc": acc}
        if src == "ubx":
            self.ubx = rec
        else:
            self.tpv = rec
            if self._ubx_good(now):
                return  # the binary fix wins
        if problem:
            return
        self.source = src
        g = self.steady.offer(lat, lon, now)
        if g:
            self._accepted = (g, "gps/" + src)
            self.log("gps: accepted %s from the receiver's %s (%s satellites, about %d m)"
                     % (g, "binary fix" if src == "ubx" else "NMEA fix via gpsd",
                        sv if sv is not None else "?", round(acc or 0)))
            try:
                self.on_accept(g, "gps/" + src)
            except Exception as e:  # never let a save kill the reader
                self.log("gps: can't save the accepted grid: %s" % e)

    def _ubx_good(self, now):
        u = self.ubx
        return bool(u and not u["problem"] and now - u["t"] < FRESH_S)

    # ------------------------------------------------------------- state

    def live(self):
        """(grid, "gps/ubx" | "gps/tpv") for an accepted, current fix, else None."""
        with self.lock:
            now = self.clock()
            if self.steady.accepted and self.steady.live(now) and self.source:
                return self.steady.accepted, "gps/" + self.source
            return None

    def status(self):
        """{"state", "reason", "satellites", "accuracy_m", "source", "gpsd"}."""
        with self.lock:
            return self._status(self.clock())

    def _status(self, now):
        out = {"state": None, "reason": None, "satellites": None, "accuracy_m": None,
               "source": None, "gpsd": self.gpsd}
        if not self.connected:
            out["state"] = "unavailable"
            out["reason"] = "can't reach gpsd at %s%s" % (
                self.gpsd, ": " + self.conn_error if self.conn_error else "")
            return out
        if self.devices == 0:
            out["state"], out["reason"] = "unavailable", "gpsd has no GPS receiver"
            return out
        u = self.ubx if self.ubx and now - self.ubx["t"] < FRESH_S else None
        p = self.tpv if self.tpv and now - self.tpv["t"] < FRESH_S else None
        fake_nmea = (self.nmea_fake_t is not None and now - self.nmea_fake_t < FRESH_S) or \
            bool(p and p["problem"] in ("placeholder", "no satellites"))
        good = u if u and not u["problem"] else p if p and not p["problem"] else None
        if good and self.steady.live(now):
            out.update(state="fix", source=self.source, satellites=good["sv"],
                       accuracy_m=int(round(good["acc"])) if good["acc"] is not None else None)
            notes = []
            if good is u and fake_nmea:
                notes.append("GPS position rejected: the receiver's NMEA reports a placeholder "
                             "position; using its binary fix instead")
            left = self.steady.settling(now)
            if self.steady.accepted is None or left is not None:
                notes.append("GPS has a fix; it's used once it holds one grid square for %d "
                             "minutes" % round(self.steady.hold / 60))
            out["reason"] = ". ".join(notes) or "GPS fix"
            return out
        bad = u or p
        if bad and bad["problem"] != "no fix":
            out.update(state="rejected", satellites=bad["sv"],
                       accuracy_m=int(round(bad["acc"])) if bad["acc"] is not None else None,
                       source="ubx" if bad is u else "tpv")
            out["reason"] = "GPS position rejected: " + _problem_words(bad["problem"], bad["sv"],
                                                                     bad["acc"])
            return out
        if self.heard is None or now - self.heard > GAP_S:
            out["state"], out["reason"] = "unavailable", "gpsd is sending nothing"
            return out
        out["state"] = "no fix"
        out["satellites"] = self.sky_used if self.sky_t and now - self.sky_t < 3 * FRESH_S else None
        out["reason"] = "GPS has no fix" + (" (the receiver's NMEA reports a placeholder position)"
                                            if fake_nmea else "")
        return out

    def _say(self):
        """Log the state when it changes (never a position)."""
        with self.lock:
            now = self.clock()
            if self.connected and self.since is not None and now - self.since < 2 * FRESH_S:
                return  # the first epochs: let every source be heard before saying anything
            st = self._status(now)
        key = (st["state"], st["reason"].split(":")[0] if st["reason"] else None)
        if key != self._said:
            self._said = key
            self.log("gps: %s: %s" % (st["state"], st["reason"]))


# -------------------------------------------------------------- the grid

class SavedGrid:
    """The last accepted GPS grid, saved atomically to path."""

    def __init__(self, path=None, log=None):
        self.path = path or state_path()
        self.log = log or (lambda m: None)
        self.problem = None
        self.doc = self._read()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            return self._bad("can't read %s: %s" % (self.path, e))
        if not isinstance(doc, dict) or not isinstance(doc.get("grid"), str) or \
                not GRID6.match(doc["grid"]) or doc.get("source") not in SOURCES or \
                not isinstance(doc.get("accepted"), str):
            return self._bad("%s isn't a saved GPS grid" % self.path)
        return {"grid": doc["grid"], "source": doc["source"], "accepted": doc["accepted"]}

    def _bad(self, why):
        self.problem = why + "; ignoring it"
        self.log("location: " + self.problem)
        return None

    def save(self, grid, source, when=None):
        if self.doc and self.doc["grid"] == grid and self.doc["source"] == source:
            return
        doc = {"grid": grid, "source": source,
               "accepted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)
        self.doc, self.problem = doc, None


GPS_SHORT = {"no fix": "GPS has no fix", "rejected": "GPS position rejected",
             "off": "GPS is off", "unavailable": "GPS unavailable"}


class Location:
    """The station's grid: live GPS > saved GPS > station file > none.

    default() returns the station file's grid (or None); gps is a
    GpsLocation, or None when the GPS is off.
    """

    def __init__(self, default, saved=None, gps=None, off_reason="GPS is off"):
        self.default = default
        self.saved = saved if saved is not None else SavedGrid()
        self.gps = gps
        self.off_reason = off_reason

    def attach(self, gps):
        self.gps = gps
        gps.on_accept = lambda g, s: self.saved.save(g, s)

    def grid(self):
        """(grid, source): source "gps", "gps-saved", "station" or None."""
        live = self.gps.live() if self.gps else None
        if live:
            return live[0], "gps"
        if self.saved.doc:
            return self.saved.doc["grid"], "gps-saved"
        d = self.default()
        return (d, "station") if d else (None, None)

    def gps_status(self):
        if self.gps is None:
            return {"state": "off", "reason": self.off_reason, "satellites": None,
                    "accuracy_m": None, "source": None, "gpsd": None}
        return self.gps.status()

    def status(self):
        """For /api/v1/status's station: grid, grid_source, grid_reason,
        default_grid, saved_grid and gps. Never a position."""
        grid, src = self.grid()
        gps = self.gps_status()
        default = self.default()
        saved = self.saved.doc
        if gps["state"] == "fix" and src != "gps":
            why = "GPS is settling"
        else:
            why = GPS_SHORT.get(gps["state"], "GPS")
        if src == "gps":
            n = gps.get("satellites")
            text = "%s from GPS%s" % (grid, " (%d satellites)" % n if n else "")
        elif src == "gps-saved":
            text = "%s from the last GPS fix: %s" % (grid, why)
        elif src == "station":
            text = "%s from station config: %s" % (grid, why)
        else:
            text = "No grid: %s, and no grid in the station file" % why
        if src in ("gps", "gps-saved") and default and default != grid:
            text += "; station config says %s" % default
        return {"grid": grid, "grid_source": src, "grid_reason": text,
                "default_grid": default, "saved_grid": saved["grid"] if saved else None,
                "saved_problem": self.saved.problem, "gps": gps}
