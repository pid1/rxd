#!/usr/bin/env python3
"""install.py: install rxd from a release tarball (or a checkout).

    sudo python3 install.py --prefix /usr/local --user NAME

Run it from the unpacked rxd-X.Y.Z/ directory. It installs:

  PREFIX/lib/rxd/X.Y.Z-HASH/       rxd.py, js8.py, callsign_db.py, radio.py,
                                   station.py, location.py, hd.py, rxd_tui.py and
                                   web/index.html,
                                   in a directory named for their content
  PREFIX/lib/rxd/current           a link to that directory
  PREFIX/bin/rxd, radio, rxd-tui   links into current/
  PREFIX/share/bash-completion/completions/radio
  UNIT_DIR/rxd@.service            the systemd user unit (UNIT_DIR defaults
                                   to /etc/systemd/user)

and then, as the user (--user NAME, or whoever runs it), the data rxd needs:

  ~/.local/share/rxd/js8/jsc_map.txt     JS8's JSC word table, built from
                                         JS8Call's jsc_map.cpp at the commit
                                         and SHA-256 js8.py pins (skipped when
                                         already built from it)
  ~/.local/share/rxd/callsigns.sqlite    the callsign database, from this
                                         repository's data-callsigns release
                                         (skipped when unchanged)

It never writes the station file (/etc/rxd/station.json) or rxd.env: those
belong to whoever sets the machine up. See docs/install.md.

Idempotent: a run with nothing new changes nothing. Atomic: the code goes into
a new directory that is complete before current/ is switched to it in one
rename, so a failed run leaves the previous install working; every other
file is written beside its target and renamed over it. Data failures (no
network) are warnings: rxd works without either file, and the ones already
there are kept.
"""
import argparse
import hashlib
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

SRC = os.path.dirname(os.path.realpath(__file__))

# source in the tarball or checkout -> name in lib/rxd/X.Y.Z-HASH/
FILES = (
    ("rxd/rxd.py", "rxd.py", 0o755),
    ("rxd/js8.py", "js8.py", 0o755),
    ("rxd/callsign_db.py", "callsign_db.py", 0o755),
    ("rxd/radio.py", "radio.py", 0o755),
    ("rxd/station.py", "station.py", 0o644),
    ("rxd/location.py", "location.py", 0o644),
    ("rxd/hd.py", "hd.py", 0o644),
    ("clients/tui/rxd_tui.py", "rxd_tui.py", 0o755),
    ("clients/web/index.html", "web/index.html", 0o644),
)
BINS = (("rxd", "rxd.py"), ("radio", "radio.py"), ("rxd-tui", "rxd_tui.py"))

UNIT = """\
# Installed by rxd's install.py: the rxd receiver for one radio profile.
# Started and stopped by 'radio'; not enabled on its own.
[Unit]
Description=rxd receiver (%i)
Documentation=https://github.com/pid1/rxd
Wants=pipewire-pulse.service
After=pipewire-pulse.service wireplumber.service

[Service]
EnvironmentFile=-{sysconfdir}/rxd/rxd.env
ExecStart={prefix}/bin/rxd %i
Restart=on-failure
RestartSec=3
"""

DRY = False


def say(msg):
    print("rxd install: " + msg, flush=True)


def warn(msg):
    print("rxd install: !! " + msg, file=sys.stderr, flush=True)


def version():
    with open(os.path.join(SRC, "rxd", "rxd.py"), encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r'^VERSION = "([0-9][0-9.]*)"$', line.rstrip("\n"))
            if m:
                return m[1]
    raise SystemExit("rxd install: no VERSION in rxd/rxd.py")


# ------------------------------------------------------------ files

def read(rel):
    with open(os.path.join(SRC, rel), "rb") as fh:
        return fh.read()


def write_atomic(path, data, mode):
    """Write data to path through a temp file beside it; False if it was
    already exactly that."""
    try:
        with open(path, "rb") as fh:
            if fh.read() == data and (os.stat(path).st_mode & 0o7777) == mode:
                return False
    except OSError:
        pass
    if DRY:
        say("[dry-run] would write " + path)
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return True


def link_atomic(path, target):
    """Make path a symlink to target, replacing what is there in one rename;
    False if it already was."""
    try:
        if os.readlink(path) == target:
            return False
    except OSError:
        pass
    if DRY:
        say("[dry-run] would link %s -> %s" % (path, target))
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)
    return True


