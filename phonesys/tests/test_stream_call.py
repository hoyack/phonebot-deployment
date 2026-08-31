"""Tests for the barge-in state machine.

The HeardTracker tests are pure and exact. The rest drive the real
StreamingCallHandler against injected fakes: no sockets, no network, and no
wall-clock races -- interruptions are fired from inside the outbound send
loop (via FakeAudio.on_sent_hook) at an exact frame boundary, so "the caller
interrupted halfway through sentence 2" is a deterministic event rather than
a sleep that hopes to land in the right place.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass

import httpx
import pytest

from phonesys.config import ExtensionConfig
from phonesys.hermes import HermesError
from phonesys.stream_call import (
    HERMES_FAILURE_MESSAGE,
    STT_FAILURE_MESSAGE,
    UTTERANCE_END_FALLBACK,
    HeardTracker,
    SpeechBurst,
    StreamingCallHandler,
)
from phonesys.stream_stt import SttEvent
from phonesys.stream_tts import TtsStreamError
from phonesys.tasks import BACKGROUND_TASKS


# --------------------------------------------------------------------------
# HeardTracker: what the agent is allowed to believe the caller heard.
# --------------------------------------------------------------------------

def test_nothing_sent_means_nothing_heard():
    t = HeardTracker()
    assert t.heard_text(["One.", "Two."]) == ""


def test_fully_sent_sentence_counts_as_heard():
    t = HeardTracker()
    t.expect(0, frames=3)
    for _ in range(3):
        t.record_sent(0)
    assert t.heard_text(["One.", "Two."]) == "One."


def test_partially_sent_sentence_is_not_counted():
    """Round DOWN: claiming a half-spoken sentence was heard makes the agent
    reference words the caller never got."""
    t = HeardTracker()
    t.expect(0, frames=3)
    t.expect(1, frames=3)
    for _ in range(3):
        t.record_sent(0)
    t.record_sent(1)          # only 1 of 3 frames of sentence 1
    assert t.heard_text(["One.", "Two."]) == "One."


def test_multiple_complete_sentences_are_joined():
    t = HeardTracker()
    t.expect(0, frames=2)
    t.expect(1, frames=2)
    for _ in range(2):
        t.record_sent(0)
    for _ in range(2):
        t.record_sent(1)
    assert t.heard_text(["One.", "Two.", "Three."]) == "One. Two."


def test_a_later_complete_sentence_cannot_jump_a_gap():
    """Sentences are heard in order. If sentence 1 was cut off, sentence 2
    cannot have reached the caller either, even if its frame count happens
    to be satisfied."""
    t = HeardTracker()
    t.expect(0, frames=2)
    t.expect(1, frames=2)
    t.expect(2, frames=2)
    for _ in range(2):
        t.record_sent(0)
    t.record_sent(1)
    for _ in range(2):
        t.record_sent(2)
    assert t.heard_text(["One.", "Two.", "Three."]) == "One."


def test_a_sentence_with_no_frames_is_never_heard():
    """A very short sentence can synthesise to zero whole 20ms frames --
    _frames_from_ulaw drops the sub-frame tail. Nothing was played, so
    `0 sent >= 0 expected` must not credit it as heard."""
    t = HeardTracker()
    t.expect(0, frames=0)
    t.expect(1, frames=1)
    t.record_sent(1)
    assert t.heard_text(["Hm.", "Two."]) == ""


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _StreamingExt(ExtensionConfig):
    """ExtensionConfig as task 9 will extend it.

    barge_in_confirm_ms lands on the real dataclass in the config task; the
    handler reads it defensively so this task can be exercised now.
    """
    barge_in_confirm_ms: int = 300


def _ext(**over) -> _StreamingExt:
    base = dict(
        profile="phonebot",
        hermes_base_url="http://127.0.0.1:8642",
        hermes_api_key="k",
        greeting="Hello there.",
        voice_id="voice-abc",
        max_silence_seconds=1,
        max_turn_seconds=30,
        max_call_seconds=600,
    )
    base.update(over)
    return _StreamingExt(**base)


class FakeAudio:
    """Stand-in for AudioSocketSession.

    frames() streams inbound audio continuously until hangup is set, which is
    what a live call does. send_frames() honours stop_sending() and calls
    on_sent only for frames it actually "wrote", exactly like the real one.
    """

    MAX_INBOUND = 2000   # safety net so a misbehaving test cannot spin forever

    def __init__(self, frame_delay: float = 0.001, send_delay: float = 0.001,
                 track_concurrency: bool = False) -> None:
        self.frame_delay = frame_delay
        self.send_delay = send_delay
        self.hangup = asyncio.Event()
        self.sent: list[tuple[bytes, int]] = []
        # Decoded text of every REPLY frame written (index >= 0 -- filler
        # frames are tagged with the negative FILLER_SENTENCE_INDEX and are
        # deliberately excluded, same as HeardTracker excludes them). Lets
        # abandon tests assert on what the caller actually received without
        # reaching into FakeTts.calls, which records synthesis, not writes.
        self.spoken_text: list[str] = []
        self.inbound_count = 0
        self.is_sending = False
        self.stopped = 0
        self.cancelled = False        # send_frames was cancelled mid-write
        self.finished = False         # send_frames returned under its own steam
        # (sentence_index, frames sent so far FOR THIS SENTENCE) -> None.
        # Counted per send_frames call, not per index: sentence indexes
        # restart at 0 on every reply, so a shared counter would silently
        # stop the hooks from firing.
        self.on_sent_hook = None
        # Filler tests need to prove there is never more than one send_frames
        # in flight at once -- two concurrent writers would interleave frames
        # on the wire AND stop each other (stop_sending() is session-wide).
        self.track_concurrency = track_concurrency
        self._concurrent_senders = 0
        self.max_concurrent_senders = 0

    async def frames(self):
        while not self.hangup.is_set() and self.inbound_count < self.MAX_INBOUND:
            await asyncio.sleep(self.frame_delay)
            if self.hangup.is_set():
                break
            self.inbound_count += 1
            yield bytes([self.inbound_count % 256]) * 320

    def stop_sending(self) -> None:
        self.is_sending = False
        self.stopped += 1

    async def send_frames(self, frames, on_sent) -> None:
        # Mirrors AudioSocketSession.send_frames: the stop flag is checked
        # only at the TOP of each iteration, so a frame whose write has
        # already started completes and IS credited via on_sent. The sleep
        # stands in for write()+drain() -- cancelling the task inside it is
        # exactly the half-written frame that loses the credit.
        self.is_sending = True
        if self.track_concurrency:
            self._concurrent_senders += 1
            self.max_concurrent_senders = max(
                self.max_concurrent_senders, self._concurrent_senders
            )
        count = 0
        try:
            for payload, index in frames:
                if not self.is_sending:
                    break
                await asyncio.sleep(self.send_delay)
                self.sent.append((payload, index))
                if index >= 0:
                    with contextlib.suppress(UnicodeDecodeError):
                        self.spoken_text.append(payload.decode())
                on_sent(index)
                count += 1
                if self.on_sent_hook is not None:
                    self.on_sent_hook(index, count)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        else:
            self.finished = True
        finally:
            self.is_sending = False
            if self.track_concurrency:
                self._concurrent_senders -= 1


class FakeStt:
    def __init__(self, audio: FakeAudio | None = None,
                 timeout_at: int | None = None) -> None:
        self._audio = audio
        self._timeout_at = timeout_at
        self._queue: asyncio.Queue = asyncio.Queue()
        self.attempts = 0
        self.sent: list[bytes] = []
        self.sent_while_speaking: list[bool] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send(self, pcm: bytes) -> None:
        self.attempts += 1
        if self._timeout_at is not None and self.attempts > self._timeout_at:
            raise asyncio.TimeoutError("deepgram send stalled")
        self.sent.append(pcm)
        if self._audio is not None:
            self.sent_while_speaking.append(self._audio.is_sending)

    async def events(self):
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        self.closed = True

    def emit(self, ev: SttEvent) -> None:
        self._queue.put_nowait(ev)


class FakeTts:
    def __init__(self, frames_per_sentence: int = 4,
                 synthesis_delay: float = 0.0) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self._n = frames_per_sentence
        # Stands in for the real ElevenLabs round trip (400-600ms per
        # filler.py's own module docstring), so tests can prove what happens
        # to filler audio WHILE synthesis is still in flight.
        self._synthesis_delay = synthesis_delay

    async def synthesize_frames(self, text, voice_id, sentence_index):
        if self._synthesis_delay:
            await asyncio.sleep(self._synthesis_delay)
        self.calls.append((text, voice_id, sentence_index))
        # The payload carries the sentence text (rather than opaque zero
        # bytes) so FakeAudio.spoken_text can report what was actually
        # written to the socket, independent of what was merely synthesised.
        return [(text.encode(), sentence_index) for _ in range(self._n)]

    @property
    def spoken(self) -> list[str]:
        return [c[0] for c in self.calls]


class FailingTts(FakeTts):
    """Fails the Nth synthesis call (1-based), as ElevenLabs would on a 5xx."""

    def __init__(self, fail_on_call: int, frames_per_sentence: int = 3) -> None:
        super().__init__(frames_per_sentence)
        self._fail_on = fail_on_call
        self.attempts = 0

    async def synthesize_frames(self, text, voice_id, sentence_index):
        self.attempts += 1
        if self.attempts == self._fail_on:
            raise TtsStreamError("elevenlabs failed: HTTP 500")
        return await super().synthesize_frames(text, voice_id, sentence_index)


class FakeHermes:
    def __init__(self, deltas=(), first_delta_delay: float = 0.0) -> None:
        self.sessions: list[str] = []
        self.chats: list[tuple[str, str]] = []
        self.appended: list[tuple[str, str]] = []
        self.closed = False
        self._deltas = list(deltas)
        # How long the agent takes to START answering -- the exact gap
        # filler exists to cover. Only the FIRST delta is delayed: once
        # Hermes starts streaming, later deltas arrive quickly in practice.
        self._first_delta_delay = first_delta_delay
        # Set in the generator's `finally`, so it is True whether the stream
        # ran to completion OR was closed early via contextlib.aclosing --
        # proving an abandoned reply does not leak the underlying SSE
        # response.
        self.stream_closed = False

    async def create_session(self, session_id: str) -> str:
        self.sessions.append(session_id)
        return session_id

    async def chat_stream(self, session_id, message):
        self.chats.append((session_id, message))
        try:
            for i, delta in enumerate(self._deltas):
                if i == 0 and self._first_delta_delay:
                    await asyncio.sleep(self._first_delta_delay)
                else:
                    await asyncio.sleep(0)
                yield delta
        finally:
            self.stream_closed = True

    async def append_assistant(self, session_id, text) -> None:
        self.appended.append((session_id, text))

    async def aclose(self) -> None:
        self.closed = True


class ScriptedHermes(FakeHermes):
    """A DIFFERENT reply per chat_stream call.

    FakeHermes replays one fixed delta list on every call, so a multi-turn
    test cannot tell turn 1's audio apart from turn 2's -- which is exactly
    what a test about later turns has to assert on.
    """

    def __init__(self, deltas_per_chat, first_delta_delay: float = 0.0) -> None:
        super().__init__(first_delta_delay=first_delta_delay)
        self._per_chat = [list(d) for d in deltas_per_chat]

    async def chat_stream(self, session_id, message):
        self.chats.append((session_id, message))
        n = len(self.chats)
        deltas = self._per_chat[n - 1] if n <= len(self._per_chat) else []
        try:
            for i, delta in enumerate(deltas):
                if i == 0 and self._first_delta_delay:
                    await asyncio.sleep(self._first_delta_delay)
                else:
                    await asyncio.sleep(0)
                yield delta
        finally:
            self.stream_closed = True


class ExplodingHermes(FakeHermes):
    """chat_stream that dies part-way through a reply.

    The raise happens from INSIDE the generator body, which is where the real
    client raises: HermesClient.chat_stream opens the SSE response and only
    then checks the status (hermes.py), so a 502 from a restarted api_server --
    or an httpx ReadError mid-stream -- surfaces from anext() in _next_delta,
    never from the call that merely creates the generator.

    deltas_per_chat gives each chat_stream call its own delta list, so the
    reply that dies and the reply that follows it stay distinguishable.

    first_delta_delay, like FakeHermes's and ScriptedHermes's own parameter
    of the same name, delays only the FIRST delta -- the "thinking window" a
    slow-to-die stream still has before it fails, long enough for filler to
    actually be sending when the failure lands rather than merely scheduled
    to.
    """

    def __init__(self, deltas_per_chat, fail_on_chat: int = 1,
                 exc: BaseException | None = None,
                 first_delta_delay: float = 0.0) -> None:
        super().__init__()
        self._per_chat = [list(d) for d in deltas_per_chat]
        self._fail_on_chat = fail_on_chat
        self._exc = exc if exc is not None else HermesError("HTTP 502")
        self._first_delta_delay = first_delta_delay

    async def chat_stream(self, session_id, message):
        self.chats.append((session_id, message))
        n = len(self.chats)
        try:
            for i, delta in enumerate(
                self._per_chat[n - 1] if n <= len(self._per_chat) else []
            ):
                if i == 0 and self._first_delta_delay:
                    await asyncio.sleep(self._first_delta_delay)
                else:
                    await asyncio.sleep(0)
                yield delta
            if n == self._fail_on_chat:
                raise self._exc
        finally:
            self.stream_closed = True


class FakeAri:
    def __init__(self) -> None:
        self.answered: list[str] = []
        self.bridges: list[str] = []
        self.added: list[tuple[str, str]] = []
        self.originated: list[tuple[str, int, str, str]] = []
        self.destroyed: list[str] = []
        self.hungup: list[str] = []

    # Every method suspends once, like the real HTTP client: teardown steps
    # must be genuine cancellation points for the cancellation tests to mean
    # anything.
    async def answer(self, channel_id):
        await asyncio.sleep(0)
        self.answered.append(channel_id)

    async def create_bridge(self) -> str:
        await asyncio.sleep(0)
        bridge_id = f"bridge-{len(self.bridges)}"
        self.bridges.append(bridge_id)
        return bridge_id

    async def add_to_bridge(self, bridge_id, channel_id):
        await asyncio.sleep(0)
        self.added.append((bridge_id, channel_id))

    async def originate_audiosocket(self, host, port, uuid, app) -> str:
        await asyncio.sleep(0)
        self.originated.append((host, port, uuid, app))
        return "as-chan"

    async def destroy_bridge(self, bridge_id):
        await asyncio.sleep(0)
        self.destroyed.append(bridge_id)

    async def hangup(self, channel_id):
        await asyncio.sleep(0)
        self.hungup.append(channel_id)


class FakeServer:
    def __init__(self, session: FakeAudio) -> None:
        self._session = session
        self.waited: list[tuple[str, float]] = []

    async def wait_for_session(self, uuid, timeout=10.0) -> FakeAudio:
        self.waited.append((uuid, timeout))
        return self._session


class SequentialFakeServer:
    """Like FakeServer, but hands out a DIFFERENT session per call, in order,
    ignoring uuid the same way FakeServer does.

    Needed for tests that drive one StreamingCallHandler instance through
    more than one handle() call: production has exactly one handler per
    process (see main.py's _first_streaming_extension) serving every call to
    that extension, each with its own AudioSocket session, while everything
    else about the handler -- including any greeting_frames cache -- is the
    same object across calls. FakeServer's single fixed session cannot model
    two calls at once; this can.
    """

    def __init__(self, sessions: list[FakeAudio]) -> None:
        self._sessions = list(sessions)
        self.waited: list[tuple[str, float]] = []

    async def wait_for_session(self, uuid, timeout=10.0) -> FakeAudio:
        self.waited.append((uuid, timeout))
        return self._sessions.pop(0)


class FakeBank:
    """Returns one distinguishable frame per tier.

    frame_count: how many 20ms frames each take() call returns. Defaults to
    1, the original scaffold value -- but a single 1ms frame finishes so
    fast (against a repeat interval of 10ms in most of these tests) that a
    GENUINELY BROKEN implementation that never stops filler before the real
    reply speaks can still, by sheer luck, almost never have a filler send
    in flight at the exact moment the real sender starts. A real filler
    phrase is 1-2s of audio (dozens of frames); tests that need to prove a
    stop actually happened, not just that timing happened to dodge the
    race, override this to something long enough to still be playing when
    the real reply arrives.

    sentence_index: the sentence index tagged on every frame. Production
    always uses FILLER_SENTENCE_INDEX (-1, see filler.py), which
    HeardTracker.heard_text ignores outright regardless of which on_sent
    callback wrote it -- so a test using the default here can never
    distinguish "on_sent correctly discards filler" from "on_sent doesn't
    matter because the sentinel already protects us". Tests targeting
    _discard_frame's OWN protection, independent of that sentinel, override
    this to a REAL sentence index instead.
    """

    def __init__(self, tiers=3, empty=False, frame_count=1, sentence_index=-1):
        self._tiers, self._empty = tiers, empty
        self._frame_count = frame_count
        self._sentence_index = sentence_index
        self.taken: list[int] = []

    def is_empty(self):
        return self._empty

    @property
    def last_tier(self):
        return self._tiers - 1

    def take(self, tier):
        if self._empty:
            return None
        self.taken.append(tier)
        return [(bytes([tier]) * 320, self._sentence_index)] * self._frame_count


def _handler(ari=None, stt=None, tts=None, hermes=None, server=None,
             ari_app="phonesys", filler_bank=None,
             greeting_frames=None) -> StreamingCallHandler:
    return StreamingCallHandler(
        ari=ari or FakeAri(),
        stt_factory=lambda: stt,
        tts=tts or FakeTts(),
        hermes_factory=lambda ext: hermes,
        audiosocket_server=server,
        ari_app=ari_app,
        filler_bank=filler_bank,
        greeting_frames=greeting_frames,
    )


def _final(text: str) -> SttEvent:
    return SttEvent(kind="transcript", text=text, is_final=True)


def _interim(text: str) -> SttEvent:
    return SttEvent(kind="transcript", text=text, is_final=False)


def _utt_end() -> SttEvent:
    return SttEvent(kind="utterance_end")


def _speech_started() -> SttEvent:
    return SttEvent(kind="speech_started")


async def _until(predicate, limit: int = 500) -> None:
    """Yield to the loop until predicate() holds. Bounded, never a bare sleep."""
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition never became true")


async def _settle() -> None:
    """Let fire-and-forget work (append_assistant) finish before asserting.

    append_assistant is deliberately NOT awaited in the hot path -- it runs a
    whole Hermes turn and would stall the caller -- so tests have to wait for
    the registry to drain instead of assuming it ran inline.
    """
    for _ in range(50):
        pending = [t for t in BACKGROUND_TASKS if not t.done()]
        if not pending:
            await asyncio.sleep(0)
            return
        await asyncio.wait(pending, timeout=1.0)
    raise AssertionError("background tasks never drained")


def _deadline(seconds: float = 60.0) -> float:
    return asyncio.get_running_loop().time() + seconds


async def _run_one_turn(handler, ext, audio=None, interrupt_after=None,
                        new_utterance_after=None, final_after=None):
    """Drive a single reply turn through _reply(), the same call every
    _reply-focused test above already uses directly.

    Filler lives entirely inside _reply, so exercising it does not require
    standing up the whole handle() state machine (ARI, AudioSocket session,
    STT pumps) -- only the inputs _reply itself takes: audio, ext, hermes,
    and the interrupt/utterance_ready/final_seen/ended signals.

    interrupt_after, if given, fires the interrupt event ALONE that many
    seconds in -- the same signal a bare confirmed interim (line noise, a
    cough) sets on the way into _reply -- so the "stop filler before
    speaking" and "does not abandon on noise" paths can be exercised without
    wiring a fake STT and racing real transcript events.

    new_utterance_after, if given, fires utterance_ready that many seconds
    in -- the same signal handle() sets once Deepgram reports a COMPLETE new
    utterance (UtteranceEnd) -- so the "abandon a stale reply" path can be
    exercised the same way.

    final_after, if given, fires interrupt AND final_seen TOGETHER that many
    seconds in -- mirroring pump_stt's final-transcript branch, which sets
    both on the same final-transcript branch (stream_call.py:326 and :331,
    two lines apart only because a comment sits between them). This is what a
    final transcript looks like BEFORE its own UtteranceEnd (utterance_ready)
    has arrived -- distinct from interrupt_after, which has no final behind
    it at all.
    """
    audio = audio or FakeAudio()
    hermes = handler._hermes_factory(ext)
    interrupt = asyncio.Event()
    utterance_ready = asyncio.Event()
    final_seen = asyncio.Event()
    ended = asyncio.Event()

    async def fire_interrupt() -> None:
        await asyncio.sleep(interrupt_after)
        interrupt.set()

    async def fire_new_utterance() -> None:
        await asyncio.sleep(new_utterance_after)
        utterance_ready.set()

    async def fire_final() -> None:
        await asyncio.sleep(final_after)
        interrupt.set()
        final_seen.set()

    waiters = []
    if interrupt_after is not None:
        waiters.append(asyncio.create_task(fire_interrupt()))
    if new_utterance_after is not None:
        waiters.append(asyncio.create_task(fire_new_utterance()))
    if final_after is not None:
        waiters.append(asyncio.create_task(fire_final()))
    try:
        await asyncio.wait_for(
            handler._reply(audio, ext, hermes, "sid", "hi",
                           interrupt=interrupt,
                           utterance_ready=utterance_ready,
                           final_seen=final_seen, ended=ended,
                           deadline=_deadline()),
            timeout=5,
        )
    finally:
        for w in waiters:
            w.cancel()
        for w in waiters:
            with contextlib.suppress(asyncio.CancelledError):
                await w
    await _settle()


# --------------------------------------------------------------------------
# SpeechBurst: barge-in is confirmed by ELAPSED TIME, not by event count.
# --------------------------------------------------------------------------

class _Clock:
    """A hand-wound clock, so 300ms of "speech" costs no wall time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_burst_fires_on_elapsed_time_not_on_event_count():
    """The shipped value is barge_in_confirm_ms=300, and the design commits to
    interrupting after ~300ms of speech. Counting interim EVENTS instead --
    300//20 = 15 of them -- means anywhere from 1.5s to 4s of the agent
    talking over the caller, because Deepgram's interim rate is not 50Hz and
    is not something we control."""
    clock = _Clock()
    burst = SpeechBurst(300, clock)

    assert burst.interim() is False        # t=0: window opens
    clock.advance(0.299)
    assert burst.interim() is False        # 299ms: not yet
    clock.advance(0.002)
    assert burst.interim() is True         # 301ms: confirmed, on the 3rd event


def test_burst_does_not_fire_when_interims_stop_before_the_window_elapses():
    """A cough produces a couple of interims and then silence. Nothing is
    scheduled, so it can never fire later -- and the next burst starts its own
    window rather than inheriting this one."""
    clock = _Clock()
    burst = SpeechBurst(300, clock)

    assert burst.interim() is False
    clock.advance(0.05)
    assert burst.interim() is False
    clock.advance(0.05)
    assert burst.interim() is False        # 100ms of speech, then it stops

    clock.advance(30.0)                    # ...half a minute of silence...
    # The next interim only re-reads the ORIGINAL window's start if the
    # window was left open, which is exactly the regression this guards.
    burst.reset()
    assert burst.interim() is False, "a lone later interim must not confirm"


def test_burst_re_arms_after_firing():
    """Deepgram does not reliably re-emit SpeechStarted per utterance, so a
    window left open past a fire turns one later interim into an instant
    interruption of the agent's next reply."""
    clock = _Clock()
    burst = SpeechBurst(300, clock)

    burst.interim()
    clock.advance(0.4)
    assert burst.interim() is True

    clock.advance(10.0)
    assert burst.interim() is False, "counter did not re-arm on fire"


def test_burst_of_many_fast_events_does_not_fire():
    """Fifteen interims in 10ms is a fast talker, not 300ms of speech."""
    clock = _Clock()
    burst = SpeechBurst(300, clock)
    for _ in range(15):
        clock.advance(0.0007)
        assert burst.interim() is False


# --------------------------------------------------------------------------
# _reply: what stops, and what the agent is told it said.
# --------------------------------------------------------------------------

async def test_interrupted_reply_reports_only_complete_sentences_to_hermes():
    audio = FakeAudio()
    tts = FakeTts(frames_per_sentence=5)
    hermes = FakeHermes(["One. ", "Two. ", "Three."])
    interrupt, ended = asyncio.Event(), asyncio.Event()

    def hook(index, count):
        if index == 1 and count == 2:      # halfway through sentence 2
            interrupt.set()

    audio.on_sent_hook = hook
    handler = _handler(tts=tts)

    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=interrupt, utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=ended,
                       deadline=_deadline()),
        timeout=2,
    )
    await _settle()

    assert hermes.appended == [("sid", "One.")]
    assert tts.spoken == ["One.", "Two."]      # "Three." never synthesised
    assert audio.stopped == 1


async def test_uninterrupted_reply_tells_hermes_nothing():
    audio = FakeAudio()
    tts = FakeTts(frames_per_sentence=3)
    hermes = FakeHermes(["One. ", "Two."])
    handler = _handler(tts=tts)

    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=asyncio.Event(),
                       utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=_deadline()),
        timeout=2,
    )
    await _settle()

    assert hermes.appended == []
    assert tts.spoken == ["One.", "Two."]
    assert len(audio.sent) == 6


async def test_interrupted_sender_is_drained_not_cancelled():
    """The frame already being written when the interrupt lands still reaches
    the caller, so it must still be credited.

    Cancelling the send task instead would abandon that frame half-written
    AND lose its on_sent credit -- the only corruption path into the record
    HeardTracker reasons over. Here that difference is the difference between
    sentence 1 being complete (3/3) and being silently downgraded to
    'never heard' (2/3).
    """
    audio = FakeAudio(send_delay=0.005)
    tts = FakeTts(frames_per_sentence=3)
    hermes = FakeHermes(["One. ", "Two."])
    interrupt = asyncio.Event()

    def hook(index, count):
        if index == 0 and count == 2:      # one frame still in flight
            interrupt.set()

    audio.on_sent_hook = hook
    handler = _handler(tts=tts)

    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=interrupt, utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=_deadline()),
        timeout=2,
    )
    await _settle()

    assert audio.cancelled is False, "sender was cancelled mid-write"
    assert audio.finished is True
    assert len(audio.sent) == 3            # the in-flight frame went out
    assert hermes.appended == [("sid", "One.")]   # ...and was credited


