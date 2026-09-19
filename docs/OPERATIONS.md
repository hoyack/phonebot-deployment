# Operations, verification and recovery

For a fresh install use [SETUP](SETUP.md). For PBX/Windows tasks, media mounts,
firewalls and relay persistence use [WINDOWS-PBX](WINDOWS-PBX.md). These are
separate operational domains; restarting Hermes cannot repair SIP/RTP routing.

## Ownership and installed paths

| Role | Profile / API | User service |
|---|---|---|
| Mary (7000 / 7001) | phonebot / 127.0.0.1:8642 | hermes-phonebot.service |
| John (7002) | jyoder / 127.0.0.1:8643 | hermes-jyoder.service |
| Atlas (7003) | atlas / 127.0.0.1:8644 | hermes-atlas.service |
| Bridge | ~/.config/phonesys | phonesys-bridge.service |

Profiles live under `~/.hermes/profiles/NAME`. Each owns its model login, API
key, state and capabilities. Do not copy the default profile or enable a second
multiplexed owner. Fresh John's key is `HERMES_JYODER_API_KEY`; the checked-in
bridge snapshot historically shares Mary's key, but the generated runtime
configuration deliberately separates it. Edit the installed runtime config,
not the snapshot, for installation-specific routing.

The bridge service invokes `scripts/run-bridge.py --runtime ~/.config/phonesys`
with its own virtualenv, private `bridge.env`, and a media symlink into the
checkout. Launching `python -m phonesys.main` directly instead reads the shipped
snapshot and can bypass your fresh runtime mapping. Use the rendered service or
launcher, not an invented upstream `--config` flag.

## Read-only checks first

```bash
systemctl --user --no-pager status hermes-phonebot hermes-jyoder hermes-atlas phonesys-bridge
systemctl --user is-active hermes-phonebot hermes-jyoder hermes-atlas phonesys-bridge
ss -lntup
for port in 8642 8643 8644; do curl --fail "http://127.0.0.1:$port/health"; done
journalctl --user -u phonesys-bridge.service -n 80 --no-pager
journalctl --user -u hermes-atlas.service -n 80 --no-pager
```

Logs can contain caller speech, phone numbers, tool output and secrets. Review
locally and redact before sharing. Do not dump environment files, auth stores,
`systemctl show ... Environment`, recordings or whole profiles into support
messages. A successful `/health` proves only an HTTP adapter is answering.

Use the PBX guide's read-only Asterisk commands to check `ari show apps`, loaded
custom dialplan, MOH classes and `core show channels count`. Confirm the
`phonesys` Stasis app rather than an obsolete `hermes` application name. Check
all three profiles when transfers fail; Mary's success does not prove John's
key/model/port works.

For an offline config check (no API calls), from the checkout:

```bash
phonesys/.venv/bin/python scripts/run-bridge.py --runtime "$HOME/.config/phonesys" --check
phonesys/.venv/bin/python -m pytest -q tests_deployment
(cd phonesys && .venv/bin/python -m pytest -q -m 'not live')
```

`--check` reads the runtime secret file without printing values. It does not
validate provider balances, model authorization or account-specific voice IDs.
Do not accidentally run a `live` test marker against an uncontrolled account.

## Authenticated inference (explicit external spend)

With permission for paid model requests, use a private Python process to read
the profile key rather than placing it on a curl command line. Example from the
repository root; substitute each of phonebot/8642, jyoder/8643, atlas/8644:

```bash
phonesys/.venv/bin/python - phonebot 8642 <<'PY'
import sys
from pathlib import Path
import httpx
from dotenv import dotenv_values
name, port = sys.argv[1:]
key = dotenv_values(Path.home() / '.hermes/profiles' / name / '.env')['API_SERVER_KEY']
r = httpx.post(f'http://127.0.0.1:{port}/v1/chat/completions',
    headers={'Authorization': f'Bearer {key}'},
    json={'model': 'hermes', 'messages': [{'role': 'user', 'content': 'Reply READY only.'}], 'stream': False},
    timeout=120)
print('HTTP', r.status_code)
r.raise_for_status()
print(r.json()['choices'][0]['message']['content'])
PY
```

This is distinct from telephone acceptance. No API key should be included in
an error report. Check rejected missing/incorrect authentication using a dummy
key if API exposure is being reviewed; never loosen auth to fix a 401.

## Changes and restarts

1. Verify the affected installation, user and config; inspect current listeners
   and active calls. Coordinate a maintenance window; never interrupt an active
   call without permission.
