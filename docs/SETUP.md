# Fresh Linux / WSL setup

For a shorter prerequisite-first entry point, see
[FreePBX/Telnyx deployment](free-pbx-deploy.md). `scripts/deploy.py` wraps the
existing helpers with default dry-runs, optional digest-pinned PBX preparation,
and credential validation before explicitly acknowledged staging. The detailed
procedure below remains available; do not run both generation paths for the
same output/media directories.

This is a **fresh-install** procedure, not an updater for an existing phone system.
The base `scripts/setup.py` helper does not start/stop services, install packages,
run Hermes, change Docker or touch the default profile. The separate optional
[standalone Atlas worker helper](ATLAS-WORKER-SETUP.md) does invoke the Hermes
config CLI for its explicitly selected worker, but never activates services.
Do not run an older `install.sh`.
The new compatibility entry point requires an explicit `generate` or `stage`
subcommand. Back up an existing system and use a separately reviewed migration;
there is intentionally no `--force` or overwrite option.

## 1. Prerequisites and boundaries

Use a dedicated Linux Unix user on Debian/Ubuntu or a WSL2 distro with systemd.
Store the checkout, virtualenv, generated secrets and profiles on Linux storage
(e.g. `/home/you`), not `/mnt/c`; Windows filesystem permissions are not an
adequate substitute for 0600 credential files. Keep generated credentials and
runtime state private even though the source repository is public. Run commands
as the intended service user, not root. Use sudo only for OS prerequisites or
the explicit linger choice.

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates xz-utils python3 python3-venv ffmpeg
# Python >=3.11 required; uv can download a supported interpreter below.
git clone https://github.com/hoyack/phonebot-deployment.git "$HOME/phonebot-deployment"
cd "$HOME/phonebot-deployment"
REPO=$(pwd -P)
```

Install Docker Engine + Compose v2 on native Linux, or Docker Desktop with WSL
integration on Windows, following the vendor's instructions. `docker version`
and `docker compose version` must work for your user. Joining the docker group
is effectively granting root access; do not grant it casually to a shared agent
account. Complete [Windows/PBX networking and provisioning](WINDOWS-PBX.md),
including WSL systemd, before activating services. Native Linux does not need
the Windows relay: use an address reachable **from the PBX container** and the
bridge's real listener port in the runtime YAML.

Check existing port listeners before proceeding (`ss -lntup`). Required topology:

| Interface | Purpose | Exposure |
|---|---|---|
| 127.0.0.1:8642 | phonebot (7000 turn / 7001 streaming) | Local authenticated API |
| 127.0.0.1:8643 | jyoder (7002, PIN protected) | Local authenticated API |
| 127.0.0.1:8644 | atlas (7003, PIN protected) | Local authenticated API |
| PBX ARI 8088 | Bridge control | Loopback/private trusted network only |
| WSL 9010 / Windows 19010 | AudioSocket media | Only trusted PBX path |
| SIP/RTP and PBX web | Telephony/admin | See PBX guide, never unrestricted |

AudioSocket is not an authenticated public service. Firewall it to the PBX path.
Do not expose Hermes API ports, ARI, or PBX management to the Internet.

## 2. Install and verify Hermes separately

Follow the official [installation](https://hermes-agent.nousresearch.com/docs/)
and [profile](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
documentation. Review the official installer before running it. This repository
does not vendor Hermes or credentials. Baseline checked here: Hermes **0.21.3**,
revision `c661785f872b5647fbac7c138d965180783bd9af`; do not assume an arbitrary
future release has identical settings. No local custom Hermes patch is required
for this voice setup.

On the **new host only**, download and review the official shell installer, then
run it as the intended Unix user. Its `--skip-setup` flag avoids configuring a
default-profile model; the three telephone identities are configured separately
below. This installs the upstream current version, not a promise that it equals
the recorded baseline. Record/check the version and rerun acceptance if it differs.

```bash
INSTALLER=$(mktemp)
curl --fail --silent --show-error --location \
  https://hermes-agent.nousresearch.com/install.sh --output "$INSTALLER"
