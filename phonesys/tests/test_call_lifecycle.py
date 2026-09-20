"""Offline Atlas call lifecycle regressions; no provider/media network."""
import asyncio
import time
import math

import pytest

from phonesys.config import load_config
from test_config import CONFIG_YAML, _write
from test_stream_call import (_ext, _handler, FakeAudio, FakeStt, FakeServer,
                              FakeHermes, ScriptedDtmf, FakeTts)
from test_background_feedback import call
from test_hold import say, until
from phonesys.native_work import WorkError
from test_stream_stalls import StalledHermes
from test_hold import MusicAri
from phonesys.hermes import HermesClient
from phonesys.stream_stt import SttEvent
from test_native_work_cancellation import gateway


@pytest.fixture(autouse=True)
def finite_asyncio_timers(monkeypatch):
    """Reject invalid/infinite timer handles on every exercised lifecycle."""
    original = asyncio.BaseEventLoop.call_later
    def checked(loop, delay, *args, **kwargs):
        assert math.isfinite(delay), f'invalid asyncio timer: {delay}'
        return original(loop, delay, *args, **kwargs)
    monkeypatch.setattr(asyncio.BaseEventLoop, 'call_later', checked)


async def test_active_atlas_survives_former_call_limit(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, enabled=False,
                    call_limit_enabled=False, max_call_seconds=.06,
                    idle_hangup_seconds=.4, idle_presence_grace_seconds=.1) as (audio, stt, hermes, status):
        for _ in range(3):
            say(stt, 'Keep talking')
            await asyncio.sleep(.05)
        assert not hermes.closed
        assert len(hermes.chats) == 3
        assert 'Dispatched.' in audio.spoken_text


async def test_idle_prompts_then_waits_full_grace_and_closes(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, enabled=False,
                    call_limit_enabled=False, idle_hangup_seconds=.05,
                    idle_presence_grace_seconds=.12) as (audio, stt, hermes, status):
        await until(lambda: any('Are you still there?' in s for s in audio.spoken_text))
        await asyncio.sleep(.05)
        assert not hermes.closed
        await until(lambda: hermes.closed)
        assert not hermes.chats and not hermes.appended
        assert len([s for s in audio.spoken_text if 'Are you still there?' in s]) == 1


async def test_presence_response_resets_idle_cycle(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, enabled=False,
                    call_limit_enabled=False, idle_hangup_seconds=.08,
                    idle_presence_grace_seconds=.04) as (audio, stt, hermes, status):
        await until(lambda: any('Are you still there?' in s for s in audio.spoken_text))
        say(stt, 'Yes I am here')
        await until(lambda: 'Dispatched.' in audio.spoken_text)
        await asyncio.sleep(.055)
        assert not hermes.closed
        assert len([s for s in audio.spoken_text if 'Are you still there?' in s]) == 1
        await until(lambda: len([s for s in audio.spoken_text if 'Are you still there?' in s]) == 2)


async def test_unknown_work_cannot_disable_presence_forever(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, call_limit_enabled=False,
                    idle_hangup_seconds=.05, idle_presence_grace_seconds=.08) as (audio, stt, hermes, status):
        await status('a', 'running')
        hermes._work.runs.get.side_effect = WorkError('offline')
        with pytest.raises(WorkError):
            await hermes._work.observe(hermes._work.jobs['a'])
        await asyncio.sleep(.12)
        assert not hermes.closed
        assert not any('Are you still there?' in s for s in audio.spoken_text)
        hermes._work.jobs['a']['deadline'] = time.time() - 1
        await until(lambda: any("can't confirm" in s for s in audio.spoken_text))
        await until(lambda: any('Are you still there?' in s for s in audio.spoken_text))
        await until(lambda: hermes.closed)
        assert 'a' in hermes._work.jobs, 'uncertainty must retain reconciliation metadata'
        assert hermes.work_statuses()['a'] == 'unknown'


async def test_completed_answer_stalled_synthesis_is_bounded_separately(monkeypatch, tmp_path):
    class BlockedAnswer(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if text == 'Dispatched.':
                await asyncio.Event().wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)
    async with call(monkeypatch, tmp_path, enabled=False, tts=BlockedAnswer(frames_per_sentence=1),
                    call_limit_enabled=False, hermes_reply_timeout_seconds=.04,
                    idle_hangup_seconds=.06, idle_presence_grace_seconds=.08) as (audio, stt, hermes, status):
        say(stt, 'Answer this')
        await until(lambda: any('trouble speaking' in s for s in audio.spoken_text))
        assert not hermes.closed
        await until(lambda: any('Are you still there?' in s for s in audio.spoken_text))


