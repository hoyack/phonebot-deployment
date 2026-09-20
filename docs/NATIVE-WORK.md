# Native Hermes work during an Atlas phone call

## Delivered boundary

Opt-in streaming Atlas integration dispatches a short, explicitly scoped read-only
subject to **native `/v1/runs`**, returns control to the foreground, and supports
list/status/result/steer/cancel through an installed skill + stdlib CLI. The bridge
controls lifecycle; Hermes owns execution, native sessions, run status and results.
There is no custom agent executor, Kanban, subprocess agent, or work queue.

## Proactive audio boundary

Opt-in native work now has **bounded generic progress and one-time terminal
notices** in `stream_call.py`, after the foreground dispatcher returns:

- The listening loop checks an in-memory, status-only snapshot once per second;
  it does not issue additional native requests. The existing two-second watcher
  remains the sole background API polling loop. Failed/in-flight observations
  invalidate the cached claim; nonterminal observations expire after six seconds.
- With a fresh `running` status, generic progress is attempted no more than once
  every 15 seconds, at most four times per call. Queued, stopping, acceptance-
  unknown or stale states are never described as running or complete.
- Completed subjects get a generic “results are ready for review” notice. Failed,
  cancelled or interrupted subjects get “some background work did not complete.”
  Simultaneous terminal states are coalesced; each subject is notified at most
  once, sharing the 15-second notice spacing. No keys, worker output or errors
  are read aloud. A notice invites an explicit request, not automatic delivery.
- Notices require three seconds of quiet after caller activity/foreground audio,
  no banked utterance and no reclaimed caller floor. The listening loop cannot
  speak during a foreground reply or its hold/music controller. Hold consent,
  foreground floor policy and PIN handling are unchanged; `hold.py` is untouched
  by this feature. No new music or implicit hold is introduced.
- SpeechStarted or any transcript aborts notice synthesis/playback; already
  in-flight audio frames are drained, never cut mid-write. Each attempt is bounded
  by ten seconds and the call deadline. Interrupted/failed notices are not replayed
  in a loop, so one-time means **at most one attempt**, not guaranteed delivery.
- Pending work (including unknown observations) or an unannounced terminal
  snapshot suspends the normal idle hangup check, not the maximum call deadline.
  A watcher outage must not silently idle-cancel an accepted job. After a notice
  the caller gets a new listening interval. Unavailable status never produces a
  running claim; the maximum call deadline still bounds the call. Existing work
  deadlines and hangup/transfer cleanup remain.

Notices create **no synthetic user turn, assistant-history correction, prompt-cache
edit, result fetch or cancellation**. The primary assistant fetches and reviews
results through the skill only on the caller's explicit request. Other subjects
survive foreground cancellation and acoustic barge-in. The repository skill
explicitly forbids cancelling jobs because of a notice or topic change. No live
profile skill was changed.

Disabled by default. No live profile, deployment, provider, PSTN call or service was
modified/tested by this implementation.

## Verified upstream contract

Reference: [official programmatic integration documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration).
Source inspected at Hermes `c661785f872b5647fbac7c138d965180783bd9af`:

- `gateway/platforms/api_server_runs.py`: POST admission consumes `input`,
  `session_id`, `instructions`; `Idempotency-Key` fingerprints the full request.
  A replay returns the same `run_id`, even at native capacity. Accepted response
  is HTTP 202 with `run_id`, `status`, `replayed`.
- The worker gets a **new, deterministic work session** (`phonework-<digest>`),
  never the foreground call's session or history. It is not a delegate child.
  Native eventstream disconnect does not cancel the run; this client does not
  connect to the eventstream at all.
- GET `/v1/runs/{run_id}` is the status/result truth (`output` is final text).
  POST `/{run_id}/steer` uses `{"message":"..."}`.
  POST `/{run_id}/stop` is cooperative. Follow-up GET, not POST acceptance, is
  used to report actual state. `stopping` is not reported as cancelled/completed.
- `waiting_for_approval` is denied with POST `/{run_id}/approval`,
  `{"choice":"deny","request_id":"<exact pending ID>"}`, then stopped. If
  there is no exact request ID, only stop is sent. No approval command/tool payload
  is exposed by the helper. A stopped approval returns `needs_approval: true`.
