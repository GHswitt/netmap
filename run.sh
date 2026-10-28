#!/bin/sh
# Start netmap from its own venv; rebuild the venv when Arch upgrades Python under it.
set -eu
cd "$(dirname "$0")"

want=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
have=$(venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
if [ "$want" != "$have" ] || [ requirements.txt -nt venv/.installed ]; then
    echo "setting up venv for Python $want (was: ${have:-none})"
    rm -rf venv
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
    touch venv/.installed
fi

exec venv/bin/python -m netmap -c config.yaml serve