async def test_tts_failure_mid_reply_still_records_what_was_heard():
    audio = FakeAudio()
    tts = FailingTts(fail_on_call=2)       # sentence 1 of the reply blows up
    hermes = FakeHermes(["One. ", "Two. ", "Three."])
    handler = _handler(tts=tts)

    # Returns normally -- a TTS failure is not fatal to the call.
    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=asyncio.Event(),
                       utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=_deadline()),
        timeout=2,
    )
    await _settle()

    assert hermes.appended == [("sid", "One.")]
    assert tts.spoken == ["One."]          # "Three." never attempted


async def test_reply_stops_when_the_caller_hangs_up_mid_sentence():
    """A dead call is not an interruption: there is nobody to tell what they
    heard, and continuing to synthesise burns TTS on a channel that is gone."""
    audio = FakeAudio()
    tts = FakeTts(frames_per_sentence=3)
    hermes = FakeHermes(["One. ", "Two. ", "Three."])
    ended = asyncio.Event()

    def hook(index, count):
        if index == 0 and count == 3:
            ended.set()

    audio.on_sent_hook = hook
    handler = _handler(tts=tts)

    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=asyncio.Event(),
                       utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=ended,
                       deadline=_deadline()),
        timeout=2,
    )
    await _settle()

    assert tts.spoken == ["One."]
    assert hermes.appended == []


async def test_barge_in_does_not_wait_for_the_history_correction_to_finish():
    """append_assistant POSTs to /chat, which runs a WHOLE synchronous agent
    turn -- a reply nobody will ever hear, taking as long as any other reply
    and bounded only by the 60s client timeout. Awaiting it in the hot path
    means every interruption freezes the caller for the length of one extra
    LLM turn before the line goes back to LISTENING."""
    audio = FakeAudio()
    tts = FakeTts(frames_per_sentence=5)
    released = asyncio.Event()

    class SlowHermes(FakeHermes):
        async def append_assistant(self, session_id, text) -> None:
            await released.wait()          # stands in for a full agent turn
            await super().append_assistant(session_id, text)

    hermes = SlowHermes(["One. ", "Two. ", "Three."])
    interrupt = asyncio.Event()
    audio.on_sent_hook = lambda i, c: interrupt.set() if (i, c) == (1, 2) else None
    handler = _handler(tts=tts)

    # If credit_heard were awaited, this would time out: released is never set
    # until after _reply has already returned.
    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=interrupt, utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=_deadline()),
        timeout=1,
    )
    assert hermes.appended == []           # still in flight, not yet recorded

    released.set()
    await _settle()
    assert hermes.appended == [("sid", "One.")]


async def test_a_failing_history_correction_does_not_break_the_call():
    audio = FakeAudio()
    tts = FakeTts(frames_per_sentence=5)

    class BrokenHermes(FakeHermes):
        async def append_assistant(self, session_id, text) -> None:
            raise RuntimeError("hermes is down")

    hermes = BrokenHermes(["One. ", "Two. ", "Three."])
    interrupt = asyncio.Event()
    audio.on_sent_hook = lambda i, c: interrupt.set() if (i, c) == (1, 2) else None
    handler = _handler(tts=tts)

    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=interrupt, utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=_deadline()),
        timeout=1,
    )
    await _settle()                        # the failure is swallowed and logged
    assert hermes.appended == []


async def test_reply_is_bounded_by_max_call_seconds():
    """max_call_seconds is only checked BETWEEN turns in handle()'s loop, so
    without a deadline inside _reply a single runaway reply is unbounded: a
    long enough answer (or a stuck sender) keeps the caller on a call that is
    already meant to be over."""
    audio = FakeAudio(send_delay=0.002)
    tts = FakeTts(frames_per_sentence=1000)     # ~2s per sentence
    hermes = FakeHermes(["One. ", "Two. ", "Three."])
    handler = _handler(tts=tts)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(
        handler._reply(audio, _ext(), hermes, "sid", "hi",
                       interrupt=asyncio.Event(),
                       utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=loop.time() + 0.1),
        timeout=2,
    )
    await _settle()

    assert loop.time() - started < 1.5, "reply ran past its deadline"
    assert tts.spoken == ["One."]           # cut off mid-first-sentence
    assert hermes.appended == []            # nothing was fully heard


# --------------------------------------------------------------------------
# handle(): the whole state machine against fakes.
# --------------------------------------------------------------------------

async def test_inbound_audio_still_reaches_stt_while_the_agent_is_speaking():
    """Carry-forward constraint from task 6: Deepgram's endpointing is VAD
    based and only finalises an utterance while audio keeps arriving. If the
    pump paused during playback no final transcript would ever come back and
    the conversation would hang."""
    audio = FakeAudio(frame_delay=0.001, send_delay=0.001)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=20)
    hermes = FakeHermes()

    audio.on_sent_hook = lambda i, c: audio.hangup.set() if c == 20 else None
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    assert stt.connected
    assert any(stt.sent_while_speaking), "STT went deaf while the agent spoke"


async def test_stt_send_timeout_stops_the_pump_and_is_never_retried():
    """Carry-forward constraint from task 6: a send() timeout means the
    websocket is dead. Retrying in place can leave a half-written frame and
    corrupt every later send, so the pump must stop dead."""
    audio = FakeAudio(frame_delay=0.001, send_delay=0.001)
    stt = FakeStt(audio=audio, timeout_at=3)
    tts = FakeTts(frames_per_sentence=10)
    handler = _handler(stt=stt, tts=tts, hermes=FakeHermes(),
                       server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    assert stt.attempts == 4          # 3 good sends, one timeout, then nothing
    assert stt.closed


async def test_caller_hangup_ends_the_call_without_waiting_for_max_call_seconds():
    audio = FakeAudio()
    audio.hangup.set()
    stt = FakeStt(audio=audio)
    hermes = FakeHermes()
    ari = FakeAri()
    handler = _handler(ari=ari, stt=stt, tts=FakeTts(), hermes=hermes,
                       server=FakeServer(audio))

    await asyncio.wait_for(
        handler.handle("chan-1", "7001", _ext(max_call_seconds=600)), timeout=5
    )

    assert ari.destroyed == ["bridge-0"]
    assert ari.hungup == ["as-chan", "chan-1"]
    assert stt.closed and hermes.closed


# --------------------------------------------------------------------------
# Idle hangup: caller silence WHILE LISTENING ends the call.
# --------------------------------------------------------------------------

async def test_a_silent_caller_ends_the_call_instead_of_holding_the_line(caplog):
    """Live defect: the agent said goodbye, the caller said nothing back, and
    the line stayed open for the whole 600s max_call_seconds -- the only
    timeout the loop had -- until they hung up by hand."""
    audio = FakeAudio(frame_delay=0.002)
    stt = FakeStt(audio=audio)
    hermes = FakeHermes()
    ari = FakeAri()
    handler = _handler(ari=ari, stt=stt, tts=FakeTts(), hermes=hermes,
                       server=FakeServer(audio))

    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(
            handler.handle("chan-1", "7001",
                           _ext(idle_hangup_seconds=0.2, max_call_seconds=600)),
            timeout=5,
        )
    elapsed = loop.time() - started

    # 2.0s is far below anything else that could end this call: inbound audio
    # only runs out after MAX_INBOUND * frame_delay = 4s, and
    # max_call_seconds is 600. So this can only pass if the idle timer fired.
    assert elapsed < 2.0, \
        f"the line was held for {elapsed:.2f}s of caller silence"
    assert any("no caller speech" in r.getMessage() for r in caplog.records), \
        "the call ended without saying why"
    assert ari.hungup == ["as-chan", "chan-1"]
    assert hermes.chats == []


async def test_the_idle_clock_does_not_run_while_the_agent_is_working():
    """The clock measures CALLER SILENCE WHILE LISTENING -- not the caller
    being quiet because the agent is talking, filling, or thinking.

    Here one reply takes longer (generation alone) than the whole idle
    window, and the caller then answers well within that window measured
    from the moment the agent stopped. Anchoring the clock anywhere but "the
    bridge started listening again" hangs up on someone who has not been
    given a chance to speak yet.
    """
    idle = 0.4
    thinking = 0.5          # deliberately longer than the idle window
    audio = FakeAudio(frame_delay=0.002, send_delay=0.001)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=4)
    hermes = ScriptedHermes([["Sure thing."], ["Still here too."]],
                            first_delta_delay=thinking)
    phase = {"name": "greeting"}
    scheduled: list[asyncio.Task] = []

    async def answer_after_the_reply() -> None:
        # Comfortably inside the idle window measured from the end of the
        # agent's turn, and comfortably outside it measured from when the
        # caller last spoke -- which is the whole distinction under test.
        await asyncio.sleep(0.15)
        stt.emit(_final("still here"))
        stt.emit(_utt_end())

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 4:
            phase["name"] = "reply-1"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "reply-1" and index == 0 and count == 4:
            phase["name"] = "reply-2"
            scheduled.append(asyncio.create_task(answer_after_the_reply()))

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(
            handler.handle("chan-1", "7001", _ext(idle_hangup_seconds=idle)),
            timeout=10,
        )
    finally:
        for task in scheduled:
            task.cancel()
    await _settle()

    assert hermes.chats == [("call-chan-1", "tell me a story"),
                            ("call-chan-1", "still here")], \
        "the caller was cut off while the agent was still working on the " \
        "previous turn, or immediately after it finished speaking"
    # ...and the timer is still armed afterwards: the call ends on the idle
    # window following the SECOND reply, not on inbound audio running out
    # (4s) or max_call_seconds.
    assert loop.time() - started < 3.0


async def _speaks_just_before_the_deadline(emit_late_speech, idle=0.3):
    """Stay quiet for most of the idle window, then start talking.

    Returns the ScriptedHermes so the caller can assert the utterance was
    answered. `emit_late_speech(stt)` is the sequence of STT events a caller
    who starts speaking near the deadline produces; the FINAL always lands
    AFTER the un-refreshed deadline has passed, which is the whole point --
    with endpointing=300 Deepgram finalises only after a ~300ms pause, so the
    0.5-1.5s in which someone is audibly talking is invisible to any clock
    driven by finals.
    """
    audio = FakeAudio(frame_delay=0.002, send_delay=0.001)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=4)
    hermes = ScriptedHermes([["Go on."]])
    scheduled: list[asyncio.Task] = []

    def hook(index, count):
        # Fires once, on the last frame of the GREETING -- the moment the
        # bridge starts listening and the idle clock starts.
        if index == 0 and count == 4 and not scheduled:
            scheduled.append(asyncio.create_task(emit_late_speech(stt)))

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))
    try:
        await asyncio.wait_for(
            handler.handle("chan-1", "7001",
                           _ext(idle_hangup_seconds=idle,
                                barge_in_confirm_ms=50,
                                max_call_seconds=600)),
            timeout=10,
        )
    finally:
        for task in scheduled:
            task.cancel()
    await _settle()
    return hermes


async def test_a_caller_who_starts_speaking_before_the_idle_deadline_is_not_cut_off():
    """The line must not drop mid-word.

    A caller who says nothing for most of the idle window and then answers is
    NOT idle -- but `utterance_parts` only fills on finals, so for the whole
    0.5-1.5s between the first sound and Deepgram's first final they look
    identical to silence. The deadline expires while they are talking and the
    call ends mid-sentence.
    """
    idle = 0.3

    async def emit(stt):
        await asyncio.sleep(idle * 0.7)
        stt.emit(_speech_started())
        await asyncio.sleep(idle * 0.6)      # now past the stale deadline
        stt.emit(_final("are you still there"))
        stt.emit(_utt_end())

    hermes = await _speaks_just_before_the_deadline(emit, idle=idle)

    assert hermes.chats == [("call-chan-1", "are you still there")], \
        "the line dropped while the caller was speaking"


async def test_interims_alone_hold_the_line_open_for_a_caller_who_is_talking():
    """SpeechStarted cannot be the only thing that counts as voice.

    Deepgram does not reliably re-emit SpeechStarted per utterance (see
    SpeechBurst's own docstring), so on a live call the evidence that someone
    is mid-sentence is usually a run of interims and nothing else.
    """
    idle = 0.3

    async def emit(stt):
        await asyncio.sleep(idle * 0.5)
        stt.emit(_interim("are"))
        await asyncio.sleep(0.1)             # > barge_in_confirm_ms, confirms
        stt.emit(_interim("are you"))
        await asyncio.sleep(idle * 0.5)      # now past the stale deadline
        stt.emit(_final("are you still there"))
        stt.emit(_utt_end())

    hermes = await _speaks_just_before_the_deadline(emit, idle=idle)

    assert hermes.chats == [("call-chan-1", "are you still there")], \
        "the line dropped while the caller was speaking"


