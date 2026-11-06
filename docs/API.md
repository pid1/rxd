# rxd HTTP API, version 1

rxd is controlled over HTTP. Everything the two reference clients (the browser
UI and `rxd-tui`) do, they do through this API, so any client can do it too.

- **Address:** `--http HOST:PORT`, else `$RXD_HTTP`, else `127.0.0.1:8073`.
  `--http off` turns HTTP off.
- **No authentication.** Anyone who can reach the port can tune the radio.
  Keep it on 127.0.0.1, or behind a firewall that admits only your own
  network.
- **Bodies** are JSON (`Content-Type: application/json`), one object, answered
  with one JSON object and a newline. A field this page doesn't list may be
  added in a later rxd of the same API version; ignore what you don't know.
- **Times** are UTC. Frequencies are Hz, on the **dial's** scale: on USB a
  signal 1500 Hz up in the audio is 1500 Hz above the dial.

## Versions

- **The prefix:** every endpoint is under `/api/v1/`, the tiles too
  (`/api/v1/tiles/...`).
- **The old paths** (`/api/status`, `/tiles/...` and so on, without `v1`) are
  aliases kept for older clients. They answer exactly as `/api/v1/` does, and
  will go away in a later release. New clients use `/api/v1/`.
- **Another version** (`/api/v2/...`) answers `404` with
  `{"error": "this rxd speaks API version 1 (/api/v1/)", "api_version": 1}`.
- **In the status:** `api_version` (1) is the API, and `version` is rxd's own
  release (`0.3.0`). A client checks `api_version` and says plainly when it
  doesn't understand the rxd it reached. Both reference clients do.
- **The policy:**
  - A new endpoint or a new field in a response is not a new version.
  - A new optional request field isn't either.
  - Removing or renaming anything, changing what a field means or its type,
    or refusing a request that used to work is a new version: `/api/v2/`.
    rxd then keeps `/api/v1/` working for at least one release, and its
    changelog says when it goes.

## Errors

| Code | Means | Body |
|---|---|---|
| 200 | Done | the result |
| 202 | Accepted, happening now (a profile switch) | `{"switching": NAME}` |
| 400 | The request is wrong: an unknown field, a value out of range, bad JSON | `{"error": "why"}` |
| 404 | No such endpoint, tile or API version; or no web UI installed | `{"error": "why"}` (and `api_version` for a version) |
| 429 | Too many audio listeners (4) | `{"error": "4 clients are already listening"}` |
| 503 | Not available now: no spectrum yet, no band plan, `radio` couldn't be run | `{"error": "why"}` |

## GET /api/v1/status

What rxd is doing. Clients poll it, about twice a second.

