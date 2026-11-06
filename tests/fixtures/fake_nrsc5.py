#!/usr/bin/env python3
"""A stand-in for nrsc5 in pipe mode, for rxd's tests. No real HD decoding.

  fake_nrsc5.py -r - -o - -t raw PROGRAM

Reads cu8 IQ on stdin and, paced by how much IQ it has read (1488375 S/s),
writes nrsc5-style log lines on stderr and 44.1 kHz s16 stereo audio frames
of 2048 samples on stdout, the way nrsc5 does. What it says and plays comes
from $FAKE_NRSC5, a JSON object (all keys optional):

  sync_at   IQ seconds before "Synchronized" (default 0.5)
  audio_at  IQ seconds before audio starts (default 1.0)
  lead      seconds the audio's content runs ahead of the IQ it came from:
            positive is HD ahead of analog, as nrsc5 usually is (default 2.0)
  lost      [[t0, t1], ...] IQ seconds with sync lost (no audio, as nrsc5)
  bad       [[t0, t1], ...] IQ seconds whose frames are silence (bad packets)
  ber, mer  what it reports once a second while in sync
  station, title, artist, programs [[n, "type"], ...]
  exit_at   IQ seconds after which it exits with status 1
  marker_hz a tone added to the HD audio only, so a test can tell HD from analog
  shift     [[t, dt], ...] from IQ second t on, the audio runs dt seconds further ahead
  log       a file to append its argv to

program_audio(t, program) is the audio, a function of content time, so a
test can FM-modulate the same audio into the IQ it feeds rxd.
"""
import json
import os
import sys
import time

import numpy as np

RATE = 1488375
AUDIO = 44100
FRAME = 2048


_TABLES = {}


def _table(program):
    """120 s of band-limited noise at 8 kHz, the same every run."""
    if program not in _TABLES:
        from scipy import signal as sps
        rng = np.random.default_rng(1000 + program)
        x = rng.standard_normal(8000 * 120)
        b, a = sps.butter(6, [250, 3000], "bandpass", fs=8000)
        x = sps.filtfilt(b, a, x)
        _TABLES[program] = (x / np.std(x)).astype(np.float64)
    return _TABLES[program]


def program_audio(t, program=0):
    """The station's audio at content times t (seconds): speech-band noise
    under a slow swell, so it lines up at one lag only, as real audio does."""
    t = np.asarray(t, np.float64)
    tab = _table(program)
    pos = (t * 8000.0) % len(tab)
    i = np.floor(pos).astype(np.int64)
    f = pos - i
    x = tab[i] * (1 - f) + tab[(i + 1) % len(tab)] * f
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 0.37 * t) * np.sin(2 * np.pi * 0.11 * t + 1)
    return (np.clip(env * x * 0.25, -0.9, 0.9)).astype(np.float32)


def inside(t, spans):
    return any(a <= t < b for a, b in spans)


def main():
    cfg = json.loads(os.environ.get("FAKE_NRSC5") or "{}")
    if cfg.get("log"):
        with open(cfg["log"], "a") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\n")
    program = int(sys.argv[-1])
    sync_at = cfg.get("sync_at", 0.5)
    audio_at = cfg.get("audio_at", 1.0)
    lead = cfg.get("lead", 2.0)
    lost = cfg.get("lost", [])
    bad = cfg.get("bad", [])
    out, err = sys.stdout.buffer, sys.stderr

    def say(msg):
        err.write(time.strftime("%H:%M:%S ") + msg + "\n")
        err.flush()

    consumed = 0
    synced = False
    said = set()
    frame = 0          # next audio frame
    audio_from = None  # IQ time audio (re)started at
    next_q = sync_at
    while True:
        b = sys.stdin.buffer.read(65536)
        if not b:
            return 0
        consumed += len(b) // 2
        t = consumed / RATE
        if cfg.get("exit_at") is not None and t >= cfg["exit_at"]:
            say("fake: exiting")
            return 1
        want = t >= sync_at and not inside(t, lost)
        if want and not synced:
            synced = True
            say("Synchronized")
            say("Frequency offset: 12 Hz")
            say("Primary service mode: 1")
            audio_from = None
        elif synced and not want:
            synced = False
            say("Lost synchronization")
        if not synced:
            continue
        if "station" not in said and t >= sync_at + 0.2:
            said.add("station")
            if cfg.get("station"):
                say("Station name: %s" % cfg["station"])
            for n, typ in cfg.get("programs", [[0, "None"]]):
                say("Audio service %d: public, type: %s, codec: 0, blend: 2, gain: 0 dB, "
                    "delay: 96, latency: 8" % (n, typ))
            if cfg.get("title"):
                say("Title: %s" % cfg["title"])
                say("Artist: %s" % cfg.get("artist", ""))
        if t >= next_q:
            next_q = t + 1.0
            mer = cfg.get("mer", [12.0, 13.0])
            say("MER: %.1f dB (lower), %.1f dB (upper)" % tuple(mer))
            ber = cfg.get("ber", 0.0)
            say("BER: %f, avg: %f, min: %f, max: %f" % (ber, ber, ber, ber))
        if t < audio_at:
            continue
        if audio_from is None:
            audio_from = max(t, audio_at)
            frame = 0
        # Frames whose audio has been "decoded" by now.
        while audio_from + (frame + 1) * FRAME / AUDIO <= t:
            start = audio_from + frame * FRAME / AUDIO
            if inside(start, bad):
                pcm = np.zeros((FRAME, 2), "<i2")
            else:
                ahead = lead + sum(dt for at, dt in cfg.get("shift", []) if start >= at)
                c = start + ahead + np.arange(FRAME) / AUDIO
                a = program_audio(c, program)
                if cfg.get("marker_hz"):
                    a = a + 0.1 * np.sin(2 * np.pi * cfg["marker_hz"] * c).astype(np.float32)
                pcm = np.repeat((a * 32767).astype("<i2")[:, None], 2, axis=1)
            out.write(pcm.tobytes())
            frame += 1
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
