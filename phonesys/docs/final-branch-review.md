# Final pre-merge assessment — `feat/barge-in-streaming`

Merge-base `7f01f3d`, HEAD `b362f00`, 43 commits. 193 passed / 1 deselected.
Scope of this pass: whole branch, with emphasis on the second-session work
(ARI playback completion, 7000 silence policy, filler, stale-reply abandonment,
latency instrumentation, live test tier).

---

## 1. Merge recommendation

**Merge after fixing the listed items.**

Rationale, in the terms that matter here:

- **Extension 7000 is not endangered.** Every finding that touches 7000's files
  (`call.py`, `pipeline.py`, `recordings.py`, `stt.py`, `tts.py`) survived
  verification only at **minor** severity. None of them can break a live 7000
  call, change turn-taking, or alter conversational behaviour. The two 7000
  items that are genuine regressions against merge-base (a leaked `.ulaw` file
  when `play()` raises; `_speak` swallowing `CancelledError`) are grime and
  shutdown ergonomics respectively — neither is caller-visible. The playback
  completion work itself is a net improvement: it fixes 7000 hanging up on the
  caller mid-reply, which is the more serious pre-existing bug.
- **Nothing critical survived.** All four findings originally filed as critical
  were downgraded on verification: two are test-coverage gaps against correct
  shipped code, one (`main.py` streaming-rejection branch) is only reachable
  when streaming is *already* down, and one (startup blocking) is only reachable
  when ElevenLabs is already failing, in which case 7000 is already degraded.
- **7001 has one real behavioural gap** — Hermes stream errors drop the call
  instead of apologizing — which is a deviation from the branch's own design
  doc, in a file the design doc's sibling rows are implemented in. That is the
  only shipped-code must-fix.
- **The rest of the must-fix list is test coverage on load-bearing one-liners.**
  Three separate single-line guards in `stream_call.py` — `final_seen.clear()`,
  handle()'s `interrupt.clear()`, and pump_stt's `if utterance_parts:` — can each
  be deleted with a fully green 193-test suite, and each deletion produces a
  silent, call-breaking 7001 regression. The code is correct today; the suite
  does not defend it. Merging without those tests means the next refactor of
  this state machine has no net.

Do not merge before a live call on 7001 with filler enabled (section 5). The
branch has never been exercised end-to-end by a human.

---

## 2. Must fix before merge

### M1 — Hermes stream errors end the 7001 call instead of returning to LISTENING
`src/phonesys/stream_call.py:812` (`_reply` try block), handlers at `:832-848`.

`_reply` catches `_Interrupted`, `_TimeLimit`, `TtsStreamError`, `_CallEnded`
and nothing else. `HermesClient.chat_stream` raises `HermesError` on any
status >= 400 from *inside* the generator body (`hermes.py:62`), so it surfaces
from `nxt.result()` in `_next_delta` (`stream_call.py:917`), propagates out of
`_reply`, and lands in `handle()`'s generic `except Exception` (`:433`) which
tears down the bridge and hangs up both channels. `httpx.ReadError` /
`ReadTimeout` mid-SSE take the same path. Note the asymmetry: `stream_tts.py:114`
deliberately wraps httpx errors into `TtsStreamError` so callers need not catch
them; `hermes.py` does no such wrapping.

**Failure scenario (reproduced):** Hermes `api_server` restarts or a proxy
returns 502 on the caller's third question. Driving the real `handle()` against
a `chat_stream` that raises: `tts.spoken == ['Hello there.']` (greeting only),
no apology, `ari.hungup == ['as-chan', 'chan-1']`. The caller is dropped
mid-conversation on a transient blip the next request would have survived.

**Spec deviation, not just a gap:**
`docs/superpowers/specs/2026-08-04-barge-in-streaming-design.md:147` reads
`| Hermes stream errors | Apologize aloud, return to LISTENING |`. The rows
either side of it (146, 148) are implemented, 146 with an explicit comment
deprecating the reconnect. This row has no implementation and no comment
acknowledging a deviation.

**Fix:** add an arm alongside `TtsStreamError`, same shape:

