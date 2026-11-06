"""Tests for rxd/js8.py, rxd's own JS8 decoder.

    python -m unittest discover rxd

Signals are synthesized with the module's own encoder. Callsigns are Win's
(KA1PID) and Q-prefixed ones, which are never issued. Real recordings hold
other stations' callsigns, so none are kept in git: point RXD_JS8_REAL at a
directory of slot WAVs and their JS8Call ALL.TXT lines to run those too.
"""
import json
import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import js8  # noqa: E402


def noise_slot(sub, rng, seconds=None):
    m = js8.SUBMODES[sub]
    before, after = js8.Decoder(sub).window()
    n = int(((seconds or m.slot) + before + after) * js8.RATE)
    return rng.normal(0, 1, n).astype(np.float32), -before


def add_signal(x, t0, frame, i3, sub, f0, dt, snr):
    """A frame at f0 Hz starting dt s after the nominal start, at snr dB in
    2500 Hz against unit-variance noise sampled at 12 kHz."""
    m = js8.SUBMODES[sub]
    amp = np.sqrt(2 * 10 ** (snr / 10) * 2500 / (js8.RATE / 2))
    s = js8.synth(js8.tones(frame, i3, m), f0, m, amp, 0.7)
    a = int(round((m.delay + dt - t0) * js8.RATE))
    x[a:a + len(s)] += s[:max(0, len(x) - a)]


class Code(unittest.TestCase):
    def test_parity_checks_are_the_generators_dual(self):
        rng = np.random.default_rng(1)
        for _ in range(50):
            m = rng.integers(0, 2, 87).astype(np.uint8)
            cw = np.concatenate([(js8._GEN.astype(int) @ m) % 2, m]).astype(np.uint8)
            self.assertTrue(js8.check_parity(cw))
        counts = np.bincount(np.concatenate([np.array(r) for r in js8.NM]), minlength=174)
        self.assertTrue((counts == 3).all())
        self.assertEqual(len(js8.NM), 87)

    def test_crc_round_trip_and_rejection(self):
        frame, i3 = js8.pack_directed("KA1PID", "Q1ABC", " SNR", -7)
        bits = js8.frame_bits(frame, i3)
        self.assertEqual(js8.bits_frame(bits), (frame, i3))
        for k in (0, 40, 74, 80):
            bad = list(bits)
            bad[k] ^= 1
            self.assertIsNone(js8.bits_frame(bad))

    def test_tones_carry_the_costas_arrays(self):
        frame, i3 = js8.pack_heartbeat("KA1PID", "EM13")
        t = js8.tones(frame, i3, "normal")
        self.assertEqual(list(t[0:7]), [4, 2, 5, 6, 1, 3, 0])
        self.assertEqual(list(t[72:79]), [4, 2, 5, 6, 1, 3, 0])
        t = js8.tones(frame, i3, "fast")
        self.assertEqual(list(t[36:43]), [1, 5, 0, 2, 3, 6, 4])

    def test_belief_propagation_corrects_errors(self):
        frame, i3 = js8.pack_directed("KA1PID", "Q2XYZ", " ACK")
        cw = js8.encode(frame, i3)
        rng = np.random.default_rng(2)
        llr = (cw * 2.0 - 1) * 2 + rng.normal(0, 0.5, 174)
        wrong = rng.choice(174, 15, replace=False)
        llr[wrong] = -0.5 * llr[wrong]   # 15 bits received wrong, if weakly
        bits, ok, _ = js8.bp_decode(llr[None])
        self.assertTrue(ok[0])
        self.assertEqual(js8.bits_frame(bits[0, 87:]), (frame, i3))


