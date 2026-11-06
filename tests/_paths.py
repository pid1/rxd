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
