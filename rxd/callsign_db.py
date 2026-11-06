#!/usr/bin/env python3
"""callsign_db: an offline callsign -> Maidenhead grid database for rxd's map.

  callsign_db.py update [--db PATH] [--work DIR] [--force] [--summary FILE]
                                       build it (the weekly GitHub workflow,
                                       .github/workflows/callsigns.yml)
  callsign_db.py validate DB [--previous DB]   the gate before publishing
  callsign_db.py install [--db PATH]   fetch the published build (the Pi:
                                       setup.sh runs this; nothing else does)
  callsign_db.py lookup CALL [...]     what the database says (via rxd's own lookup)
  callsign_db.py info                  sources, counts and build time

The build runs on GitHub, not the Pi: the workflow publishes the database as
the release asset data-callsigns/callsigns.sqlite.gz with a .sha256 beside it,
and the Pi only downloads that.

JS8 stations often never send a grid. This builds a local database so rxd can
still place them, with nothing looked up online while rxd runs:

  US calls  FCC ULS amateur licences (public domain), the weekly l_amat.zip:
            HD.dat for active licences, EN.dat for the licence address. Each
            address is geocoded once, at build time, by the US Census Bureau's
            batch geocoder, and only the 6-character grid is kept ("addr").
            Addresses it can't place (PO boxes, rural routes) fall back to the
            grid of the ZIP code's centroid from the Census Gazetteer ZCTA file
            (public domain), flagged "zip".
  Others    callsign prefix -> DXCC entity centre from AD1C's Big CTY cty.dat
            (country-files.com, MIT licence), flagged "country".

Privacy: the database keeps only call, grid, precision and state or entity.
No names, street addresses or FRNs. The geocode cache keeps a hash of each
address (to notice when one changes) and its grid, never the address. The
raw downloads are deleted once a build has used them completely.

update is idempotent and polite: each source is fetched only when its ETag or
Last-Modified changed, geocoding is resumable (results are cached per batch),
and the new database is written to a temporary file and renamed over the old
one, so a failure anywhere keeps the old database.

Standard library only. Python 3.9+.
"""
import argparse
import csv
import fcntl
import hashlib
import hmac
import io
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

FCC_URL = "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"
ZCTA_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
            "2025_Gazetteer/2025_Gaz_zcta_national.zip")
CTY_URL = "https://www.country-files.com/bigcty/cty.dat"
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
USER_AGENT = "rxd-callsign-db/1 (offline map for an amateur receiver)"

DB_PATH = os.path.expanduser(os.environ.get("RXD_CALLSIGNS", "~/.local/share/rxd/callsigns.sqlite"))
WORK_DIR = os.path.expanduser(os.environ.get("RXD_CALLSIGNS_WORK", "~/.cache/rxd/callsign-db"))
BATCH = 10000          # the Census geocoder's limit per batch
PUBLISH_EVERY = 20     # batches between interim databases while geocoding
SCHEMA = 1


def log(msg):
    print("callsign_db: " + msg, file=sys.stderr, flush=True)