```python
except (HermesError, httpx.RequestError) as exc:
    logger.warning("Hermes stream failed mid-reply, returning to listening: %s", exc)
    credit_heard("hermes stream failed mid-reply")
    # speak a short apology, then fall through to LISTENING
```

Test: `FakeHermes.chat_stream` that raises after the greeting; assert the call
survives, an apology is spoken, and the next utterance is answered normally.

### M2 — `handle()`'s `final_seen.clear()` is an unguarded mutant
`src/phonesys/stream_call.py:393`; gap at `tests/test_stream_call.py:1842`.

Deleting line 393 passes **193/193**. Without it `final_seen` latches set on the
first final transcript of the call and never clears, so speak_sentence's guard
at `:699-700` (`index == 0 and not utterance_ready.is_set() and not
final_seen.is_set()`) is False forever and the one-shot `interrupt.clear()`
never runs again. Every turn from 2 onward then hits `raise _Interrupted` at
`:770` on its first frame.

**Reproduced:** turn 1 plays 8 frames; turn 2, with two interims 25ms apart
during the thinking window (`barge_in_confirm_ms=20`, no final behind them),
plays **1 frame** — effectively silence — and this repeats for the rest of the
call. Suite green either way.

Why no test sees it: every guard test goes through `_run_one_turn`
(`tests/test_stream_call.py:538`), which constructs fresh Events per turn and
calls `_reply` directly, so `handle()`'s clearing is structurally invisible. The
only two multi-turn `handle()` tests never fire a bare interim burst in a later
turn's thinking window.

**Fix:** one multi-turn `handle()`-level test: two full turns, bare interim
burst during turn 2's thinking window, assert turn 2's reply plays in full.

### M3 — Filler is never exercised through `handle()`
`src/phonesys/stream_call.py:379-380`; gap at `tests/test_stream_call.py:1495`.

All 19 filler-related tests call `handler._reply(...)` (via `_run_one_turn`),
never `handler.handle(...)`. `filler_bank=` is never combined with `handle()`
anywhere in the suite. That means `interrupt` is always *clear* at `_reply`
entry in tests — the opposite of production, where pump_stt set it on the final
transcript that triggered the turn. The only thing stopping
`silence_filler_on_interrupt` (`:626-637`) from firing instantly on entry is
the uncommented two-liner at `:379-380`.

**Reproduced:** replacing `:379-380` with `pass` gives `bank.taken == []` and
**0 filler frames**, against a baseline of `[0,1,2,2,2,2,2]` and 140 frames.
Filler is 100% dead on every call, restoring exactly the 4-10s of dead air the
feature was built to remove — and the suite still reports 193 passed. (The same
mutation also spins `handle()`'s main loop, which no test catches either.)

`speak_sentence`'s `index == 0` guard does not save it: that runs *after*
`await stop_filler()` at `:698`.

**Fix:** one integration test that pre-sets `interrupt` before entering
`handle()` with a `filler_bank`, and asserts filler frames reach the socket.
Also add a comment at `:379-380` naming the contract.

### M4 — pump_stt's `if utterance_parts:` guard is unguarded
`src/phonesys/stream_call.py:351-354`; gap at `tests/test_stream_call.py:1264`.

`elif ev.kind == "utterance_end": burst.reset(); if utterance_parts:
utterance_ready.set()`. `utterance_ready` is the abandon signal `_next_delta`
races the SSE stream against (`:812`, `:894-903`), so any UtteranceEnd landing
in the thinking window kills the in-flight reply. The guard is the only thing
distinguishing "caller finished a new thought" from "a bare UtteranceEnd with
nothing behind it". It carries no comment.

**Reproduced:** with the guard deleted, a bare UtteranceEnd 0.08s into a 0.4s
thinking window gives `tts.spoken == ['Hello there.']` and **0 reply frames** —
the caller's question is never answered, silently (no crash, no hangup, handle()
just loops back to listening). Suite: 193 passed.

