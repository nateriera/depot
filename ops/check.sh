#!/usr/bin/env bash
# Read-only Linux/systemd user installation check.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/check.py" "$@"
