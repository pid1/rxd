"""Tests for install.py: into a temp prefix, run twice (nothing changes the
second time), upgraded (a new directory, current/ switched, old ones pruned),
and the data steps against local files only. No test touches the network:
every URL is file://, and http(s)_proxy points at a dead port in case one
slipped through.

    python3 -m unittest discover tests
"""
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402  rxd/ and clients/tui/ on sys.path
import rxd  # noqa: E402
from test_callsign_db import good_db  # noqa: E402

DEAD = "http://127.0.0.1:9"

# Stands in for js8.py's two JSC commands, with a pin the test chooses.
STUB_JS8 = r'''#!/usr/bin/env python3
import hashlib, os, sys
URL, SHA = os.environ["STUB_JSC_URL"], os.environ["STUB_JSC_SHA"]
DEST = os.path.join(os.environ["HOME"], ".local", "share", "rxd", "js8", "jsc_map.txt")
if sys.argv[1] == "--jsc-pin":
    try:
        have = open(DEST + ".sha256").read().split()[0]
    except OSError:
        have = None
    print(URL); print(SHA); print("installed" if have == SHA else "missing")
elif sys.argv[1] == "--jsc-import-source":
    data = open(sys.argv[2], "rb").read()
    if hashlib.sha256(data).hexdigest() != SHA:
        sys.exit(1)
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    open(DEST, "w").write(data.decode())
    open(DEST + ".sha256", "w").write(SHA + "  jsc_map.cpp\n")
'''


def copy_source(dest):
    """What a release tarball holds, enough for install.py."""
    for d in ("rxd", "clients", "completions"):
        shutil.copytree(os.path.join(_paths.ROOT, d), os.path.join(dest, d),
                        ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(os.path.join(_paths.ROOT, "install.py"), dest)
    return dest


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rxd-install-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src = copy_source(os.path.join(self.dir, "rxd-src"))
        self.prefix = os.path.join(self.dir, "prefix")
        self.units = os.path.join(self.dir, "units")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, http_proxy=DEAD, https_proxy=DEAD,
                        HTTP_PROXY=DEAD, HTTPS_PROXY=DEAD, RXD_STATION=_paths.STATION)
        self.env.pop("RXD_CALLSIGNS_URL", None)

    def install(self, *args, ok=True):
        out = subprocess.run([sys.executable, os.path.join(self.src, "install.py"),
                              "--prefix", self.prefix, "--unit-dir", self.units] + list(args),
                             capture_output=True, text=True, env=self.env, timeout=120)
        if ok:
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def lib(self, *parts):
        return os.path.join(self.prefix, "lib", "rxd", *parts)

    def versions(self):
        return sorted(d for d in os.listdir(self.lib()) if d[0].isdigit())


class Installer(Base):
    def test_installs_the_layout(self):
        out = self.install("--no-data")
        name = os.readlink(self.lib("current"))
        self.assertRegex(name, r"^%s-[0-9a-f]{12}$" % rxd.VERSION.replace(".", r"\."))
        for f in ("rxd.py", "js8.py", "callsign_db.py", "radio.py", "station.py", "location.py", "hd.py", "rxd_tui.py",
                  "web/index.html"):
            self.assertTrue(os.path.isfile(self.lib(name, f)), f)
        self.assertTrue(os.access(self.lib(name, "rxd.py"), os.X_OK))
        for cmd, target in (("rxd", "rxd.py"), ("radio", "radio.py"), ("rxd-tui", "rxd_tui.py")):
            self.assertEqual(os.readlink(os.path.join(self.prefix, "bin", cmd)),
                             os.path.join("..", "lib", "rxd", "current", target))
        with open(os.path.join(self.units, "rxd@.service")) as fh:
            unit = fh.read()
        self.assertIn("ExecStart=%s/bin/rxd %%i" % self.prefix, unit)
        self.assertIn("EnvironmentFile=-/etc/rxd/rxd.env", unit)
        self.assertTrue(os.path.isfile(os.path.join(self.prefix, "share", "bash-completion",
                                                    "completions", "radio")))
        self.assertIn("rxd %s is installed" % rxd.VERSION, out.stdout)

    def test_the_installed_commands_run_through_their_links(self):
        self.install("--no-data")
        bin_ = os.path.join(self.prefix, "bin")
        out = subprocess.run([sys.executable, os.path.join(bin_, "rxd"), "--version"],
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(out.stdout.strip(), "rxd " + rxd.VERSION, out.stderr)
        # radio finds station.py beside its real path, and reads the station file
        out = subprocess.run([sys.executable, os.path.join(bin_, "radio"), "--names"],
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("dcara", out.stdout.split())
        out = subprocess.run([sys.executable, os.path.join(bin_, "rxd-tui"), "--help"],
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        # rxd serves the web UI installed beside it
        code = ("import sys; sys.path.insert(0, %r); import rxd; print(rxd.web_page() is not None)"
                % self.lib("current"))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=dict(self.env, RXD_WEB=""), timeout=60)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)

    def test_second_run_changes_nothing(self):
        self.install("--no-data")
        stamp = {}
        for root, dirs, files in os.walk(self.dir):
            for f in dirs + files:
                p = os.path.join(root, f)
                stamp[p] = os.lstat(p).st_mtime_ns
        out = self.install("--no-data")
        self.assertNotIn("installed /", out.stdout)
        self.assertNotIn("wrote", out.stdout)
        self.assertNotIn("linked", out.stdout)
        for p, t in stamp.items():
            if p.startswith(self.prefix) or p.startswith(self.units):
                self.assertEqual(os.lstat(p).st_mtime_ns, t, p)

    def test_upgrade_switches_current_and_prunes(self):
        self.install("--no-data")
        first = os.readlink(self.lib("current"))
        for n in (1, 2):
            with open(os.path.join(self.src, "rxd", "station.py"), "a") as fh:
                fh.write("# change %d\n" % n)
            self.install("--no-data")
        third = os.readlink(self.lib("current"))
        self.assertNotEqual(first, third)
        # the current one and the one before it; the first is gone
        self.assertEqual(len(self.versions()), 2)
        self.assertNotIn(first, self.versions())
        self.assertIn(third, self.versions())
        self.assertFalse([d for d in os.listdir(self.lib()) if d.startswith(".stage-")])

    def test_dry_run_writes_nothing(self):
        out = self.install("--no-data", "--dry-run")
        self.assertIn("[dry-run]", out.stdout)
        self.assertFalse(os.path.exists(self.prefix))
        self.assertFalse(os.path.exists(self.units))

    def test_the_station_file_is_left_alone(self):
        etc = os.path.join(self.dir, "etc")
        self.install("--no-data", "--sysconfdir", etc)
        self.assertFalse(os.path.exists(etc))
        with open(os.path.join(self.units, "rxd@.service")) as fh:
            self.assertIn("EnvironmentFile=-%s/rxd/rxd.env" % etc, fh.read())


class DataSteps(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.src, "rxd", "js8.py"), "w") as fh:
            fh.write(STUB_JS8)
        self.jsc = os.path.join(self.dir, "jsc_map.cpp")
        with open(self.jsc, "w") as fh:
            fh.write("pretend table\n")
        with open(self.jsc, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        self.env.update(STUB_JSC_URL="file://" + self.jsc, STUB_JSC_SHA=sha)
        rel = os.path.join(self.dir, "release")
        os.makedirs(rel)
        db = os.path.join(self.dir, "built.sqlite")
        good_db(db)
        with open(db, "rb") as fh:
            gz = gzip.compress(fh.read())
        with open(os.path.join(rel, "callsigns.sqlite.gz"), "wb") as fh:
            fh.write(gz)
        with open(os.path.join(rel, "callsigns.sqlite.gz.sha256"), "w") as fh:
            fh.write(hashlib.sha256(gz).hexdigest() + "  callsigns.sqlite.gz\n")
        self.release = "file://" + rel + "/"
        self.table = os.path.join(self.home, ".local", "share", "rxd", "js8", "jsc_map.txt")
        self.db = os.path.join(self.home, ".local", "share", "rxd", "callsigns.sqlite")

    def test_fetches_both_then_finds_them_current(self):
        out = self.install("--callsigns-url", self.release)
        self.assertIn("installed JS8's JSC word table", out.stdout)
        self.assertIn("callsign database up to date", out.stdout)
        with open(self.table) as fh:
            self.assertEqual(fh.read(), "pretend table\n")
        self.assertEqual(rxd.CallBook(self.db).lookup("KA1PID")["grid"], "EN52lk")
        out = self.install("--callsigns-url", self.release)
        self.assertIn("JS8's JSC word table is current", out.stdout)
        self.assertNotIn("installed JS8's", out.stdout)

    def test_a_local_source_and_a_wrong_one(self):
        self.env["STUB_JSC_URL"] = DEAD + "/jsc_map.cpp"   # never used with --jsc-source
        out = self.install("--no-data")
        self.assertFalse(os.path.exists(self.table))
        out = self.install("--jsc-source", self.jsc, "--callsigns-url", self.release)
        self.assertIn("installed JS8's JSC word table", out.stdout)
        os.remove(self.table + ".sha256")
        bad = os.path.join(self.dir, "other.cpp")
        with open(bad, "w") as fh:
            fh.write("something else\n")
        out = self.install("--jsc-source", bad, "--callsigns-url", self.release)
        self.assertIn("not the pinned one", out.stderr)
        self.assertEqual(out.returncode, 0)                 # a warning, not a failure

    def test_offline_keeps_what_is_there(self):
        self.install("--callsigns-url", self.release)
        before = os.path.getmtime(self.db)
        os.remove(self.table + ".sha256")                   # make the table look stale
        self.env["STUB_JSC_URL"] = DEAD + "/jsc_map.cpp"
        out = self.install("--callsigns-url", DEAD + "/release/")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("could not fetch JS8's JSC word table", out.stderr)
        self.assertIn("could not update the callsign database", out.stderr)
        self.assertTrue(os.path.exists(self.table))
        self.assertEqual(os.path.getmtime(self.db), before)


if __name__ == "__main__":
    unittest.main()
