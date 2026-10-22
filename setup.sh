#!/usr/bin/env bash
# Configuration is parsed as data; never source config.sh.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${A1GRAB_PYTHON:-python3}" -I "$SCRIPT_DIR/a1grab.py" setup "$@"
