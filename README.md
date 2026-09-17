# Phonebot deployment

Private deployment source for the local FreePBX/Asterisk AI receptionist stack.
The telephone agents are isolated Hermes profiles: `phonebot` (Mary), `jyoder`
(John Yoder), and `atlas` (private personal assistant). FreePBX runs in Docker;
the voice bridge and Hermes gateways currently run as WSL user systemd services.

## Current host layout and source synchronization

- GitHub: `git@github.com:hoyack/phonebot-deployment.git` (private).
- Deployment checkout: `/home/hoyack/work/phonesys-deployment`.
- Active bridge checkout: `/home/hoyack/work/phonesys` (not `~/work/phonebot`).
- Active PBX Compose directory: `/home/hoyack/freepbx-docker`.
- Active profile homes: `/home/hoyack/.hermes/profiles/{phonebot,jyoder,atlas}`.
- Services: `phonesys-bridge`, `hermes-phonebot`, `hermes-jyoder`, `hermes-atlas`
  under `systemctl --user`.

The `phonesys/` subtree is the versioned bridge source/configuration snapshot.
Do not deploy an older snapshot over uncommitted runtime changes: compare first,
preserve a rollback copy, test, then synchronize the approved files. A Git push
alone does not update the running services. The local runtime checkout has its own
Git history and is not an upstream clone of this deployment repository.

Routes: 7000 → phonebot (turn-based, port 8642); 7001 → Mary (streaming,
8642); 7002 → John Yoder (streaming, 8643); 7003 → Atlas (streaming, 8644).
John and Atlas require PINs. The default Archon profile is not a telephone route.

See `docs/atlas-call-stalls.md` for the response-budget changes and caller test
checklist. Never commit `.env`, profile runtime databases, recordings, or PBX
volume dumps.

## Contents

- `phonesys/` — ARI/AudioSocket bridge, directory routing, DTMF transfer logic,
  streaming STT/TTS, barge-in handling, tests, and current local changes.
- `freepbx/` — FreePBX 17 / Asterisk 21 Docker Compose deployment and database bootstrap.
- `phonebot/` — sanitized declarative Hermes profile and receptionist prompt.
- `systemd/` — user-service templates for Hermes and phonesys.
- `scripts/` — installation, validation, and runtime-backup helpers.

## Secret and state boundary

This repository contains no live credentials or mutable PBX state. FreePBX trunks,
routes, extensions, ARI credentials, and GUI-managed values live in MariaDB and
Docker volumes. Generate a runtime archive with `scripts/backup-runtime.sh`, encrypt
it, and store it outside Git.

## Fresh-host bootstrap (legacy, Mary-only)

The scripts and `systemd/` files below are the original Mary-only bootstrap, not
a safe in-place updater for this host. They assume `~/phonebot-deployment`, can
overwrite the phonebot profile, and do not provision John or Atlas. **Do not run
`install.sh` on the existing installation to synchronize it.** Adding a complete
multi-profile fresh-host installer is separate from the call-stall repair.

1. Clone the repository and install Docker Engine, Compose, Python 3.11+, and Hermes Agent.
2. Run `scripts/install.sh`.
3. Populate `~/.config/phonebot/phonebot.env` from `.env.example`.
4. Create the files listed in `freepbx/secrets/README.md`.
5. Restore a current runtime backup, or perform the FreePBX initial setup described
   in `freepbx/README.upstream-notes.md`.
6. Run `scripts/validate.sh`, then enable the user services.

Host-specific NAT addresses, phone numbers, trunk credentials, and PBX database
state must be reviewed when moving to another host.

