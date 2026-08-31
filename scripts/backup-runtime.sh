#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out_dir=${1:-"$root_dir/backups/$stamp"}
mkdir -p "$out_dir"
docker compose --project-directory "$root_dir/freepbx" exec -T db \
  sh -c 'exec mariadb-dump -uroot -p"$(cat /run/secrets/mysql_root_password)" --all-databases --single-transaction --routines --events' \
  > "$out_dir/freepbx-all-databases.sql"
docker run --rm \
  -v freepbx-docker_var_data:/source/var:ro \
  -v freepbx-docker_etc_data:/source/etc:ro \
  -v "$out_dir:/backup" alpine \
  tar -C /source -czf /backup/freepbx-volumes.tar.gz var etc
cp "$root_dir/phonesys/config.yaml" "$out_dir/phonesys-config.yaml"
echo "Backup created at $out_dir. Encrypt and move it out of the repository."

