import asyncio

import httpx

import phonesys.call as call_mod
from phonesys.call import CallHandler
from phonesys.config import ExtensionConfig
from phonesys.recordings import RecordingWaiter
from phonesys.waiters import PlaybackWaiter

EXT = ExtensionConfig(
    profile="phonebot", hermes_base_url="http://h", hermes_api_key="k",
    greeting="Hello there", voice_id="v", max_silence_seconds=2,
    max_turn_seconds=30, max_call_seconds=600,
)


class FakeAri:
    """Stands in for Asterisk. record() immediately signals RecordingFinished,
    optionally writing a file, mirroring what the real event loop does."""

    def __init__(self, waiter=None, rec_dir=None, payload=b"WAV", finish=True,
                 finish_playback=True):
        self.answered, self.played, self.recorded, self.hungup = [], [], [], []
        self._waiter, self._rec_dir, self._payload, self._finish = waiter, rec_dir, payload, finish
        # Asterisk reports playback completion over the event socket, exactly
        # as it does for recordings.
        self.playbacks = PlaybackWaiter()
        self._finish_playback = finish_playback

    async def answer(self, cid): self.answered.append(cid)

    async def play(self, cid, media, playback_id=None):
        self.played.append(media)
        if self._finish_playback and playback_id:
            self.playbacks.resolve(playback_id, ok=True)
        return playback_id or "pb"

    async def record(self, cid, name, max_silence, max_duration):
        self.recorded.append(name)
        if self._rec_dir is not None and self._payload:
            (self._rec_dir / f"{name}.wav").write_bytes(self._payload)
        if self._waiter is not None and self._finish:
            self._waiter.resolve(name, ok=True)

    async def hangup(self, cid): self.hungup.append(cid)


class FakeStt:
    def __init__(self, texts): self.texts = list(texts)
    async def transcribe(self, audio, content_type="audio/wav"):
        return self.texts.pop(0) if self.texts else ""


class FakeHermes:
    def __init__(self): self.seen = []
    async def create_session(self, sid, system_prompt=None): return sid
    async def chat(self, sid, msg):
        self.seen.append(msg)
        return f"echo {msg}"
    async def aclose(self): pass


class FakeTts:
    def __init__(self): self.seen = []
    async def synthesize(self, text, voice_id):
        self.seen.append(text)
        return b"\x00\x01"


def _dirs(tmp_path):
    sounds, rec = tmp_path / "sounds", tmp_path / "rec"
    sounds.mkdir(); rec.mkdir()
    return sounds, rec


def _handler(sounds, rec, ari, stt, hermes, tts, waiter):
    return CallHandler(ari, stt, tts, lambda ext: hermes, sounds, rec,
                       recordings=waiter, playbacks=ari.playbacks)


async def _until(predicate, limit: int = 500) -> None:
    """Yield to the loop until predicate() holds. Bounded, never a bare sleep."""
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition never became true")


async def test_answers_and_plays_greeting_first(tmp_path):
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes, tts = FakeAri(w, rec, payload=b""), FakeHermes(), FakeTts()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)
    assert ari.answered == ["chan-1"]
    assert tts.seen[0] == "Hello there"
    assert ari.played and ari.played[0].startswith("sound:phonesys/")


async def test_writes_then_removes_tts_audio(tmp_path):
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes = FakeAri(w, rec, payload=b""), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    await h.handle("chan-1", "7000", EXT)
    # Cleaned up after playback so the shared mount does not fill up.
    assert list(sounds.glob("*.ulaw")) == []


