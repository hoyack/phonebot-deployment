# Publication verification and compatibility

## Scope

The publication synchronizes the active bridge's reviewed Python source/tests,
configuration and dependency metadata into this private deployment repository,
then adds fresh-install helpers and documentation. It does **not** export a
working user's secrets, profiles, histories, recordings, database volumes or
licensed music. The running installation was not restarted or modified during
this publication work; original dirty worktrees were preserved.

## Known source/runtime difference: PIN audio privacy

Publication review reproduced a privacy defect in the previous runtime:
a successful local PIN authentication left 75 synthetic frames in the inbound
queue; later transcription received all 75 instead of only the one post-auth
frame. The publication fixes that defect in **two source files**:

- `phonesys/src/phonesys/audiosocket.py`: synchronous PCM suppression window,
  clearing backlog while preserving EOF and local DTMF detection; no sensitive
  listener exception detail in logs.
- `phonesys/src/phonesys/stream_call.py`: all protected authentication prompts,
  attempts and cleanup run inside that window, including transfers with an
  existing STT pump. No-PIN behavior remains unchanged.

Added `tests/test_pin_audio_privacy.py` (13 regressions), plus a no-op interface
on the existing queue-less test fake. RED: **2 failed in 0.06s**; GREEN privacy
suite: **13 passed in 0.08s**. The active installation does **not** receive this
change merely because GitHub is updated. Deploy only after a separately approved
idle-call maintenance action, with rollback copies and bridge-only restart.

Boundary: PCM delivered before/during authentication is kept local; audio already
sent before that window cannot be recalled, and delayed upstream packets/tone
tails delivered after it closes are not covered. This is not a claim that all
possible acoustic DTMF leakage is eliminated. Hold music and consent logic were
not changed by the privacy correction.

## Actually executed locally

- Fresh Python **3.11.14** environment installed with
  `uv sync --frozen --extra dev --python 3.11`, using committed `phonesys/uv.lock`.
- Full release bridge suite on that fresh environment:
  **403 passed, 1 deselected in 24.69s**.
- Same release source with existing Python **3.14.6** interpreter:
  **403 passed, 1 deselected in 24.74s**.
- Linux deployment-helper suite: **21 passed in 1.77s**.
- FreePBX helper suite: **7 tests, OK**.
- Independent temporary-home generate → validate → real bridge config `--check`
  → stage → backup cycle passed. Four unit files passed
  `systemd-analyze --user verify` (exit 0); repeated staging refused (exit 2).
- Independent reviewer read back all four documented Hermes settings in all
  three temporary profiles; no default config was created.
- Both Windows PowerShell helpers passed the actual Windows PowerShell parser.
  These were parsed, **not activated**.
- Compose rendered with dummy secrets in temporary directories, including
  paths with spaces, private ARI/unpublished DB and media mount declarations.
- Shell syntax and patch whitespace checks passed.
- Gitleaks **8.30.1**, upstream archive checksum verified, scanned current files
  and history. One exact historical README finding is allowlisted because its
  curl authentication reads a password using shell substitution, not a literal
  credential. No other findings were accepted.

Local baseline before the privacy correction was **390 passed, 1 deselected**
on each Python environment. Test counts describe separate suites/runs; do not
sum repeated runs into distinct test coverage.

## CI

`.github/workflows/offline.yml` runs the locked bridge suite on Python 3.11 and
3.14, Linux/PBX helper regressions, shell syntax, a Windows PowerShell parse job
and pinned Gitleaks history scanning. Action references and scanner digest are
pinned. Consult GitHub Actions for the actual result of the commit being used;
this document is not a substitute for that commit's CI status.

## External components and manual acceptance

Observed Hermes: **0.21.3**, upstream commit
`c661785f872b5647fbac7c138d965180783bd9af` (confirmed present upstream).
The official installer selects current upstream, so verify compatibility again
when it differs. No unrelated local Hermes modifications are bundled.

Observed PBX/MariaDB image digests and the upstream installer help/exit caveats
are recorded in [WINDOWS-PBX.md](WINDOWS-PBX.md). SQL initialization sequencing
was reviewed against the actual MariaDB entrypoint; a fresh database was not
initialized during publication.

**Not performed:** new-host OS installation, real provider authentication,
new-stack activation/database initialization, firewall/task registration,
backup restore, reboot recovery, live acoustic acceptance, or PSTN calls.
All require target-specific operator execution and evidence. HTTP health,
registered ARI and passing offline tests do not establish end-to-end audio.

The fresh profile policy intentionally starts with restricted tools and separate
credentials; optional private business MCPs/skills are not silently inherited.
John's generated runtime configuration uses its own API key rather than the
source snapshot's historical shared Mary key. See [SETUP.md](SETUP.md).