# --------------------------------------------------------------------------
# Greeting cache (Task #3): pre-synthesized at startup, shared across calls.
# --------------------------------------------------------------------------

async def test_two_calls_without_a_cached_greeting_each_pay_for_synthesis():
    """Baseline this task changes: with no greeting cache (greeting_frames
    defaults to None -- the behaviour of every StreamingCallHandler built
    before this task), each call pays its own ElevenLabs round trip for the
    exact same config-constant greeting text. This pins the "before" side of
    the before/after this task is supposed to fix."""
    tts = FakeTts()
    audios = [FakeAudio(), FakeAudio()]
    for a in audios:
        a.hangup.set()
    server = SequentialFakeServer(audios)
    handler = _handler(tts=tts, hermes=FakeHermes(), server=server, stt=FakeStt())

    for _ in range(2):
        await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    assert tts.calls.count(("Hello there.", "voice-abc", 0)) == 2


async def test_second_call_reuses_the_cached_greeting_without_a_new_tts_request():
    """The cache __init__ takes (mirroring main.py's startup pre-synthesis)
    must be reused by every call this ONE handler instance serves -- not
    just accepted and then ignored after the first -- proving _speak's cache
    lookup, not merely that the constructor stored the argument. This pins
    the "after" side: two calls, zero greeting synthesis requests."""
    tts = FakeTts()
    greeting_frames = [(b"cached-greeting-audio", 0)] * 4
    audios = [FakeAudio(), FakeAudio()]
    for a in audios:
        a.hangup.set()
    server = SequentialFakeServer(audios)
    handler = _handler(tts=tts, hermes=FakeHermes(), server=server, stt=FakeStt(),
                       greeting_frames=greeting_frames)

    for _ in range(2):
        await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    assert tts.calls == [], "cached frames must be reused, never re-synthesized"
    # Both calls actually played the SAME cached audio (all 4 frames of it)
    # -- proving the cache was read, not merely that synthesis was silently
    # skipped.
    assert audios[0].spoken_text == ["cached-greeting-audio"] * 4
    assert audios[1].spoken_text == ["cached-greeting-audio"] * 4


async def test_startup_synthesis_failure_falls_back_to_per_call_synthesis():
    """greeting_frames=None is exactly what a failed startup pre-synthesis
    produces (see main.py's try/except around it, tested separately in
    test_main.py). This drives a REAL call all the way through handle() with
    that None cache and proves the caller still hears a greeting via
    per-call synthesis -- not merely that _speak takes some branch on a
    mock. This is the one that protects availability: a cache failure must
    degrade gracefully, never go mute."""
    tts = FakeTts()
    audio = FakeAudio()
    audio.hangup.set()
    ari = FakeAri()
    handler = _handler(ari=ari, tts=tts, hermes=FakeHermes(), stt=FakeStt(),
                       server=FakeServer(audio), greeting_frames=None)

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    assert tts.calls == [("Hello there.", "voice-abc", 0)], \
        "with no cache the greeting must still be synthesized per call"
    assert audio.spoken_text == ["Hello there."] * 4, \
        "the caller must still actually hear the greeting"
    # ...and the call completed normally: teardown ran, nothing hung.
    assert ari.destroyed == ["bridge-0"]
    assert ari.hungup == ["as-chan", "chan-1"]


async def test_a_greeting_installed_between_calls_is_used_by_the_next(
):
    """The startup greeting synthesis is no longer awaited before the ARI
    event loop starts (main.py's _prepare_streaming_speech: it was holding
    the already-live extension 7000 out of service for ~1.6s), so the frames
    can land while this handler is already serving calls -- between two of
    them, not necessarily before the first one. A constructor argument
    cannot express that -- it is a snapshot taken before the first call --
    hence set_greeting_frames.

    Drives two REAL calls around the install, with the install happening
    only after the first call has fully completed and before the second
    starts -- the two calls here run sequentially, never concurrently, so
    this proves "installed between calls", not "installed mid-call": the
    first must fall back to per-call synthesis, the second must play the
    installed frames and issue no synthesis request at all. Asserting on
    what the caller HEARD, not on the attribute, is the point -- a setter
    that stores somewhere _speak never reads would pass an attribute
    check."""
    tts = FakeTts()
    audios = [FakeAudio(), FakeAudio()]
    for a in audios:
        a.hangup.set()
    handler = _handler(tts=tts, hermes=FakeHermes(), stt=FakeStt(),
                       server=SequentialFakeServer(audios),
                       greeting_frames=None)  # nothing synthesized yet

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    assert tts.calls == [("Hello there.", "voice-abc", 0)], \
        "the mid-preparation call must fall back to per-call synthesis"

    handler.set_greeting_frames([(b"late-greeting-audio", 0)] * 4)

    await asyncio.wait_for(handler.handle("chan-2", "7001", _ext()), timeout=5)

    assert tts.calls == [("Hello there.", "voice-abc", 0)], \
        "the later call must reuse the installed frames, not re-synthesize"
    assert audios[1].spoken_text == ["late-greeting-audio"] * 4
    # The earlier call is untouched by the late arrival -- it heard the
    # freshly synthesized greeting, in full.
    assert audios[0].spoken_text == ["Hello there."] * 4


async def test_audiosocket_is_originated_into_the_configured_ari_app():
    audio = FakeAudio()
    audio.hangup.set()
    ari = FakeAri()
    server = FakeServer(audio)
    handler = _handler(ari=ari, stt=FakeStt(), tts=FakeTts(),
                       hermes=FakeHermes(), server=server, ari_app="phonesys-app")

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    host, port, call_uuid, app = ari.originated[0]
    assert (host, port, app) == ("host.docker.internal", 9010, "phonesys-app")
    assert server.waited[0][0] == call_uuid          # same UUID both sides
    assert server.waited[0][1] > 0                   # and an explicit timeout
    assert ari.added == [("bridge-0", "chan-1"), ("bridge-0", "as-chan")]


async def test_audiosocket_originates_to_the_extensions_configured_host_and_port():
    """A non-default audiosocket_host/audiosocket_port on ExtensionConfig
    must actually reach the origination call -- not be shadowed by a value
    fixed at handler-construction time (task-9 fix round 1, finding 2)."""
    audio = FakeAudio()
    audio.hangup.set()
    ari = FakeAri()
    server = FakeServer(audio)
    handler = _handler(ari=ari, stt=FakeStt(), tts=FakeTts(),
                       hermes=FakeHermes(), server=server)
    ext = _ext(audiosocket_host="alt-audiosocket-host", audiosocket_port=9099)

    await asyncio.wait_for(handler.handle("chan-1", "7001", ext), timeout=5)

    host, port, call_uuid, app = ari.originated[0]
    assert (host, port) == ("alt-audiosocket-host", 9099)