class Frames(unittest.TestCase):
    """Unpacking, against what JS8Call shows for the same frames."""

    def test_heartbeat_and_cq_keep_the_grid(self):
        u = js8.unpack(*js8.pack_heartbeat("KA1PID", "EM13"))
        self.assertEqual(u["kind"], "heartbeat")
        self.assertEqual(u["text"], "KA1PID: @HB HEARTBEAT EM13 ")
        u = js8.unpack(*js8.pack_heartbeat("KA1PID", "EM13", cq="CQ CQ CQ"))
        self.assertEqual(u["text"], "KA1PID: @ALLCALL CQ CQ CQ EM13 ")
        u = js8.unpack(*js8.pack_heartbeat("Q1ABC/P"))
        self.assertEqual(u["text"], "Q1ABC/P: @HB HEARTBEAT  ")

    def test_directed_commands(self):
        cases = [(("KA1PID", "Q1ABC", " SNR", -3), "KA1PID: Q1ABC SNR -03 "),
                 (("Q1ABC", "KA1PID", " HEARTBEAT SNR", 12), "Q1ABC: KA1PID HEARTBEAT SNR +12 "),
                 (("Q1ABC/P", "@ALLCALL", " SNR?", None), "Q1ABC/P: @ALLCALL SNR? "),
                 (("KA1PID", "Q9TST", " HW CPY?", None), "KA1PID: Q9TST HW CPY? "),
                 (("KA1PID", "Q9TST", " ", None), "KA1PID: Q9TST  ")]
        for args, text in cases:
            u = js8.unpack(*js8.pack_directed(*args))
            self.assertEqual(u["kind"], "directed")
            self.assertEqual(u["text"], text)

    def test_known_frames(self):
        # (frame, i3) -> text: frames sent by JS8Call, as its ALL.TXT shows them.
        saved, js8._JSC = js8._JSC, False   # as if no JSC table were installed
        try:
            self.assertEqual(js8.unpack("y0++++++++++", 2)["kind"], "jsc")
        finally:
            js8._JSC = saved
        self.assertEqual(js8.frame_value(js8.value_frame(12345678901234567)), 12345678901234567)

    def test_uncompressed_data(self):
        u = js8.unpack(*js8.pack_huffman("HELLO 73"))
        self.assertEqual(u, {"kind": "data", "text": "HELLO 73"})

    def test_calls_and_grids_round_trip(self):
        for call in ("KA1PID", "Q1ABC", "Q1ABC/P", "Q2X", "@ALLCALL", "@HB"):
            v, p = js8.pack_call(call)
            self.assertEqual(js8.unpack_call(v, p), call)
        for grid in ("EM13", "FN42", "JO65", "QF22", "AA00"):
            self.assertEqual(js8.unpack_grid(js8.pack_grid(grid)), grid)

    def test_jsc_decoding_with_a_supplied_table(self):
        # A made-up table, to check the (s,c)-dense decoder itself.
        table = ["W%d" % i for i in range(5000)]
        # A stopper (3) then a space; a continuer (8) and a stopper (1), the
        # word 7 + (8 - 7) * 7 + 1.
        bits = [0, 0, 1, 1, 1] + [1, 0, 0, 0, 0, 0, 0, 1, 0]
        self.assertEqual(js8.jsc_decompress(bits, table), "W3 W15")


class Messages(unittest.TestCase):
    def feed(self, a, slot, *frames, freq=1000.0):
        decodes = [{"frame": f, "i3": i3, "freq": freq, "snr": -10, "submode": "A"}
                   for f, i3 in frames]
        return a.feed(slot, decodes, 15, "A")

    def test_single_frame_messages(self):
        a = js8.Assembler()
        out = self.feed(a, 0, js8.pack_heartbeat("KA1PID", "EM13"))
        self.assertEqual(out[0]["text"], "KA1PID: @HB HEARTBEAT EM13 ♢")
        out = self.feed(a, 15, js8.pack_directed("Q1ABC", "KA1PID", " HEARTBEAT SNR", -9))
        self.assertEqual(out[0]["text"], "Q1ABC: KA1PID HEARTBEAT SNR -09 ♢")
        self.assertEqual((out[0]["from"], out[0]["to"]), ("Q1ABC", "KA1PID"))

    def test_directed_then_data_frames(self):
        a = js8.Assembler()
        self.assertEqual(self.feed(a, 0, js8.pack_directed("KA1PID", "Q1ABC", " ", i3=js8.FIRST)), [])
        self.assertEqual(self.feed(a, 15, js8.pack_huffman("GOOD MORNING ", 0)), [])
        out = self.feed(a, 30, js8.pack_huffman("FROM TEXAS", js8.LAST))
        self.assertEqual(out[0]["text"], "KA1PID: Q1ABC  GOOD MORNING FROM TEXAS ♢")

    def test_relay_checksum_is_checked_and_removed(self):
        text = "Q2XYZ HELLO"   # (a real relay names its next hop with ">", sent compressed)
        for good in (True, False):
            a = js8.Assembler()
            self.feed(a, 0, js8.pack_directed("KA1PID", "Q1ABC", ">", i3=js8.FIRST))
            tail = " " + (js8.checksum16(text) if good else "AAA")
            self.feed(a, 15, js8.pack_huffman(text, 0))
            out = self.feed(a, 30, js8.pack_huffman(tail, js8.LAST))
            if good:
                self.assertEqual(out[0]["text"], "KA1PID: Q1ABC> Q2XYZ HELLO \u2662")
            else:
                self.assertEqual(out, [])   # JS8Call drops it too

    def test_missing_frame_shows_a_gap(self):
        a = js8.Assembler()
        self.feed(a, 0, js8.pack_directed("KA1PID", "Q1ABC", " ", i3=js8.FIRST))
        out = self.feed(a, 30, js8.pack_huffman("THE END", js8.LAST))
        self.assertIn("……", out[0]["text"])
        self.assertTrue(out[0]["text"].endswith("THE END ♢"))

    def test_unfinished_message_is_let_go(self):
        a = js8.Assembler()
        self.feed(a, 0, js8.pack_directed("KA1PID", "Q1ABC", " ", i3=js8.FIRST))
        self.feed(a, 15, js8.pack_huffman("NO LAST FRAME", 0))
        out = self.feed(a, 90)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["complete"])
        self.assertNotIn("♢", out[0]["text"])

    def test_feed_line_shape(self):
        # What rxd's Js8Feed reads back out of the DIRECTED.TXT line.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import rxd
        msg = js8.Assembler().feed(0, [{"frame": f, "i3": i3, "freq": 1234.4, "snr": -12,
                                        "submode": "A"} for f, i3 in
                                       [js8.pack_heartbeat("KA1PID", "EM13")]], 15, "A")[0]
        line = js8.directed_line(1762430415, 7078000, msg)
        got = rxd.Js8Feed.parse(line)
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(1762430415))
        self.assertEqual(got, {"time": when, "freq": 7078000.0, "offset": 1234,
                               "snr": -12, "from": "KA1PID", "to": None,
                               "text": "KA1PID: @HB HEARTBEAT EM13"})


