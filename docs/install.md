# Installing rxd

## What it needs

- **Linux** with systemd and PipeWire (or PulseAudio).
- **Python 3.11** or later, with **numpy** and **scipy** (on Debian:
  `python3-numpy python3-scipy`). The tests pin Debian bookworm's numpy 1.24
  and scipy 1.10; don't rely on anything newer.
- **librtlsdr** and **libpulse-simple** (`librtlsdr0 libpulse0`), loaded with
  ctypes: no build step, no `-dev` packages.
- **wsprd**, from WSJT-X (Debian's `wsjtx`), for WSPR. Without it WSPR
  profiles say so and don't decode.
- **chrony**, if you want rxd to say when the clock is too far off for JS8
  and WSPR. Optional.

## From a release

Each release has `rxd-X.Y.Z.tar.gz` and `rxd-X.Y.Z.tar.gz.sha256`.

```bash
v=0.4.0
curl -fLO https://github.com/pid1/rxd/releases/download/v$v/rxd-$v.tar.gz
curl -fLO https://github.com/pid1/rxd/releases/download/v$v/rxd-$v.tar.gz.sha256
sha256sum -c rxd-$v.tar.gz.sha256
tar xzf rxd-$v.tar.gz
sudo python3 rxd-$v/install.py --prefix /usr/local --user "$USER"
```

`install.py` ([source](../install.py)) puts:

| Path | What |
|---|---|
| `PREFIX/lib/rxd/X.Y.Z-HASH/` | the code: rxd.py, js8.py, callsign_db.py, radio.py, station.py, rxd_tui.py, web/index.html |
| `PREFIX/lib/rxd/current` | a link to the directory above |
| `PREFIX/bin/rxd`, `radio`, `rxd-tui` | links into `current/` |
| `PREFIX/share/bash-completion/completions/radio` | tab completion |
| `/etc/systemd/user/rxd@.service` | the user unit (`--unit-dir` moves it) |

Then, as the user (`--user NAME` when run as root; yourself otherwise):

| Path | What |
|---|---|
| `~/.local/share/rxd/js8/jsc_map.txt` | JS8's JSC word table, built from JS8Call's `jsc_map.cpp` (GPL-3, so not in this repository), fetched from the commit and checked against the SHA-256 that js8.py pins. `--jsc-source FILE` uses a copy you have. |
| `~/.local/share/rxd/callsigns.sqlite` | the callsign database, from this repository's `data-callsigns` release. `--callsigns-url URL` fetches it from elsewhere. |

- **It is safe to run again.** A run with nothing new changes nothing, and
  each data step is skipped when what's installed is current.
- **It can't leave a half install.** The code goes into a new directory that
  is complete before `current` is switched to it in one rename; every other
  file is written beside its target and renamed over it. The install before
  is kept (an rxd still running from it keeps working) and older ones are
  removed.
- **Offline is fine.** A data step that can't fetch is a warning, and what's
  there is kept; rxd works without either file.
- **What it never touches:** the station file (`/etc/rxd/station.json`,
  [station.md](station.md)), `/etc/rxd/rxd.env`, the firewall, and anything
  else about the machine.
- **Options:** `--prefix`, `--unit-dir`, `--sysconfdir` (where the unit looks
  for `rxd/rxd.env`, default `/etc`), `--user`, `--no-data`, `--jsc-source`,
  `--callsigns-url`, `--dry-run`. `python3 install.py --help` has them all.

## Then

1. Write `/etc/rxd/station.json` ([station.md](station.md)), at least `call`,
   `grid` and `sdr.serial`.
2. Optionally `/etc/rxd/rxd.env`, read by the unit:
   ```bash
   RXD_HTTP=127.0.0.1:8073   # where the API and the browser UI listen
   ```
   Set `0.0.0.0:8073` to serve your network, behind a firewall: there is no
   authentication.
3. `radio js8` (as the user) starts rxd on the 40 m JS8 dial. `radio` lists
   the profiles; [the README](../README.md#radio) says more.
4. Open `http://localhost:8073/`, or run `rxd-tui`.

## From a checkout

Nothing needs building. `rxd/rxd.py PROFILE` runs from the tree, and finds
the web UI in `clients/web/`. `python3 install.py --prefix ~/.local
--unit-dir ~/.config/systemd/user` installs for one user without root.