async def test_full_call_final_transcript_reply_and_barge_in():
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=5)
    hermes = FakeHermes(["Alpha one. ", "Beta two. ", "Gamma three."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 5:
            phase["name"] = "first_reply"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "first_reply" and index == 1 and count == 2:
            phase["name"] = "second_reply"
            stt.emit(_final("never mind"))
            stt.emit(_utt_end())
        elif phase["name"] == "second_reply" and index == 2 and count == 5:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    await _settle()

    assert hermes.sessions == ["call-chan-1"]
    assert hermes.chats == [("call-chan-1", "tell me a story"),
                            ("call-chan-1", "never mind")]
    assert hermes.appended == [("call-chan-1", "Alpha one.")]
    assert tts.spoken == ["Hello there.",
                          "Alpha one.", "Beta two.",
                          "Alpha one.", "Beta two.", "Gamma three."]


async def test_sustained_interim_speech_interrupts_before_any_final_arrives():
    """Barge-in must not wait for a final transcript -- that is a second or
    more of the agent talking over the caller. Interims sustained for
    barge_in_confirm_ms of WALL CLOCK are the confirmation signal."""
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=40)      # ~120ms of speech per sentence
    hermes = FakeHermes(["Alpha one. ", "Beta two. ", "Gamma three."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 40:
            phase["name"] = "reply"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "reply" and index == 1:
            if count == 2:
                stt.emit(SttEvent(kind="speech_started"))
                stt.emit(_interim("wait"))     # window opens here
                # Inbound audio stops now, so the call winds down as soon as
                # the interrupted reply returns.
                audio.hangup.set()
            elif count in (7, 12, 17, 22, 27, 32):
                stt.emit(_interim("wait a"))   # ...and keeps arriving

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    # 40ms of sustained interims confirms; the frames above are 3ms apart, so
    # the window elapses partway through the second sentence.
    await asyncio.wait_for(
        handler.handle("chan-1", "7001", _ext(barge_in_confirm_ms=40)), timeout=5
    )
    await _settle()

    assert hermes.appended == [("call-chan-1", "Alpha one.")]
    assert tts.spoken == ["Hello there.", "Alpha one.", "Beta two."]
    # No final transcript ever arrived, so no second reply was started.
    assert hermes.chats == [("call-chan-1", "tell me a story")]


async def test_barge_in_at_the_shipped_confirm_value_of_300ms():
    """Every other barge-in test uses 40 or 60ms, which is how a threshold
    that counted interim EVENTS rather than elapsed time survived review: at
    the SHIPPED barge_in_confirm_ms=300 it needed 15 interim events, which at
    a realistic interim rate is seconds of the agent talking over the caller.

    Here interims arrive every ~60ms. Confirmation must land ~300ms after the
    first one -- about 6 interims, not 15."""
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=150)     # ~450ms of speech per sentence
    hermes = FakeHermes(["Alpha one. ", "Beta two. ", "Gamma three."])
    phase = {"name": "greeting"}
    interims = []

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 150:
            phase["name"] = "reply"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "reply" and index == 1:
            if count == 5:
                audio.hangup.set()
            if count >= 5 and (count - 5) % 20 == 0:
                interims.append(count)
                stt.emit(_interim("hold on a moment"))

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    ext = _ext()
    assert ext.barge_in_confirm_ms == 300, "this test is about the shipped value"
    await asyncio.wait_for(handler.handle("chan-1", "7001", ext), timeout=10)
    await _settle()

    # Cut off during the SECOND sentence, so the first is credited as heard.
    assert tts.spoken == ["Hello there.", "Alpha one.", "Beta two."]
    assert hermes.appended == [("call-chan-1", "Alpha one.")]
    # The event-counting implementation needed 15 interims and would have run
    # clean past the end of this sentence.
    assert len(interims) < 15, f"took {len(interims)} interim events to confirm"


async def test_a_lone_interim_after_a_barge_in_does_not_cut_off_the_next_reply():
    """The confirm counter must re-arm when it fires.

    Deepgram does not reliably re-emit SpeechStarted for every utterance, so
    a counter left sitting on the threshold turns the next single interim --
    a cough, a chair creak -- into an instant interruption.

    The final transcript is delivered BEFORE the interims that cross the
    threshold, so the only thing that can re-arm the window here is the
    firing itself -- otherwise the single later interim is measured against a
    window opened long ago and cuts the reply off at its first sentence.
    """
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=40)
    hermes = FakeHermes(["Alpha one. ", "Beta two. ", "Gamma three."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting":
            # All of this lands during the (non-interruptible) greeting.
            if count == 2:
                stt.emit(_final("tell me a story"))
                stt.emit(_utt_end())
            elif count in (10, 15, 20, 25):
                stt.emit(_interim("wait"))      # 45ms of interims: the 40ms
                                                # window elapses and fires
            elif count == 40:
                phase["name"] = "reply"
        elif phase["name"] == "reply" and index == 0 and count == 2:
            phase["name"] = "coughed"
            stt.emit(_interim("uh"))            # ONE interim, no speech_started
        elif phase["name"] == "coughed" and index == 2 and count == 40:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(
        handler.handle("chan-1", "7001", _ext(barge_in_confirm_ms=40)), timeout=5
    )
    await _settle()

    assert hermes.appended == []               # the reply was never cut off
    assert hermes.chats == [("call-chan-1", "tell me a story")]
    assert tts.spoken == ["Hello there.", "Alpha one.", "Beta two.", "Gamma three."]


async def test_interims_before_a_final_do_not_carry_over_into_the_next_reply():
    """Consuming a final transcript also re-arms the window: the interims that
    preceded it belong to the utterance just finished, not to the next one.

    Without the reset, the "mm"/"mhm" during the reply would be measured
    against a window opened ~150ms earlier during the greeting and would cut
    the reply off instantly."""
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=40)
    hermes = FakeHermes(["Alpha one. ", "Beta two. ", "Gamma three."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0:
            if count == 2:
                stt.emit(_interim("tell"))      # window opens...
            elif count == 5:
                stt.emit(_interim("tell me"))   # ...9ms later, still open
            elif count == 8:
                stt.emit(_final("tell me a story"))   # reset happens here
                stt.emit(_utt_end())
            elif count == 40:
                phase["name"] = "reply"
        elif phase["name"] == "reply" and index == 0:
            if count == 2:
                stt.emit(_interim("mm"))        # a fresh 9ms window,
            elif count == 5:
                stt.emit(_interim("mhm"))       # nowhere near 60ms
                phase["name"] = "murmuring"
        elif phase["name"] == "murmuring" and index == 2 and count == 40:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(
        handler.handle("chan-1", "7001", _ext(barge_in_confirm_ms=60)), timeout=5
    )
    await _settle()

    assert hermes.appended == []               # never interrupted
    assert tts.spoken == ["Hello there.", "Alpha one.", "Beta two.", "Gamma three."]


# --------------------------------------------------------------------------
# utterance_end: when the caller has actually finished a thought.
# --------------------------------------------------------------------------

async def test_two_finals_separated_by_a_pause_are_one_utterance():
    """endpointing=300 makes Deepgram finalise at every ~300ms pause, which is
    mid-thought for anyone who says "I'd like to book... a flight tomorrow".
    Replying on the first final means answering half a sentence -- the agent
    interrupting the CALLER. Finals accumulate until UtteranceEnd, which is
    what utterance_end_ms=1000 is requested for."""
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=40)
    hermes = FakeHermes(["Sure thing."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0:
            if count == 5:
                stt.emit(_final("I'd like to book"))
            elif count == 25:                  # ...a 60ms pause, then more
                stt.emit(_final("a flight tomorrow"))
            elif count == 30:                  # only ONE utterance_end
                stt.emit(_utt_end())
            elif count == 40:
                phase["name"] = "reply"
        elif phase["name"] == "reply" and index == 0 and count == 40:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    await _settle()

    assert hermes.chats == [("call-chan-1", "I'd like to book a flight tomorrow")]
    assert tts.spoken == ["Hello there.", "Sure thing."]


async def test_a_final_still_stops_the_agent_before_utterance_end_arrives():
    """Gating the REPLY on utterance_end must not delay the STOP. A final
    transcript means the caller is definitely talking, so the agent goes quiet
    at once and only starts thinking when the caller is done."""
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=40)
    hermes = FakeHermes(["Alpha one. ", "Beta two. ", "Gamma three."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 40:
            phase["name"] = "reply"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "reply" and index == 1 and count == 2:
            phase["name"] = "stopped"
            stt.emit(_final("actually never mind"))   # no utterance_end yet
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    await _settle()

    # The agent stopped mid-"Beta two." ...
    assert tts.spoken == ["Hello there.", "Alpha one.", "Beta two."]
    assert hermes.appended == [("call-chan-1", "Alpha one.")]
    # ...but did NOT start a second reply, because the caller had not finished.
    assert hermes.chats == [("call-chan-1", "tell me a story")]


async def test_a_missing_utterance_end_eventually_still_gets_a_reply(monkeypatch):
    """Safety net: if UtteranceEnd is dropped, the caller must not be left
    talking to an agent that has banked their words and gone permanently
    silent."""
    monkeypatch.setattr("phonesys.stream_call.UTTERANCE_END_FALLBACK", 0.05)
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=20)
    hermes = FakeHermes(["Sure thing."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 20:
            phase["name"] = "reply"
            stt.emit(_final("are you there"))   # and no utterance_end, ever
        elif phase["name"] == "reply" and index == 0 and count == 20:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    await _settle()

    assert hermes.chats == [("call-chan-1", "are you there")]
    assert UTTERANCE_END_FALLBACK >= 3.0, "the shipped fallback must not race "\
                                          "a real 1s utterance_end_ms"


async def test_a_later_turns_reply_survives_a_bare_burst_in_its_thinking_window():
    """handle() must clear `final_seen` before EVERY reply, not just the first.

    speak_sentence's one-shot guard is `index == 0 and not
    utterance_ready.is_set() and not final_seen.is_set()`. Every utterance is
    triggered by a final transcript and pump_stt sets `final_seen` on that same
    final, so if handle() stops clearing it the flag LATCHES on the first final
    of the call: the guard is False forever, the one-shot `interrupt.clear()`
    never runs again, and any bare interim burst landing in a later thinking
    window leaves `interrupt` armed into `waiter = create_task(interrupt.
    wait())`, which resolves on its first pass. That reply is cut to a single
    ~20ms frame -- effective silence -- and so is every reply after it.

    Deliberately MULTI-TURN and through handle(). Every other guard test in
    this file goes through _run_one_turn, which builds fresh Events per turn
    and calls _reply directly, so handle()'s clearing is structurally invisible
    to them: deleting `final_seen.clear()` leaves the whole suite green. Turn 1
    is deliberately clean (no burst) so a failure here can only be the latched
    flag, never the guard itself.
    """
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=10)
    hermes = ScriptedHermes([["First answer."], ["Second answer."]],
                            first_delta_delay=0.20)
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 10:
            phase["name"] = "turn1"
            stt.emit(_final("first question"))
            stt.emit(_utt_end())
        elif phase["name"] == "turn1" and index == 0 and count == 10:
            phase["name"] = "turn2"            # turn 1 played out in full
            stt.emit(_final("second question"))
            stt.emit(_utt_end())

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    async def noise_in_turn_2s_thinking_window() -> None:
        # chat_stream's body only runs once _next_delta awaits the first
        # anext(), so a second entry in chats means turn 2's thinking window
        # has genuinely started.
        await _until(lambda: len(hermes.chats) == 2)
        # A BARE interim burst: line noise, no final behind it, so `final_seen`
        # is legitimately clear and the guard is entitled to fire. Two interims
        # 30ms apart confirm against barge_in_confirm_ms=20.
        await asyncio.sleep(0.02)
        stt.emit(_interim("shh"))
        await asyncio.sleep(0.03)
        stt.emit(_interim("shh"))
        # Well past turn 2's first delta (0.20s) and its 10 frames of audio.
        await asyncio.sleep(0.40)
        audio.hangup.set()

    driver = asyncio.create_task(noise_in_turn_2s_thinking_window())
    try:
        await asyncio.wait_for(
            handler.handle("chan-1", "7001", _ext(barge_in_confirm_ms=20)),
            timeout=5,
        )
    finally:
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await driver
    await _settle()

    assert hermes.chats == [("call-chan-1", "first question"),
                            ("call-chan-1", "second question")]
    assert audio.spoken_text.count("First answer.") == 10, (
        "turn 1 was already truncated -- this test can say nothing about "
        "later turns until turn 1 is clean"
    )
    assert audio.spoken_text.count("Second answer.") == 10, (
        "turn 2's reply was truncated by a bare interim burst -- `final_seen` "
        "latched set from an earlier turn and permanently disabled "
        "speak_sentence's one-shot interrupt.clear() guard"
    )


async def test_filler_plays_through_handle_with_interrupt_armed_by_the_final():
    """Filler, driven through the real handle() state machine.

    Every other filler test calls _reply directly (via _run_one_turn) with its
    own freshly-built Events, so `interrupt` is always CLEAR on entry -- the
    opposite of production, where the final transcript that triggered the turn
    set it. handle()'s `if interrupt.is_set(): interrupt.clear()` is the only
    thing that restores that precondition, and _reply's
    silence_filler_on_interrupt is nothing but `await interrupt.wait()` before
    filler_stop.set() + audio.stop_sending(). Leaving the flag armed therefore
    kills filler on the first scheduling pass of EVERY reply -- 100% dead in
    production, restoring the whole 4-10s of dead air the feature exists to
    remove -- while the suite stays green, because no filler test ever reaches
    handle() at all. speak_sentence's `index == 0` guard is no help: it runs
    after `await stop_filler()`.
    """
    bank = FakeBank(frame_count=5)
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=8)
    hermes = FakeHermes(["Real answer."], first_delta_delay=0.25)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 8:
            phase["name"] = "reply"
            # A real final transcript -- which arms `interrupt` AND
            # `final_seen` in pump_stt, exactly as production does -- followed
            # by its UtteranceEnd. This is the only way to enter _reply the
            # way a live call does.
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "reply" and index == 0 and count == 8:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio),
                       filler_bank=bank)

    await asyncio.wait_for(handler.handle("chan-1", "7001", ext), timeout=5)
    await _settle()

    assert bank.taken, (
        "no filler phrase was ever taken on a 0.25s thinking window -- "
        "`interrupt` was still armed at _reply entry and "
        "silence_filler_on_interrupt stopped filler before it began"
    )
    assert [i for _, i in audio.sent if i < 0], (
        "filler frames never reached the socket"
    )
    assert tts.spoken == ["Hello there.", "Real answer."]


async def test_a_bare_utterance_end_after_a_stalled_reply_does_not_abandon_it(monkeypatch):
    """pump_stt's `if utterance_parts:` guard, exercised on its reachable path.

    `utterance_ready` is the one signal _next_delta races the SSE stream
    against, so an UtteranceEnd landing in the thinking window abandons the
    in-flight reply -- silently: no crash, no hangup, handle() just loops back
    to listening and the caller's question is never answered.

    The reachable case is NOT a duplicate UtteranceEnd. It is the
    UTTERANCE_END_FALLBACK stall: handle() replies with no UtteranceEnd yet
    seen and clears `utterance_parts` on the way, so the real UtteranceEnd --
    whenever it finally turns up -- arrives with parts empty, on exactly the
    turn already having turn-detection trouble. The mirror case (a bare final
    during the thinking window) is already tested; this is the asymmetry.
    """
    monkeypatch.setattr("phonesys.stream_call.UTTERANCE_END_FALLBACK", 0.05)
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=10)
    hermes = FakeHermes(["Sure thing."], first_delta_delay=0.20)
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 10:
            phase["name"] = "stalling"
            stt.emit(_final("are you there"))   # ...and no UtteranceEnd behind
                                                # it, so the fallback fires

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    async def late_utterance_end() -> None:
        # A non-empty chats list means the stall fallback has already replied
        # -- and therefore already emptied utterance_parts.
        await _until(lambda: hermes.chats != [])
        await asyncio.sleep(0.05)          # 0.05s into a 0.20s thinking window
        stt.emit(_utt_end())               # the real UtteranceEnd, arriving
                                           # late and with nothing behind it
        await asyncio.sleep(0.40)          # let the reply play out in full
        audio.hangup.set()

    driver = asyncio.create_task(late_utterance_end())
    try:
        await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    finally:
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await driver
    await _settle()

    assert hermes.chats == [("call-chan-1", "are you there")]
    assert tts.spoken == ["Hello there.", "Sure thing."], (
        "the reply was abandoned before a single delta arrived -- a bare "
        "UtteranceEnd with no banked transcript behind it must not set "
        "utterance_ready, or the stalled turn's question goes unanswered"
    )
    assert audio.spoken_text.count("Sure thing.") == 10, (
        "the reply reached the caller only partly -- the late UtteranceEnd "
        "abandoned it mid-flight"
    )


async def test_transcription_dying_apologizes_aloud_instead_of_dropping_the_call():
    """The design's error table says apologize and end. Dropping the line
    silently leaves the caller talking to dead air, assuming the agent is
    thinking."""
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=5)
    hermes = FakeHermes()
    ari = FakeAri()
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 5:
            phase["name"] = "deaf"
            stt.emit(None)                     # the Deepgram websocket ends

    audio.on_sent_hook = hook
    handler = _handler(ari=ari, stt=stt, tts=tts, hermes=hermes,
                       server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    assert tts.spoken == ["Hello there.", STT_FAILURE_MESSAGE]
    assert ari.destroyed == ["bridge-0"]       # ...and the call ended cleanly
    assert ari.hungup == ["as-chan", "chan-1"]


async def test_caller_hangup_does_not_apologize_to_an_empty_line():
    """The apology is for a dead transcriber, not a dead caller: synthesising
    speech for a channel nobody is on is pure cost and delays teardown."""
    audio = FakeAudio()
    audio.hangup.set()
    stt = FakeStt(audio=audio)
    tts = FakeTts()
    handler = _handler(stt=stt, tts=tts, hermes=FakeHermes(),
                       server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)

    assert tts.spoken == ["Hello there."]


async def test_cancelling_the_call_still_destroys_the_bridge_and_channels():
    audio = FakeAudio()
    stt = FakeStt(audio=audio)
    hermes = FakeHermes()
    ari = FakeAri()
    handler = _handler(ari=ari, stt=stt, tts=FakeTts(frames_per_sentence=50),
                       hermes=hermes, server=FakeServer(audio))

    task = asyncio.create_task(handler.handle("chan-1", "7001", _ext()))
    await _until(lambda: stt.attempts > 0)     # the call is genuinely running
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ari.destroyed == ["bridge-0"]
    assert ari.hungup == ["as-chan", "chan-1"]
    assert stt.closed and hermes.closed


async def test_repeated_cancellation_still_completes_teardown():
    """An enclosing wait_for or TaskGroup can cancel more than once. Every
    teardown step must still run, or Asterisk is left holding a bridge and
    two channels for a call that no longer exists."""
    audio = FakeAudio()
    stt = FakeStt(audio=audio)
    hermes = FakeHermes()
    ari = FakeAri()
    handler = _handler(ari=ari, stt=stt, tts=FakeTts(frames_per_sentence=50),
                       hermes=hermes, server=FakeServer(audio))

    task = asyncio.create_task(handler.handle("chan-1", "7001", _ext()))
    await _until(lambda: stt.attempts > 0)
    for _ in range(5):
        task.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ari.destroyed == ["bridge-0"]
    assert ari.hungup == ["as-chan", "chan-1"]
    assert stt.closed and hermes.closed


async def test_tts_failure_mid_call_returns_to_listening_instead_of_hanging_up():
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FailingTts(fail_on_call=3)     # greeting, reply-1 s0, then boom
    hermes = FakeHermes(["Alpha one. ", "Beta two. ", "Gamma three."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 3:
            phase["name"] = "first_reply"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "first_reply" and index == 0 and count == 3:
            phase["name"] = "second_reply"
            stt.emit(_final("try again"))
            stt.emit(_utt_end())
        elif phase["name"] == "second_reply" and index == 2 and count == 3:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=3)
    await _settle()

    # What the caller did hear before the failure is still on the record...
    assert hermes.appended == [("call-chan-1", "Alpha one.")]
    # ...and the call went back to listening rather than ending.
    assert hermes.chats == [("call-chan-1", "tell me a story"),
                            ("call-chan-1", "try again")]
    assert tts.spoken == ["Hello there.", "Alpha one.",
                          "Alpha one.", "Beta two.", "Gamma three."]


@pytest.mark.parametrize("exc", [
    HermesError("chat_stream failed: HTTP 502"),
    httpx.ReadError("connection reset while reading the SSE stream"),
])
async def test_a_hermes_stream_failure_apologizes_and_returns_to_listening(exc):
    """A dead reply stream must not take the call down with it.

    HermesClient.chat_stream raises HermesError on any status >= 400 from
    inside the generator body, and -- unlike stream_tts.py, which deliberately
    wraps httpx's errors into TtsStreamError -- it does not wrap httpx at all,
    so a ReadError/ReadTimeout mid-SSE arrives as itself. Both surface from
    nxt.result() in _next_delta. With no arm for them in _reply they escaped
    into handle()'s generic `except Exception`, which tears down the bridge and
    hangs up both channels: the caller was dropped mid-conversation over a
    restarted api_server or a proxy 502 that the very next request survives.

    The design's error table says "Hermes stream errors -> apologize aloud,
    return to LISTENING" (2026-08-04-barge-in-streaming-design.md), which is
    what this asserts: the sentence already heard is still credited, an apology
    is spoken, and the caller's NEXT question is answered on the same call.
    """
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=3)
    hermes = ExplodingHermes([["Alpha one. "], ["Beta two."]],
                             fail_on_chat=1, exc=exc)
    ari = FakeAri()
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 3:
            phase["name"] = "dying_reply"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())
        elif phase["name"] == "dying_reply" and index == 0 and count == 3:
            phase["name"] = "apology"          # "Alpha one." is fully out;
                                               # the stream dies on the next
                                               # delta
        elif phase["name"] == "apology" and index == 0 and count == 3:
            phase["name"] = "second_reply"     # the apology is fully out
            stt.emit(_final("try again"))
            stt.emit(_utt_end())
        elif phase["name"] == "second_reply" and index == 0 and count == 3:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(ari=ari, stt=stt, tts=tts, hermes=hermes,
                       server=FakeServer(audio))

    await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    await _settle()

    assert tts.spoken == ["Hello there.", "Alpha one.",
                          HERMES_FAILURE_MESSAGE, "Beta two."], (
        "the call did not survive the stream failure -- it must apologize "
        "aloud and go back to listening, not drop the caller"
    )
    assert hermes.chats == [("call-chan-1", "tell me a story"),
                            ("call-chan-1", "try again")], (
        "the caller's next question was never answered -- the call ended "
        "instead of returning to LISTENING"
    )
    # What the caller actually got to hear before the stream died is still on
    # the record, exactly as on the TTS-failure path.
    assert hermes.appended == [("call-chan-1", "Alpha one.")]
    assert hermes.stream_closed, "the SSE stream must be closed, not leaked"


async def test_hermes_stream_failure_stops_filler_before_the_apology():
    """The one-writer invariant, for the Hermes-failure arm specifically.

    The apology spoken in the `except (HermesError, httpx.HTTPError)` arm
    (stream_call.py:894) is a SECOND writer on the socket. `await
    stop_filler()` at stream_call.py:916 -- called BEFORE the apology, not
    merely via `_reply`'s own `finally: await stop_filler()` below -- is
    what stops filler from writing underneath it.

    test_a_hermes_stream_failure_apologizes_and_returns_to_listening above
    never exercises this: it passes no filler_bank, so filler never starts
    and the guarded call is a no-op there. This test gives the stream a
    partial delta ("Alpha", no terminal punctuation) so no sentence ever
    completes and speak_sentence's OWN stop_filler() call (line 747) is
    never reached either -- filler is still the only thing running when the
    stream dies, and the guarded call at :916 is the first and only thing
    that has ever tried to stop it. first_delta_delay is the "thinking
    window": long enough that filler's filler_delay_ms has already elapsed
    and it is genuinely mid-send (not merely scheduled) when the failure
    lands. Without :916, filler keeps writing, unstopped, for the entire
    span the apology is also writing -- a guaranteed overlap, not a race.
    """
    bank = FakeBank(frame_count=200)   # a real filler phrase is dozens of
                                        # frames; long enough to still be
                                        # mid-send when the stream dies
    audio = FakeAudio(track_concurrency=True)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = ExplodingHermes([["Alpha"]], fail_on_chat=1,
                             first_delta_delay=0.05)
    h = _handler(hermes=hermes, filler_bank=bank)

    await _run_one_turn(h, ext, audio=audio)

    assert bank.taken, "filler never played; test proves nothing"
    assert audio.max_concurrent_senders == 1, (
        "filler and the hermes-failure apology wrote to the socket at the "
        "same time -- the one-writer invariant was violated"
    )


# --------------------------------------------------------------------------
# Filler: escalating stall phrases spoken while Hermes generates a reply.
# --------------------------------------------------------------------------

async def test_no_filler_when_the_reply_is_fast():
    """Under the threshold the caller must hear nothing extra -- this is the
    common case for short replies."""
    bank = FakeBank()
    ext = _ext(filler_delay_ms=200, filler_repeat_ms=200)
    hermes = FakeHermes(deltas=["Quick answer."])
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken == [], "filler played despite a fast reply"


async def test_filler_plays_when_the_reply_is_slow():
    bank = FakeBank()
    ext = _ext(filler_delay_ms=20, filler_repeat_ms=20)
    hermes = FakeHermes(deltas=["Slow answer."], first_delta_delay=0.20)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken, "no filler played during a slow reply"


async def test_filler_escalates_tiers_and_holds_at_the_last():
    bank = FakeBank(tiers=2)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken[:2] == [0, 1]
    assert set(bank.taken[2:]) <= {1}, "escalation ran past the highest tier"


async def test_filler_is_never_credited_to_the_caller_history():
    """THE invariant: Hermes did not generate 'Let me think about that.'
    Crediting it corrupts the transcript HeardTracker exists to keep honest.

    This must depend on _discard_frame ITSELF, not on Task 1's
    FILLER_SENTENCE_INDEX sentinel: HeardTracker.heard_text only ever reads
    _sent[i] for i in range(len(sentences)), so a frame tagged with the real
    -1 sentinel is invisible to it no matter which on_sent callback wrote it
    -- swapping _discard_frame for tracker.record_sent against a -1-tagged
    bank passes cleanly and proves nothing (confirmed by mutation, see the
    fix report). To pressure-test _discard_frame's OWN protection,
    independent of that sentinel, this bank tags its frames with sentence
    index 1 -- as if the sentinel protection did not exist -- so a bug that
    let filler frames reach the real HeardTracker inflates sentence 1's
    ("Real two.") credited frame count before that sentence is ever spoken
    for real, and the sentence that gets genuinely interrupted mid-flight
    would be falsely credited as fully heard.
    """
    bank = FakeBank(frame_count=20, sentence_index=1)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Real one. ", "Real two. ", "Real three."],
                        first_delta_delay=0.30)
    # Slow audio widens the send window for each sentence (4 frames x 50ms =
    # 200ms) so a real-clock interrupt can land deterministically -- inside
    # sentence 2, well clear of sentence 1 finishing and sentence 3 starting.
    audio = FakeAudio(send_delay=0.05)
    h = _handler(hermes=hermes, filler_bank=bank)

    await _run_one_turn(h, ext, audio=audio, interrupt_after=0.65)

    assert bank.taken, "the test is meaningless if filler never actually played"
    assert hermes.appended, "the test is meaningless if nothing was credited"
    for _, text in hermes.appended:
        assert text == "Real one.", (
            "credited text must be exactly the one sentence fully heard -- "
            "anything else means filler leaked into a real sentence's credit"
        )


