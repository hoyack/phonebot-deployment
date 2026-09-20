"""Native status-only feedback through the real duplex call handler."""
import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from phonesys import stream_call
from phonesys.hermes import HermesClient
from phonesys.native_work import WorkCall, WorkError
from phonesys.stream_stt import SttEvent
from test_hold import say, until
from test_stream_call import FakeAudio, FakeStt, FakeTts, FakeServer, FakeHermes, _ext, _handler


class WorkHermes(FakeHermes):
    work_statuses = HermesClient.work_statuses

    def __init__(self, work):
        super().__init__(['Dispatched.'])
        self._work = work


@asynccontextmanager
async def call(monkeypatch, tmp_path, *, tts=None, enabled=True, hermes_type=WorkHermes, ari=None, **settings):
    baseline = asyncio.all_tasks()
    for name, value in [('BACKGROUND_CHECK_SECONDS', .01),
                        ('BACKGROUND_FEEDBACK_SECONDS', .06),
                        ('BACKGROUND_QUIET_SECONDS', .04)]:
        monkeypatch.setattr(stream_call, name, value, raising=False)
    work = WorkCall('call', 'http://localhost', 'key', tmp_path, tmp_path / 'config')
    work.runs.get = AsyncMock(return_value={'status': 'running', 'output': 'SECRET'})
    work.runs.stop = AsyncMock()
    async def status(key, state):
        job = work.jobs.setdefault(key, {'run_id': 'run_' + key, 'deadline': time.time() + 120})
        work.runs.get.return_value = {'status': state, 'output': 'SECRET'}
        await work.observe(job)
    audio, stt, hermes = FakeAudio(track_concurrency=True), FakeStt(), hermes_type(work)
    from phonesys.config import NativeWorkConfig
    from test_stream_call import ScriptedDtmf
    config = NativeWorkConfig('http://127.0.0.1:8642/p/worker', tmp_path,
                              tmp_path / 'worker' / 'config.yaml')
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt, ari=ari,
        dtmf=ScriptedDtmf(['1234']),
        server=FakeServer(audio), tts=tts or FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True,
        mode='streaming', access_pin='1234',
        native_work=config if enabled else None, **settings)))
    try:
        await until(lambda: bool(audio.sent))
        yield audio, stt, hermes, status
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)
        await work.runs.client.aclose()
        assert not audio.cancelled
        assert audio.max_concurrent_senders == 1
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - baseline)


async def test_background_feedback_after_dispatch_is_periodic_and_bounded(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path) as (audio, stt, hermes, status):
        say(stt, 'Research this')
        await until(lambda: 'Dispatched.' in audio.spoken_text)
        await status('a', 'running')
        await until(lambda: any('background work is still running' in s for s in audio.spoken_text))
        await asyncio.sleep(.4)
        notices = [s for s in audio.spoken_text if 'background work is still running' in s]
        assert len(notices) == 4
        await asyncio.sleep(.1)
        assert len([s for s in audio.spoken_text if s in notices]) == 4
        assert len(hermes.chats) == 1 and not hermes.appended
        assert hermes._work.runs.get.await_count == 1, 'feedback polled native API'
        hermes._work.runs.stop.assert_not_awaited()


@pytest.mark.parametrize('state, phrase', [('completed', 'ready for review'),
    ('failed', 'did not complete'), ('interrupted', 'did not complete'),
    ('cancelled', 'did not complete')])
async def test_terminal_notice_once_no_results_or_new_turn(monkeypatch, tmp_path, state, phrase):
    async with call(monkeypatch, tmp_path) as (audio, stt, hermes, status):
        await status('a', state)
        await until(lambda: any(phrase in s for s in audio.spoken_text))
        await asyncio.sleep(.2)
        assert len([s for s in audio.spoken_text if phrase in s]) == 1
        assert not any('SECRET' in s for s in audio.spoken_text)
        assert not hermes.chats and not hermes.appended
        hermes._work.runs.stop.assert_not_awaited()
        say(stt, 'Please review the results')
        await until(lambda: len(hermes.chats) == 1)
        assert hermes.chats[0][1] == 'Please review the results'


async def test_simultaneous_terminal_subjects_coalesce_without_replaying_old_subjects(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path) as (audio, stt, hermes, status):
        await status('a', 'completed')
        await status('b', 'failed')
        await until(lambda: any('ready for review' in s for s in audio.spoken_text))
        notices = [s for s in audio.spoken_text if 'ready for review' in s]
        assert len(notices) == 1 and 'did not complete' in notices[0]
        await status('c', 'completed')
        await until(lambda: len([s for s in audio.spoken_text if 'ready for review' in s]) == 2)
        assert 'did not complete' not in audio.spoken_text[-1]
        await asyncio.sleep(.15)
        assert len([s for s in audio.spoken_text if 'ready for review' in s]) == 2