async def test_a_failed_playback_leaves_no_audio_file_behind(tmp_path):
    """The .ulaw must be removed however _speak exits, not only when the
    PlaybackFinished wait completes.

    A 404 out of play() is the documented NORMAL case -- the caller hung up
    mid-prompt (see handle()'s AriError arm) -- and nothing ever sweeps the
    shared sounds mount: preflight_media_dirs only checks permissions. Every
    dropped call would otherwise leave tens of KB there forever.
    """
    from phonesys.ari import AriError

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class HangsUpMidPlaybackAri(FakeAri):
        async def play(self, cid, media, playback_id=None):
            self.played.append(media)
            raise AriError("channel gone", 404)

    ari, hermes = HangsUpMidPlaybackAri(w, rec, payload=b""), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    await h.handle("chan-1", "7000", EXT)

    assert ari.played, "play() was never reached; the test proves nothing"
    assert list(sounds.iterdir()) == [], \
        "a .ulaw was left in the shared sounds mount after play() raised"


async def test_cancelling_a_call_mid_playback_does_not_start_another_recording(tmp_path):
    """Ctrl-C must end the call, not roll the turn loop round once more.

    _speak awaits PlaybackFinished. Swallowing CancelledError there loses the
    cancellation permanently -- control falls to the top of handle()'s while
    loop, which POSTs a new /channels/{id}/record AFTER the operator asked the
    process to stop, and then blocks the full max_turn_seconds +
    RECORDING_GRACE_SECONDS waiting for an event main()'s consumer is already
    gone to deliver (~40s per live call with the shipped config). Only
    TimeoutError may be swallowed there.
    """
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    # finish_playback=False: the greeting's PlaybackFinished never arrives, so
    # the call is parked in _speak's wait -- exactly where a Ctrl-C lands.
    ari, hermes = FakeAri(w, rec, payload=b"", finish_playback=False), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)

    task = asyncio.create_task(h.handle("chan-1", "7000", EXT))
    await _until(lambda: bool(ari.played))
    task.cancel()
    try:
        await _until(task.done)
        assert task.cancelled(), "the cancellation was swallowed instead of propagating"
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=1)

    assert ari.recorded == [], \
        "a recording was started after the call task had been cancelled"
    # Cancellation is also an exit path _speak must clean up after.
    assert list(sounds.glob("*.ulaw")) == []


async def test_hangs_up_after_repeated_silence(tmp_path):
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes = FakeAri(w, rec, payload=b""), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    await h.handle("chan-1", "7000", EXT)
    assert ari.hungup == ["chan-1"]
    assert hermes.seen == []
    # A silent caller is prompted once, then given the same budget again.
    assert len(ari.recorded) == 2 * call_mod.SILENT_TURNS_BEFORE_PROMPT


async def test_asks_whether_the_caller_is_still_there_before_hanging_up(tmp_path):
    """A caller who talks over the agent is not heard at all -- 7000 is
    half-duplex, it cannot record while it plays. Ending the call on that
    silence drops people mid-conversation, so the agent asks first."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes, tts = FakeAri(w, rec, payload=b""), FakeHermes(), FakeTts()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)
    assert call_mod.STILL_THERE_PROMPT in tts.seen
    # Asked once, not once per silent turn.
    assert tts.seen.count(call_mod.STILL_THERE_PROMPT) == 1


async def test_a_reply_after_the_prompt_re_arms_it(tmp_path):
    """The prompt is per-lull, not per-call: a caller who answers and later
    goes quiet again must be asked again rather than dropped immediately."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), FakeHermes(), FakeTts()
    n = call_mod.SILENT_TURNS_BEFORE_PROMPT
    # Go quiet, get prompted, speak, then go quiet all over again.
    stt = FakeStt([""] * n + ["still here"] + [""] * (2 * n))
    h = _handler(sounds, rec, ari, stt, hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)
    assert hermes.seen == ["still here"]
    assert tts.seen.count(call_mod.STILL_THERE_PROMPT) == 2, \
        "the second lull must be prompted too, not dropped"