async def test_filler_is_drained_before_the_first_real_sentence():
    """send_frames sets _sending=True on entry and stop_sending() is
    session-wide, so two concurrent writers interleave frames AND stop each
    other. There must never be two send_frames in flight.

    frame_count=500 (~500ms of audio) is deliberate: a real filler phrase is
    1-2s long, so in production it is normally STILL PLAYING when the reply's
    first delta arrives. A single tiny frame finishes in ~1ms out of every
    ~11ms cycle, so a broken implementation that never calls stop_filler()
    could still, by sheer luck, almost never have a filler send in flight
    when the real sender starts -- confirmed by mutation to pass 20/20 runs
    against that exact bug (see the fix report). A long-running phrase
    removes the luck.
    """
    bank = FakeBank(frame_count=500)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.15)
    audio = FakeAudio(track_concurrency=True)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext, audio=audio)
    assert bank.taken, "filler never played; test proves nothing"
    assert audio.max_concurrent_senders <= 1


async def test_an_empty_bank_behaves_exactly_as_before():
    bank = FakeBank(empty=True)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.10)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken == []


async def test_filler_delay_of_zero_creates_no_filler_task():
    bank = FakeBank()
    ext = _ext(filler_delay_ms=0)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.10)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken == []


async def test_interrupt_stops_filler_audio_immediately():
    """silence_filler_on_interrupt must be the thing that stops filler
    promptly -- not merely "some call to stop_sending happened somewhere in
    the whole reply", which would be true even with interrupt_watch never
    scheduled at all: stop_filler()'s own unconditional audio.stop_sending()
    call, reached once the real delta eventually arrives, would satisfy a
    bare "stop_sending was called >= 1" assertion regardless of how late it
    came. This drives the timeline directly instead: a long-running filler
    phrase (frame_count=500) is still actively sending well past where the
    interrupt fires, so if audio.sent stops growing within a couple of frame
    intervals of interrupt.set() -- long before the 0.40s first delta could
    otherwise have stopped it via speak_sentence's own stop_filler() call --
    that can only be interrupt_watch's doing.
    """
    bank = FakeBank(frame_count=500)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.40)
    audio = FakeAudio()
    interrupt = asyncio.Event()
    ended = asyncio.Event()
    handler = _handler(hermes=hermes, filler_bank=bank)

    reply_task = asyncio.create_task(
        handler._reply(audio, ext, hermes, "sid", "hi",
                       interrupt=interrupt, utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=ended,
                       deadline=_deadline())
    )
    await _until(lambda: bank.taken)          # filler is genuinely playing
    await asyncio.sleep(0.05)
    interrupt.set()
    await asyncio.sleep(0.02)                 # comfortably more than one
                                               # 1ms frame interval
    stopped_at = len(audio.sent)
    await asyncio.sleep(0.05)                 # still well before t=0.40s
    assert len(audio.sent) == stopped_at, (
        "filler kept sending after the interrupt -- "
        "silence_filler_on_interrupt did not stop it promptly"
    )

    await asyncio.wait_for(reply_task, timeout=2)
    await _settle()


async def test_filler_is_stopped_even_when_no_sentence_is_ever_spoken():
    """finally: await stop_filler() is the ONLY thing that can stop filler
    when the reply's deadline is already gone before the FIRST sentence is
    ever spoken: speak_sentence's own stop_filler() call never runs in that
    case, because speak_sentence itself is never called -- _TimeLimit fires
    in _reply's own "async for delta" loop, strictly before the sentence
    loop that would call speak_sentence. Without the finally, filler would
    be abandoned mid-utterance: still calling bank.take() and writing audio
    for as long as Hermes keeps streaming (up to its own 60s client
    timeout), with nothing left to stop it.
    """
    bank = FakeBank(frame_count=500)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.05)
    audio = FakeAudio(track_concurrency=True)
    handler = _handler(hermes=hermes, filler_bank=bank)

    loop = asyncio.get_running_loop()
    # The deadline is already gone by the time the first delta arrives
    # (0.05s later), so _TimeLimit is raised in _reply's own loop -- before
    # speak_sentence, and therefore before speak_sentence's own
    # stop_filler() call, ever runs.
    deadline = loop.time() + 0.02

    await asyncio.wait_for(
        handler._reply(audio, ext, hermes, "sid", "hi",
                       interrupt=asyncio.Event(),
                       utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=deadline),
        timeout=2,
    )

    assert bank.taken, "filler never played; test proves nothing"
    assert audio.max_concurrent_senders <= 1
    # If `finally: await stop_filler()` were missing, filler would still be
    # actively sending when _reply returns -- give it a moment and confirm
    # it has genuinely stopped, not merely paused between frames.
    await asyncio.sleep(0.05)
    sent_after_return = len(audio.sent)
    await asyncio.sleep(0.05)
    assert len(audio.sent) == sent_after_return, (
        "filler kept sending after _reply returned -- "
        "finally: stop_filler() did not run"
    )