class Decoding(unittest.TestCase):
    MSGS = [js8.pack_directed("KA1PID", "Q1ABC", " SNR", -12),
            js8.pack_heartbeat("Q2XYZ", "EM48"),
            js8.pack_directed("Q9TST", "KA1PID", " HW CPY?"),
            js8.pack_huffman("HELLO WORLD 73"),
            js8.pack_heartbeat("Q1ABC/P", "FN42", cq="CQ CQ CQ")]

    def run_slot(self, sub, snr, seed, n=5):
        rng = np.random.default_rng(seed)
        x, t0 = noise_slot(sub, rng)
        m = js8.SUBMODES[sub]
        truth = []
        span = (2800 - 300) / n
        for k, (f, i3) in enumerate(self.MSGS[:n]):
            f0 = 300 + k * span + rng.uniform(0, span - 9 * m.baud)
            dt = rng.uniform(-1.0, 1.0) * min(1.5, js8.Decoder.DT_MAX[sub] - 0.2)
            add_signal(x, t0, f, i3, sub, f0, dt, snr)
            truth.append((f, f0, dt))
        got = {d["frame"]: d for d in js8.Decoder(sub).decode(x, t0)}
        return truth, got

    def test_normal_at_several_snrs_offsets_and_times(self):
        for snr in (-8, -14, -18):
            truth, got = self.run_slot("normal", snr, seed=40 + snr)
            for frame, f0, dt in truth:
                self.assertIn(frame, got, "missed at %d dB" % snr)
                d = got[frame]
                self.assertLess(abs(d["freq"] - f0), 1.0)
                self.assertLess(abs(d["dt"] - dt), 0.03)
                self.assertLess(abs(d["snr"] - snr), 3)
            self.assertEqual(set(got) - {t[0] for t in truth}, set())

    def test_other_submodes(self):
        for sub in ("fast", "turbo", "slow"):
            truth, got = self.run_slot(sub, -12, seed=3, n=3)
            self.assertEqual({t[0] for t in truth}, set(got), sub)

    def test_normal_ignores_other_submodes(self):
        rng = np.random.default_rng(4)
        x, t0 = noise_slot("normal", rng)
        f, i3 = self.MSGS[0]
        add_signal(x, t0, f, i3, "fast", 1000, 0.0, -5)
        self.assertEqual(js8.Decoder("normal").decode(x, t0), [])

    def test_no_false_decodes_in_noise(self):
        rng = np.random.default_rng(5)
        for _ in range(4):
            x, t0 = noise_slot("normal", rng)
            self.assertEqual(js8.Decoder("normal").decode(x, t0), [])

    def test_overlapping_signals_need_subtraction(self):
        rng = np.random.default_rng(6)
        x, t0 = noise_slot("normal", rng)
        a, b = self.MSGS[0], self.MSGS[2]
        add_signal(x, t0, a[0], a[1], "normal", 1000.0, 0.0, 0)
        add_signal(x, t0, b[0], b[1], "normal", 1018.75, 0.4, -14)
        got = {d["frame"] for d in js8.Decoder("normal").decode(x, t0)}
        self.assertEqual(got, {a[0], b[0]})


