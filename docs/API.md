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
| 409 | Not without a person: a profile across the upconverter's switch needs it confirmed; or there's no switch to confirm | `{"error": "why"}` (and `needs`, `position` for a profile) |
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
| `upconverter` | object or null | The upconverter's switch, below; null when the station has no upconverter |
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
| `hd` | object | HD Radio on a WFM profile, below |

**`upconverter`**: `name`, `switch` (true: a person flips it), `needs`
(`upconvert` or `bypass`: the side the tuning needs now), `position` (the
switch as a person last confirmed it: `upconvert`, `bypass`, or `unknown`
when nobody has since the machine started; null with no switch),
`confirmed_at` (ISO time, or null), `why` (why it's unknown, or null) and
`ok` (it's confirmed on the side needed, or there's no switch). Clients show
it in words ("Ham It Up: ✓ BYPASS (confirmed 14:02)") and, while it's
`unknown`, ask which way it is. See [station.md](station.md#the-switch).

**`station`** comes from the station file ([station.md](station.md)) and the
GPS: `call` and `grid` (null when not set), `missing` (what to set, only when
either is null), `file` (the file read, or null for none), `upconverter` (null,
or `{"name", "hz", "below_hz", "switch"}`; `switch` true means a person must
flip it, so clients show which way it should be) and `problems` (what was
wrong with the file, or with the saved GPS grid). Where `grid` comes from:

| Field | Type | Meaning |
|---|---|---|
| `grid` | string or null | The grid in use: WSPRnet's `rgrid`, the map's centre and distances, and the band plan's sun |
| `grid_source` | string or null | `gps` (a live fix rxd accepted), `gps-saved` (the last one, saved), `station` (the station file's `grid`) or null (none) |
| `grid_reason` | string | One sentence for the clients to show: "EN52lk from GPS (10 satellites)", "EN52lk from station config: GPS has no fix". When a GPS grid differs from the station file's it ends "; station config says EM12" |
| `default_grid` | string or null | The station file's `grid` (`$RXD_GRID` wins) |
| `saved_grid` | string or null | The last GPS grid rxd accepted, kept across restarts |
| `gps` | object | The GPS, below |

**`station.gps`**: `state` (`fix`, `no fix`, `rejected`, `off` or
`unavailable`), `reason` (a plain sentence: "GPS position rejected: the
receiver's NMEA reports a placeholder position; using its binary fix
instead"), `satellites` (used, or null), `accuracy_m` (whole metres, or null),
`source` (`ubx`, the receiver's binary fix, or `tpv`, gpsd's NMEA-derived one,
or null) and `gpsd` (HOST:PORT, or null when off). `fix` with no `gps`
`grid_source` means the fix is new: it's used once it holds one grid square
for 3 minutes. No precise position is ever in the status: only grids.

**`wspr`**: `state` (`waiting`, `recording` or `decoding`), `slot` and
`decoding` (ISO times), `last` (`{"slot", "spots"}`), `error`, and `report`:
`enabled`, `call`, `grid`, `sent`, `pending`, `online`, `error`, or `reason`
when it can't report. `grid` is the station's grid at the moment, which a GPS
fix can change while rxd runs; each spot is reported with the grid it was
heard at.

**`js8_decoder`**: `engine` (`rxd`), `version`, `submodes`, `state`, `slot`,
`last`, `error`, `skipped`, `cpu_pct`, `jsc` (the JSC word table is
installed), `log` (the DIRECTED.TXT path).

**`hd`**: HD Radio (NRSC-5), decoded by
[nrsc5](https://github.com/theori-io/nrsc5) run as a separate program.

A WFM profile plays analog at once. When the spectrum shows HD's digital
sidebands, rxd starts nrsc5. Once nrsc5 has held sync with clean audio for a
few seconds, rxd crossfades to HD1, lined up with the analog. Any fault fades
straight back to analog. Each fallback doubles the wait before HD returns
(4 s up to 64 s), so a fringe station doesn't flap. HD2 and up are
digital-only: silent without HD. The block is always present, with `state`
`off` when HD isn't possible.

| Field | Type | Meaning |
|---|---|---|
| `state` | string | `off`, `no signal`, `acquiring`, `digital` or `analog fallback` |
| `reason` | string | One plain sentence: "Playing HD Radio.", "Analog: lining HD up with analog.", "Back on analog: nrsc5 lost sync.", "HD Radio: nrsc5 not installed" |
| `available` | bool | HD can run on this profile (WFM, turned on, nrsc5 installed) |
| `enabled` | bool | The profile's HD setting (`radio save` keeps it) |
| `station_enabled` | bool | The station file's `hd` setting ([station.md](station.md)) |
| `program` | int | The program chosen: 0 is HD1, 1 is HD2 ... (`radio save` keeps it) |
| `programs` | list | The station's audio programs: `{"program", "name" ("HD1"), "type" (a program type such as "Jazz", or null)}`. Empty until nrsc5 has heard the list |
| `station`, `slogan` | string or null | The station's name and slogan, as broadcast |
| `title`, `artist`, `album` | string or null | What the chosen program is playing |
| `mer_db` | object or null | Modulation error ratio, `{"lower", "upper"}` dB, one per sideband |
| `ber` | number or null | nrsc5's bit error rate (before error correction) |
| `synced` | bool | nrsc5 is in sync now |
| `clean_s` | number | In `digital`, how long it has played; otherwise how long the signal has been clean toward the hold |
| `hold_s` | number | How long it must stay clean before HD plays |
| `sidebands` | object or null | The detector: `present`, `lower_db` and `upper_db` (each sideband over the floor), `floor_db` |
| `running` | bool | nrsc5 is running |
| `analog_delay_s` | number | How far the analog is delayed to line up with HD. 0 when HD comes out ahead, as it usually does; then the HD is held back instead |
| `hd_lead_s` | number or null | How far nrsc5's audio runs ahead of the same audio on analog; negative is behind |
| `dropped`, `bad_frames` | int | IQ blocks nrsc5 couldn't take, and audio frames it filled with silence |

A client shows `state` in words, not by colour alone, and doesn't show
`clean_s` as a ticking age.

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
| `hd_program` | int | 0 (HD1) to 7 (HD8): the HD Radio program; changing it restarts nrsc5 |
| `hd_enabled` | bool | HD Radio on this profile |
| `audio_auto`, `audio_filter`, `audio_norm`, `audio_limit`, `audio_squelch` | bool | |
| `audio_nr` | bool or null | null: by mode (on, except WFM) |

An unknown field, or a value out of range, is `400` and changes nothing.
`radio save` keeps the tuning in the profile.

## GET /api/v1/spectrum

The captured band, about 5 times a second: `bins` (512 numbers, dB),
`start_hz` and `stop_hz` (the band's edges), `dial_hz`, `passband`
(`[low, high]` Hz, what is demodulated), `seq`, and `block` (the IQ block it was made from). All on the dial's scale.
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

**Across the switch.** When the upconverter has a switch a person flips and
NAME needs the other side from the position confirmed (or none is), add
`"switch": "upconvert"` or `"bypass"`, the side NAME needs, once the person
has flipped it: that confirms the new position, then rxd switches. Without
it the answer is `409` with a plain `error` ("fm-105.3 needs the Ham It Up on
BYPASS, and it's on UPCONVERT. Flip the switch, then send "switch":
"bypass" to confirm it."), `needs` and `position`, and nothing changes; a
`switch` that isn't the side NAME needs is `400`. The profile already up,
and stations with no such switch, need no `switch`. (This refuses what
rxd 0.4 did without asking, on purpose: it heard nothing.)

## POST /api/v1/switch

`{"position": "upconvert"}` or `{"position": "bypass"}`: a person says which
way the upconverter's switch is now. The answer is the new status. `400` for
anything else; `409` when the station's upconverter has no switch to
confirm, or there's no upconverter.

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
volume: a stream that runs until either side closes it. On a WFM profile
playing HD Radio it is the HD audio (blended with analog, or HD2 and up),
in mono, the same as the speaker.

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
`every_min`, `cross_side`, `side`, `side_limit`, `sun`, `conditions`,
`conditions_error`),
`all_bands`, and `profiles`: every band profile with `name`, `mode`, `band`,
`freq`, `upconvert`, `enabled`, `eligible`, `why_not`, `score`, `basis` and
`heard`.

With a switch a person flips, `phase` is `paused` (and `reason` "Paused:
confirm the Ham It Up's switch") while its position is unknown; `side` is the
position confirmed (`UPCONVERT` or `BYPASS`, null while unknown; without a
switch, the tuning's side), and `side_limit` says so on Auto ("Only BYPASS
bands: 6 m, 2 m"). A band across the switch has `why_not` "needs the Ham It
Up on BYPASS".

`POST` takes any of these and answers like `GET`:

| Field | Type | |
|---|---|---|
| `plan` | `auto` or `manual` | |
| `mode` | `wspr` or `js8` | |
| `bands` | list of bands, or null | null is the default: every band |
| `dwell_min` | number | 2 to 60: time on each band when scanning |
| `every_min` | number | 10 to 240: how often it looks for a better band |
| `cross_side` | bool | accepted and kept, but no longer has an effect: Auto never crosses a switch a person flips |
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
curl -s -XPOST -d '{"position": "bypass"}' 127.0.0.1:8073/api/v1/switch
curl -s -XPOST -d '{"name": "wspr-20m", "switch": "upconvert"}' 127.0.0.1:8073/api/v1/profile
curl -s '127.0.0.1:8073/api/v1/js8?after=0'
curl -s -XPOST -d '{"hd_program": 1}' 127.0.0.1:8073/api/v1/tune   # HD2
curl -s '127.0.0.1:8073/api/v1/audio?rate=24000' | aplay -f S16_LE -r 24000 -c 1
```