- **There is no supported request wall-clock timeout or per-request toolset/
  iteration override in this source's `_handle_create_run`. Do not invent one.**
  `api_server.py::_create_agent` takes `_current_max_iterations()` from native
  runtime config. `gateway/run.py::_current_max_iterations` reads the routed
  profile's `agent.max_turns` directly. A separately restricted worker profile is
  required, rather than broad Atlas + a prompt promising read-only. Standalone
  gateways use their own profile config and root API; `/p/<worker>/` applies only
  to an already configured multiplexed gateway, not the standalone phone gateways.
- Restart marks interrupted native runs; it does not automatically resume them.
  Native idempotency/status retention defaults to 24h, subject to gateway config.

### Stable foreground identity — not `HERMES_SESSION_ID`

The exact public call URL id is established by the existing bridge as
`call-<channel_id>` (or `call-<channel_id>-<extension>` after transfer).
`api_server.py::_prepare_session_chat` reads that URL id and passes it to
`_run_agent`; `_bind_api_server_session` binds **`chat_id=session_id`**.
`gateway/session_context.py` holds that in task-local `HERMES_SESSION_CHAT_ID`.
`tools/environments/local.py::_inject_session_context_env` exports those
ContextVars into each local terminal subprocess, stripping unset stale mirrors.

The installed CLI requires `HERMES_SESSION_PLATFORM=api_server`, reads that
`HERMES_SESSION_CHAT_ID`, and derives this call's socket name. It never uses
`HERMES_SESSION_ID`, which can be a mutable durable/compressed session id. No new
prompt, prior-message rewrite, prompt-cache mutation or per-turn tool change is
introduced. This mechanism requires the local terminal backend and a shared Unix
filesystem/UID between the foreground Hermes and bridge; do not enable it for
remote/Docker terminal backends without a separately reviewed identity transport.

## Security and admission

- Config allows this feature only on PIN-protected **streaming Atlas**, not Mary,
  unprotected Atlas, or turn mode. Existing direct and transfer paths call
  `create_session` only after successful PIN; work is opened there, not in the
  client constructor. `HermesClient.aclose` closes admission and requests stops
  on transfer/hangup. Reply stream cancellation/barge-in never closes it.
- A private 0700 directory holds one 0600 Unix socket per active call. Its server
  is a fixed call association: commands accept **subject keys, never arbitrary
  run IDs/call IDs**. Foreign keys and extraneous identity fields are rejected.
  A worker's `phonework-*` session has no call socket and no terminal tools.
- This is a **trusted same-UID deployment boundary**, not a sandbox against a
  malicious process/agent already able to modify environment variables or read
  that user's private files. Do not give unrelated principals that Unix account.
  Unix session environment is routing context, not cryptographic authentication;
  PIN-authenticated bridge socket creation is the authorization gate. Hostile
  multi-tenant same-UID use requires a genuine capability-bearing transport and
  is outside this initial implementation.
- No credentials in the skill/helper/metadata. Configure the worker's bearer key
  explicitly with `api_key_env`. A distinct-origin standalone gateway requires its
  own key; foreground-token fallback exists only for the same-origin routed case
  and is not a reason to reuse tokens across isolated gateways. Redirects and
  environment proxies are disabled for native requests. No provider secrets are
  copied into a new file or profile by the installer.
- Restricted profile config must explicitly contain `platform_toolsets.api_server:
  [web, no_mcp]`, `agent.max_turns: 1..8`, and no configured MCP servers, context
  engine or plugins. Admission reads that operator-specified config and checks
  native GET `/v1/toolsets`: every enabled concrete tool must be `web_search` or
  `web_extract`. Broad terminal/delegation/skills/MCP/write tools are refused.
  Do not install profile plugins, portable tool overrides, or modify the routed
  profile concurrently with calls. The operator must verify the config path is
  the actual routed profile home; this is not remotely attestable by the API.
- **Two active/uncertain runs globally per shared work directory**, enforced with
  cross-process nonblocking `flock` and private reservation metadata before POST.
  Four distinct subjects per call; input up to 4000 characters, steer up to 2000.
  **Zero local backlog**, intentionally below the requested maximum backlog of
  two: when both slots are occupied, admission rejects rather than silently queues.
  Use one dedicated shared directory/worker route for all bridge instances.
