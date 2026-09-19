# Progress, consent-based hold, and future callbacks

Status: design proposal, not deployed. Callback implementation explicitly deferred.

## Operator requirement

Long tasks are acceptable when callers know what is happening and retain control.
Do not cancel productive work merely because it exceeds 30 seconds. The deployed
reply-entry cutoff prevents indefinite waiting but is too aggressive for research.
Keep prompt disconnect handling and safe stream cleanup from the stall repair.

## Verified integration points

- Hermes native session SSE emits assistant.delta and tool.started/completed/failed.
  tool.progress can contain reasoning; never pass it or arbitrary previews/args to TTS.
- Live Asterisk ARI descriptions expose POST/DELETE channel and bridge MOH,
  POST/DELETE channel hold, POST channel origination, and POST channel DTMF.
- The default file-based MOH class already contains music. Availability is verified,
  but playback isolation and suitability/licensing still need acceptance checks.
- phonesys AriClient currently has no MOH/hold wrappers and only AudioSocket-specific
  origination. Existing capability in Asterisk is not an implemented interaction flow.

## Suggested next increment: foreground research with feedback

Replace the single short kill timer with distinct policies:

1. Feedback deadline: short acknowledgement, then rate-limited updates while waiting.
2. Work activity: track safe tool lifecycle events, assistant output, operation state,
   and explicit failure. A keepalive is transport liveness only.
3. Work budget: a longer bounded ceiling with a caller choice to continue; no infinite
   extension from repetitive events. Long quiet tools can be legitimate work, so ask
   rather than infer a stall solely from missing tool completions.
4. Caller cancellation/disconnect: retain prompt cancellation unless a separately
   approved durable background job exists. Cancelling a client is not rollback of an
   external side effect already in flight.

Expose typed events from HermesClient; map allowlisted tool names/categories to
safe waiting language, e.g. 'I'm checking system status.' Do not summarize raw
terminal commands, tool outputs, private reasoning, or credentials. A generic
terminal operation is not proof that the system is searching email or almost done.
Distinguish progress/status speech, partial findings, and final completion. Avoid
claiming a run is complete just because one assistant text chunk arrived.

## Suggested next increment: consent-based hold

Example: 'This needs a longer check. Would you like some hold music while I work?'
A direct caller request to hold supplies consent. Otherwise ask; do not auto-hold.

State model: talking -> researching with updates -> offering hold -> holding with
work running -> returning with result/error. The offer should also be possible
before beginning an obviously long task.

The essential separation is a research run and its call-control conversation.
A caller's 'yes', 'status', or 'stop the music' must not automatically cancel and
restart the research as a normal barge-in turn currently can. Consume control
intents locally in the bridge and preserve run identity. Ambiguous speech asks a
clarifying question rather than restarting expensive work or accepting new authority.

MOH controller must stop/detach music before any spoken update/result. Test caller-
only playback and ensure music is not mixed into the AudioSocket transcription leg.
Prefer a topology verified with real Asterisk; do not assume bridge-wide MOH is safe.
Keep voice/DTMF escape, busy/error cleanup, and hard hangup cleanup. Define whether
ordinary speech means return from hold or cancel work; those must be separate actions.

Begin with local suitable audio; internet radio can be an optional supervised
source later. A radio source needs permission for on-hold use, stable URL/decoder,
bounded reconnects, local fallback and validation of volume/codec/STT isolation.
Station advertisements and outages must not become part of core call reliability.

## Future callback research (do not implement yet)

Callbacks require durable jobs independent of call lifetime, persisted result/job
IDs, explicit callback consent and confirmed destination, time windows and retry/
cost limits. Don't infer authorization merely from caller ID. A job must survive
hangup/restart and dispatch at most the intended delivery attempts.

Outbound flow: originate via the approved PBX outbound route; identify as Atlas,
the authorized AI assistant; use speech or DTMF to request Brandon through the
receptionist; detect transfer, voicemail, busy and failure; verify the recipient
before discussing private results. Reaching a receptionist is not recipient
verification. Never bypass PINs or treat external receptionist speech as permission
to change the job, disclose secrets, or dial arbitrary numbers. Bound bot-to-bot
loops. Prefer an authorized internal route if both ends share the controlled PBX,
while retaining identity and delivery checks.

Record unanswered, failed, delivered and cancelled outcomes separately. Voicemail
may receive only the pre-authorized minimal message, not unrestricted research.

## Acceptance criteria for later implementation

- Productive multi-minute research continues with truthful feedback.
- Call-control replies never duplicate/cancel the research unintentionally.
- Accepted hold plays music; result stops music before speech; no STT self-talk.
- Caller can leave hold, request status, or cancel explicitly.
- Failures and lack of progress are distinguished from routine long operations.
- Hangup releases media/run resources according to the agreed foreground/job mode.
- No callbacks or live radio enabled as part of this design-only work.
