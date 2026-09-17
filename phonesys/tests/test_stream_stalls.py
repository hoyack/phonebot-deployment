"""Offline regressions for stalled streams and transferred filler."""
import asyncio
import contextlib

import httpx
import pytest

from phonesys.hermes import HermesClient

from test_stream_call import FakeAudio, FakeHermes, FakeTts, _ext, _handler


class StalledHermes(FakeHermes):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_stream(self, session_id, message):
        self.chats.append((session_id, message))
        self.started.set()
        try:
            await self.release.wait()
            yield "Late answer."
        finally:
            # Exercise asynchronous response cleanup, not only a sync flag.
            await asyncio.sleep(0)
            self.stream_closed = True


def reply_task(handler, hermes, audio, *, ended=None, deadline=None, ext=None,
               interrupt=None, utterance_ready=None):
    return asyncio.create_task(handler._reply(
        audio, ext or _ext(), hermes, "session", "Research this",
        interrupt=interrupt or asyncio.Event(),
        utterance_ready=utterance_ready or asyncio.Event(),
        final_seen=asyncio.Event(), ended=ended or asyncio.Event(),
        deadline=deadline or asyncio.get_running_loop().time() + 10,
    ))


async def test_real_sse_keepalives_and_tool_events_do_not_extend_budget():
    class KeepaliveStream(httpx.AsyncByteStream):
        closed = False
        events = 0

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.002)
                self.events += 1
                yield (b': keepalive\n\nevent: tool.progress\n'
                       b'data: {"delta":"private tool work"}\n\n')

        async def aclose(self):
            self.closed = True

    body = KeepaliveStream()
    hermes = HermesClient("http://offline.test", "fake")
    await hermes._client.aclose()
    hermes._client = httpx.AsyncClient(
        base_url="http://offline.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)),
    )
    audio = FakeAudio()
    try:
        await asyncio.wait_for(reply_task(_handler(), hermes, audio,
                              ext=_ext(hermes_reply_timeout_seconds=0.04)), 0.3)
        assert body.closed and body.events > 1
        assert any("taking too long" in text for text in audio.spoken_text)
        assert all("private tool work" not in text for text in audio.spoken_text)
    finally:
        await hermes.aclose()


async def test_repeated_outer_cancellation_drains_generator_without_leaked_tasks():
    baseline = asyncio.all_tasks()
    cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()

    class SlowCleanup(StalledHermes):
        async def chat_stream(self, session_id, message):
            self.started.set()
            try:
                await self.release.wait()
                yield "Late answer."
            finally:
                cleaning.set()
                await finish_cleanup.wait()
                self.stream_closed = True

    hermes = SlowCleanup()
    task = reply_task(_handler(), hermes, FakeAudio())
    await hermes.started.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "outer cancellation bypassed response cleanup"
    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hermes.stream_closed
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)


async def test_transferred_filler_barge_in_during_synthesis_stays_silent():
    hermes = StalledHermes()
    started, release = asyncio.Event(), asyncio.Event()
    interrupt = asyncio.Event()

    class GatedTts(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if sentence_index == -1:
                started.set()
                await release.wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)

    audio = FakeAudio(track_concurrency=True)
    handler = _handler(tts=GatedTts(), filler_profile="mary")
    task = reply_task(handler, hermes, audio, interrupt=interrupt,
                      ext=_ext(profile="atlas", filler_delay_ms=1,
                               filler_repeat_ms=1, filler_phrases=(("Checking.",),)))
    await started.wait()
    interrupt.set()
    await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0.01)
    assert not audio.sent
    hermes.release.set()
    await asyncio.wait_for(task, 0.3)
    assert "Late answer." in audio.spoken_text
    assert audio.max_concurrent_senders == 1


async def test_complete_long_sentence_is_not_cut_off_or_called_incomplete():
    hermes, tts = FakeHermes(["A complete answer. "]), FakeTts(frames_per_sentence=12)
    audio = FakeAudio(send_delay=0.005)
    handler = _handler(hermes=hermes, tts=tts)
    await reply_task(handler, hermes, audio,
                     ext=_ext(hermes_reply_timeout_seconds=0.02))
    assert audio.spoken_text == ["A complete answer."] * 12
    assert hermes.stream_closed
    assert not hermes.appended


