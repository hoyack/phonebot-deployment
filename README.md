# FreePBX + Hermes telephone assistants

Private deployment source for a Docker FreePBX/Asterisk PBX, a Python ARI/AudioSocket voice bridge, three isolated Hermes phone profiles, and the Windows→WSL AudioSocket relay.

**Easy deployment: start at [docs/free-pbx-deploy.md](docs/free-pbx-deploy.md)**
for the default-dry-run `scripts/deploy.py` helper, container FreePBX preparation,
Telnyx trunk setup, and receptionist plus two protected profiles. Prerequisites
for PSTN activation are an existing Hermes install, deployed FreePBX and a
functional SIP trunk. The helper prepares files; activation stays explicit.
The detailed [setup guide](docs/SETUP.md) and [Windows/PBX guide](docs/WINDOWS-PBX.md)
remain authoritative for model authentication, networking and activation.
These are fresh-host procedures, not an in-place updater for an existing system.

## What runs where

```text
SIP phone or optional carrier
    ↕ SIP / RTP
FreePBX + Asterisk (Docker) ─── ARI control ─── phonesys (WSL user service)
    ↕ AudioSocket                                    ↕
Windows TCP 19010 → WSL TCP 9010               Deepgram STT / ElevenLabs TTS
                                                     ↕
                                      Hermes API gateways (loopback only)
                                      Mary 8642 / John 8643 / Atlas 8644
```

Routes in the supplied bridge configuration:

- **7000** — turn-based phonebot / Mary, API port 8642.
- **7001** — streaming Mary receptionist, directory transfers and barge-in.
- **7002** — streaming John Yoder, PIN protected, API port 8643.
- **7003** — streaming Atlas, PIN protected, API port 8644; consent-based hold music, bounded work, caller-floor protection.

The default Archon/Slack profile is **not** a telephone route. Profile histories, credentials and memories stay separate. Fresh-install API/PIN secrets are generated locally, not shipped in Git.

## Start here

1. Read the [complete setup guide](docs/SETUP.md): prerequisites, supported layout, credentials, isolated profiles/model auth, Python environment, service installation and activation.
2. Follow [Windows, Docker, FreePBX and networking](docs/WINDOWS-PBX.md): initial database/PBX setup, four-extension dialplan, ARI, SIP/RTP/NAT, internal endpoint, optional carrier routing, Windows relay and autostart, media permissions and hold music.
3. Run the documented offline checks **before** activating services. Verify health and actual media routing separately; a healthy HTTP endpoint does not prove telephone audio.
4. Use [operations, updates and recovery](docs/OPERATIONS.md) for backups, restore, updates and rollback. Never run a fresh installer over an existing profile or service.

Helper entry points document supported options with `--help`. Installation prepares files but does not silently start Docker, open firewall rules, place calls, or replace an existing system.

## Optional Atlas native work

[Native work and caller feedback](docs/NATIVE-WORK.md) describes opt-in independent
read-only research, explicit result retrieval, cooperative cancellation and bounded
proactive notices. Follow [standalone worker setup](docs/ATLAS-WORKER-SETUP.md) for
a separate loopback gateway and worker bearer key; standalone Atlas does not expose
`/p/atlas-work`. The shipped bridge configuration does not enable this feature.
Source publication and offline checks are not live deployment or call acceptance.

## Repository contents

- `phonesys/` — current bridge source/configuration and isolated offline tests.
- `scripts/` — safe bootstrap, validation and backup helpers.
- `templates/`, `systemd/` — fresh-install profile/service inputs.
- `freepbx/` — Compose, database initialization, dialplan/ARI/MOH examples; no live PBX state.
- `windows/` — parameterized AudioSocket relay and startup guidance.
- `phonebot/`, `atlas/` — reviewed declarative voice-assistant policy; customize names/authorization for your deployment.
- `docs/` — installation, operations, scope and behavioral verification notes.
- `.github/workflows/` — offline application/deployment validation on proposed changes.

## Secrets, safety and external dependencies

You need Docker/Compose, Linux user systemd (WSL2 for the documented Windows topology), Python 3.11+, Hermes Agent, model-provider access, Deepgram and ElevenLabs accounts. A SIP trunk/DID is optional for internal calls. Provider plans, voice availability, model names, carrier routing and local network addresses must be selected/verified on the target host.

- **Never commit** `.env`, API keys, PINs, trunk passwords, OAuth data, profile databases/history, recordings, logs, or PBX volume/database archives.
- ARI and Hermes APIs stay loopback-bound on the host. Restrict SIP/RTP and relay firewall access to intended peers. The supplied no-fail2ban Docker image is not a substitute for network controls.
- The relay's documented concurrency limit matters: do not advertise production multi-call capacity from bridge unit tests.
- Hold music must be a locally licensed/cleared file; the operator's original audio is intentionally not redistributed. Voice IDs are identifiers, not credentials, and still require access in your ElevenLabs account.
- Startups may pre-synthesize greetings/filler and incur provider charges. Live calls, dry-run speech/model requests and carrier tests are separate opt-in actions.
- No automatic durable callback, DTMF hold menu, or Archon task-handoff service is promised by this repository. Optional MCP/Upwork/business integrations require separate installation and authorization.

## Runtime synchronization and compatibility

The established runtime lives at `/home/hoyack/work/phonesys`, while its deployment snapshot lives at `/home/hoyack/work/phonesys-deployment`. Those are reference host paths, **not installation requirements**. The base publication captured an allowlisted runtime snapshot; this branch adds reviewed native-work source and setup helpers that require separate deployment. Do not assume source/runtime byte equality from publication. Runtime secrets and media are excluded.

Fresh installation may render path/credential-specific settings rather than copying machine-specific paths verbatim. The running installation is not modified by publication. Do not overwrite runtime dirty changes from a Git checkout: compare, review, preserve rollback copies, test, then perform a no-active-calls bridge-only restart if a runtime update is actually needed.

[Compatibility and verification](docs/RELEASE-VERIFICATION.md) records tested versions, actual checks, and remaining fresh-host/manual acceptance boundaries. Historical `phonesys/HANDOFF.md`, superpowers design notes, and older incident documents are not current installation instructions.
