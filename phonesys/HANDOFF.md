# phonesys — Session Handoff

**Date:** 2026-08-05
**Branch:** `feat/barge-in-streaming` (22 commits, 142 tests passing, NOT merged)
**Base:** `main` at `7f01f3d`

---

## What this project is

A Python bridge connecting a local Asterisk/FreePBX PBX to a Hermes AI agent, so
you can phone an extension and talk to it.

Two working agents:

| Ext | Mode | Status |
|---|---|---|
| **7000** | turn-based | ✅ **merged to `main`, working, verified by live call** |
| **7001** | streaming + barge-in | ⚠️ on branch, **never tested by a human call** |

Extension 7000 keeping working is the project's hardest constraint. It is
enforced by config default (`mode: turn`) and by isolating streaming startup
failures so a port conflict cannot take 7000 down.

---

## IMMEDIATE NEXT STEP

**Live human verification of 7001 has never been done.** Everything else is
blocked behind it. The bridge and Hermes gateway are currently running.

1. Dial **7000** — regression check, must behave exactly as before.
2. Dial **7001** — expect greeting, then a reply that begins sooner than 7000's
   (audio starts after the first sentence, not the whole reply).
3. Dial **7001 and talk over it** — should stop within ~300 ms.
4. Ask **"what did you just tell me?"** — should reference only what played.

If 7000 has regressed, stop and fix before anything else.

To restart the bridge if needed:

```bash
cd /home/hoyack/work/phonesys && .venv/bin/python -m phonesys.main
```

Prerequisites (all currently up): ARI on `127.0.0.1:8088`, Hermes `phonebot`
gateway on `127.0.0.1:8642`, AudioSocket listener on `0.0.0.0:9010`.

---

## Architecture as built

```
Zoiper ──SIP/RTP──> Asterisk 21.10.2 (FreePBX 17, Docker)
                         │  7000 → Stasis(phonesys,7000) → CallHandler      (turn-based)
                         │  7001 → Stasis(phonesys,7001) → StreamingCallHandler
                         ▼
                  phonesys bridge (WSL host, Python 3.14)
                    │           │            │
               Deepgram     Hermes       ElevenLabs
               live WS     phonebot        (TTS)
                            :8642
```

7001 bridges the caller with an AudioSocket channel (TCP, port 9010) for
continuous duplex 8 kHz PCM.

### Modules

| File | Responsibility |
|---|---|
| `audiosocket_codec.py` | frame codec `[type:1][len:2 BE][payload]` |
| `audiosocket.py` | TCP server, session per call, **real-time paced writer** |
| `ulaw.py` | G.711 µ-law → slin (256-entry table; `audioop` gone in 3.13+) |
| `stream_stt.py` | Deepgram live WebSocket → `SttEvent` |
| `stream_tts.py` | `SentenceBuffer` + ElevenLabs → 320-byte frames |
| `stream_call.py` | **the barge-in state machine**, `HeardTracker`, `SpeechBurst` |
| `tasks.py` | module-level registry so fire-and-forget tasks aren't GC'd |
| `hermes.py`, `ari.py`, `config.py`, `main.py` | shared, extended not replaced |
| `call.py`, `recordings.py`, `pipeline.py` | **turn-based path — do not touch** |

---

## Measured facts — do NOT "correct" these from memory

All verified against live Asterisk 21.10.2 on this host on 2026-08-04.

1. **`formats=slin` (PLURAL) is mandatory** on ARI origination. Singular
   `format` belongs to `externalMedia`, is silently ignored, and the driver
   then negotiates **`slin192`** — 7680-byte frames at 375 KB/s instead of
   320-byte 8 kHz frames. Everything appears to connect while delivering
   unusable audio.
2. **AudioSocket framing:** `0x01` = UUID as 16 **raw bytes**; `0x10` = audio,
   320 bytes = 20 ms slin 8 kHz mono.
3. **Asterisk paces INBOUND at 20 ms** but does **not** throttle our writes.
   Outbound must be paced by us — without it Asterisk buffers speech the caller
   hasn't heard, breaking **both** interruption and heard-tracking.
   *If barge-in ever looks broken, suspect pacing before detection.*