def install_code(prefix):
    """lib/rxd/X.Y.Z-HASH/, current/ and bin/: returns (dir name, changed)."""
    payload = [(dst, read(src), mode) for src, dst, mode in FILES]
    h = hashlib.sha256()
    for dst, data, mode in payload:
        h.update(("%s %o %d\n" % (dst, mode, len(data))).encode())
        h.update(data)
    name = "%s-%s" % (version(), h.hexdigest()[:12])
    lib = os.path.join(prefix, "lib", "rxd")
    final = os.path.join(lib, name)
    changed = False
    if not os.path.isdir(final):
        changed = True
        if DRY:
            say("[dry-run] would install %s" % final)
        else:
            os.makedirs(lib, exist_ok=True)
            stage = tempfile.mkdtemp(prefix=".stage-", dir=lib)
            try:
                for dst, data, mode in payload:
                    path = os.path.join(stage, dst)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.chmod(path, mode)
                os.chmod(stage, 0o755)
                os.rename(stage, final)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            say("installed %s" % final)
    current = os.path.join(lib, "current")
    try:
        before = os.readlink(current)
    except OSError:
        before = None
    if link_atomic(current, name):
        changed = True
        say("lib/rxd/current -> %s" % name)
    for cmd, target in BINS:
        if link_atomic(os.path.join(prefix, "bin", cmd),
                       os.path.join("..", "lib", "rxd", "current", target)):
            changed = True
            say("linked bin/%s" % cmd)
    prune(lib, keep={name, before})
    return name, changed


def prune(lib, keep):
    """Remove installs other than the current and the one before it (an rxd
    still running from the old one keeps its files until it restarts)."""
    if DRY or not os.path.isdir(lib):
        return
    for d in os.listdir(lib):
        path = os.path.join(lib, d)
        if d in keep or d == "current" or os.path.islink(path) or not os.path.isdir(path):
            continue
        if re.match(r"^[0-9][0-9.]*-[0-9a-f]{12}$", d) or d.startswith(".stage-"):
            shutil.rmtree(path, ignore_errors=True)
            say("removed the old install %s" % d)


def install_unit(prefix, unit_dir, sysconfdir):
    body = UNIT.format(prefix=prefix.rstrip("/") or "", sysconfdir=sysconfdir.rstrip("/"))
    if write_atomic(os.path.join(unit_dir, "rxd@.service"), body.encode(), 0o644):
        say("wrote %s" % os.path.join(unit_dir, "rxd@.service"))
        return True
    return False


def install_completion(prefix):
    path = os.path.join(prefix, "share", "bash-completion", "completions", "radio")
    if write_atomic(path, read("completions/radio"), 0o644):
        say("wrote %s" % path)
        return True
    return False


# ------------------------------------------------------------- the user

class User:
    """Who the data belongs to, and how to run things as them."""

    def __init__(self, name=None):
        me = os.getuid()
        pw = pwd.getpwnam(name) if name else pwd.getpwuid(me)
        self.name, self.uid, self.home = pw.pw_name, pw.pw_uid, pw.pw_dir
        if name is None or self.uid == me:
            # this process is the user: keep its own HOME (a test's, say)
            self.home = os.environ.get("HOME") or self.home
            self.become = []
        elif me == 0:
            self.become = ["runuser", "-u", self.name, "--"]
        else:
            raise SystemExit("rxd install: only root can install for another user (--user %s)" % name)

    def run(self, argv, **kw):
        env = ["env", "HOME=" + self.home] + (["PATH=" + os.environ.get("PATH", "")]
                                               if not self.become else [])
        return subprocess.run(self.become + env + argv, **kw)


