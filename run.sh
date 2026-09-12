#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -d "$ROOT/.runtime/usr/lib" ]; then
    export LD_LIBRARY_PATH="$ROOT/.runtime/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
if [ -d "$ROOT/.runtime/usr/lib/tk8.6" ]; then
    export TK_LIBRARY="$ROOT/.runtime/usr/lib/tk8.6"
fi
exec "$ROOT/.venv/bin/python" "$ROOT/auto_fishing.py" "$@"