Reachability is not only duplicate UtteranceEnds: the `stalled` fallback path
(`:381-390`) replies with no UtteranceEnd yet seen and clears `utterance_parts`,
so the late real UtteranceEnd arrives with parts empty — exactly the guarded
case, on the turn already having turn-detection trouble. `stream_stt.py` also
drops empty transcripts, so a noise burst yielding only an empty final lets its
UtteranceEnd through with nothing banked.

The mirror case *is* tested (a bare final during the thinking window must not
abandon), which makes the asymmetry conspicuous.

**Fix:** a test emitting a bare UtteranceEnd during the thinking window;
assert the reply completes. Add a comment to the guard.

### M5 — 7000: `.ulaw` leak when `ari.play()` raises (regression vs merge-base)
`src/phonesys/call.py:83-97`.

`path.unlink(missing_ok=True)` lives in the `finally` of the *second* try block,
which covers only `wait_for(finished)`. Any exception out of `play()` re-raises
from the `except BaseException` at `:87` and never reaches it. At merge-base
`git show 7f01f3d:src/phonesys/call.py` wrapped the whole play call in
`try/finally: path.unlink(...)`, so this path *did* clean up. Splitting the try
blocks to add the PlaybackFinished wait dropped that coverage.

Reachable on the documented normal case — `call.py:130` "A 404 anywhere
(greeting, playback, answer) means the same thing: the caller hung up
mid-operation". Reproduced: fake ARI raising `AriError(404)` from `play()`
leaves `reply-<hex>.ulaw` in the shared sounds mount. Nothing sweeps that
directory (`preflight_media_dirs` only checks permissions).

Included as must-fix not for severity — it is minor, tens of KB, slow grime —
but because it is a **regression on the protected extension**, the fix is one
indentation level, and "7000 must never regress" is the branch's stated bar.

**Fix:** hoist `finally: path.unlink(missing_ok=True)` to wrap both blocks.
Test: assert `sounds/` is empty after `play()` raises.

### M6 — 7000: `_speak` swallows `CancelledError` and resumes the turn loop
`src/phonesys/call.py:91`.

`except (asyncio.TimeoutError, asyncio.CancelledError):` does not re-raise. The
justifying comment covers the timeout case only; `CancelledError` was bundled in
without discussion. Swallowing it at an await point loses the cancellation
permanently, and control falls to the top of the `while` loop, which issues a
new `POST /channels/{id}/record`.

Reproduced twice, including with a real SIGINT to a real `asyncio.run`: Ctrl-C
during playback logs "playback ... never finished", fires a `record` POST *after*
the operator asked the process to stop, then blocks the full
`max_turn_seconds + RECORDING_GRACE_SECONDS` (40s with shipped config) because
main()'s event consumer is already gone. Exit at +12.51s with
`max_turn_seconds=2`. At merge-base the same Ctrl-C landed in the recording wait
at `:132`, which `break`s, so the task ended promptly. The pre-existing handler
at `:132` bundles the same two exceptions but `break`s — safe; the new one
`continue`s.

Again minor in blast radius (operator ergonomics, one stray recording, an unread
`.wav`), but it is a **new behaviour change on 7000** introduced by this branch
and the fix is two lines.

**Fix:** catch `TimeoutError` alone; move `self._playbacks.discard(playback_id)`
into the existing `finally` at `:96` so cancellation still deregisters the
waiter while propagating.

---

## 3. Deferred minors triage

Every item below is ruled. Nothing is left implicit.

