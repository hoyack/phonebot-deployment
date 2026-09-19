"""Offline consent/long-work regressions, including the real call handler."""
import asyncio
import contextlib
from dataclasses import replace

import pytest

from test_stream_call import FakeAudio, FakeStt, FakeTts, FakeHermes, _ext, _handler
from test_stream_stalls import StalledHermes, reply_task
from phonesys.hermes import HermesEvent, HermesError
from phonesys.stream_stt import SttEvent
from test_stream_call import FakeAri, FakeServer


class MusicAri(FakeAri):
    def __init__(self):
        super().__init__()
        self.music = False
        self.starts = []
        self.stops = []

    async def start_moh(self, channel, moh_class):
        self.starts.append((channel, moh_class))
        self.music = True

    async def stop_moh(self, channel):
        self.stops.append(channel)
        self.music = False


def say(stt, text):
    stt.emit(SttEvent(kind='transcript', text=text, is_final=True))
    stt.emit(SttEvent(kind='utterance_end'))


async def until(predicate):
    async with asyncio.timeout(0.7):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_consent_yes_keeps_one_pending_run_and_stops_music_before_result():
    ari, hermes = MusicAri(), StalledHermes()
    audio = FakeAudio(track_concurrency=True)
    stt = FakeStt(audio)
    tts = FakeTts(frames_per_sentence=1)
    handler = _handler(ari=ari, hermes=hermes, stt=stt, tts=tts,
                       server=FakeServer(audio))
    ext = _ext(hold_enabled=True, hold_offer_seconds=0.015,
               hold_feedback_seconds=0.1, hermes_reply_timeout_seconds=300)
    def frame(index, count):
        assert not ari.music, 'MOH overlaps spoken audio'
    audio.on_sent_hook = frame
    task = asyncio.create_task(handler.handle('caller', '7003', ext))
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await hermes.started.wait()
        await until(lambda: any('hold music' in s for s in audio.spoken_text))
        say(stt, 'yes')
        await until(lambda: ari.music)
        assert hermes.chats == [('call-caller', 'Research this')]
        assert not hermes.stream_closed
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
        assert ari.starts == [('caller', 'phonesys_hold')]
        assert ari.stops and not ari.music
        assert audio.max_concurrent_senders == 1
        assert stt.sent
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)
    assert hermes.stream_closed


@pytest.mark.parametrize('control', ['no', 'status', 'stop the music', 'put me on hold', 'cancel'])
async def test_local_controls_keep_run_except_explicit_cancel(control):
    ari, hermes = MusicAri(), StalledHermes()
    audio = FakeAudio(track_concurrency=True)
    stt = FakeStt(audio)
    handler = _handler(ari=ari, hermes=hermes, stt=stt,
                       tts=FakeTts(frames_per_sentence=1), server=FakeServer(audio))
    ext = _ext(hold_enabled=True, hold_offer_seconds=0.01, hold_feedback_seconds=0.04)
    task = asyncio.create_task(handler.handle('caller', '7003', ext))
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await until(lambda: any('hold music' in s for s in audio.spoken_text))
        if control == 'stop the music':
            say(stt, 'yes')
            await until(lambda: ari.music)
        before = len(audio.spoken_text)
        say(stt, control)
        if control == 'put me on hold':
            await until(lambda: ari.music)
        else:
            await until(lambda: len(audio.spoken_text) > before)
        assert len(hermes.chats) == 1, 'local control started another Hermes turn'
        if control == 'cancel':
            await until(lambda: hermes.stream_closed)
        else:
            assert not hermes.stream_closed
            if control in ('no', 'stop the music'):
                assert not ari.music
            hermes.release.set()
            await until(lambda: 'Late answer.' in audio.spoken_text)
        assert audio.max_concurrent_senders == 1
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)
    assert not ari.music


