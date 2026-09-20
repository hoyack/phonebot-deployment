# Deploy FreePBX, Telnyx and three Hermes phone profiles

This is the short entry point for **fresh installations**, not an updater. Start
with `python3 scripts/deploy.py`: it prints the prerequisites and makes no changes.
Every subcommand is also a **dry-run by default**. `--apply` creates local files
only; it never invokes Docker, Hermes, systemd, provider APIs or a phone call.
Do not run these helpers over a working PBX, existing profiles or runtime media.

## Prerequisites and order

For a public telephone receptionist you need:

1. An **existing Hermes installation**, an executable launcher, and independent
   model authentication for each telephone profile. Install Hermes separately
   using the [official documentation](https://hermes-agent.nousresearch.com/docs/).
2. A **deployed FreePBX/Asterisk** with functioning SIP/RTP, ARI and AudioSocket.
   If absent, prepare a new container project below, then provision it manually.
3. A **functional SIP trunk and DID**, such as Telnyx, already verified with an
   ordinary SIP phone before involving AI. Internal-only extension tests can
   omit the carrier; record that as an internal-only deployment, not PSTN-ready.
4. Python 3.11+, Docker/Compose v2 for a new PBX, Linux user systemd, `uv`, ffmpeg,
   funded Deepgram/ElevenLabs accounts, and access to each configured TTS voice.
   Use [SETUP.md](SETUP.md) for locked bridge dependencies and model credentials.
5. Reviewed port/subnet availability, firewall/NAT, shared-media permissions,
   caller consent/retention, and explicit approval for any paid acceptance calls.

Use canonical absolute Linux paths (including the resolved Hermes executable),
not symlinks or Windows `/mnt/c` storage. Paths with spaces work. Traversal,
control characters, `$`, `%`, double quotes and backslashes are refused. Run as
the intended service user, not root. No live credentials belong in command-line
arguments, Git, chat, or captured terminal output.

Already have FreePBX and a trunk? **Skip container and trunk creation** and go
to [Prepare the phone profiles](#prepare-the-phone-profiles). Keep your existing
carrier credentials, networking and data; make reviewed ARI/dialplan additions
with private backups rather than running a fresh installer.

## Optional: prepare a fresh container deployment

The helper reuses `freepbx/docker-compose.yml`, SQL, config examples and the
existing guarded installer; it does not introduce a second PBX implementation.
It copies these to a **new private directory outside the checkout**, generates
new database secrets and creates dedicated bridge media directories. It refuses
existing output, secrets, media directories and symlink paths; there is no force
or reset option. A failed disk write can leave partial *new* files: inspect them
and use a new path, never remove live data to bypass a guard.

From the checkout root:

```bash
REPO=$(pwd -P)
PBX_DIR="$HOME/phonebot-pbx"             # must not exist
PHONESYS_DIR="$REPO/phonesys"            # must exist; fresh media dirs only
# Set these to your reviewed, architecture-compatible image digests:
PBX_IMAGE='escomputers/freepbx@sha256:<64-lowercase-hex-digest>'
DB_IMAGE='mariadb@sha256:<64-lowercase-hex-digest>'
python3 "$REPO/scripts/deploy.py" pbx \
  --output "$PBX_DIR" --phonesys-dir "$PHONESYS_DIR" \
  --pbx-image "$PBX_IMAGE" --db-image "$DB_IMAGE"
# After reviewing the plan, repeat the same command with --apply.
```

Replace the placeholders; they intentionally fail validation. Choose reviewed
FreePBX 17 **nofail2ban** and MariaDB 10.11 images compatible with the shipped
installer. [WINDOWS-PBX.md section 3](WINDOWS-PBX.md#3-secrets-database-initialization-and-gui)
records previously observed digests, not a guarantee of current security or a
fresh-host installation. The helper validates digest *syntax*, not registry
existence, provenance, architecture or version. It does not pull anything.
Record image/module versions and retain the generated Compose file for repeatable
image selection. New installations deliberately generate different secrets.

Generated files are mode 0600 in a 0700 project, including:

- `docker-compose.yml` with **digest-pinned images**, three persistent named
  volumes and an unpublished database port;
- `.env` with the exact absolute `PHONESYS_DIR` and loopback admin bind;
- `secrets/` database credentials and an empty SMTP credential file (mail is not
  configured); ARI, GUI and SIP credentials remain separate;
- `init.sql`, `my.cnf`, `install-fresh.sh` and ARI/dialplan/MOH examples.

The generated `.env` is Compose configuration; **never source it as shell code**.
Default network 10.77.0.0/24 must not overlap LAN, VPN, WSL or another stack. Edit
all of `PBX_SUBNET`, `PBX_GATEWAY`, `PBX_DB_IP` and `PBX_IP` consistently if needed.
Review published ports before startup:

| Host publication | Intended access |
|---|---|
| TCP 8080 / 8443 | Loopback FreePBX GUI; VPN/tunnel for remote administration |
| TCP 8088 | Loopback ARI only |
| UDP 5060 | PJSIP, restricted to trusted phones and carrier signaling ranges |
| UDP 10000–10099 | RTP, restricted to approved media peers |
| TCP 8642–8644 | Loopback authenticated Hermes APIs, outside Docker |
| TCP 9010 / Windows 19010 | AudioSocket/relay, trusted PBX path only |

**SIP/RTP bind all host interfaces in the template. Enforce network restrictions
before starting it.** This third-party image is not an official Sangoma appliance;
no-fail2ban requires external controls. Docker NAT can hide original source IPs.
Do not publish database, ARI, Hermes, AudioSocket or admin access to the Internet.

### Explicit manual container activation (not run by the helper)

After port/subnet/firewall review on the **new host**:

```bash
cd "$PBX_DIR"
docker compose config --quiet           # render only; avoid dumping secrets
docker compose pull                    # network/download, explicit operator step
docker compose up -d db
docker compose exec -T db healthcheck.sh --connect --innodb_initialized
# Continue only after database health succeeds.
docker compose up -d freepbx
docker compose ps
bash install-fresh.sh                  # dry-run, no Docker commands
```

Complete the **two-schema connection check from inside FreePBX** and guarded
`bash install-fresh.sh --apply` procedure in
[WINDOWS-PBX.md section 3](WINDOWS-PBX.md#3-secrets-database-initialization-and-gui).
For the generated project, replace that guide's `cd "$REPO/freepbx"` with
`cd "$PBX_DIR"` throughout. Its `../phonesys` media examples must use your exact
`"$PHONESYS_DIR"` instead. **Skip that guide's prepare.py generation commands:**
the new helper already created secrets/media. Do not generate them twice.

The install must use database host `db`, user `freepbxuser`, and both `asterisk`
and `asteriskcdrdb` schemas. Never retry a partial installer automatically or
use `--force`, remote root, or `docker compose down -v` to troubleshoot data you
need. Complete GUI admin creation, updates and private backups. Configure SIP
transport, external/local addresses and RTP 10000–10099, then test an ordinary
PJSIP phone (e.g. 1001) as in the existing guide. Do not create ordinary PJSIP
endpoints for 7000–7003; those are bridge custom destinations.

## Telnyx SIP trunk and inbound receptionist route

Telnyx setup is an **operator task**, not automated by this repository. The
provider references below describe FreePBX **15**, while this project targets
17; confirm corresponding UI/settings in your installed version. Do not copy
an old ISO installation, carrier IP allowlist, catch-all route or dial pattern
unchanged. Public reference pages were consulted; no Telnyx account, trunk API,
registration or call was exercised for this change.

### 1. Provision the connection in Telnyx

- Use your own authorized, funded Telnyx account and a voice-capable DID.
  Create a SIP connection and assign the DID to **that exact connection**.
- Choose **credentials authentication** (unique SIP username/password) or
  **IP authentication** (your actual stable public source IP). These are
  alternatives, not settings to combine blindly. A private Docker/WSL IP is
  not the carrier-facing public IP.
- For authorized outbound calls only, assign an outbound voice profile and
  restrict destinations, spend, caller ID and concurrency. An inbound
  receptionist does not require broad outbound routes. Establish emergency
  calling arrangements separately; this assistant is not an emergency service.
- Match region/proxy, transport, inbound number format and codecs to the PBX.
  Fetch the provider's current signaling/media ranges for firewall allowlists
  rather than relying on historical copied addresses.

### 2. Configure the FreePBX PJSIP trunk

Use **Connectivity → Trunks → Add SIP (chan_pjsip) Trunk**, not legacy chan_sip.
Give it a distinct name such as `telnyx-reception`. For a credential connection:

| Field | Value / source |
|---|---|
| Username / Auth Username | SIP connection username, not Telnyx portal login |
| Secret | SIP connection password, entered privately in FreePBX |
| Authentication / Registration | Outbound / Send for the registration-based credential design |
| SIP Server | Proxy selected for your Telnyx region; official US example is `sip.telnyx.com` |
| SIP Server Port / Transport | 5060 UDP for this template's baseline; review TLS separately |
| Codecs | Begin with G.711 ulaw/alaw supported by the connection |
| DTMF | RFC4733 (often labeled RFC2833), then verify against the actual path |
| Context | External inbound processing (`from-pstn`), **never `from-internal`** |

For IP authentication follow the separate provider IP-trunk guide: do not
supply invented credentials or require registration as a readiness test.
Configure source matching to the current approved provider peers. Keep anonymous
and guest SIP disabled. Under NAT, review RTP symmetric/force rport/rewrite
contact and disable direct media where needed; verify the advertised SDP address
and media ports are actually reachable. Registration alone does not prove RTP.

Telnyx recommends TLS. The supplied Compose publishes UDP only; encrypted SIP
and SRTP need a separately reviewed transport, certificate, port/firewall and
media configuration on both sides. Selecting TLS in one GUI does not secure
this UDP example. Do not expose extra ports casually.

### 3. Prove trunk delivery before connecting the assistant

1. Route the exact DID temporarily to the ordinary test phone. Match the DNIS
   format actually configured/delivered by Telnyx, including country code and
   whether `+` is present; do not assume all countries use eleven digits.
2. Apply Config in an approved idle window. Inspect `pjsip show registrations`
   for credential registration, or endpoint/identify configuration for IP auth.
3. With an explicitly agreed number, participant and cost limit, test inbound
   ringing, answer, sustained two-way audio, DTMF and hangup. Test outbound only
   if required and separately authorized. Keep SIP logs private (numbers and
   authentication metadata); turn debugging off afterward.
4. Only after that succeeds, add the bridge ARI user/dialplan and **Admin → Custom
   Destinations → `phonesys-custom,7001,1`**, Return disabled. Set the **exact DID**
   inbound route to that receptionist destination. Do not use an any-DID route
   or expose a private assistant directly. Preserve the previous route for rollback.

Merge the shipped custom dialplan rather than overwriting unrelated contexts;
use ARI application `phonesys`, user `phonesys`, and the same ARI secret in PBX
and bridge. See [WINDOWS-PBX.md section 5](WINDOWS-PBX.md#5-http-ari-and-four-extension-dialplan).

Official references:

- [Telnyx FreePBX 15 credentials / PJSIP](https://support.telnyx.com/en/articles/5619597-freepbx-v15-credentials-pjsip)
- [Telnyx FreePBX 15 IP trunk / PJSIP](https://support.telnyx.com/en/articles/5619595-freepbx-v15-ip-trunk-pjsip)
- [Telnyx connection number formats](https://support.telnyx.com/en/articles/1130706-sip-connection-number-formats)

## Prepare the phone profiles

The helper uses `scripts/setup.py` as the single bundle/staging implementation.
It creates **new**, isolated identities, not copies of any existing profile:

| Route | Profile | API port | Access |
|---|---|---|---|
| 7001 (7000 fallback) | phonebot / Mary receptionist | 8642 | Public receptionist |
| 7002 | jyoder / John | 8643 | `JOHN_YODER_PIN`, independent API key |
| 7003 | atlas / Atlas | 8644 | `ATLAS_PIN`, independent API key |

Each profile gets a separate bearer key and private environment; generated John
uses `HERMES_JYODER_API_KEY`, not Mary's key. Both protected routes retain their
PIN policy. PINs are independently generated eight-digit values; distribute
through a password manager, never in greetings. The default/Slack profile and
all existing memories, sessions and credentials are untouched. Review names,
receptionist business facts, directory visibility, model and tool permissions.

```bash
REPO=$(pwd -P)                         # run from the checkout root
HERMES_BIN=$(readlink -f "$(command -v hermes)")
BUNDLE="$HOME/phonebot-fresh-bundle"     # must not exist
python3 "$REPO/scripts/deploy.py" profiles \
  --repo "$REPO" --home "$HOME" --hermes-bin "$HERMES_BIN" --output "$BUNDLE"
# After review, repeat the same command with --apply.
```

This checks the launcher exists/is executable but does **not** run it or certify
Hermes compatibility. Generated credentials are private and not printed. Fill
Deepgram/ElevenLabs credentials in `"$BUNDLE/bridge.env"` with a local editor.
Match the existing PBX ARI password; never rotate it implicitly. Review
`"$BUNDLE/config.yaml"` for voice IDs, ARI address, directory/PIN settings and
media topology. Complete [SETUP.md sections 3–5](SETUP.md) including the actual
offline YAML loader check with `scripts/run-bridge.py --runtime "$BUNDLE" --check`.

```bash
python3 "$REPO/scripts/deploy.py" stage --bundle "$BUNDLE" --home "$HOME"
# Only after reviewing the bundle and prerequisites:
python3 "$REPO/scripts/deploy.py" stage --bundle "$BUNDLE" --home "$HOME" \
  --apply --prerequisites-reviewed
```

Staging validates required credentials, private permissions, independent API
keys and PIN format before checking **all** fresh destinations. The default
plan performs those checks without copying files. `--prerequisites-reviewed`
is an operator acknowledgement, not a connectivity test. Existing profiles,
units or runtime targets are refused. Lower-level `setup.py` commands remain
available for compatibility; the easy entry point adds the validation gate.

Next follow [SETUP.md sections 7–9](SETUP.md): authenticate **each** profile using
the Hermes CLI, disable profile multiplexing, review low-privilege tools, then
manually activate the three gateways and bridge. No existing/default secrets
are copied. No worker, MCP account or background agent is provisioned implicitly.
Before activation, establish shared-media ACLs using the actual Asterisk UID/GID;
helper-created private media directories intentionally require that step.

On WSL, configure the Windows AudioSocket relay/firewall and logon behavior using
[WINDOWS-PBX.md section 7](WINDOWS-PBX.md#7-windows-relay-and-logon-startup). Its
current capacity is **one streaming call**, including transfers; limit ingress
accordingly. On native Linux, choose an actual container-reachable bridge address
and port instead of copying the Windows 19010 relay setting. Install licensed
`phonesys_hold` music or explicitly disable optional hold until provisioned.

## Acceptance, failure and rollback

Offline helper tests run without Docker, Hermes, systemd or provider access:

```bash
python3 -m pytest -q tests_deployment/test_deploy.py
python3 -m unittest discover -s freepbx -p 'test_*.py'
# Full helper/bridge suites require locked dev dependencies; see SETUP.md.
```

Tests prove file preparation/guards, not a fresh container install, valid Telnyx
registration, secure carrier access, model inference or usable telephone audio.
Before claiming deployment success, separately record:

- DB/GUI readiness, loaded ARI/dialplan, profile HTTP health and model auth;
- internal audio and trunk two-way audio, sustained media and hangup;
- **wrong/missing/correct PINs** on both direct private routes and receptionist
  transfers, without weakening restrictions;
- interruption/consent hold, retention policy, and reboot/restore behavior.

Calls and bridge startup may incur speech/model/provider charges. Obtain explicit
scope first. If routing fails, restore the prior exact DID destination; if a new
service fails, stop only the newly introduced service after checking active
calls. Do not wipe profiles or volumes. Retain encrypted backups and follow
[OPERATIONS.md](OPERATIONS.md) and the PBX guide's separate restore rehearsal.
No live deployment, service activation, carrier call or provider API validation
was performed in developing this helper.