async def test_transferred_filler_repeats_in_target_voice_and_caches():
    from test_stream_call import FakeBank

    hermes, tts = StalledHermes(), FakeTts(frames_per_sentence=1)
    audio = FakeAudio(track_concurrency=True)
    handler = _handler(hermes=hermes, tts=tts, filler_bank=FakeBank(),
                       filler_profile="mary")
    ext = _ext(profile="atlas", voice_id="atlas-voice", filler_delay_ms=1,
               filler_repeat_ms=5, filler_phrases=(("Checking.",), ("Still checking.",)))
    filler = []

    def on_sent(index, count):
        if index == -1:
            filler.append(audio.sent[-1][0].decode())
            if len(filler) == 3:
                hermes.release.set()

    audio.on_sent_hook = on_sent
    task = reply_task(handler, hermes, audio, ext=ext)
    try:
        done, _ = await asyncio.wait({task}, timeout=0.25)
        assert task in done, "transferred filler stopped after one phrase"
        await task
        assert filler[:3] == ["Checking.", "Still checking.", "Still checking."]
        calls = [call for call in tts.calls if call[2] == -1]
        assert calls == [("Checking.", "atlas-voice", -1),
                         ("Still checking.", "atlas-voice", -1)]
        assert audio.max_concurrent_senders == 1
        assert "Late answer." in audio.spoken_text
    finally:
        hermes.release.set()
        with contextlib.suppress(BaseException):
            await task


class BusyHermes(StalledHermes):
    """Tool/keepalive work produces no reply, or endless incomplete text."""
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.events = 0

    async def chat_stream(self, session_id, message):
        self.chats.append((session_id, message))
        self.started.set()
        try:
            if self.kind == "partial":
                yield "Here is what I know. "
            while not self.release.is_set():
                await asyncio.sleep(0.005)
                self.events += 1
                if self.kind == "trickle":
                    yield "word "
            yield "Late answer."
        finally:
            await asyncio.sleep(0)
            self.stream_closed = True


@pytest.mark.parametrize("kind", ["keepalive", "trickle", "partial"])
async def test_reply_budget_is_absolute_and_returns_to_listening(kind):
    hermes = BusyHermes(kind)
    audio, tts = FakeAudio(track_concurrency=True), FakeTts()
    handler = _handler(hermes=hermes, tts=tts)
    ext = _ext(hermes_reply_timeout_seconds=0.04, filler_delay_ms=1,
               filler_repeat_ms=5, filler_phrases=(("Checking.",),))
    task = reply_task(handler, hermes, audio, ext=ext)
    try:
        done, _ = await asyncio.wait({task}, timeout=0.25)
        assert task in done, "active stream defeated wall-clock reply budget"
        await task
        assert hermes.stream_closed
        assert hermes.events > 1
        assert any("taking too long" in text for text in audio.spoken_text)
        assert not hermes.appended, "timeout must not launch another agent run"
        assert audio.max_concurrent_senders == 1
        # Same handler and audio can answer the next caller turn.
        next_hermes = FakeHermes(["Next answer."])
        await reply_task(handler, next_hermes, audio, ext=ext)
        assert "Next answer." in audio.spoken_text
    finally:
        hermes.release.set()
        with contextlib.suppress(BaseException):
            await task


@pytest.mark.parametrize("exit_kind", ["ended", "deadline", "cancel"])
async def test_stalled_delta_exits_and_finalizes(exit_kind):
    hermes = StalledHermes()
    audio, ended = FakeAudio(), asyncio.Event()
    handler = _handler(hermes=hermes)
    deadline = asyncio.get_running_loop().time() + (0.03 if exit_kind == "deadline" else 10)
    task = reply_task(handler, hermes, audio, ended=ended, deadline=deadline)
    await hermes.started.wait()
    if exit_kind == "ended":
        ended.set()
    elif exit_kind == "cancel":
        task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done, "stalled delta ignored call termination"
        if exit_kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert hermes.stream_closed, "in-flight generator was not finalized"
        assert not audio.sent
    finally:
        hermes.release.set()
        with contextlib.suppress(BaseException):
            await task
