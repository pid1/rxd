#!/usr/bin/env python3
"""release.py: build dist/rxd-X.Y.Z.tar.gz and dist/rxd-X.Y.Z.tar.gz.sha256.

    python3 scripts/release.py [--ref REF] [--out DIR]

The release workflow (.github/workflows/release.yml) runs exactly this, and
so can you. The tarball is reproducible: it is made from the files git has
at REF (default HEAD), never the working tree, in sorted order, every entry
owned by 0:0 with the mode git records (0755 or 0644) and REF's commit time,
in plain ustar, gzipped by Python's zlib at level 9 with no name and a zero
time in the header. The same REF gives the same bytes on any machine, so a
local build of a tag must match the published release's .sha256.

What goes in: everything at REF but .github/, tests/ and dotfiles, under
rxd-X.Y.Z/. X.Y.Z is VERSION in rxd/rxd.py at REF.
"""
import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SKIP = (".github/", "tests/")


def git(*args, binary=False):
    out = subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode()


def wanted(path):
    return not path.startswith(SKIP) and not any(p.startswith(".") for p in path.split("/"))


def build(ref, out):
    commit = git("rev-parse", "--verify", ref + "^{commit}").strip()
    mtime = int(git("log", "-1", "--format=%ct", commit).strip())
    rxd = git("show", commit + ":rxd/rxd.py")
    m = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', rxd, re.M)
    if not m:
        raise SystemExit("release.py: no VERSION in rxd/rxd.py at %s" % ref)
    version = m[1]
    top = "rxd-%s" % version

    files = []
    for line in git("ls-tree", "-r", "-z", "--full-tree", commit).split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob" or not wanted(path):
            continue
        if mode not in ("100644", "100755"):
            raise SystemExit("release.py: %s is a %s, which a release can't carry" % (path, mode))
        files.append((path, mode == "100755", sha))
    files.sort()

    dirs = sorted({"/".join(p.split("/")[:i]) for p, _, _ in files
                   for i in range(1, p.count("/") + 1)})

    def info(name, isdir=False, size=0, exe=False):
        t = tarfile.TarInfo(name)
        t.type = tarfile.DIRTYPE if isdir else tarfile.REGTYPE
        t.mode = 0o755 if isdir or exe else 0o644
        t.uid = t.gid = 0
        t.uname = t.gname = ""
        t.mtime = mtime
        t.size = size
        return t

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for d in [""] + dirs:
            tar.addfile(info(top + ("/" + d if d else "") + "/", isdir=True))
        for path, exe, sha in files:
            data = git("cat-file", "blob", sha, binary=True)
            tar.addfile(info(top + "/" + path, size=len(data), exe=exe), io.BytesIO(data))

    os.makedirs(out, exist_ok=True)
    name = top + ".tar.gz"
    gz = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=gz, compresslevel=9, mtime=0) as fh:
        fh.write(raw.getvalue())
    body = gz.getvalue()
    digest = hashlib.sha256(body).hexdigest()
    for fname, data in ((name, body), (name + ".sha256", ("%s  %s\n" % (digest, name)).encode())):
        tmp = os.path.join(out, fname + ".part")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(out, fname))
    return os.path.join(out, name), digest, commit


def main(argv=None):
    ap = argparse.ArgumentParser(prog="release.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args(argv)
    path, digest, commit = build(a.ref, a.out)
    print("%s  %s  (from %s)" % (digest, path, commit[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