async def test_typed_progress_is_periodic_and_partial_text_is_not_completion():
    class Tools(StalledHermes):
        async def chat_events(self, session_id, message):
            self.chats.append((session_id, message))
            try:
                yield HermesEvent('assistant.delta', 'A partial finding. ')
                yield HermesEvent('tool.started', "I'm searching for information.")
                await self.release.wait()
                yield HermesEvent('assistant.delta', 'The final answer.')
                yield HermesEvent('done')
            finally:
                self.stream_closed = True
    hermes, audio = Tools(), FakeAudio()
    task = reply_task(_handler(), hermes, audio,
                      ext=_ext(hold_enabled=True, hold_offer_seconds=0.01,
                               hold_feedback_seconds=0.02))
    try:
        await until(lambda: any('searching for information' in t for t in audio.spoken_text))
        assert 'A partial finding.' in audio.spoken_text
        assert not task.done() and not hermes.stream_closed
        hermes.release.set()
        await asyncio.wait_for(task, 0.5)
        assert 'The final answer.' in audio.spoken_text
    finally:
        hermes.release.set()
        await task


async def test_opted_in_hold_is_uninterrupted_until_the_final_answer():
    """Narration/tool progress must not repeatedly tear down active MOH."""
    class Tools(StalledHermes):
        async def chat_events(self, session_id, message):
            self.chats.append((session_id, message))
            try:
                yield HermesEvent('assistant.delta', 'Let me keep searching. ')
                yield HermesEvent('tool.started', "I'm searching for information.")
                yield HermesEvent('tool.completed', "An operation has finished. I'm still working.")
                await self.release.wait()
                yield HermesEvent('assistant.delta', 'The final answer.')
                yield HermesEvent('done')
            finally:
                self.stream_closed = True

    ari, hermes, audio = MusicAri(), Tools(), FakeAudio()
    pending = ['put me on hold']
    task = asyncio.create_task(_handler(
        ari=ari, tts=FakeTts(frames_per_sentence=1))._reply(
        audio, _ext(hold_enabled=True, hold_offer_seconds=0.01,
                    hold_feedback_seconds=0.01), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+2, channel_id='caller',
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await until(lambda: ari.music)
        await asyncio.sleep(0.04)  # cross several former feedback intervals
        assert ari.stops == []
        assert audio.spoken_text == []
        hermes.release.set()
        await asyncio.wait_for(task, 0.5)
        assert ari.starts == [('caller', 'phonesys_hold')]
        assert ari.stops == ['caller']
        assert audio.spoken_text == ['The final answer.']
    finally:
        hermes.release.set()
        await task


@pytest.mark.parametrize('backchannel', ['Right.', 'uh huh', 'mhm', 'mm hmm', 'got it'])
async def test_backchannel_during_hold_keeps_music_and_original_run(backchannel):
    """A brief acknowledgement must not become a second Hermes request."""
    ari, hermes, audio = MusicAri(), StalledHermes(), FakeAudio()
    stt = FakeStt(audio)
    task = asyncio.create_task(_handler(
        ari=ari, hermes=hermes, stt=stt,
        tts=FakeTts(frames_per_sentence=1), server=FakeServer(audio)).handle(
        'caller', '7003', _ext(hold_enabled=True, hold_offer_seconds=0.01,
                              hold_feedback_seconds=1)))
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'put me on hold')
        await until(lambda: ari.music)
        before = list(audio.spoken_text)
        say(stt, backchannel)
        await asyncio.sleep(0.04)
        assert ari.music
        assert ari.stops == []
        assert audio.spoken_text == before
        assert hermes.chats == [('call-caller', 'Research this')]
        assert not hermes.stream_closed
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        hermes.release.set()
        await asyncio.wait_for(task, 1)


@pytest.mark.parametrize('control', [
    'take me off hold', 'Hey Atlas, take me off hold', 'Atlas', "I'm back",
])
async def test_caller_can_barge_in_and_leave_hold_without_restarting_work(control):
    ari, hermes, audio = MusicAri(), StalledHermes(), FakeAudio()
    stt = FakeStt(audio)
    task = asyncio.create_task(_handler(ari=ari, hermes=hermes, stt=stt,
        tts=FakeTts(frames_per_sentence=1), server=FakeServer(audio)).handle(
        'caller', '7003', _ext(hold_enabled=True, hold_offer_seconds=0.01,
                              hold_feedback_seconds=1)))
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'put me on hold')
        await until(lambda: ari.music)
        before = len(audio.spoken_text)
        say(stt, control)
        await until(lambda: not ari.music and len(audio.spoken_text) > before)
        assert len(hermes.chats) == 1
        assert not hermes.stream_closed
        if control in ('Atlas', "I'm back"):
            assert any("I'm here" in text for text in audio.spoken_text[before:])
        else:
            assert any('stopped the hold music' in text for text in audio.spoken_text[before:])
        await asyncio.sleep(0.02)
        assert not ari.music
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        hermes.release.set()
        await asyncio.wait_for(task, 1)


