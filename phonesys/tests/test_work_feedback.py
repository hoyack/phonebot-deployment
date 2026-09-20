"""Bounded feedback policy with fake time; no providers or live calls."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from phonesys import hold
from phonesys.hermes import HermesEvent
from phonesys.stream_call import HeardTracker
from test_hold import MusicAri, until
from test_stream_call import FakeAudio, FakeTts, _ext
from test_stream_stalls import StalledHermes


class FeedbackRun:
    def __init__(self, monkeypatch, *, suppress_ack=True, hermes=None, tts=None, **settings):
        self.clock = 0.0
        monkeypatch.setattr(hold, 'now', lambda: self.clock)
        self.audio = FakeAudio(track_concurrency=True)
        self.tts = tts or FakeTts(frames_per_sentence=1)
        self.ari = MusicAri()
        self.hermes = hermes or StalledHermes()
        self.pending = []
        self.floor = False
        self.ended = asyncio.Event()
        self.interrupt = asyncio.Event()
        self.heard = []
        self.audio.on_sent_hook = self.on_frame
        options = dict(profile='atlas', hold_enabled=True, filler_delay_ms=800,
                       hold_offer_seconds=15, hold_feedback_seconds=15,
                       hermes_reply_timeout_seconds=120)
        options.update(settings)
        self.task = asyncio.create_task(hold.run_hold_reply(
            audio=self.audio, ext=_ext(**options), hermes=self.hermes,
            session_id='s', utterance='Research this', ari=self.ari,
            channel_id='caller', ended=self.ended, deadline=1000,
            read_utterance=lambda: self.pending[0] if self.pending else '',
            consume=self.pending.clear, tts=self.tts, tracker=HeardTracker(),
            suppress_ack=suppress_ack, caller_has_floor=lambda: self.floor,
            interrupt=self.interrupt))

    def on_frame(self, index, count):
        assert not self.floor, 'feedback stole the caller floor'
        assert not self.ari.music, 'speech overlapped hold music'
        self.heard.append((self.clock, self.audio.spoken_text[-1]))

    async def advance(self, seconds):
        self.clock = seconds
        # Only the scheduler runs in real time. The production policy clock
        # is frozen, so even a loaded test runner cannot cross a boundary.
        await asyncio.sleep(0.065)

    async def control(self, text):
        self.pending.append(text)
        await until(lambda: not self.pending)
        await asyncio.sleep(0.02)


@asynccontextmanager
async def running(monkeypatch, **settings):
    baseline = asyncio.all_tasks()
    run = FeedbackRun(monkeypatch, **settings)
    try:
        await run.hermes.started.wait()
        yield run
    finally:
        run.ended.set()
        await asyncio.wait_for(run.task, 0.7)
        assert run.hermes.stream_closed
        assert not run.audio.is_sending and not run.audio.cancelled
        assert not run.ari.music
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - baseline), 'controller leaked tasks'


async def test_replacement_gets_bounded_feedback_not_an_immediate_duplicate(monkeypatch):
    async with running(monkeypatch) as run:
        await run.advance(0.8)
        assert run.heard == []
        await run.advance(2.99)
        assert run.heard == []
        await run.advance(3.0)
        assert run.heard == [(3.0, hold.STATUS)]
        await run.advance(13.87)
        assert run.heard == [(3.0, hold.STATUS)]
        assert not run.task.done() and not run.hermes.stream_closed
        run.hermes.release.set()
        await asyncio.wait_for(run.task, 0.7)
        assert run.audio.spoken_text == [hold.STATUS, 'Late answer.']
        assert run.audio.max_concurrent_senders == 1


@pytest.mark.parametrize('suppress_ack', [False, True])
async def test_floor_cancels_immediate_ack_but_bounds_feedback_after_noise(monkeypatch, suppress_ack):
    async with running(monkeypatch, suppress_ack=suppress_ack) as run:
        run.floor = True
        await run.advance(0.8)
        await run.advance(5)
        assert run.heard == []
        run.floor = False  # owner's noise/floor policy released the claim
        await run.advance(5)
        assert run.heard == [], 'stale feedback fired at floor release'
        await run.advance(7.99)
        assert run.heard == []
        await run.advance(8)
        assert run.heard == [(8, hold.STATUS)]


async def test_explicit_status_satisfies_deferred_feedback(monkeypatch):
    async with running(monkeypatch) as run:
        await run.advance(2)
        await run.control('status')
        assert run.audio.spoken_text == [hold.STATUS]
        await run.advance(3)
        assert run.audio.spoken_text == [hold.STATUS], 'timer repeated the status reply'


async def test_due_hold_offer_supersedes_deferred_update(monkeypatch):
    async with running(monkeypatch) as run:
        # A delayed scheduler must not speak both overdue timers in a burst.
        await run.advance(15)
        assert run.audio.spoken_text == [hold.OFFER]
        assert not run.ari.starts, 'offer is not consent'


async def test_normal_ack_is_prompt_and_periodic_feedback_remains_restrained(monkeypatch):
    async with running(monkeypatch, suppress_ack=False) as run:
        await run.advance(0.79)
        assert run.heard == []
        await run.advance(0.8)
        assert run.heard == [(0.8, "I'm working on your request.")]
        await run.advance(3)
        assert len(run.heard) == 1
        await run.advance(15)
        assert run.audio.spoken_text[-1] == hold.OFFER
        await run.advance(29.99)
        assert len(run.heard) == 2
        await run.advance(30)
        assert run.heard[-1] == (30, hold.STATUS)
        await run.advance(43.51)
        assert len(run.heard) == 3
        assert not run.hermes.stream_closed
        run.hermes.release.set()
        await asyncio.wait_for(run.task, 0.7)
        assert run.audio.spoken_text[-1] == 'Late answer.'


async def test_disabled_filler_stays_disabled(monkeypatch):
    async with running(monkeypatch, filler_delay_ms=0) as run:
        await run.advance(3)
        assert not run.heard
        await run.advance(15)
        assert run.audio.spoken_text == [hold.OFFER]


@pytest.mark.parametrize('control', ['put me on hold', 'wait', 'cancel'])
async def test_controls_win_over_due_deferred_feedback(monkeypatch, control):
    async with running(monkeypatch) as run:
        run.pending.append(control)
        await run.advance(3)
        if control == 'cancel':
            await asyncio.wait_for(run.task, 0.7)
            assert run.hermes.stream_closed
            assert run.audio.spoken_text == ["Okay, I've requested cancellation of this request."]
        else:
            assert not run.heard
            assert not run.hermes.stream_closed
            await run.advance(30)
            assert not run.heard
            if control == 'put me on hold':
                assert run.ari.music and len(run.ari.starts) == 1
                await run.control('right')
                assert run.ari.music and not run.ari.stops
                await run.control('status')
                await until(lambda: run.ari.music)
                assert run.audio.spoken_text == [hold.STATUS]
                await run.control('take me off hold')
                assert not run.ari.music
                assert len(run.hermes.chats) == 1


async def test_ready_answer_wins_over_due_feedback(monkeypatch):
    async with running(monkeypatch) as run:
        run.floor = True
        await run.advance(2)
        run.hermes.release.set()
        await until(lambda: run.hermes.stream_closed)
        run.clock = 15  # answer and both feedback timers ready together
        run.floor = False
        await asyncio.wait_for(run.task, 0.7)
        assert run.audio.spoken_text == ['Late answer.']


async def test_deferred_update_uses_structured_progress_without_finishing_work(monkeypatch):
    class Tools(StalledHermes):
        async def chat_events(self, session_id, message):
            self.started.set()
            try:
                yield HermesEvent('tool.completed', "An operation has finished. I'm still working.")
                await self.release.wait()
                yield HermesEvent('assistant.delta', 'Final answer.')
                yield HermesEvent('done')
            finally:
                self.stream_closed = True

    async with running(monkeypatch, hermes=Tools()) as run:
        await run.advance(3)
        assert run.audio.spoken_text == ["An operation has finished. I'm still working."]
        assert not run.task.done() and not run.hermes.stream_closed


async def test_floor_reclaimed_during_deferred_synthesis_prevents_audio(monkeypatch):
    class BlockedTts(FakeTts):
        def __init__(self):
            super().__init__(frames_per_sentence=1)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def synthesize_frames(self, text, voice_id, sentence_index):
            self.started.set()
            await self.release.wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)

    tts = BlockedTts()
    async with running(monkeypatch, tts=tts) as run:
        run.clock = 3
        await asyncio.wait_for(tts.started.wait(), 0.7)
        run.floor = True
        tts.release.set()
        await run.advance(4)
        assert not run.heard
        run.floor = False
        await run.advance(4)
        assert not run.heard
        await run.advance(7)
        assert run.heard == [(7, hold.STATUS)]


async def test_new_request_is_banked_instead_of_deferred_feedback(monkeypatch):
    async with running(monkeypatch) as run:
        run.pending.append('Research a different topic')
        await run.advance(3)
        await asyncio.wait_for(run.task, 0.7)
        assert run.pending == ['Research a different topic']
        assert not run.heard


async def test_feedback_does_not_extend_work_cutoff(monkeypatch):
    async with running(monkeypatch, hermes_reply_timeout_seconds=10) as run:
        await run.advance(3)
        assert run.audio.spoken_text == [hold.STATUS]
        await run.advance(10)
        await asyncio.wait_for(run.task, 0.7)
        assert 'work time limit' in run.audio.spoken_text[-1]
        assert run.hermes.stream_closed


async def test_cancellation_during_deferred_synthesis_drains_tasks(monkeypatch):
    class BlockedTts(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            started.set()
            await asyncio.Event().wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)

    baseline = asyncio.all_tasks()
    started = asyncio.Event()
    run = FeedbackRun(monkeypatch, tts=BlockedTts())
    await run.hermes.started.wait()
    run.clock = 3
    await asyncio.wait_for(started.wait(), 0.7)
    run.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run.task, 0.7)
    assert not run.heard and run.hermes.stream_closed
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