| # | Item | Ruling | Reason |
|---|------|--------|--------|
| 1 | `filler.py` `prepare()` logs no exception detail — an ElevenLabs outage gives indistinguishable log lines | **Fix now** | One-line change (`logger.warning("...: %s", exc)`). Filler is the feature with the largest silent-failure surface and the least live exposure; the first live call that comes up mute needs a diagnosable log. Cheapest possible insurance on the thing we are least sure of. |
| 2 | `last_tier` returns 0 for a fully empty bank; contract is "check `is_empty()` first", undocumented and untested | **Fix later** | Add a docstring line + a one-line test in the follow-up. No caller violates the contract today (`main.py` only constructs the bank when `filler_phrases` is non-empty), so it cannot fire. Not worth blocking. |
| 3 | `test_config.py` has three near-identical self-contained YAML blobs instead of `CONFIG_YAML` + delta | **Accept permanently** | Self-contained fixtures are a legitimate style choice and arguably better for config parsing tests, where a shared base makes it hard to see what each case actually asserts. Not a defect. Revisit only if a fourth blob appears. |
| 4 | `test_stream_call.py`: a `reply_task` created and never awaited/cancelled (false-negative shape if `_reply` raised early) | **Fix now** | This is a test that can pass while asserting nothing, in the file that already has three unguarded mutants (M2/M3/M4). Given how much this review's confidence rests on `test_stream_call.py`, false-negative shapes there get closed while we are already in the file. One `await`/`assert not task.cancelled()`. |
| 5 | `test_stream_call.py`: a `reply_task.cancel()` after `wait_for` that is dead code | **Fix now** | Same trip, one-line deletion. Dead cancellation reads as coverage that does not exist — it is what made the cancellation gap in `_next_delta` look tested when it is not. |
| 6 | A non-`CancelledError` during the abandon unwind escapes as itself rather than `_Interrupted` | **Fix later** | Real inconsistency in the abandon drain, but it needs the generator's `athrow` to raise something other than the injected cancellation — i.e. a bug in Hermes' client generator or httpx teardown. No known trigger. Track with M1's Hermes error work, which touches the same region. |
| 7 | The tail flush has no abandon re-check; worst case ~20ms of stale audio | **Accept permanently** | 20ms is one frame, below perceptual threshold on a phone line, and the re-check would add a branch to the barge-in path — the highest-risk code in the branch — for no audible gain. Explicitly accepted, not deferred. |
| 8 | `credit_heard` logs "interrupted" on the abandon path, where the cause was a new utterance | **Fix later** | Misleading operator log on the exact path we will be reading logs for during live tuning, but it is a string. Roll into the same commit as item 1 if convenient; not blocking. |
| 9 | `test_a_final_transcript_during_synthesis` has the file's tightest wall-clock margin (~50ms) | **Accept permanently** | Measured invariant under 40 CPU burners on a 24-core host: every wait is timer-driven `asyncio.sleep`, not CPU-bound, so durations did not move by a millisecond. There is no fake-clock alternative — promptness *is* what the test asserts. One-sided false-FAIL only. Revisit only if it ever actually flakes. |
| 10 | A cough Deepgram transcribes as a word sets `final_seen`, truncating sentence 0 and being answered as an utterance; the cough guarantee is only for WORDLESS noise | **Fix later — documentation, this week** | Not a code fix: this is the honest boundary of the barge-in guarantee, and it is currently written nowhere. Update the design doc and `README`'s known-limitations list to say the guarantee is "wordless noise does not interrupt", not "coughs do not interrupt". Live testing (section 5, step 7) should measure how often Deepgram words a cough before anyone decides whether code needs to change. |

Also carried from the verified-findings list as **fix later**, since they were
downgraded to minor but are all one-liners in the same neighbourhood:

- `_next_delta` leaks the in-flight `anext` task on outer cancellation
  (`stream_call.py:911`) — cancel `nxt` and `await asyncio.wait({nxt})` in the
  `finally`, matching the drain the abandon path already does. Only reachable at
  Ctrl-C; cost is a misleading RuntimeError traceback at shutdown.
- Unguarded `ari.answer()` in main()'s streaming-rejection branch
  (`main.py:273`) — give `answer` the same 404 exemption `hangup` and
  `destroy_bridge` already have (`ari.py:44-48`, `:86-89`). Dead code in a
  healthy deployment, but it is one line and it removes a way for the event loop
  to die.
- ~~Startup TTS synthesis blocks reaching `ari.events()`~~ **Fixed** (see
  `_prepare_streaming_speech` in `main.py`, spawned from `_start_streaming`):
  filler and greeting synthesis now run in a background task instead of being
  awaited before `ari.events()`, exactly the "spawn it and let the handler
  pick up the bank when ready" option this item proposed. Measured 7000
  dispatch latency during that window dropped from ~1602ms to ~0.8ms.
