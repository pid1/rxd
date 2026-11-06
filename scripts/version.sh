#!/bin/sh
# Prints rxd's version: the VERSION line in rxd/rxd.py, the one place it is
# set. The release workflow and scripts/release.sh use this.
set -eu
cd "$(dirname "$0")/.."
sed -n 's/^VERSION = "\([0-9][0-9.]*\)"$/\1/p' rxd/rxd.py | head -1 | grep . \
  || { echo "version.sh: no VERSION line in rxd/rxd.py" >&2; exit 1; }
