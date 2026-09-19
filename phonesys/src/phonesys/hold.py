"""Opt-in foreground work/consent controller; one stream and one audio writer.

The STT pump remains owned by handle(). Controls consume complete utterances
locally; unrecognized utterances remain banked for the normal conversation loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import deque

import httpx

from phonesys.hermes import HermesError, HermesEvent
from phonesys.stream_tts import SentenceBuffer, TtsStreamError

logger = logging.getLogger(__name__)

OFFER = ('This is taking a little longer. Would you like hold music while I work? '
         'Say yes or no. While music is playing, say Atlas, take me off hold, '
         'or stop music at any time; say status for an update or cancel to stop working.')
STATUS = "I'm still working on your request. I don't have a complete answer yet."


def control_intent(text: str, *, offered: bool) -> str | None:
    """Narrow whole-utterance grammar: never mistake a new question for consent."""
    words = re.sub(r"[^a-z0-9\s]", "", text.lower())
    words = " ".join(w for w in words.split() if w != "please")
    if words in ('atlas', 'hey atlas', 'hello atlas', 'atlas are you there',
                 'im back', 'i am back', 'come back'):
        return 'attention'
    for prefix in ('hey atlas ', 'hello atlas ', 'atlas '):
        if words.startswith(prefix):
            words = words[len(prefix):]
            break
    if words in ('yes', 'yeah', 'yep', 'sure', 'okay', 'ok'):
        return 'hold' if offered else 'clarify'
    if words in ('put me on hold', 'hold music', 'play hold music', 'play music',
                 'put me back on hold', 'back on hold', 'return me to hold',
                 'can you put me on hold', 'id like hold music'):
        return 'hold'
    if words in ('no', 'no thanks', 'no thank you', 'stop the music', 'stop music',
                 'stop hold music', 'take me off hold', 'get me off hold',
                 'come off hold', 'turn off the music', 'turn off hold music'):
        return 'quiet'
    if words in ('status', 'what is the status', 'whats the status', 'any update',
                 'are you still there', 'hows it going', 'what are you doing'):
        return 'status'
    # Short acknowledgements commonly leak through STT while MOH is active
    # (or are spoken simply to signal that the caller is still listening).
    # They are not a new request and must never cancel/restart the in-flight
    # Hermes turn. Keep this deliberately narrow so a substantive utterance
    # still reaches the normal conversation loop as a real barge-in.
    if words in ('right', 'uh huh', 'uhuh', 'mhm', 'mm hmm', 'got it'):
        return 'backchannel'
    if words in ('cancel', 'cancel that', 'cancel the request', 'stop working',
                 'never mind', 'nevermind'):
        return 'cancel'
    if words in ('hang on', 'hold on', 'stop', 'wait'):
        return 'pause'
    if words in ('maybe', 'what', 'huh'):
        return 'clarify'
    return None


def now() -> float:
    """Monotonic policy clock, separable from scheduler time in offline tests."""
    return asyncio.get_running_loop().time()


async def run_hold_reply(*, audio, ext, hermes, session_id, utterance, ari,
                         channel_id, ended, deadline, read_utterance, consume,
                         tts, tracker, local_reply='', interrupt=None, final_seen=None,
                         t0=None, delivery_notes=None, suppress_ack=False,
                         caller_has_floor=lambda: False):
    """Keep the pending generator alive while the caller chooses music."""
    cutoff = min(deadline, now() + ext.hermes_reply_timeout_seconds)
    clock = asyncio.get_running_loop().time
    started_at = clock() if t0 is None else t0
    first_frames: dict[str, float | None] = {'feedback': None, 'reply': None}
    # The caller just reclaimed the floor: do not reward a replacement request
    # with another immediate copy of the same filler. Timed hold feedback and
    # explicit status remain intact, as does the first ordinary-turn ack.
    ack_due = (now() + ext.filler_delay_ms / 1000
               if ext.filler_delay_ms > 0 and not suppress_ack else float('inf'))
    interrupt = interrupt if interrupt is not None else asyncio.Event()
    final_seen = final_seen if final_seen is not None else asyncio.Event()
    interrupted_at = None
    feedback = now() + ext.hold_offer_seconds
    offered = False
    holding = False
    paused = False
    music = False
    buf = SentenceBuffer()
    progress = STATUS
    has_text = False
    generated_text = False
    typed = not local_reply and hasattr(hermes, "chat_events")
    finished = False
    sentences = deque()
    answer_frames = None
    spoken = []
    playback_completed = False
    discarded_text = False
    stream_factory = getattr(hermes, "chat_events", hermes.chat_stream)
    async def local_stream():
        # Use the same interruptible, deadline-bounded audio path without
        # starting an agent run for a known-incomplete request.
        yield local_reply

    stream = local_stream() if local_reply else stream_factory(session_id, utterance)
    # Read generation independently of paced speech, with bounded lookahead.
    # A finite queue also prevents a hot stream from monopolizing the loop.
    events = asyncio.Queue(maxsize=64)

    def diagnostic(event, outcome, **metrics):
        # Only internal enums and bounded identifiers; never speech, tool
        # arguments/results, exception text, config objects, or PINs.
        def identifier(value):
            return value if re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value) else 'redacted'
        fields = dict(event=event, outcome=outcome,
                      call_id=identifier(channel_id), session_id=identifier(session_id), **metrics)
        logger.info('event=%s call=%s session=%s outcome=%s metrics=%s', event,
                    fields['call_id'], fields['session_id'], outcome, metrics, extra=fields)

    async def collect_events():
        nonlocal generated_text
        try:
            while not ended.is_set() and now() < cutoff:
                try:
                    event = await anext(stream)
                except StopAsyncIteration as exc:
                    await events.put(exc)
                    return
                if ((isinstance(event, str) and event) or
                        (isinstance(event, HermesEvent) and event.kind == 'assistant.delta' and event.text)):
                    generated_text = True
                await events.put(event)
                if isinstance(event, HermesEvent) and event.kind == 'done':
                    return
                await asyncio.sleep(0)
        except Exception as exc:
            await events.put(exc)

    producer = asyncio.create_task(collect_events())
    nxt = asyncio.create_task(events.get())
    gone = asyncio.create_task(ended.wait())
    interrupted = asyncio.create_task(interrupt.wait())

    def finish_stream(*, incomplete=False):
        nonlocal finished
        finished = True
        diagnostic('hold.result', 'incomplete' if incomplete else
                   ('generated' if has_text else 'empty'))
        tail = buf.flush()
        if tail:
            sentences.append(tail)
        if not has_text or incomplete:
            sentences.append("Sorry, the request ended before I could give a complete answer. Please try again.")

    async def close_run():
        for task in (nxt, producer):
            if not task.done():
                task.cancel()
        await asyncio.gather(nxt, producer, return_exceptions=True)
        await stream.aclose()

    async def start_music():
        request = asyncio.create_task(ari.start_moh(channel_id, ext.hold_moh_class))
        try:
            done, _ = await asyncio.wait({request, gone}, timeout=max(0, min(
                2.0, cutoff - now())), return_when=asyncio.FIRST_COMPLETED)
            if ended.is_set() or now() >= cutoff:
                return
            if request not in done:
                raise TimeoutError("MOH start timed out")
            request.result()
            diagnostic('hold.transition', 'music_started')
        finally:
            if not request.done():
                request.cancel()
            async def drain_request():
                await asyncio.gather(request, return_exceptions=True)
            await _finish(drain_request())

    async def stop_music():
        nonlocal music
        if music:
            for attempt in range(2):
                try:
                    await asyncio.wait_for(ari.stop_moh(channel_id), timeout=2.0)
                    music = False
                    diagnostic('hold.transition', 'music_stopped')
                    return
                except Exception:
                    diagnostic('hold.transition', 'music_stop_failed' if attempt else 'music_stop_retry')
                    if attempt:
                        raise

    async def speech(text, *, answer=False):
        nonlocal answer_frames
        if ended.is_set() or now() >= deadline or caller_has_floor():
            return False
        await stop_music()
        sent = 0
        def on_sent(_):
            nonlocal sent
            sent += 1
            if answer:
                tracker.record_sent(len(spoken) - 1)
            kind = 'reply' if answer else 'feedback'
            if first_frames[kind] is None:
                first_frames[kind] = clock()
        sending = answer and answer_frames is not None
        work = asyncio.create_task(
            audio.send_frames(answer_frames, on_sent=on_sent) if sending else
            tts.synthesize_frames(text, ext.voice_id, 0))
        try:
            while True:
                await asyncio.wait({work, gone, interrupted}, timeout=max(0, min(
                    0.05, deadline - now())),
                    return_when=asyncio.FIRST_COMPLETED)
                if work.done() and sending:
                    work.result()
                    return True
                if ended.is_set() or now() >= deadline or interrupt.is_set() or read_utterance():
                    return False
                if not sending and caller_has_floor():
                    return False
                if work.done():
                    result = work.result()
                    if answer:
                        answer_frames = result
                        tracker.expect(len(spoken), len(result))
                        spoken.append(text)
                    work = asyncio.create_task(audio.send_frames(result, on_sent=on_sent))
                    sending = True
        finally:
            async def finish_speech():
                stop_at = None
                confirmed_at = getattr(interrupt, 'confirmed_at', None)
                if not work.done():
                    if sending:
                        stop_at = clock()
                        audio.stop_sending()
                        # Do not cancel a paced frame mid-write.
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(work, 2.0)
                    else:
                        work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                if stop_at is not None and interrupt.is_set():
                    drained_at = clock()
                    diagnostic('hold.interrupt', 'sender_drained',
                               confirmed_at=confirmed_at, stop_requested_at=stop_at,
                               sender_drained_at=drained_at,
                               interrupt_to_stop_ms=None if confirmed_at is None else (stop_at-confirmed_at)*1000,
                               interrupt_to_drained_ms=None if confirmed_at is None else (drained_at-confirmed_at)*1000)
            await _finish(finish_speech())
            if answer and answer_frames is not None:
                # Retain only the unsent audio; a local control must never
                # restart a sentence the caller already partially received.
                answer_frames = answer_frames[sent:]

    try:
        while not ended.is_set() and now() < deadline:
            if caller_has_floor():
                # Cancel outright rather than merely deferring: a due ack that
                # survives the floor claim would fire the instant it clears,
                # as if the caller's interruption had never happened.
                ack_due = float('inf')
            text = read_utterance()
            if interrupt.is_set() and not text:
                # Acoustic stopping and semantic replacement are separate.
                # Keep finals banked until endpoint/fallback; never consume a
                # fragment merely because it stopped outbound audio.
                if interrupted_at is None:
                    interrupted_at = now()
                if now() - interrupted_at >= 3.0 and not final_seen.is_set():
                    interrupt.clear()  # confirmed noise with no final
                    interrupted = asyncio.create_task(interrupt.wait())
                    interrupted_at = None
                else:
                    if not finished and now() >= cutoff:
                        diagnostic('hold.timeout', 'reply_deadline' if cutoff < deadline else 'call_deadline')
                        break
                    await asyncio.wait({gone}, timeout=0.01)
                    continue
            if text:
                interrupt.clear()
                interrupted.cancel()
                await asyncio.gather(interrupted, return_exceptions=True)
                interrupted = asyncio.create_task(interrupt.wait())
                interrupted_at = None
                intent = control_intent(text, offered=offered)
                diagnostic('hold.control', intent or 'new_request')
                if intent != 'backchannel':
                    paused = False
                if intent == 'hold':
                    consume()
                    holding = True
                    offered = True
                elif intent == 'pause':
                    consume()
                    paused = True
                    holding = False
                    await stop_music()
                elif intent == 'quiet':
                    consume()
                    holding = False
                    offered = True
                    await speech("Okay, I've stopped the hold music. I'll keep working quietly.")
                elif intent == 'attention':
                    consume()
                    holding = False
                    offered = True
                    await speech("I'm here. I've stopped the hold music. What do you need?")
                elif intent == 'status':
                    consume()
                    await speech(progress)
                elif intent == 'backchannel':
                    consume()
                    # Preserve both the current run and active hold music.
                    # No acknowledgement is spoken: that would itself tear
                    # down MOH and defeat the caller's explicit hold choice.
                    continue
                elif intent == 'cancel':
                    consume()
                    await close_run()
                    await speech("Okay, I've requested cancellation of this request.")
                    return
                elif intent == 'clarify':
                    consume()
                    offered = True
                    await speech("Would you like hold music? Say yes or no, status for an update, or cancel to stop working.")
                else:
                    return
            # Tentative speech gates NEW output even before barge-in is
            # confirmed. Existing playback still uses the confirmed signal.
            # No-final noise expires in the owner's bounded floor callback.
            if caller_has_floor():
                if not finished and now() >= cutoff:
                    diagnostic('hold.timeout', 'reply_deadline' if cutoff < deadline else 'call_deadline')
                    break
                await asyncio.wait({gone}, timeout=0.01)
                continue
            if not finished and nxt.done():
                ready = [nxt.result()]
                ready.extend(events.get_nowait() for _ in range(events.qsize()))
                for delta in ready:
                    if isinstance(delta, StopAsyncIteration):
                        finish_stream(incomplete=typed)
                        break
                    if isinstance(delta, Exception):
                        raise delta
                    if isinstance(delta, HermesEvent):
                        if delta.kind == "done":
                            finish_stream()
                            break
                        elif delta.kind.startswith("tool."):
                            progress = delta.text
                            # While the caller is on hold, assistant text that
                            # precedes another tool call is narration, not the
                            # final answer. Keep MOH continuous and discard that
                            # superseded narration. The assistant text emitted
                            # after the last tool event is retained and played
                            # only when Hermes sends the terminal `done` event.
                            if holding:
                                discarded_text = discarded_text or has_text
                                buf = SentenceBuffer()
                                sentences.clear()
                                answer_frames = None
                                has_text = False
                        delta = delta.text if delta.kind == "assistant.delta" else ""
                    if delta:
                        has_text = True
                        sentences.extend(buf.feed(delta))
                if not finished:
                    nxt = asyncio.create_task(events.get())
            # Ready completion wins over the generation cutoff. Completed
            # playback belongs to the call deadline, not the work budget.
            # Only a bounded snapshot is drained: an endless ready stream
            # cannot starve the cutoff or caller controls.
            if not finished and now() >= cutoff:
                diagnostic('hold.timeout', 'reply_deadline' if cutoff < deadline else 'call_deadline')
                break
            if paused:
                await asyncio.wait({gone}, timeout=0.01)
                continue
            if sentences and (not holding or finished):
                if await speech(sentences[0], answer=True):
                    sentences.popleft()
                    answer_frames = None
                continue
            if finished:
                playback_completed = True
                diagnostic('hold.result', 'playback_completed')
                return
            if now() >= ack_due:
                ack_due = float('inf')
                if first_frames['reply'] is None and not holding:
                    await speech("I'm working on your request.")
            if now() >= feedback:
                if not offered:
                    diagnostic('hold.offer', 'requested')
                    offered = True
                    await speech(OFFER)
                elif not holding:
                    # Quiet mode needs periodic reassurance; opted-in hold
                    # music does not. Progress is still available on demand
                    # through the explicit `status` control.
                    await speech(progress)
                feedback = now() + ext.hold_feedback_seconds
            if (holding and not music and not nxt.done()
                    and not ended.is_set() and now() < cutoff):
                # Mark uncertain before the request: a timed-out HTTP response
                # can still have enabled playback in Asterisk.
                music = True
                try:
                    await start_music()
                except Exception:
                    diagnostic('hold.transition', 'music_start_failed')
                    holding = False
                    await speech("Hold music is unavailable. I'll keep working and give spoken updates.")
            await asyncio.wait({nxt, gone}, timeout=max(0, min(
                0.05, feedback - now(), ack_due - now(), cutoff - now())),
                return_when=asyncio.FIRST_COMPLETED)
        if not ended.is_set() and now() < deadline:
            await close_run()
            if not paused:
                await speech("Sorry, the work time limit has been reached. I don't have a complete answer yet.")
    except TtsStreamError:
        diagnostic('hold.result', 'speech_error')
        if not ended.is_set():
            with contextlib.suppress(TtsStreamError, httpx.HTTPError):
                await speech("Sorry, I had trouble speaking the answer. Please try again.")
    except (HermesError, httpx.HTTPError):
        diagnostic('hold.result', 'hermes_error')
        if not ended.is_set():
            await speech("Sorry, the request failed before I could finish. Please try again.")
    finally:
        if now() >= deadline:
            diagnostic('hold.timeout', 'call_deadline')
        diagnostic('hold.latency', 'finished', **{
            f'{kind}_first_frame_at': value for kind, value in first_frames.items()
        }, **{
            f'{kind}_first_audio_ms': None if value is None else (value - started_at) * 1000
            for kind, value in first_frames.items()
        })
        async def cleanup():
            for task in (nxt, producer, gone, interrupted):
                if not task.done():
                    task.cancel()
            await asyncio.gather(nxt, producer, gone, interrupted, return_exceptions=True)
            try:
                await stream.aclose()
            finally:
                await stop_music()
        await _finish(cleanup())
        heard = tracker.heard_text(spoken)
        if (delivery_notes is not None and not local_reply and generated_text
                and (not playback_completed or discarded_text or heard != ' '.join(spoken))):
            # Carry correction on the NEXT real request, never POST /chat
            # merely to fix history (that would start another agent run).
            delivery_notes.append(
                '[Bridge delivery note: The previous generated answer was not fully delivered. '
                'Only these complete sentences were sent: ' + (heard or '(none)') +
                ' Do not assume any other generated text was heard.]')


async def _finish(coro):
    """Drain cooperative cleanup even under repeated outer cancellation."""
    task = asyncio.create_task(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError
