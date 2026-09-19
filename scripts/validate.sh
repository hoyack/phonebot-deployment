#!/usr/bin/env bash
# Compatibility entry point: explicit arguments required; no automatic activation.
set -euo pipefail
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$root_dir/scripts/setup.py" validate "$@"
