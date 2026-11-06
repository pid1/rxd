# rxd

A small RTL-SDR receiver daemon in Python. rxd opens one RTL-SDR, demodulates
one channel (USB, LSB, AM, FM, or WFM in mono), plays it to a PipeWire or
PulseAudio sink, and decodes **JS8** (with its own decoder) and **WSPR** (with
`wsprd`) from it. It reports WSPR spots to WSPRnet, picks the band itself from
band conditions and what it has heard, and maps the stations it hears on
offline tiles. Receive only: nothing here transmits.

It runs at about a fifth of one Raspberry Pi CM4 core while decoding JS8, and
needs only Python 3.11 with numpy and scipy; librtlsdr and libpulse are loaded
with ctypes.

- **`rxd PROFILE`** is the daemon, one profile at a time, as the systemd user
  unit `rxd@PROFILE`.
- **`radio`** switches profiles (`radio js8`, `radio wspr-20m`), saves the
  tuning, and resumes each profile where it was left.
- **An HTTP API** controls it: [docs/API.md](docs/API.md), version 1, under
  `/api/v1/`.

## Clients

rxd comes with two **reference clients**:

- **The browser UI** (`clients/web/index.html`), served by rxd at
  `http://HOST:8073/`: one self-contained page, no external requests, with
  Day, Dark and Night (red) themes.
- **`rxd-tui`** (`clients/tui/rxd_tui.py`), for a terminal.

They are references, not the only way in: everything they do goes through the
documented [HTTP API](docs/API.md). Use either, use any other client, or build
your own against the API. Both check the API version rxd reports and say
plainly when they reach an rxd they don't understand.

## Install

From a release, verified by its checksum:

```bash
v=0.4.0
curl -fLO https://github.com/pid1/rxd/releases/download/v$v/rxd-$v.tar.gz
curl -fLO https://github.com/pid1/rxd/releases/download/v$v/rxd-$v.tar.gz.sha256
sha256sum -c rxd-$v.tar.gz.sha256 && tar xzf rxd-$v.tar.gz
sudo python3 rxd-$v/install.py --prefix /usr/local --user "$USER"
```

