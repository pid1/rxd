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

## JS8, decoded by rxd (rxd/js8.py)

A JS8 profile with `"js8_engine": "rxd"` (or `RXD_JS8_ENGINE=rxd`) decodes
JS8 in rxd, with no JS8Call. The default, `"js8call"`, leaves it to JS8Call
as before. Receive only: nothing here transmits.

- **Recording:** like WSPR, from the audio before squelch and volume,
  resampled to 12 kHz, in slots by the system clock with up to 2.5 s of
  audio either side. A slot is dropped if the dial moves or the audio stalls
  during it.
- **Decoding:** in a separate `js8.py --serve` process at nice 10, so a busy
  band can't starve the DSP thread. `"js8_submodes"` picks the speeds: normal
  (15 s), fast (10 s), turbo (6 s) and slow (30 s). The default is normal,
  fast and slow.
- **Stages:** candidates from a quarter-symbol spectrogram scored against
  the three Costas arrays; each one mixed to baseband from one FFT of the
  slot, then timed to 1/24 symbol and tuned to 1/16 tone; Rician soft bits;
  belief propagation on LDPC(174,87), all candidates at once; CRC-12. Decoded
  signals are subtracted and the slot is searched again.
- **Output:** `ALL.TXT` (frames) and `DIRECTED.TXT` (messages) under
  `~/.local/share/rxd/js8/`, in JS8Call's formats, each rolled over to `.1`
  past 8 MB. `/api/js8` tails that DIRECTED.TXT, so its lines keep their
  shape, and history survives a restart.
- **Status:** `js8_decoder`: state (waiting, recording or decoding), the last
  slot's decode, message, CPU and wall-clock counts, skipped slots, the
  error, and whether the JSC table is installed. For a JS8 profile left to
  JS8Call it is `{"engine": "js8call"}`.

Two places where rxd's lines differ from JS8Call's DIRECTED.TXT, both on
purpose:
- The offset column is the sending station's. JS8Call writes its own
  transmit offset there.
- Heartbeats and CQs keep their grid, for the station map.

### Compressed text and the JSC table

Heartbeats, CQs, directed commands and SNR reports, callsigns (compound ones
too), grids and uncompressed (Huffman) data frames all decode without help.
Most free text is sent compressed with JS8Call's "JSC" code, which needs its
262,144-word table. In the 1,135 frames of the box's ALL.TXT, 214 (19%)
needed it; every message with free text contains such frames. The table is
JS8Call's (GPL-3), so rxd doesn't ship it. Instead,

```
python3 js8.py --jsc-import /usr/bin/js8call
```

reads it out of the user's own installed js8call program into
`~/.local/share/rxd/js8/jsc_map.txt` (`$RXD_JSC` overrides). Without it, the
parts of a message that need it show as `……`.

### Sources and licences

No JS8Call or WSJT-X code is used or translated.
- **ft8_lib** by Kārlis Goba (MIT, github.com/kgoba/ft8_lib, its 2018 FT8 v1
  commits) is the source for the (174,87) generator matrix, the bit order on
  the air and the CRC-12 polynomial.
- **The sparse parity checks** were derived from that generator here, as the
  87 lowest-weight words of the code's dual. test_js8 checks them.
- **JS8's own protocol constants** are facts about the protocol, taken from
  JS8Call 2.2's behaviour and its GPL source as the only specification: the
  Costas arrays, symbol lengths and start delays, the CRC's XOR with 42, the
  frame layouts, the callsign, group, command and CQ tables, and the Huffman
  code. They were checked by decoding real signals off the air and by
  reproducing JS8Call's text for 1,129 of 1,135 frames in its ALL.TXT. The
  other 6 differ only in padding.
- **The JSC decompressor** is (s,c)-dense coding (Brisaboa et al., 2003),
  with JS8's parameters: 4-bit digits, s = 7, c = 9, and a space flag after
  each word.
- **Ordered-statistics decoding** (Fossorier and Lin) was tried and dropped:
  it gained almost nothing over belief propagation here, at many times the
  CPU.

### Measured

On the uConsole (CM4, Python 3.11.2, numpy 1.24.2, scipy 1.10.1), from 18
minutes of 40 m audio recorded at 7.078 MHz, 2026-11-06. CPU per slot at
nice 10, as a share of one core:

| Submode | Mean (max) per slot | Share of one core |
|---|---|---|
| normal | 1.16 s (1.95 s) | 7.7% |
| fast | 0.96 s (1.88 s) | 9.6% |
| turbo | 0.84 s (0.88 s) | 14.1% |
| slow | 1.00 s (1.16 s) | 3.3% |

On synthetic slots with white noise, decoding is certain down to -18 dB SNR
in 2500 Hz and about half the time at -20 dB, with no false decodes in
noise.

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
profile loading. test_js8 covers the JS8 decoder on synthesized slots (all
four speeds, several SNRs, offsets and times, overlapping signals, noise
alone), frame unpacking, message assembly, slot recording and the decoder
process. Real recordings hold other stations' callsigns, so they stay out
of git: `RXD_JS8_REAL=DIR` runs the decoder over a directory of them. They pass on Python 3.11, numpy 1.24 and scipy 1.10 (the
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
