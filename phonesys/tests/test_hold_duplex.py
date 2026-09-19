"""Offline hold-path duplex regressions; no network or live calls."""
import asyncio
import contextlib

import pytest

from test_stream_call import FakeAudio, FakeStt, FakeTts, FakeServer, _ext, _handler
from test_stream_stalls import StalledHermes
from test_hold import until, say
from phonesys.stream_stt import SttEvent


async def test_early_truthful_ack_uses_configured_delay_and_sent_telemetry(caplog):
    import logging
    caplog.set_level(logging.INFO, logger='phonesys.hold')
    hermes, audio, ended = StalledHermes(), FakeAudio(), asyncio.Event()
    start = asyncio.get_running_loop().time()
    task = asyncio.create_task(_handler(tts=FakeTts(frames_per_sentence=1))._reply(
        audio, _ext(hold_enabled=True, filler_delay_ms=30, hold_offer_seconds=15),
        hermes, 's', 'research', interrupt=asyncio.Event(),
        utterance_ready=asyncio.Event(), final_seen=asyncio.Event(), ended=ended,
        deadline=start+3, t0=start))
    try:
        await asyncio.sleep(0.015)
        assert not audio.sent
        await asyncio.sleep(0.12)
        assert audio.sent, 'hold path bypassed early acknowledgment'
        assert [payload.decode() for payload, _ in audio.sent] == ["I'm working on your request."]
        hermes.release.set()
        await asyncio.wait_for(task, 0.3)
        latency = next(r for r in caplog.records if getattr(r, 'event', '') == 'hold.latency')
        assert start <= latency.feedback_first_frame_at < latency.reply_first_frame_at
        assert 30 <= latency.feedback_first_audio_ms < 150
        assert latency.reply_first_audio_ms >= latency.feedback_first_audio_ms
    finally:
        ended.set()
        await task


async def test_discarded_hold_narration_is_not_assumed_heard_after_cancel():
    from test_hold import MusicAri
    from phonesys.hermes import HermesEvent
    class Narration(StalledHermes):
        async def chat_events(self, session_id, message):
            yield HermesEvent('assistant.delta', 'Unspoken narration. ')
            yield HermesEvent('tool.started', "I'm searching for information.")
            self.started.set()
            await self.release.wait()
    hermes, pending, notes = Narration(), ['put me on hold'], []
    task = asyncio.create_task(_handler(ari=MusicAri(), tts=FakeTts(frames_per_sentence=1))._reply(
        FakeAudio(), _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+1, delivery_notes=notes,
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear))
    try:
        await hermes.started.wait()
        await asyncio.sleep(0.03)
        pending.append('cancel')
        await asyncio.wait_for(task, 0.3)
        assert notes and 'Only these complete sentences were sent: (none)' in notes[0]
        assert 'Unspoken narration.' not in notes[0]
    finally:
        hermes.release.set()
        await task


async def test_generated_but_unread_events_are_not_assumed_heard():
    from test_stream_call import FakeHermes
    generated = asyncio.Event()
    class Ready(FakeHermes):
        async def chat_stream(self, session_id, message):
            yield 'Generated during caller speech.'
            generated.set()
    interrupt, pending, notes = asyncio.Event(), [], []
    interrupt.set()  # confirmed speech, but the semantic utterance is not ready
    task = asyncio.create_task(_handler(tts=FakeTts(frames_per_sentence=1))._reply(
        FakeAudio(), _ext(hold_enabled=True), Ready(), 's', 'research',
        interrupt=interrupt, utterance_ready=asyncio.Event(), final_seen=asyncio.Event(),
        ended=asyncio.Event(), deadline=asyncio.get_running_loop().time()+1,
        delivery_notes=notes, read_utterance=lambda: pending[0] if pending else '',
        consume=pending.clear))
    await generated.wait()
    pending.append('cancel')
    await asyncio.wait_for(task, 0.3)
    assert notes and 'Only these complete sentences were sent: (none)' in notes[0]


async def test_pause_keeps_work_deadline_without_speaking_timeout_over_caller():
    hermes, audio, pending = StalledHermes(), FakeAudio(), ['wait']
    await asyncio.wait_for(_handler(tts=FakeTts(frames_per_sentence=1))._reply(
        audio, _ext(hold_enabled=True, hermes_reply_timeout_seconds=0.04),
        hermes, 's', 'research', interrupt=asyncio.Event(),
        utterance_ready=asyncio.Event(), final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+1,
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear), 0.2)
    assert audio.sent == [], 'work cutoff ignored the caller\'s pause'
    assert hermes.stream_closed


async def test_zero_audio_is_not_reported_as_delivered():
    from test_stream_call import FakeHermes
    notes = []
    await _handler(tts=FakeTts(frames_per_sentence=0))._reply(
        FakeAudio(), _ext(hold_enabled=True), FakeHermes(['Never heard.']), 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+1, delivery_notes=notes)
    assert notes and 'Only these complete sentences were sent: (none)' in notes[0]


