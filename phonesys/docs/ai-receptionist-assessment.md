# ai-receptionist as a source for phonesys — final assessment

**Date:** 2026-08-06
**Source:** `git@github.com:hoyackagency/ai-receptionist.git` @ `085b87f`
(was checked out at `./ai-receptionist/` for this review; that working copy has since
been deleted as it is not part of this project. Re-clone at that SHA to re-verify any
`call_handler/...` citation below.)
**Reviewed against:** phonesys `src/phonesys/` @ `1433fc9`

---

## 1. Verdict on usefulness

Keep it, but keep it as a **latency-and-testing reference, not an architecture
reference**. Roughly 80% of the codebase is inapplicable: the transport is Twilio
Media Streams over WebSocket, the runtime is pipecat + pipecat-ai-flows, the TTS is
Cartesia, and the domain is a JSON decision tree over OpenEMR/HubSpot. None of that
survives contact with hard constraints 2 and 3, and the parts that look most
transferable (interruption policy, VAD turn-taking, incremental TTS) turn out to be
*framework defaults* rather than decisions ai-receptionist made and validated — in two
cases the cited code is dead (`call_handler/src/audio.py`'s `UserAudioCollector` is
defined at :14 and never imported anywhere in the tree). What is genuinely worth
having is narrower and mostly non-architectural: they instrumented latency and phonesys
does not (zero elapsed-time measurement exists in `src/phonesys/` — every `loop.time()`
call is deadline arithmetic); they built a synthetic caller that speaks the real
transport at real-time pacing and asserted a time-to-first-audio budget, and phonesys's
only equivalent is `dryrun.py`, 46 lines against the 7000 path with no transport; they
tiered their pytest suite so live-service tests skip instead of failing; and they hit,
and documented, the failure modes of an E2E suite that asserts on transcript wording
(15 of 19 tests failing, 12 of them from Whisper returning `(silence)`). The one
structural idea worth taking — client-owned conversation history with the assistant
turn committed only after audio egress — is something phonesys already decided to do
independently (`HANDOFF.md:129-155`, the `/v1/runs` migration); ai-receptionist
corroborates it and contributes two useful disciplines around it, but it does not
supply it. Net: a good corroborating second opinion and a decent testing cookbook.
Not a design donor.

