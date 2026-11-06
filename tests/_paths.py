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
# No GPS and no saved GPS grid: tests never reach the machine's gpsd or read
# its ~/.local/state/rxd/location.json. A test that wants them sets its own.
os.environ["RXD_GPSD"] = "off"
os.environ["RXD_LOCATION"] = os.path.join(TESTS, "fixtures", "no-location.json")
# The upconverter switch's confirmed position (station.py): none, unless a test
# confirms one in a file of its own. The boot is a fixed one, so a position a
# test confirms holds until the test says the machine started again.
os.environ["RXD_SWITCH"] = os.path.join(TESTS, "fixtures", "no-switch.json")
os.environ["RXD_BOOT_ID"] = os.path.join(TESTS, "fixtures", "boot_id")
# No real nrsc5: a machine with one installed would otherwise start HD Radio
# in every WFM test. The HD tests point rxd at tests/fixtures/fake_nrsc5.py.
os.environ["RXD_NRSC5"] = "off"
FAKE_NRSC5 = os.path.join(TESTS, "fixtures", "fake_nrsc5.py")