- `greeting_frames == []` permanently disables the greeting (`main.py:160`) —
  normalize `[]` to `None` and log it, matching `filler.py:54-57`'s `if frames:`.
- `filler_phrases` as a flat list explodes into one phrase per character
  (`config.py:92`) — two-line `isinstance(tier, str)` rejection in `load_config`.
- `config.yaml:37` — delete `max_silence_seconds` / `max_turn_seconds` from the
  7001 block; both default in `config.py:83-84` and neither is read on the
  streaming path.
- Test-quality gaps that survived as minor and should be closed opportunistically:
  `test_recording_does_not_start_until_playback_finishes` cannot detect a
  register-after-play regression (have the fake assert the waiter is registered
  at `play()` time); the audio-duration term in `_speak`'s playback timeout is
  never exercised (one test with a long ulaw payload and a small patched grace);
  the latency latches (`stream_call.py:669`, `:588`) are asserted only as
  non-negative (bump `send_delay` and assert `reply_first_audio_ms` is far below
  total playout).

---

## 4. Residual risk at merge

Assuming M1-M6 are fixed and everything in section 3 marked "fix later" or
"accept" ships as-is.

**Knowingly unfixed, ranked by likelihood a live call exposes it:**

1. **Coughs and throat-clears that Deferred item 10 describes.** *A live call
   will absolutely expose this.* If Deepgram transcribes a cough as a word, it
   sets `final_seen`, truncates the reply's first sentence, and the "utterance"
   is answered — the agent responds to a cough. Shipped `barge_in_confirm_ms:
   300` makes the wordless case safe; the worded case is not defended anywhere.
   This is the single most likely thing to make 7001 feel broken to a human.
2. **Filler behaviour under real network timing.** Filler has never run against
   a real ElevenLabs latency profile or a real Hermes thinking window. The
   tests that would have caught total filler death do not exist (M3 fixes that
   in the suite, not in reality). Symptoms to expect: filler that never fires
   because Hermes is faster than `filler_delay_ms: 800`, filler that clips the
   first reply sentence, or filler that repeats too eagerly. All audible, none
   dangerous.
3. **Startup window after a deploy.** Previously ~3-5s where the Stasis app was
   unregistered and a 7000 caller was dropped by the dialplan, because
   `ari.events()` was not reached until filler/greeting synthesis finished.
   That ordering is fixed (see the deferred-items list above): synthesis now
   runs in a background task and `ari.events()` is reached immediately, so
   this window is now whatever config load, preflight checks, and the
   AudioSocket bind take -- milliseconds, not seconds. What remains, and is
   still self-healing: a 7000 caller dialing in the sub-second gap before the
   process finishes that startup work.
4. **Shutdown ergonomics.** With M6 fixed, Ctrl-C is clean on 7000. The
   `_next_delta` cancellation leak (fix later) still produces a spurious
   "streaming call ... failed" RuntimeError traceback at shutdown for any 7001
   call in the Hermes thinking window. Cosmetic; will confuse whoever reads the
   log first.
5. **`_next_delta` orphan + `hermes.aclose()` ordering at shutdown.** The
   orphaned `anext` task is cancelled in the same `_cancel_all_tasks` pass, so
   the "SSE response yanked out from under it" scenario does not materialize.
   Log noise only.
6. **Overlapping `append_assistant` with the next turn's `chat_stream` on the
   same Hermes session.** Already documented as an open decision in
   `HANDOFF.md:127-131` with three options; the `/v1/runs` migration removes it.
   Whether Hermes' `api_server` serialises per session is *not established* — a
   live call with a barge-in followed immediately by a new question is the way
   to find out. Ships as an accepted, owned risk, not an unknown.
7. **Second streaming extension.** Documented in `README.md:320-324` and
   `HANDOFF.md:222` as unsupported. `config.yaml` has exactly one. No risk in
   any supported configuration.
8. **Hermes error path (after M1).** Fixed in code but the fix will not have
   been exercised against a real 502. Low confidence-per-test-line; worth one
   deliberate live check (section 5, step 8).