@pytest.mark.parametrize('failure', ['empty', 'error', 'timeout'])
async def test_pending_failure_speaks_feedback(failure):
    class Failed(FakeHermes):
        async def chat_stream(self, session_id, message):
            if failure == 'error':
                raise HermesError('private error details')
            if failure == 'timeout':
                await asyncio.Event().wait()
            if False:
                yield ''
    audio = FakeAudio()
    await reply_task(_handler(), Failed(), audio,
                     ext=_ext(hold_enabled=True, hold_offer_seconds=1,
                              hermes_reply_timeout_seconds=0.02))
    assert any('Sorry' in t for t in audio.spoken_text)
    assert not any('private' in t for t in audio.spoken_text)


async def test_moh_start_error_degrades_without_losing_work():
    class BrokenMusic(MusicAri):
        async def start_moh(self, channel, moh_class):
            raise RuntimeError('unavailable')
    ari, hermes, audio = BrokenMusic(), StalledHermes(), FakeAudio()
    pending = ['put me on hold']
    task = asyncio.create_task(_handler(ari=ari)._reply(
        audio, _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+2, channel_id='caller',
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await until(lambda: any('music' in t and 'unavailable' in t for t in audio.spoken_text))
        assert not hermes.stream_closed
        hermes.release.set()
        await task
        assert 'Late answer.' in audio.spoken_text
    finally:
        hermes.release.set()
        with contextlib.suppress(Exception):
            await task


@pytest.mark.parametrize('exit_kind', ['hangup', 'deadline', 'cancel'])
async def test_hold_cleanup_during_slow_speech(exit_kind):
    hermes, audio, ended = StalledHermes(), FakeAudio(send_delay=0.01), asyncio.Event()
    tts = FakeTts(frames_per_sentence=100)
    task = reply_task(_handler(tts=tts), hermes, audio, ended=ended,
                      deadline=asyncio.get_running_loop().time() + (0.07 if exit_kind == 'deadline' else 5),
                      ext=_ext(hold_enabled=True, hold_offer_seconds=0.01))
    await until(lambda: audio.is_sending)
    if exit_kind == 'hangup':
        ended.set()
    elif exit_kind == 'cancel':
        task.cancel()
    done, _ = await asyncio.wait({task}, timeout=0.2)
    try:
        assert task in done, 'slow feedback ignored call termination'
        if exit_kind == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert hermes.stream_closed
        assert not audio.is_sending
        assert not audio.cancelled, 'cancelled a frame mid-write'
    finally:
        hermes.release.set()
        with contextlib.suppress(BaseException):
            await task


async def test_new_question_during_offer_is_not_lost():
    class TwoRuns(StalledHermes):
        async def chat_stream(self, session_id, message):
            if message == 'A different question':
                self.chats.append((session_id, message))
                yield 'New answer.'
            else:
                async for text in super().chat_stream(session_id, message):
                    yield text
    hermes, audio, ari = TwoRuns(), FakeAudio(), MusicAri()
    stt = FakeStt(audio)
    task = asyncio.create_task(_handler(ari=ari, hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=3)).handle(
        'caller', '7003', _ext(hold_enabled=True, hold_offer_seconds=0.01)))
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await until(lambda: any('hold music' in s for s in audio.spoken_text))
        say(stt, 'A different question')
        await until(lambda: 'New answer.' in audio.spoken_text)
        assert [m for _,m in hermes.chats] == ['Research this', 'A different question']
    finally:
        audio.hangup.set()
        await task


async def test_configured_long_work_survives_31_seconds_of_logical_time(monkeypatch):
    import phonesys.hold as hold
    assert hasattr(hold, 'now'), 'controller needs an injectable monotonic clock'
    clock = [0.0]
    monkeypatch.setattr(hold, 'now', lambda: clock[0])
    hermes, audio = StalledHermes(), FakeAudio()
    task = reply_task(_handler(), hermes, audio,
        ext=_ext(hold_enabled=True, hermes_reply_timeout_seconds=300), deadline=1000)
    try:
        await hermes.started.wait()
        clock[0] = 31
        await until(lambda: any('hold music' in t for t in audio.spoken_text))
        assert not task.done() and not hermes.stream_closed
        await until(lambda: audio.finished)
        await asyncio.sleep(0.01)  # let the offer's completion arm its next deadline
        clock[0] = 46
        await until(lambda: any('still working' in t for t in audio.spoken_text))
        assert not task.done()
        hermes.release.set()
        await task
        assert 'Late answer.' in audio.spoken_text
    finally:
        hermes.release.set()
        await task


async def test_typed_eof_without_done_reports_incomplete_partial_answer():
    class Partial(FakeHermes):
        async def chat_events(self, session_id, message):
            yield HermesEvent('assistant.delta', 'A partial finding. ')
    audio = FakeAudio()
    await reply_task(_handler(), Partial(), audio, ext=_ext(hold_enabled=True))
    assert 'A partial finding.' in audio.spoken_text
    assert any('Sorry' in t for t in audio.spoken_text)


async def test_transient_stop_moh_error_retries_before_speaking():
    class RetryMusic(MusicAri):
        failures = 1
        async def stop_moh(self, channel):
            if self.failures:
                self.failures -= 1
                raise RuntimeError('temporary')
            await super().stop_moh(channel)
    ari, hermes, audio = RetryMusic(), StalledHermes(), FakeAudio()
    pending = ['put me on hold']
    task = asyncio.create_task(_handler(ari=ari)._reply(
        audio, _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+2, channel_id='caller',
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await until(lambda: ari.music)
        hermes.release.set()
        await task
        assert not ari.music
        assert 'Late answer.' in audio.spoken_text
    finally:
        hermes.release.set()
        with contextlib.suppress(Exception):
            await task


@pytest.mark.parametrize('control', ['Yes, please.', 'no, thank you', 'what is the status?',
                                             'stop the music please', 'please put me on hold',
                                             'put me back on hold', 'maybe', 'Right.'])
async def test_natural_voice_controls_do_not_restart_research(control):
    from phonesys import hold
    assert hasattr(hold, 'control_intent')
    expected = {'Yes, please.': 'hold', 'no, thank you': 'quiet',
                'what is the status?': 'status', 'stop the music please': 'quiet',
                'please put me on hold': 'hold', 'put me back on hold': 'hold',
                'maybe': 'clarify', 'Right.': 'backchannel'}
    assert hold.control_intent(control, offered=True) == expected[control]
    assert hold.control_intent('yes', offered=False) == 'clarify'
    assert hold.control_intent('Hey Atlas, take me off hold', offered=True) == 'quiet'
    assert hold.control_intent('Atlas', offered=True) == 'attention'
    assert hold.control_intent('what is the weather tomorrow', offered=True) is None


async def test_tts_failure_gets_spoken_apology_and_closes_stream():
    from test_stream_call import FailingTts
    audio, hermes = FakeAudio(), FakeHermes(['Answer.'])
    await reply_task(_handler(tts=FailingTts(fail_on_call=1)), hermes, audio,
                     ext=_ext(hold_enabled=True))
    assert any('Sorry' in t for t in audio.spoken_text)
    assert hermes.stream_closed


async def test_hold_cleanup_stops_music_even_when_stream_close_fails():
    class BadClose(StalledHermes):
        async def chat_stream(self, session_id, message):
            try:
                yield 'Partial. '
                await self.release.wait()
            finally:
                raise RuntimeError('cleanup failed')
    ari, audio, hermes = MusicAri(), FakeAudio(), BadClose()
    pending = ['put me on hold']
    ended = asyncio.Event()
    task = asyncio.create_task(_handler(ari=ari)._reply(
        audio, _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=ended,
        deadline=asyncio.get_running_loop().time()+2, channel_id='caller',
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await until(lambda: ari.music)
        ended.set()
        with contextlib.suppress(RuntimeError):
            await task
        assert not ari.music
    finally:
        hermes.release.set()
        with contextlib.suppress(Exception):
            await task


async def test_cancel_closes_pending_work_before_acknowledging():
    hermes, audio = StalledHermes(), FakeAudio()
    pending = []
    task = asyncio.create_task(_handler()._reply(
        audio, _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+2,
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    def on_frame(index, count):
        if 'requested cancellation' in audio.spoken_text[-1]:
            assert hermes.stream_closed
    audio.on_sent_hook = on_frame
    try:
        await hermes.started.wait()
        pending.append('cancel')
        await task
        assert any('requested cancellation' in text for text in audio.spoken_text)
        assert not any("I've cancelled" in text for text in audio.spoken_text)
    finally:
        hermes.release.set()
        with contextlib.suppress(Exception):
            await task


async def test_done_finishes_without_waiting_for_transport_eof():
    class Done(FakeHermes):
        async def chat_events(self, session_id, message):
            try:
                yield HermesEvent('assistant.delta', 'Complete answer.')
                yield HermesEvent('done')
                await asyncio.Event().wait()
            finally:
                self.stream_closed = True
    hermes, audio = Done(), FakeAudio()
    task = reply_task(_handler(), hermes, audio, ext=_ext(hold_enabled=True))
    try:
        done, _ = await asyncio.wait({task}, timeout=0.15)
        assert task in done
        await task
        assert hermes.stream_closed
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


@pytest.mark.parametrize('exit_kind', ['hangup', 'cancel', 'deadline'])
async def test_real_handler_releases_active_moh_on_every_call_exit(exit_kind):
    ari, hermes, audio = MusicAri(), StalledHermes(), FakeAudio()
    stt = FakeStt(audio)
    ext = _ext(hold_enabled=True, hold_offer_seconds=0.01,
               max_call_seconds=0.18 if exit_kind == 'deadline' else 600)
    task = asyncio.create_task(_handler(ari=ari, hermes=hermes, stt=stt,
        tts=FakeTts(frames_per_sentence=1), server=FakeServer(audio)).handle('caller', '7003', ext))
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'put me on hold')
        await until(lambda: ari.music)
        if exit_kind == 'hangup':
            audio.hangup.set()
        elif exit_kind == 'cancel':
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert not ari.music and ari.stops
        assert hermes.stream_closed and stt.closed
        assert set(ari.hungup) == {'caller', 'as-chan'}
    finally:
        audio.hangup.set()
        hermes.release.set()
        with contextlib.suppress(BaseException):
            await task


async def test_status_during_moh_pauses_music_and_resumes_same_run():
    ari, hermes, audio = MusicAri(), StalledHermes(), FakeAudio()
    stt = FakeStt(audio)
    task = asyncio.create_task(_handler(ari=ari, hermes=hermes, stt=stt,
        tts=FakeTts(frames_per_sentence=2), server=FakeServer(audio)).handle(
        'caller', '7003', _ext(hold_enabled=True, hold_offer_seconds=0.01)))
    def check_audio(index, count):
        assert not ari.music
    audio.on_sent_hook = check_audio
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'put me on hold')
        await until(lambda: ari.music)
        say(stt, 'status')
        await until(lambda: len(ari.starts) == 2)
        assert ari.music and ari.stops
        assert any('still working' in t for t in audio.spoken_text)
        assert len(hermes.chats) == 1 and not hermes.stream_closed
    finally:
        audio.hangup.set()
        await task


async def test_hold_repeated_cancellation_drains_cooperative_generator():
    baseline = asyncio.all_tasks()
    cleaning, finish = asyncio.Event(), asyncio.Event()
    class Cleanup(StalledHermes):
        async def chat_stream(self, session_id, message):
            self.started.set()
            try:
                await self.release.wait()
                yield 'answer'
            finally:
                cleaning.set()
                await finish.wait()
                self.stream_closed = True
    hermes = Cleanup()
    task = reply_task(_handler(), hermes, FakeAudio(), ext=_ext(hold_enabled=True))
    await hermes.started.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hermes.stream_closed
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)


async def test_status_during_partial_answer_does_not_drop_remaining_sentences():
    class Chunks(StalledHermes):
        async def chat_stream(self, session_id, message):
            self.chats.append((session_id, message))
            try:
                yield 'First finding. Second finding. '
                await self.release.wait()
                yield 'Final finding.'
            finally:
                self.stream_closed = True
    hermes, audio, stt = Chunks(), FakeAudio(send_delay=0.005), FakeStt()
    fired = False
    def on_frame(index, count):
        nonlocal fired
        if audio.spoken_text[-1] == 'First finding.' and not fired:
            fired = True
            say(stt, 'status')
    audio.on_sent_hook = on_frame
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=3)).handle(
        'caller', '7003', _ext(hold_enabled=True)))
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, 'Research this')
        await until(lambda: 'Second finding.' in audio.spoken_text)
        assert any('still working' in s for s in audio.spoken_text)
        assert len(hermes.chats) == 1
        hermes.release.set()
        await until(lambda: 'Final finding.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        await task


async def test_hangup_during_pending_moh_start_does_not_stall_teardown():
    class SlowStart(MusicAri):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
        async def start_moh(self, channel, moh_class):
            self.music = True  # PBX acted, but its HTTP reply has not arrived
            self.started.set()
            await asyncio.Event().wait()
    ari, hermes, audio, ended = SlowStart(), StalledHermes(), FakeAudio(), asyncio.Event()
    pending = ['put me on hold']
    task = asyncio.create_task(_handler(ari=ari)._reply(
        audio, _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=ended,
        deadline=asyncio.get_running_loop().time()+2, channel_id='caller',
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await ari.started.wait()
        ended.set()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done, 'MOH HTTP call blocked hangup'
        await task
        assert not ari.music and hermes.stream_closed
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


@pytest.mark.parametrize('chunks', [
    ['First finding. Second finding. '],
    ['Complete answer. '],
    ['First finding. ', 'Second finding. '],
])
async def test_completed_answer_drains_after_generation_cutoff(monkeypatch, chunks):
    from phonesys import hold
    clock = [0.0]
    monkeypatch.setattr(hold, 'now', lambda: clock[0])

    class Complete(FakeHermes):
        async def chat_events(self, session_id, message):
            try:
                for chunk in chunks:
                    yield HermesEvent('assistant.delta', chunk)
                yield HermesEvent('done')
            finally:
                self.stream_closed = True

    hermes, audio = Complete(), FakeAudio()
    def cross_cutoff(index, count):
        clock[0] = 301
    audio.on_sent_hook = cross_cutoff
    await reply_task(_handler(tts=FakeTts(frames_per_sentence=1)), hermes, audio,
                     ext=_ext(hold_enabled=True, hermes_reply_timeout_seconds=300),
                     deadline=1000)
    assert audio.spoken_text == [s.strip() + '.' for s in ''.join(chunks).split('.') if s.strip()]
    assert hermes.stream_closed


async def test_complete_long_sentence_has_no_false_timeout_with_real_scheduler():
    class Complete(FakeHermes):
        async def chat_events(self, session_id, message):
            try:
                yield HermesEvent('assistant.delta', 'Complete answer. ')
                yield HermesEvent('done')
            finally:
                self.stream_closed = True
    hermes, audio = Complete(), FakeAudio(send_delay=0.01)
    await reply_task(_handler(tts=FakeTts(frames_per_sentence=12)), hermes, audio,
                     ext=_ext(hold_enabled=True, hermes_reply_timeout_seconds=0.06))
    assert audio.spoken_text == ['Complete answer.'] * 12
    assert hermes.stream_closed


@pytest.mark.parametrize('hot', [False, True])
async def test_unfinished_stream_cuts_off_even_with_buffered_events(monkeypatch, hot):
    from phonesys import hold
    clock = [0.0]
    monkeypatch.setattr(hold, 'now', lambda: clock[0])
    class Unfinished(FakeHermes):
        async def chat_events(self, session_id, message):
            try:
                yield HermesEvent('assistant.delta', 'Partial finding. ')
                while hot:
                    yield HermesEvent('tool.started', 'Searching.')
                await asyncio.Event().wait()
            finally:
                self.stream_closed = True
    hermes, audio = Unfinished(), FakeAudio()
    def cross_cutoff(index, count):
        clock[0] = 301
    audio.on_sent_hook = cross_cutoff
    await asyncio.wait_for(reply_task(
        _handler(tts=FakeTts(frames_per_sentence=1)), hermes, audio,
        ext=_ext(hold_enabled=True, hermes_reply_timeout_seconds=300),
        deadline=1000), 0.5)
    assert audio.spoken_text[0] == 'Partial finding.'
    assert len(audio.spoken_text) == 2
    assert 'work time limit' in audio.spoken_text[-1]
    assert hermes.stream_closed


async def test_completed_playback_still_obeys_call_deadline(monkeypatch):
    from phonesys import hold
    clock = [0.0]
    monkeypatch.setattr(hold, 'now', lambda: clock[0])
    class Complete(FakeHermes):
        async def chat_events(self, session_id, message):
            yield HermesEvent('assistant.delta', 'First finding. Second finding. ')
            yield HermesEvent('done')
    audio = FakeAudio(send_delay=0.005)
    def end_call(index, count):
        clock[0] = 1001
    audio.on_sent_hook = end_call
    await asyncio.wait_for(reply_task(
        _handler(tts=FakeTts(frames_per_sentence=100)), Complete(), audio,
        ext=_ext(hold_enabled=True, hermes_reply_timeout_seconds=300),
        deadline=1000), 0.5)
    assert 0 < len(audio.spoken_text) < 100
    assert set(audio.spoken_text) == {'First finding.'}
    assert not audio.is_sending and not audio.cancelled


async def test_completed_playback_remains_controllable_after_generation_cutoff(monkeypatch):
    from phonesys import hold
    clock, pending = [0.0], []
    monkeypatch.setattr(hold, 'now', lambda: clock[0])
    class Complete(FakeHermes):
        async def chat_events(self, session_id, message):
            try:
                yield HermesEvent('assistant.delta', 'First finding. Second finding. ')
                yield HermesEvent('done')
            finally:
                self.stream_closed = True
    hermes, audio = Complete(), FakeAudio()
    def cancel_after_first(index, count):
        if audio.spoken_text[-1] == 'First finding.':
            clock[0] = 301
            pending.append('cancel')
    audio.on_sent_hook = cancel_after_first
    await asyncio.wait_for(_handler(tts=FakeTts(frames_per_sentence=1))._reply(
        audio, _ext(hold_enabled=True, hermes_reply_timeout_seconds=300), hermes,
        's', 'research', interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(), deadline=1000,
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear), 0.5)
    assert audio.spoken_text == [
        'First finding.', "Okay, I've requested cancellation of this request."]
    assert hermes.stream_closed


async def test_generation_lookahead_is_bounded_during_slow_playback():
    baseline = asyncio.all_tasks()
    produced = []
    blocked = asyncio.Event()
    class Hot(FakeHermes):
        async def chat_events(self, session_id, message):
            try:
                yield HermesEvent('assistant.delta', 'First finding. ')
                while True:
                    produced.append(None)
                    yield HermesEvent('tool.started', 'Searching.')
            finally:
                self.stream_closed = True
    class SlowTts(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            blocked.set()
            await asyncio.Event().wait()
    hermes = Hot()
    task = reply_task(_handler(tts=SlowTts()), hermes, FakeAudio(),
                      ext=_ext(hold_enabled=True))
    try:
        await asyncio.wait_for(blocked.wait(), 0.5)
        await asyncio.sleep(0.02)
        before = len(produced)
        await asyncio.sleep(0.02)
        assert len(produced) == before, 'generation ignored playback backpressure'
        assert 0 < before < 100
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert hermes.stream_closed
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)


def test_hold_config_is_opt_in_and_validated():
    ext = _ext()
    assert getattr(ext, 'hold_enabled', None) is False
    enabled = replace(ext, hold_enabled=True, hold_offer_seconds=15,
                      hold_feedback_seconds=15, hold_moh_class='phonesys_hold',
                      hermes_reply_timeout_seconds=300)
    assert enabled.hermes_reply_timeout_seconds == 300
    for key in ('hold_offer_seconds', 'hold_feedback_seconds'):
        for bad in (0, -1, float('inf'), float('nan')):
            with pytest.raises(ValueError):
                replace(enabled, **{key: bad})