def fetch(url, dest, timeout=120):
    """url (https:// or file://) -> dest."""
    req = urllib.request.Request(url, headers={"User-Agent": "rxd-install/" + version()})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def install_jsc(lib, user, source=None):
    """JS8's JSC word table, from the source js8.py pins. Returns "current",
    "installed" or "failed"."""
    js8 = os.path.join(lib, "js8.py")
    out = user.run([sys.executable, js8, "--jsc-pin"], capture_output=True, text=True)
    lines = out.stdout.split("\n")
    if out.returncode != 0 or len(lines) < 3 or not lines[0] or not re.match(r"^[0-9a-f]{64}$", lines[1]):
        warn("could not read the JSC word table's pin from js8.py; JS8 free text will show gaps")
        return "failed"
    url, sha, state = lines[0], lines[1], lines[2]
    if state == "installed":
        say("JS8's JSC word table is current")
        return "current"
    if DRY:
        say("[dry-run] would fetch and build JS8's JSC word table")
        return "installed"
    fd, tmp = tempfile.mkstemp(prefix="jsc_map.", suffix=".cpp")
    os.close(fd)
    try:
        try:
            if source:
                shutil.copyfile(source, tmp)
            else:
                fetch(url, tmp)
        except (OSError, ValueError) as e:
            warn("could not fetch JS8's JSC word table (%s); any earlier one is kept" % e)
            return "failed"
        got = sha256_of(tmp)
        if got != sha:
            warn("JS8's JSC word table source is not the pinned one (sha256 %s, want %s); "
                 "any earlier one is kept" % (got, sha))
            return "failed"
        os.chmod(tmp, 0o644)  # the user reads what root fetched
        r = user.run([sys.executable, js8, "--jsc-import-source", tmp])
        if r.returncode != 0:
            warn("the JSC word table didn't install; any earlier one is kept")
            return "failed"
        say("installed JS8's JSC word table (JS8Call's, GPL-3) for %s" % user.name)
        return "installed"
    finally:
        os.remove(tmp)


def install_callsigns(lib, user, url=None):
    """The callsign database. Returns "ok" or "failed"."""
    if DRY:
        say("[dry-run] would fetch the callsign database")
        return "ok"
    argv = [sys.executable, os.path.join(lib, "callsign_db.py"), "install"]
    if url:
        argv += ["--url", url]
    if user.run(argv).returncode == 0:
        say("callsign database up to date (%s/.local/share/rxd/callsigns.sqlite)" % user.home)
        return "ok"
    warn("could not update the callsign database; keeping the one installed, if any")
    return "failed"


def reload_user_systemd(user):
    """After a new unit, the user's running systemd re-reads it. Without one
    running it reads the unit fresh at the next login."""
    if DRY or not shutil.which("systemctl"):
        return
    if subprocess.run(["systemctl", "is-active", "--quiet", "user@%d.service" % user.uid]).returncode:
        return
    argv = (["systemctl", "--user", "--machine=%s@" % user.name, "daemon-reload"]
            if os.getuid() == 0 else ["systemctl", "--user", "daemon-reload"])
    if subprocess.run(argv).returncode != 0:
        warn("could not reload %s's systemd; log out and back in to pick up rxd@.service" % user.name)


# ------------------------------------------------------------------ main

def main(argv=None):
    global DRY
    ap = argparse.ArgumentParser(prog="install.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", default="/usr/local")
    ap.add_argument("--unit-dir", default="/etc/systemd/user",
                    help="where rxd@.service goes (default /etc/systemd/user)")
    ap.add_argument("--sysconfdir", default="/etc",
                    help="the unit reads SYSCONFDIR/rxd/rxd.env (default /etc)")
    ap.add_argument("--user", help="who runs rxd: the data goes in their home (default: you)")
    ap.add_argument("--no-data", action="store_true",
                    help="skip the JSC word table and the callsign database")
    ap.add_argument("--jsc-source", metavar="FILE",
                    help="a local copy of the pinned jsc_map.cpp, instead of downloading it")
    ap.add_argument("--callsigns-url", metavar="URL",
                    help="where the callsign database's release assets are")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print(version())
        return 0
    DRY = a.dry_run
    prefix = os.path.abspath(a.prefix)
    user = User(a.user)
    os.umask(0o022)

    name, _ = install_code(prefix)
    unit_new = install_unit(prefix, os.path.abspath(a.unit_dir), a.sysconfdir)
    install_completion(prefix)
    if unit_new and a.user:
        reload_user_systemd(user)

    lib = os.path.join(prefix, "lib", "rxd", name)
    if DRY and not os.path.isdir(lib):
        lib = os.path.join(SRC, "rxd")   # nothing installed to run; ask the source
    if not a.no_data:
        install_jsc(lib, user, a.jsc_source)
        install_callsigns(lib, user, a.callsigns_url)
    say("rxd %s is installed in %s" % (version(), prefix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
