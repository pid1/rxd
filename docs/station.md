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
  whenever the file changes.
- **When it's wrong:** a bad value is ignored, its default used instead, and
  the reason logged (`journalctl --user -u 'rxd@*'`) and listed in the
  status's `station.problems`. A bad file never stops rxd.

## Example

```json
{
  "call": "KA1PID",
  "grid": "EM13lc",
  "sdr": {"serial": "57818712"},
  "upconverter": {"name": "Ham It Up", "hz": 125000000, "below_hz": 30000000, "switch": true},
  "sink": "Built-in Audio",
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
| `grid` | string | none | The Maidenhead grid, 4 or 6 characters (`EM13` or `EM13lc`): the map's centre, the sun's position for the band plan, and the WSPR report's locator. |
| `sdr.serial` | string | `""` | The RTL-SDR's serial number (`rtl_test` lists it). rxd opens that dongle however the USB enumerated, and says so plainly when it's missing. Empty: the first one found. |
| `upconverter` | object or null | null | An upconverter in front of the dongle, below. null or absent: none, so `"offset": "auto"` never upconverts. |
| `upconverter.name` | string | `upconverter` | What to call it: "Flip the Ham It Up to BYPASS". |
| `upconverter.hz` | number | 125000000 | Its offset, Hz. 0 is the same as no upconverter. |
| `upconverter.below_hz` | number | 30000000 | Profiles with `"offset": "auto"` go through it below this frequency, and straight to the dongle at and above it. |
| `upconverter.switch` | bool | true | It has an UPCONVERT/BYPASS switch that only a person can flip (a Ham It Up's is physical). Then `radio` and the clients say when to flip it, and the band plan keeps Auto to the side it's on unless `cross_side` is set. false: an upconverter that switches itself, or a diplexer. |
| `sink` | string or null | null | The PipeWire or PulseAudio sink the speaker plays to, by name. `"Built-in Audio"` and null both mean the default sink. |
| `profiles` | object | `{}` | This station's own profiles, name to profile: local repeaters, broadcast stations. Below. |

`$RXD_CALL` and `$RXD_GRID`, when set (in rxd.env, say), win over `call` and
`grid`.

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
