"""Tests for the `radio` command, which setup.sh installs from a heredoc.

The script is cut out of setup.sh and loaded as a module with HOME in a temp
dir. systemctl and journalctl are fake shell scripts on PATH that log their
arguments and keep the "running" unit in a state file; a small HTTP server
plays rxd's /api/status for whichever unit that file names. i3, JS8Call and
PipeWire are stubbed on the module.

    python3 -m unittest discover rxd
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SETUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup.sh")

FAKE_SYSTEMCTL = r"""#!/bin/sh
# fake systemctl --user: the running unit lives in $FAKE/up
echo "$*" >> "$FAKE/systemctl.log"
[ "$1" = --user ] && shift
cmd=$1; shift
case $cmd in
  start)
    [ -e "$FAKE/fail-start" ] && { echo "Job for $1 failed." >&2; exit 1; }
    [ -e "$FAKE/fail" ] || echo "$1" > "$FAKE/up" ;;
  stop)
    if [ -e "$FAKE/up" ]; then
      up=$(cat "$FAKE/up")
      case $up in $1) rm -f "$FAKE/up" ;; esac
    fi ;;
  is-active)
    [ -e "$FAKE/up" ] && [ "$(cat "$FAKE/up")" = "$1" ] && { echo active; exit 0; }
    if [ -e "$FAKE/fail" ]; then echo failed; else echo inactive; fi
    exit 3 ;;
