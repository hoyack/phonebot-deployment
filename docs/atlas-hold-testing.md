# Atlas consent-based hold and long-running feedback

## Enabled scope

Only Atlas (7003) opts into the new call controller. Mary/phonebot (7000/7001)
and John (7002) retain their existing behavior. No PIN, SIP trunk, outbound route,
or callback functionality changes.

Atlas settings in `phonesys/config.yaml`:

```yaml
hermes_reply_timeout_seconds: 300
hold_enabled: true
hold_offer_seconds: 15
hold_feedback_seconds: 15
hold_moh_class: phonesys_hold
```

The overall 600-second call limit remains separate. Hold never extends either
limit. This replaces the aggressive 30-second Atlas research cutoff, not the
hangup/cancellation safeguards. Music setup and the real Asterisk isolation probe
are documented in `hold-music-operations.md`.

## Caller experience

During a pending reply, the bridge offers hold music after the configured waiting
interval, when no answer sentence is being delivered. It asks for yes/no consent.
A direct “put me on hold” request also supplies consent. A declined offer keeps
research running without music. While waiting, sanitized lifecycle updates are
spoken periodically; any music is stopped before speech and resumed if appropriate.
Intervals are scheduling targets, not precise wall-clock guarantees during speech.

The same pending Hermes stream remains active across local control utterances.
Controls are not sent as new agent turns. Recognized voice controls:

- `yes`, `yeah`, `sure`, `okay`: accept a preceding offer.
- `put me on hold`, `play hold music`: explicitly request music.
- `no`, `no thanks`, `stop the music`, `take me off hold`: continue without music.
- `status`, `what's the status`, `any update`: hear an update without restarting work.
- `cancel`, `cancel that`, `stop working`, `never mind`: cancel the current request.
- `stop`, `wait`, `maybe`, `hold on`: clarify which control the caller wants.

This initial version uses a narrow whole-utterance grammar, not arbitrary natural-
language command understanding. Other questions leave the hold controller and go
through normal conversation handling, cancelling the prior foreground run. Voice
controls are available only while a reply is pending; keypad shortcuts are not
implemented. Keep commands short for the first end-to-end test.

Partial assistant answers still play. If a local control interrupts a sentence,
that sentence may be replayed afterward so the answer is not lost. On final
completion, music stops and queued answer speech finishes. Failed/empty/truncated
streams and the work limit receive explicit spoken handling rather than indefinite
silence. Music-start failures fall back to spoken updates; inability to stop music
fails closed rather than speaking over it.

Progress uses typed `tool.started`, `tool.completed`, and `tool.failed` events,
mapped to safe stock language. Raw args, previews, outputs, and `tool.progress`
reasoning never reach speech. A finished operation is not claimed to mean the
whole research task is finished.

## Acceptance test

1. Call Mary, request Atlas, and use the existing PIN.
2. Ask a genuine research question that needs several checks. Confirm it continues
   beyond 30 seconds with spoken feedback instead of the prior timeout apology.
3. On the hold offer, say `yes`. Expect the supplied Tranquil Pads track.
4. Say `status`. Expect music to stop for speech and resume if work remains.
5. Say `stop the music`. Work should continue without a duplicate request.
6. Say `put me on hold` to resume music while research is pending.
7. Let the task finish. Verify music stops before the answer, no overlap, and no
   self-transcription of the digital music path.
8. Start another request and say `cancel that`; then ask a new question.
9. Hang up during pending work and confirm resource cleanup from logs.
10. Regression-test direct Atlas PIN denial/admission, John routing, and 7000.

Do not ask Atlas to promise callbacks: foreground work still belongs to the live
call. Explicit durable-job/callback support remains research-only.

## Verification boundary

Offline tests cover the controller, parser, stream protocol, handling integration,
resource cleanup, and preservation of the research run across controls. An isolated
real Asterisk probe proves caller-channel music does not digitally leak into the
STT leg and stops cleanly. Human-call speech recognition, acoustic echo, natural
pacing, and actual research completion still require the operator's acceptance test.
Cancellation is cooperative and does not undo side effects of tools already running.
