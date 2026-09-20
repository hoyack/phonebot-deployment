# Optional standalone Atlas work gateway

This is an **opt-in, separate native Hermes gateway**, not another agent executor.
Use it when the foreground Atlas gateway is isolated (`gateway.multiplex_profiles:
false`): its `/p/atlas-work` route is not served. Do not enable multiplexing or
broaden foreground tools to make that route work. The worker instead serves the
native API at **`http://127.0.0.1:8645`**, with its own token.

`scripts/setup-atlas-worker.py` prepares files and applies bounded worker settings
through **`hermes -p atlas-work config set …`**. It never runs a gateway, checks or
binds a port, contacts a provider/model, invokes systemctl, enables a service, or
restarts anything. Review/start are separate operator decisions. Do not run it
against an active worker; stop that worker first during a maintenance window.

## Prerequisites and scope

- Python 3.10+ with PyYAML (the project's/Hermes Python environment is suitable).
- An explicit absolute, regular executable Hermes launcher, supporting
  `gateway run --external-supervisor` and structured `config set` values.
  Symlink launchers are refused; select their reviewed real launcher instead.
- An **already created** `$HOME/.hermes/profiles/atlas-work` profile with its own
  `config.yaml`, explicit `model.default`, `model.provider`, and private `.env`.
  Alternative names must end in `-work` or `-worker`; `default`, `atlas`,
  `phonebot`, `jyoder`, and arbitrary foreground names cannot be selected.
  The canonical HOME path ensures `-p NAME` and `--profile-dir` select the same
  directory. Custom profile roots are intentionally not supported.
- Provision the selected provider and web credentials privately in that worker
  first. Never clone a foreground profile or copy its `.env` wholesale.
  Supported credential preflights: Nous (`NOUS_API_KEY`), OpenAI, Anthropic,
  OpenRouter, DeepSeek, Groq, xAI (their respective `*_API_KEY`). Missing selected
  credentials fail before writes; ambient shell/default-profile credentials are
  not accepted. Auto provider selection, OAuth-only and local provider setups
  are deliberately not guessed. Credential presence is checked, **not validity**.
  Provision a working web backend separately (for example `FIRECRAWL_API_KEY`).
- All target directories must exist, be owned by the current user and private
  (`0700`); existing config/env/unit files must be private (`0600`). Ancestors
  cannot be group/world writable (root-owned sticky `/tmp` is allowed above
  private test directories). Symlink components, hard-linked files and path
  metacharacters are refused. Spaces in paths are supported.
- The worker must be API-only: every supported non-API platform (built-in and
  bundled plugin, including local/webhooks/relay) is explicitly disabled. Unknown
  platform names, ambiguous `gateway.<platform>` overlays, `gateway.json`, `.op.env`
  and external `secrets` configuration are refused rather than guessed.
- Worker `.env` uses a **positive allowlist**, not platform prefixes: the supported
  provider keys above; `FIRECRAWL_API_KEY`, `FIRECRAWL_API_URL`, `TAVILY_API_KEY`,
  `BRAVE_API_KEY`; and the helper's API/multiplex/safe-mode controls. Any other key,
  even empty, fails before writes. Request review to add a different web backend.
  No credential files are copied wholesale. Do not install unreviewed hooks or
  extensions in this profile. This is a tool restriction, **not an OS sandbox**.
- Managed installations (`/etc/hermes`) are not supported: the generated unit's
  `ExecStartPre` refuses to start if that path exists. Never remove an administrator's
  policy to make this check pass; obtain a separately reviewed managed deployment.

For a new deployment, the separate, explicit profile-creation step is:

```bash
"$HOME/.local/bin/hermes" profile create atlas-work --no-skills --no-alias
```

Skip this if it already exists. Do not use `profile use`, `--clone`, `--clone-all`,
`--clone-from`, or `--clone-channels`. Select/configure the model for **this profile**
using the supported Hermes UI/CLI and provision credentials privately before
running the helper. Model choice and credential provisioning are intentionally
not automated here. The helper preserves the existing model/provider block,
provider/web credential lines, and unrelated config settings.

## Prepare (does not activate)

Run from this repository, as the intended user, not through sudo. Review the
absolute launcher and file locations below. The bridge env must be the explicit
file already consumed by that bridge's service, not a foreground profile env.
Back up the worker config/env and bridge env in a private directory before first
use if an exact rollback is required; backups contain credentials.

```bash
umask 077
mkdir -p "$HOME/.config/systemd/user" "$HOME/.config/phonesys"
chmod 700 "$HOME/.hermes/profiles/atlas-work" \
  "$HOME/.config/systemd/user" "$HOME/.config/phonesys"
chmod 600 "$HOME/.hermes/profiles/atlas-work/config.yaml" \
  "$HOME/.hermes/profiles/atlas-work/.env"
# If an existing bridge.env is not private, review its ownership, then:
# chmod 600 "$HOME/.config/phonesys/bridge.env"

python scripts/setup-atlas-worker.py \
  --profile-dir "$HOME/.hermes/profiles/atlas-work" \
  --hermes-executable "$HOME/.local/bin/hermes" \
  --unit-dir "$HOME/.config/systemd/user" \
  --bridge-env "$HOME/.config/phonesys/bridge.env" \
  --port 8645

systemd-analyze --user verify "$HOME/.config/systemd/user/hermes-atlas-work.service"
```

All four path options are required; only `--port` has a default (8645). An explicit
alternative unprivileged port is allowed except 8642–8644 (foreground gateways)
and 9010 (bridge). There is no host option: the host is literal `127.0.0.1`.
The helper does not discover other listeners; the operator must ensure the
chosen alternate port is free before activation.

The resulting worker policy is:

- API platform enabled; all reviewed non-API platforms have `enabled: false`;
  `platform_toolsets.api_server: [web, no_mcp]` only.
- `agent.max_turns: 8`; persistent memory and user profile memory disabled.
- `approvals.mode: manual` (the supported interactive mode); `cron_mode`,
  `single_query_mode`, and `unattended_mode`: `deny`. `mode: deny` is not valid.
- `gateway.multiplex_profiles: false`; empty MCP/plugin configuration and no
  configured context engine. No changes to any foreground profile.
- `API_SERVER_ENABLED=true`, `API_SERVER_HOST=127.0.0.1`, `API_SERVER_PORT=8645`;
  independent generated `API_SERVER_KEY` in the worker's private `.env`.
- That same token written **only** as `HERMES_ATLAS_WORK_API_KEY` in the explicit
  private bridge env. Existing unrelated lines/keys are preserved.

**Source compatibility:** inspected Hermes revision
`c661785f872b5647fbac7c138d965180783bd9af`. `gateway/config_loader.py` marks explicit
`enabled: false` settings with `_enabled_explicit`; `gateway/config_env.py`
`_enable_from_env()` and `_enable_plugin_platform()` honor that marker even when
credentials are present. The loader merges `gateway.<platform>` **after** top-level
platform settings, hence the helper refuses those overlays. Do not depend on
missing credentials alone, or merely disable four newly discovered families.

`hermes_cli/env_loader.py:418–432` loads the worker `.env`, then fills missing keys
from the **source-checkout `.env`**, even with `HERMES_HOME` set. There is no invented
"disable dotenv" flag here: the explicit non-API disables prevent those credentials
from enabling adapters. `HERMES_SAFE_MODE=1` is pinned in the unit and worker `.env`;
`hermes_cli/plugins.py::discover_and_load()` supports it and skips plugin discovery.
This is adapter isolation, **not a claim that source-checkout secrets cannot enter
process memory**. Use a clean, reviewed source checkout for credential separation.
The helper never reads or copies the source-checkout `.env`.

`gateway.multiplex_profiles: false` plus `GATEWAY_MULTIPLEX_PROFILES=false` disables
multiplexing. `API_SERVER_KEY/HOST/PORT` are supported by `gateway/config_env.py`.
Unused `HERMES_GATEWAY_PORT` and `HERMES_SINGLE_PROFILE` stamps were removed: they
are not security controls. Old generated env files containing them require private
review/removal before preparation. Settings otherwise use the Hermes config CLI.

The unit invokes the launcher through `/usr/bin/env -i`, explicitly supplying only
canonical `HOME`, a fixed system `PATH`, `HERMES_HOME`, `HERMES_SAFE_MODE=1`, and
`HERMES_MANAGED_DIR=/etc/hermes`. This discards the user manager's inherited platform
credentials and process-routing variables, including future unknown names. There
is no `EnvironmentFile`: Hermes loads the selected worker `.env` itself. Pinning
`HERMES_MANAGED_DIR` to its default prevents source fallback from selecting another
managed overlay; a pre-start check refuses an existing `/etc/hermes` rather than
bypassing its policy. The reviewed launcher must work with this minimal environment
and actually use the tested Hermes checkout, not source unrelated shell env files.

It uses `UMask=0077`, `NoNewPrivileges=true`, and `Restart=on-failure`; no secret
arguments, shell sourcing, broad `--replace`,
`--all`, or lifecycle commands are generated/executed by the helper. CLI output
is suppressed so it cannot leak credential diagnostics. Failure is reported
without echoing file contents. All preflight failures occur before CLI writes;
a later CLI/I/O failure can leave partial settings/files, **not an active service**.
Writes are individually atomic, not a transaction across three files. Do not
edit the same targets concurrently. Policy/model readback is verified before
any token is distributed.

## Manual bridge integration and activation

In the **existing Atlas extension's** `native_work` section, select the standalone
endpoint and separate token variable (retain the configured socket directory):

```yaml
native_work:
  base_url: http://127.0.0.1:8645
  api_key_env: HERMES_ATLAS_WORK_API_KEY
  directory: /absolute/private/native-work-sockets
  worker_config: /absolute/home/.hermes/profiles/atlas-work/config.yaml
```

Do not append `/p/atlas-work` to this standalone URL. This does not change the
foreground Atlas endpoint/token or enable work for another extension. See
[NATIVE-WORK.md](NATIVE-WORK.md) for PIN/streaming requirements and the foreground
skill. The helper does not edit bridge configuration or install that skill.

**Activation remains blocked** until the offline actual-source integration below
passes against the exact checkout used by the reviewed launcher, source/config/env
inputs are reviewed, and separate operational authorization is granted. The helper
only prepares files: it does not attest an arbitrary launcher, freeze the installed
Hermes version, or protect against subsequent owner/admin edits. Repeat this review
after upgrades or configuration changes; an inventory mismatch is a stop condition,
not permission to skip a test. Then, and only with explicit operational authorization:

```bash
systemctl --user daemon-reload
systemctl --user start hermes-atlas-work.service
systemctl --user is-active hermes-atlas-work.service
```

Enabling at login is optional and separate:
`systemctl --user enable hermes-atlas-work.service`. Restart only the specific
bridge service consuming the edited bridge env during an approved window; do
not restart foreground Hermes gateways. Before real work, verify the native
worker toolset through the bridge's policy check: only `web_search` and
`web_extract` may be exposed. Setup tests do not prove live provider/API health.

## Token recovery, rotation and rollback

- **Lost bridge token only:** stop the worker during maintenance, then rerun the
  exact preparation command. It preserves a valid existing worker token and
  resynchronizes the bridge copy without printing it. An existing unit must be
  byte-identical; a differing unit is refused before any change. Review/archive
  only that worker unit before changing its port or launcher.
- **Rotate a compromised token:** stop only `hermes-atlas-work.service`. Using a
  private local editor, remove only the worker `.env` `API_SERVER_KEY` assignment
  (never paste it into chat, arguments, logs or source control). Rerun the helper;
  a new 256-bit random token replaces the bridge copy. Restart only this worker
  and the consuming bridge after authorization. Existing weak/non-URL-safe
  tokens are refused, not silently reused; remove that assignment to rotate.
- **Partial preparation failure:** do not start the unit. Resolve the reported
  preflight/CLI issue and rerun while stopped. Existing generated tokens are
  retained, so recovery does not gratuitously rotate them. Restore an exact
  private backup only if intentionally abandoning setup; never copy default
  profile credentials as a recovery shortcut.
- **Disable this optional feature:** remove/disable just the Atlas `native_work`
  section in bridge configuration and reload the bridge in an approved window.
  Then, if this worker was activated:

  ```bash
  systemctl --user disable --now hermes-atlas-work.service
  rm -- "$HOME/.config/systemd/user/hermes-atlas-work.service"
  systemctl --user daemon-reload
  ```

  Privately remove only `HERMES_ATLAS_WORK_API_KEY` from the explicit bridge env.
  Leave the worker profile restricted and stopped for audit/reuse, or archive it
  privately. Do not delete its provider credentials, erase all profiles, change
  multiplexing, or stop any foreground gateway. No blanket rollback/restart is
  built into the helper.

## Reproducible offline verification

```bash
python -m unittest discover -s tests_deployment -p test_atlas_worker.py -v
python -m pytest tests_deployment -q
# Required before activation: use that checkout's Python with its dependencies.
HERMES_TEST_SOURCE=/absolute/reviewed/hermes-checkout \
  /absolute/reviewed/hermes-checkout/venv/bin/python -m unittest \
  tests_deployment.test_atlas_worker -v
```

Preparation tests use temporary HOME directories, dummy credentials, and a fake
Hermes config-only executable. They check CLI targeting, policy/model preservation,
secret non-disclosure, token recovery, permissions/link/path guards, allowlisting,
overlay refusal and native systemd syntax. The extracted `env -i` command is executed
with synthetic inherited credentials to verify environment isolation without systemd.
Where installed, `systemd-analyze --user verify` parses the generated unit only.

With `HERMES_TEST_SOURCE` set, the integration test imports **actual Hermes**
`load_hermes_dotenv()` and `load_gateway_config()`. It checks all built-in and bundled
platform names, injects every current credential-gated platform's synthetic keys,
and separately exercises inherited env and a synthetic source-checkout `.env`.
A positive control removes explicit disables in the temporary config and proves
Mattermost/Home Assistant/WeCom/WeCom callback/BlueBubbles really become enabled;
restoring the prepared policy leaves **only API enabled**, with loopback host/port
and multiplexing disabled. Socket connect/bind are blocked in that subprocess.
Without `HERMES_TEST_SOURCE`, that integration test explicitly skips; unit-test
success alone is **not** source compatibility evidence.

No live profiles, services, ports, providers, model requests or real credentials
are used. The actual-source test does not start adapters or the gateway, validate
provider credentials, or establish live voice/tool health. Do not edit config or
install managed policy while the worker is running; stop and re-review first.
