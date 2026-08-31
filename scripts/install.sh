#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 -m venv "$root_dir/phonesys/.venv"
"$root_dir/phonesys/.venv/bin/pip" install --upgrade pip
"$root_dir/phonesys/.venv/bin/pip" install -e "$root_dir/phonesys"
mkdir -p "$root_dir/phonesys/media/sounds" "$root_dir/phonesys/media/rec"

mkdir -p "$HOME/.hermes/profiles/phonebot" "$HOME/.config/phonebot" "$HOME/.config/systemd/user"
install -m 0644 "$root_dir/phonebot/profile.yaml" "$HOME/.hermes/profiles/phonebot/profile.yaml"
install -m 0644 "$root_dir/phonebot/config.yaml" "$HOME/.hermes/profiles/phonebot/config.yaml"
install -m 0644 "$root_dir/phonebot/SOUL.md" "$HOME/.hermes/profiles/phonebot/SOUL.md"
install -m 0644 "$root_dir/systemd/hermes-phonebot.service" "$HOME/.config/systemd/user/"
install -m 0644 "$root_dir/systemd/phonesys-bridge.service" "$HOME/.config/systemd/user/"

if [[ ! -f "$HOME/.config/phonebot/phonebot.env" ]]; then
  install -m 0600 "$root_dir/.env.example" "$HOME/.config/phonebot/phonebot.env"
fi
systemctl --user daemon-reload
echo "Installed. Populate phonebot.env and FreePBX secrets before starting services."

