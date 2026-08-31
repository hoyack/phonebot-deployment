# Phonebot deployment

Private deployment source for the local FreePBX/Asterisk AI receptionist stack.
The active Hermes profile is `phonebot`; its spoken identity is Mary.

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

## Deploy

1. Clone the repository and install Docker Engine, Compose, Python 3.11+, and Hermes Agent.
2. Run `scripts/install.sh`.
3. Populate `~/.config/phonebot/phonebot.env` from `.env.example`.
4. Create the files listed in `freepbx/secrets/README.md`.
5. Restore a current runtime backup, or perform the FreePBX initial setup described
   in `freepbx/README.upstream-notes.md`.
6. Run `scripts/validate.sh`, then enable the user services.

Host-specific NAT addresses, phone numbers, trunk credentials, and PBX database
state must be reviewed when moving to another host.