esac
exit 0
"""

FAKE_JOURNALCTL = r"""#!/bin/sh
echo "$*" >> "$FAKE/journalctl.log"
echo "Nov 05 10:00:00 uconsole rxd[123]: rxd: dongle SN 57818712 is not plugged in"
echo "Nov 05 10:00:00 uconsole systemd[1]: rxd@vhf.service: Failed with result 'exit-code'."
"""


def load_radio(home):
    with open(SETUP, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("<<'RADIO'\n") + len("<<'RADIO'\n")
    end = text.index("\nRADIO\n", start)
    path = os.path.join(home, "radio.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text[start:end] + "\n")
    old = os.environ.get("HOME")
    os.environ["HOME"] = home  # the module reads it at import
    try:
        spec = importlib.util.spec_from_file_location("radio_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old is not None:
            os.environ["HOME"] = old
    return mod


class FakeRxd:
    """Answers /api/status as rxd would, for the unit the fake systemctl runs."""

    def __init__(self, fake_dir):
        self.fake_dir = fake_dir
        self.status = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                up = os.path.join(outer.fake_dir, "up")
                if self.path != "/api/status" or not os.path.exists(up):
                    self.send_response(503)
                    self.end_headers()
                    return
                with open(up) as fh:
                    unit = fh.read().strip()
                name = unit[len("rxd@"):-len(".service")]
                body = json.dumps(dict(outer.status, profile=name, running=True)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # Port 0: 8073 may be in use by a real rxd.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RadioTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="radio-test-")
        self.home = os.path.join(self.tmp, "home")
        self.fake = os.path.join(self.tmp, "fake")
        bindir = os.path.join(self.tmp, "bin")
        for d in (self.home, self.fake, bindir):
            os.makedirs(d)
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("journalctl", FAKE_JOURNALCTL)):
            path = os.path.join(bindir, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, 0o755)

        self.env = {k: os.environ.get(k) for k in ("PATH", "FAKE", "DISPLAY")}
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        os.environ["FAKE"] = self.fake
        os.environ.pop("DISPLAY", None)  # no i3-nagbar

        self.rxd = FakeRxd(self.fake)
        self.r = load_radio(self.home)
        self.r.RXD_URL = self.rxd.url
        self.r.RXD_START_SECS = 2

        # Desktop side effects, recorded instead of run.
        self.calls = []
        self.js8_up = False
        r = self.r
        r.close = lambda proc, crit: self.calls.append(("close", proc))
        r.running = lambda proc: proc == "js8call" and self.js8_up
        r.start = lambda argv, pat: self.calls.append(("start", argv[0]))
        r.ensure_js8_sink = lambda: self.calls.append(("sink",))
        r.check_clock = lambda *a: None
        r.i3 = lambda cmd: self.calls.append(("i3", cmd))

    def tearDown(self):
        self.rxd.close()
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    # -------------------------------------------------------------- helpers

    def radio(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["radio", *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.r.main()
        finally:
            sys.argv = old
            # Kept for tests where main() exits: the return never happens.
            self._last_err = (out.getvalue(), err.getvalue())
        return self._last_err

    def systemctl_log(self):
        path = os.path.join(self.fake, "systemctl.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def clear_log(self):
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(self.fake, "systemctl.log"))

    def up(self):
        path = os.path.join(self.fake, "up")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read().strip()

    def run_unit(self, name):
        with open(os.path.join(self.fake, "up"), "w") as fh:
            fh.write("rxd@%s.service\n" % name)
        self.r.set_active(name)

    def saved(self, name):
        with open(os.path.join(self.r.STATE, name + ".json")) as fh:
            return json.load(fh)

    def write_saved(self, name, doc):
        os.makedirs(self.r.STATE, exist_ok=True)
        with open(os.path.join(self.r.STATE, name + ".json"), "w") as fh:
            json.dump(doc, fh)

    # ---------------------------------------------------------------- tests

    def test_first_switch_writes_merged_profile_and_starts_unit(self):
        self.assertFalse(os.path.exists(os.path.join(self.r.STATE, "vhf.json")))
        out, _ = self.radio("vhf")
        # rxd knows only the file, so the built-in default must be on disk.
        self.assertEqual(self.saved("vhf"), self.r.PROFILES["vhf"])
        self.assertNotIn("vfo_offset", self.saved("vhf"))
        log = self.systemctl_log()
        self.assertIn("--user stop rxd@*.service", log)
        self.assertIn("--user start rxd@vhf.service", log)
        self.assertLess(log.index("--user stop rxd@*.service"),
                        log.index("--user start rxd@vhf.service"))
        self.assertEqual(self.up(), "rxd@vhf.service")
        self.assertEqual(self.r.active(), "vhf")
        self.assertIn("146.5200 MHz", out)

    def test_saved_edits_merge_over_defaults(self):
        self.write_saved("vhf", {"volume": 0.9})
        self.radio("vhf")
        p = self.saved("vhf")
        self.assertEqual(p["volume"], 0.9)
        self.assertEqual(p["frequency"], 146520000.0)

    def test_switch_saves_profile_being_left_from_status(self):
        # An old SDR++-era file still carrying vfo_offset.
        self.write_saved("vhf", dict(self.r.PROFILES["vhf"], vfo_offset=-250000.0))
        self.run_unit("vhf")
        self.rxd.status = {"freq": 146940000.0, "vfo_hz": 146940000.0, "mode": "FM",
                           "bandwidth": 10000.0, "gain_db": 28.0, "volume": 0.7}
        _, err = self.radio("js8")

        p = self.saved("vhf")
        self.assertEqual(p["frequency"], 146940000.0)
        self.assertEqual(p["demod"], "FM")
        self.assertEqual(p["demod_settings"]["bandwidth"], 10000.0)
        self.assertEqual(p["rtl"]["gain_db"], 28.0)
        self.assertEqual(p["rtl"]["gain"], 20)  # index kept alongside
        self.assertEqual(p["volume"], 0.7)
        self.assertNotIn("vfo_offset", p)

        self.assertEqual(self.up(), "rxd@js8.service")
        self.assertEqual(self.r.active(), "js8")
        self.assertIn("--user start rxd@js8.service", self.systemctl_log())
        # JS8 side: sink made, dial written, JS8Call started, warning shown.
        self.assertIn(("sink",), self.calls)
        self.assertEqual(self.r.read_ini().get("Common", "DialFreq"), "7078000")
        self.assertIn(("start", self.r.JS8CALL), self.calls)
        self.assertIn("flip the Ham It Up to UPCONVERT", err)

    def test_js8_capture_keeps_dial_and_calibration(self):
        self.radio("js8")
        self.rxd.status = {"freq": 7080000.0, "vfo_hz": 7079900.0, "mode": "USB",
                           "bandwidth": 2800.0, "gain_db": 3.7, "volume": 0.2}
        self.radio("vhf")
        p = self.saved("js8")
        self.assertEqual(p["js8call_dial"], 7080000)
        self.assertEqual(p["frequency"], 7079900.0)
        self.assertEqual(p["js8call_dial"] - p["frequency"], 100.0)

    def test_rerun_active_profile_does_not_restart(self):
        self.radio("vhf")
        before = self.saved("vhf")
        self.rxd.status = {"freq": 146940000.0, "vfo_hz": 146940000.0, "mode": "FM",
                           "bandwidth": 10000.0, "gain_db": 28.0, "volume": 0.7}
        self.clear_log()
        self.radio("vhf")
        log = self.systemctl_log()
        self.assertFalse([ln for ln in log if " start " in ln or " stop " in ln], log)
        self.assertEqual(self.up(), "rxd@vhf.service")
        self.assertEqual(self.saved("vhf"), before)  # live tuning not frozen

    def test_rerun_active_js8_keeps_running_dial(self):
        self.radio("js8")
        self.js8_up = True
        self.calls.clear()
        self.rxd.status = {"freq": 7078500.0, "vfo_hz": 7078400.0, "mode": "USB"}
        self.r.write_dial("7078500")  # what JS8Call has on its dial
        self.radio("js8")
        self.assertNotIn(("close", "js8call"), self.calls)
        self.assertEqual(self.r.read_ini().get("Common", "DialFreq"), "7078500")

    def test_active_profile_on_another_unit_is_restarted(self):
        # 'radio save NEW' makes NEW active while rxd@vhf still plays.
        self.run_unit("vhf")
        self.r.set_active("dcara")
        self.clear_log()
        self.radio("dcara")
        self.assertIn("--user start rxd@dcara.service", self.systemctl_log())
        self.assertEqual(self.up(), "rxd@dcara.service")

    def test_off_saves_and_stops(self):
        self.run_unit("vhf")
        self.rxd.status = {"vfo_hz": 162550000.0, "mode": "FM", "bandwidth": 12500.0}
        out, _ = self.radio("off")
        self.assertEqual(self.saved("vhf")["frequency"], 162550000.0)
        self.assertIn("--user stop rxd@*.service", self.systemctl_log())
        self.assertIsNone(self.up())
        self.assertIn(("close", "js8call"), self.calls)
        self.assertIn("radio resume", out)

    def test_off_without_rxd_keeps_saved_profile(self):
        self.write_saved("vhf", dict(self.r.PROFILES["vhf"], frequency=146550000.0))
        self.r.set_active("vhf")
        self.radio("off")
        self.assertEqual(self.saved("vhf")["frequency"], 146550000.0)

    def test_save_active_and_new_name(self):
        self.run_unit("vhf")
        self.rxd.status = {"vfo_hz": 146580000.0, "mode": "FM", "bandwidth": 12500.0,
                           "gain_db": 33.8, "volume": 0.5}
        self.radio("save")
        self.assertEqual(self.saved("vhf")["frequency"], 146580000.0)
        self.assertEqual(self.saved("vhf")["rtl"]["gain_db"], 33.8)

        out, _ = self.radio("save", "simplex")
        p = self.saved("simplex")
        self.assertEqual(p["frequency"], 146580000.0)
        self.assertEqual(p["description"], "simplex")
        self.assertEqual(self.r.active(), "simplex")
        self.assertIn("saved simplex", out)

    def test_save_without_rxd_dies(self):
        self.r.set_active("vhf")
        with self.assertRaises(SystemExit):
            self.radio("save")

    def test_save_rejects_name_systemd_would_mangle(self):
        self.run_unit("vhf")
        with self.assertRaises(SystemExit):
            self.radio("save", "my profile")

    def test_resume_starts_active_profile(self):
        self.write_saved("vhf", dict(self.r.PROFILES["vhf"], frequency=146550000.0))
        self.r.set_active("vhf")
        self.radio("resume")
        self.assertEqual(self.up(), "rxd@vhf.service")
        self.assertEqual(self.saved("vhf")["frequency"], 146550000.0)

    def test_resume_when_up_is_a_no_op(self):
        self.run_unit("vhf")
        self.write_saved("vhf", self.r.PROFILES["vhf"])
        self.radio("resume")
        self.assertFalse([ln for ln in self.systemctl_log() if " start " in ln])

    def test_resume_with_nothing_active(self):
        out, _ = self.radio("resume")
        self.assertIn("no active profile", out)
        self.assertEqual(self.systemctl_log(), [])

    def test_failing_unit_dies_with_journal(self):
        open(os.path.join(self.fake, "fail"), "w").close()
        with self.assertRaises(SystemExit) as cm:
            self.radio("vhf")
        self.assertEqual(cm.exception.code, 1)
        _, err = self._last_err
        self.assertIn("dongle SN 57818712 is not plugged in", err)
        self.assertIn("rxd@vhf.service exited", err)
        log = self.systemctl_log()
        self.assertIn("--user stop rxd@vhf.service", log)  # no restart loop left behind
        with open(os.path.join(self.fake, "journalctl.log")) as fh:
            self.assertIn("--user -u rxd@vhf.service -n 15 --no-pager", fh.read())

    def test_start_command_failing_dies_with_journal(self):
        open(os.path.join(self.fake, "fail-start"), "w").close()
        with self.assertRaises(SystemExit):
            self.radio("vhf")
        _, err = self._last_err
        self.assertIn("rxd@vhf.service did not start: dongle SN 57818712", err)

    def test_upconverting_follows_frequency_unless_pinned(self):
        up = self.r.upconverting
        for name in self.r.PROFILES:
            self.assertEqual(self.r.PROFILES[name]["offset"], "auto", name)
        self.assertTrue(up(self.r.PROFILES["js8"]))
        self.assertTrue(up(self.r.PROFILES["am-820"]))
        self.assertFalse(up(self.r.PROFILES["vhf"]))
        self.assertFalse(up({"frequency": 7e6, "offset": "None"}))  # old saved, pinned
        self.assertTrue(up({"frequency": 146e6, "offset": "Ham-It-Up"}))
        self.assertFalse(up({"frequency": 7e6, "offset": "auto", "upconvert_below_hz": 5e6}))
        self.assertIn("Ham It Up bypass", self.r.summary("vhf", self.r.PROFILES["vhf"]))
        self.assertIn("Ham It Up upconvert", self.r.summary("js8", self.r.PROFILES["js8"]))

    def test_flip_warning_to_bypass(self):
        self.radio("am-820")
        _, err = self.radio("noaa")
        self.assertIn("flip the Ham It Up to BYPASS", err)
        _, err = self.radio("vhf")
        self.assertNotIn("flip", err)

    def test_capture_keeps_offset_mode(self):
        self.radio("js8")
        self.rxd.status = {"freq": 7078000.0, "vfo_hz": 7077900.0, "mode": "USB",
                           "upconverter_hz": 125e6, "offset_mode": "auto"}
        self.radio("vhf")
        p = self.saved("js8")
        self.assertEqual(p["offset"], "auto")
        self.assertNotIn("upconverter_hz", p)

    def test_list_and_show(self):
        out, _ = self.radio()
        self.assertIn("ghostnet", out)
        out, _ = self.radio("show", "vhf")
        self.assertEqual(json.loads(out), self.r.PROFILES["vhf"])

    def test_capture_keeps_squelch(self):
        p = self.r.capture(self.r.profile("vhf"), {"squelch": -55.0})
        self.assertEqual((p["demod_settings"]["squelchEnabled"],
                          p["demod_settings"]["squelchLevel"]), (True, -55.0))
        p = self.r.capture(p, {"squelch": None})
        self.assertFalse(p["demod_settings"]["squelchEnabled"])
        self.assertEqual(self.r.capture(p, {})["demod_settings"], p["demod_settings"])

    def test_wspr_profile_and_its_reporting_switch(self):
        p = self.r.profile("wspr")
        self.assertEqual((p["decoder"], p["wspr_report"], p["volume"]), ("wspr", True, 0.0))
        self.assertTrue(self.r.upconverting(p))
        off = {"wspr": {"report": {"enabled": False, "call": "KA1PID", "grid": "EM13lc"}}}
        self.assertFalse(self.r.capture(p, off)["wspr_report"])
        # No callsign: rxd can't report, which says nothing about the setting.
        cant = {"wspr": {"report": {"enabled": False, "reason": "no callsign"}}}
        self.assertTrue(self.r.capture(p, cant)["wspr_report"])
        self.assertNotIn("wspr_report", self.r.capture(self.r.profile("js8"), off))

    def test_switch_to_wspr_closes_js8call(self):
        self.radio("js8")
        self.calls.clear()
        self.radio("wspr")
        self.assertIn(("close", "js8call"), self.calls)
        self.assertEqual(self.up(), "rxd@wspr.service")
        self.assertEqual(self.saved("wspr")["decoder"], "wspr")

    def test_json_lists_profiles_for_the_web_ui(self):
        self.radio("js8")
        doc = json.loads(self.radio("--json")[0])
        self.assertEqual(doc["active"], "js8")
        rows = {r["name"]: r for r in doc["profiles"]}
        self.assertEqual(list(rows)[:2], ["ghostnet", "js8"])
        self.assertEqual(rows["js8"], {"name": "js8", "description": "JS8, 40 m",
                                       "freq": 7078000.0, "mode": "USB", "upconvert": True,
                                       "js8": True, "decoder": "js8", "saved": True})
        self.assertEqual((rows["wspr"]["decoder"], rows["wspr"]["js8"], rows["wspr"]["freq"]),
                         ("wspr", False, 7038600.0))
        self.assertIsNone(rows["vhf"]["decoder"])
        self.assertEqual((rows["fm-105.3"]["upconvert"], rows["fm-105.3"]["js8"],
                          rows["fm-105.3"]["mode"]), (False, False, "WFM"))
        self.assertTrue(rows["am-820"]["upconvert"])


if __name__ == "__main__":
    unittest.main()