@pytest.mark.parametrize('exit_kind', ['hangup', 'cancel'])
async def test_waiting_for_interrupted_utterance_has_bounded_cleanup(exit_kind):
    baseline = asyncio.all_tasks()
    audio, hermes, interrupted, ended = FakeAudio(), StalledHermes(), asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_handler()._reply(
        audio, _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=interrupted, utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=ended,
        deadline=asyncio.get_running_loop().time()+10))
    await hermes.started.wait()
    interrupted.set()
    await asyncio.sleep(0.03)
    if exit_kind == 'hangup':
        ended.set()
        await asyncio.wait_for(task, 0.2)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)
    assert hermes.stream_closed and not audio.is_sending
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)


async def test_status_resumes_unsent_frames_not_sentence_replay():
    from test_stream_call import FakeHermes
    hermes, audio = FakeHermes(['One finding. Next finding.']), FakeAudio(send_delay=0.005,
        track_concurrency=True)
    pending, fired = [], False
    tts = FakeTts(frames_per_sentence=30)
    def frame(index, count):
        nonlocal fired
        if not fired and audio.spoken_text[-1] == 'One finding.' and count == 2:
            fired = True
            pending.append('status')
    audio.on_sent_hook = frame
    await asyncio.wait_for(_handler(tts=tts)._reply(
        audio, _ext(hold_enabled=True), hermes, 's', 'research',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+3,
        read_utterance=lambda: pending[0] if pending else '', consume=pending.clear), 2)
    assert audio.spoken_text.count('One finding.') == 30, 'already-sent frames replayed'
    assert audio.spoken_text.count('Next finding.') == 30
    assert tts.spoken.count('One finding.') == 1
    assert audio.max_concurrent_senders == 1 and not audio.cancelled


@pytest.mark.parametrize('control', ['hang on', 'hold on', 'wait', 'stop'])
async def test_pause_controls_are_silent_and_preserve_work(control):
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(hold_enabled=True, hold_offer_seconds=0.06)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        before = list(audio.spoken_text)
        say(stt, control)
        await asyncio.sleep(0.12)
        assert audio.spoken_text == before, 'pause was treated as hold consent/clarification'
        assert len(hermes.chats) == 1 and not hermes.stream_closed
        hermes.release.set()
        await asyncio.sleep(0.03)
        assert 'Late answer.' not in audio.spoken_text
        say(stt, 'status')
        await until(lambda: 'Late answer.' in audio.spoken_text)
        assert len(hermes.chats) == 1
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_delivery_note_only_credits_complete_sentences_on_next_turn():
    from test_stream_call import FakeHermes
    class LongReply(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            self._n = 50 if text == 'Unheard remainder.' else 1
            return await super().synthesize_frames(text, voice_id, sentence_index)
    hermes = FakeHermes(['Heard sentence. Unheard remainder.'])
    audio, stt = FakeAudio(send_delay=0.005), FakeStt()
    fired = False
    def frame(index, count):
        nonlocal fired
        if not fired and audio.spoken_text[-1] == 'Unheard remainder.':
            fired = True
            say(stt, 'Compare the other option')
    audio.on_sent_hook = frame
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=LongReply()).handle(
        'caller', '7003', _ext(hold_enabled=True)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await until(lambda: len(hermes.chats) == 2)
        message = hermes.chats[1][1]
        assert 'Only these complete sentences were sent: Heard sentence.' in message
        assert 'Unheard remainder.' not in message
        assert message.endswith('Compare the other option')
        assert hermes.appended == [], 'delivery correction started a hidden agent turn'
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_confirmed_interrupt_stops_before_final_and_banks_fragments(caplog):
    import logging
    caplog.set_level(logging.INFO, logger='phonesys.hold')
    hermes, audio, stt = StalledHermes(), FakeAudio(send_delay=0.01), FakeStt()
    class LongReply(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            self._n = 100 if text == 'Late answer.' else 1
            return await super().synthesize_frames(text, voice_id, sentence_index)
    tts = LongReply()
    # Short greeting; long reply lets us distinguish stop from natural EOF.
    handler = _handler(hermes=hermes, stt=stt, server=FakeServer(audio), tts=tts)
    task = asyncio.create_task(handler.handle('caller', '7003', _ext(
        greeting='', hold_enabled=True, barge_in_confirm_ms=0)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
        stt.emit(SttEvent(kind='transcript', text='Actually', is_final=False))
        await asyncio.sleep(0.08)
        assert not audio.is_sending, 'confirmed interim did not stop hold speech'
        timing = next((r for r in caplog.records if getattr(r, 'event', '') == 'hold.interrupt'), None)
        assert timing is not None, 'missing confirmed-interrupt-to-drained telemetry'
        assert timing.confirmed_at <= timing.stop_requested_at <= timing.sender_drained_at
        assert 0 <= timing.interrupt_to_stop_ms < 50
        assert 0 <= timing.interrupt_to_drained_ms < 80
        assert len(hermes.chats) == 1
        stt.emit(SttEvent(kind='transcript', text='Actually compare', is_final=True))
        await asyncio.sleep(0.04)
        stt.emit(SttEvent(kind='transcript', text='the other two options', is_final=True))
        await asyncio.sleep(0.04)
        assert len(hermes.chats) == 1, 'fragment replaced research before endpoint'
        stt.emit(SttEvent(kind='utterance_end'))
        await until(lambda: len(hermes.chats) == 2)
        assert hermes.chats[1][1].endswith('Actually compare the other two options')
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 2)