less "$INSTALLER"                 # review before approving execution
bash "$INSTALLER" --skip-setup --skip-browser --skip-computer-use
rm -- "$INSTALLER"
export PATH="$HOME/.local/bin:$PATH"
```

The official installer supplies `uv`, a supported Python, Hermes's own virtualenv
and launcher. Browser/computer-control installation is deliberately skipped for
the minimal telephone setup; install those separately only if approved tools
need them. No `hermes gateway install` is required: this repository renders the
three standalone gateway units instead.

```bash
hermes --version
hermes --help
hermes config set --help
hermes gateway --help
HERMES_BIN=$(readlink -f "$(command -v hermes)")
test -x "$HERMES_BIN"
```

Use the installed executable path, not an alias. The helper deliberately refuses
symlink path components; use canonical paths (`pwd -P`, `readlink -f`). No
existing root/default profile credentials, Slack tokens, OAuth stores, memory
or sessions are copied. **Every profile must authenticate its own model.**

## 3. Build the bridge virtualenv (locked dependencies)

Install reviewed `uv` using its official instructions; CI uses uv 0.9.26.
From the repository root:

```bash
uv --version
(cd "$REPO/phonesys" && uv sync --frozen --extra dev --python 3.11)
"$REPO/phonesys/.venv/bin/python" -m pytest -q "$REPO/tests_deployment"
(cd "$REPO/phonesys" && .venv/bin/python -m pytest -q -m 'not live')
```

The committed `phonesys/uv.lock` pins bridge and dev dependencies. A fallback
`python3 -m venv phonesys/.venv` followed by
`phonesys/.venv/bin/python -m pip install -e 'phonesys[dev]'` works but is **not**
the locked reproducible path. Hermes has its own environment; never pip-install
this bridge into Hermes. Offline tests are not telephone acceptance.

## 4. Generate a private review bundle

Use an output path that does **not exist**, outside the checkout. All path
arguments must be absolute, without traversal/control characters or symlinks.
Space-containing paths are supported. Dollar signs, percent signs, double quotes
and backslashes in paths are rejected rather than emitting ambiguous systemd
directives. The output parent must already exist.

```bash
BUNDLE="$HOME/phonebot-fresh-bundle"
python3 "$REPO/scripts/setup.py" generate \
  --repo "$REPO" --home "$HOME" --hermes-bin "$HERMES_BIN" --output "$BUNDLE"
```

This creates only the bundle, with 0700 directories and 0600 files:

* `profiles/{phonebot,jyoder,atlas}/`: independent API key `.env`, fresh
  `profile.yaml`, and reviewable `SOUL.md`. John/Atlas are minimal new identities,
  not exported private operational profiles. Customize these instructions and
  business facts; the supplied receptionist SOUL must also be reviewed.
* `bridge.env`: random 256-bit ARI password, three independent 256-bit API keys,
  and independently generated eight-digit John/Atlas PINs. Deepgram and
  ElevenLabs keys are deliberately blank. Secrets are never printed by helper.
* `config.yaml`: fresh runtime copy of bridge configuration. All three profiles
  use independent `HERMES_*_API_KEY` variables, including
  `HERMES_JYODER_API_KEY` on 7002.
* `units/`: four user services with resolved absolute paths. Each Hermes unit
  sets `API_SERVER_ENABLED=true`, `API_SERVER_HOST=127.0.0.1`, its assigned
  `API_SERVER_PORT`, and its own `HERMES_HOME`/credential file. These API env names
  match the checked Hermes gateway implementation; API key is `API_SERVER_KEY`.

Keep the bundle private; do not commit it or post it to chat. If generation is
interrupted, inspect/remove **only that newly created output** before retrying
with a new path. Never delete an existing profile to make this installer pass.

## 5. Supply external credentials and PBX/media settings

Open `"$BUNDLE/bridge.env"` with a local editor (not a command that embeds a
secret in shell history). Fill `DEEPGRAM_API_KEY` and `ELEVENLABS_API_KEY` from
your own funded accounts. Keep simple `NAME=value` entries, one per line; no
shell commands, variable substitutions, multiline values or duplicate keys.
Enable the required Deepgram transcription model and add/licence each configured
ElevenLabs voice to your account. The shipped `voice_id` values are examples,
not evidence of account access. Change them in `"$BUNDLE/config.yaml"` if needed.

Initialize PBX secrets first using the PBX guide (root `.env.example` belongs to
PBX provisioning, not the bridge). If that process has already generated the
ARI password, replace `ARI_PASSWORD` in the private bundle with **that exact
value**, retaining `ARI_USER=phonesys`; do not generate a second independent PBX
password. Alternatively, if ARI has not yet been provisioned, configure the PBX
ARI user with this bundle's generated password. Both sides must agree. Use
application `phonesys` and the custom extension routes. Database/admin/SIP trunk
secrets are **separate** and provisioned in that guide; this helper does not
generate a working carrier account. Keep generated PINs
in a password manager and deliver privately to the authorized users. Do not
remove PIN requirements merely to pass a smoke test.

Review runtime `ari.base_url`, `ari.app`, each `hermes_base_url`, voice, greeting,
call/time budgets, directory and PIN policy, and AudioSocket host/ports. ARI
URL/app are YAML settings; `ARI_BASE_URL`/`ARI_APP` environment values do not
override this bridge. All streaming routes share a listener: keep their local
ports consistent. The shipped WSL example uses host.docker.internal:19010 and
local 9010; native Linux must select its actual container-to-host path instead.

Atlas's consent hold references `phonesys_hold`. Install a licensed local MOH
asset and class as the PBX guide describes, or explicitly disable `hold_enabled`
until it exists. Do not replace the PBX default MOH class.

```bash
python3 "$REPO/scripts/setup.py" validate --bundle "$BUNDLE"
# Same command via compatibility wrapper: bash scripts/validate.sh --bundle "$BUNDLE"
"$REPO/phonesys/.venv/bin/python" "$REPO/scripts/run-bridge.py" \
  --runtime "$BUNDLE" --check