async def test_speaking_up_before_the_prompt_keeps_the_call_alive(tmp_path):
    """The budget must be consecutive silence, not cumulative -- otherwise a
    long call accumulates its way to a hangup mid-conversation."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), FakeHermes(), FakeTts()
    n = call_mod.SILENT_TURNS_BEFORE_PROMPT
    # Repeatedly go almost-quiet, then speak. Never n silent turns in a row.
    stt = FakeStt(([""] * (n - 1) + ["hello"]) * 3 + [""] * (2 * n))
    h = _handler(sounds, rec, ari, stt, hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)
    assert hermes.seen == ["hello", "hello", "hello"]


async def test_transcript_reaches_hermes_and_reply_is_spoken(tmp_path):
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), FakeHermes(), FakeTts()
    stt = FakeStt(["what time is it", "", ""])
    h = _handler(sounds, rec, ari, stt, hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)
    assert hermes.seen == ["what time is it"]
    assert "echo what time is it" in tts.seen
    assert len(ari.played) >= 2  # greeting + reply


async def test_waits_for_recording_before_transcribing(tmp_path):
    """The regression this fixes: reading the file before the recording
    finished sent zero bytes to Deepgram and returned HTTP 400."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    seen_audio = []

    class RecordingStt:
        async def transcribe(self, audio, content_type="audio/wav"):
            seen_audio.append(audio)
            return ""

    class SlowAri(FakeAri):
        async def record(self, cid, name, max_silence, max_duration):
            self.recorded.append(name)

            async def later():
                await asyncio.sleep(0.05)
                (rec / f"{name}.wav").write_bytes(b"COMPLETE-WAV")
                w.resolve(name, ok=True)

            asyncio.create_task(later())

    ari, hermes = SlowAri(w, rec), FakeHermes()
    h = _handler(sounds, rec, ari, RecordingStt(), hermes, FakeTts(), w)
    await h.handle("chan-1", "7000", EXT)
    # Every transcribe call saw the fully written file, never empty bytes.
    assert seen_audio and all(a == b"COMPLETE-WAV" for a in seen_audio)


async def test_recording_does_not_start_until_playback_finishes(tmp_path):
    """The regression this fixes: the reply's playback and the turn recorder
    overlapped, so every second the agent spent talking was recorded as caller
    silence. Two silent turns in a row end the call, so the caller got a single
    ~1s window to start speaking after each reply or was hung up on."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    order = []

    class OrderedAri(FakeAri):
        async def play(self, cid, media, playback_id=None):
            self.played.append(media)
            order.append("play-start")

            async def finish():
                # Asterisk emits PlaybackFinished only once the audio has
                # actually played out to the caller.
                await asyncio.sleep(0.05)
                order.append("play-finish")
                self.playbacks.resolve(playback_id, ok=True)

            asyncio.create_task(finish())
            return playback_id

        async def record(self, cid, name, max_silence, max_duration):
            order.append("record")
            await super().record(cid, name, max_silence, max_duration)

    ari, hermes = OrderedAri(w, rec, payload=b""), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    await h.handle("chan-1", "7000", EXT)

    assert order[:3] == ["play-start", "play-finish", "record"]
    assert "record" not in order[:order.index("play-finish")], \
        "the caller was still hearing the agent when the recorder started"


async def test_playback_that_never_finishes_does_not_strand_the_call(tmp_path):
    """PlaybackFinished can be lost (the caller hangs up mid-prompt). Waiting
    for it forever would hold the channel open until max_call_seconds."""
    import phonesys.call as call_mod

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes = FakeAri(w, rec, payload=b"", finish_playback=False), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    original = call_mod.PLAYBACK_GRACE_SECONDS
    call_mod.PLAYBACK_GRACE_SECONDS = 0
    try:
        await h.handle("chan-1", "7000", EXT)
    finally:
        call_mod.PLAYBACK_GRACE_SECONDS = original
    assert ari.hungup == ["chan-1"]
    assert ari.recorded, "the turn loop must still run after a lost PlaybackFinished"


async def test_recording_that_never_finishes_ends_the_call(tmp_path):
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes = FakeAri(w, rec, finish=False), FakeHermes()
    ext = ExtensionConfig(**{**EXT.__dict__, "max_turn_seconds": 0})
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    h_ext = ext
    import phonesys.call as call_mod
    original = call_mod.RECORDING_GRACE_SECONDS
    call_mod.RECORDING_GRACE_SECONDS = 0
    try:
        await h.handle("chan-1", "7000", h_ext)
    finally:
        call_mod.RECORDING_GRACE_SECONDS = original
    assert ari.hungup == ["chan-1"]


async def test_caller_hangup_is_not_logged_as_a_failure(tmp_path, caplog):
    """A 404 from record means the channel is gone -- the caller hung up.
    Logging that as a failure with a traceback makes normal call completion
    look broken."""
    import logging

    from phonesys.ari import AriError

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class HungUpAri(FakeAri):
        async def record(self, cid, name, max_silence, max_duration):
            self.recorded.append(name)
            raise AriError("channel gone", 404)

    ari, hermes = HungUpAri(w, rec), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    with caplog.at_level(logging.INFO):
        await h.handle("chan-1", "7000", EXT)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "an ordinary hangup must not log at ERROR"
    assert any("hung up" in r.getMessage() for r in caplog.records)
    assert ari.hungup == ["chan-1"]


async def test_non_404_ari_error_still_logged_as_failure(tmp_path, caplog):
    import logging

    from phonesys.ari import AriError

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class BrokenAri(FakeAri):
        async def record(self, cid, name, max_silence, max_duration):
            raise AriError("server exploded", 500)

    ari, hermes = BrokenAri(w, rec), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    with caplog.at_level(logging.INFO):
        await h.handle("chan-1", "7000", EXT)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a real fault must still be logged at ERROR"


async def test_failed_recording_is_not_treated_as_silence(tmp_path, caplog):
    """Regression: Asterisk could not write recordings (permissions), the
    turn loop read nothing, and it looked exactly like a silent caller."""
    import logging
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class FailingAri(FakeAri):
        async def record(self, cid, name, max_silence, max_duration):
            self.recorded.append(name)
            w.resolve(name, ok=False)   # RecordingFailed

    ari, hermes = FailingAri(w, rec), FakeHermes()
    h = _handler(sounds, rec, ari, FakeStt([]), hermes, FakeTts(), w)
    with caplog.at_level(logging.ERROR):
        await h.handle("chan-1", "7000", EXT)
    assert any("FAILED" in r.message or "FAILED" in r.getMessage() for r in caplog.records), \
        "a failed recording must be logged as an error, not swallowed as silence"
    assert len(ari.recorded) == 1, "should stop immediately, not retry as if silent"
    assert ari.hungup == ["chan-1"]


class StallingHermes(FakeHermes):
    """Hermes that stalls on the Nth chat (1-based) and answers the rest.

    Live evidence this models: /chat was issued at 15:24:34.974, Hermes never
    answered, httpx.ReadTimeout fired 64s later. Hermes was not down -- a
    second call chatted successfully while that one hung -- so the caller's
    NEXT question would have worked, if the call had survived to ask it.
    """

    def __init__(self, stall_on: int = 1, exc: BaseException | None = None) -> None:
        super().__init__()
        self._stall_on = stall_on
        self._exc = exc if exc is not None else httpx.ReadTimeout("timed out")

    async def chat(self, sid, msg):
        self.seen.append(msg)
        if len(self.seen) == self._stall_on:
            raise self._exc
        return f"echo {msg}"


async def test_a_hermes_stall_apologizes_aloud_and_keeps_listening(tmp_path, caplog):
    """A single stalled Hermes request must cost one turn, not the call.

    Before this, the exception reached handle()'s generic `except Exception`:
    traceback, hangup, and the caller sat through ~64s of silence first.
    """
    import logging

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), StallingHermes(), FakeTts()
    n = call_mod.SILENT_TURNS_BEFORE_PROMPT
    stt = FakeStt(["what time is it", "try again"] + [""] * (2 * n))
    h = _handler(sounds, rec, ari, stt, hermes, tts, w)
    with caplog.at_level(logging.INFO):
        await h.handle("chan-1", "7000", EXT)

    assert call_mod.TURN_FAILURE_MESSAGE in tts.seen, \
        "the caller was left in silence instead of being told to try again"
    # The call went on: the retry reached Hermes and was answered aloud.
    assert hermes.seen == ["what time is it", "try again"]
    assert "echo try again" in tts.seen
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a recoverable stall must not be logged as a call failure"
    assert ari.hungup == ["chan-1"]


async def test_a_hermes_stall_whose_apology_also_fails_does_not_end_the_call(tmp_path):
    """The apology is itself a network round trip. If it fails too, the arm
    must still return to listening -- otherwise it just moves the dropped
    call one step later."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class ApologyFailsTts(FakeTts):
        async def synthesize(self, text, voice_id):
            if text == call_mod.TURN_FAILURE_MESSAGE:
                raise httpx.ConnectError("elevenlabs unreachable")
            return await super().synthesize(text, voice_id)

    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), StallingHermes(), ApologyFailsTts()
    n = call_mod.SILENT_TURNS_BEFORE_PROMPT
    stt = FakeStt(["what time is it", "try again"] + [""] * (2 * n))
    h = _handler(sounds, rec, ari, stt, hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)

    assert hermes.seen == ["what time is it", "try again"]
    assert "echo try again" in tts.seen