def latlon_grid(lat, lon, n=6):
    """(lat, lon) -> Maidenhead locator of 4 or 6 characters, e.g. EM13lc."""
    lon = min(max(lon + 180.0, 0.0), 359.999999)
    lat = min(max(lat + 90.0, 0.0), 179.999999)
    g = chr(65 + int(lon // 20)) + chr(65 + int(lat // 10))
    g += str(int(lon % 20 // 2)) + str(int(lat % 10))
    if n >= 6:
        g += chr(97 + int(lon % 2 * 12)) + chr(97 + int(lat % 1 * 24))
    return g


# ------------------------------------------------------------------ parsers

def parse_hd(lines):
    """HD.dat -> {unique_system_identifier: call} for active amateur licences."""
    active = {}
    for line in lines:
        f = line.split("|")
        # 1 HD, 2 usid, 5 call sign, 6 licence status, 7 radio service
        if len(f) < 7 or f[0] != "HD" or f[5] != "A" or f[6] not in ("HA", "HV"):
            continue
        call = f[4].strip().upper()
        if call:
            active[f[1]] = call
    return active


def parse_en(lines, active):
    """EN.dat -> {call: (street, city, state, zip5)} for the active licences.

    Only these four address fields are read; the name, FRN, phone, email and
    the rest of each line are never kept."""
    out = {}
    for line in lines:
        f = line.split("|")
        # 2 usid, 5 call, 6 entity type, 16 street, 17 city, 18 state, 19 zip
        if len(f) < 19 or f[0] != "EN":
            continue
        call = active.get(f[1])
        if not call or (call in out and f[5] != "L"):
            continue
        z = re.sub(r"[^0-9]", "", f[18])[:5]
        out[call] = (f[15].strip(), f[16].strip(), f[17].strip().upper(), z if len(z) == 5 else "")
    return out


def parse_zcta(lines):
    """Census Gazetteer ZCTA file (tab or | separated) -> {zip5: (lat, lon)}."""
    it = iter(lines)
    head = next(it, "")
    sep = "|" if "|" in head else "\t"
    cols = [c.strip().upper() for c in head.split(sep)]
    try:
        gi, la, lo = cols.index("GEOID"), cols.index("INTPTLAT"), cols.index("INTPTLONG")
    except ValueError:
        raise ValueError("ZCTA file has no GEOID/INTPTLAT/INTPTLONG columns")
    out = {}
    for line in it:
        f = line.rstrip("\r\n").split(sep)
        try:
            out[f[gi].strip()[-5:]] = (float(f[la]), float(f[lo]))
        except (IndexError, ValueError):
            continue
    return out


CTY_ALIAS = re.compile(r"^(=?)([A-Z0-9/]+)(.*)$")


def parse_cty(text):
    """cty.dat -> [(key, grid4, entity)]; key is a prefix, or '=CALL' for an exact call.

    Longitudes in cty.dat are + for West. Entries keep the file's order and
    the first of any duplicate wins, as the format asks."""
    out, seen = [], set()
    entity = None
    buf = ""
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace() and raw.count(":") >= 8:
            f = [x.strip() for x in raw.split(":")]
            entity = (f[0], float(f[4]), -float(f[5]))
            buf = ""
            continue
        if entity is None:
            continue
        buf += raw.strip()
        if not buf.endswith(";"):
            continue
        for alias in buf.rstrip(";").split(","):
            m = CTY_ALIAS.match(alias.strip())
            if not m:
                continue
            exact, key, rest = m[1], m[2], m[3]
            lat, lon = entity[1], entity[2]
            ll = re.search(r"<([-0-9.]+)/([-0-9.]+)>", rest)
            if ll:
                lat, lon = float(ll[1]), -float(ll[2])
            k = ("=" if exact else "") + key
            if k not in seen:
                seen.add(k)
                out.append((k, latlon_grid(lat, lon, 4), entity[0]))
        buf = ""
    return out


def addr_hash(street, city, state, z, key=None):
    """A short fingerprint of an address, to notice when a licence's address
    changes. With a key (the workflow's secret) it's an HMAC, so the geocode
    cache it publishes can't be used to test whether an address is in it."""
    norm = "|".join(re.sub(r"\s+", " ", x.strip().upper()) for x in (street, city, state, z)).encode()
    if key:
        return hmac.new(key.encode(), norm, hashlib.sha256).hexdigest()[:24]
    return hashlib.sha256(norm).hexdigest()[:20]


def zip_lines(path, member):
    zf = zipfile.ZipFile(path)
    names = {n.rsplit("/", 1)[-1].upper(): n for n in zf.namelist()}
    if member.upper() not in names:
        raise ValueError("%s has no %s" % (path, member))
    return io.TextIOWrapper(zf.open(names[member.upper()]), encoding="latin-1", newline="")


# ------------------------------------------------------------------ geocoder

class CensusGeocoder:
    """The Census Bureau's batch address geocoder: up to 10,000 rows a request."""

    def __init__(self, url=GEOCODER_URL, timeout=900, tries=3, pause=2.0):
        self.url, self.timeout, self.tries, self.pause = url, timeout, tries, pause

    def __call__(self, rows):
        """rows: [(id, street, city, state, zip)] -> {id: (lat, lon) or None}."""
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for r in rows:
            w.writerow(r)
        boundary = uuid.uuid4().hex
        body = ("--%s\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\nPublic_AR_Current\r\n"
                "--%s\r\nContent-Disposition: form-data; name=\"addressFile\"; filename=\"a.csv\"\r\n"
                "Content-Type: text/csv\r\n\r\n" % (boundary, boundary)).encode() + \
            buf.getvalue().encode("utf-8") + ("\r\n--%s--\r\n" % boundary).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary, "User-Agent": USER_AGENT,
            "Accept": "*/*"})
        err = None
        for attempt in range(self.tries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return parse_geocoder(r.read().decode("utf-8", "replace"))
            except (urllib.error.URLError, OSError, ValueError) as e:
                err = e
                time.sleep(self.pause * 4 ** attempt)
        raise RuntimeError("geocoder failed: %s" % err)


def parse_geocoder(text):
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            continue
        ll = None
        if row[2] == "Match" and len(row) >= 6:
            try:
                lon, lat = (float(x) for x in row[5].split(","))
                ll = (lat, lon)
            except ValueError:
                pass
        out[row[0]] = ll
    return out


# ------------------------------------------------------------------ downloads

def fetch(url, dest, meta, key, force=False, reuse=False):
    """Download url to dest unless unchanged since meta[key]; True if there's new data.

    reuse: a copy kept from a build that didn't finish is used as it is."""
    if reuse and os.path.exists(dest):
        return True
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}  # data.fcc.gov refuses a request without Accept
    old = meta.get(key + "_etag"), meta.get(key + "_modified")
    if not force and old[0]:
        headers["If-None-Match"] = old[0]
    if not force and old[1]:
        headers["If-Modified-Since"] = old[1]
    req = urllib.request.Request(url, headers=headers)
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
            want = int(r.headers.get("Content-Length") or -1)
            n = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                fh.write(b)
                n += len(b)
            if want >= 0 and n != want:
                raise OSError("%s: got %d of %d bytes" % (url, n, want))
            etag, mod = r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return False
        raise
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, dest)
    meta[key + "_etag_new"], meta[key + "_modified_new"] = etag or "", mod or ""
    return True


def read_meta(path):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            return dict(con.execute("SELECT key, value FROM meta"))
        finally:
            con.close()
    except sqlite3.Error:
        return {}


# ------------------------------------------------------------------ build

def open_cache(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS geo (ahash TEXT PRIMARY KEY, grid TEXT NOT NULL) WITHOUT ROWID")
    return con


def write_db(path, calls, prefixes, meta):
    """Write the database to path + '.tmp', then rename it over path."""
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    try:
        con.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
            CREATE TABLE calls (call TEXT PRIMARY KEY, grid TEXT NOT NULL, prec TEXT NOT NULL,
                                region TEXT) WITHOUT ROWID;
            CREATE TABLE prefixes (prefix TEXT PRIMARY KEY, grid TEXT NOT NULL, entity TEXT) WITHOUT ROWID;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        con.executemany("INSERT INTO calls VALUES (?,?,?,?)", calls)
        con.executemany("INSERT OR IGNORE INTO prefixes VALUES (?,?,?)", prefixes)
        con.executemany("INSERT INTO meta VALUES (?,?)", sorted((k, str(v)) for k, v in meta.items()))
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def copy_table(path, table):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            return list(con.execute("SELECT * FROM " + table))
        finally:
            con.close()
    except sqlite3.Error:
        return None


def update(db=DB_PATH, work=WORK_DIR, geocoder=None, geocode_max=None, urls=None,
           min_calls=100000, min_prefixes=300, min_zips=30000, clock=time.time, force=False):
    """Bring the database up to date. Returns a summary dict; raises on failure
    (the old database is untouched then)."""
    urls = dict({"fcc": FCC_URL, "zcta": ZCTA_URL, "cty": CTY_URL}, **(urls or {}))
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    with open(os.path.join(work, "lock"), "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("another update is running")
        return _update(db, work, geocoder, geocode_max, urls, min_calls, min_prefixes, min_zips, clock,
                       force)


def _update(db, work, geocoder, geocode_max, urls, min_calls, min_prefixes, min_zips, clock, force):
    t0 = clock()
    old = read_meta(db) if os.path.exists(db) else {}
    meta = dict(old)
    pending = old.get("geocode_pending", "0") != "0"
    raw = {k: os.path.join(work, n) for k, n in
           (("fcc", "l_amat.zip"), ("zcta", "zcta.zip"), ("cty", "cty.dat"))}
    have_db = bool(old) and old.get("schema") == str(SCHEMA) and not force
    # only the FCC file is kept between runs, and only while geocoding is unfinished
    for k, p in raw.items():
        if os.path.exists(p) and not (k == "fcc" and pending and have_db):
            os.remove(p)
    got = {}
    got["cty"] = fetch(urls["cty"], raw["cty"], meta, "cty", force=not have_db)
    got["zcta"] = fetch(urls["zcta"], raw["zcta"], meta, "zcta", force=not have_db)
    # the calls table needs EN.dat whenever the FCC file, the ZIP table or
    # unfinished geocoding asks for a rebuild
    need_fcc = not have_db or pending or got["zcta"]
    got["fcc"] = fetch(urls["fcc"], raw["fcc"], meta, "fcc", force=need_fcc, reuse=pending and have_db)
    if got["fcc"] and not got["zcta"]:  # rebuilding the calls needs the ZIP table too
        got["zcta"] = fetch(urls["zcta"], raw["zcta"], meta, "zcta", force=True)
    if not any(got.values()):
        log("up to date (built %s)" % old.get("built", "?"))
        return {"changed": False, "calls": int(old.get("calls", 0))}

    # prefixes
    if got["cty"]:
        with open(raw["cty"], encoding="latin-1") as fh:
            prefixes = parse_cty(fh.read())
        if len(prefixes) < min_prefixes:
            raise ValueError("cty.dat has only %d prefixes" % len(prefixes))
    else:
        prefixes = copy_table(db, "prefixes")
    meta["prefixes"] = len(prefixes)

    if not got["fcc"]:
        calls = copy_table(db, "calls")
        stats = {}
    else:
        zcta = parse_zcta(zip_lines(raw["zcta"], next(
            n for n in zipfile.ZipFile(raw["zcta"]).namelist() if n.lower().endswith(".txt"))))
        if len(zcta) < min_zips:
            raise ValueError("ZCTA file has only %d ZIP codes" % len(zcta))
        t = clock()
        active = parse_hd(zip_lines(raw["fcc"], "HD.dat"))
        addrs = parse_en(zip_lines(raw["fcc"], "EN.dat"), active)
        del active
        if len(addrs) < min_calls:
            raise ValueError("only %d active licences with an address" % len(addrs))
        log("%d active licences read in %.0f s" % (len(addrs), clock() - t))
        cache = open_cache(os.path.join(work, "geocache.sqlite"))
        key = os.environ.get("RXD_CALLSIGN_KEY") or None
        hashes = {c: addr_hash(*a, key=key) for c, a in addrs.items() if a[0]}
        known = set(h for (h,) in cache.execute("SELECT ahash FROM geo"))
        todo = sorted({h: c for c, h in hashes.items() if h not in known}.items())
        if geocoder is None and geocode_max != 0:
            geocoder = CensusGeocoder()
        if geocode_max is not None:
            todo_now = todo[:geocode_max]
        else:
            todo_now = todo
        log("%d addresses cached, %d to geocode%s" % (
            len(known), len(todo), "" if len(todo_now) == len(todo) else
            " (%d this run)" % len(todo_now)))

        def assemble():
            geo = dict(cache.execute("SELECT ahash, grid FROM geo"))
            rows, n = [], {"addr": 0, "zip": 0, "none": 0}
            for c, (street, city, state, z) in addrs.items():
                if not CALL_RE.match(c):
                    n["none"] += 1
                    continue
                state = state if STATE_RE.match(state) else None
                g = geo.get(hashes.get(c)) if street else None
                if g:
                    rows.append((c, g, "addr", state or None))
                    n["addr"] += 1
                elif z in zcta:
                    rows.append((c, latlon_grid(*zcta[z]), "zip", state or None))
                    n["zip"] += 1
                else:
                    n["none"] += 1
            return sorted(rows), n

        def publish(left):
            rows, n = assemble()
            m = dict(meta, schema=SCHEMA, calls=len(rows), built=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())), geocode_pending=left,
                calls_addr=n["addr"], calls_zip=n["zip"], calls_unplaced=n["none"])
            write_db(db, rows, prefixes, finish_meta(m, got))
            return n

        publish(len(todo))
        tg = clock()
        done = 0
        for i in range(0, len(todo_now), BATCH):
            chunk = todo_now[i:i + BATCH]
            try:
                res = geocoder([(h, *addrs[c]) for h, c in chunk])
            except Exception as e:  # noqa: BLE001 - keep what's done; the next run carries on
                log("geocoding stopped: %s" % e)
                break
            cache.executemany("INSERT OR REPLACE INTO geo VALUES (?,?)",
                              [(h, latlon_grid(*res[h]) if res.get(h) else "") for h, _ in chunk])
            cache.commit()
            done += len(chunk)
            log("geocoded %d of %d (%.0f s)" % (done, len(todo_now), clock() - tg))
            if (i // BATCH + 1) % PUBLISH_EVERY == 0:
                publish(len(todo) - done)
        left = len(todo) - done
        stats = publish(left)
        stats["geocoded"], stats["geocode_s"] = done, round(clock() - tg)
        # forget cached addresses nobody has any more
        live = set(hashes.values())
        stale = [(h,) for (h,) in cache.execute("SELECT ahash FROM geo") if h not in live]
        cache.executemany("DELETE FROM geo WHERE ahash=?", stale)
        cache.commit()
        cache.close()
        if left:
            meta["geocode_pending"] = left
            log("%d addresses still to geocode: the next update carries on" % left)
            stats["pending"] = left
        calls = None
    if calls is not None:  # only the prefixes changed
        write_db(db, calls, prefixes, finish_meta(dict(meta, schema=SCHEMA, built=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))), got))
    # the raw downloads go once a build has used them completely
    for k, p in raw.items():
        if os.path.exists(p) and not (k == "fcc" and stats.get("pending")):
            os.remove(p)
    stats.update(changed=True, seconds=round(clock() - t0), size=os.path.getsize(db))
    log("built %s: %s" % (db, stats))
    return stats