async def test_late_terminal_after_unknown_expiry_gets_result_notice(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, call_limit_enabled=False,
                    idle_hangup_seconds=.3, idle_presence_grace_seconds=.08) as (audio, stt, hermes, status):
        await status('a', 'running')
        hermes._work.jobs['a']['deadline'] = time.time() - 1
        await until(lambda: any("can't confirm" in s for s in audio.spoken_text))
        await status('a', 'completed')
        await until(lambda: any('ready for review' in s for s in audio.spoken_text))


async def test_failed_uncertainty_notice_still_requires_delivered_presence_warning(monkeypatch, tmp_path):
    class BrokenUncertainty(FakeTts):
        def __init__(self):
            super().__init__(frames_per_sentence=1)
            self.attempts = 0
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if "can't confirm" in text:
                self.attempts += 1
                raise RuntimeError('uncertainty notice unavailable')
            return await super().synthesize_frames(text, voice_id, sentence_index)
    tts = BrokenUncertainty()
    async with call(monkeypatch, tmp_path, tts=tts, call_limit_enabled=False,
                    idle_hangup_seconds=.05, idle_presence_grace_seconds=.12) as (audio, stt, hermes, status):
        await status('a', 'running')
        hermes._work.jobs['a']['deadline'] = time.time() - 1
        await until(lambda: tts.attempts == 1)
        assert not hermes.closed
        await until(lambda: any('Are you still there?' in s for s in audio.spoken_text))
        await asyncio.sleep(.05)
        assert not hermes.closed
        await until(lambda: hermes.closed)
        assert tts.attempts == 1 and 'a' in hermes._work.jobs
        assert not any("can't confirm" in s for s in audio.spoken_text)


@pytest.mark.parametrize('exit_kind', ['work_timeout', 'eof'])
async def test_consented_hold_not_idle_but_work_and_eof_still_bound_it(monkeypatch, tmp_path, exit_kind):
    class HeldWork(StalledHermes):
        work_statuses = HermesClient.work_statuses
        work_waiting_keys = HermesClient.work_waiting_keys
        def __init__(self, work):
            super().__init__()
            self._work = work
    ari = MusicAri()
    async with call(monkeypatch, tmp_path, enabled=False, ari=ari, hermes_type=HeldWork,
                    call_limit_enabled=False, max_call_seconds=.04,
                    idle_hangup_seconds=.03, idle_presence_grace_seconds=.08,
                    hermes_reply_timeout_seconds=.24) as (audio, stt, hermes, status):
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'put me on hold')
        await until(lambda: ari.music)
        await asyncio.sleep(.10)
        assert not hermes.closed and ari.music
        assert not any('Are you still there?' in s for s in audio.spoken_text)
        if exit_kind == 'eof':
            audio.hangup.set()
            await until(lambda: hermes.closed)
        else:
            await until(lambda: any('work time limit' in s for s in audio.spoken_text))
            assert not hermes.closed
            await until(lambda: any('Are you still there?' in s for s in audio.spoken_text))
        assert not ari.music
        assert hermes.stream_closed
        assert len(hermes.chats) == 1


async def test_completed_answer_playback_not_idle_or_generation_time(monkeypatch, tmp_path):
    class LongAnswer(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            self._n = 100 if text == 'Dispatched.' else 1
            return await super().synthesize_frames(text, voice_id, sentence_index)
    async with call(monkeypatch, tmp_path, enabled=False, tts=LongAnswer(),
                    call_limit_enabled=False, max_call_seconds=.04,
                    idle_hangup_seconds=.05, idle_presence_grace_seconds=.08,
                    hermes_reply_timeout_seconds=.04) as (audio, stt, hermes, status):
        say(stt, 'Answer this')
        await until(lambda: 'Dispatched.' in audio.spoken_text)
        await asyncio.sleep(.07)
        assert not hermes.closed and audio.is_sending
        assert not any('Are you still there?' in s for s in audio.spoken_text)
        await until(lambda: not audio.is_sending)
        assert len([s for s in audio.spoken_text if s == 'Dispatched.']) == 100
        assert not any('time limit' in s for s in audio.spoken_text)


async def test_caller_during_presence_synthesis_aborts_prompt_without_barge_or_hold(monkeypatch, tmp_path):
    class SlowPresence(FakeTts):
        def __init__(self):
            super().__init__(frames_per_sentence=1)
            self.started = asyncio.Event()
        async def synthesize_frames(self, text, voice_id, sentence_index):
            if 'Are you still there?' in text:
                self.started.set()
                await asyncio.Event().wait()
            return await super().synthesize_frames(text, voice_id, sentence_index)
    tts = SlowPresence()
    async with call(monkeypatch, tmp_path, enabled=False, tts=tts,
                    call_limit_enabled=False, idle_hangup_seconds=.05,
                    idle_presence_grace_seconds=.08) as (audio, stt, hermes, status):
        await asyncio.wait_for(tts.started.wait(), .5)
        say(stt, 'Still here')
        await until(lambda: 'Dispatched.' in audio.spoken_text)
        assert not any('Are you still there?' in s or 'hold music' in s for s in audio.spoken_text)
        assert len(hermes.chats) == 1 and not hermes.appended


@pytest.mark.parametrize('unlimited', [True, False])
async def test_transfer_adopts_unlimited_but_preserves_legacy_cap(unlimited):
    audio, stt, ari = FakeAudio(track_concurrency=True), FakeStt(), MusicAri()
    mary = _ext(receptionist=True, max_call_seconds=.06)
    atlas = _ext(profile='atlas', mode='streaming', full_name='Atlas Assistant',
                 directory_listing=True, access_pin='1234', hold_enabled=True,
                 call_limit_enabled=not unlimited, idle_hangup_seconds=.4,
                 idle_presence_grace_seconds=.08, greeting='Atlas online.')
    clients = {mary.profile: FakeHermes(), atlas.profile: FakeHermes(['An answer.'])}
    task = asyncio.create_task(_handler(ari=ari, stt=stt, tts=FakeTts(frames_per_sentence=1),
        server=FakeServer(audio), dtmf=ScriptedDtmf(['1234']),
        extensions={'7001': mary, '7003': atlas},
        hermes_factory=lambda ext: clients[ext.profile]).handle('transfer', '7001', mary))
    try:
        await until(lambda: mary.greeting in audio.spoken_text)
        say(stt, 'I need Atlas')
        await until(lambda: atlas.greeting in audio.spoken_text)
        await asyncio.sleep(.1)
        assert task.done() is not unlimited
        if unlimited:
            say(stt, 'Keep chatting')
            await until(lambda: 'An answer.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)
    assert stt.closed and clients[atlas.profile].closed
    assert set(ari.hungup) == {'transfer', 'as-chan'}
    assert audio.max_concurrent_senders == 1 and not audio.cancelled


async def test_unlimited_wait_uses_none_not_infinite_asyncio_timer():
    from phonesys.stream_call import _wait_any
    event = asyncio.Event()
    task = asyncio.create_task(_wait_any((event,), float('inf')))
    await asyncio.sleep(0)
    event.set()
    await asyncio.wait_for(task, .2)


async def test_presence_warning_must_actually_finish_playback():
    class DroppedAudio(FakeAudio):
        async def send_frames(self, frames, on_sent):
            return  # broken/closed transport without credited output
    delivered = await _handler()._background_notice(
        DroppedAudio(), _ext(), 'Are you still there?',
        interrupt=asyncio.Event(), ended=asyncio.Event(),
        deadline=float('inf'), allowed=lambda: True)
    assert delivered is False


async def test_stuck_completed_answer_sender_has_separate_transport_bound(monkeypatch):
    from phonesys import hold
    offset = 0
    real_now = hold.now
    monkeypatch.setattr(hold, 'now', lambda: real_now() + offset)
    class BlockedSender(FakeAudio):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()
            self.blocked = False
        async def send_frames(self, frames, on_sent):
            nonlocal offset
            if frames[0][0] == b'An answer.':
                self.blocked = True
                offset = 11  # exhaust frame duration + 10s without a real delay
                await self.release.wait()
                return
            return await super().send_frames(frames, on_sent)
        def stop_sending(self):
            self.release.set()
            super().stop_sending()
    baseline = asyncio.all_tasks()
    audio, hermes = BlockedSender(), FakeHermes(['An answer.'])
    await asyncio.wait_for(_handler(tts=FakeTts(frames_per_sentence=1))._reply(
        audio, _ext(profile='atlas', mode='streaming', hold_enabled=True,
                    call_limit_enabled=False, idle_hangup_seconds=30,
                    idle_presence_grace_seconds=20),
        hermes, 's', 'question', interrupt=asyncio.Event(),
        utterance_ready=asyncio.Event(), final_seen=asyncio.Event(),
        ended=asyncio.Event(), deadline=float('inf')), .5)
    assert audio.blocked and audio.release.is_set()
    assert any('trouble speaking' in s for s in audio.spoken_text)
    assert hermes.stream_closed
    assert not (asyncio.all_tasks() - baseline)


async def test_vad_without_final_resets_grace_not_just_deadline(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, enabled=False,
                    call_limit_enabled=False, idle_hangup_seconds=.12,
                    idle_presence_grace_seconds=.04) as (audio, stt, hermes, status):
        await until(lambda: any('Are you still there?' in s for s in audio.spoken_text))
        stt.emit(SttEvent(kind='speech_started'))
        await asyncio.sleep(.07)
        assert not hermes.closed
        assert len([s for s in audio.spoken_text if 'Are you still there?' in s]) == 1


async def test_unlimited_stt_eof_still_apologizes_and_tears_down(monkeypatch, tmp_path):
    async with call(monkeypatch, tmp_path, enabled=False,
                    call_limit_enabled=False, idle_hangup_seconds=.3,
                    idle_presence_grace_seconds=.08) as (audio, stt, hermes, status):
        stt.emit(None)
        await until(lambda: hermes.closed)
        from phonesys.stream_call import STT_FAILURE_MESSAGE
        assert STT_FAILURE_MESSAGE in audio.spoken_text


async def test_expired_unknown_work_retains_authority_through_real_close(gateway):
    work, state = gateway
    work.jobs['a']['deadline'] = time.time() - 1
    state.update(fail_get=10, fail_stop=True)
    assert work.waiting_keys() == set()
    assert work.jobs['a']['run_id'] == 'run_known'
    await asyncio.wait_for(work.close(), 1)
    assert work.record_path.exists()
    assert work.jobs['a']['termination_unconfirmed']
    assert any(path.endswith('/stop') for _, path, _ in state['requests'])
    assert work.runs.client.is_closed


def test_shipped_atlas_only_and_work_budget_unchanged(monkeypatch):
    from pathlib import Path
    import yaml
    path = Path(__file__).parents[1] / 'config.yaml'
    raw = yaml.safe_load(path.read_text())
    for name in ('ARI_USER', 'ARI_PASSWORD'):
        monkeypatch.setenv(name, 'dummy')
    for ext in raw['extensions'].values():
        for key in ('hermes_api_key_env', 'access_pin_env'):
            if key in ext:
                monkeypatch.setenv(ext[key], 'dummy')
    config = load_config(path)
    atlas = config.extensions['7003']
    assert not atlas.call_limit_enabled
    assert atlas.idle_hangup_seconds == 30 and atlas.idle_presence_grace_seconds == 20
    assert atlas.hermes_reply_timeout_seconds == 120
    assert all(ext.call_limit_enabled and ext.max_call_seconds == 600
               and ext.idle_presence_grace_seconds == 0
               for key, ext in config.extensions.items() if key != '7003')


def test_explicit_unlimited_atlas_config(tmp_path, monkeypatch):
    for key in ('ARI_USER', 'ARI_PASSWORD', 'HERMES_PHONEBOT_API_KEY'):
        monkeypatch.setenv(key, 'dummy')
    raw = CONFIG_YAML.replace('profile: phonebot', 'profile: atlas') + '''    mode: streaming
    hold_enabled: true
    call_limit_enabled: false
    idle_hangup_seconds: 30
    idle_presence_grace_seconds: 20
'''
    ext = load_config(_write(tmp_path, raw)).extensions['7000']
    assert ext.call_limit_enabled is False
    assert ext.max_call_seconds == 600
    assert ext.idle_presence_grace_seconds == 20
    assert _ext().call_limit_enabled is True
    assert _ext().idle_presence_grace_seconds == 0


@pytest.mark.parametrize('settings', [
    {'call_limit_enabled': 'false'},
    {'call_limit_enabled': False},
    {'profile': 'atlas', 'call_limit_enabled': False},
    {'profile': 'atlas', 'mode': 'streaming', 'call_limit_enabled': False},
    {'profile': 'atlas', 'mode': 'streaming', 'call_limit_enabled': False,
     'idle_hangup_seconds': 30, 'idle_presence_grace_seconds': 20},
    {'idle_presence_grace_seconds': float('inf')},
    {'idle_presence_grace_seconds': -1},
])
def test_invalid_lifecycle_configuration_rejected(settings):
    with pytest.raises(ValueError):
        _ext(**settings)