async def test_a_sustained_provider_outage_ends_the_call_instead_of_looping(tmp_path, caplog):
    """A provider that is DOWN must end the call, not hold a silent line.

    The stall arm catches httpx.HTTPError, which is also what a transport
    failure on run_turn's TTS leg raises -- and its `continue` bypasses the
    silence budget, the only other thing that can end this loop. So an
    ElevenLabs outage produced up to max_call_seconds (600s live) of open
    line, no audio at all (the apology's own synthesis fails the same way),
    and not one ERROR in the log. Worse, it was outage-shape dependent: an
    HTTP 500 raises TtsError and dropped the call, while a refused connection
    from the same outage looped.
    """
    import logging

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class AlwaysHearsStt:
        async def transcribe(self, audio, content_type="audio/wav"):
            return "hello"

    class OutageTts(FakeTts):
        """Healthy for the greeting, then the provider goes away."""

        async def synthesize(self, text, voice_id):
            if self.seen:
                raise httpx.ConnectError("elevenlabs unreachable")
            return await super().synthesize(text, voice_id)

    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), FakeHermes(), OutageTts()
    h = _handler(sounds, rec, ari, AlwaysHearsStt(), hermes, tts, w)
    # A backstop, not the mechanism under test: without the cap the loop runs
    # until max_call_seconds, and this keeps the failing run short enough to
    # read rather than turning it into a hang.
    ext = ExtensionConfig(**{**EXT.__dict__, "max_call_seconds": 1})
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(h.handle("chan-1", "7000", ext), timeout=10)

    assert len(ari.recorded) <= call_mod.MAX_CONSECUTIVE_TURN_FAILURES, \
        (f"the outage looped for {len(ari.recorded)} turns, holding a silent "
         f"line open")
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a whole call lost to an outage left nothing at ERROR in the log"
    assert ari.hungup == ["chan-1"]