def finish_meta(m, got):
    for k in ("fcc", "zcta", "cty"):
        if got.get(k) and (k + "_etag_new") in m:
            m[k + "_etag"], m[k + "_modified"] = m[k + "_etag_new"], m[k + "_modified_new"]
    return {k: v for k, v in m.items() if not k.endswith("_new")}


# ------------------------------------------------------------------ validation

CALL_RE = re.compile(r"^[A-Z0-9]{3,10}$")
GRID6_RE = re.compile(r"^[A-R]{2}[0-9]{2}[a-x]{2}$")
GRID4_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")
STATE_RE = re.compile(r"^[A-Z]{2}$")
PREFIX_RE = re.compile(r"^=?[A-Z0-9/]{1,20}$")
META_KEYS = re.compile(r"^(schema|calls|calls_addr|calls_zip|calls_unplaced|prefixes|built|"
                       r"geocode_pending|(fcc|zcta|cty)_(etag|modified))$")
SPOTS = {"KA1PID": "EM13", "W1AW": "FN31"}


def validate(path, previous=None, min_calls=500000, max_calls=3000000, min_prefixes=300,
             spots=SPOTS, min_addr_share=0.6):
    """Problems with a built database, as a list of strings (empty when it's good).

    This is the gate before publishing: the schema is exactly what rxd reads,
    every value has the shape it should (so nothing else, such as a name or
    an address, can have slipped in), the counts are sane and close to the
    previous build's, and some known calls are where they should be."""
    bad = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        con.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as e:
        return ["not a readable database: %s" % e]
    try:
        tables = {n: [c[1] for c in con.execute("PRAGMA table_info(%s)" % n)]
                  for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        want = {"calls": ["call", "grid", "prec", "region"], "prefixes": ["prefix", "grid", "entity"],
                "meta": ["key", "value"]}
        if tables != want:
            return ["schema is %r, want %r" % (tables, want)]
        if con.execute("SELECT count(*) FROM sqlite_master WHERE type NOT IN ('table', 'index')").fetchone()[0]:
            bad.append("unexpected views or triggers")
        n = {"addr": 0, "zip": 0}
        for call, grid, prec, region in con.execute("SELECT * FROM calls"):
            if (not CALL_RE.match(call or "") or prec not in n or not GRID6_RE.match(grid or "")
                    or not (region is None or STATE_RE.match(region))):
                bad.append("bad calls row: %r" % (call,))
                if len(bad) > 20:
                    break
                continue
            n[prec] += 1
        calls = n["addr"] + n["zip"]
        entities = set()
        for prefix, grid, entity in con.execute("SELECT * FROM prefixes"):
            if not PREFIX_RE.match(prefix or "") or not GRID4_RE.match(grid or "") or not entity:
                bad.append("bad prefixes row: %r" % (prefix,))
            entities.add(entity)
        if any(len(e) > 40 for e in entities):
            bad.append("an entity name is too long to be a country")
        meta = dict(con.execute("SELECT key, value FROM meta"))
        for k in meta:
            if not META_KEYS.match(k):
                bad.append("unexpected meta key %r" % k)
        if not min_calls <= calls <= max_calls:
            bad.append("%d calls, outside %d-%d" % (calls, min_calls, max_calls))
        np = con.execute("SELECT count(*) FROM prefixes").fetchone()[0]
        if np < min_prefixes:
            bad.append("only %d prefixes" % np)
        pending = int(meta.get("geocode_pending") or 0)
        if calls and not pending and n["addr"] / calls < min_addr_share:
            bad.append("only %.0f%% of calls geocoded" % (100 * n["addr"] / calls))
        for call, grid4 in (spots or {}).items():
            r = con.execute("SELECT grid FROM calls WHERE call=?", (call,)).fetchone()
            if not r or not r[0].startswith(grid4):
                bad.append("%s is at %s, want %s" % (call, r and r[0], grid4))
    finally:
        con.close()
    if previous and os.path.exists(previous):
        pm = read_meta(previous)
        try:
            pc = int(pm.get("calls", 0))
        except ValueError:
            pc = 0
        if pc and not 0.9 * pc <= calls <= 1.15 * pc:
            bad.append("%d calls, the previous build had %d" % (calls, pc))
    return bad


# ------------------------------------------------------------------ install (the Pi)

RELEASE_URL = os.environ.get("RXD_CALLSIGNS_URL",
                             "https://github.com/pid1/rx/releases/download/data-callsigns/")


def http_get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT, "Accept": "*/*"},
                                                   **(headers or {})))
    return urllib.request.urlopen(req, timeout=timeout)


