#!/usr/bin/env bash
# Linux/systemd user installation; use --prefix DIR --no-start to stage without changing services.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/install.py" "$@"