Then write the [station file](docs/station.md) and run `radio js8`.
[docs/install.md](docs/install.md) has the details: what goes where, the
data it fetches (JS8's word table and the callsign database), and running
from a checkout.

## The station file

Everything that makes one receiver different from another is in
`/etc/rxd/station.json`, not in the code: the callsign and grid, the RTL-SDR's
serial number, the upconverter (a Ham It Up, say: its offset, and whether its
switch is one a person flips), the audio sink, and the station's own profiles
(local repeaters, broadcast stations). Every key is optional; with no file rxd
is a generic receiver. With a GPS behind gpsd, rxd takes the grid from it
when it has a steady fix. [docs/station.md](docs/station.md) has the schema.

## Who owns what

rxd is the receiver and nothing else. Setting up a particular machine
belongs to that machine's own provisioning, which for the ClockworkPi
uConsole it was written on is [pid1/rx](https://github.com/pid1/rx).

| rxd (this repository) | the machine (pid1/rx, for the uConsole) |
|---|---|
| rxd, `radio`, the browser UI, `rxd-tui`, js8.py, callsign_db.py | the station file and `/etc/rxd/rxd.env` it writes |
| `install.py`: the code, the `rxd@.service` user unit, radio's completion | installing a pinned rxd release, checked by SHA-256, and caching it for offline re-runs |
| the JSC word table fetch (pinned commit and SHA-256) and the callsign database download, as the user | the firewall (port 8073 to the LAN and tailnet only) |
| the weekly callsign database build and its `data-callsigns` release | wsprd's package (`wsjtx`) and its apt pin, chrony and GPS, the RTL-SDR's udev rules and DVB blacklist |
| the HTTP API and its versions | i3, the mesh radios (`radio mesh` comes from rx as a `radio-mesh` extension), offline map tiles, everything else |

`radio` takes extensions: `radio CMD ARGS...` runs `radio-CMD` from PATH
when CMD isn't a profile, so a machine can add commands of its own.

## Layout

```
rxd/            the daemon and its modules: rxd.py, js8.py, callsign_db.py,
                radio.py, station.py, location.py
clients/web/    the browser UI (one file)
clients/tui/    rxd-tui
completions/    radio's bash completion
install.py      the installer
scripts/        release.py (the release tarball), version.sh
tests/          python3 -m unittest discover tests
docs/           API.md, station.md, install.md
```

rxd's version is `VERSION` in `rxd/rxd.py`, the one place it's set: the
status reports it, and the release workflow refuses a tag that doesn't match.

## Releases

A tag `vX.Y.Z` runs `.github/workflows/release.yml`: the tests, then
`scripts/release.py`, which builds `rxd-X.Y.Z.tar.gz` and its `.sha256`, and
publishes them as the GitHub release. The tarball is reproducible: built from
git's files at the tag, never the working tree, with fixed owners, modes,
times and gzip header, so `python3 scripts/release.py --ref vX.Y.Z` anywhere
gives the same bytes as the published one.

## Radio

`radio` is one command per setup. `radio NAME` stops whatever is running and
brings NAME up ready to listen. Profile names tab-complete.

```bash
radio                 # list; * marks the active profile
radio wspr-20m        # switch
radio off             # stop rxd
radio resume          # bring the active profile back (at login, say)
radio save            # keep the current tuning in the active profile
radio save NAME       # ... or as a new profile
radio reset NAME      # back to the default
radio show NAME       # print a profile
radio help
```

- **A profile is tuning only:** the dial frequency, demodulator and its
  settings, audio sink and volume, the RTL-SDR's settings, and what rxd
  decodes (`"decoder": "js8"` or `"wspr"`). The decoders take the audio
  before the volume, so the volume is only the speaker's.
- **Each one resumes:** switching saves the profile being left, into
  `~/.config/rxd/radio/NAME.json`. The defaults are in `radio` and the station
  file, and are only starting points.
- **rxd reads only NAME.json:** `radio` writes the merged profile there
  before it starts `rxd@NAME`. Edit it while it plays and
  `systemctl --user kill -s HUP rxd@NAME` re-reads it.
- **The upconverter:** profiles use `"offset": "auto"`, so below the
  station's `upconverter.below_hz` the signal goes through the upconverter
  and above it not, including when you retune live. `"Ham-It-Up"` or `"None"`
  pins it. If its switch is one a person flips, `radio` says when to flip it.

### JS8 and WSPR on every band

One JS8 and one WSPR profile per band, on the standard dial (USB). 40 m keeps
the names `js8` and `wspr`; `js8-40m` and `wspr-40m` are aliases. The dials
were checked 2026-11-06 against JS8Call-improved 3.0.3's default list
(`JS8_Main/FrequencyList.cpp`) and WSJT-X's (`models/FrequencyList.cpp`).

| Band | JS8 dial, MHz | WSPR dial, MHz |
|---|---|---|
| 2190 m | — | 0.136000 |
| 630 m | — | 0.474200 |
| 160 m | 1.843500 | 1.836600 |
| 80 m | 3.578000 | 3.568600 |
| 60 m | 5.363000 | 5.364700 |
| 40 m | 7.078000 (`js8`) | 7.038600 (`wspr`) |
| 30 m | 10.130000 | 10.138700 |
| 20 m | 14.078000 | 14.095600 |
| 17 m | 18.104000 | 18.104600 |
| 15 m | 21.078000 | 21.094600 |
| 12 m | 24.922000 | 24.924600 |
| 10 m | 28.078000 | 28.124600 |
| 6 m | 50.318000 | 50.293000 |
| 2 m | 144.178000 | 144.489000 |

- Older JS8 lists have 1.842 on 160 m and no 60 m; the current one moved
  160 m to 1.8435 and added 5.363.
- WSJT-X lists no 60 m WSPR. 5.3647 is where WSPRnet's 60 m spots are: in
  the first week of November 2026 wspr.live had about 552,000 there and
  190,000 on the older 5.2872.

Also built in: `ghostnet` (JS8 GhostNet, 7.107 MHz), `vhf` (146.52 MHz FM),
`uhf` (446.0 MHz FM) and `noaa` (162.55 MHz FM). Local ones go in the station
file.

### Band plan

rxd chooses the JS8 or WSPR band itself when its **band plan** is on Auto
(the Digital view of the browser UI, `a` in `rxd-tui`, or
`POST /api/v1/bands {"plan": "auto"}`). On Manual it stays put. Picking a
band yourself, from a picker or with `radio NAME`, sets it to Manual.

- **With internet** it ranks the bands by N0NBH's calculated band conditions
  from hamqsl.com (SFI, A and K, day and night per band group), NOAA SWPC's
  solar flux and K index if hamqsl doesn't answer. It fetches at most every
  45 minutes, with a 10 s timeout, and caches them on disk. It combines them
  with the sun at the station's grid (day, gray line or night) and with what
  rxd has heard on each band at this hour.
- **Without internet** it scans the bands it hasn't heard at this hour, at
  least two WSPR slots or the scan time each (6 minutes by default), keeps
  the counts, and then picks the band that heard the most. What it hears,
  per profile and UTC hour, is kept in `~/.local/share/rxd/bands.json` and
  counts for half as much every 14 days.
- **Bands:** every band is enabled by default, 2190 m and 630 m included
  (Win's call, 2026-11-06: no assumptions about antennas); turn off the ones
  without an antenna under *Bands and timing*.
- **An upconverter with a manual switch:** Auto only uses bands on the side
  the switch is on now (the side the running profile needs), unless *Allow,
  I'll flip it* (`cross_side`) is set.
- **Switching** goes through `radio NAME`. A switch waits for a WSPR slot's
  decode to finish, and stays put unless another band scores 15 % better.
  The re-check interval is 30 minutes by default.
- **State:** settings in `~/.config/rxd/bands.json`; where the plan is in
  `~/.local/share/rxd/bandplan.json`; the last conditions in
  `~/.local/share/rxd/conditions.json`. `RXD_BANDS=off` in `/etc/rxd/rxd.env`
  turns it off; `RXD_HAMQSL`, `RXD_SWPC_FLUX` and `RXD_SWPC_KP` point its
  fetches elsewhere.

### WSPR

A WSPR profile records each two-minute slot from rxd's own audio, from the
even minute, and runs `wsprd` on it. Spots come from `GET /api/v1/wspr`, and
wsprd's `ALL_WSPR.TXT` is kept in `~/.local/share/rxd/wspr/`.

- **Reporting** to WSPRnet is on by default, as the station file's `call`
  at its grid (the GPS's, when it has one: docs/station.md); without both rxd decodes but doesn't report, and its status
  says why. Spots that can't be sent wait (up to 2000) for the next slot.
  `POST /api/v1/tune {"wspr_report": false}` turns it off, and `radio` saves
  the choice with the profile.

JS8 and WSPR profiles warn when chrony says the clock isn't synchronised:
both modes' slots tolerate about 2 s of clock error and fail silently past
it.

### rxd by hand

```
rxd js8                       # ~/.config/rxd/radio/js8.json, to its sink
rxd js8 --audio stdout | ...  # raw s16le mono 48 kHz
rxd js8 --iq FILE             # cu8 IQ from a file (or - for stdin), not a dongle
rxd js8 --iq synth --audio null --http 127.0.0.1:8083
                              # no dongle: noise plus a tone, for testing
rxd --version
```

A profile's `rtl.gain_db` wins over `rtl.gain`, SDR++'s index into the
tuner's gain table, and `rtl.ppm` corrects the dongle's crystal. SIGHUP
re-reads the profile; SIGINT or SIGTERM stops.

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
  `.1` past 8 MB. `/api/v1/js8` tails that DIRECTED.TXT, so its lines keep their
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
every message with free text contains such frames.

- **Origin and licence:** the table is JS8Call's, in its `jsc_map.cpp`
  (GPL-3, © 2018 Jordan Sherer), so nothing of it is in rxd.
- **Fetched at install:** install.py downloads that file from the original
  js8call repository at a fixed commit (v2.2.0-ga, `JSC_SOURCE_URL` in
  js8.py, 6,296,397 bytes), checks its SHA-256 (`JSC_SOURCE_SHA256`), and
  runs `js8.py --jsc-import-source FILE` as the user.
- **Imported:** the importer is rxd's own code. It writes
  `~/.local/share/rxd/js8/jsc_map.txt` (`$RXD_JSC` overrides), with the
  source's SHA-256 beside it in `jsc_map.txt.sha256`, so a later run skips
  the download when the table is current.
- **Safe to fail:** a failed download, check or parse keeps whatever table
  is already there. rxd reads the table when it starts, and works without
  one: those words show as `……`.
- **Same everywhere:** the same table, entry for entry, is in the js8call
  repository's later main and in JS8Call-improved v3.0.3
  (`JS8_JSC/JSC_map.cpp`, laid out differently), and JS8Call 2.2.0's own
  program carries it too.

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
  Blocks that don't fit in the queue are dropped and counted in the status.
- **SIGHUP** re-reads the profile.

## Automatic gain and audio clean-up

- **Auto RF gain** (`AutoGain`, on unless a profile sets `rtl.gain_auto: false`)
  steps through the dongle's gain table from the raw IQ: clipped bytes, RMS
  and the band's noise floor from the spectrum, against the 8-bit ADC's own
  floor. It aims for a floor 6-13 dB over the ADC's (the antenna sets the
  noise, not the ADC) without clipping or an RMS over -10 dBFS. After a start
  or a retune of a MHz or more it settles in a few jumps, then moves at most
  every 5 s; under JS8 or WSPR only in the gap between slots, unless the
  signal overloads or is far too low. `POST /api/v1/tune {"gain_db": X}` sets it
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

## Station map and the callsign database

The Digital view's map plots the JS8 and WSPR stations heard, at their grid
square's centre, on offline tiles. Nothing is looked up online while rxd
runs.

- **`GET /api/v1/stations?hours=24`** (up to 720): one station per mode and
  call, with `grid`, `lat`, `lon`, `km` from the station's grid, best and last SNR,
  and `src`. That is `heard` when the station sent its grid, else the
  callsign database's `addr`, `zip` or `country`. `me` is this station, and
  `no_grid` counts those that couldn't be placed.
- **Grids heard** come from WSPR spots and from JS8 heartbeats and CQs, read
  from rxd's JS8 logs (DIRECTED.TXT and every frame in ALL.TXT). They are
  kept in `~/.local/share/rxd/grids.json` (`$RXD_GRIDS`), so a station that
  sent its grid once is placed when heard again.
- **`GET /api/v1/tiles/Z/X/Y.png`** serves offline tiles: `$RXD_TILES`, or
  the tree meshtastic-ui keeps in `~/.portduino/default/maps/` (pid1/rx
  installs OpenStreetMap's, zoom 1-6). `GET /api/v1/tiles/info.json` says
  what's there. With none, the map draws grid squares only.
- **The callsign database** places stations that never send a grid:
  - US calls come from FCC licence addresses, geocoded at build time to a
    6-character grid, with ZIP centroids as the fallback.
  - Everyone else comes from cty.dat prefixes, at their country's centre.
  - It keeps only call, grid, precision and state or entity.
- **How it is built and installed:** a weekly GitHub workflow
  (`.github/workflows/callsigns.yml`) builds it with `callsign_db.py update`
  and publishes it as this repository's `data-callsigns` release.
  install.py runs `callsign_db.py install` as the user. That
  downloads it only when its published SHA-256 changed, checks it and
  swaps it into `~/.local/share/rxd/callsigns.sqlite` (`$RXD_CALLSIGNS`). A
  failure keeps the database already there.
- **`callsign_db.py lookup CALL`** says what it knows.

## Tests

```
python3 -m unittest discover tests
```

On Python 3.11 with numpy 1.24 and scipy 1.10 (Debian bookworm's), as CI
runs them. They cover the DSP (block-invariant filtering, sideband
rejection, the passband, AM, WFM and AGC), retuning, the HTTP API and its
versions, profiles and `radio`, the station file, the installer (into a
temp prefix, offline), the release tarball, the band plan, the map, both
clients, and the JS8 decoder on synthesized slots (all four speeds, several
SNRs, offsets and times, overlapping signals, noise alone). Real recordings
hold other stations' callsigns, so they stay out of git:
`RXD_JS8_REAL=DIR` runs the decoder over a directory of them. Nothing in the
tests reaches the network, and none of them reads the machine's own
station file.

## Not done yet

- **I/Q correction:** none yet. A strong signal mirrored around the tuner
  frequency could leak in at roughly -30 to -40 dB.
- **Signal strength:** `l STRENGTH` is uncalibrated: dBFS + 73.

## Licence

BSD-3-Clause ([LICENSE](LICENSE)). The JSC word table rxd fetches at install
is JS8Call's (GPL-3) and is never in this repository.
