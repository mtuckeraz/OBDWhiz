#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$project_dir"
if [[ ! -x .venv/bin/python ]]; then
  printf 'Run bash install-pi.sh first to install Python and Bluetooth dependencies.\n' >&2
  exit 1
fi
if ! .venv/bin/python -c 'import bleak, serial, reportlab'; then
  printf 'ProActive dependencies are missing. Run bash install-pi.sh again.\n' >&2
  exit 1
fi
exec .venv/bin/python server.py "$@"