4. **Deepgram needs continuous audio** (including silence) to finalise an
   utterance. The STT pump must forward in **every** state, including while the
   agent speaks. Pausing it during playback silently hangs the conversation.
5. **Hermes SSE:** reply text is `assistant.delta` **only**. `tool.progress`
   also carries a `delta` field holding the model's private reasoning — filter
   by event *name*, never by presence of `delta`, or the agent reads its own
   reasoning aloud to the caller.
6. **Port 9010**, not 9000 — 9000 is taken by an unrelated minio container.
7. **Extension 7000's `media_address`** must be set per-extension under Docker
   Desktop, else one-way audio and a call that dies at exactly 30 s. See
   `~/freepbx-docker/README.md`.

---

## OPEN DECISION (needs the user)

**Interrupted-history handling.** You chose *"truncate history to what was
actually heard"* over *"keep the whole reply"*.

What shipped does **not** deliver that, and cannot:

- `chat_stream` commits the **full** un-heard reply server-side as it generates.
- Hermes exposes `GET /api/sessions/{id}/messages` and **no way to edit or
  truncate** history (verified).
- `append_assistant` therefore only *annotates*: it posts a `[system]` note
  saying "the caller only heard X".
- It also had to become fire-and-forget (awaiting it stalled the caller for a
  full LLM turn), so a correction can now **overlap the next user turn** on the
  same session.

Three options, unchanged:

- **(a) Accept the annotation.** Zero work; relies on the model honouring the
  note; overlap risk remains.
- **(b) Move the Hermes seam to `/v1/runs`** with bridge-owned history
  (see external review below — this is now the recommended path).
- **(c) Ship and judge from live calls.** My prior recommendation, now
  superseded by (b) — see assessment.

---

## External review (2026-08-05) — assessment

A third party reviewed the design. **Verified against this deployment:**

### ✅ Right, and actionable now — move Hermes seam to `/v1/runs`

Confirmed our Hermes exposes `POST /v1/runs`, `GET /v1/runs/{id}`,
`GET /v1/runs/{id}/events`, **`POST /v1/runs/{id}/stop`**, plus
caller-provided `conversation_history`.

This is the strongest recommendation and directly resolves the open decision.
Bridge owns the transcript; on interruption call `/stop`; commit only the text
that actually played; supply truncated history on the next run. Removes the
annotation hack *and* the overlapping-turn race in one change.

**This is what I would do first after live baseline testing.**

### ✅ Right — `is_final` is the wrong turn-end signal

We use `is_final` and never `speech_final`. Deepgram's `is_final` means "this
segment is stable", not "the person finished". Our own final review found the
same defect independently (the agent would start answering mid-thought). The
current fix gates on `UtteranceEnd`, which the reviewer correctly notes is also
imperfect — it can fire during a deliberate pause. Their suggested policy
(accumulate finals, prefer `speech_final`, client-side debounce, longer timeout
for digits/addresses) is sound and worth adopting.

### ✅ Right — `HeardTracker` is misnamed

It tracks *bytes written to a socket*, not audio heard. Rename to
`PlayoutTracker`. Cheap, prevents a real misconception.

### ⚠️ Blocked — `chan_websocket` requires an Asterisk upgrade

**We are on 21.10.2. `chan_websocket` needs 21.11+.** Verified: no websocket
channel driver is registered (`res_http_websocket`, `res_pjsip_transport_websocket`,
`res_websocket_client` are present — those are different things).

The recommendation is genuinely attractive — `PAUSE_MEDIA` / `CONTINUE_MEDIA` /
`FLUSH_MEDIA` / `MARK_MEDIA` / `MEDIA_MARK_PROCESSED` / `MEDIA_XOFF` would
replace our paced writer and give a much stronger "heard" boundary than a socket
write. But it costs an image upgrade (`escomputers/freepbx:17-nofail2ban`) on a
PBX that currently works, and it is **one patch version away**. Treat as a
deliberate, separately-tested upgrade — not a quick win.

### ❌ Wrong — we do not use Cartesia

The review's TTS section is entirely about Cartesia. **We use ElevenLabs**
(`stream_tts.py`). The transferable principles still hold: cancel TTS locally
rather than trusting provider cancellation, ignore late chunks, use one context
per assistant turn rather than per sentence. But the specific API guidance does
not apply, and the "decimal-token failure" it credits to Cartesia docs was found
here by our own review of `SentenceBuffer`.

### Verdict on the review

Its two headline structural criticisms — the paced-playback approximation and
the post-hoc history correction — are **correct and match what our own reviews
flagged as weakest**. Sequence I'd follow: live baseline test → `/v1/runs`
migration → rename `HeardTracker` → turn-end policy → consider the Asterisk
upgrade for `chan_websocket` last.

---

## Parked findings (real, ruled non-blocking)

Full rulings in `.superpowers/sdd/2026-08-04-barge-in-streaming/progress.md`
(git-ignored; recreate from `git log` if lost).

1. **Overlapping Hermes turns** — folded into the open decision; option (b) removes it.
2. **`SpeechBurst` docstring overclaims** "a cough can never fire later" — the
   window is left open, so a lone later interim can fire. One-line fix.
3. **`test_burst_does_not_fire_when_interims_stop...`** calls `reset()` before
   its assertion, so it asserts nothing about the invariant it names.
4. **Two `test_tasks.py` tests pass without the registry** (a bare `ensure_future`
   in `asyncio.sleep` isn't collectable). `test_main.py`'s gc test does catch a revert.
5. **`stt_ended`/`audio_ended` race** can silently skip the spoken apology.
6. **`UTTERANCE_END_FALLBACK = 3.0`** can fire mid-thought during >3 s of
   continuous speech with no intervening final.
7. **TTS content-type reject-list** passes a missing content-type and `audio/mpeg`.
8. **Deepgram WS reconnect not implemented** — apology + clean end only (deliberate;
   a half-implemented reconnect was judged worse).
9. **Only the first streaming extension's port** binds the shared listener; a
   second streaming extension would not work today.

---

## Process notes

- Superpowers skills drove this: brainstorming → writing-plans →
  subagent-driven-development, with a task review + fix loop per task and a
  whole-branch review at the end.
- **16 defects were found in the plan's own reference code**, none from
  implementer error: missing timeouts, a hangup race, two independent
  "assume parsed JSON is a dict" crashes, a dependency pin that only worked by
  accident, a `SentenceBuffer` contradicting its own test, a resource leak on
  the most common exit path, a port conflict that would have taken 7000 down.
- The **whole-branch review was the highest-value review** — it caught three
  things every per-task review missed: `barge_in_confirm_ms` being ~15 interim
  events (1.5–4 s) rather than 300 ms; `utterance_end` parsed but never
  consumed; interruption stalling the caller a full LLM turn.
- Synthetic call verification (originate `Local/7001@from-internal`) exercises
  the whole topology without a handset and **proved zero orphaned bridges or
  channels** after teardown. Reuse this technique.

---

## Environment

- **Repo:** `/home/hoyack/work/phonesys` — `.env` is gitignored, holds
  `ARI_*`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `HERMES_PHONEBOT_API_KEY`.
  Never read, print, log, or commit its values.
- **PBX:** `/home/hoyack/freepbx-docker` (not a git repo). Its README documents
  the one-way-audio/`media_address` trap and the phonesys bind mounts.
- **Hermes profile:** `~/.hermes/profiles/phonebot` — voice-tuned `SOUL.md`,
  isolated memory, inherited Slack tokens commented out, `api_server` on 8642.
  Skills trimmed to 5; `.no-bundled-skills` marker prevents re-seeding.
- **Python 3.14** in `.venv`. Run tests: `.venv/bin/pytest`.
- Dialplan for 7000/7001 lives in the container's
  `/etc/asterisk/extensions_custom.conf` (FreePBX never regenerates it).

---

## Merging

Branch is **not merged** and the plan's Task 10 is incomplete pending live
calls. When ready, use `superpowers:finishing-a-development-branch`. The
repo has no git remote — pushing anywhere is a decision for the user.