- Native at-most-eight-iteration budget is enforced by the validated routed
  profile, not prompt text. A control-only watcher polls while the bounded call
  lives (two-second interval), denies approvals, and requests cooperative stop
  at the fixed 120-second deadline. Native API requests have a 10-second transport
  timeout. **Neither is a hard wall-clock execution kill guarantee.** A stalled
  native provider/tool, bridge crash, or unreachable gateway can outlive it.
  The bridge does not replace the native executor or claim stronger semantics.
- Stop/result state stays native. Local JSON contains only call id, subject key,
  subject fingerprint, native idempotency key, separate session id, deadline,
  and accepted run id (plus an approval-required control flag). No input, transcript, output, tool payload or secrets.
  Only after terminal native statuses are read is teardown metadata removed.

## Failure / recovery rules

No automatic POST submission retries. If acceptance is lost, its reservation stays
occupied with `acceptance_unknown`; normal control polling does not resubmit it.
During the same active call, an explicit repeat of the exact subject/key before
its original deadline reuses the exact payload and native idempotency key. A
changed subject conflicts, and an expired unknown request cannot start fresh work.

If the bridge dies, or stops are unconfirmed, metadata remains. Native still owns
status/results. New admission counts those reservations; inaccessible/expired
native status fails closed rather than guessing that a slot is free. Restart does
not resurrect the caller's socket/authorization. An operator must inspect native
run/session state and explicitly stop/reconcile orphaned work with the existing
secure native API tooling before removing leftover metadata. In the rare lost-
acceptance/no-run-id case, use the recorded session/idempotency identity and native
store/admin tooling; **do not delete its reservation and re-dispatch under a new
key**. There is deliberately no agent-facing arbitrary-run admin/recovery command.

## Installation (operator, after review; not performed here)

1. Prepare a separate native worker profile, e.g. `atlas-work`, following
   [ATLAS-WORKER-SETUP.md](ATLAS-WORKER-SETUP.md). For standalone Atlas, use a
   separate literal-loopback gateway with its own bearer key. Its foreign
   `/p/atlas-work` route returns 404; creating a profile does not expose that route.
   Do not enable broad multiplexing or reuse the foreground bearer to bypass this.
   Provision model credentials privately; never copy secrets into this repository.
2. With the **explicit worker home** selected, use Hermes config tooling, e.g.:

   ```bash
   HERMES_HOME="$WORKER_HOME" hermes config set platform_toolsets.api_server '["web","no_mcp"]'
   HERMES_HOME="$WORKER_HOME" hermes config set agent.max_turns 8
   ```

   `$WORKER_HOME` must be the real `atlas-work` profile directory, not foreground
   Atlas. Verify no MCP/context-engine/plugins are enabled and GET `/v1/toolsets`
   agrees. Verify native gateway capacity leaves room for foreground turns; do
   not globally cap a shared gateway to two when two workers plus Atlas need
   three slots. The bridge enforces its own two-work-run cap.
3. Add this opt-in block only to the streaming Atlas extension in the bridge's
   deployment YAML. Replace these illustrative paths/port with real reviewed
   values; the loader does not expand shell variables in YAML:

   ```yaml
   native_work:
     base_url: http://127.0.0.1:8645
     api_key_env: HERMES_ATLAS_WORK_API_KEY
     directory: /run/user/1000/atlas-work
     worker_config: /absolute/hermes/profiles/atlas-work/config.yaml
   ```

   A dedicated gateway must use a literal loopback address, a distinct origin,
   and an explicit worker token variable in the private bridge environment.
   Alternatively, an already configured same-origin multiplexed worker route is
   supported, but must use that worker profile's own credential reference.
   The directory must be owned by the bridge/foreground user and mode 0700, with
   a short absolute path (Unix socket path limit). For crash-persistent metadata
   use a private persistent directory rather than a volatile `/run` directory.
   Do not reuse it for unrelated data.
4. Install only into the explicit **foreground Atlas** profile home, using repo-
   relative installer invocation:

   ```bash
   python3 scripts/install-atlas-native-work.py \
     --profile-home "$ATLAS_HOME" --socket-dir "$WORK_DIRECTORY"
   ```

   The installer requires an existing `atlas/config.yaml`, refuses an existing
   skill destination and symlinked skills directory, and never edits profile
   config or credentials. It copies the skill and writes only a nonsecret
   `scripts/settings.json` alongside its portable stdlib helper. For upgrades,
   review/archive the old skill explicitly, then reinstall; don't overwrite a
   live profile behind an active session's prompt cache.