async def test_notice_barge_stops_audio_not_jobs_or_conversation(monkeypatch, tmp_path):
    class LongNotice(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            self._n = 80 if 'ready for review' in text else 1
            return await super().synthesize_frames(text, voice_id, sentence_index)
    async with call(monkeypatch, tmp_path, tts=LongNotice()) as (audio, stt, hermes, status):
        def barge(index, count):
            if 'ready for review' in audio.spoken_text[-1] and count == 1:
                stt.emit(SttEvent(kind='speech_started'))
        audio.on_sent_hook = barge
        await status('a', 'completed')
        await status('b', 'running')
        await until(lambda: any('ready for review' in s for s in audio.spoken_text))
        await until(lambda: not audio.is_sending)
        assert len([s for s in audio.spoken_text if 'ready for review' in s]) <= 2
        assert hermes.work_statuses()['b'] == 'running'
        hermes._work.runs.stop.assert_not_awaited()
        assert not hermes.chats and not hermes.appended
        await asyncio.sleep(.15)
        assert len([s for s in audio.spoken_text if 'ready for review' in s]) <= 2


async def test_speech_during_notice_synthesis_returns_listening_without_audio(monkeypatch, tmp_path):
    class BlockedNotice(FakeTts):
        def __init__(self):
            super().__init__(frames_per_sentence=1)
            self.started = asyncio.Event()
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if 'ready for review' in text:
                self.started.set()
                await asyncio.Event().wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)
    tts = BlockedNotice()
    async with call(monkeypatch, tmp_path, tts=tts) as (audio, stt, hermes, status):
        await status('a', 'completed')
        await asyncio.wait_for(tts.started.wait(), .7)
        say(stt, 'A different subject')
        await until(lambda: bool(hermes.chats))
        assert hermes.chats[0][1] == 'A different subject'
        assert not any('ready for review' in s for s in audio.spoken_text)


async def test_observation_outage_does_not_idle_cancel_an_accepted_job(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, idle_hangup_seconds=.025) as (audio, stt, hermes, status):
        await status('a', 'running')
        hermes._work.runs.get.side_effect = WorkError('offline')
        with pytest.raises(WorkError):
            await hermes._work.observe(hermes._work.jobs['a'])
        await asyncio.sleep(.1)
        assert not hermes.closed, 'unknown observation prematurely tore down accepted work'
        assert not any('background work is still running' in s for s in audio.spoken_text)


async def test_waiting_work_outlives_idle_hangup_and_notice_allows_response(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, idle_hangup_seconds=.025) as (audio, stt, hermes, status):
        await status('a', 'running')
        await until(lambda: any('background work is still running' in s for s in audio.spoken_text))
        assert not hermes.closed
        await status('a', 'completed')
        await until(lambda: any('ready for review' in s for s in audio.spoken_text))
        assert not hermes.closed
        say(stt, 'Review that')
        await until(lambda: bool(hermes.chats))


@pytest.mark.parametrize('state', ['running', 'completed', 'failed', 'stopping', 'queued'])
async def test_disabled_native_work_never_notifies(monkeypatch, tmp_path, state):
    async with call(monkeypatch, tmp_path, enabled=False) as (audio, stt, hermes, status):
        await status('a', state)
        await asyncio.sleep(.15)
        assert not any('background' in s.lower() for s in audio.spoken_text)


async def test_floor_defers_notice_until_caller_finished(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path) as (audio, stt, hermes, status):
        await status('a', 'completed')
        for _ in range(8):
            stt.emit(SttEvent(kind='transcript', text='still explaining', is_final=False))
            await asyncio.sleep(.02)
        assert not any('ready for review' in s for s in audio.spoken_text)
        say(stt, 'Now answer this')
        await until(lambda: 'Dispatched.' in audio.spoken_text)
        await until(lambda: any('ready for review' in s for s in audio.spoken_text))


async def test_hold_and_foreground_barge_leave_separate_subjects_alive(monkeypatch, tmp_path):
    from test_hold import MusicAri
    from test_stream_stalls import StalledHermes
    class HeldWork(StalledHermes):
        work_statuses = HermesClient.work_statuses
        def __init__(self, work):
            super().__init__()
            self._work = work
    ari = MusicAri()
    monkeypatch.setattr(stream_call, 'POST_BARGE_QUIET_SECONDS', .04)
    async with call(monkeypatch, tmp_path, hermes_type=HeldWork, ari=ari,
                    hold_offer_seconds=.02) as (audio, stt, hermes, status):
        say(stt, 'Research a third subject')
        await hermes.started.wait()
        await status('a', 'running')
        await status('b', 'running')
        await until(lambda: any('Would you like hold music' in s for s in audio.spoken_text))
        assert not ari.music
        say(stt, 'yes')
        await until(lambda: ari.music)
        await status('a', 'completed')
        await asyncio.sleep(.15)
        assert not any('Background' in s or 'background work' in s for s in audio.spoken_text)
        say(stt, 'take me off hold')
        await until(lambda: not ari.music)
        say(stt, 'Now another subject')
        await until(lambda: len(hermes.chats) == 2)
        assert hermes.work_statuses() == {'a': 'completed', 'b': 'running'}
        hermes._work.runs.stop.assert_not_awaited()
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
        await until(lambda: any('ready for review' in s for s in audio.spoken_text))
        hermes._work.runs.stop.assert_not_awaited()