def install(db=DB_PATH, base=RELEASE_URL, get=http_get):
    """Fetch the published database if it changed, check it, and swap it in.

    Returns "installed" or "unchanged"; raises on any failure, leaving the
    installed database as it was."""
    base = base.rstrip("/") + "/"
    side = db + ".sha256"
    state = {}
    try:
        with open(side) as fh:
            state = dict(line.rstrip("\n").split(" ", 1) for line in fh if " " in line)
    except (OSError, ValueError):
        pass
    have = os.path.exists(db)
    hdr = {"If-None-Match": state["etag"]} if have and state.get("etag") else {}
    try:
        with get(base + "callsigns.sqlite.gz.sha256", hdr) as r:
            text, etag = r.read(4096).decode("ascii", "replace"), r.headers.get("ETag") or ""
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "unchanged"
        raise
    m = re.match(r"^([0-9a-f]{64})\b", text.strip())
    if not m:
        raise ValueError("no sha256 in the checksum file")
    want = m[1]
    if have and state.get("sha256") == want:
        write_state(side, want, etag)
        return "unchanged"
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    gz, tmp = db + ".gz.part", db + ".part"
    try:
        h = hashlib.sha256()
        with get(base + "callsigns.sqlite.gz", None, 300) as r, open(gz, "wb") as fh:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                h.update(b)
                fh.write(b)
        if h.hexdigest() != want:
            raise ValueError("checksum mismatch: got %s, want %s" % (h.hexdigest(), want))
        import gzip
        with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
            while True:
                b = src.read(1 << 20)
                if not b:
                    break
                dst.write(b)
            dst.flush()
            os.fsync(dst.fileno())
        probs = validate(tmp, min_calls=1, max_calls=10 ** 8, min_prefixes=1, spots=None, min_addr_share=0)
        if probs:
            raise ValueError("the download isn't a good database: " + "; ".join(probs[:3]))
        os.replace(tmp, db)
    finally:
        for p in (gz, tmp):
            if os.path.exists(p):
                os.remove(p)
    write_state(side, want, etag)
    return "installed"


