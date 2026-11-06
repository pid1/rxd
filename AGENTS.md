# AGENTS.md

## Sadr

Sadr: off. The owner keeps this repo out of Sadr's runs.

## Working here

- **Public repository.** No third-party callsigns anywhere, history and
  commit messages included: tests and examples use KA1PID (the owner's),
  W1AW, K1ABC, or calls checked as unissued against FCC data. Real
  recordings stay out of git (`RXD_JS8_REAL=DIR`).
- **Tests:** `python3 -m unittest discover tests` on Python 3.11 with numpy
  1.24 and scipy 1.10 (Debian bookworm's, the receiver's). Don't use newer
  APIs. Tests never reach the network and never read the machine's own
  station file (tests/_paths.py points `RXD_STATION` at
  tests/fixtures/station.json).
- **The API** is versioned (docs/API.md): a new field or endpoint is fine
  within v1; removing, renaming or changing a meaning is `/api/v2/`. Keep
  docs/API.md in step with every change, and both reference clients on
  `/api/v1/`.
- **The version** is `VERSION` in rxd/rxd.py and nowhere else. A release is
  a tag `vX.Y.Z` matching it; then pid1/rx gets a PR bumping `RXD_VERSION`
  and `RXD_SHA256` in its setup.sh.
- **Nothing machine-specific in the code.** A station's hardware, identity
  and local profiles go in the station file (docs/station.md); a machine's
  own commands go in `radio-CMD` extensions.
- **The web UI** stays one self-contained file with no external requests.
  Status is never shown by colour alone; Day, Dark and Night (red) all work.