**Merge note.** Five of the reviewed patterns ("per-turn latency instrumentation",
"time-to-first-utterance on the wire", "two-clock latency measurement", "per-call
metric state object", and half of "config-sweep harness") are the same item seen from
different files. They are collapsed into item **#2** below. Three others
("synthetic caller that speaks the transport", "…that speaks and listens", "on-the-wire
synthetic caller") are collapsed into **#7**.

---

## 2. What to adopt, ranked by value ÷ effort

### #1 — Marker-gated live test tier (ADOPT, small, ~1h)

**What:** `pytest.ini` in the source has `--strict-markers` plus a registered
`system:` marker; `tests/system/conftest.py:65-94` is a session fixture that polls
`/health` for 10s and calls `pytest.skip` with a message naming the env var to set,
with a per-test re-check at :126-141. Missing infrastructure skips; it never fails.

**Where:** `pyproject.toml:22-24` currently contains exactly `asyncio_mode` and
`testpaths` — no markers, no addopts. Add
`markers = ["live: requires Asterisk/Deepgram/ElevenLabs/Hermes"]` and
`addopts = ["--strict-markers"]`; create `tests/live/` with a session fixture that
pings Hermes on the per-extension `hermes_base_url` (`config.py:19`) and skips naming
the service that is down.

**Effort:** small. **Risk:** near-zero — `grep` confirms no existing `pytest.mark`
usage that `--strict-markers` could break.
**Solves:** none directly. **Enables:** #2's assertion half, #7, #8.

**Why first:** it is the only item with a real dependency edge into constraint 5.
Every other live-service item puts network dependencies into the same pytest run as
the 7000 tests. Without this gate, a Deepgram hiccup turns extension 7000's suite red
and nobody can distinguish an outage from a regression. Ship it alone, before
anything else here.

*Second-order risk worth naming: a tier that skips silently rots. Print the skip
reason loudly, or you end up with a broken E2E suite that always looks green — a
different flavour of the exact failure the source suffered.*

### #2 — Per-turn stage latency instrumentation (ADOPT, small, ~1h)

**What:** stamp the four boundaries of a reply and log the deltas once per turn.
Source precedent: `src/call_metrics.py:30-48` (per-call state, `time_to_first_voice`),
`testtool/server.py:162` + `:388` (matched-clock TTFU), and the offline percentile
report at `llm_latency_report_20260402.md:371-381` (P50 697ms / P95 6069ms).

**Where:** `stream_call.py` only. `loop` is already bound at `:460`. Four
`loop.time()` reads into locals inside `_reply`, plus one `logger.info` at turn end:

| mark | site |
|---|---|
| t0 — utterance accepted | just before `await self._reply(...)` at `:360-362` |
| t1 — first Hermes delta | first return from `_next_delta` (`:646-656`) |
| t2 — first TTS frames back | `synthesize_frames` returns, `:542` |
| t3 — first frame on the wire | first `tracker.record_sent` credit, `on_sent` at `:592` |

Log t1−t0 (Hermes generation), t2−t1 (ElevenLabs TTFB), t3−t2 (queueing), and which
trigger fired (`utterance_ready` vs the `stalled` fallback).

**Effort:** small, ~15 lines. **Risk:** effectively zero for 7000 (module not
imported by `call.py`). Two ways to get it wrong: (a) do **not** log inside
`audiosocket.py:73-94`'s paced loop — that runs every 20ms and its timing accuracy is
what constraint 6 rests on; latch a timestamp in an `on_sent` closure at the
`stream_call.py` call site instead. (b) Do not insert an `await` between `:591-619`
or between `synthesize_frames` returning and `stop_filler()` at `:558` — that
ordering is load-bearing per the comment at `:546-557`.
**Solves:** A (attribution), and possibly reclassifies part of A as C.

**Why:** problem A is currently a stopwatch observation with four candidate owners
and no way to tell them apart, and `filler_delay_ms`/`filler_repeat_ms` are being
tuned blind against it. Note especially: `stalled` turns (`stream_call.py:348-354`,
`UTTERANCE_END_FALLBACK = 3.0` at `:59`) fire with `utterance_ready` never set. If
the fallback is firing often, a chunk of the "4-10s" is C wearing A's clothes, and
half the filler work is aimed at the wrong problem. Stamping only at `:322` would
silently exclude exactly those cases.

Log **two** first-audio deltas, not one: filler frames route to `_discard_frame`
(`:490`, `:690-696`) and reply frames to `tracker.record_sent` (`:592`), so the split
is nearly free — and without it filler hides every regression.

Do **not** copy the source's implementation details: it uses `time.time()` wall clock
(`call_metrics.py:36`), its `time_to_first_voice` actually fires from the
`LLMFullResponseEndFrame` branch (`pipelines.py:184-185`) so it measures LLM
*completion*, not first audio, and its module-level `_call_state` dict with a
once-only guard and a `finally: pop` exists only because pipecat's callbacks are
decoupled. In phonesys the whole turn is one function with closure locals; use
`if t1 is None`, no dict, no pop.

Defer the threshold *assertion*. The source's own data kills it:
`SYSTEM_TEST_REPORT_20260430_002.md:44-50` shows 9.554s PASS and 10.298s FAIL against
a 10.0s threshold on identical code. Record the number every run; assert only on a
deliberately generous ceiling, and only inside #1's tier.

### #3 — Cache the greeting synthesis at startup (ADAPT, small, ~30min)

**What:** the source's general principle is "keep known-cost setup off the
caller-facing critical path" (`utils.py:273-291`, the `gather` rewrite whose comment
reads *"adding 5-7 seconds of blocking init time while the caller was already on the
line"*). The phonesys application is inferred, not mined.

**Where:** `stream_call.py:330` calls `self._speak(audio, ext, [ext.greeting])`, and
`_speak` (`:449-452`) calls `synthesize_frames` on **every call**, for a string that
is a frozen-dataclass constant (`config.py:21`, populated at `:81`). `filler.py:1-9`
puts the ElevenLabs round trip at 400-600ms and exists specifically to avoid paying it
at the moment of need. The machinery already exists: `main.py:133-140` pre-synthesizes
fixed strings at startup via `FillerBank.prepare` (`filler.py:38-61`). Add the
greeting to that.

**Effort:** small. **Risk:** low. Cached frames become shared across concurrent calls
— safe as written, because `send_frames` (`audiosocket.py:73-95`) only iterates and
the payloads are immutable `bytes`, and `FillerBank` already shares frames the same
way, but state it rather than assume it. A startup synthesis failure must degrade to
per-call synthesis, not kill the extension — copy the try/except isolation at
`main.py:134-144`.
**Solves:** A, partially — ~500ms off the front of every 7001 call.

**Do not** bundle this with parallelizing `handle`'s setup chain
(`stream_call.py:243-256`). `hermes.create_session` is already at `:244` and Hermes is
on localhost, so hoisting it buys milliseconds; the only real target is `stt.connect()`
at `:256`, and moving that above the AudioSocket wait is **actively dangerous** — it
can then sit idle for up to `AUDIOSOCKET_WAIT_TIMEOUT = 10.0s` (`:43`), Deepgram closes
sockets that receive no audio, `stt.events()` ends immediately, `stt_ended` fires
(`:324`), and the call takes the apologize-and-hang-up path at `:367-374`. That trades
~200ms for intermittent dead calls. Leave `stt.connect()` where it is.

### #4 — Log `spoken` alongside `heard` in `credit_heard` (ADAPT, trivial, ~10min)

**What:** the source's `LLMLogger` writes a durable transcript from an observer,
decoupled from the model's context object. 95% of that is irrelevant here (no DB, no
celery, no frame bus). The transferable residue is one line.

**Where:** `stream_call.py:638-639` computes `heard = tracker.heard_text(spoken)` and
logs only `heard`. `spoken` (`:461`, appended at `:544`) is never logged, so nothing
in the log shows where the truncation boundary fell.

**Effort:** trivial. **Risk:** `spoken` is full reply text, so this widens what
conversation content lands in logs. If logs ship anywhere, that is a content-exposure
change, not just verbosity.
**Solves:** D, marginally.

Correction worth recording: the *full generated reply* is not obtainable. On
`_Interrupted` the SSE generator is abandoned inside `contextlib.aclosing`
(`:655-657`), so `spoken` holds only sentences that reached `synthesize_frames`. The
diff you get is **generated-and-queued vs heard**, not generated vs heard. Do not
build an observer abstraction to get more; continuing to drain the stream after
barge-in is real cost for log fidelity nobody needs.

### #5 — First-delta watchdog on `chat_stream` (ADAPT, medium, ~half day)

**What:** `ResilientOpenRouterLLMService` (`utils.py:54-199`):
`_FIRST_RESPONSE_TIMEOUT = 10.0` at `:78`, `_has_pushed_text` set in the `push_frame`
override at `:104-110`, a 100ms poll loop with cancel-on-timeout at `:129-152`, retry
at `:153-160`, a spoken last resort at `:164-167`, and a partial-aware recovery
("…sorry about that, one moment please") at `:174-187`.

**Where:** `stream_call.py`'s `_reply`/`_next_delta`. Today `_next_delta`
(`:699-754`) calls `asyncio.wait({nxt, stop})` at `:713-715` with **no timeout**, and
the deadline check at `:659` only runs *after* a delta arrives. `hermes.py:21-28` sets
one blanket `timeout=60.0`.

**Take the watchdog. Do not take the retry.** ai-receptionist retries a *stateless*
request with caller-supplied context. Under phonesys's current `/chat/stream`, Hermes
commits the assistant turn server-side as it generates (problem B) and has no
truncate/edit — a blind re-POST risks a duplicated user turn plus an orphaned partial
assistant turn, i.e. it converts a latency fix into exactly the history corruption
`credit_heard` exists to contain. The `_has_pushed_text == False` guard makes it
*probably* safe, but that is an assumption about Hermes internals. Either verify
Hermes's commit semantics on an abandoned `/chat/stream`, or wait for #6.

**Effort:** medium. **Risk:** the timeout must be layered so `_next_delta`'s existing
drain still runs — its docstring at `:740-752` documents that cancelling `nxt`
requires an unbounded `asyncio.wait({nxt})` before `aclose()`, or CPython raises
*"aclose(): asynchronous generator is already running"*. A naive `asyncio.wait_for`
around `_next_delta` hits exactly that. Also: put it in `stream_call.py`, **not** in
`HermesClient` — `hermes.py` is shared with 7000 via `pipeline.py:32`, so do not touch
the constructor timeout at `:21/:27`. And the spoken last resort must route through
`_speak`/`speak_sentence`, not be injected as a delta, or it bypasses
`tracker.expect`/`record_sent` and corrupts the accounting.
**Solves:** A's tail, not its median. Filler (`:482-495`) already covers the audible
symptom; the current worst case is bounded at ~60s by httpx's read timeout, not
unbounded. This truncates a tail.

Skip the repetition-loop detector. No looping Hermes agent has been observed, and
`speak_sentence` already checks the deadline per sentence (`:540`), so a loop is
capped by `max_call_seconds`.

### #6 — Client-owned history, assistant turn committed after egress (ADAPT, large)

**What:** `bot_simple.py` builds `OpenAILLMContext(messages=..., tools=...)`
in-process and the pipeline list ends:

```
tts, transport.output(), context_aggregator.assistant(),  # must go after transport.output()
```

That comment is verbatim and is the whole idea: **the assistant turn is committed by
the code that knows what hit the wire, not by the code that generated it.**
`functions.py` reads `context.messages` back as a plain list of dicts.

**Where:** `hermes.py` gains a *strictly additive* `/v1/runs` surface; `stream_call.py`
gains a bridge-owned message list; `credit_heard` (`:624-643`) changes from
`spawn(_quietly("append_assistant", ...))` to an in-process truncating append.

**Honest framing:** this is already phonesys's own top-priority decision
(`HANDOFF.md:133` option (b), `:146-148` confirming `POST /v1/runs`,
`GET /v1/runs/{id}/events`, `POST /v1/runs/{id}/stop` and caller-provided
`conversation_history`, `:153` *"this is what I would do first"*). ai-receptionist
confirms the shape works in production with no server-side session. It contributes
three disciplines and nothing else:

1. **Commit after egress, not after generation.** phonesys already implements the
   principle — `HeardTracker` (`:109-142`) credits a sentence only when every expected
   frame was written, breaks at the first gap (`:140`), and treats zero expected frames
   as not-heard (`:136`); frames are credited from `send_frames`' `on_sent`
   (`audiosocket.py:90`) only after `write()`+`drain()` succeeded (`:77-78`). The
   missing piece is a *writable* history, which comes from Hermes' API, not from here.
2. **Replace-once / append-thereafter.** `engine.py:134`
   (`self._initial_node_generated = False  # Track first node to avoid role_message
   accumulation`) and the warning at `:574-577`: *"re-adding role_messages on every
   transition causes them to accumulate in the LLM context — eventually confusing the
   model into repeating instructions."* This is a fixed bug, not theory. Write it into
   the `/v1/runs` design as an invariant plus one unit test: **steering/system content
   is constructed once at run construction and never appended mid-conversation;
   per-turn corrections EDIT the last assistant message, never add a new one.** The
   inverse must not be over-applied — the run body still ships the whole history every
   turn; the rule is about not *growing* the array.
3. **Assistant pre-fill.** `engine.py` auto-exec queues a `TTSSpeakFrame` *before* the
   API call, then on the post-node deliberately skips re-emitting audio while still
   doing `post_config['task_messages'].insert(0, {'role': 'assistant', 'content':
   _strip_say_prefix(action_prompt)})`. i.e. text spoken outside the LLM is written
   into history as an assistant turn. phonesys does the exact opposite on purpose:
   `FILLER_SENTENCE_INDEX = -1` (`filler.py:20`) and `_discard_frame`
   (`stream_call.py:690-696`), whose docstring says crediting filler *"would make
   append_assistant tell Hermes it said words it never generated."* Under today's API
   that reasoning is correct and the alternative is literally unimplementable
   (`append_assistant` POSTs a full synchronous agent turn, `hermes.py:78-86`). Once
   the bridge owns the array it becomes a few lines, and the clear win is **the
   greeting**: `_speak` (`:438-453`) never reads `heard_text`, so Hermes has no idea
   the greeting was said and the agent can structurally re-greet. Seed the array with
   the greeting as `message[0]`.

**Effort:** large. **Risk:** listed in full below — this is the highest-risk adoptable
item.
**Solves:** B, completely.

**Two blockers to clear before committing:**

- **`hermes.py` is shared with 7000** (`call.py:106`, `pipeline.py:32`). Additions
  must be strictly additive: new methods or a separate `RunsClient` selected by
  `ExtensionConfig.mode`. Do not repoint `chat()`, do not widen the shared
  `httpx.AsyncClient` at `:24-28` — add a second client if `/v1/runs` needs different
  auth or timeout.
- **UNVERIFIED and load-bearing:** does `GET /v1/runs/{id}/events` emit *token-level*
  assistant deltas? `chat_stream` (`hermes.py:49-76`) filters SSE by event name
  `assistant.delta` specifically because `tool.progress` also carries a `delta` field
  holding private reasoning. If the runs event stream is coarser (whole-message or
  status-only), the migration destroys sentence-level streaming TTS and makes problem A
  materially worse. **Prove the event stream yields incremental text against a live
  Hermes before writing any code.**

Also: `credit_heard` runs on four paths (`_Interrupted`, `_TimeLimit`,
`TtsStreamError`, and the success fall-through) — all four must append to the local
list or history silently diverges. `/v1/runs/{id}/stop` is a new network call on the
interruption hot path and must be fire-and-forget or bounded, or it reintroduces
exactly the stall `credit_heard`'s docstring (`:630-636`) warns about. And the
SSE-consumption drain logic in `_next_delta` — the most fragile code in the file — has
to be reproduced against a different event shape.

### #7 — In-process synthetic caller over the real AudioSocket (ADAPT, large)

**What:** `testtool/server.py` is a fake Twilio: `:150-164` handshake with
`connected_at = time.monotonic()`, `:255-296` chunks TTS audio to 160-byte frames
paced at 20ms under the comment *"Deepgram and VAD expect audio arriving at
~real-time speed"*, `:299-411` a dual-deadline receive-until-silence, `:210-243` a
drain-between-turns, `:461-511` reconnect.

**Where:** new `tests/live/`. **The obvious design does not work:**
`AudioSocketServer._handle` keys sessions by the UUID in the first frame
(`audiosocket.py:158-164`), but `StreamingCallHandler.handle` *generates* `call_uuid`
internally (`stream_call.py:224`) and tells Asterisk to dial it back (`:248-250`). A
cold client connecting with a self-chosen UUID creates a session nobody is waiting on.

The tractable design: **drive `StreamingCallHandler.handle` in-process against the
`FakeAri` that already exists** (`tests/test_stream_call.py:331`, whose
`originate_audiosocket` at `:358-361` already records `(host, port, uuid, app)` — so
the harness can read the real UUID out of it, with **zero `src/` changes**), running a
**real** `AudioSocketServer` on an ephemeral port with a real TCP client and real
Deepgram/ElevenLabs/Hermes. That exercises the true framing, the paced writer,
`SpeechBurst`, `HeardTracker`, filler and barge-in.

Existing prior art to build on, not duplicate: `tests/test_audiosocket.py:128-169` is
already a wire-level fake Asterisk (binds on `127.0.0.1:0`, writes
`encode(TYPE_UUID, call_uuid.bytes)` + `TYPE_AUDIO` frames, reads the echoed outbound
frame back with `readexactly(323)`). Every protocol primitive is already demonstrated.
`dryrun.py` is already a live-services single-turn harness, but only for the 7000 path.
The gap is the conversation layer, not the wire layer.

**Five corrections to the source's design:**

1. **Wire format is slin, not mu-law** — `audiosocket_codec.py` pins `0x10` audio to
   320 bytes of 8kHz mono slin. Import `encode`/`FrameDecoder` from
   `phonesys.audiosocket_codec` rather than reimplementing; codec drift then gets
   caught for free.
2. **Do not copy `asyncio.sleep(0.02)`** (`server.py:295`). That drifts — sleep plus
   send time means it emits *slower* than real time, accumulating error over a long
   utterance, which changes Deepgram's endpointing behaviour and silently corrupts the
   thing being measured. Copy the absolute-deadline accumulator phonesys already has at
   `audiosocket.py:72,91-94`. *Copy the principle from ai-receptionist, copy the code
   from phonesys.*
3. **Never go silent between turns.** `HANDOFF.md:99-101`: *"Deepgram needs continuous
   audio (including silence) to finalise an utterance."* A harness that stops sending
   manufactures the exact turn-end bug (C) it is supposed to measure.
4. **No Whisper.** `testtool/audio.py` pulls `whisper` (:14), `gTTS` (:16) and
   `audioop` (:9,43,75,120 — removed in Python 3.13+). Importing torch into a project
   whose entire dependency list is httpx/websockets/PyYAML/python-dotenv is not
   proportionate. Use the existing `stt.py` Deepgram batch client (which `dryrun.py`
   already proves works) for a smoke check, or dump the outbound track to WAV and
   listen.
5. **Drain stale audio between turns, exempting the greeting.** `server.py:210-243` is
   `drain_pending(settle_seconds=0.5)`; `:558-565` skips it on the first request under
   the comment *"that's the greeting capture, and the greeting audio is legitimately in
   the buffer."* Maps cleanly: `stream_call.py:330` speaks the greeting before the main
   loop at `:333`, and `_speak` is explicitly non-interruptible, so the exemption is
   unconditional — simpler than their request-count gate. Better still, because
   phonesys paces its own output you can detect end-of-reply *structurally* (N
   consecutive frame intervals with no inbound frame) instead of guessing a 0.5s
   settle constant. Without this, the tail of reply N is captured as part of turn N+1
   and the harness produces confident wrong conclusions.

**Effort:** large — this is a build, not a port (their equivalent is ~1400 lines and
needed a retry-on-silence layer at `helpers.py:57-98` to stay usable).
**Risk:** zero to production code if it lives entirely in `tests/live/` and imports
nothing from `call.py`/`pipeline.py`/`recordings.py`. Operational risks: it burns real
Deepgram/ElevenLabs/Hermes quota per run, so #1 must land first; and **it must take an
explicit port defaulting to ephemeral, never 9010** — `main.py:110-118` swallows the
bind `OSError`, logs *"streaming extensions will be rejected until this process
restarts"*, and returns `None`, so a harness squatting on 9010 silently disables 7001
on the daemon's next restart with only one log line.
**Solves:** D, partially. **Honest ceiling:** this fakes ARI and fakes Asterisk, so it
cannot catch the highest-value failure mode in the system — Asterisk buffering unpaced
audio (constraint 6). It proves the phonesys *process* behaves; it does not prove the
*call* sounds right. Their own docs concede this
(`LLM_MODEL_TEST_PROCEDURE.md:150`: *"Always do a manual test call"*).

**Assertion policy for this tier — write it into the README before the first test
merges.** `TEST_FAILURES.md:29-30` records *15 failed, 4 passed*, with :52-89
attributing 12 to Whisper returning `(silence)`; `helpers.py:45-52` is the phrase-match
assertion and `:57-98` the retry-on-silence coping mechanism;
`SYSTEM_TEST_REPORT_20260430_002.md:141-152` still lists 5 "LLM response variation
failures" as expected noise *after* stabilization. So: **assert on timing and
structure; transcribe only as a smoke check that the audio is speech at all; never gate
on wording.** The constraint bites harder here than there — Hermes has no decision tree
pinning its phrasing. Scope this rule to `tests/live/` only: the deterministic content
assertions against fakes (`tests/test_stream_call.py:614-638` asserting
`hermes.appended == [('call-chan-1', 'Alpha one.')]`, `:1375-1413` asserting filler is
never credited) are phonesys's actual safety net for the interrupted-history invariant
and belong exactly where they are.

Note also that three of the four assertions this tier would add already exist against
fakes: barge-in within 300ms (`tests/test_stream_call.py:985-1000`), 20ms pacing
(`tests/test_audiosocket.py:61-71`), and no leaked bridge
(`tests/test_stream_call.py:870, :1245, :1278, :1302`). The genuinely new one is
*time from caller-stops to first outbound frame* — which is #2's metric wearing a
different hat.

### #8 — Full-call stereo WAV recording (ADAPT, medium)

**What:** `bot_simple.py:750` `AudioBufferProcessor()`, `:781-786` a `RECORD_AUDIO`
env gate + `pipeline_items.insert(-1, audiobuffer)`, `:1055-1056` start, `:1206-1211`
stop + `save_audio(...)`, `utils.py:34-51` stdlib `wave` into `BytesIO`.

**Where:** `stream_call.py` (tee inbound at `:266`) and `audiosocket.py` (tee outbound
at `:77`). Both are already 8kHz mono 16-bit slin — no conversion needed. Write the
file in `_teardown` (`:418-436`) via `_quietly`, so a failed write cannot skip the
bridge/channel cleanup.

**Do not copy the source's shape.** It saves *mono* (`num_channels=1`, merged), so it
cannot support offline diffing at all; and `bot_simple.py:1212-1215` registers a second
`on_audio_data` handler writing to the *same* `{conversation_id}.wav` path as
`:1207-1211`, so the two writers race. Take the idea and the env gate, nothing else.

**The real work, which makes this medium not small:** the two buffers are **not
time-aligned**. Inbound accumulates every 20ms for the whole call (`pump_audio` never
pauses). Outbound accumulates only while `send_frames` is writing — nothing during
LISTENING, nothing during the 4-10s think window. Naive concatenation gives a
compressed, progressively-skewed outbound track. You need a wall-clock-indexed outbound
writer that pads silence for every 20ms tick with no frame sent.

**Effort:** medium. **Risk:** `max_call_seconds` defaults to 600 (`config.py:26`,
`:83`) → ~9.6 MB per direction per call plus padding, so keep the env gate hard-off by
default. Do **not** put the append inside `send_frames`' paced loop without measuring
(`HANDOFF.md:95-97`: *"If barge-in ever looks broken, suspect pacing before
detection"*) — append to a preallocated buffer, never do I/O there.
**Solves:** D. A truncated barge-in becomes a file you can listen to offline instead
of needing a human on a handset.

**Drop the claimed HeardTracker payoff** — it is circular. The recorded outbound track
is built from the same socket writes `HeardTracker` already counts
(`audiosocket.py:77`, credited via `on_sent` at `:90`). It tells you nothing about what
Asterisk transmitted or the caller heard, which is the actual gap
(`HANDOFF.md:167-170`).

### #9 — Local acoustic VAD, scoped to replacing the blind 3.0s fallback (ADAPT, medium)

**What:** `requirements.txt` pins `onnxruntime==1.23.2`; `bot_simple.py` imports
`SileroVADAnalyzer` and both inbound and outbound params set `vad_enabled=True`,
`vad_analyzer=SileroVADAnalyzer()`, `vad_audio_passthrough=True`. A speech signal
independent of the ASR socket.

**Adopt one thing only:** replace `UTTERANCE_END_FALLBACK = 3.0`
(`stream_call.py:59`, applied at `:339-344`, `:348-349`, `:353`) with a *measured local
silence duration*. That is a real improvement to a real weak spot for little cost.

**Two claimed benefits that do not survive scrutiny:**

- *Barge-in latency:* today's cost is Deepgram RTT (~100-200ms) **plus** the 300ms
  confirm window (`config.yaml:31 barge_in_confirm_ms: 300`). Local VAD removes only
  the RTT — you still need a confirm window, and Silero is *more* trigger-happy than a
  transcript, not less (`SpeechBurst`'s docstring at `:76-84` exists precisely to
  reject a cough). ~150ms on a system whose headline problem is 4-10 **seconds**.
- *Problem E:* near-worthless. `:367-374` apologizes and hangs up on `stt_ended`
  precisely because a VAD-only bridge has no transcript. Knowing the caller is talking
  while unable to understand a word does not keep a call alive.

Note the source has **not** tuned any of this: `grep` for
`VADParams|stop_secs|smart_turn|turn_analyzer` across the whole tree returns **zero**
hits. `SileroVADAnalyzer()` is bare in both places, i.e. an untuned fixed silence
timer that fires on any pause — strictly worse than phonesys's UtteranceEnd gate as a
turn-end signal. Take the *idea of a second independent signal*, not their config.

**Effort:** medium. **Risk:** four, in descending order.
(1) **Echo** — the one that can regress working barge-in. Silero fires on any
speech-like energy including the agent's own voice returning through PSTN
hybrid/echo; the 300ms sustained-interim confirm plus punctuate/smart_format make the
transcript path far more forgiving. A VAD-armed interrupt could make the agent cut
itself off mid-reply, a user-visible regression on the flagship 7001 feature.
(2) **CPU on the audio path** — inference must not delay `stt.send()`; the module
docstring (`:6-11`) is explicit that continuous forwarding is what makes Deepgram
finalise at all, and `stream_stt.py:45`'s 5s SEND_TIMEOUT plus `:269-274` means a
stalled pump ends the call. Run inference off the pump task.
(3) **Frame-size mismatch** — inbound is 320 bytes = 160 samples at 8kHz; Silero's
8kHz path wants 256-sample windows. Getting the re-chunk wrong degrades detection
silently rather than erroring.
(4) **Dependency weight** — `pyproject.toml` currently lists four pure-Python deps.
`onnxruntime` is a large native wheel plus a model file: a qualitative change to the
deploy surface.
**Solves:** C, narrowly. **Sequence after #2 and #7** — none of this can be proven
against fakes, and echo behaviour appears only on a real handset.

### #10 — Per-call config re-read, whitelist-scoped (ADAPT, small, marginal)

**What:** `db.py:14-30` fetches per-call config and validates it;
`bot_simple.py:251-256` fails the call with HTTP 400 if it is missing.

**Honest framing:** the source's *reason* does not transfer. ai-receptionist is
multi-tenant and config is per phone number in a database. phonesys is single-tenant
with one YAML read once at `main.py:163`. The proposed benefit — tune 7001 without a
restart — is a repurposing, and a small one: tuning requires re-dialling anyway
(problem D), and the restart itself is one Python process. The only thing making
restart non-instant is `filler_bank.prepare()` (`main.py:133-140`), N ElevenLabs round
trips. Fixing *that* delivers most of the value with less surface.

Kept only because the plumbing already exists: `ExtensionConfig` is a frozen dataclass
(`config.py:16-36`) and `main.py:231` already looks it up per StasisStart.

**Risk — two hazards that make this a whitelist, not a re-read:**
- `main.py:231` is the **shared** dispatch point; `ext_cfg` goes to `CallHandler.handle`
  at `:265` as well as `StreamingCallHandler` at `:256`. A naive re-read puts freshly
  parsed `max_call_seconds` on the 7000 path (`call.py:110-111`). Gate on
  `ext_cfg.mode == "streaming"`.
- **`audiosocket_port`/`audiosocket_host` must be pinned to startup values.** `handle`
  reads `ext.audiosocket_port` at `:249` to tell Asterisk where to dial back, but the
  listener binds once from the *startup* config at `main.py:113-116`. A re-read that
  picks up an edited port originates every streaming call to a port nothing is
  listening on → total 7001 outage at `AUDIOSOCKET_WAIT_TIMEOUT` with a confusing
  symptom. Same for `greeting`/`voice_id`, since the filler bank was synthesized
  against the startup voice.

Let only `barge_in_confirm_ms`, `filler_delay_ms`, `filler_repeat_ms`,
`max_silence_seconds` float. `load_config` does `load_dotenv` + `_require_env`
(`config.py:53-64`) and is a blocking file read + `yaml.safe_load` on the event loop at
answer time — wrap in try/except with fallback to the last good config.

**Solves:** D, marginally.

---

## 3. What NOT to take

Recorded so these are not re-raised.

| Pattern | Why not |
|---|---|
| **pipecat's `STTMuteFilter` / declarative interruption policy** | **Already present, and phonesys's version is richer.** All three behaviours exist: MUTE_UNTIL_FIRST_BOT_COMPLETE ↔ `_speak` is non-interruptible (`stream_call.py:438-452`); FUNCTION_CALL ↔ the THINKING-window `interrupt.clear()` guarded at `:559-590`; plus `silence_filler_on_interrupt` at `:502-512`. The delta is spelling, not behaviour. Worse, phonesys's policy set is **not expressible** as pipecat's flat enum: pipecat mutes the user turn wholesale during a function call, while phonesys deliberately keeps `utterance_ready` live so a genuine new question can abandon a stale reply. And on the greeting, pipecat *discards* an overlapping utterance where phonesys *answers* it — on a phone call phonesys is better. Refactoring would risk the merged, live-tested barge-in machine for zero behavioural gain. |
| **VAD-derived turn-end (STT as pure transcriber)** | **Load-bearing citation is dead code.** `audio.py:30-41` sits inside `UserAudioCollector`, defined at `audio.py:14` and never imported or instantiated anywhere in the tree. What remains is a bare `SileroVADAnalyzer()` with no `VADParams` — a framework default, not a validated decision. Also the highest-risk change available: it would replace `stream_call.py:296-322`/`:339-354`, i.e. exactly what commit `fa6155e` just landed, and it must be tuned against live calls that cannot currently be reproduced. See #9 for the narrow slice worth taking. |
| **Pre-roll ring buffer for the leading edge of speech** | **Already handled, by a better mechanism.** `pump_audio` (`:258-268`) forwards *every* inbound frame in *every* state, unconditionally — there is no capture gate that could clip a phoneme, so there is no pre-speech window to preserve. Also dead code in the source (same `UserAudioCollector`). Re-proposing it means introducing a speech gate into the one loop that must never gate, i.e. manufacturing the exact hang the current design avoids. |
| **Incremental TTS consumption (Cartesia streaming)** | **No evidence + highest risk.** The sole citation (`utils.py:202-212`) is a `provider_factory` branch constructing `CartesiaTTSService(...)`. That is a library selection, not a pattern — no first-party implementation, no measurement. The phonesys gap is real (`stream_tts.py:106-110` fully buffers the body, `:127` frames it), but `tracker.expect(index, len(frames))` (`stream_call.py:543`) **requires the total frame count up front**, and `heard_text` decides what was received via `self._sent.get(i, 0) < expected` (`:136`). Streaming synthesis opens a window where `expected` is understated and a partial sentence is credited as fully heard — flowing straight into `append_assistant` and breaking interruption correctness *silently*. It also invalidates the `stop_filler()`-after-`synthesize_frames` ordering (`:558`, justified at `:546-557`). Revisit only if #2 implicates the ElevenLabs round trip specifically. |
| **Fire filler at the tool-call boundary instead of a blind timer** | Masking only — it does not shrink the gap. Already recorded as considered-and-deferred (`docs/superpowers/specs/2026-08-05-streaming-filler-speech-design.md:240-244`). The cheap alternative is untried: `filler_delay_ms` is a config value (`config.py:33`). Implementation cost is understated — it means changing `chat_stream`'s contract from `AsyncIterator[str]`, which ripples into `_next_delta` and `_reply`'s delta handling, the two most delicately-commented functions in the file. And `tool.progress` carries the model's **private reasoning** in its `delta` field (`hermes.py:52-55`, covered by `test_hermes.py:68-76`); widening the yield type is exactly where that leak gets reintroduced. |
| **Config-sweep harness (N candidates × M scenarios)** | Their own harness took 200-250s for 7×4 and returned all-ERROR for 3 of 7 candidates from infrastructure failures. Problem C is not threshold-shaped anyway: raising `UTTERANCE_END_FALLBACK` trades mid-thought interruptions against making *every* turn slower, i.e. it worsens A — a fixed tradeoff curve with no optimum. Once #2 exists, `@pytest.mark.parametrize` over `barge_in_confirm_ms` gets ~90% of the value in 20 lines. Also note step zero would be making `UTTERANCE_END_FALLBACK` configurable at all (it is a module constant at `stream_call.py:59`), and if that ever happens it needs a floor — 0 means replying on every 300ms Deepgram final, the exact half-sentence bug the comment at `:304-317` exists to prevent. |
| **Parallelize call setup / prefetch in the connect window** | Category error: everything it touches (`create_session`, `stt.connect`, bridge setup) happens **once, before the greeting**, and is not on any turn's critical path — while problem A is per-turn, mid-conversation. Residual benefit ~100-400ms once. And the specific proposal to hoist `stt.connect()` is actively harmful (see #3). |
| **ERROR-level log sink wired to an out-of-band notifier (apprise)** | Solves no listed problem; phonesys is a single local process whose operator reads stderr on the same machine. Worse, `apprise.notify` does **synchronous network I/O** and a logging handler runs inline on the calling thread — every cited call site (`stream_call.py:380`, `:382`, `:436`, `_quietly` at `:186`) runs on the event loop, so a slow webhook could audibly glitch a concurrent call's outbound speech via the 20ms paced writer. Doing it safely needs `QueueHandler`+`QueueListener`, no longer a five-line addition. The one failure genuinely worth surfacing (the AudioSocket bind failure at `main.py:117-124`) is already surfaced per rejected call at `main.py:245-252`. |
| **Their health endpoint** | `webhook.py:96-99` returns a hardcoded `ok`. |
| **`testtool`'s dependency stack** | `audioop` (removed in 3.13+), `whisper` (→ torch), `gTTS`. See #7. |

---

## 4. Where the two codebases disagree — and who was right

The interesting part. Five real forks in the road.

### 4.1 Who owns the conversation history — **ai-receptionist wins**

ai-receptionist keeps the message array in-process and treats the LLM endpoint as
stateless per turn; the assistant turn is appended by a processor placed *after*
`transport.output()`, with a comment explaining that the position is the point.
phonesys hands the history to Hermes behind a `session_id` and, when a reply is cut
off, mails a correction after the fact — `spawn(_quietly("append_assistant", ...))` at
`stream_call.py:641`, fire-and-forget because awaiting it stalls the caller for a full
LLM turn.

phonesys is wrong here and already knows it. The generalizable insight — worth stating
in exactly these words because it survives the transport difference — is that
**committing an assistant turn is an egress-ordered event, not a generation-ordered
one.** pipecat encodes that as list position; phonesys would encode it as calling the
commit from the code that owns `HeardTracker`. Same invariant, different syntax.

Two caveats against over-crediting the source. First, phonesys already owns the
*harder* half: byte-accurate accounting of what actually left the socket, which
ai-receptionist does not attempt. Second, phonesys must additionally call
`POST /v1/runs/{id}/stop` to halt server-side generation — a step with no analogue in
the source, because their LLM call is a plain HTTP request that dies when you drop it.

### 4.2 Where the turn boundary comes from — **neither, and the fix is a hybrid**

ai-receptionist derives turn boundaries acoustically and locally (Silero), and
configures Deepgram as a plain transcriber — `LiveOptions(language="multi",
model="nova-3-general")`, no `endpointing`, no `utterance_end_ms`, no `vad_events`.
phonesys derives them textually and remotely — `endpointing=300`,
`utterance_end_ms=1000`, `vad_events=true` (`stream_stt.py:94-99`), gated on
UtteranceEnd with a blind 3.0s wall-clock fallback.

Each has the half the other lacks. ai-receptionist has the right *source* — turn-taking
is an acoustic phenomenon and a local detector has no network in the loop — with
literally zero tuning (`grep` for `VADParams|stop_secs|smart_turn` returns nothing).
phonesys has done the tuning, on a signal that requires a round trip to a third party
to answer a question about the last 300ms of local audio, and whose failure mode is a
constant chosen by feel.

In hindsight the honest answer is that **phonesys is currently better and structurally
weaker**. The cheap merge is #9: keep UtteranceEnd as the primary signal and let a
local silence measurement replace the blind constant. That is the only place the
source's approach is strictly additive rather than a substitution.

### 4.3 What counts as "the caller heard it" — **phonesys wins, and the difference is the transport**

phonesys built `HeardTracker`: expected-frame accounting per sentence, credited only
from writes that drained, breaking at the first gap, with a paced writer that makes
write ≈ playout. ai-receptionist has nothing comparable; pipecat approximates by frame
ordering.

This is the clearest case where "we cannot adopt their approach" is correct rather than
NIH, and the reason is worth writing down: **Twilio's Media Stream is a flow-controlled
WebSocket with mark events; Asterisk's AudioSocket is a raw TCP pipe that will happily
buffer three seconds of speech the caller has not heard.** ai-receptionist can afford
not to care. phonesys cannot — constraint 6 is precisely this. Different transports
impose different obligations, and a pattern that exists in one and not the other is
often evidence about the transport, not about the engineering.

Two honesty notes on phonesys's side. The tracker counts bytes handed to a socket, not
audio in an ear — `HANDOFF.md:187-189` calls it "the paced-playback approximation" and
review flagged the name (`PlayoutTracker` is correct). And what makes the number good
is the 20ms pacing, not the counting. If the PBX ever moves to Asterisk 21.11+,
`chan_websocket`'s `MARK_MEDIA`/`MEDIA_MARK_PROCESSED` gives a strictly stronger
boundary and this whole mechanism becomes an approximation of something exact
(`HANDOFF.md`, "Blocked").

### 4.4 How to test a voice agent — **ai-receptionist had the right direction and the wrong assertions**

ai-receptionist built the E2E rig phonesys lacks — real transport, real pacing, real
services, a measured TTFU with matched clocks — and then asserted on *what the bot
said*. Result: 15 of 19 tests failing, 12 from Whisper returning `(silence)`, a
retry-on-silence helper bolted on to cope, 5 "LLM response variation failures" still
listed as expected noise after stabilization, and a 10s threshold that flipped
PASS/FAIL at 9.554s and 10.298s on identical code. A suite in that state gets muted.

phonesys did the opposite: ~180 fast, deterministic, fake-driven tests including
content assertions that pin the interrupted-history invariant exactly
(`tests/test_stream_call.py:614-638`), and **no E2E at all** — hence problem D.

Neither is right alone, and the synthesis is specific: phonesys's fake-driven content
assertions are correct *where they are* and must not be softened; the new live tier
should assert on **timing and structure only**, transcribe purely as a smoke check that
the audio is speech, and never gate on wording. Hermes has no decision tree pinning its
phrasing, so phrase-matching would be *less* stable here than the 15/19 the source
managed. Their failure is the cheapest lesson in this whole exercise: they paid for it,
phonesys can just read the postmortem.

### 4.5 Framework vs hand-rolled — **a genuine draw, with a non-obvious asymmetry**

ai-receptionist bought pipecat and got VAD, interruption, context aggregation and
transport for free. Then it wrote `ResilientOpenRouterLLMService` (`utils.py:54-199`,
~150 lines) to wrap the framework's LLM service in a poll-cancel-retry watchdog;
`STTMuteConfig` sets to re-express interruption; and a `gather` rewrite because the
framework's init was serial and blocking the caller for 5-7 seconds. phonesys
hand-rolled everything and has `stream_call.py` at 771 lines, including 31 lines of
comment justifying a single `interrupt.clear()`.

The framework bought the boring 80% and taxed the interesting 20%. The asymmetry worth
noting: **pipecat's interruption vocabulary cannot express phonesys's semantics.**
`STTMuteStrategy` is a flat set — mute the user during the greeting, mute the user
during a function call. phonesys's THINKING window deliberately keeps `utterance_ready`
live while clearing `interrupt`, so a cough does not abort a reply but a genuine new
question does (`stream_call.py:559-590`, `:699-709`). There is no way to say that in
the source's abstraction. That is not a maturity gap — it is an abstraction boundary
placed where "did the caller hear it" was not the central question. For a system where
it *is* the central question, the boundary is in the wrong place.

Which is the real reason most of this codebase does not transfer, and the reason to
stop looking for more.

### 4.6 (Minor) Should stall speech go into history? — **opposite calls, both locally correct**

ai-receptionist writes the phrase it spoke into the array as an assistant message
(`engine.py`, the `insert(0, {'role': 'assistant', ...})` on the post-node) so the model
knows it already said "one moment". phonesys deliberately excludes filler —
`FILLER_SENTENCE_INDEX = -1` (`filler.py:20`), `_discard_frame`
(`stream_call.py:690-696`) — because crediting it would tell Hermes it said words it
never generated.

Both are right for their own API. Under phonesys's current `/chat/stream` the only
write path is `append_assistant`, which POSTs a *full synchronous agent turn*
(`hermes.py:78-86`), so "crediting" filler costs a spurious LLM turn — it is
unimplementable, not merely undesirable. Once #6 lands and the bridge owns the array,
ai-receptionist's call becomes the right one and it is a few lines. Note the
priority within it, though: the **greeting** is the clear correctness win (the agent
can currently re-greet, because `_speak` never reads `heard_text`); the filler half
fixes a duplication problem with no logged evidence in phonesys, and filler is off by
default anyway (`config.py:33`, `filler_delay_ms=0`).

---

## 5. Suggested sequence

1. **#1** marker tier — alone, first, protects constraint 5.
2. **#2** latency instrumentation + **#3** greeting cache + **#4** log `spoken`. One
   afternoon; #3 is the only one that changes behaviour.
3. **Live baseline call** with #2 in place. Attribute problem A before doing anything
   else. Several items below are contingent on what it says.
4. **#6** `/v1/runs` migration — but first prove `GET /v1/runs/{id}/events` yields
   incremental text. This is the only structural change worth making, and it deletes
   the annotation hack, the overlapping-turn race, and (via §4.6) the re-greeting bug
   in one go. Rename `HeardTracker` → `PlayoutTracker` while you are in there.
5. **#5** first-delta watchdog — trivially safer once #6 makes cancellation
   expressible.
6. **#7** live harness and **#8** WAV recording — for D, once there is something worth
   verifying repeatedly.
7. **#9** local VAD for the fallback constant, **#10** config whitelist — only if #2
   and live calls show they matter.

Not scheduled: E (STT reconnect) and F (multiple streaming extensions binding the
shared listener). ai-receptionist has nothing useful for either — its reconnect
(`testtool/server.py:461-511`) is harness-side, rebuilding the whole call from the
client, which is not an option when the client is a person on a phone.