async def test_interleaved_silence_does_not_defeat_the_failure_cap(tmp_path, caplog):
    """A silent turn is a COMPLETED turn: run_turn returns successfully
    without ever touching Hermes or TTS (pipeline.run_turn's empty-transcript
    branch). Resetting failure_streak unconditionally on any non-raising
    run_turn call -- rather than only on a turn that actually reached Hermes
    -- let a caller who alternates speech and silence clear the cap every
    other turn. live config.yaml sets max_silence_seconds=1, so a caller
    pausing for a second is enough to produce that silent turn.

    A mid-call outage with a caller alternating speech and silence must still
    end the call via the failure cap, in a small, bounded number of turns --
    not ride out max_call_seconds with an open, silent line and nothing
    logged at ERROR, which is exactly the symptom the cap was added to
    prevent.
    """
    import logging

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class AlternatingSpeechAndSilenceStt:
        """Models a caller who pauses for a beat between utterances."""

        def __init__(self):
            self._n = 0

        async def transcribe(self, audio, content_type="audio/wav"):
            self._n += 1
            return "hello" if self._n % 2 == 1 else ""

    class OutageTts(FakeTts):
        """Healthy for the greeting, then the provider goes away."""

        async def synthesize(self, text, voice_id):
            if self.seen:
                raise httpx.ConnectError("elevenlabs unreachable")
            return await super().synthesize(text, voice_id)

    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), FakeHermes(), OutageTts()
    h = _handler(sounds, rec, ari, AlternatingSpeechAndSilenceStt(), hermes, tts, w)
    # A backstop, not the mechanism under test: without the fix the loop rides
    # to max_call_seconds, and this keeps a failing run short enough to read
    # rather than turning it into a hang.
    ext = ExtensionConfig(**{**EXT.__dict__, "max_call_seconds": 1})
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(h.handle("chan-1", "7000", ext), timeout=10)

    # fail, silent, fail, silent, fail: failure_streak only ever increments
    # (the interleaved silent turns must not reset it), so the cap trips on
    # the 3rd failure, the 5th turn overall.
    assert len(ari.recorded) == 2 * call_mod.MAX_CONSECUTIVE_TURN_FAILURES - 1, (
        f"the outage looped for {len(ari.recorded)} turns despite the cap -- "
        f"an interleaved silent turn must not clear failure_streak"
    )
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a whole call lost to an outage left nothing at ERROR in the log"
    assert ari.hungup == ["chan-1"]


async def test_a_flaky_stt_link_interleaved_with_silence_still_ends_the_call(tmp_path, caplog):
    """Same root cause, different leg of the pipeline: a dead STT link raises
    before run_turn ever produces a transcript, so it goes through the same
    failure arm as a Hermes or TTS outage -- and a genuinely silent turn
    (STT returning "") in between must not clear that budget either.
    """
    import logging

    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class FlakySttAlternatingWithSilence:
        """Alternates a dead STT link with a caller who said nothing."""

        def __init__(self):
            self._n = 0

        async def transcribe(self, audio, content_type="audio/wav"):
            self._n += 1
            if self._n % 2 == 1:
                raise httpx.ConnectError("deepgram unreachable")
            return ""

    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), FakeHermes(), FakeTts()
    h = _handler(sounds, rec, ari, FlakySttAlternatingWithSilence(), hermes, tts, w)
    ext = ExtensionConfig(**{**EXT.__dict__, "max_call_seconds": 1})
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(h.handle("chan-1", "7000", ext), timeout=10)

    # Same shape as the outage case above: fail, silent, fail, silent, fail --
    # the cap trips on the 3rd failure, the 5th turn overall.
    assert len(ari.recorded) == 2 * call_mod.MAX_CONSECUTIVE_TURN_FAILURES - 1, (
        f"the flaky STT link looped for {len(ari.recorded)} turns despite "
        f"the cap -- an interleaved silent turn must not clear failure_streak"
    )
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a whole call lost to a flaky STT link left nothing at ERROR in the log"
    assert ari.hungup == ["chan-1"]
    assert hermes.seen == [], "a dead STT link must never reach Hermes"


