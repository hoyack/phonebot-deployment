# Atlas call-stall repair

## Failure mechanism

A transferred Atlas call could keep a foreground Hermes SSE request open while
performing tool work without producing assistant text. The HTTP read timeout is
an inactivity timeout: keepalives and tool events can keep it alive indefinitely.
The bridge's prior `_next_delta` waited for text or another caller utterance but
not the call-ended event or a wall-clock deadline. Additionally, transferred
profiles had no matching startup filler cache and spoke only one fallback phrase.

Do not expose raw `tool.progress` data to fix silence: some events contain private
reasoning. Only `assistant.delta` is conversational reply text.

## Intended behavior

- A bounded reply-generation wait independent of transport keepalives.
- Prompt stream cancellation when the caller disconnects or call time expires.
- Correctly voiced recurring filler after transfer, stopped before reply audio
  and on interruption. Filler is a waiting indication, not proof of progress.
- A truthful timeout explanation and return to listening when the caller remains
  connected. Never promise background completion unless an actual durable job and
  delivery path have been established.
- Atlas answers quick status questions with a focused check and concise result;
  deeper investigation is an explicit choice. Telephone clarification is spoken,
  not a UI-only `clarify` dialog.

## Response-budget setting

`hermes_reply_timeout_seconds` defaults to **30.0**, must be finite and positive,
and is explicitly set to 30 for Atlas in the live configuration. It is a single
monotonic cutoff measured from reply entry, not reset by text or tool/keepalive
events. It is checked on delta waits and between sentences: a sentence already
playing may finish, and an already-ready end-of-stream is accepted as completion.
Thus this is not a promise that all playback ends precisely at 30 seconds. The
600-second overall call limit remains separate. The turn-based 7000 path does not
use this new setting.

The timeout message is: “Sorry, this is taking too long. I don't have a complete
answer yet. Would you like to ask something else?”

## Scope boundaries

Preserve existing routing, direct/transfer PIN authentication, the 7000 turn-based
path, STT forwarding, and one-writer audio pacing. This repair does not change SIP
trunks, Docker networking, model/provider choice, or speech endpointing thresholds.
The split transcription of “OpenClaw” remains possible; Atlas's prompt accounts
for adjacent fragments, but no acoustic/endpointer fix is claimed.

## Deployment checks

1. Save rollback copies of changed source/config and Atlas `SOUL.md` outside Git.
2. Run the offline suite; exclude `tests/live` explicitly.
3. Compare approved bridge source/tests/config byte-for-byte between the live
   checkout and this repository's `phonesys/` subtree. Do not copy `.env`, media,
   logs, private session history, or credentials.
4. Confirm `docker exec freepbx-docker-freepbx-1 asterisk -rx 'core show channels count'`
   reports no active calls before restarting the bridge and Atlas gateway.
5. Restart only `phonesys-bridge.service` and `hermes-atlas.service` under
   `systemctl --user`; verify service state, ARI `phonesys` registration, all
   extension routes, and the health endpoints on 8642/8643/8644.
6. A healthy gateway does not prove telephone audio. Perform the checklist below.

## Caller acceptance checklist

- Dial Mary on 7001, request Atlas, enter the existing PIN; confirm Atlas greeting.
- Ask “What is OpenClaw doing right now?” Expect a short answer or an explicit
  limitation instead of a multi-minute silent investigation.
- Ask a question requiring a longer check. While waiting, verify Atlas's voice
  provides periodic waiting speech; on the response budget, expect a truthful
  timeout message and the ability to ask another question.
- Interrupt filler and reply speech; verify no overlapping audio or stale answer.
- Hang up during a long check; correlate bridge and Atlas logs to confirm prompt
  stream closure/interruption. Cancellation is best-effort for an external tool
  already performing side effects, not transactional rollback.
- Verify wrong PIN still denies access and correct PIN still admits access.
- Regression: call 7000 and John on 7002; confirm normal conversation/routing.

## Rollback

Use the deployment report's backup directory to restore only modified files (not
an entire profile or PBX volume). Check for active calls, restart the bridge and
Atlas gateway as needed, then repeat control-plane health checks. Do not reset the
live Git worktree: it contains pre-existing uncommitted feature work.
