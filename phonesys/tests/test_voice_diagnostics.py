"""Lifecycle diagnostics carry identifiers and enums, never user/tool payloads."""
import asyncio
import logging

import pytest

from test_stream_call import FakeAudio, FakeTts, FakeHermes, _handler, _ext
from test_stream_stalls import StalledHermes, reply_task
from test_hold import MusicAri, until


async def test_hold_lifecycle_logs_are_structured_and_do_not_expose_payloads(caplog):
    from phonesys.hold import OFFER
    caplog.set_level(logging.INFO)
    audio, ari, hermes = FakeAudio(), MusicAri(), StalledHermes()
    pending = []
    task = asyncio.create_task(_handler(ari=ari, tts=FakeTts(frames_per_sentence=1))._reply(
        audio, _ext(hold_enabled=True, hold_offer_seconds=0.01, access_pin='PIN-SECRET'),
        hermes, 'call-caller-7003', 'Research PASSWORD-SECRET',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(), final_seen=asyncio.Event(),
        ended=asyncio.Event(), deadline=asyncio.get_running_loop().time()+2,
        channel_id='caller', read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await until(lambda: OFFER in audio.spoken_text)
        pending.append('yes')
        await until(lambda: ari.music)
        hermes.release.set()
        await asyncio.wait_for(task, 1)
        records = [r for r in caplog.records if getattr(r, 'event', '').startswith('hold.')]
        events = [r.event for r in records]
        assert 'hold.offer' in events, 'no consent-offer diagnostic'
        assert 'hold.control' in events
        assert 'hold.transition' in events
        assert 'hold.result' in events
        assert all(r.call_id == 'caller' and r.session_id == 'call-caller-7003' for r in records)
        assert {'music_started', 'music_stopped'} <= {r.outcome for r in records}
        assert 'PIN-SECRET' not in str([r.__dict__ for r in records])
        assert 'PASSWORD-SECRET' not in str([r.__dict__ for r in records])
        assert all(term in OFFER.lower() for term in ('status', 'stop music', 'cancel'))
    finally:
        hermes.release.set()
        await task


@pytest.mark.parametrize('failure, code, reason, expected_reason', [
    (False, 1000, '', 'empty'),
    (True, 1011, 'NET-0001: Token API-SECRET PIN-SECRET\nforged log', 'NET-0001'),
    (True, 1008, 'Token API-SECRET PIN-SECRET', 'redacted'),
])
async def test_stt_close_logs_code_and_safe_reason_without_swallowing_failure(
        caplog, failure, code, reason, expected_reason):
    from phonesys.stream_stt import LiveStt
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    caplog.set_level(logging.INFO)

    class Socket:
        close_code = code
        close_reason = reason
        def __aiter__(self):
            return self
        async def __anext__(self):
            if failure:
                raise ConnectionClosedError(Close(code, reason), None)
            raise StopAsyncIteration

    stt = LiveStt('API-SECRET')
    stt.call_id = 'caller'
    stt._ws = Socket()
    if failure:
        with pytest.raises(ConnectionClosedError):
            _ = [event async for event in stt.events()]
    else:
        assert [event async for event in stt.events()] == []
    records = [r for r in caplog.records if getattr(r, 'event', '') == 'stt.closed']
    assert len(records) == 1, 'STT EOF/error has no correlated close diagnostic'
    record = records[0]
    assert record.call_id == 'caller'
    assert record.close_code == code
    assert record.close_reason == expected_reason
    assert record.outcome == ('error' if failure else 'eof')
    assert all(secret not in str(record.__dict__) for secret in ('API-SECRET', 'PIN-SECRET', 'forged log'))


@pytest.mark.parametrize('ending', ['audio_eof', 'stt_eof', 'stt_send_error', 'stt_send_timeout'])
async def test_handler_distinguishes_audio_end_from_transcription_failure(caplog, ending):
    from test_stream_call import FakeStt, FakeServer
    from phonesys.stream_call import STT_FAILURE_MESSAGE
    caplog.set_level(logging.INFO)
    audio = FakeAudio()

    class Stt(FakeStt):
        async def send(self, pcm):
            if ending == 'stt_send_error':
                raise RuntimeError('API-SECRET PIN-SECRET')
            if ending == 'stt_send_timeout':
                raise TimeoutError('API-SECRET PIN-SECRET')
            await super().send(pcm)

    stt = Stt(audio)
    task = asyncio.create_task(_handler(stt=stt, hermes=FakeHermes(),
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle('diag-caller', '7003', _ext()))
    try:
        await until(lambda: bool(audio.spoken_text))
        if ending == 'audio_eof':
            audio.hangup.set()
        elif ending == 'stt_eof':
            stt._queue.put_nowait(None)
        await asyncio.wait_for(task, 1)
        records = [r for r in caplog.records if getattr(r, 'event', '') == 'call.stream_ended']
        assert records, 'no source-specific termination diagnostic'
        assert any(r.outcome == ending and r.call_id == 'diag-caller' for r in records)
        assert stt.call_id == 'diag-caller'
        assert (STT_FAILURE_MESSAGE in audio.spoken_text) == (ending != 'audio_eof')
        assert 'API-SECRET' not in caplog.text and 'PIN-SECRET' not in caplog.text
    finally:
        audio.hangup.set()
        await task


@pytest.mark.parametrize('failure', ['hermes', 'tts', 'moh', 'call_deadline'])
async def test_hold_failure_diagnostics_never_log_exceptions_or_tool_payloads(caplog, failure):
    from phonesys.hermes import HermesError, HermesEvent
    from phonesys.stream_tts import TtsStreamError
    caplog.set_level(logging.INFO)
    secret = 'PIN-SECRET raw_args=PASSWORD-SECRET'

    class Hermes(StalledHermes):
        async def chat_events(self, session, message):
            if failure == 'hermes':
                raise HermesError(secret)
            yield HermesEvent('tool.started', secret)
            if failure == 'tts':
                yield HermesEvent('assistant.delta', 'Answer.')
            await self.release.wait()
            yield HermesEvent('done')

    class Tts(FakeTts):
        async def synthesize_frames(self, text, *args):
            if text == 'Answer.':
                raise TtsStreamError(secret)
            return await super().synthesize_frames(text, *args)

    class Ari(MusicAri):
        async def start_moh(self, *args):
            raise RuntimeError(secret)

    hermes, audio = Hermes(), FakeAudio()
    pending = ['put me on hold'] if failure == 'moh' else []
    task = asyncio.create_task(_handler(ari=Ari(), tts=Tts())._reply(
        audio, _ext(hold_enabled=True, hold_offer_seconds=10), hermes,
        'call-diag', secret, interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time() + (0.02 if failure == 'call_deadline' else 1),
        channel_id='diag', read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        if failure == 'moh':
            await until(lambda: any('unavailable' in s for s in audio.spoken_text))
            hermes.release.set()
        await asyncio.wait_for(task, 1)
        expected = {'hermes': ('hold.result', 'hermes_error'), 'tts': ('hold.result', 'speech_error'),
                    'moh': ('hold.transition', 'music_start_failed'),
                    'call_deadline': ('hold.timeout', 'call_deadline')}[failure]
        records = [r for r in caplog.records if getattr(r, 'event', '').startswith('hold.')]
        assert expected in [(r.event, r.outcome) for r in records]
        assert secret not in str([r.__dict__ for r in records])
        assert 'PASSWORD-SECRET' not in caplog.text and 'PIN-SECRET' not in caplog.text
    finally:
        hermes.release.set()
        await task


@pytest.mark.parametrize('permanent', [False, True])
async def test_stop_music_diagnostics_are_correlated_without_raw_errors(caplog, permanent):
    caplog.set_level(logging.INFO)

    class StopError(MusicAri):
        failures = 0
        async def stop_moh(self, channel):
            self.failures += 1
            if permanent or self.failures == 1:
                raise RuntimeError('STOP-SECRET')
            await super().stop_moh(channel)

    ari, hermes, audio, pending = StopError(), StalledHermes(), FakeAudio(), ['put me on hold']
    task = asyncio.create_task(_handler(ari=ari)._reply(
        audio, _ext(hold_enabled=True), hermes, 'call-stop', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(), final_seen=asyncio.Event(),
        ended=asyncio.Event(), deadline=asyncio.get_running_loop().time()+1,
        channel_id='stop', read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await until(lambda: ari.music)
        hermes.release.set()
        if permanent:
            with pytest.raises(RuntimeError):
                await task
        else:
            await task
        records = [r for r in caplog.records if getattr(r, 'event', '') == 'hold.transition']
        assert any(r.outcome == 'music_stop_retry' for r in records)
        assert any(r.outcome == ('music_stop_failed' if permanent else 'music_stopped') for r in records)
        assert all(r.call_id == 'stop' and r.session_id == 'call-stop' for r in records)
        assert 'STOP-SECRET' not in caplog.text
    finally:
        hermes.release.set()
        if not task.done():
            await task


async def test_hold_timeout_logs_budget_without_error_details(caplog):
    caplog.set_level(logging.INFO)
    audio = FakeAudio()
    await reply_task(_handler(), StalledHermes(), audio,
                     ext=_ext(hold_enabled=True, hermes_reply_timeout_seconds=0.02))
    assert any(getattr(r, 'event', '') == 'hold.timeout' and r.outcome == 'reply_deadline'
               for r in caplog.records)