async def test_filler_wakes_promptly_when_stopped_even_with_a_long_repeat():
    """run_filler waits on filler_stop via _wait_any, not a bare
    asyncio.sleep. With the shipped default filler_repeat_ms=3000, a bare
    sleep would keep the filler task asleep for up to the FULL repeat
    interval after being told to stop, and stop_filler()'s _drain_sender
    would sit there waiting for it -- delaying the caller's first real
    sentence by however much of that interval remained, and risking
    _drain_sender's own SENDER_DRAIN_TIMEOUT if the interval were long
    enough. filler_repeat_ms=500 here (rather than the shipped 3000) is
    purely to keep the test fast; the mechanism under test -- waking early
    on filler_stop rather than sleeping through it -- is identical.
    """
    bank = FakeBank()
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=500)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.10)
    h = _handler(hermes=hermes, filler_bank=bank)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await _run_one_turn(h, ext)
    elapsed = loop.time() - started

    assert bank.taken, "filler never played; test proves nothing"
    assert elapsed < 0.3, (
        f"_reply took {elapsed:.3f}s to finish a 0.10s-delayed reply -- "
        "filler's wait did not wake early when told to stop"
    )


async def test_filler_stops_taking_when_the_call_ends_before_any_reply():
    """run_filler also watches `ended`, not only `filler_stop`. Without it,
    a caller who hangs up while Hermes is still generating would leave
    filler calling bank.take() and writing dead-air audio nobody hears every
    filler_repeat_ms for as long as Hermes keeps streaming (up to its own
    60s client timeout) -- harmless to the (absent) caller, but it keeps
    churning the bank's rotation state and the filler task alive for no
    reason.
    """
    bank = FakeBank()
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=1.0)
    audio = FakeAudio()
    interrupt = asyncio.Event()
    ended = asyncio.Event()
    utterance_ready = asyncio.Event()
    handler = _handler(hermes=hermes, filler_bank=bank)

    reply_task = asyncio.create_task(
        handler._reply(audio, ext, hermes, "sid", "hi",
                       interrupt=interrupt, utterance_ready=utterance_ready,
                       final_seen=asyncio.Event(), ended=ended,
                       deadline=_deadline())
    )
    await _until(lambda: bank.taken)
    ended.set()
    await asyncio.sleep(0.05)
    taken_when_ended = len(bank.taken)
    await asyncio.sleep(0.1)
    assert len(bank.taken) == taken_when_ended, (
        "filler kept escalating tiers after the call had already ended"
    )

    # reply_task is still alive here -- FakeHermes's first_delta_delay=1.0
    # means _reply is still blocked awaiting the first delta, well past this
    # test's own assertions, and would outlive the test if left running.
    # utterance_ready, not task.cancel(): _reply is currently suspended
    # inside _next_delta's own race between the stream and this exact event
    # (see _next_delta's `abandon` parameter), so this is the same clean
    # abandonment path a genuinely new utterance takes in production.
    # Cancelling the outer task instead leaves _next_delta's inner `nxt`
    # task -- itself never cancelled -- still "running" the generator, and
    # contextlib.aclosing's aclose() then raises RuntimeError instead of
    # letting _Interrupted (and a normal return) propagate.
    utterance_ready.set()
    await reply_task


async def test_filler_keeps_talking_through_the_tts_round_trip():
    """stop_filler() sits AFTER synthesize_frames returns, not before it.

    ElevenLabs is a 400-600ms round trip (filler.py's own module docstring),
    and stopping filler before making that call would reintroduce exactly
    the dead air this feature exists to remove, on the FIRST sentence of
    every single reply:

        first sentence text arrives -> stop_filler() -> [400-600ms SILENCE]
        -> speech begins

    instead of filler talking through the whole round trip:

        first sentence text arrives -> filler keeps talking through
        [400-600ms round trip] -> stop_filler() + drain -> speech begins

    This proves filler frames keep reaching the socket for the WHOLE
    duration of a slow synthesis call, not just up to the moment
    speak_sentence begins it -- sampling audio.sent at two points inside the
    0.2s synthesis window and requiring growth between them, rather than
    merely requiring it to be non-zero once (which a filler that stopped
    immediately after one lucky frame could still satisfy).
    """
    bank = FakeBank()
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    tts = FakeTts(synthesis_delay=0.2)
    hermes = FakeHermes(deltas=["Answer."])
    audio = FakeAudio()
    handler = _handler(tts=tts, hermes=hermes, filler_bank=bank)

    reply_task = asyncio.create_task(
        handler._reply(audio, ext, hermes, "sid", "hi",
                       interrupt=asyncio.Event(),
                       utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=_deadline())
    )
    await _until(lambda: bank.taken)      # filler is genuinely playing
    await asyncio.sleep(0.08)             # comfortably inside the 0.2s
                                           # synthesis window
    mid_synthesis_sent = len(audio.sent)
    assert mid_synthesis_sent > 0, "filler never even started sending"
    await asyncio.sleep(0.08)             # still inside the window (0.16s
                                           # total; synthesis returns at 0.2s)
    assert len(audio.sent) > mid_synthesis_sent, (
        "filler frames stopped arriving before synthesis finished -- "
        "stop_filler() is running too early again"
    )

    await asyncio.wait_for(reply_task, timeout=2)
    assert bank.taken, "filler never played; test proves nothing"


# --------------------------------------------------------------------------
# Abandon: a complete new utterance during the wait discards a stale reply.
# --------------------------------------------------------------------------

async def test_a_new_utterance_during_the_wait_abandons_the_stale_reply():
    """The caller asked something else. Answering the previous question would
    talk over them with a stale answer -- the exact failure barge-in exists
    to prevent.

    Also asserts PROMPTNESS: without nxt.cancel() in _next_delta, the
    abandoned anext() task is never cancelled -- it simply keeps running
    until the stream's next real delta happens to arrive on its own, so the
    "abandon" is really just a silent wait for the stale reply to finish
    generating. With first_delta_delay=0.30 that would make this test still
    pass on every OTHER assertion while taking ~0.30s; only the wall-clock
    bound below catches that regression. In production the same missing
    cancellation would mean waiting out HermesClient's full 60s client
    timeout (hermes.py) before the caller ever hears an answer to their new
    question.
    """
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Stale answer."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, filler_bank=FakeBank())
    audio = FakeAudio()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _run_one_turn(h, ext, audio=audio, new_utterance_after=0.05)
    elapsed = loop.time() - started
    assert "Stale answer." not in audio.spoken_text
    assert hermes.stream_closed, "the SSE stream must be closed, not leaked"
    assert elapsed < 0.15, (
        f"abandon took {elapsed:.3f}s -- _next_delta must cancel the "
        "pending anext() promptly (new_utterance_after=0.05s), not silently "
        "wait out the rest of the stale stream (first_delta_delay=0.30s)"
    )


async def test_line_noise_during_the_wait_does_not_abandon_the_reply():
    """interrupt fires on any confirmed interim burst, including noise. If
    that abandoned the reply the caller would get silence and no answer.

    Also guards against a subtler failure than outright abandonment: nothing
    inside _reply ever clears `interrupt` on its own (handle() normally does,
    but it is blocked awaiting this whole call), so a burst confirmed during
    the THINKING window stays set into the first real sentence. Without the
    one-shot clear in speak_sentence, `waiter = asyncio.create_task(
    interrupt.wait())` for sentence 0 resolves on its very first pass and
    the reply is truncated to a single ~20ms frame -- the caller effectively
    gets silence even though `_Interrupted` is never abandon-triggered and
    "Real answer." IS technically "in" spoken_text (one frame's worth).
    Asserting the FULL reply (every frame of the one-sentence delta) is what
    catches that, where a bare "in" substring/membership check would not.
    """
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    tts = FakeTts(frames_per_sentence=10)
    hermes = FakeHermes(deltas=["Real answer."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, tts=tts, filler_bank=FakeBank())
    audio = FakeAudio()
    await _run_one_turn(h, ext, audio=audio, interrupt_after=0.05)
    assert audio.spoken_text == ["Real answer."] * 10, (
        "the reply was truncated -- a leftover `interrupt` flag from the "
        "thinking-window cough cut the first sentence short, leaving the "
        "caller with effective silence instead of the full answer"
    )


async def test_nothing_is_credited_when_the_reply_is_abandoned_before_a_word():
    """No sentence was spoken, so there is nothing the caller heard. An empty
    credit must not be posted to Hermes."""
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Stale."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, filler_bank=FakeBank())
    await _run_one_turn(h, ext, new_utterance_after=0.05)
    assert hermes.appended == []


async def test_a_final_transcript_during_synthesis_still_stops_a_stale_reply():
    """A genuine new question -- confirmed by BOTH `interrupt` (the final
    transcript) and `utterance_ready` (the utterance_end that followed it)
    -- can land WHILE sentence 0 is being synthesised, i.e. after
    _next_delta already handed the first delta back to _reply. The abandon
    race in _next_delta cannot fire again for a delta already in hand, so
    `interrupt` is the only remaining thing that can stop this stale reply.

    The one-shot `interrupt.clear()` added to speak_sentence for the
    line-noise case must NOT wipe a signal like this: it is guarded by
    `not utterance_ready.is_set()` specifically so a burst that turned out
    to be a complete, final-backed utterance is left alone. Without that
    guard this reply would play out in full -- exactly the "answer talks
    over the caller's new question" failure barge-in exists to prevent, in
    a form the earlier line-noise fix would otherwise have reintroduced.
    """
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    tts = FakeTts(frames_per_sentence=4, synthesis_delay=0.10)
    hermes = FakeHermes(deltas=["Stale one. ", "Stale two."],
                        first_delta_delay=0.20)
    h = _handler(hermes=hermes, tts=tts, filler_bank=FakeBank())
    audio = FakeAudio()
    # Both fire at t=0.25: first delta arrives at 0.20, synthesis (0.10s)
    # is still in flight, so this lands squarely inside sentence 0's TTS
    # round-trip -- after the delta was handed back, before speak_sentence
    # reaches its own interrupt.clear() guard.
    await _run_one_turn(h, ext, audio=audio, interrupt_after=0.25,
                        new_utterance_after=0.25)
    assert "Stale two." not in audio.spoken_text, (
        "the stale reply's second sentence was spoken in full -- a "
        "genuine new question (final transcript + utterance_end) landing "
        "during sentence 0's synthesis was wiped by the one-shot "
        "interrupt.clear() instead of stopping the reply"
    )


async def test_a_barge_in_between_sentences_still_stops_the_reply():
    """The one-shot `interrupt.clear()` in speak_sentence is scoped to
    `index == 0` (the first sentence only) so a barge-in landing in the GAP
    between two LATER sentences -- after sentence N has fully played, before
    sentence N+1's own waiter is created -- is not swallowed.

    Every OTHER barge-in test in this file sets `interrupt` DURING a
    sentence's own playback, after that sentence's waiter already exists,
    so none of them exercises this specific gap. Mutating the guard from
    `index == 0` to `True` (clearing before every sentence) would discard
    exactly this kind of barge-in and pass every other test in the file.
    """
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    tts = FakeTts(frames_per_sentence=4)
    hermes = FakeHermes(deltas=["One. ", "Two. ", "Three."])
    audio = FakeAudio()
    interrupt = asyncio.Event()

    def hook(index, count):
        if index == 0 and count == 4:      # sentence 0 just finished
            interrupt.set()                # barge-in lands in the gap

    audio.on_sent_hook = hook
    handler = _handler(hermes=hermes, tts=tts)

    await asyncio.wait_for(
        handler._reply(audio, ext, hermes, "sid", "hi",
                       interrupt=interrupt, utterance_ready=asyncio.Event(),
                       final_seen=asyncio.Event(), ended=asyncio.Event(),
                       deadline=_deadline()),
        timeout=2,
    )

    # synthesize_frames is called unconditionally at the TOP of
    # speak_sentence, before `interrupt` is checked (same as every other
    # interrupted-reply test in this file, e.g.
    # test_interrupted_reply_reports_only_complete_sentences_to_hermes),
    # so "Two." legitimately reaches tts.spoken even though its audio is
    # cut short. What must NOT happen is sentence 1's audio reaching the
    # caller, or a THIRD sentence ever being attempted.
    assert tts.spoken == ["One.", "Two."], (
        "sentence 2 ('Three.') was attempted -- the reply continued well "
        "past the gap where the barge-in landed"
    )
    # Frame-boundary granularity (same as every other "cut off mid-sentence"
    # test in this file) means a single already-in-flight frame can still
    # land before the waiter resolves -- what matters is sentence 1 was
    # NOT fully delivered (all 4 frames), not that literally zero arrived.
    assert audio.spoken_text.count("Two.") < 4, (
        "sentence 1's audio reached the caller in full -- a barge-in "
        "landing in the gap between sentences must still cut it short"
    )


async def test_a_final_before_its_utterance_end_still_stops_a_stale_reply():
    """The ~700ms gap between when a final sets `interrupt`
    (stream_call.py:326) and when the UtteranceEnd that follows it sets
    `utterance_ready` (:335) -- endpointing=300 / utterance_end_ms=1000,
    stream_stt.py -- comfortably contains a 400-600ms TTS round trip. If
    speak_sentence's one-shot `interrupt.clear()` guard evaluates INSIDE
    that gap it sees a real, final-backed interrupt with utterance_ready not
    yet set, and `not utterance_ready.is_set()` alone cannot tell that apart
    from line noise from the thinking window. Clearing it there lets the
    entire stale reply play at the caller -- the exact failure barge-in
    exists to prevent, at the more likely Deepgram timing (a final almost
    always precedes its own UtteranceEnd by close to a second, not by
    milliseconds).

    final_after fires `interrupt` and `final_seen` TOGETHER, mirroring
    pump_stt's final-transcript branch which sets both at the same
    statement -- unlike interrupt_after, which mimics a bare interim (no
    final behind it at all).
    """
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    tts = FakeTts(frames_per_sentence=4, synthesis_delay=0.15)
    hermes = FakeHermes(deltas=["Stale one. ", "Stale two."],
                        first_delta_delay=0.20)
    h = _handler(hermes=hermes, tts=tts, filler_bank=FakeBank())
    audio = FakeAudio()
    # final lands at t=0.25s; UtteranceEnd (utterance_ready) only at
    # t=0.60s. Sentence 0's guard evaluates at ~0.35s (0.20s first delta +
    # 0.15s synthesis) -- squarely inside that gap.
    await _run_one_turn(h, ext, audio=audio, final_after=0.25,
                        new_utterance_after=0.60)
    assert "Stale two." not in audio.spoken_text, (
        "the entire stale reply was spoken -- a final-backed interrupt "
        "landing before its own UtteranceEnd was wiped by the one-shot "
        "interrupt.clear() guard, which had no way to tell it apart from "
        "line noise until utterance_ready caught up ~700ms later"
    )


async def test_a_backchannel_during_the_wait_does_not_abandon_the_reply():
    """Drives handle() -- NOT _reply directly -- because this is the one
    property that depends on WHICH event handle() hands to which parameter.

    _reply takes four adjacent asyncio.Event parameters. The only place
    utterance_ready and final_seen are told apart is
    `_next_delta(stream, utterance_ready)`: everything else that reads them
    (speak_sentence's `not utterance_ready.is_set() and not
    final_seen.is_set()`) is symmetric and therefore blind to a swap by
    construction. Every other abandon test calls _reply directly with its own
    locals, so none of them can see handle()'s wiring at all.

    The scenario is the swap's production consequence. With endpointing=300
    Deepgram finalises at every ~300ms pause, so a backchannel ("mhm",
    "okay") during the 4-10s thinking window produces a bare final -- and NO
    UtteranceEnd, because the caller was not asking anything. Wired
    correctly, `interrupt` and `final_seen` fire, `utterance_ready` does not,
    and the reply is NOT abandoned: it arrives and is cut short by barge-in,
    which is recoverable. Wired backwards, _next_delta races against
    final_seen instead, the reply is discarded before a single delta, and
    handle() goes on to answer "mhm" -- the caller's real question is never
    answered at all.

    Keyword-only parameters on _reply make the positional form of this
    mistake unrepresentable; this test covers the mis-typed-keyword form that
    remains.
    """
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=8)
    hermes = FakeHermes(["Real answer."], first_delta_delay=0.30)
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 8:
            phase["name"] = "reply"
            stt.emit(_final("tell me a story"))
            stt.emit(_utt_end())

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    async def backchannel() -> None:
        # chat_stream's body only runs once _next_delta awaits the first
        # anext(), so a non-empty chats list means the reply's SSE stream is
        # genuinely open and the thinking window has started.
        await _until(lambda: hermes.chats != [])
        await asyncio.sleep(0.05)          # 0.05s into a 0.30s wait
        stt.emit(_final("mhm"))            # a final, and deliberately no
                                           # utterance_end behind it
        await asyncio.sleep(0.40)          # let the delta land and be spoken
        audio.hangup.set()                 # ...then wind the call down

    driver = asyncio.create_task(backchannel())
    try:
        await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
    finally:
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await driver
    await _settle()

    assert tts.spoken == ["Hello there.", "Real answer."], (
        "the reply was abandoned before a single delta arrived -- a bare "
        "final with no UtteranceEnd behind it must not discard the answer "
        "the caller is still waiting for (check which event handle() passes "
        "as utterance_ready: _next_delta must race utterance_ready, never "
        "final_seen)"
    )
    # ...and it is still interruptible: the final DID arm interrupt, so the
    # sentence is cut short rather than played out in full.
    assert audio.spoken_text.count("Real answer.") < 8, (
        "the whole sentence reached the caller -- a final-backed interrupt "
        "must still cut the reply short"
    )


