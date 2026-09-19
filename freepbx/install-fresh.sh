#!/usr/bin/env bash
# Explicit, guarded installer invocation for an already-running NEW PBX only.
set -euo pipefail
set +x
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ${1:-} != --apply || $# != 1 ]]; then
    printf '%s\n' 'Dry run: would verify secrets, DB health, and absence of /etc/freepbx.conf,' \
      'then invoke the image installer with asterisk/asteriskcdrdb, freepbxuser, host db.' \
      'No Docker commands run. Read docs/WINDOWS-PBX.md; pass --apply only for a fresh PBX.'
    exit 0
fi
python3 - <<'PY'
from pathlib import Path
import re
import stat
p = Path('secrets/freepbxuser_password.txt')
if p.is_symlink() or not p.is_file() or stat.S_IMODE(p.stat().st_mode) & 0o077:
    raise SystemExit('DB secret must be a regular private (0600) file, not a symlink.')
# Restrict to generator format: avoids upstream PHP/shell/config quoting surprises.
if not re.fullmatch(r'[0-9a-f]{64}\n?', p.read_text()):
    raise SystemExit('Expected generated 64-hex DB secret. Do not change existing DB credentials to bypass this guard.')
PY
docker compose config --quiet
docker compose exec -T db healthcheck.sh --connect --innodb_initialized
# Refuse install/upgrade/force paths on existing or partial installs.
docker compose exec -T freepbx sh -ec '
  test ! -e /etc/freepbx.conf && test ! -e /etc/amportal.conf || {
    printf "%s\n" "Existing/partial PBX configuration: refusing fresh installer." >&2; exit 1;
  }
'
# Password is not passed in the host argv. Container administrators can still
# inspect the install process arguments; do not log/trace this invocation.
docker compose exec -T -w /usr/local/src/freepbx freepbx sh -ec '
  IFS= read -r dbpass
  exec php install -n --dbhost=db --dbport=3306 --dbname=asterisk \
    --cdrdbname=asteriskcdrdb --dbuser=freepbxuser --dbpass="$dbpass"
' < secrets/freepbxuser_password.txt
# Some image revisions return nonzero even after a success message. Fail closed:
# review private logs and GUI; never automatically retry or use --force.
printf '%s\n' 'Installer exited zero; verify GUI, DB and generated config per docs before activation.'
