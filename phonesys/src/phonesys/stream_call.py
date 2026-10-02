"""Streaming call handler with barge-in.

The only module that knows about interruption. Two facts shape everything
here, both established by live testing rather than guesswork:

1. Caller audio is forwarded to Deepgram in EVERY state, including while the
   agent is speaking. Its endpointing is VAD-based and only finalises an
   utterance while audio keeps arriving, so pausing the pump during playback
   would mean no final transcript ever comes back and the conversation would
   hang. Forwarding continuously is also what makes interruption detectable
   mid-sentence.

2. A LiveStt.send() timeout means the websocket is dead, not slow. Cancelling
   or retrying a send mid-write can leave a partial frame and corrupt every
   subsequent send, so the pump stops for good and the call winds down.

The other load-bearing idea is HeardTracker: after an interruption the agent
must be told only what the caller actually heard, counted in frames genuinely
written to the socket, never in text that was merely generated.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import secrets
import uuid as uuid_mod
from typing import Callable, Sequence

import httpx

from phonesys.ari import AriClient, AriError
from phonesys.audiosocket import AudioSocketServer, AudioSocketSession
from phonesys.config import ExtensionConfig
from phonesys.directory import DirectoryPerson, resolve_transfer
from phonesys.filler import FillerBank, Frames
from phonesys.hermes import HermesClient, HermesError
from phonesys.stream_stt import LiveStt
from phonesys.stream_tts import SentenceBuffer, StreamTts, TtsStreamError
from phonesys.tasks import spawn
from phonesys.waiters import DtmfCollector

logger = logging.getLogger(__name__)

# Asterisk has already been told to dial us; if the AudioSocket connection
# has not arrived by now something is wrong with the dialplan or the network
# path, and waiting longer only holds a dead call open.
AUDIOSOCKET_WAIT_TIMEOUT = 10.0

# After stop_sending() the writer stops within one frame interval. This bound
# only exists so a wedged socket cannot hold the reply loop open forever.
SENDER_DRAIN_TIMEOUT = 2.0

# How long teardown is allowed to keep running after handle() has been
# cancelled. Cancellation must not leave an ARI bridge and two channels up.
TEARDOWN_TIMEOUT = 5.0

# Deepgram is asked for utterance_end_ms=1000, so UtteranceEnd normally lands
# about a second after the last final transcript. This is the safety net for
# the case where it never arrives at all: without it a dropped UtteranceEnd
# would leave the agent mute for the rest of the call, with the caller's words
# accumulated and never answered. It is deliberately well above 1s so it
# cannot pre-empt a real UtteranceEnd and split one utterance in two.
UTTERANCE_END_FALLBACK = 3.0

# An endpoint is a pause, not permission to seize the floor after barge-in.
# Atlas gives a reclaimed floor two further seconds to assemble a follow-up.
# Renewed speech invalidates the endpoint; provider/PBX timers stay unchanged.
POST_BARGE_QUIET_SECONDS = 2.0

# Local cached-state checks, not additional native API polling. Notices are
# restrained even if the watcher/foreground loops return unusually quickly.
BACKGROUND_CHECK_SECONDS = 1.0
BACKGROUND_FEEDBACK_SECONDS = 15.0
BACKGROUND_QUIET_SECONDS = 3.0
BACKGROUND_PROGRESS_LIMIT = 4
BACKGROUND_PROGRESS = (
    "Some background work is still running. You can keep talking to me."
)

# Said aloud when transcription dies mid-call. The design's error table says
# to apologize and end rather than drop the line silently; a caller who hears
# nothing assumes the agent is thinking and keeps talking to a dead channel.
STT_FAILURE_MESSAGE = (
    "Sorry, I'm having trouble hearing you right now. Please call back. Goodbye."
)

# Said aloud when the agent's own reply stream dies. Unlike the STT failure
# above this does NOT end the call: the design's error table says "Hermes
# stream errors -> apologize aloud, return to LISTENING", because the usual
# cause is a restarted api_server or a proxy 502 that the next request would
# survive. Short on purpose -- the caller is about to be asked to repeat.
HERMES_FAILURE_MESSAGE = (
    "Sorry, I lost my train of thought there. Could you say that again?"
)


HERMES_TIMEOUT_MESSAGE = (
    "Sorry, this is taking too long. I don't have a complete answer yet. "
    "Would you like to ask something else?"
)


class _ReplyTimeout(Exception):
    """The reply's wall-clock generation budget expired."""


class SpeechBurst:
    """Time-based confirmation that the caller is really talking over us.

    Deepgram emits interim transcripts at a rate nobody controls -- it depends
    on speech rate, model and network -- so counting interim EVENTS says
    nothing about how long the caller has been speaking. This measures wall
    clock instead: the first interim of a burst starts the window, and the
    first interim arriving at or after `confirm_ms` later confirms it.

    Consequences that matter:
      * Interims that stop before the window elapses never confirm. Nothing is
        scheduled, so a cough that produces one or two interims and then
        silence can never fire later.
      * Firing re-arms the window (`_start = None`). Deepgram does not
        reliably re-emit SpeechStarted per utterance, so a window left open
        would let a single later interim cut off the agent's next reply.
    """

    def __init__(self, confirm_ms: int, clock: Callable[[], float]) -> None:
        self._window = max(0.0, confirm_ms / 1000.0)
        self._clock = clock
        self._start: float | None = None

    def reset(self) -> None:
        self._start = None

    def interim(self) -> bool:
        """Record one interim transcript; True means barge-in is confirmed."""
        now = self._clock()
        if self._start is None:
            self._start = now
            # A zero/negative window means "interrupt on any speech at all";
            # without this the first interim of every burst is free.
            return self._window <= 0.0
        if now - self._start >= self._window:
            self._start = None
            return True
        return False


class InterruptSignal(asyncio.Event):
    """Latch the confirmed STT edge, not the later audio-waiter wakeup."""

    confirmed_at: float | None = None

    def set(self) -> None:
        if not self.is_set():
            self.confirmed_at = asyncio.get_running_loop().time()
        super().set()

    def clear(self) -> None:
        super().clear()
        self.confirmed_at = None


class HeardTracker:
    """Counts frames actually written, per sentence.

    A sentence counts as heard only when every one of its frames was written.
    Rounding down matters: crediting a half-spoken sentence makes the agent
    reference words the caller never received.
    """

    def __init__(self) -> None:
        self._expected: dict[int, int] = {}
        self._sent: dict[int, int] = {}

    def expect(self, index: int, frames: int) -> None:
        self._expected[index] = frames

    def record_sent(self, index: int) -> None:
        self._sent[index] = self._sent.get(index, 0) + 1

    def heard_text(self, sentences: list[str]) -> str:
        heard: list[str] = []
        for i, text in enumerate(sentences):
            expected = self._expected.get(i)
            # `not expected` covers both "never queued" and "queued zero
            # frames". Zero is reachable: _frames_from_ulaw drops a trailing
            # sub-20ms tail, so a very short sentence can yield no whole
            # frames at all -- nothing was played, so nothing was heard, and
            # `0 >= 0` must not be allowed to credit it.
            if not expected or self._sent.get(i, 0) < expected:
                # Stop at the first gap rather than skipping it: speech is
                # heard in order, so nothing after a cut-off sentence
                # reached the caller either.
                break
            heard.append(text)
        return " ".join(heard)


class _Interrupted(Exception):
    """The caller spoke over the agent."""


class _CallEnded(Exception):
    """The caller is gone; stop mid-reply without inventing a conversation."""


class _TimeLimit(Exception):
    """max_call_seconds expired while the agent was still speaking."""


