# Atlas conversation lifetime and presence checks

This change is an **offline-verified deployment candidate**, not evidence of a
live telephone acceptance test. No PSTN, inference, STT, or TTS requests are
required by the tests below. Do not restart a live bridge during a call.

## Settings and compatibility

`phonesys/config.yaml`, extension `7003` only:

```yaml
max_call_seconds: 600          # retained for rollback, NOT set to zero
call_limit_enabled: false     # explicit supported opt-out of total call limit
idle_hangup_seconds: 30        # silence while listening before presence check
idle_presence_grace_seconds: 20 # response time AFTER the warning finishes
hermes_reply_timeout_seconds: 120 # unchanged foreground generation budget
hold_enabled: true            # existing duplex controller and consent policy
```

A disabled call limit is supported only for streaming Atlas with its existing
hold-enabled duplex controller and finite positive idle/presence timers.
`call_limit_enabled` must be an actual YAML boolean, not a quoted string. Its
loader default is `true`. `idle_presence_grace_seconds` defaults to `0`, retaining
the previous immediate idle hangup for existing configurations. Negative or
non-finite grace values fail validation. Omitting the new settings therefore
preserves existing configurations. Extensions 7000/7001/7002 keep their current
600-second limits and their previous silence behavior.

The fresh-setup generator copies the canonical bridge YAML, so newly generated
bundles inherit the Atlas-only setting. Existing installations require an
explicit narrow configuration update; setup/stage is not an in-place updater.
Old bridge code ignores unknown settings: **deploy the reviewed code and config
together**, not just the YAML. Re-enable `call_limit_enabled: true` to restore the
retained 600-second cap. Presence checking can independently remain enabled.

## Behavior

- An active Atlas conversation has no total wall-clock cutoff. This also applies
  after an authenticated transfer from Mary. Limited transfers retain their
  original call deadline, rather than resetting or replacing it.
- After 30 seconds of ordinary listening silence, Atlas says:
  **“Are you still there? If I don't hear from you, I'll end the call.”**
  It then allows 20 seconds to respond. No response ends the call through the
  existing teardown path. Caller VAD/confirmed speech or an answered utterance
  resets the presence cycle, not just a single deadline.
- The clock begins after greeting/reply playback. Foreground generation,
  answer playback, local hold controls and consented hold are not idle time.
  Presence audio runs only in the outer listening loop, using the existing
  single-writer, interruptible local-notice path. It neither creates an agent
  turn nor supplies hold consent. Its synthesis is capped at ten seconds.
- The grace starts only after all warning frames have been credited as sent.
  Interrupted, empty, failed or incomplete warning playback does not authorize
  silent hangup: return to listening and retry after a fresh idle interval.
  Persistent TTS failure can therefore leave a silent caller connected; EOF,
  STT failure and explicit cancellation still tear down the call. Sent frames
  are bridge evidence, not proof of carrier audibility.
- Native work excuses listening silence only inside its **original 120-second
  work budget**. An expired nonterminal/unknown observation is not completion or
  successful cancellation. Atlas attempts one best-effort notice that the work
  limit passed and that it cannot confirm whether work stopped; normal presence
  checking resumes. As with existing result-ready notices, an interrupted or
  failed native notice is not retried indefinitely and is not proof of delivery.
  It never substitutes for the separately delivery-gated presence warning before
  an idle hangup. This prevents permanent notice failure from granting an
  infinite idle exemption while preserving the job's reconciliation authority.
  Late terminal confirmation can still produce a separate result-ready notice.
  Native IDs, idempotency keys, reservation files and cooperative stop/close
  reconciliation are retained. The native eight-turn maximum is unchanged.
- Foreground generation remains bounded by the configured 120 seconds. With the
  total call cap disabled, each hold-controller synthesis operation also has
  a finite bound of `hermes_reply_timeout_seconds`; completed sentence playback
  gets its actual frame duration plus ten seconds of transport slack, rather
  than the expired generation deadline. This changes no audio pacing. Stalled
  speech reports the existing speaking-error apology and returns to listening.
  Cooperative cancellation/drain assumptions remain; this is not a process-level
  hard-kill guarantee for non-cooperative third-party coroutines.

Internally, the disabled call deadline uses an infinity sentinel only for
comparison, finite-time subtraction and minimum selection. It is never handed
to asyncio as an infinite timeout: unrestricted event waits use `None`, and
work, notice and media-operation waits use finite bounds. The regression fixture
rejects non-finite asyncio timer handles. No `infinity - infinity` occurs.

PIN handling, voice, speech pace, barge confirmation, hold consent/music,
worker policy, native job execution and profile routing are not changed.

## Reproducible offline checks

From a clean deployment-repository checkout, without live `.env` files:

```sh
cd phonesys
umask 022
uv sync --frozen --extra dev --python 3.11
PYTHON_DOTENV_DISABLED=1 uv run --frozen --extra dev python -m pytest -q tests/test_call_lifecycle.py
PYTHON_DOTENV_DISABLED=1 uv run --frozen --extra dev python -m pytest -q -m 'not live'
cd ..
PYTHON_DOTENV_DISABLED=1 phonesys/.venv/bin/python -m pytest -q tests_deployment
python3 -m unittest discover -s freepbx -p 'test_*.py'
for file in scripts/*.sh freepbx/*.sh; do bash -n "$file"; done
git diff --check
```

Repeat the locked environment/full suites with Python 3.14. For both interpreters,
set `HERMES_TEST_SOURCE=/absolute/path/to/reviewed/hermes-agent` on the
`tests_deployment` command to exercise the optional actual Hermes writer/resolver
integration (otherwise those two tests explicitly skip). It uses isolated
synthetic profiles, not the live profile. CI additionally
checks PowerShell parsing and pinned Gitleaks history scanning. Lifecycle tests
use scaled durations and injected local audio/STT/Hermes adapters, plus an
isolated loopback native-control fake; they do not dial or contact providers.
They exercise active calls beyond the old cap, warning/grace/response reset,
transfer compatibility, consenting hold versus work timeout, completed speech,
blocked synthesis, EOF/teardown, no overlapping writers, finite timers, and
unknown native-job metadata retention after failed stop confirmation.

## Narrow deployment plan (requires independent review first)

1. Review the PR, require the complete offline suites and independent review,
   and merge only the reviewed commit. Do not reset the dirty deployment snapshot.
2. Read current runtime/snapshot hashes and parsed settings; ensure the baseline
   for each touched file matches the reviewed parent or reconcile drift first.
   Capture protected rollback copies outside Git of exact files and YAML.
3. Copy only these reviewed source files to both active `phonesys/src/phonesys/`
   and the deployment snapshot's `phonesys/src/phonesys/`:
   `config.py`, `hermes.py`, `hold.py`, `native_work.py`, `stream_call.py`.
   Copy the reviewed `test_call_lifecycle.py` and `test_background_feedback.py`
   into each corresponding tests directory, preserving unrelated test drift.
4. Patch only extension 7003's bridge YAML in each target: add
   `call_limit_enabled: false`, set `idle_hangup_seconds: 30`, and add
   `idle_presence_grace_seconds: 20`. Preserve the existing native-work block,
   worker endpoint/key references, routing and all other YAML values. Do not
   copy the shipped YAML over the live file. Parse/compare against the backup
   after removing exactly those three approved key differences. Verify the
   existing 120-second reply budget and worker eight-turn policy unchanged.
5. Run each deployment's offline tests against **its own source**, with dummy
   credential variables and dotenv disabled. Packaging-only tests need the
   reviewed repository fixture paths, as documented in deployment operations;
   do not copy unrelated repository/profile trees to make them pass.
6. Check Asterisk has zero active channels. Restart **only**
   `phonesys-bridge.service`. No PBX, foreground Hermes, native-worker or Windows
   proxy restart; no profile/PIN/hold-music edits.
7. Verify reviewed source hashes, effective extension settings, new bridge PID,
   ARI registration and bridge-owned 9010 listener; read-only foreground/worker
   health is not inference or human-call acceptance. Preserve native uncertain
   work metadata. Arrange separately authorized human-call acceptance rather
   than placing an unapproved provider test call.
8. Rollback: when channels are clear, restore the exact backed-up source/YAML
   and restart only the bridge. Keep uncertain native job files for reconciliation.
