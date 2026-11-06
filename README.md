# rxd

A small RTL-SDR receiver daemon in Python, the receiver behind the `radio`
profiles. It reads the same profile JSON as `radio`, opens the dongle by
serial, demodulates one channel, plays it to a PipeWire sink, and decodes JS8
and WSPR from it. It is controlled over HTTP, on 127.0.0.1 by default:

- **8073, HTTP JSON.** `GET /api/status`, and `POST /api/tune` with any of
  `freq`, `mode`, `bandwidth`, `gain_db` and `volume`. Status also reports
  `profile`, `gains` (the dongle's gain steps in dB) and `station` (the
  callsign and grid from `RXD_CALL` and `RXD_GRID`, with `missing` saying
  what to set when either isn't). This is what `radio save` reads. `GET /api/spectrum` gives the whole captured band
  about 5 times a second: 512 dB bins with `start_hz`, `stop_hz`, `dial_hz`
  and the demodulated `passband`, all on the dial's scale. `GET /` serves the
  browser UI from `$RXD_WEB`, `/usr/local/share/rxd/index.html` or
  `web/index.html` beside rxd.py. The listen address defaults to `$RXD_HTTP`
  when there's no `--http` flag.

Modes: USB, LSB, AM, FM and WFM (mono, no RDS). A profile with
`"decoder": "js8"` decodes JS8 (below). One with `"decoder": "wspr"` decodes
WSPR: each two-minute slot of its audio goes
to `wsprd`, the spots to `GET /api/wspr?after=SEQ` and, unless
`"wspr_report": false`, to WSPRnet (see docs/radio.md). Status reports the
decoder (`decoder`: js8, wspr or null) and the WSPR cycle and reporting
state (`wspr`). Needs only `python3-numpy` and
`python3-scipy`, already on the uConsole; librtlsdr and libpulse-simple are
loaded with ctypes.

```
rxd js8                       # ~/.config/rx/radio/js8.json, to its sink
rxd js8 --audio stdout | ...  # raw s16le mono 48 kHz
rxd js8 --iq synth --audio null --http 127.0.0.1:8083
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

A profile's `frequency` is the dial: on USB, a signal 1500 Hz up in the
audio is 1500 Hz above it. `rtl.ppm` corrects the dongle's crystal.

## JS8, decoded by rxd (rxd/js8.py)

A JS8 profile (`"decoder": "js8"`) decodes JS8 in rxd. Receive only:
nothing here transmits.

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
  `~/.local/share/rxd/js8/`, in JS8's usual log formats, each rolled over to
  `.1` past 8 MB. `/api/js8` tails that DIRECTED.TXT, so its lines keep their
  shape, and history survives a restart.
- **Status:** `js8_decoder`: state (waiting, recording or decoding), the last
  slot's decode, message, CPU and wall-clock counts, skipped slots, the
  error, and whether the JSC table is installed. It is null on other
  profiles.

Two places where rxd's lines differ from JS8Call's DIRECTED.TXT, both on
purpose:
- The offset column is the sending station's. JS8Call writes its own
  transmit offset there.
- Heartbeats and CQs keep their grid, for the station map.

### Compressed text and the JSC table

Heartbeats, CQs, directed commands and SNR reports, callsigns (compound ones
too), grids and uncompressed (Huffman) data frames all decode without help.
Most free text is sent compressed with JS8's "JSC" code, which needs its
262,144-word table. In 1,135 frames heard on 40 m, 214 (19%) needed it;
every message with free text contains such frames. The table is JS8Call's
(GPL-3), so rx doesn't ship it, and setup doesn't fetch it. Without it, the
parts of a message that need it show as `……`. Given JS8Call's `jsc_map.cpp`
(`JSC_SOURCE_URL` in js8.py, pinned by size and SHA-256),

```
python3 js8.py --jsc-import jsc_map.cpp
```

writes it to `~/.local/share/rxd/js8/jsc_map.txt` (`$RXD_JSC` overrides),
where rxd finds it at its next start.

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
- **Ordered-statistics decoding** (Fossorier and Lin, 1995) is a second try
  for the few strongest candidates that belief propagation fails on.

### Checked against JS8Call (2026-11-06, 40 m, 7.078 MHz)

JS8Call 2.2.0, the desktop application, and rxd decoded the same audio for
17 minutes, 13:03 to 13:20 UTC, rxd live and JS8Call from rxd's audio output.
JS8Call stamps each ALL.TXT decode with the start of the slot *before* the
one it was sent in. Its DIRECTED.TXT write times and the synced clock show
rxd's slot is the right one, so frames were compared a slot apart.

| | JS8Call | rxd | Both | Recall |
|---|---|---|---|---|
| Frames, live (an earlier build) | 68 | 60 | 58 | 85% |
| Frames, the same audio decoded again (this build) | 67 | 64 | 61 | 91% |
| Messages, as in DIRECTED.TXT (this build) | 36 | 38 | 33 | 92% |

- **Frames only rxd decoded** (3 to 5) are real: heartbeat replies JS8Call
  missed, and frames JS8Call caught in a neighbouring slot.
- **False decodes:** none seen, and none in 200 slots of pure noise.
- **What rxd misses** is mostly at -18 to -23 dB, signals more than 1 s
  early, or symbols hit at the slot's edges.
- **Agreement:** rxd's SNR reads about 2 dB above JS8Call's, and its dt
  0.2 s below.

### CPU on the uConsole

CM4, Python 3.11.2, numpy 1.24.2, scipy 1.10.1, at nice 10, from the 13:03
recording:

| Submode | Mean (max) per slot | Share of one core |
|---|---|---|
| normal | 2.35 s (2.91 s) | 15.6% |
| fast | 1.28 s (2.55 s) | 12.8% |
| turbo | 1.09 s (1.23 s) | 18.2% |
| slow | 1.32 s (1.52 s) | 4.4% |

Live, decoding normal, fast and slow with an earlier and cheaper build, the
decoder process used 25% of one core and rxd 17% (its DSP included). For
comparison, JS8Call on the same audio used 63% for its `js8` decoder and
12.5% for its window.

On synthetic slots with white noise, decoding is certain down to -18 dB SNR
in 2500 Hz and about half the time at -20 dB.

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

## Automatic gain and audio clean-up

- **Auto RF gain** (`AutoGain`, on unless a profile sets `rtl.gain_auto: false`)
  steps through the dongle's gain table from the raw IQ: clipped bytes, RMS
  and the band's noise floor from the spectrum, against the 8-bit ADC's own
  floor. It aims for a floor 6-13 dB over the ADC's (the antenna sets the
  noise, not the ADC) without clipping or an RMS over -10 dBFS. After a start
  or a retune of a MHz or more it settles in a few jumps, then moves at most
  every 5 s; under JS8 or WSPR only in the gap between slots, unless the
  signal overloads or is far too low. `POST /api/tune {"gain_db": X}` sets it
  by hand and turns auto off; `{"gain_auto": true}` turns it back on. Status
  has `gain_auto`, `gain_db` (the gain in use) and `gain_loop` (state,
  reason, floor, margin, RMS, clip fraction, steps).
- **Audio clean-up** (`AudioFx`) is for listening only: the uConsole speaker
  and the phone/TUI stream. The JS8 and WSPR decoders get the audio
  untouched. Stages, each an `audio_*` tune
  field and reported under status `audio`: `audio_squelch` (an NFM noise
  squelch, off while a level squelch is set), `audio_filter` (hum and DC; on
  NFM a 300-3500 Hz voice band, which also takes out sub-audible tones and
  the discriminator's hiss), `audio_nr` (spectral subtraction; `null` means by
  mode, on except WFM), `audio_norm` (loudness to -20 dBFS RMS, -20 to +12 dB)
  and `audio_limit` (soft limiter). `audio_auto: false` bypasses them all. A
  profile may carry them as `"audio": {"nr": false, ...}`.

## Measured on the uConsole (CM4, 2026-11-05)

| | CPU |
|---|---|
| 30 s of 240 kS/s IQ from a file, as fast as it runs | 5.0 CPU-s, about 16% of one core in real time, including startup |
| DSP thread, live, on a JS8 profile (before rxd decoded JS8) | 25% of one core |
| SDR++Brown today, for comparison | about 220% |

numpy's OpenBLAS thread pool must be held to one thread, and rxd does this
itself. With the default pool a live run sat at 131% CPU.

A real dongle and a live decode comparison against SDR++ are not tested yet.

## Tests

```
python3 -m unittest discover rxd
```

They cover the DSP (block-invariant filtering, sideband rejection, the
passband, AM, WFM and AGC), retuning, the HTTP API, and profile loading. test_js8 covers the JS8 decoder on synthesized slots (all
four speeds, several SNRs, offsets and times, overlapping signals, noise
alone), frame unpacking, message assembly, slot recording and the decoder
process. Real recordings hold other stations' callsigns, so they stay out
of git: `RXD_JS8_REAL=DIR` runs the decoder over a directory of them. They
pass on Python 3.11, numpy 1.24 and scipy 1.10 (the uConsole's) both locally
and on the device.

## Not done yet

- **I/Q correction:** none yet. A strong signal mirrored around the tuner
  frequency could leak in at roughly -30 to -40 dB.
- **Signal strength:** `l STRENGTH` is uncalibrated: dBFS + 73.

## Band plan

`BandPlanner` picks the JS8 or WSPR band when it's on Auto; docs/radio.md
says how it chooses. `GET /api/bands` gives its settings, its status and
every band profile with its score; `POST /api/bands` takes `plan` (auto or
manual), `mode` (wspr or js8), `bands` (a list, or null for the default),
`dwell_min` (2 to 60), `every_min` (10 to 240), `cross_side`, and
`{"scan": true}` to scan now. Status carries the same status as
`band_plan`. It runs only on the dongle (`RXD_BANDS=on` forces it, `off`
stops it). Its URLs can be pointed elsewhere with `RXD_HAMQSL`,
`RXD_SWPC_FLUX` and `RXD_SWPC_KP`, its files with `RXD_BANDS_CONF` and
`RXD_BANDS_DIR`.

## Ideas for later (Win, 2026-11-06)

- **Band conditions and automatic band choice:** done as the band plan
  (above). The antenna is cut for 40 m today.
- **A map in the web UI** of JS8 and WSPR stations, plotted at the grid-square centre, using the map tiles already downloaded for the mesh clients so it works offline.
- **Auto-adjusting audio quality:** done 2026-11-06, see "Automatic gain and
  audio clean-up". Not done: per-mode AGC tuning.