5. Start fresh calls after profile/gateway/bridge reload according to deployment
   procedure. Parent/operator must perform bounded real native API + actual-model
   validation: skill discoverability, local terminal context export, profile
   route/key authorization, worker tool policy, iteration limit, foreground
   continues after dispatch, barge-in survival, explicit result, cooperative stop,
   hangup and unknown-acceptance recovery. Offline tests are not that attestation.

## Offline verification

`phonesys/tests/test_native_work*.py` uses real loopback HTTP, private Unix sockets,
actual standalone helper subprocesses, and a temporary explicit-profile install.
No Hermes model/provider/PBX/service is contacted. It covers wire fields, global
slots, cross-call key isolation, policy drift, malformed input, uncertain acceptance,
exact idempotent replay, deadline checks, approval denial, honest stopping states,
helper disconnect, lifecycle closure, config gating and installer portability.

Use an isolated test environment with `pytest`, `pytest-asyncio` and the phonesys
runtime dependencies, then from `phonesys/`:

```bash
PYTHONPATH=src python -m pytest tests/test_native_work.py tests/test_native_work_config.py tests/test_native_work_install.py -q
PYTHONPATH=src python -m pytest tests -q
```

Initial genuine RED/GREEN outputs were: transport `1 failed` → `1 passed`;
associations `1 failed, 1 passed` → `2 passed`; policy/approval/deadline
`2 failed, 2 passed` → `4 passed`; CLI socket `1 failed, 4 passed` → `5 passed`;
client lifecycle `1 failed, 5 passed` → `6 passed`; config `7 failed` → focused
combined `13 passed`; global slots `1 failed, 6 passed` → combined `14 passed`;
malformed requests `2 failed, 11 passed` → combined `20 passed`; installer
`1 failed` → `1 passed`; expired unknown replay `1 failed, 14 passed` → `15 passed`.
Additional regression cases extend those original vertical slices. The first test
attempt in the default Hermes interpreter was an environment error (missing
pytest-asyncio), **not** counted as a RED assertion; subsequent runs used the
parent's already isolated `/tmp/phonesys-hermes-native-proof/.venv` interpreter.
Final hardening also reproduced then fixed approval-flag loss, raw approval data
in cancel responses, and duplicate call socket replacement (`1 failed` →
`1 passed` for each focused regression).

Final executed outputs in that isolated environment:

```text
26 passed in 2.47s
445 passed, 1 deselected in 30.04s
```

`git diff --check` exited 0 with no output. These initial results preceded the
proactive audio addition. No live model/provider/native gateway validation was
performed by this implementation.

### Combined proactive audio verification

`test_background_feedback.py` drives the real `StreamingCallHandler` with duplex
STT/audio fakes and a real `WorkCall` status cache (native transport mocked). It
covers bounded periodic progress, terminal coalescing/at-most-once attempts,
explicit result requests, caller activity during synthesis/playback, hold consent
and active music, foreground topic replacement with separate jobs surviving,
unknown/stale states, no extra API polling, native-disabled behavior, idle timeout,
TTS failure and hangup task cleanup. No output payload becomes notice text.

Genuine RED → GREEN slices: missing snapshot method (`1 failed` → `1 passed`),
missing background audio (`1 failed, 1 passed` → `2 passed`), terminal notices
(`4 failed, 2 passed` → `6 passed`), interruptible notice audio/synthesis
(`2 failed, 6 passed` → `8 passed`), and idle timeout before first feedback
(`1 failed, 8 passed` → `9 passed`). Additional cases exercise existing safety
invariants. A full-suite installer assertion required retaining the word
“proactive” in the updated skill boundary; corrected before the final run.

Executed with the same isolated interpreter:

```text
new background tests: 25 passed in 5.03s
new + Atlas floor + hold + work feedback: 111 passed in 15.40s
full suite: 494 passed, 1 deselected in 35.26s
git diff --check: exit 0
```

These totals include other owners' worktree changes. This feature changed
`stream_call.py`, the small status snapshot interfaces in `native_work.py` and
`hermes.py`, the new test file, this document and the repository skill text only.
It did not change `hold.py`, commit/push, or touch live profiles/services. Live
native gateway/model/PBX acceptance remains the operator's separate responsibility.