class Recording(unittest.TestCase):
    """SlotDecoder: rxd's 48 kHz blocks in, JS8Call-style logs out."""

    def test_slots_from_pushed_audio(self):
        rng = np.random.default_rng(8)
        with tempfile.TemporaryDirectory() as d:
            now = [1762430400.0 - 3]           # 3 s before a slot boundary
            sd = js8.SlotDecoder(7078000, ["normal"], datadir=d, clock=lambda: now[0],
                                 inline=True)
            # 12 kHz audio for the slot, then up-sampled to rxd's 48 kHz.
            total = 3 + 15 + 2.0
            x12 = rng.normal(0, 0.05, int(total * 12000)).astype(np.float32)
            f, i3 = js8.pack_heartbeat("KA1PID", "EM13")
            s = js8.synth(js8.tones(f, i3), 1500, "normal", 0.05)
            a = int((3 + 0.5) * 12000)
            x12[a:a + len(s)] += s
            x48 = np.repeat(x12, 4)
            blk = 4096
            for i in range(0, len(x48) - blk + 1, blk):
                now[0] += blk / 48000
                sd.push(x48[i:i + blk], 7078000)
            deadline = time.time() + 60
            while sd.last is None and time.time() < deadline:
                time.sleep(0.05)
            st = sd.status()
            self.assertEqual(st["last"]["decodes"], 1, st)
            with open(os.path.join(d, "DIRECTED.TXT"), encoding="utf-8") as fh:
                row = fh.read().strip().split("\t")
            self.assertEqual(row[1], "7.078000")
            self.assertAlmostEqual(int(row[2]), 1500, delta=2)
            self.assertEqual(row[4], "KA1PID: @HB HEARTBEAT EM13 ♢")
            sd.close()

    def test_retune_drops_the_slot(self):
        with tempfile.TemporaryDirectory() as d:
            now = [1762430400.0 - 1]
            sd = js8.SlotDecoder(7078000, ["normal"], datadir=d, clock=lambda: now[0],
                                 inline=True)
            blk = np.zeros(4800, np.float32)
            for i in range(200):
                now[0] += 0.1
                sd.push(blk, 7078000 if i < 50 else 7074000)
            self.assertTrue(sd.jobs.empty() and sd.last is None)
            sd.close()

    def test_serve_protocol(self):
        import io
        rng = np.random.default_rng(9)
        x, t0 = noise_slot("normal", rng)
        f, i3 = js8.pack_heartbeat("KA1PID", "EM13")
        add_signal(x, t0, f, i3, "normal", 800, 0.2, -10)
        inp = io.BytesIO((json.dumps({"submode": "normal", "t0": t0, "n": len(x)}) + "\n")
                         .encode() + x.astype("<f4").tobytes())
        out = io.StringIO()
        js8.serve(inp, out)
        reply = json.loads(out.getvalue())
        self.assertEqual([d["frame"] for d in reply["decodes"]], [f])
        self.assertGreater(reply["cpu"], 0)


class RxdWiring(unittest.TestCase):
    def test_engine_and_submodes_from_the_profile(self):
        import rxd
        self.assertEqual(rxd.js8_engine({}), "js8call")
        self.assertEqual(rxd.js8_engine({"js8_engine": "rxd"}), "rxd")
        with self.assertRaises(ValueError):
            rxd.js8_engine({"js8_engine": "wsjtx"})
        self.assertIsNotNone(rxd.load_js8())
        p = {"frequency": 7077900, "js8call_dial": 7078000, "js8_engine": "rxd",
             "js8_submodes": ["normal", "warp"]}
        with self.assertRaises(ValueError):
            rxd.make_js8(rxd.Settings(p, "js8"), p)


@unittest.skipUnless(os.environ.get("RXD_JS8_REAL"), "set RXD_JS8_REAL to a recordings dir")
class RealSlots(unittest.TestCase):
    """Each NAME.wav (a slot from 2 s before its start) beside NAME.txt, the
    frames JS8Call decoded in it, one '<frame> <i3>' per line."""

    def test_recall(self):
        root = os.environ["RXD_JS8_REAL"]
        want = got = 0
        for name in sorted(os.listdir(root)):
            if not name.endswith(".wav"):
                continue
            x = js8.read_wav(os.path.join(root, name))
            with open(os.path.join(root, name[:-4] + ".txt")) as fh:
                truth = {tuple(r.split()) for r in fh if r.strip()}
            dec = {(d["frame"], str(d["i3"])) for d in js8.Decoder("normal").decode(x, -2.0)}
            want += len(truth)
            got += len(truth & dec)
        self.assertGreaterEqual(got, 0.8 * want)


if __name__ == "__main__":
    unittest.main()