# --------------------------------------------------------------------------
# Per-turn latency instrumentation: t0..t3 and the "turn latency" log line.
# --------------------------------------------------------------------------

def _latency_fields(caplog) -> dict[str, str]:
    """Pull the key=value pairs out of the one 'turn latency' log line.

    Raises rather than returning {} when no such line exists, so a missing
    log emits a clear failure instead of every downstream field assertion
    passing vacuously via KeyError.
    """
    for record in caplog.records:
        message = record.getMessage()
        if "turn latency:" in message:
            tail = message.split("turn latency:", 1)[1]
            return dict(re.findall(r"(\w+)=(\S+)", tail))
    raise AssertionError("no per-turn latency log line was emitted")


async def test_reply_logs_per_turn_latency_with_two_distinct_first_audio_marks(caplog):
    """The four loop.time() marks -- t0 (passed in from handle()), t1 (first
    Hermes delta), t2 (first TTS frames back), t3 (first reply frame on the
    wire) -- must surface as three named deltas (Hermes generation, TTS
    TTFB, queueing) plus the reply's own first-audio number, all measured
    from t0.

    No filler bank is wired up here, so filler_first_audio_ms must read as
    "this never happened" (None), not silently borrow the reply's own
    timing: collapsing the two would hide a real reply-latency regression
    behind filler audio in production, which is the whole reason they are
    tracked separately.
    """
    audio = FakeAudio()
    tts = FakeTts(frames_per_sentence=3)
    hermes = FakeHermes(["One. ", "Two."])
    handler = _handler(tts=tts)
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(
            handler._reply(audio, _ext(), hermes, "sid", "hi",
                           interrupt=asyncio.Event(),
                           utterance_ready=asyncio.Event(),
                           final_seen=asyncio.Event(), ended=asyncio.Event(),
                           deadline=_deadline(), t0=t0,
                           trigger="utterance_ready"),
            timeout=2,
        )
        await _settle()

    fields = _latency_fields(caplog)
    assert fields["trigger"] == "utterance_ready"
    for key in ("hermes_ms", "tts_ms", "queue_ms", "reply_first_audio_ms"):
        assert fields[key] != "None", f"{key} was never stamped"
        assert int(fields[key]) >= 0, f"{key} was negative: {fields[key]}"
    assert fields["filler_first_audio_ms"] == "None", (
        "no filler ever played in this turn -- filler_first_audio_ms must "
        "not borrow the reply's own timing"
    )


async def test_a_stalled_turn_still_gets_per_turn_latency_logged(monkeypatch, caplog):
    """stalled turns (the UTTERANCE_END_FALLBACK safety net, handle():
    `stalled = ... and not utterance_ready.is_set() ...`) fire with
    utterance_ready never set. Stamping t0 only where utterance_ready is
    consumed -- rather than unconditionally, right before every call into
    _reply -- would silently exclude exactly these turns from the latency
    log. That matters: if the fallback is firing often in production, part
    of the "4-10s dead air" is a turn-detection problem wearing this
    feature's clothes, and the `trigger` field is what makes the two
    distinguishable at all.
    """
    monkeypatch.setattr("phonesys.stream_call.UTTERANCE_END_FALLBACK", 0.05)
    audio = FakeAudio(frame_delay=0.002, send_delay=0.003)
    stt = FakeStt(audio=audio)
    tts = FakeTts(frames_per_sentence=20)
    hermes = FakeHermes(["Sure thing."])
    phase = {"name": "greeting"}

    def hook(index, count):
        if phase["name"] == "greeting" and index == 0 and count == 20:
            phase["name"] = "reply"
            stt.emit(_final("are you there"))   # and no utterance_end, ever
        elif phase["name"] == "reply" and index == 0 and count == 20:
            audio.hangup.set()

    audio.on_sent_hook = hook
    handler = _handler(stt=stt, tts=tts, hermes=hermes, server=FakeServer(audio))

    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(handler.handle("chan-1", "7001", _ext()), timeout=5)
        await _settle()

    fields = _latency_fields(caplog)
    assert fields["trigger"] == "stalled"
    assert fields["hermes_ms"] != "None", (
        "t0 was never stamped for the stalled-fallback path -- exactly the "
        "regression this test guards against"
    )


async def test_marks_stay_none_when_their_stage_never_ran(caplog):
    """A reply abandoned before Hermes ever returns a single delta must log
    real None values for every downstream mark, not fabricated zeros --
    the direct behavioural consequence of `if tN is None` guards with no
    dict and no once-only-flag-plus-pop bookkeeping: a stage that never
    executes simply never assigns its local.

    Filler DOES play in this scenario (filler_delay_ms=10 is well under the
    0.30s stale reply), which is the point: filler_first_audio_ms must be a
    real number here even while every reply-side mark is None, proving the
    two first-audio marks are genuinely independent.
    """
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Stale answer."], first_delta_delay=0.30)
    handler = _handler(hermes=hermes, filler_bank=FakeBank())
    audio = FakeAudio()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    utterance_ready = asyncio.Event()

    async def fire_new_utterance() -> None:
        await asyncio.sleep(0.02)
        utterance_ready.set()

    waiter = asyncio.create_task(fire_new_utterance())
    with caplog.at_level(logging.INFO):
        try:
            await asyncio.wait_for(
                handler._reply(audio, ext, hermes, "sid", "hi",
                               interrupt=asyncio.Event(),
                               utterance_ready=utterance_ready,
                               final_seen=asyncio.Event(), ended=asyncio.Event(),
                               deadline=_deadline(), t0=t0,
                               trigger="utterance_ready"),
                timeout=1,
            )
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        await _settle()

    fields = _latency_fields(caplog)
    assert fields["hermes_ms"] == "None", "t1 must stay None -- no delta ever arrived"
    assert fields["tts_ms"] == "None"
    assert fields["queue_ms"] == "None"
    assert fields["reply_first_audio_ms"] == "None", (
        "no reply sentence was ever synthesised, let alone sent"
    )
    assert fields["filler_first_audio_ms"] != "None", (
        "filler genuinely played here -- if this collapsed onto the reply's "
        "own (nonexistent) timing it would falsely suggest a reply was heard"
    )


# --------------------------------------------------------------------------
# credit_heard: logging `spoken` alongside `heard`.
# --------------------------------------------------------------------------

async def test_credit_heard_logs_spoken_alongside_heard(caplog):
    """credit_heard's log line must show BOTH `heard` (what actually
    reached the caller) and `spoken` (every sentence that reached
    synthesize_frames, including the one cut short by the interruption) --
    the difference between the two is exactly where the truncation
    boundary fell. `spoken` here is generated-and-queued, not the full
    generated reply: "Three." was never even synthesised.
    """
    audio = FakeAudio()
    tts = FakeTts(frames_per_sentence=5)
    hermes = FakeHermes(["One. ", "Two. ", "Three."])
    interrupt, ended = asyncio.Event(), asyncio.Event()

    def hook(index, count):
        if index == 1 and count == 2:      # halfway through sentence 2
            interrupt.set()

    audio.on_sent_hook = hook
    handler = _handler(tts=tts)

    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(
            handler._reply(audio, _ext(), hermes, "sid", "hi",
                           interrupt=interrupt, utterance_ready=asyncio.Event(),
                           final_seen=asyncio.Event(), ended=ended,
                           deadline=_deadline()),
            timeout=2,
        )
        await _settle()

    record = next(r for r in caplog.records if "caller heard:" in r.getMessage())
    message = record.getMessage()
    assert "heard: 'One.'" in message
    assert "spoken: 'One. Two.'" in message, (
        "spoken must include 'Two.' -- it was synthesised (and partially "
        "sent) even though it was not fully heard, and 'Three.' must stay "
        "out -- it was never even synthesised"
    )