2. Make a private snapshot first. Change bridge behavior in
   `~/.config/phonesys/config.yaml`; change Hermes settings through
   `env -u HERMES_HOME -u HERMES_PROFILE hermes -p NAME config set KEY VALUE`.
3. For secret rotation, edit files privately, not shell arguments/history.
   Rotate a gateway's `API_SERVER_KEY` together with its matching bridge
   `HERMES_*_API_KEY`. Keep other profiles' keys independent. Rotate ARI on both
   PBX and bridge in one coordinated window. Rotate the PIN env entries without
   removing `access_pin_env` from routing.
4. Stop/restart only affected services after zero calls. A unit-file change
   requires `systemctl --user daemon-reload`; YAML/env changes require process
   restart but not daemon-reload. Example, for an Atlas key change:
   `systemctl --user restart hermes-atlas.service phonesys-bridge.service`.
5. Read back exact nonsecret settings, check services/health/authenticated
   inference, then carry out scoped phone acceptance. Report what actually ran.

Moving/renaming the checkout invalidates absolute unit paths and the media link.
Fresh staging is not an updater. Render a separate review bundle if useful, then
review a manual migration/rollback plan; never rerun staging over existing state.
The units are not sandboxes. Profiles with powerful tools retain the Unix user's
filesystem/network permissions even when callers require a PIN.

## Safe local snapshots (not a PBX backup)

Stop all writers during an approved, no-active-calls maintenance window before
copying profile databases. The helper does not stop services or make SQLite live
snapshots and cannot guarantee consistency while profiles are running.

```bash
# Only after checking calls and approving the outage:
systemctl --user stop phonesys-bridge hermes-phonebot hermes-jyoder hermes-atlas
mkdir -p "$HOME/private-backups"
chmod 700 "$HOME/private-backups"
python3 scripts/setup.py backup --home "$HOME" \
  --output "$HOME/private-backups/phonesys-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
```

Equivalent wrapper: `bash scripts/backup-runtime.sh --home "$HOME" --output ABSOLUTE_NEW_FILE`.
The output must not exist, must be outside the repository/source trees, and has
0600 permissions. All named profiles, runtime config/env and exact four unit
files must exist. The archive contains those three profiles (including secrets,
state, logs and memories), runtime regular files and the four units. It excludes
symlinks/special files, including runtime/media; it does not traverse arbitrary
linked homes, guess Docker volumes, run containers, or export a database.

**Not included:** PBX SQL, Docker volumes, media/recordings/MOH, Windows relay or
scheduled-task state, other profiles, the repository and dependency binaries.
Use the separately scoped PBX backup procedure for SQL/volumes; record exact
Compose project/volume names and check restore compatibility. Encrypt snapshots
before off-host transfer, restrict retention, and test restoration in an isolated
home/PBX instance. File permissions alone are not encryption. Never put backup
archives in Git. A failed copy may leave a partial archive; inspect it, do not
call it a valid backup merely because a file exists.

After snapshot/review, start gateways first, verify them, then start bridge.
There is no automatic restore command: extract to a new private directory,
inspect members and permissions, and selectively restore only authorized files
with services stopped. Preserve current files separately; do not overwrite a
working default profile or blindly untar into HOME.

## Diagnostic layers and acceptance

* **HTTP:** gateways answer but model may still be unauthenticated or unavailable.
* **ARI:** `phonesys` registers but media can still be unreachable.
* **Media:** confirm container-to-relay-to-WSL AudioSocket and RTP independently;
  stale WSL IPs and incorrect advertised host/port are common failure points.
* **STT/TTS:** confirm external credentials/quota/model and voice availability.
  Startup filler synthesis can incur cost before a caller arrives.
* **Access controls:** authorized correct/incorrect PIN tests for both John and
  Atlas, including receptionist transfers and direct extension dialing. Never
  print PIN/DTMF events in acceptance logs.
* **Conversation:** greeting, complete response, barge-in, caller pause handling,
  timeout feedback, hangup cleanup, transfer voice/STT context and optional hold
  music. Keep hold music off the STT path; speaking a hold promise does not prove
  music actually started.
* **Human/PSTN:** scoped approved calls, cost/time budget and recording consent.
  Label isolated tests, configured state, authenticated API inference and live
  end-to-end evidence separately. Do not claim one proves the others.

No fresh-host/PSTN deployment was performed by the helper test suite. An operator
must complete these environment-specific acceptance checks after manual setup.