```

Validation is offline: it checks required credentials, independent matching API
keys and PIN format/private bundle permissions, not whether providers accept
keys. The second command invokes the actual bridge YAML loader without network
calls. No credentials are printed. Correct any error before staging.

## 6. Stage new profiles and units, without starting them

```bash
python3 "$REPO/scripts/setup.py" stage --bundle "$BUNDLE" --home "$HOME"
```

Staging refuses any existing named profile (even empty), runtime directory,
unit file or symlink, and preflights all destinations before copying. It does
not merge or overwrite. Default and other profiles are not targets. Do not run
concurrent staging/configuration writers; this is not a hostile multi-user
filesystem transaction. A disk/permission failure can leave a partial fresh
installation; inspect and recover manually, never use an overwrite shortcut.

Targets are `~/.hermes/profiles/{phonebot,jyoder,atlas}`,
`~/.config/phonesys/{bridge.env,config.yaml}` and four units under
`~/.config/systemd/user`. The runtime `media` symlink points to
`$REPO/phonesys/media`, preserving the PBX bind mounts. The launcher explicitly
sets the bridge's runtime root because upstream `phonesys.main` has no
`--config` option; source and tracked config are not modified.

## 7. Authenticate and configure EACH Hermes profile

Staging creates recognized blank profile identities but deliberately does not
copy `config.yaml`. Install actual settings with the Hermes CLI. Unset inherited
profile selectors so a shell launched inside another agent cannot redirect the
commands. These commands affect only the three named profiles:

```bash
for p in phonebot jyoder atlas; do
  env -u HERMES_HOME -u HERMES_PROFILE hermes -p "$p" model
  env -u HERMES_HOME -u HERMES_PROFILE hermes -p "$p" config set gateway.multiplex_profiles false
  env -u HERMES_HOME -u HERMES_PROFILE hermes -p "$p" config set memory.memory_enabled false
  env -u HERMES_HOME -u HERMES_PROFILE hermes -p "$p" config set memory.user_profile_enabled false
  env -u HERMES_HOME -u HERMES_PROFILE hermes -p "$p" config set platform_toolsets.api_server '[safe]'
