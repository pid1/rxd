"""Tests for scripts/release.py: the tarball holds what install.py needs,
under rxd-X.Y.Z/, and is the same bytes every time from the same commit.

    python3 -m unittest discover tests
"""
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402  rxd/ and clients/tui/ on sys.path
import rxd  # noqa: E402

RELEASE = os.path.join(_paths.ROOT, "scripts", "release.py")


def in_git():
    return subprocess.run(["git", "-C", _paths.ROOT, "rev-parse", "HEAD"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(in_git(), "needs the git checkout (a release tarball has no history)")
class Release(unittest.TestCase):
    def build(self):
        out = tempfile.mkdtemp(prefix="rxd-release-")
        self.addCleanup(shutil.rmtree, out, True)
        r = subprocess.run([sys.executable, RELEASE, "--out", out], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        name = "rxd-%s.tar.gz" % rxd.VERSION
        return os.path.join(out, name), r.stdout

    def test_reproducible(self):
        a, _ = self.build()
        b, _ = self.build()
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())
        with open(a + ".sha256") as fh:
            sha, name = fh.read().split()
        self.assertEqual(name, os.path.basename(a))
        self.assertRegex(sha, r"^[0-9a-f]{64}$")

    def test_contents(self):
        path, _ = self.build()
        top = "rxd-%s/" % rxd.VERSION
        with tarfile.open(path) as tar:
            members = {m.name: m for m in tar.getmembers()}
        for f in ("install.py", "rxd/rxd.py", "rxd/js8.py", "rxd/callsign_db.py", "rxd/radio.py",
                  "rxd/station.py", "rxd/location.py", "rxd/hd.py", "clients/web/index.html", "clients/tui/rxd_tui.py",
                  "completions/radio", "LICENSE", "README.md", "docs/API.md", "docs/station.md"):
            self.assertIn(top + f, members, f)
        self.assertFalse([n for n in members if n.startswith(top + "tests/")
                          or n.startswith(top + ".github/")])
        for m in members.values():
            self.assertTrue(m.name.startswith(top) or m.name == top.rstrip("/"), m.name)
            self.assertEqual((m.uid, m.gid, m.uname, m.gname), (0, 0, "", ""))
        self.assertEqual(members[top + "install.py"].mode, 0o755)
        self.assertEqual(members[top + "rxd/station.py"].mode, 0o644)


if __name__ == "__main__":
    unittest.main()
