"""Puts the code under test on sys.path: rxd/ (the daemon, js8, callsign_db,
radio, station) and clients/tui/ (rxd-tui). Every test module imports this
first.

    python3 -m unittest discover tests
"""
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
RXD = os.path.join(ROOT, "rxd")
TUI = os.path.join(ROOT, "clients", "tui")
WEB = os.path.join(ROOT, "clients", "web", "index.html")

for _p in (TUI, RXD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The tests' station: the hardware rxd was first written for (an RTL-SDR
# behind a Ham It Up with its manual switch) and four local profiles, but no
# call or grid, so nothing can report to WSPRnet. Never the machine's own
# /etc/rxd/station.json. A test that wants another station sets RXD_STATION.
STATION = os.path.join(TESTS, "fixtures", "station.json")
os.environ["RXD_STATION"] = STATION
for _k in ("RXD_CALL", "RXD_GRID"):
    os.environ.pop(_k, None)