async def test_no_running_claim_after_state_changes_during_tts(monkeypatch, tmp_path):
    class SlowProgress(FakeTts):
        def __init__(self):
            super().__init__(frames_per_sentence=1)
            self.started, self.release = asyncio.Event(), asyncio.Event()
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if 'background work is still running' in text:
                self.started.set()
                await self.release.wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)
    tts = SlowProgress()
    async with call(monkeypatch, tmp_path, tts=tts) as (audio, stt, hermes, status):
        await status('a', 'running')
        await asyncio.wait_for(tts.started.wait(), .7)
        await status('a', 'completed')
        tts.release.set()
        await until(lambda: any('ready for review' in s for s in audio.spoken_text))
        assert not any('background work is still running' in s for s in audio.spoken_text)


async def test_notice_tts_failure_does_not_end_call_or_repeat(monkeypatch, tmp_path):
    class BrokenNotice(FakeTts):
        def __init__(self):
            super().__init__(frames_per_sentence=1)
            self.attempts = 0
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if 'ready for review' in text:
                self.attempts += 1
                raise RuntimeError('TTS offline')
            return await super().synthesize_frames(text, voice_id, sentence_index)
    tts = BrokenNotice()
    async with call(monkeypatch, tmp_path, tts=tts) as (audio, stt, hermes, status):
        await status('a', 'completed')
        await until(lambda: tts.attempts == 1)
        await asyncio.sleep(.15)
        assert tts.attempts == 1 and not hermes.closed
        say(stt, 'Continue chatting')
        await until(lambda: bool(hermes.chats))


async def test_hangup_during_notice_synthesis_drains_all_tasks(monkeypatch, tmp_path):
    class BlockedNotice(FakeTts):
        def __init__(self):
            super().__init__(frames_per_sentence=1)
            self.started = asyncio.Event()
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if 'ready for review' in text:
                self.started.set()
                await asyncio.Event().wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)
    tts = BlockedNotice()
    async with call(monkeypatch, tmp_path, tts=tts) as (audio, stt, hermes, status):
        await status('a', 'completed')
        await asyncio.wait_for(tts.started.wait(), .7)
        # The fixture hangs up and asserts real handler/pump/synthesis cleanup.


async def test_snapshot_expiry_unknown_states_and_fresh_terminal(monkeypatch, tmp_path):
    import phonesys.native_work as native
    clock = 100.0
    from types import SimpleNamespace
    monkeypatch.setattr(native, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock))
    work = WorkCall('call', 'http://localhost', 'key', tmp_path, tmp_path / 'config')
    job = {'run_id': 'run_a', 'deadline': time.time() + 120}
    work.jobs['a'] = job
    work.runs.get = AsyncMock(return_value={'status': 'running'})
    try:
        await work.observe(job)
        clock = 107.0
        assert work.statuses() == {'a': 'unknown'}
        work.runs.get.return_value = {'status': 'completed', 'output': 'SECRET'}
        await work.observe(job)
        clock = 1000.0
        assert work.statuses() == {'a': 'completed'}
    finally:
        await work.runs.client.aclose()


@pytest.mark.parametrize('state', ['queued', 'stopping', 'acceptance_unknown'])
async def test_nonrunning_states_do_not_claim_progress(monkeypatch, tmp_path, state):
    async with call(monkeypatch, tmp_path) as (audio, stt, hermes, status):
        await status('a', state)
        await asyncio.sleep(.15)
        assert not any('background' in s.lower() for s in audio.spoken_text)


async def test_cached_status_surface_never_polls_or_exposes_output(tmp_path):
    work = WorkCall('call', 'http://localhost', 'key', tmp_path, tmp_path / 'config')
    client = HermesClient('http://localhost', 'key')
    client._work = work
    job = {'run_id': 'run_a', 'deadline': time.time() + 120}
    work.jobs['a'] = job
    work.runs.get = AsyncMock(return_value={'status': 'running', 'output': 'SECRET'})
    try:
        await work.observe(job)
        assert client.work_statuses() == {'a': 'running'}
        assert client.work_statuses() == {'a': 'running'}
        assert work.runs.get.await_count == 1
        work.runs.get.side_effect = WorkError('offline')
        with pytest.raises(WorkError):
            await work.observe(job)
        assert client.work_statuses() == {'a': 'unknown'}, 'failed observation must invalidate running claim'
    finally:
        client._work = None
        await client.aclose()
        await work.runs.client.aclose()