done
```

The model picker selects provider/model and authenticates independently, via
API key or its supported OAuth flow. Never copy OAuth refresh stores between
profiles. `hermes -p NAME setup` is available for other tool credentials;
`hermes -p NAME auth add PROVIDER` adds a provider credential explicitly. Keep
provider secrets in that profile's protected `.env`/auth store, not in YAML or
the bridge's env. See official
[models/providers](https://hermes-agent.nousresearch.com/docs/user-guide/providers)
and configuration docs. Do not put an API secret into `config set` arguments.

`gateway.multiplex_profiles=false` prevents duplicate standalone ownership.
Do not also install Hermes's multiplex/default gateway for these same profiles.
`platform_toolsets.api_server` is consumed by Hermes but 0.21.3's generic setter
may print a schema notice after successfully saving it; inspect with
`hermes -p NAME tools` and review effective tool choices. `safe` excludes terminal
access but includes network/image tools and is not a sandbox or zero-cost mode.
Add business skills/MCP/tools explicitly per profile only after reviewing access,
approvals and cost. Phone PINs do not make privileged agent tools safe. Memory
is off initially; enable only under an agreed retention policy. The templates
are not a promise of parity with a pre-existing private assistant.

With approval for a paid model request, test each explicitly:

```bash
for p in phonebot jyoder atlas; do
  env -u HERMES_HOME -u HERMES_PROFILE hermes -p "$p" chat -q 'Reply with READY only.'
done
```

This is real inference, not part of the offline helper suite. Check permissions
on each credential store after setup; `.env`/auth files should be 0600 and private
profile directories 0700. Do not display their contents in support reports.

### Optional: independent Atlas research

The three-profile bootstrap does not enable native work or create its worker.
Read [native work behavior and security](NATIVE-WORK.md), then follow the separate
[standalone worker setup](ATLAS-WORKER-SETUP.md). Keep the foreground gateways
standalone: creating `atlas-work` does not expose `/p/atlas-work` on Atlas's API.
Use a separate literal-loopback gateway with its own worker bearer key, explicit
bridge `api_key_env`, restricted worker policy, and the foreground Atlas skill.
Do not forward the Atlas token to a different origin. Actual-source compatibility
checks and separately authorized runtime validation are prerequisites to activation;
offline tests and publication alone do not establish live acceptance.

## 8. Media permissions and manual systemd activation

Create the bridge media directories. Complete the exact shared-volume and
Asterisk UID/GID permission checks in [WINDOWS-PBX](WINDOWS-PBX.md):

```bash
mkdir -p "$REPO/phonesys/media/sounds" "$REPO/phonesys/media/rec"
```

Asterisk must read sounds and write recordings; the bridge must read recordings
and write sounds. Determine actual runtime IDs and grant a dedicated shared
group or ACL narrowly. Do not recursively chmod 777 or chown PBX volumes. Test
write/read access as the actual Asterisk user, not only root. Private secrets
remain 0600 regardless of media sharing permissions.

Only after prerequisites, PBX/relay and credential/model checks are complete:

```bash
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/hermes-phonebot.service" \
  "$HOME/.config/systemd/user/hermes-jyoder.service" \
  "$HOME/.config/systemd/user/hermes-atlas.service" \
  "$HOME/.config/systemd/user/phonesys-bridge.service"
systemctl --user daemon-reload
systemctl --user enable --now hermes-phonebot.service hermes-jyoder.service hermes-atlas.service
# Check gateways before bridge. Bridge startup may synthesize paid filler audio.
for port in 8642 8643 8644; do curl --fail "http://127.0.0.1:$port/health"; done
systemctl --user enable --now phonesys-bridge.service
systemctl --user --no-pager status hermes-phonebot hermes-jyoder hermes-atlas phonesys-bridge
```

If unattended startup after logout is intended, explicitly authorize
`sudo loginctl enable-linger "$USER"`. WSL must itself be started after Windows
reboots; see the Windows guide for persistence and relay setup. The helper does
not enable linger or create Windows tasks. API keys remain mandatory even on
loopback; `/health` alone is not authenticated inference verification.

## 9. Acceptance and handoff

Follow [OPERATIONS](OPERATIONS.md). Verify ARI app registration, API health and
**authenticated** gateway inference independently. With a scoped destination,
budget and human approval, test 7000/7001, correct and incorrect John/Atlas PINs,
directory transfers, STT/TTS, interruption, hangup and optional hold music. Do
not place unsolicited PSTN calls or infer end-to-end success from green tests.
Retain an encrypted backup and record actual service versions/configuration.
This release's isolated tests do not certify a fresh real-host deployment.
