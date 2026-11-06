"""Tests for the browser UI's upconverter switch (clients/web/index.html): the
question asked while its position is unknown, the confirmation before a
profile on the other side, and the readout, in words with a symbol. Where
node is installed, the readout's words run as the page runs them.

    python3 -m unittest discover tests
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402


def page():
    with open(_paths.WEB, encoding="utf-8") as fh:
        return fh.read()


class Switch(unittest.TestCase):
    def test_the_question_and_the_confirmation_are_there(self):
        html = page()
        # asked on every view: outside any data-v section, near the top
        ask = re.search(r'<section id="swAsk"[^>]*>(.*?)</section>', html, re.S)
        self.assertIsNotNone(ask)
        self.assertNotIn("data-v", ask.group(0).split(">", 1)[0])
        self.assertLess(html.index('id="swAsk"'), html.index('id="views"'))
        self.assertIn('Is the <span class="upName">upconverter</span> on UPCONVERT or BYPASS?', ask[1])
        for bid, word in (("swAskUp", "⇅ UPCONVERT"), ("swAskBy", "→ BYPASS")):
            self.assertRegex(ask[1], r'id="%s">%s<small>' % (bid, word))
        # big targets: .btn is 56 px at least, the answers 72
        self.assertRegex(html, r"\.btn\{min-height:56px")
        self.assertRegex(html, r"\.askbtn\{min-height:72px")
        self.assertIn('role="dialog" aria-modal="true"', html)
        # the profile switch carries the confirmation, and a 409 asks again
        self.assertIn('{name: p.name, switch: confirm}', html)
        self.assertIn("r.status === 409", html)
        self.assertIn('api(API + "/switch", {position: pos})', html)
        # the old "allow the other side" setting is gone from the UI
        self.assertNotIn("bpSideOn", html)

    def test_night_has_no_white(self):
        html = page()
        night = "".join(re.findall(r":root\[data-theme=night\][^{]*\{([^}]*)\}", html))
        self.assertNotRegex(night.lower(), r"#fff\b|#ffffff|white")
        for rule in re.findall(r"\.(?:card\.)?(?:ask|dlg|overlay)\{([^}]*)\}", html):
            self.assertNotRegex(rule.lower(), r"#fff\b|#ffffff|white")

    def run_js(self, expr):
        node = shutil.which("node")
        if not node:
            self.skipTest("node isn't installed")
        html = page()
        m = re.search(r"// switch:words begin\n(.*?)// switch:words end", html, re.S)
        self.assertIsNotNone(m, "the switch's words are missing from index.html")
        helpers = [re.search(r"\nfunction utcDate\(t\) \{.*?\n\}", html, re.S)[0],
                   re.search(r"\nconst p2 = .*?;", html)[0],
                   re.search(r"\nfunction hhmm\(d, secs\) .*", html)[0]]
        env = dict(os.environ, TZ="America/Chicago")
        out = subprocess.run([node, "-e", "\n".join(helpers) + "\n" + m[1] +
                              "\nconsole.log(JSON.stringify(" + expr + "));"],
                             capture_output=True, text=True, timeout=20, check=True, env=env)
        return json.loads(out.stdout)

    def test_readout_in_words_local_24_hour(self):
        got = self.run_js('[swWords({position: "unknown"}),'
                          ' swWords({position: "bypass", ok: true, confirmed_at: "2026-11-06T20:02:00Z"}),'
                          ' swWords({position: "upconvert", ok: false, confirmed_at: "2026-11-06T20:02:00Z"}),'
                          ' sideWord("bypass"), pside({upconvert: true})]')
        self.assertEqual(got[0], "? Not confirmed")
        # 20:02 UTC is 14:02 in Chicago; another day adds the date
        self.assertRegex(got[1], r"^✓ BYPASS \(confirmed (\S+ \d+ )?14:02\)$")
        self.assertRegex(got[2], r"^▲ UPCONVERT \(confirmed (\S+ \d+ )?14:02\)$")
        self.assertEqual(got[3:], ["BYPASS", "upconvert"])


if __name__ == "__main__":
    unittest.main()