async def test_a_recovered_blip_does_not_count_towards_the_failure_cap(tmp_path):
    """The cap counts CONSECUTIVE failures. A call that hits an occasional
    stall and recovers each time must run as long as the caller wants it to --
    the arm exists precisely because those recover."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class IntermittentHermes(FakeHermes):
        """Stalls every other chat, forever."""

        async def chat(self, sid, msg):
            self.seen.append(msg)
            if len(self.seen) % 2 == 1:
                raise httpx.ReadTimeout("timed out")
            return f"echo {msg}"

    n = call_mod.SILENT_TURNS_BEFORE_PROMPT
    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), IntermittentHermes(), FakeTts()
    # Far more alternating stalls than the cap, then silence to end the call.
    stt = FakeStt(["q"] * 12 + [""] * (2 * n))
    h = _handler(sounds, rec, ari, stt, hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)

    assert len(hermes.seen) == 12, \
        "the call was cut short by stalls it demonstrably recovered from"
    assert "echo q" in tts.seen


async def test_a_failed_turn_does_not_carry_the_silence_streak_forward(tmp_path):
    """A caller who spoke must get the full silence budget back.

    run_turn only reaches Hermes/TTS after transcribing something, so the
    usual way into the stall arm is a turn where the caller demonstrably
    spoke. Leaving the streak alone let the call end on "no answer to 'Are
    you still there?' after N more silent turns" -- a log line that is simply
    false about someone who was talking.
    """
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()
    n = call_mod.SILENT_TURNS_BEFORE_PROMPT

    class PromptWatchingTts(FakeTts):
        def __init__(self, ari):
            super().__init__()
            self._ari = ari
            self.prompted_after: list[int] = []

        async def synthesize(self, text, voice_id):
            if text == call_mod.STILL_THERE_PROMPT:
                self.prompted_after.append(len(self._ari.recorded))
            return await super().synthesize(text, voice_id)

    ari = FakeAri(w, rec, payload=b"WAV")
    tts = PromptWatchingTts(ari)
    # Quiet up to one turn short of the prompt, then SPEAK on a turn that
    # fails, then go quiet again.
    stt = FakeStt([""] * (n - 1) + ["what time is it"] + [""] * (3 * n))
    h = _handler(sounds, rec, ari, stt, FakeHermes(), tts, w)
    # Stall the one and only chat, i.e. the turn the caller spoke on.
    h._hermes_factory = lambda ext: StallingHermes()
    await h.handle("chan-1", "7000", EXT)

    assert tts.prompted_after, "the caller was never asked whether they were there"
    assert tts.prompted_after[0] == 2 * n, (
        f"the prompt came after {tts.prompted_after[0]} turns, not {2 * n}: "
        f"the {n - 1} silent turns from before the caller spoke were still "
        f"being counted against them"
    )


async def test_a_programming_error_in_the_turn_is_still_fatal(tmp_path):
    """The stall arm catches Hermes/transport failures ONLY. Catching bare
    Exception would turn every bug in the turn pipeline into an apology
    spoken on a loop for the rest of the call."""
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class BuggyHermes(FakeHermes):
        async def chat(self, sid, msg):
            raise TypeError("this is a bug, not a stall")

    ari, hermes, tts = FakeAri(w, rec, payload=b"WAV"), BuggyHermes(), FakeTts()
    h = _handler(sounds, rec, ari, FakeStt(["hello"]), hermes, tts, w)
    await h.handle("chan-1", "7000", EXT)

    assert call_mod.TURN_FAILURE_MESSAGE not in tts.seen
    assert len(ari.recorded) == 1, "a bug must stop the call, not be retried"
    assert ari.hungup == ["chan-1"]


async def test_hangs_up_even_when_a_stage_raises(tmp_path):
    sounds, rec = _dirs(tmp_path)
    w = RecordingWaiter()

    class BoomStt:
        async def transcribe(self, audio, content_type="audio/wav"):
            raise RuntimeError("stt exploded")

    ari, hermes = FakeAri(w, rec, payload=b"WAV"), FakeHermes()
    h = _handler(sounds, rec, ari, BoomStt(), hermes, FakeTts(), w)
    await h.handle("chan-1", "7000", EXT)
    # A failure must never strand the channel up.
    assert ari.hungup == ["chan-1"]