def write_state(path, sha, etag):
    tmp = path + ".part"
    with open(tmp, "w") as fh:
        fh.write("sha256 %s\netag %s\n" % (sha, etag))
    os.replace(tmp, path)


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="callsign_db", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update", help="download, geocode and build (the GitHub workflow)")
    u.add_argument("--db", default=DB_PATH)
    u.add_argument("--work", default=WORK_DIR)
    u.add_argument("--geocode-max", type=int, default=None,
                   help="geocode at most N addresses this run (the rest next time)")
    u.add_argument("--no-geocode", action="store_true", help="ZIP-centroid grids only, this run")
    u.add_argument("--force", action="store_true", help="rebuild even if no source changed")
    u.add_argument("--summary", help="write the build's summary here as JSON")
    v = sub.add_parser("validate", help="check a built database before publishing it")
    v.add_argument("db")
    v.add_argument("--previous", help="the last published build, to compare counts with")
    i = sub.add_parser("install", help="fetch the published database (the Pi; setup.sh runs this)")
    i.add_argument("--db", default=DB_PATH)
    i.add_argument("--url", default=RELEASE_URL, help="where the release assets are")
    lk = sub.add_parser("lookup", help="look calls up")
    lk.add_argument("calls", nargs="+")
    lk.add_argument("--db", default=DB_PATH)
    inf = sub.add_parser("info", help="what the database holds")
    inf.add_argument("--db", default=DB_PATH)
    a = ap.parse_args(argv)
    if a.cmd == "update":
        try:
            st = update(a.db, a.work, geocode_max=0 if a.no_geocode else a.geocode_max, force=a.force)
        except Exception as e:  # noqa: BLE001 - any failure keeps the old database
            log("update failed, the old database is unchanged: %s" % e)
            return 1
        if a.summary:
            import json
            with open(a.summary, "w") as fh:
                json.dump(st, fh)
        return 0
    if a.cmd == "validate":
        probs = validate(a.db, a.previous)
        for p in probs:
            log("invalid: " + p)
        if not probs:
            log("%s is good: %s" % (a.db, {k: v for k, v in read_meta(a.db).items()
                                          if k.startswith(("calls", "prefixes", "geocode", "built"))}))
        return 1 if probs else 0
    if a.cmd == "install":
        try:
            log("callsign database %s: %s" % (install(a.db, a.url), a.db))
        except Exception as e:  # noqa: BLE001 - any failure keeps the installed database
            log("couldn't update the callsign database, keeping the one installed: %s" % e)
            return 1
        return 0
    if a.cmd == "info":
        m = read_meta(a.db)
        if not m:
            log("no database at %s" % a.db)
            return 1
        for k in sorted(m):
            if not k.endswith(("_etag", "_modified")):
                print("%-16s %s" % (k, m[k]))
        print("%-16s %.1f MB" % ("size", os.path.getsize(a.db) / 1e6))
        return 0
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rxd  # the lookup rxd itself uses
    book = rxd.CallBook(a.db)
    for c in a.calls:
        print(c, book.lookup(c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
