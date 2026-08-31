#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test -f "$HOME/.config/phonebot/phonebot.env"
test -f "$root_dir/freepbx/secrets/mysql_root_password.txt"
test -f "$root_dir/freepbx/secrets/freepbxuser_password.txt"
test -f "$root_dir/freepbx/secrets/sasl_passwd.txt"
docker compose --project-directory "$root_dir/freepbx" config --quiet
"$root_dir/phonesys/.venv/bin/python" -m pytest -q "$root_dir/phonesys/tests"
systemd-analyze --user verify "$root_dir/systemd/hermes-phonebot.service" "$root_dir/systemd/phonesys-bridge.service"
echo "Deployment definitions and phonesys tests passed."