| Field | Type | Meaning |
|---|---|---|
| `api_version` | int | This API's version: 1 |
| `version` | string | rxd's release, `X.Y.Z` |
| `profile` | string or null | The profile rxd is playing |
| `freq` | number | The dial, Hz |
| `mode` | string | `USB`, `LSB`, `AM`, `FM` or `WFM` |
| `bandwidth` | number | Hz |
| `gain_db` | number or null | The RF gain in use, dB |
| `gain_auto` | bool | Automatic RF gain on |
| `gain_loop` | object | Automatic gain's state: `state`, `reason`, `floor_db`, `margin_db`, `rms_dbfs`, `clip`, `steps`, `cpu` |
| `gains` | list of numbers | The tuner's gain steps, dB |
| `volume` | number | 0 to 4; the speaker only, never the decoders |
| `squelch` | number or null | Level squelch, dBFS; null is off |
| `squelch_open` | bool | Audio passing now |
| `audio` | object | Listening clean-up: `auto`, `nr`, `nr_setting` (null is by mode), `filter`, `norm`, `limit`, `squelch`, `active` (the stages running), `squelch_open`, `norm_gain_db`, `cpu` |
| `offset_mode` | string | `auto`, `Ham-It-Up` or `None` |
| `upconverter_hz` | number | The upconverter offset in use now; 0 when the signal goes direct |
| `upconvert_below_hz` | number | Where `auto` stops upconverting |
| `level_dbfs` | number or null | Channel level |
| `tuner_hz` | number | Where the tuner is, RF |
| `offset_hz` | number | The software mixer's offset from it |
| `sample_rate` | int or null | Tuner sample rate |
| `dsp_cpu` | number | Share of one core the DSP thread uses |
| `blocks`, `dropped` | int | IQ blocks processed, and dropped for lack of time |
| `error` | string or null | The last error, said plainly |
| `running` | bool | |
| `decoder` | string or null | `js8`, `wspr` or null |
| `station` | object | Below |
| `wspr` | object or null | WSPR decoding, below; null unless `decoder` is `wspr` |
| `js8_decoder` | object or null | JS8 decoding, below; null unless `decoder` is `js8` |
| `clock` | object | `synced` (bool, null when chrony can't say), `source` (`GPS`, `network` or null), `offset_s` |
| `band_plan` | object or null | The band plan's status (see `GET /api/v1/bands`); null when it isn't running |
| `listeners` | int | Audio streams open |

**`station`** comes from the station file ([station.md](station.md)):
`call` and `grid` (null when not set), `missing` (what to set, only when either
is null), `file` (the file read, or null for none), `upconverter` (null, or
`{"name", "hz", "below_hz", "switch"}`; `switch` true means a person must
flip it, so clients show which way it should be) and `problems` (what was
wrong with the file).

**`wspr`**: `state` (`waiting`, `recording` or `decoding`), `slot` and
`decoding` (ISO times), `last` (`{"slot", "spots"}`), `error`, and `report`:
`enabled`, `call`, `grid`, `sent`, `pending`, `online`, `error`, or `reason`
when it can't report.

**`js8_decoder`**: `engine` (`rxd`), `version`, `submodes`, `state`, `slot`,
`last`, `error`, `skipped`, `cpu_pct`, `jsc` (the JSC word table is
installed), `log` (the DIRECTED.TXT path).

## POST /api/v1/tune

Changes tuning now. Any of these fields; the answer is the new status.

| Field | Type | Limits |
|---|---|---|
| `freq` | number, Hz | 100 kHz to 1.7 GHz |
| `mode` | string | `USB`, `LSB`, `AM`, `FM`, `WFM` (any case) |
| `bandwidth` | number, Hz | over 0 |
| `gain_db` | number | dB, set on the dongle as its nearest gain step; turns automatic gain off |
| `gain_auto` | bool | |
| `volume` | number | 0 to 4 |
| `squelch` | number or null | -150 to 0 dBFS; null turns it off |
| `wspr_report` | bool | report WSPR spots to WSPRnet |
| `audio_auto`, `audio_filter`, `audio_norm`, `audio_limit`, `audio_squelch` | bool | |
| `audio_nr` | bool or null | null: by mode (on, except WFM) |

An unknown field, or a value out of range, is `400` and changes nothing.
`radio save` keeps the tuning in the profile.

## GET /api/v1/spectrum

The captured band, about 5 times a second: `bins` (512 numbers, dB),
`start_hz` and `stop_hz` (the band's edges), `dial_hz`, `passband`
(`[low, high]` Hz, what is demodulated), `seq`. All on the dial's scale.
`503` until the first one is ready.

## GET /api/v1/profiles

`radio`'s profiles: `{"active": NAME, "profiles": [...]}`, each with `name`,
`description`, `freq`, `mode`, `upconvert` (bool: through the upconverter),
`js8` (bool), `decoder`, `band` (`"40m"` or null), `plan` (bool: the band plan
may pick it) and `saved` (bool: has saved state). `503` when `radio` can't be
run.

## POST /api/v1/profile

`{"name": NAME}`: switch to profile NAME, through `radio NAME`. rxd stops and
the next one starts, so the answer is `202 {"switching": NAME}` and the
status stops answering for a few seconds. `400` for a name `radio` doesn't
list. A switch made this way puts the band plan on Manual.

## GET /api/v1/js8?after=SEQ

JS8 messages decoded, from DIRECTED.TXT: `{"seq": N, "lines": [...]}`, each
line `{"seq", "time", "freq", "offset", "snr", "from", "to", "text"}`
(`time` is `YYYY-MM-DD HH:MM:SS` UTC, `freq` the dial). With `after` 0 or
missing, the last 200; else those numbered over `after`. Poll with the `seq`
you got back. `400` if `after` isn't a number.

## GET /api/v1/wspr?after=SEQ

WSPR spots: `{"seq": N, "lines": [...]}`, each `{"seq", "time" (ISO),
"snr", "dt", "freq" (Hz, the signal), "drift", "call", "grid", "dbm", "km",
"text"}`. On another decoder, `{"seq": 0, "lines": [], "decoder": ...}`.

## GET /api/v1/audio?rate=R

The demodulated audio as it plays, after squelch and clean-up but before the
volume: a stream that runs until either side closes it.

- **Rates:** 12000, 24000 (default) or 48000 Hz; another is `400`.
- **Format:** raw signed 16-bit little-endian, mono, no header
  (`Content-Type: application/octet-stream`, `X-Audio-Rate: R`,
  `X-Audio-Format: s16le; channels=1`). HTTP/1.0: no chunking, no length.
- **Listeners:** at most 4 (`429` past that). A client that stops reading
  for 10 s is dropped.

## GET /api/v1/bands, POST /api/v1/bands

The band plan, which picks the JS8 or WSPR band on Auto. `503` when it isn't
running (it runs only on a real dongle; `RXD_BANDS=on` forces it, `off` stops
it).

`GET` gives `config` (the settings below), `status` (as in the status's
`band_plan`: `plan`, `mode`, `phase`, `profile`, `band`, `reason`, `scan`,
`next_at`, `switching_to`, `bands`, `default_bands`, `dwell_min`,
`every_min`, `cross_side`, `side`, `sun`, `conditions`, `conditions_error`),
`all_bands`, and `profiles`: every band profile with `name`, `mode`, `band`,
`freq`, `upconvert`, `enabled`, `eligible`, `why_not`, `score`, `basis` and
`heard`.

`POST` takes any of these and answers like `GET`:

| Field | Type | |
|---|---|---|
| `plan` | `auto` or `manual` | |
| `mode` | `wspr` or `js8` | |
| `bands` | list of bands, or null | null is the default: every band |
| `dwell_min` | number | 2 to 60: time on each band when scanning |
| `every_min` | number | 10 to 240: how often it looks for a better band |
| `cross_side` | bool | may pick a band on the other side of an upconverter whose switch is manual |
| `scan` | true | scan now |

## GET /api/v1/stations?hours=H

Stations heard by JS8 and WSPR in the last H hours (default 24, at most 720),
for the map: `{"me": {"call", "grid", "lat", "lon"} or null, "hours": H,
"stations": [...], "no_grid": {"js8": N, "wspr": N}}`. Each station:
`call`, `mode`, `grid`, `lat`, `lon` (its grid square's centre), `km` from
this station (null when placed only by country), `snr` (best), `last` and
`last_snr`, `freq`, `dbm` (WSPR), `src` (`heard`: it sent its grid; `addr`,
`zip` or `country`: from the callsign database) and `region`. `400` for a bad
`hours`.

## GET /api/v1/tiles/Z/X/Y.png

Offline map tiles (Web Mercator z/x/y; `.png`, `.jpg` or `.webp`), from
`$RXD_TILES` or `~/.portduino/default/maps/<style>/`. Nothing is fetched from
the internet. `404` for a tile that isn't there, or any path outside the tile
tree. **`GET /api/v1/tiles/info.json`**: `available`, `zooms`, `min`, `max`,
`attribution`.

## GET /

The browser UI (`clients/web/index.html`), from `$RXD_WEB` or the copy
installed beside rxd. `404` when there is none. It is one file with no
external requests.

## Example

```bash
curl -s 127.0.0.1:8073/api/v1/status | python3 -m json.tool
curl -s -XPOST -d '{"freq": 7078000, "mode": "USB"}' 127.0.0.1:8073/api/v1/tune
curl -s '127.0.0.1:8073/api/v1/js8?after=0'
curl -s '127.0.0.1:8073/api/v1/audio?rate=24000' | aplay -f S16_LE -r 24000 -c 1
```
