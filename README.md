# rxd

A small RTL-SDR receiver daemon in Python, meant to replace SDR++ for the
`radio` profiles. It reads the same profile JSON as `radio`, opens the dongle
by serial, demodulates one channel and plays it to a PipeWire sink. It has
two control ports on 127.0.0.1:

- **4532, Hamlib NET rigctl.** JS8Call (Rig: *Hamlib NET rigctl*) or
  `rigctl -m 2 -r 127.0.0.1:4532` can get and set frequency and mode.
- **8073, HTTP JSON.** `GET /api/status`, and `POST /api/tune` with any of
  `freq`, `mode`, `bandwidth`, `gain_db` and `volume`. Status also reports
  `profile`, `vfo_hz` (the carrier, which is what a profile's `frequency`
  means), `cal_hz` and `gains` (the dongle's gain steps in dB). This is what
  `radio save` reads. `GET /api/spectrum` gives the whole captured band
  about 5 times a second: 512 dB bins with `start_hz`, `stop_hz`, `dial_hz`
  and the demodulated `passband`, all on the dial's scale. `GET /` serves the
  browser UI from `$RXD_WEB`, `/usr/local/share/rxd/index.html` or
  `web/index.html` beside rxd.py. Listen addresses default to `$RXD_HTTP` and
  `$RXD_RIGCTL` when there's no `--http` or `--rigctl` flag.

Modes: USB, LSB, AM, FM and WFM (mono, no RDS). A profile with
`"decoder": "wspr"` also decodes WSPR: each two-minute slot of its audio goes
to `wsprd`, the spots to `GET /api/wspr?after=SEQ` and, unless
`"wspr_report": false`, to WSPRnet (see docs/radio.md). Status reports the
decoder (`decoder`: js8, wspr or null) and the WSPR cycle and reporting
state (`wspr`). Needs only `python3-numpy` and
`python3-scipy`, already on the uConsole; librtlsdr and libpulse-simple are
loaded with ctypes.

```
rxd js8                       # ~/.config/rx/radio/js8.json, to its sink
rxd js8 --audio stdout | ...  # raw s16le mono 48 kHz
rxd js8 --iq synth --audio null --rigctl 127.0.0.1:4542 --http 127.0.0.1:8083
                              # no dongle: noise plus a tone, for testing
```

A profile's `offset` is `"auto"` by default: the Ham It Up's 125 MHz offset
applies below `upconvert_below_hz` (30 MHz) and not above it, so a live retune
from 40 m to 2 m just works. `"Ham-It-Up"` pins it on and `"None"` pins it off;
`upconverter_hz` changes the 125 MHz. The Ham It Up's UPCONVERT/BYPASS switch
is physical, so status reports `upconverter_hz` (0 or 125e6) for `radio` to
warn from.

A profile's `rtl.gain_db` wins over `rtl.gain`, SDR++'s index into the tuner's
gain table.

Frequencies on both ports are the dial. A JS8 profile whose VFO sits 100 Hz
under its `js8call_dial` keeps that offset as a calibration, so rigctl
reports 7078000.

## How it works

- **Tuning:** the dongle runs at 240 kS/s (1.2 MS/s for WFM). It tunes 50 kHz
  below the signal (250 kHz for WFM) to stay clear of the DC spike, and a
  software mixer covers moves of up to 100 kHz without retuning the dongle.
- **Demodulation:** an 81-tap anti-alias filter, decimation to 48 kHz, and a
  255-tap complex band-pass filter for 200 Hz to 200 Hz plus the bandwidth on
  one sideband. The opposite sideband is 60 dB down in the tests.
- **AGC:** uses the profile's SDR++ attack and decay figures.
- **Threads:** a reader thread, a DSP thread and a bounded queue between them.
  Blocks that don't fit in the queue are dropped and counted in `/api/status`.
- **SIGHUP** re-reads the profile.

## Measured on the uConsole (CM4, 2026-11-05)

| | CPU |
|---|---|
| 30 s of 240 kS/s IQ from a file, as fast as it runs | 5.0 CPU-s, about 16% of one core in real time, including startup |
| DSP thread, live, while JS8Call decodes | 25% of one core |
| SDR++Brown today, for comparison | about 220% |

numpy's OpenBLAS thread pool must be held to one thread, and rxd does this
itself. With the default pool a live run sat at 131% CPU.

A real dongle and a live decode comparison against SDR++ are not tested yet.

## Tests

```
python3 -m unittest discover rxd
```

They cover the DSP (block-invariant filtering, sideband rejection, the
passband, AM, WFM and AGC), retuning, the rigctl and HTTP protocols, and
profile loading. They pass on Python 3.11, numpy 1.24 and scipy 1.10 (the
uConsole's) both locally and on the device. Separately, `rigctl -m 2` from
Hamlib 4.5.4 on the device talks to rxd without errors.

## Not done yet

- **`radio` integration:** an `"engine": "rxd"` profile key, and setup.sh
  installing rxd to /usr/local/bin.
- **JS8Call:** switching it to Hamlib NET rigctl.
- **I/Q correction:** none yet. A strong signal mirrored around the tuner
  frequency could leak in at roughly -30 to -40 dB.
- **Signal strength:** `l STRENGTH` is uncalibrated: dBFS + 73.

## Ideas for later (Win, 2026-11-06)

- **Band conditions and automatic band choice.**
  - With internet: fetch current conditions (perhaps from a small page of our own, possibly inside Rally) and default the digital modes to the bands likely to hear the most for the time of day. The UI can override it.
  - Without internet: auto-scan the candidate frequencies, score each from a slot or two of decodes and the noise floor, and learn what works best at each hour.
  - Catch: the antenna is cut for 40 m today.
- **A map in the web UI** of JS8 and WSPR stations, plotted at the grid-square centre, using the map tiles already downloaded for the mesh clients so it works offline.
- **Auto-adjusting audio quality:**
  - automatic RF gain on the dongle's gain steps, with a manual override;
  - per-mode filtering, AGC and noise handling;
  - level normalisation for listeners.