async def _wait_any(events: Sequence[asyncio.Event], timeout: float) -> None:
    """Wait until any of `events` is set, or `timeout` elapses.

    The timeout is what enforces max_call_seconds: waiting on the interrupt
    event alone would block forever on a silent line.
    """
    waiters = [asyncio.create_task(ev.wait()) for ev in events]
    try:
        await asyncio.wait(waiters, timeout=None if timeout == float('inf') else timeout,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _quietly(what: str, coro) -> None:
    """Run a cleanup step without letting it skip the steps after it.

    A 404 from ARI means the channel is already gone because the caller hung
    up -- the normal end of a call, not a failure worth a stack trace.
    """
    try:
        await coro
    except AriError as exc:
        if exc.status_code == 404:
            logger.info("%s: channel already gone (caller hung up)", what)
        else:
            logger.warning("%s failed: %s", what, exc)
    except Exception:
        logger.exception("%s failed", what)


class StreamingCallHandler:
    def __init__(
        self,
        ari: AriClient,
        stt_factory: Callable[[], LiveStt],
        tts: StreamTts,
        hermes_factory: Callable[[ExtensionConfig], HermesClient],
        audiosocket_server: AudioSocketServer,
        ari_app: str,
        filler_bank: FillerBank | None = None,
        greeting_frames: Frames | None = None,
        extensions: dict[str, ExtensionConfig] | None = None,
        filler_profile: str | None = None,
        dtmf: DtmfCollector | None = None,
    ) -> None:
        self._ari = ari
        self._stt_factory = stt_factory
        self._tts = tts
        self._hermes_factory = hermes_factory
        self._server = audiosocket_server
        # No audiosocket_host/audiosocket_port constructor args: the address
        # Asterisk is told to dial back to is per-extension (ext.audiosocket_
        # host/port, read fresh in handle() below), not a value fixed for
        # every call this handler ever processes. A constructor-level copy
        # went stale the moment config.yaml disagreed with it -- see task-9
        # fix round 1.
        # Passed in rather than read off the ARI client's private config, so
        # the dependency is visible in the constructor.
        self._ari_app = ari_app
        # None, or an empty bank, means no filler -- exactly the behaviour
        # this handler had before filler existed. Playback itself is Task 4's
        # job; this constructor only stores the reference.
        self._filler_bank = filler_bank
        self._filler_profile = filler_profile
        self._dtmf = dtmf
        # Pre-synthesized once at startup (main.py's _start_streaming,
        # alongside the filler phrases) because ext.greeting is itself a
        # frozen-dataclass config constant (config.py's ExtensionConfig.
        # greeting) whose ElevenLabs round trip costs the same 400-600ms as
        # every filler phrase (see filler.py's module docstring) -- paid on
        # every single call otherwise, with the caller already on the line.
        # None means startup synthesis never ran or failed; _speak() below
        # falls back to synthesizing it fresh for that call, exactly as it
        # did before this cache existed -- a cache failure must degrade, not
        # take the extension down.
        #
        # Sharing this one list across every concurrent call this handler
        # serves is safe: send_frames (audiosocket.py) only iterates it and
        # never mutates it, and each frame's payload is an immutable `bytes`
        # slice, so no call can observe or corrupt another call's read of the
        # same cached list. FillerBank's cached frames are shared the same
        # way, for the same reason.
        self._greeting_frames = greeting_frames
        self._extensions = extensions or {}
        self._directory = tuple(
            DirectoryPerson(number, candidate.full_name)
            for number, candidate in self._extensions.items()
            if candidate.directory_listing and candidate.full_name
        )

    def set_greeting_frames(self, frames: Frames) -> None:
        """Install the startup-synthesized greeting after construction.

        The pre-synthesis this caches is no longer awaited before the ARI
        event loop starts (main.py's _prepare_streaming_speech): making the
        already-live turn-based extension wait out an ElevenLabs round trip
        it does not use is not a trade this feature gets to make. So the
        handler is built with no greeting and the frames arrive later, while
        it may already be serving calls.

        A setter rather than a constructor value because the constructor
        takes a SNAPSHOT: a call already in flight read `greeting_frames`
        into the handler at build time, and there is no way to hand it a
        value afterwards. FillerBank needs no equivalent -- prepare()
        mutates the bank the handler already holds a reference to, so late
        filler is picked up for free -- and this method gives the greeting
        the same shape: mutate the object the handler already holds, which
        here is the handler itself.

        Assigning from a background task is safe for the same reason the
        list is safe to share across calls: one event loop thread, and this
        rebinds the attribute rather than mutating a list some call is
        mid-way through sending. A call that started before this lands read
        the old value (None) and falls back to per-call synthesis, exactly
        as it did before the cache existed.
        """
        self._greeting_frames = frames

    async def _authenticate_pin(
        self,
        audio: AudioSocketSession,
        speaking_ext: ExtensionConfig,
        target_ext: ExtensionConfig,
        channel_id: str,
    ) -> bool:
        """Authenticate a protected extension before Hermes sees the call.

        `speaking_ext` supplies the voice used for the prompt.  During a Mary
        transfer that keeps the receptionist speaking until authentication
        succeeds; on a direct call it is the protected extension itself.
        PIN digits are deliberately never logged.
        """
        if not target_ext.access_pin:
            return True
        # Drop pre-auth backlog and suppress the entire prompt/collection/
        # retry window, not merely the tones. This also protects transfers
        # while their existing STT pump is running. Require the boundary:
        # an unsupported session adapter must fail closed, never bypass it.
        with audio.suppress_inbound_audio():
            return await self._collect_pin(audio, speaking_ext, target_ext, channel_id)

    async def _collect_pin(
        self,
        audio: AudioSocketSession,
        speaking_ext: ExtensionConfig,
        target_ext: ExtensionConfig,
        channel_id: str,
    ) -> bool:
        """Collect locally, with the caller holding the PCM privacy window."""
        if self._dtmf is None:
            logger.error(
                "call %s: PIN-protected extension %s has no DTMF collector",
                channel_id,
                target_ext.profile,
            )
            return False

        digit_count = len(target_ext.access_pin)
        logger.info(
            "call %s: starting PIN authentication for profile %s (%d digits)",
            channel_id,
            target_ext.profile,
            digit_count,
        )
        audio_listener = lambda pcm: self._dtmf.feed_audio(channel_id, pcm)
        add_listener = getattr(audio, "add_inbound_listener", None)
        remove_listener = getattr(audio, "remove_inbound_listener", None)
        if add_listener is not None:
            add_listener(audio_listener)
        try:
            for attempt in range(3):
                # Arm collection before speaking so callers may begin typing
                # during the prompt without losing the first digit.
                pin_task = asyncio.create_task(
                    self._dtmf.collect(channel_id, digit_count, timeout=15.0)
                )
                try:
                    await asyncio.sleep(0)
                    prompt = (
                        f"Please enter your {digit_count}-digit PIN using the "
                        "telephone keypad."
                        if attempt == 0 else
                        "That PIN was not correct. Please try again."
                    )
                    await self._speak(audio, speaking_ext, [prompt])
                    entered = await pin_task
                finally:
                    if not pin_task.done():
                        pin_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pin_task
                if entered is not None and secrets.compare_digest(
                    entered, target_ext.access_pin
                ):
                    logger.info(
                        "call %s: PIN authentication succeeded for profile %s",
                        channel_id,
                        target_ext.profile,
                    )
                    return True
        finally:
            if remove_listener is not None:
                remove_listener(audio_listener)

        logger.info(
            "call %s: PIN authentication failed for profile %s",
            channel_id,
            target_ext.profile,
        )
        return False

    async def handle(self, channel_id: str, extension: str,
                     ext: ExtensionConfig) -> None:
        hermes = self._hermes_factory(ext)
        stt = self._stt_factory()
        stt.call_id = channel_id
        session_id = f"call-{channel_id}"
        call_uuid = str(uuid_mod.uuid4())
        bridge_id: str | None = None
        as_channel: str | None = None
        # interrupt: the caller is speaking over the agent.
        # utterance_ready: the caller has finished a thought and is owed a reply.
        # final_seen: the burst that armed `interrupt` was backed by a real,
        # non-empty final transcript (a genuine word), not merely a
        # confirmed interim (which can be nothing but line noise). Deepgram
        # normally emits UtteranceEnd close to a second AFTER the final that
        # preceded it (endpointing=300 / utterance_end_ms=1000, see
        # stream_stt.py), so `interrupt` is very often final-backed well
        # before `utterance_ready` catches up -- see speak_sentence's guard.
        # ended: the call is over (caller gone, or STT died and the agent is
        # deaf for the rest of the call, which amounts to the same thing).
        interrupt = InterruptSignal()
        utterance_ready = asyncio.Event()
        final_seen = asyncio.Event()
        ended = asyncio.Event()
        # Which side died, so teardown can tell "caller hung up" (say nothing,
        # there is nobody there) from "transcription died" (apologize aloud).
        audio_ended = asyncio.Event()
        stt_ended = asyncio.Event()
        tasks: list[asyncio.Task] = []
        loop = asyncio.get_running_loop()

        logger.info("streaming call %s on extension %s", channel_id, extension)
        try:
            await self._ari.answer(channel_id)

            bridge_id = await self._ari.create_bridge()
            await self._ari.add_to_bridge(bridge_id, channel_id)
            as_channel = await self._ari.originate_audiosocket(
                ext.audiosocket_host, ext.audiosocket_port, call_uuid, self._ari_app
            )
            await self._ari.add_to_bridge(bridge_id, as_channel)
            audio = await self._server.wait_for_session(
                call_uuid, timeout=AUDIOSOCKET_WAIT_TIMEOUT
            )

            # RTP and AudioSocket are established before this point, but the
            # first outbound frames can still race the media path on a newly
            # answered PSTN call. Extensions may opt into a short settle
            # window so the first syllable of the greeting is not clipped.
            if ext.greeting_delay_ms:
                await asyncio.sleep(ext.greeting_delay_ms / 1000.0)

            # Authentication is deliberately before both the Hermes session
            # and STT connection.  A protected agent must receive no speech,
            # session context, or transcript until the PIN succeeds, whether
            # reached through Mary or dialled directly.
            if not await self._authenticate_pin(audio, ext, ext, channel_id):
                await self._speak(
                    audio,
                    ext,
                    ["I'm sorry, I couldn't verify that PIN. Goodbye."],
                )
                return

            await hermes.create_session(session_id)
            if ext.stt_keywords:
                stt.set_keywords(ext.stt_keywords)
            await stt.connect()
            # Serialize reconnect with sends; never cancel a frame mid-write.
            stt_lock = asyncio.Lock()

            def stream_ended(outcome):
                safe_id = channel_id if re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', channel_id) else 'redacted'
                fields = dict(event='call.stream_ended', call_id=safe_id,
                              outcome=outcome, audio_ended=audio_ended.is_set(),
                              stt_ended=stt_ended.is_set())
                logger.info('event=call.stream_ended call=%s outcome=%s audio_ended=%s stt_ended=%s',
                            safe_id, outcome, fields['audio_ended'], fields['stt_ended'], extra=fields)

            async def pump_audio() -> None:
                """Forward non-private inbound frames to STT in every call state.

                AudioSocketSession keeps authentication-window PCM local.
                Never pauses while the agent speaks (see module docstring),
                and never retries a send that timed out -- that means the
                websocket is dead, and a retry risks a corrupt half-frame.
                """
                outcome = 'audio_eof'
                try:
                    async for frame in audio.frames():
                        try:
                            async with stt_lock:
                                await stt.send(frame)
                        except asyncio.TimeoutError:
                            outcome = 'stt_send_timeout'
                            return
                        except Exception:
                            outcome = 'stt_send_error'
                            return
                except asyncio.CancelledError:
                    outcome = 'audio_cancelled'
                    raise
                except Exception:
                    outcome = 'audio_error'
                finally:
                    if outcome.startswith('stt_'):
                        stt_ended.set()
                    elif outcome != 'audio_cancelled':
                        audio_ended.set()
                    if outcome != 'audio_cancelled':
                        ended.set()
                    stream_ended(outcome)

            burst = SpeechBurst(ext.barge_in_confirm_ms, loop.time)
            # Finals accumulate here until Deepgram says the utterance is over.
            utterance_parts: list[str] = []
            last_final_at = 0.0
            # Last moment the caller was audibly making speech, as opposed to
            # having FINISHED saying something. `utterance_parts` only fills on
            # finals, and with endpointing=300 the first final of an utterance
            # lands roughly 0.5-1.5s after the caller starts talking -- so an
            # idle clock driven by finals alone cannot tell a caller who has
            # just started speaking from one who is silent, and hangs up
            # mid-word on anyone who pauses and then answers near the deadline.
            # 0.0 rather than loop.time(): before any speech there is no voice
            # to be newer than `idle_since`, and max() must not pick this.
            last_voice_at = 0.0
            reply_active = False
            floor_reclaimed = False
            floor_activity_at = 0.0
            floor_endpoint_at = 0.0
            background_interrupt = asyncio.Event()
            background_voice_at = 0.0

            def background_clear():
                return (not ended.is_set() and not utterance_parts
                        and not caller_has_floor()
                        and loop.time() - max(background_voice_at, floor_activity_at, idle_since)
                        >= BACKGROUND_QUIET_SECONDS)

            def pending_stalled():
                return (utterance_parts and not utterance_ready.is_set()
                        and loop.time() - max(last_final_at,
                            floor_activity_at if floor_reclaimed else 0)
                        >= UTTERANCE_END_FALLBACK)

            def read_pending():
                if not utterance_ready.is_set() and not pending_stalled():
                    return ""
                pending = " ".join(utterance_parts).strip()
                if floor_reclaimed and utterance_ready.is_set():
                    from phonesys.hold import control_intent
                    # Consent and explicit local controls retain their
                    # existing immediate endpoint behavior.
                    if (control_intent(pending, offered=True) is None
                            and loop.time() - max(floor_activity_at, floor_endpoint_at)
                            < POST_BARGE_QUIET_SECONDS):
                        return ""
                return pending

            def consume_pending():
                nonlocal floor_reclaimed
                floor_reclaimed = False
                utterance_parts.clear()
                utterance_ready.clear()
                interrupt.clear()
                final_seen.clear()

            def caller_has_floor():
                nonlocal floor_reclaimed
                if not floor_reclaimed:
                    return False
                if utterance_parts or (
                        loop.time() - floor_activity_at < UTTERANCE_END_FALLBACK):
                    return True
                floor_reclaimed = False
                return False

            async def pump_stt() -> None:
                nonlocal last_final_at, last_voice_at, background_voice_at
                nonlocal floor_reclaimed, floor_activity_at, floor_endpoint_at
                cancelled = False
                outcome = 'stt_eof'
                try:
                    async for ev in stt.events():
                        if ((ext.native_work or ext.idle_presence_grace_seconds)
                                and ev.kind in ('speech_started', 'transcript')):
                            # Proactive audio yields even before interim confirmation.
                            # This does not alter the foreground barge/floor policy.
                            background_interrupt.set()
                            background_voice_at = loop.time()
                        if (ext.profile == 'atlas' and ext.hold_enabled
                                and ev.kind in ('speech_started', 'transcript')):
                            floor_activity_at = loop.time()
                            if reply_active:
                                floor_reclaimed = True
                            if floor_reclaimed:
                                # A new interim can precede its final by seconds.
                                # Never hand off banked finals under a stale end.
                                utterance_ready.clear()
                        if ev.kind == 'utterance_end':
                            floor_endpoint_at = loop.time()
                        if ev.kind == "speech_started":
                            burst.reset()
                            # Deepgram's VAD says there is a voice on the line
                            # right now. Nothing is transcribed yet, and that
                            # is exactly the window this exists to cover.
                            last_voice_at = loop.time()
                        elif ev.kind == "transcript":
                            if not ev.is_final:
                                # Interim results stand in for "still
                                # talking": waiting for a final would let the
                                # agent talk over the caller for a second or
                                # more. Confirmation is measured in wall
                                # clock, not in interim events -- see
                                # SpeechBurst.
                                if burst.interim():
                                    # Stamped on CONFIRMED interims only, for
                                    # the same reason barge-in is: a single
                                    # interim can be line noise, and letting
                                    # noise refresh the idle clock would put
                                    # the ten-minute hold straight back. A
                                    # confirmed burst is `barge_in_confirm_ms`
                                    # of continuous speech measured in wall
                                    # clock -- see SpeechBurst.
                                    last_voice_at = loop.time()
                                    interrupt.set()
                            else:
                                # A final stops the agent immediately (this is
                                # what makes barge-in work) but does NOT start
                                # a reply: with endpointing=300 Deepgram
                                # finalises at every ~300ms pause, so replying
                                # here means answering half a sentence and
                                # talking over the caller. The text is banked
                                # until UtteranceEnd.
                                burst.reset()
                                utterance_parts.append(ev.text)
                                last_final_at = loop.time()
                                interrupt.set()
                                # Empty transcripts are dropped before this
                                # point (see stream_stt.py), so reaching here
                                # means Deepgram transcribed actual words --
                                # this burst is real speech, not a cough.
                                final_seen.set()
                        elif ev.kind == "utterance_end":
                            burst.reset()
                            # The guard is load-bearing, not defensive. This
                            # sets the ONE signal _next_delta races the SSE
                            # stream against, so any UtteranceEnd landing in
                            # the thinking window abandons the in-flight
                            # reply -- silently: no crash, no hangup, the
                            # caller's question simply never gets answered.
                            # `utterance_parts` non-empty is the only thing
                            # separating "the caller finished a new thought"
                            # from "an UtteranceEnd with nothing behind it",
                            # and the latter is reachable: the `stalled`
                            # fallback below replies before any UtteranceEnd
                            # has been seen AND clears utterance_parts, so
                            # the real one arrives late with parts empty --
                            # on exactly the turn already having
                            # turn-detection trouble. A noise burst that
                            # Deepgram finalises as an empty transcript does
                            # the same, since stream_stt.py drops empty
                            # finals before they are ever banked.
                            if utterance_parts:
                                utterance_ready.set()
                except asyncio.CancelledError:
                    cancelled = True
                    outcome = 'stt_cancelled'
                    raise
                except Exception:
                    # The transport logs safe close metadata. Never log its
                    # exception string, which may contain a raw close reason.
                    outcome = 'stt_error'
                finally:
                    if not cancelled:
                        stt_ended.set()
                        ended.set()
                    stream_ended(outcome)

            tasks = [asyncio.create_task(pump_audio()),
                     asyncio.create_task(pump_stt())]

            await self._speak(audio, ext, [ext.greeting],
                              cached=(self._greeting_frames
                                      if (not self._extensions or ext.receptionist)
                                      else None))

            limited_deadline = loop.time() + ext.max_call_seconds
            # Infinity is a comparison-only no-limit sentinel. All timers
            # passed to asyncio are finite or None; work budgets stay finite.
            deadline = (limited_deadline
                        if ext.call_limit_enabled else float('inf'))
            # Idle hangup. The clock measures CALLER SILENCE WHILE LISTENING,
            # so it is (re)started here, once the greeting has finished
            # playing, and again after every reply returns below. It therefore
            # cannot run while the agent is speaking, while filler is playing,
            # or while Hermes is generating -- all three happen inside _reply,
            # which this loop is blocked on, and none of them are the caller
            # being silent at us.
            #
            # 0 disables it (config.py's default), which is what every
            # extension that has not opted in gets.
            idle_seconds = ext.idle_hangup_seconds
            idle_since = loop.time()
            presence_at = None
            delivery_notes: list[str] = []
            background_next = loop.time() + BACKGROUND_FEEDBACK_SECONDS
            background_count = 0
            # Best-effort notices are attempted once, not delivery receipts.
            # Retrying failed native notices indefinitely would reintroduce an
            # unbounded idle exemption. Presence warning delivery is separate.
            background_notified: set[str] = set()
            background_uncertain: set[str] = set()

            def background_pending():
                if not ext.native_work:
                    return False
                waiting = hermes.work_waiting_keys()
                from phonesys.native_work import TERMINAL
                return any(key in waiting or key not in (
                           background_notified if state in TERMINAL else background_uncertain)
                           for key, state in hermes.work_statuses().items())

            while True:
                if presence_at is not None and last_voice_at >= presence_at:
                    presence_at = None
                idle_budget = (ext.idle_presence_grace_seconds
                               if presence_at is not None else idle_seconds)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.info("call %s hit max_call_seconds", channel_id)
                    break
                wait_for = remaining
                if ext.native_work:
                    wait_for = min(wait_for, BACKGROUND_CHECK_SECONDS)
                # The timeout handed to _wait_any must be the MINIMUM of every
                # deadline that applies, not just max_call_seconds: the loop
                # only re-evaluates when it wakes, so any deadline left out of
                # this minimum simply never fires (the shipped
                # max_call_seconds is 600s). That is the whole reason the
                # UtteranceEnd fallback below clamps it too.
                if idle_seconds and not utterance_parts and not background_pending():
                    # Skipped while finals are banked: the caller is mid-
                    # utterance, so they are not idle, and clamping to a
                    # deadline this loop is then forbidden to act on (see the
                    # matching guard on the check below) would spin.
                    #
                    # max(idle_since, last_voice_at), identical to the check
                    # below: the two must read the SAME clock or this wakes on
                    # a deadline the check refuses to act on and spins.
                    wait_for = min(wait_for, max(
                        0.0,
                        idle_budget
                        - (loop.time() - max(idle_since, last_voice_at)),
                    ))
                if utterance_parts and not utterance_ready.is_set():
                    # Wake up to apply the UtteranceEnd fallback rather than
                    # sleeping until max_call_seconds with words banked.
                    wait_for = min(wait_for, max(
                        0.0, UTTERANCE_END_FALLBACK - (loop.time() - last_final_at)
                    ))
                if caller_has_floor():
                    # A latched endpoint must not spin the listening loop or
                    # bypass floor settling when the previous work expired.
                    await _wait_any((interrupt, ended), timeout=min(0.02, remaining))
                else:
                    await _wait_any((interrupt, utterance_ready, ended), timeout=wait_for)
                if interrupt.is_set():
                    # The contract _reply is entered under: `interrupt` is
                    # CLEAR at entry. It is almost always set here -- the
                    # final transcript that produced this very utterance set
                    # it (see pump_stt above) -- and _reply's
                    # silence_filler_on_interrupt does nothing but `await
                    # interrupt.wait()` before silencing filler. Leaving it
                    # armed therefore kills filler on the first scheduling
                    # pass of every single reply, restoring exactly the
                    # 4-10s of dead air filler exists to remove, and spins
                    # this loop besides. speak_sentence's own `index == 0`
                    # guard cannot save it: that runs AFTER stop_filler().
                    interrupt.clear()
                stalled = pending_stalled()
                if stalled:
                    logger.warning("call %s: no UtteranceEnd after %ss; "
                                   "replying anyway", channel_id,
                                   UTTERANCE_END_FALLBACK)
                if read_pending():
                    utterance_ready.clear()
                    # Cleared here, not inside _reply: a stale set flag from
                    # the utterance just answered must not survive into the
                    # next reply's guard, the same reasoning that already
                    # applies to utterance_ready above.
                    #
                    # Every utterance is triggered by a final transcript, and
                    # pump_stt sets `final_seen` on that same final -- so
                    # without this the flag LATCHES on the first final of the
                    # call and speak_sentence's `not final_seen.is_set()` is
                    # False for the rest of it. The one-shot interrupt.clear()
                    # then never runs again, and any bare interim burst in a
                    # later thinking window cuts that reply to a single ~20ms
                    # frame -- effective silence, on every turn from then on.
                    final_seen.clear()
                    utterance = " ".join(utterance_parts).strip()
                    utterance_parts.clear()
                    if utterance:
                        logger.info("call %s heard: %r", channel_id, utterance)
                        if ext.receptionist:
                            resolution = resolve_transfer(utterance, self._directory)
                            if resolution.requested:
                                if len(resolution.matches) == 1:
                                    person = resolution.matches[0]
                                    target = self._extensions[person.extension]
                                    if not await self._authenticate_pin(
                                        audio, ext, target, channel_id
                                    ):
                                        await self._speak(
                                            audio, ext,
                                            ["I'm sorry, I couldn't verify that PIN. How else may I help you?"],
                                        )
                                        idle_since = loop.time()
                                        continue
                                    logger.info(
                                        "call %s transferring from %s to %s (%s)",
                                        channel_id, ext.full_name, person.full_name,
                                        person.extension,
                                    )
                                    await self._speak(
                                        audio, ext,
                                        [f"Certainly. I'll transfer you to {person.full_name}."],
                                    )
                                    await hermes.aclose()
                                    if target.stt_keywords != ext.stt_keywords:
                                        async with stt_lock:
                                            tasks[1].cancel()
                                            await asyncio.gather(tasks[1], return_exceptions=True)
                                            await stt.close()
                                            stt.set_keywords(target.stt_keywords)
                                            await stt.connect()
                                            tasks[1] = asyncio.create_task(pump_stt())
                                    ext = target
                                    deadline = (limited_deadline
                                                if ext.call_limit_enabled else float('inf'))
                                    hermes = self._hermes_factory(ext)
                                    session_id = f"call-{channel_id}-{person.extension}"
                                    await hermes.create_session(session_id)
                                    await self._speak(audio, ext, [ext.greeting])
                                    idle_seconds = ext.idle_hangup_seconds
                                    presence_at = None
                                    idle_since = loop.time()
                                    continue
                                if len(resolution.matches) > 1:
                                    names = ", or ".join(
                                        person.full_name for person in resolution.matches
                                    )
                                    await self._speak(
                                        audio, ext,
                                        [f"I found more than one match: {names}. Which person do you mean?"],
                                    )
                                    idle_since = loop.time()
                                    continue
                                await self._speak(
                                    audio, ext,
                                    ["I don't have anyone by that name in the directory. Who would you like?"],
                                )
                                idle_since = loop.time()
                                continue
                        # t0: utterance accepted -- the zero point every
                        # per-turn latency mark in _reply is measured from.
                        # Stamped here, unconditionally, for BOTH triggers:
                        # a normal utterance_end and the `stalled` fallback
                        # above. Stamping only the utterance_ready path would
                        # silently exclude every turn the fallback fires for
                        # -- exactly the turns most likely to be a
                        # turn-detection problem rather than a generation
                        # one.
                        t0 = loop.time()
                        suppress_ack = floor_reclaimed
                        floor_reclaimed = False
                        reply_active = True
                        await self._reply(audio, ext, hermes, session_id,
                                          utterance, interrupt=interrupt,
                                          utterance_ready=utterance_ready,
                                          final_seen=final_seen, ended=ended,
                                          deadline=deadline, t0=t0,
                                          channel_id=channel_id,
                                          read_utterance=read_pending,
                                          consume=consume_pending,
                                          suppress_ack=suppress_ack,
                                          caller_has_floor=caller_has_floor,
                                          delivery_notes=delivery_notes,
                                          trigger=("stalled" if stalled
                                                   else "utterance_ready"))
                        reply_active = False
                        presence_at = None
                        # Restart the idle clock the moment the bridge is
                        # listening again. Anchoring it to when the caller last
                        # SPOKE instead would charge the agent's own reply --
                        # generation plus filler plus speaking, seconds of it --
                        # to the caller's silence budget and hang up on someone
                        # who has not been given a chance to answer yet.
                        idle_since = loop.time()
                        continue
                if ended.is_set():
                    break
                # Only this listening loop owns proactive audio. It cannot run
                # during _reply (including its consent/music controller), so
                # there is never a second writer or a hold-policy bypass.
                if (ext.native_work and background_clear()
                        and loop.time() >= background_next):
                    from phonesys.native_work import TERMINAL
                    statuses = hermes.work_statuses()
                    waiting = hermes.work_waiting_keys()
                    ready = {key: state for key, state in statuses.items()
                             if state in TERMINAL and key not in background_notified}
                    uncertain = {key for key, state in statuses.items()
                                 if state not in TERMINAL and key not in waiting
                                 and key not in background_uncertain}
                    notice = ''
                    if ready or uncertain:
                        parts = []
                        if 'completed' in ready.values():
                            parts.append('Background results are ready for review.')
                        if any(state != 'completed' for state in ready.values()):
                            parts.append('Some background work did not complete.')
                        if uncertain:
                            parts.append("The background work time limit has passed. I can't confirm whether it stopped.")
                        notice = ' '.join(parts) + ' Ask me when you want to review it.'
                        background_notified.update(ready)
                        background_uncertain.update(uncertain)
                    elif (any(statuses[key] == 'running' for key in waiting)
                          and background_count < BACKGROUND_PROGRESS_LIMIT):
                        notice = BACKGROUND_PROGRESS
                        background_count += 1
                    if notice:
                        background_next = loop.time() + BACKGROUND_FEEDBACK_SECONDS
                        background_interrupt.clear()
                        await self._background_notice(
                            audio, ext, notice, interrupt=background_interrupt,
                            ended=ended, deadline=deadline,
                            allowed=lambda: background_clear() and (
                                bool(ready or uncertain) or any(
                                    hermes.work_statuses().get(key) == 'running'
                                    for key in hermes.work_waiting_keys())))
                        idle_since = loop.time()
                if idle_seconds and not utterance_parts and not caller_has_floor() and not background_pending() and \
                        loop.time() - max(idle_since, last_voice_at) >= idle_budget:
                    if ext.idle_presence_grace_seconds and presence_at is None:
                        background_interrupt.clear()
                        voice_before = last_voice_at
                        delivered = await self._background_notice(
                            audio, ext,
                            "Are you still there? If I don't hear from you, I'll end the call.",
                            interrupt=background_interrupt, ended=ended, deadline=deadline,
                            allowed=lambda: not ended.is_set() and not utterance_parts
                            and not caller_has_floor() and last_voice_at == voice_before)
                        idle_since = loop.time()
                        # Never count failed synthesis or interrupted speech as
                        # a delivered warning. Retry after a fresh idle interval.
                        if delivered and last_voice_at == voice_before:
                            presence_at = idle_since
                        continue
                    # Nothing from the caller since the agent stopped talking.
                    # `last_voice_at` as well as `idle_since` because a caller
                    # who goes quiet for nine seconds and then starts talking
                    # has an utterance in flight, not banked: no final has
                    # landed yet, so `utterance_parts` is still empty and
                    # `idle_since` alone would drop the line mid-word.
                    # Live evidence for why this exists: the agent said
                    # goodbye, the caller (reasonably) said nothing back, and
                    # the line stayed open for the full 600s max_call_seconds
                    # until they hung up by hand.
                    #
                    # break, not a hangup of its own: teardown is handle()'s
                    # `finally`, and a second path into it is a second thing to
                    # keep correct.
                    logger.info("call %s: no caller speech for %ss while "
                                "listening; ending the call",
                                channel_id, idle_seconds)
                    break

            if stt_ended.is_set() and not audio_ended.is_set():
                # Transcription died while the caller is still on the line.
                # Reconnecting is out of scope here; what must not happen is
                # the line going dead with no explanation.
                logger.warning("call %s: transcription stream ended; "
                               "apologizing and hanging up", channel_id)
                await _quietly("stt failure apology",
                               self._speak(audio, ext, [STT_FAILURE_MESSAGE]))
        except AriError as exc:
            if exc.status_code == 404:
                logger.info("streaming call %s: channel gone (caller hung up)",
                            channel_id)
            else:
                logger.exception("streaming call %s failed", channel_id)
        except Exception:
            logger.exception("streaming call %s failed", channel_id)
        finally:
            # Teardown runs as its own shielded task. If handle()'s task is
            # cancelled -- by an enclosing wait_for or TaskGroup, possibly
            # more than once -- an unshielded await here would raise on the
            # first cleanup step and abandon the rest, leaving the ARI bridge
            # and both channels up in Asterisk. Shielding means the steps run
            # to completion regardless; we just wait for them.
            # spawn(), not ensure_future(): on the detach path below the only
            # reference to this task is the stack frame we are about to leave,
            # and the event loop's own reference is weak -- so the "leaked
            # coroutine beats leaked Asterisk bridge" trade only holds if
            # something keeps it alive. The task registry does.
            teardown = spawn(self._teardown(
                stt=stt, hermes=hermes, bridge_id=bridge_id,
                as_channel=as_channel, channel_id=channel_id, pumps=tasks,
            ), name=f"teardown-{channel_id}")
            try:
                await asyncio.shield(teardown)
            except asyncio.CancelledError:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + TEARDOWN_TIMEOUT
                while not teardown.done() and loop.time() < deadline:
                    # Each repeat cancellation lands here and is swallowed;
                    # the loop is bounded by wall time, and it can only spin
                    # as fast as cancellations actually arrive.
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait(
                            {teardown},
                            timeout=max(0.0, deadline - loop.time()),
                        )
                if not teardown.done():
                    logger.warning("teardown of call %s unfinished after %ss",
                                   channel_id, TEARDOWN_TIMEOUT)
                raise

    async def _teardown(self, *, stt: LiveStt, hermes: HermesClient,
                        bridge_id: str | None, as_channel: str | None,
                        channel_id: str, pumps: list[asyncio.Task]) -> None:
        """Release everything, letting no failed step skip the ones after it."""
        try:
            for task in pumps:
                task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            await _quietly("stt close", stt.close())
            if bridge_id:
                await _quietly("destroy bridge", self._ari.destroy_bridge(bridge_id))
            if as_channel:
                await _quietly("hangup audiosocket channel",
                               self._ari.hangup(as_channel))
            await _quietly("hangup caller", self._ari.hangup(channel_id))
            await _quietly("hermes close", hermes.aclose())
        except Exception:
            logger.exception("teardown of call %s failed", channel_id)

    async def _background_notice(self, audio, ext, text, *, interrupt, ended,
                                 deadline, allowed) -> bool:
        """Local status audio only: no agent turn, history edit, or job control.

        One bounded attempt. A caller who interrupts a notice is not forced to
        hear it again. Drain frames already in flight rather than cutting a write.
        """
        loop = asyncio.get_running_loop()
        cutoff = min(deadline, loop.time() + 10.0)
        stop = asyncio.create_task(_wait_any((interrupt, ended),
                                            max(0.0, cutoff - loop.time())))
        synthesis = asyncio.create_task(self._tts.synthesize_frames(text, ext.voice_id, 0))
        sender = None
        try:
            done, _ = await asyncio.wait({stop, synthesis}, return_when=asyncio.FIRST_COMPLETED)
            if stop in done or interrupt.is_set() or not allowed():
                return False
            frames = synthesis.result()
            if not frames:
                return False
            sent = 0
            def on_sent(_):
                nonlocal sent
                sent += 1
            sender = asyncio.create_task(audio.send_frames(frames, on_sent=on_sent))
            await asyncio.wait({stop, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender.done():
                sender.result()
                return (sent == len(frames) and not interrupt.is_set()
                        and not ended.is_set() and allowed())
        except Exception:
            # Optional status audio cannot tear down a working call.
            logger.warning('Background notice unavailable; returning to listening')
        finally:
            stop.cancel()
            synthesis.cancel()
            await asyncio.gather(stop, synthesis, return_exceptions=True)
            if sender is not None:
                audio.stop_sending()
                await _drain_sender(sender)

        return False

    async def _speak(self, audio: AudioSocketSession, ext: ExtensionConfig,
                     sentences: list[str], *, cached: Frames | None = None) -> None:
        """Say something the agent initiates, such as the greeting.

        Deliberately not interruptible: the greeting is short and there is no
        reply history to correct. A caller who talks over it is still heard --
        the pump keeps running and the main loop answers their utterance as
        soon as the greeting finishes. The interrupt flag is left alone here
        precisely so that utterance is not swallowed.

        `cached`, if given, supplies pre-synthesized frames for sentence 0 --
        used for the greeting (see __init__'s greeting_frames and its call
        site in handle()), which is pre-synthesized once at startup rather
        than on every call. `_speak`'s other caller (the STT-failure apology)
        never passes it, so that message is always synthesized fresh, exactly
        as before.
        """
        tracker = HeardTracker()
        for i, text in enumerate(sentences):
            if i == 0 and cached is not None:
                frames = cached
            else:
                frames = await self._tts.synthesize_frames(text, ext.voice_id, i)
            tracker.expect(i, len(frames))
            await audio.send_frames(frames, on_sent=tracker.record_sent)

    async def _reply(self, audio: AudioSocketSession, ext: ExtensionConfig,
                     hermes: HermesClient, session_id: str, utterance: str,
                     # Keyword-only from here on. Four adjacent asyncio.Event
                     # parameters are interchangeable to Python and nearly
                     # interchangeable to a reader, and the guard that reads
                     # two of them (speak_sentence's `not
                     # utterance_ready.is_set() and not final_seen.is_set()`)
                     # is symmetric, so a positional swap of those two is
                     # invisible to the type checker AND to any test that
                     # only drives that guard. The one place the difference
                     # shows is _next_delta(stream, utterance_ready) below:
                     # swapped, the reply is abandoned on any bare final --
                     # and with endpointing=300 Deepgram finalises at every
                     # ~300ms pause, so a backchannel ("mhm") during the
                     # thinking window would discard the answer the caller is
                     # still waiting for and answer the backchannel instead.
                     # Naming them at every call site makes that class of
                     # defect unrepresentable rather than merely tested for.
                     *,
                     interrupt: asyncio.Event, utterance_ready: asyncio.Event,
                     final_seen: asyncio.Event, ended: asyncio.Event,
                     deadline: float,
                     # Per-turn latency instrumentation. t0 comes from
                     # handle() (see the comment at its call site); trigger
                     # is which of handle()'s two paths produced this turn,
                     # "utterance_ready" or "stalled". Defaulted rather than
                     # required: the many existing tests that call _reply
                     # directly to exercise barge-in/filler behaviour do not
                     # care about latency numbers, and handle() -- the only
                     # real caller -- always passes both explicitly.
                     t0: float | None = None,
                     trigger: str = "utterance_ready",
                     channel_id: str = "",
                     read_utterance: Callable[[], str] = lambda: "",
                     consume: Callable[[], None] = lambda: None,
                     suppress_ack: bool = False,
                     caller_has_floor: Callable[[], bool] = lambda: False,
                     delivery_notes: list[str] | None = None) -> None:
        # An intentionally tiny guard for the observed clipped Atlas request.
        # Do not infer completeness from generic trailing words, rewrite
        # product names, or guess a research topic. General ambiguity belongs
        # to the profile's clarification policy, not a linguistic heuristic.
        normalized = ' '.join(re.sub(r'[.?!,]', ' ', utterance.lower()).split())
        clarification = ''
        if ext.profile == 'atlas' and normalized in (
            'can you look into what you have been',
            'okay can you look into what you have been',
        ):
            clarification = ('I may have missed the end of that. Could you finish your question? '
                             'Please repeat the full request.')
        if ext.hold_enabled or clarification:
            from phonesys.hold import run_hold_reply
            if delivery_notes and not clarification:
                utterance = '\n'.join(delivery_notes) + '\nCaller request: ' + utterance
                delivery_notes.clear()
            await run_hold_reply(
                audio=audio, ext=ext, hermes=hermes, session_id=session_id,
                utterance=utterance, ari=self._ari, channel_id=channel_id,
                ended=ended, deadline=deadline, read_utterance=read_utterance,
                consume=consume, tts=self._tts,
                local_reply=clarification, interrupt=interrupt, final_seen=final_seen, t0=t0,
                tracker=HeardTracker(), delivery_notes=delivery_notes,
                suppress_ack=suppress_ack,
                caller_has_floor=caller_has_floor,
            )
            return
        buf = SentenceBuffer()
        tracker = HeardTracker()
        loop = asyncio.get_running_loop()
        # One absolute cutoff, not refreshed by text, tool events or keepalives.
        # Playback already in progress may finish; the next delta wait observes
        # this cutoff. max_call_seconds still bounds sentence playback itself.
        reply_deadline = loop.time() + ext.hermes_reply_timeout_seconds
        spoken: list[str] = []
        index = 0

        # Per-turn stage latency (see the logger.info in `finally` below).
        # t0 is the zero point, passed in from handle(). t1/t2/t3 are
        # stamped below at the FIRST occurrence of each stage only -- `if
        # tN is None` guards, no dict, no once-only-flag-plus-pop. Unlike
        # the source codebase this instruments, a turn here is one function
        # call with closure locals, so there is nothing to key by call id
        # and nothing to clean up in a finally. loop.time() (monotonic)
        # throughout; never time.time().
        t1: float | None = None   # first Hermes delta (_next_delta returns)
        t2: float | None = None   # first TTS frames back for a REPLY
                                   # sentence (synthesize_frames returns)
        t3: float | None = None   # first REPLY frame actually on the wire
                                   # (the first tracker.record_sent credit)
        # Tracked separately from t3: filler frames route through
        # _discard_frame, not tracker.record_sent, and filler almost always
        # starts speaking well before the real reply does. A single "time
        # to first audio" number would really just be measuring filler and
        # would hide every regression in actual reply latency behind it.
        first_filler_frame: float | None = None

        # The startup-prepared bank belongs to the first streaming profile's
        # voice. Never play Mary's cached filler after a transfer to John.
        # Other profiles synthesize correctly voiced filler on demand and
        # cache each phrase for this turn while Hermes generates in parallel.
        bank = (self._filler_bank if self._filler_profile in (None, ext.profile)
                else None)
        filler_stop = asyncio.Event()
        filler_task: asyncio.Task | None = None
        interrupt_watch: asyncio.Task | None = None

        def _on_filler_frame_sent(sentence_index: int) -> None:
            """on_sent for filler frames: latch first_filler_frame, then
            behave exactly like _discard_frame (never credits HeardTracker).

            A plain synchronous clock read plus a delegate call, nothing
            more -- this still runs inline inside audiosocket.py's paced
            20ms write loop, so it must stay cheap and must never log or
            await.
            """
            nonlocal first_filler_frame
            if first_filler_frame is None:
                first_filler_frame = loop.time()
            _discard_frame(sentence_index)

        async def run_filler() -> None:
            """Speak escalating stall phrases until the reply is ready.

            Sleeps wait on filler_stop rather than plain asyncio.sleep: a
            bare sleep would keep the task alive for a full repeat interval
            after it was told to stop, and _drain_sender would time out.

            Also watches `ended`: a caller who hangs up while Hermes is
            still generating would otherwise leave this loop calling
            bank.take() and writing dead-air audio nobody hears every
            filler_repeat_ms for as long as Hermes keeps streaming (up to
            its own 30s client timeout).
            """
            try:
                await _wait_any((filler_stop, ended),
                                timeout=ext.filler_delay_ms / 1000.0)
                tier = 0
                phrase_tiers = tuple(t for t in ext.filler_phrases if t)
                phrase_cache: dict[str, Frames] = {}
                while not filler_stop.is_set() and not ended.is_set():
                    if bank is not None and not bank.is_empty():
                        frames = bank.take(tier)
                        if frames is None:
                            return
                    else:
                        if not phrase_tiers:
                            return
                        phrase = random.choice(phrase_tiers[tier])
                        if phrase not in phrase_cache:
                            phrase_cache[phrase] = await self._tts.synthesize_frames(
                                phrase, ext.voice_id, -1
                            )
                        frames = phrase_cache[phrase]
                    # Synthesis can finish after a stop request. Never restart
                    # the session-wide sender after filler was silenced.
                    if filler_stop.is_set() or ended.is_set():
                        return
                    await audio.send_frames(frames, on_sent=_on_filler_frame_sent)
                    if filler_stop.is_set() or ended.is_set():
                        return
                    last_tier = (bank.last_tier
                                 if bank is not None and not bank.is_empty()
                                 else len(phrase_tiers) - 1)
                    tier = min(tier + 1, last_tier)
                    await _wait_any((filler_stop, ended),
                                    timeout=ext.filler_repeat_ms / 1000.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken filler must never fail a call.
                logger.exception("filler failed; continuing without it")

        async def silence_filler_on_interrupt() -> None:
            """Stop filler audio the moment the caller talks over it.

            Only the AUDIO stops here. Abandoning the reply is driven by a
            complete new utterance instead -- see _next_delta -- because
            `interrupt` also fires on line noise, and abandoning on that
            would leave the caller with silence and no answer coming.
            """
            await interrupt.wait()
            filler_stop.set()
            audio.stop_sending()

        async def stop_filler() -> None:
            """Guarantee the filler is finished before anything else writes.

            _drain_sender rather than a bare cancel: cancelling a sender
            mid-write abandons a half-written frame.
            """
            nonlocal filler_task, interrupt_watch
            if interrupt_watch is not None:
                interrupt_watch.cancel()
                interrupt_watch = None
            if filler_task is None:
                return
            task, filler_task = filler_task, None
            filler_stop.set()
            audio.stop_sending()
            await _drain_sender(task)

        if ((bank is not None and not bank.is_empty()) or ext.filler_phrases) \
                and ext.filler_delay_ms > 0:
            filler_task = asyncio.create_task(run_filler())
            interrupt_watch = asyncio.create_task(silence_filler_on_interrupt())

        def _on_reply_frame_sent(sentence_index: int) -> None:
            """on_sent for REPLY frames: latch t3, then credit HeardTracker.

            Same reasoning as _on_filler_frame_sent above: a plain
            synchronous clock read plus a delegate call, run inline from
            audiosocket.py's paced write loop -- no logging, no await, so
            the loop's 20ms pacing (what interruption and heard-tracking
            both depend on) is untouched.
            """
            nonlocal t3
            if t3 is None:
                t3 = loop.time()
            tracker.record_sent(sentence_index)

        async def speak_sentence(text: str) -> None:
            nonlocal index, t2
            if ended.is_set():
                raise _CallEnded
            if loop.time() >= deadline:
                raise _TimeLimit
            frames = await self._tts.synthesize_frames(text, ext.voice_id, index)
            if t2 is None:
                t2 = loop.time()
            tracker.expect(index, len(frames))
            spoken.append(text)
            # Before ANY reply audio: exactly one writer on the socket. This
            # sits AFTER synthesize_frames, not before it: ElevenLabs is a
            # 400-600ms round trip (see filler.py's module docstring), and
            # stopping filler before that call would reintroduce exactly the
            # dead air this feature exists to remove, on every reply. Filler
            # keeps talking through the whole round-trip and only goes
            # silent once real audio is actually ready to replace it. A TTS
            # failure here leaves filler running rather than stopped -- that
            # is fine, and arguably better: the caller keeps hearing the
            # agent instead of dropping into silence while a retry is
            # decided. _reply's `finally: await stop_filler()` is what
            # guarantees it is stopped by the time this whole reply ends,
            # not this call.
            await stop_filler()
            if (index == 0 and not utterance_ready.is_set()
                    and not final_seen.is_set()):
                # A confirmed interim burst from the THINKING window (before
                # any real sentence existed to interrupt) has already done
                # its only legitimate job here: silencing filler, via
                # silence_filler_on_interrupt. A genuine new question during
                # that window normally produces utterance_end and is handled
                # by the abandon path in _next_delta instead -- not by
                # leaving `interrupt` armed to instantly cut the first real
                # sentence to a single ~20ms frame the instant the waiter
                # below is created. handle() cannot clear it itself: it is
                # blocked awaiting this whole call.
                #
                # `not utterance_ready.is_set()` alone is NOT enough: Deepgram
                # normally emits UtteranceEnd close to a SECOND after the
                # final that preceded it (endpointing=300 /
                # utterance_end_ms=1000, stream_stt.py), and TTS synthesis
                # (400-600ms) very often finishes -- landing this guard --
                # inside that gap. There `interrupt` is already final-backed
                # (a real word, not noise -- empty transcripts are dropped
                # before pump_stt ever sets it, see stream_stt.py) but
                # `utterance_ready` has not caught up yet, and clearing here
                # would silently let the whole stale reply play out, talking
                # over the caller's genuine new question.
                #
                # `final_seen` closes that gap: pump_stt sets it at the same
                # statement as `interrupt` whenever the burst was a final,
                # and handle() clears it (like utterance_ready) immediately
                # before every call into _reply, so a stale set flag from the
                # utterance just answered can never leak into the next
                # reply's guard. A burst with neither utterance_ready nor
                # final_seen set is, by elimination, a bare interim with no
                # final behind it at all -- exactly line noise -- and is
                # still safe to clear.
                #
                # If the caller really is just still talking (no final
                # yet) as the reply starts, Deepgram keeps emitting interims
                # and `interrupt` re-arms within one confirm window
                # (~100-300ms), so barge-in still works on this and every
                # later sentence. Scoped to the FIRST sentence only:
                # clearing before every sentence would swallow a genuine
                # barge-in that lands in the gap between two sentences.
                interrupt.clear()
            sender = asyncio.create_task(
                audio.send_frames(frames, on_sent=_on_reply_frame_sent)
            )
            waiter = asyncio.create_task(interrupt.wait())
            try:
                # The timeout is what bounds a reply by max_call_seconds:
                # without it the whole of _reply sits outside the deadline
                # check in handle()'s loop, and a runaway agent can speak for
                # as long as it likes.
                done, _ = await asyncio.wait(
                    {sender, waiter}, timeout=(None if deadline == float('inf')
                                              else max(0.0, deadline - loop.time())),
                    return_when=asyncio.FIRST_COMPLETED
                )
            except BaseException:
                audio.stop_sending()
                sender.cancel()
                raise
            finally:
                waiter.cancel()
            if sender in done:
                # Surface a sender that blew up rather than silently
                # crediting the sentence as spoken.
                sender.result()
                index += 1
                return
            audio.stop_sending()
            await _drain_sender(sender)
            if waiter in done:
                raise _Interrupted
            # asyncio.wait timed out with neither finished: the call's time
            # is up mid-sentence.
            raise _TimeLimit

        def credit_heard(reason: str) -> None:
            """Tell the agent what the caller actually got to hear.

            Runs for any early exit that leaves the caller on the line, not
            just interruption -- otherwise the agent's history claims a reply
            the caller only partly received.

            Deliberately NOT awaited. append_assistant POSTs to /chat, which
            runs a whole synchronous agent turn: awaiting it here would stall
            the caller for up to the client timeout (30s) after every single
            barge-in, generating and discarding a reply nobody hears, before
            the line goes back to LISTENING. Failure is logged and dropped --
            a lost history correction is far cheaper than a frozen call.
            """
            heard = tracker.heard_text(spoken)
            # `spoken` is NOT the full generated reply: on interruption the
            # SSE stream is abandoned inside contextlib.aclosing (see the
            # try/except below), so it only ever holds sentences that
            # actually reached synthesize_frames. The diff this gives is
            # generated-and-queued vs heard, not generated vs heard -- logged
            # here purely to show where the truncation boundary fell, not as
            # a stand-in for "what Hermes would have said next".
            logger.info("%s; caller heard: %r; spoken: %r",
                        reason, heard, " ".join(spoken))
            if heard:
                spawn(_quietly("append_assistant",
                               hermes.append_assistant(session_id, heard)),
                      name=f"append_assistant-{session_id}")

        try:
            # aclosing: an interruption abandons the SSE stream mid-iteration,
            # and without this the underlying HTTP response would only be
            # released whenever the generator happened to be collected.
            async with contextlib.aclosing(
                hermes.chat_stream(session_id, utterance)
            ) as stream:
                while True:
                    delta = await _next_delta(
                        stream, utterance_ready, ended=ended, deadline=deadline,
                        reply_deadline=reply_deadline
                    )
                    # t1: first return from _next_delta, whatever it is --
                    # including a None on a reply that streamed no content
                    # at all. This is a plain assignment in _reply's own
                    # scope (not a nested function), so no `nonlocal` needed.
                    if t1 is None:
                        t1 = loop.time()
                    if delta is None:
                        break
                    # Also checked here, not only in speak_sentence: a reply
                    # that streams tokens without ever completing a sentence
                    # would otherwise run past the deadline unchecked.
                    if loop.time() >= deadline:
                        raise _TimeLimit
                    for sentence in buf.feed(delta):
                        await speak_sentence(sentence)
            tail = buf.flush()
            if tail:
                await speak_sentence(tail)
        except _ReplyTimeout:
            logger.warning("Hermes reply budget expired; returning to listening")
            # Do not POST a history correction: that starts another agent run
            # immediately after cancelling this one. Log partial heard text only.
            logger.info("reply timeout; caller heard: %r", tracker.heard_text(spoken))
            await stop_filler()
            if (not ended.is_set() and not utterance_ready.is_set()
                    and loop.time() < deadline):
                # The normal sender preserves one writer, frame draining,
                # barge-in and the overall call deadline for this apology too.
                with contextlib.suppress(_Interrupted, _CallEnded, _TimeLimit,
                                         TtsStreamError, httpx.HTTPError):
                    await speak_sentence(HERMES_TIMEOUT_MESSAGE)
        except _Interrupted:
            credit_heard("interrupted")
        except _TimeLimit:
            logger.info("max_call_seconds reached mid-reply; stopped speaking")
            credit_heard("call time limit reached mid-reply")
        except TtsStreamError as exc:
            # The caller is still on the line, and a TTS failure is usually a
            # single bad request rather than a dead account -- so this returns
            # to listening rather than ending the call, and the next utterance
            # gets a fresh attempt. What matters either way is that the
            # sentences already played are still recorded: dropping them would
            # leave the agent believing it said a reply the caller half heard.
            logger.warning("TTS failed mid-reply, returning to listening: %s", exc)
            credit_heard("tts failed mid-reply")
        except (HermesError, httpx.HTTPError) as exc:
            # Same reasoning as the TTS arm above, for the other half of the
            # reply pipeline. HermesClient.chat_stream raises HermesError on
            # any status >= 400 from INSIDE the generator body (hermes.py), and
            # unlike stream_tts.py it does not wrap httpx's own errors, so a
            # ReadError/ReadTimeout mid-SSE surfaces as itself. Both reach here
            # via nxt.result() in _next_delta. Without this arm they escaped
            # _reply into handle()'s generic `except Exception`, which tears the
            # bridge down and hangs up both channels -- dropping the caller
            # mid-conversation over a restarted api_server or a proxy 502 that
            # the next request would have survived.
            #
            # The design's error table (2026-08-04-barge-in-streaming-design.md)
            # says "apologize aloud, return to LISTENING", so unlike the STT
            # failure this does not end the call.
            logger.warning("Hermes stream failed mid-reply, returning to "
                           "listening: %s", exc)
            credit_heard("hermes stream failed mid-reply")
            # stop_filler() FIRST, and not merely via the `finally` below: the
            # apology is another writer on the socket, and the one-writer
            # invariant (see speak_sentence) does not survive filler still
            # talking underneath it. Idempotent, so the `finally` stays honest.
            await stop_filler()
            if not ended.is_set():
                # _quietly, because the apology's own synthesis can fail too --
                # and a failed apology must not turn a recoverable blip into a
                # dropped call, which is the whole point of this arm.
                await _quietly("hermes failure apology",
                               self._speak(audio, ext, [HERMES_FAILURE_MESSAGE]))
        except _CallEnded:
            # Nobody is listening, so there is no "what they heard" worth
            # recording -- and nothing to say it to.
            logger.info("call ended mid-reply; stopped speaking")
        finally:
            # Per-turn stage latency, once per turn, regardless of how the
            # turn ended. hermes_ms/tts_ms/queue_ms are the three deltas
            # between adjacent marks (t1-t0, t2-t1, t3-t2); reply_first_
            # audio_ms (t3-t0) and filler_first_audio_ms are the two
            # separate "time to first audio" numbers -- see the comment on
            # first_filler_frame above for why they must not be collapsed
            # into one. Any mark that never happened (e.g. t2/t3 on a reply
            # abandoned before a word was synthesised) logs as None rather
            # than a fabricated number.
            logger.info(
                "call %s turn latency: trigger=%s hermes_ms=%s tts_ms=%s "
                "queue_ms=%s filler_first_audio_ms=%s reply_first_audio_ms=%s",
                session_id, trigger,
                _delta_ms(t1, t0), _delta_ms(t2, t1), _delta_ms(t3, t2),
                _delta_ms(first_filler_frame, t0), _delta_ms(t3, t0),
            )
            # Every exit path -- interrupted, time limit, TTS failure, call
            # ended, or success -- must leave no filler task running.
            await stop_filler()


def _discard_frame(_sentence_index: int) -> None:
    """on_sent for filler frames.

    Filler is not part of the reply, so it must never reach the reply's
    HeardTracker: crediting it would make append_assistant tell Hermes it
    said words it never generated.
    """


def _delta_ms(later: float | None, earlier: float | None) -> int | None:
    """Millisecond delta between two loop.time() marks, or None if either
    mark never happened.

    A stage that never ran (e.g. t2/t3 on a reply abandoned before any
    sentence was synthesised, or first_filler_frame when no filler bank is
    configured) must read as "this stage did not happen", not as a
    fabricated 0 -- either argument can legitimately be None.
    """
    if later is None or earlier is None:
        return None
    return round((later - earlier) * 1000)


async def _next_delta(stream, abandon: asyncio.Event, *,
                      ended: asyncio.Event, deadline: float,
                      reply_deadline: float) -> str | None:
    """Race every delta against call termination and a new utterance.

    Drain anext before aclosing the stream, including on outer cancellation:
    closing a still-running async generator raises instead of releasing HTTP.
    """
    loop = asyncio.get_running_loop()
    nxt = asyncio.create_task(anext(stream))
    stop = asyncio.create_task(abandon.wait())
    hung_up = asyncio.create_task(ended.wait())
    tasks = {nxt, stop, hung_up}
    try:
        done, _ = await asyncio.wait(
            tasks, timeout=max(0.0, min(deadline, reply_deadline) - loop.time()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ended.is_set():
            raise _CallEnded
        if loop.time() >= deadline:
            raise _TimeLimit
        # Playback may have crossed the generation cutoff after the final
        # sentence. An already-finished stream is success, not an incomplete
        # answer; never grant more waiting time to obtain that EOF, though.
        if nxt in done and isinstance(nxt.exception(), StopAsyncIteration):
            return None
        if abandon.is_set() and loop.time() >= reply_deadline:
            raise _Interrupted
        if loop.time() >= reply_deadline:
            raise _ReplyTimeout
        # Preserve the existing delta preference on simultaneous barge-in.
        if nxt in done:
            try:
                return nxt.result()
            except StopAsyncIteration:
                return None
        raise _Interrupted
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Retrieve all results, including errors from a losing delta task.
        # Shield the drain against repeated cancellation; propagate cancellation
        # only after generator cleanup, never while aclose would still race it.
        drain = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = False
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                cancelled = True
        drain.result()
        if cancelled:
            raise asyncio.CancelledError


async def _drain_sender(sender: asyncio.Task) -> None:
    """Let a stopped sender finish its current frame and return.

    stop_sending() has already been called, so this returns within one frame
    interval. Cancelling outright instead would abandon a half-written frame
    and lose the on_sent credit for frames that did reach the caller -- the
    exact record HeardTracker depends on.
    """
    try:
        await asyncio.wait_for(sender, timeout=SENDER_DRAIN_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("outbound sender did not stop in %ss; cancelled",
                       SENDER_DRAIN_TIMEOUT)
    except Exception:
        logger.exception("outbound sender failed")