**What a live phone call would actually expose, in order:** filler timing (2),
cough handling (1), Hermes error recovery (8 — only if you force it). Items 3-7
are operator-facing or need a deployment event, not a call.

---

## 5. What live verification must cover

Never been exercised end-to-end by a human. Run in this order — each step's
failure invalidates everything after it, so stop and fix rather than continuing.

**Precondition:** shipped `config.yaml` (7001, `filler_delay_ms: 800`, 3/2/2
phrases, `barge_in_confirm_ms: 300`), filler enabled, latency logging on, tail
the process log in a second terminal throughout.

1. **7000 non-regression, first and non-negotiable.** Dial 7000. Complete three
   full turns. Confirm: the agent never hangs up on you mid-reply (the bug the
   playback-completion work fixes), recording starts only *after* playback
   finishes, and `media/sounds/` is empty when the call ends. Then hang up
   mid-reply and confirm the log says "caller hung up", not a traceback, and
   `media/sounds/` is still empty (this is M5's live check). **If any of this
   fails, stop — do not proceed to 7001. The branch does not merge.**
2. **7000 silence policy.** Dial 7000, say nothing for four consecutive turns.
   Confirm you get "Are you still there?" at the fourth silent turn, not
   earlier, and that the call does not drop before it.
3. **7001 answers at all.** Dial 7001. Confirm the pre-synthesized greeting plays
   promptly and is not truncated or silent (this is the `greeting_frames == []`
   degenerate case, live). Confirm the AudioSocket leg is bridged — you can hear
   yourself being heard, i.e. the agent responds at all. **If the greeting is
   silent or the agent never responds, stop.**
4. **Basic turn-taking.** Three normal turns on 7001, ordinary pacing, no
   interruption. Confirm each reply is complete (not truncated after one
   sentence — that is the M2/M4 failure signature), and confirm turn end feels
   prompt (~1s after you stop, not 3s — 3s means UtteranceEnd is not arriving
   and you are living on the fallback). Check the log for
   `no UtteranceEnd after 3.0s; replying anyway`; it should not appear.
5. **Filler fires.** Ask a question that makes Hermes think for >800ms ("tell me
   a story about..."). Confirm you hear a filler phrase before the real reply,
   that it does not overlap the reply's first sentence, and that the phrase
   rotates on subsequent long turns rather than repeating identically. Check
   `filler_first_audio_ms` is non-None in the log. **This is the single thing
   the branch has never done in reality.**
6. **Barge-in, the core feature.** During a long reply, start talking. Confirm:
   audio stops within ~300ms (not after the current sentence completes), the
   agent's next reply reflects only what you actually heard (ask it "what did
   you just say?" to check the heard-tracking correction landed), and the reply
   after the barge-in is *complete* — not one frame. Repeat on turns 2, 3 and 4
   specifically: M2's failure only appears from turn 2 onward.
7. **Cough / wordless noise, the known boundary.** Mid-reply, cough once and say
   nothing. The reply should continue uninterrupted. Then cough several times
   in the thinking window right after asking a question. Note whether the reply
   gets abandoned and whether the log shows a final transcript containing a
   word. Record the result — this measures deferred item 10 and decides whether
   it needs code.
8. **Hermes error recovery (M1's live check).** Stop Hermes' `api_server`
   mid-call, ask a question, restart it. Confirm the agent apologizes aloud and
   the call *stays up*, then answers the next question normally. Do not skip
   this: M1 is the one shipped-behaviour fix in this list and it has only unit
   coverage.
9. **Barge-in immediately followed by a new question.** Interrupt, then ask a
   different question straight away with no pause. This is the overlapping
   `append_assistant` / `chat_stream` window (residual risk 6). Confirm the
   agent's answer is on-topic and the transcript is not visibly out of order.
10. **Clean shutdown.** With no call in progress, Ctrl-C. Confirm the process
    exits promptly and without a traceback. Then repeat with a 7000 call live:
    with M6 fixed, it should end within a second or two, not 40s, and issue no
    `record` POST after the interrupt.
