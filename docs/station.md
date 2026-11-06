# The station file

What makes one receiver different from another lives in one JSON file, not
in rxd's code: who the station is, which RTL-SDR to open, the upconverter,
the speaker, and any profiles of its own. rxd and `radio` both read it.

- **Where:** `/etc/rxd/station.json`, or the file `$RXD_STATION` names.
- **Who writes it:** whoever sets the machine up. rxd's installer never
  touches it. (On a uConsole, pid1/rx's setup.sh writes it.)
- **Optional, every key of it.** With no file, rxd and `radio` are a generic
  receiver: the first RTL-SDR found, no upconverter, the default audio sink,
  no callsign or grid (so WSPR decodes but isn't reported to WSPRnet), and
  only the built-in profiles.
- **When it's read:** `radio` reads it every time it runs. rxd reads it
  when a profile loads (at start and on SIGHUP) and, for the call and grid,
  whenever the file changes. The `gps` key is read when rxd starts.
- **When it's wrong:** a bad value is ignored, its default used instead, and
  the reason logged (`journalctl --user -u 'rxd@*'`) and listed in the
  status's `station.problems`. A bad file never stops rxd.

## Example

```json
{
  "call": "KA1PID",
  "grid": "EN52lk",
  "sdr": {"serial": "57818712"},
  "upconverter": {"name": "Ham It Up", "hz": 125000000, "below_hz": 30000000, "switch": true},
  "sink": "Built-in Audio",
  "gps": {"enabled": true, "gpsd": "127.0.0.1:2947"},
  "profiles": {
    "fm-93.3": {"description": "93.3 FM", "frequency": 93300000, "demod": "WFM",
                "demod_settings": {"bandwidth": 150000}, "rtl": {"gain": 10}}
  }
}
```

## Keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `call` | string | none | The callsign WSPR spots are reported as, and the map's centre station. Upper-cased. |
| `grid` | string | none | The Maidenhead grid, 4 or 6 characters (`EN52` or `EN52lk`): the map's centre, the sun's position for the band plan, and the WSPR report's locator, when the GPS has no grid (below). |
| `sdr.serial` | string | `""` | The RTL-SDR's serial number (`rtl_test` lists it). rxd opens that dongle however the USB enumerated, and says so plainly when it's missing. Empty: the first one found. |
| `upconverter` | object or null | null | An upconverter in front of the dongle, below. null or absent: none, so `"offset": "auto"` never upconverts. |
| `upconverter.name` | string | `upconverter` | What to call it: "Flip the Ham It Up to BYPASS". |
| `upconverter.hz` | number | 125000000 | Its offset, Hz. 0 is the same as no upconverter. |
| `upconverter.below_hz` | number | 30000000 | Profiles with `"offset": "auto"` go through it below this frequency, and straight to the dongle at and above it. |
| `upconverter.switch` | bool | true | It has an UPCONVERT/BYPASS switch that only a person can flip (a Ham It Up's is physical). Then `radio` and the clients say when to flip it, and the band plan keeps Auto to the side it's on unless `cross_side` is set. false: an upconverter that switches itself, or a diplexer. |
| `sink` | string or null | null | The PipeWire or PulseAudio sink the speaker plays to, by name. `"Built-in Audio"` and null both mean the default sink. |
| `profiles` | object | `{}` | This station's own profiles, name to profile: local repeaters, broadcast stations. Below. |
| `gps` | object or false | on | Take the grid from a GPS, through gpsd. Below. false is the same as `{"enabled": false}`. |
| `gps.enabled` | bool | true | Read the GPS. When gpsd isn't there, rxd says so in its status and uses `grid`. |
| `gps.gpsd` | string | `127.0.0.1:2947` | gpsd's HOST:PORT. |

`$RXD_CALL` and `$RXD_GRID`, when set (in rxd.env, say), win over `call` and
`grid`, and `$RXD_GPSD` (`HOST:PORT`, or `off`) over `gps`.

## The grid and the GPS

The station's grid is, in order:

1. **A live GPS fix** rxd has accepted (`grid_source` `gps` in the status).
2. **The last GPS grid** rxd accepted, saved in
   `~/.local/state/rxd/location.json` (`$RXD_LOCATION`), so it survives a
   restart and works with no fix and no network (`gps-saved`).
3. **The station file's `grid`** (`station`).
4. Nothing: then WSPR decodes but isn't reported.

A GPS grid that differs from `grid` means the station has moved: the status
shows both (`grid` and `default_grid`) and says which is in use and why
(`grid_reason`). Change or delete the saved file to forget a move; rxd reads
it when it starts.

rxd asks gpsd for the receiver's own bytes as well as its reports
(`?WATCH={"json":true,"raw":2}`), and prefers a u-blox receiver's binary
(UBX) NAV-SOL or NAV-POSLLH fix to gpsd's NMEA-derived one, because some
receivers' NMEA can't be trusted: a clone u-blox was seen reporting a
placeholder position (3000.0000 N, 11960.0000 E, "119 degrees 60 minutes")
in NMEA while its binary fix was real. An NMEA (TPV) position is rejected
when it is a whole-degree or known factory-default position, when the raw
NMEA has minutes of 60 or more, when gpsd says there's no fix, or when no
satellites are used. rxd never changes the receiver's or gpsd's settings.

A fix is used only when it's believable (2D or 3D, 5 or more satellites,
accurate to better than 100 m) and steady: the same 6-character grid for 3
minutes, with a margin at the subsquare's edges so a fix jittering across
one doesn't flap between two grids. Then the grid's centre is what WSPRnet,
the map and the band plan's sun use.

**Privacy:** rxd keeps only the 6-character grid. The precise position is
never saved, logged or put in the status; the saved file holds the grid, its
source (`gps/ubx` or `gps/tpv`) and when it was accepted.

A profile's own `upconverter_hz`, `upconvert_below_hz`, `rtl.serial` and
`sink` win over the station's, for that profile.

## Station profiles

Each entry of `profiles` is a `radio` profile, as `radio show NAME` prints
one, and appears in `radio`'s list after the built-in ones. Only `frequency`
(Hz) is required; the rest default as the built-in VHF profiles do:

- `description`: the name.
- `demod`: `USB`; one of `USB`, `LSB`, `AM`, `FM`, `WFM`.
- `offset`: `auto`.
- `volume`: 0.5.
- `rtl`: gain index 20, the station's `sdr.serial`. A profile's own `rtl`
  keys win, so `"rtl": {"gain": 10}` changes only the gain.
- `sink`: the station's.

A name is letters, digits, `.`, `-` and `_`. A station profile with the name
of a built-in one replaces it. Saved state (`radio save`) still lives in
`~/.config/rxd/radio/NAME.json` and wins over both.

## The built-in profiles

Without a station file `radio` has the band plan (`js8-160m` to `js8-2m`,
`wspr-2190m` to `wspr-2m`; 40 m's are `js8` and `wspr`), `ghostnet` (JS8
GhostNet, 7.107 MHz), `vhf` (146.52 MHz FM), `uhf` (446.0 MHz FM) and `noaa`
(162.55 MHz FM). An R820T or R828D dongle tunes no lower than about 24 MHz
and rxd doesn't use direct sampling, so HF and the AM broadcast band need an
upconverter in the station file to be heard at all.
