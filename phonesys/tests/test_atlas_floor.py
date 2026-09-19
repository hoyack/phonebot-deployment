"""Offline Atlas floor reclamation through the real duplex handler."""
import asyncio

from phonesys import stream_call
from phonesys.stream_stt import SttEvent
from test_hold import say, until
from test_stream_call import FakeAudio, FakeStt, FakeTts, FakeServer, _ext, _handler
from test_stream_stalls import StalledHermes


async def test_production_quiet_window_after_interrupting_answer(monkeypatch):
    # Exercise the shipped two-second policy, not just accelerated fixtures.
    assert stream_call.POST_BARGE_QUIET_SECONDS == 2.0
    baseline = asyncio.all_tasks()
    hermes, audio, stt = StalledHermes(), FakeAudio(send_delay=0.005), FakeStt()
    monkeypatch.setattr(audio, 'MAX_INBOUND', 10000)
    class LongAnswer(FakeTts):
        async def synthesize_frames(self, text, voice_id, sentence_index):
            self._n = 200 if text == 'Late answer.' else 1
            return await super().synthesize_frames(text, voice_id, sentence_index)
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=LongAnswer()).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True, barge_in_confirm_ms=0)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
        stt.emit(SttEvent(kind='transcript', text='Actually', is_final=False))
        await until(lambda: not audio.is_sending)
        assert audio.spoken_text.count('Late answer.') < 200
        say(stt, 'Actually send that to Archon')
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(1.3)
        assert len(hermes.chats) == 1
        async with asyncio.timeout(1.5):
            while len(hermes.chats) < 2:
                await asyncio.sleep(0.01)
        assert asyncio.get_running_loop().time() - start >= 2.0
        assert hermes.chats[1][1].endswith('Actually send that to Archon')
        assert "I'm working on your request." not in audio.spoken_text
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)


async def test_renewed_interims_invalidate_endpoint_and_fallback(monkeypatch):
    monkeypatch.setattr(stream_call, 'POST_BARGE_QUIET_SECONDS', 0.08)
    monkeypatch.setattr(stream_call, 'UTTERANCE_END_FALLBACK', 0.12)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True, barge_in_confirm_ms=0)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'Take that')
        for _ in range(7):
            await asyncio.sleep(0.03)
            stt.emit(SttEvent(kind='transcript', text='and send', is_final=False))
        await asyncio.sleep(0.03)
        assert len(hermes.chats) == 1
        # No UtteranceEnd for the continuation: activity-based fallback works.
        stt.emit(SttEvent(kind='transcript', text='and send it to Archon', is_final=True))
        await until(lambda: len(hermes.chats) == 2)
        assert hermes.chats[1][1] == 'Take that and send it to Archon'
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_tentative_noise_does_not_hold_floor_forever(monkeypatch):
    monkeypatch.setattr(stream_call, 'UTTERANCE_END_FALLBACK', 0.08)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        stt.emit(SttEvent(kind='speech_started'))
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
        assert len(hermes.chats) == 1
        await asyncio.sleep(0.03)  # the completed answer has returned to listening
        say(stt, 'An ordinary next request')
        await asyncio.sleep(0.06)
        assert len(hermes.chats) == 2, 'expired noise left a stale reclaimed floor'
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_atlas_hold_consent_and_music_controls_keep_original_run(caplog):
    import logging
    caplog.set_level(logging.INFO, logger='phonesys.hold')
    from test_hold import MusicAri
    ari, hermes, audio, stt = MusicAri(), StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(ari=ari, hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True,
                             hold_offer_seconds=0.02, hold_feedback_seconds=0.1)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await until(lambda: any('Would you like hold music' in s for s in audio.spoken_text))
        say(stt, 'yes')
        await until(lambda: ari.music)
        say(stt, 'right')
        await until(lambda: any(getattr(r, 'outcome', '') == 'backchannel' for r in caplog.records))
        assert ari.music and len(hermes.chats) == 1
        say(stt, 'take me off hold')
        await until(lambda: not ari.music)
        assert len(hermes.chats) == 1 and not hermes.stream_closed
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_barge_in_endpoints_do_not_dispatch_fragments(monkeypatch):
    # Scale only the local floor policy, not provider endpointing or PBX timers.
    monkeypatch.setattr(stream_call, 'POST_BARGE_QUIET_SECONDS', 0.15)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True,
                             filler_delay_ms=20, barge_in_confirm_ms=0)))
    fragments = ["Okay. I'm interested in that one.",
                 'I want to know more about that, and I want it logged.',
                 'I also want you to take that',
                 "one and actually let's pass it over",
                 "to Archon and tell Archon to work on it."]
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        await until(lambda: "I'm working on your request." in audio.spoken_text)
        for text in fragments:
            say(stt, text)
            await asyncio.sleep(0.06)
            assert len(hermes.chats) == 1, 'endpoint dispatched a partial replacement request'
        await until(lambda: len(hermes.chats) == 2)
        assert hermes.chats[1][1] == ' '.join(fragments)
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_work_cutoff_does_not_release_reclaimed_floor_early(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger='phonesys.hold')
    monkeypatch.setattr(stream_call, 'POST_BARGE_QUIET_SECONDS', 0.2)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True,
                             hermes_reply_timeout_seconds=0.07)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'Actually take that')
        await asyncio.sleep(0.13)
        assert len(hermes.chats) == 1, 'work cutoff dispatched unfinished caller speech'
        assert audio.spoken_text == ['Hello there.']
        assert any(getattr(r, 'event', '') == 'hold.timeout' and
                   getattr(r, 'outcome', '') == 'reply_deadline' for r in caplog.records)
        say(stt, 'and send it to Archon')
        await until(lambda: len(hermes.chats) == 2)
        assert hermes.chats[1][1] == 'Actually take that and send it to Archon'
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_floor_claim_cancels_pending_ack_instead_of_deferring_it(monkeypatch):
    monkeypatch.setattr(stream_call, 'UTTERANCE_END_FALLBACK', 0.08)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True, filler_delay_ms=40)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        stt.emit(SttEvent(kind='speech_started'))
        await asyncio.sleep(0.2)
        assert audio.spoken_text == ['Hello there.'], 'deferred filler fired when caller floor expired'
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_speech_start_gates_new_audio_before_confirmed_interim(monkeypatch):
    monkeypatch.setattr(stream_call, 'POST_BARGE_QUIET_SECONDS', 0.08)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True, filler_delay_ms=60)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        before = list(audio.spoken_text)
        stt.emit(SttEvent(kind='speech_started'))
        await asyncio.sleep(0.13)
        assert audio.spoken_text == before, 'new filler stole the caller floor before the first final'
        say(stt, 'I am still explaining the request')
        await until(lambda: len(hermes.chats) == 2)
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_reclaimed_floor_does_not_restart_immediate_ack(monkeypatch):
    monkeypatch.setattr(stream_call, 'POST_BARGE_QUIET_SECONDS', 0.04)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True, filler_delay_ms=20)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await until(lambda: "I'm working on your request." in audio.spoken_text)
        say(stt, 'Actually research the other one instead')
        await until(lambda: len(hermes.chats) == 2)
        await asyncio.sleep(0.12)
        assert audio.spoken_text.count("I'm working on your request.") == 1
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_expired_noise_floor_leaves_the_next_turn_prompt_and_acknowledged(monkeypatch):
    # A reclaim that never produced a final is noise. The shipped
    # POST_BARGE_QUIET_SECONDS stays at 2.0 here on purpose: the NEXT ordinary
    # turn must not be charged a settling window for speech that never came,
    # and must not lose its acknowledgement to a stale suppression.
    monkeypatch.setattr(stream_call, 'UTTERANCE_END_FALLBACK', 0.08)
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True, filler_delay_ms=20)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        stt.emit(SttEvent(kind='speech_started'))
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
        await asyncio.sleep(0.1)  # the reclaim window has now expired unused
        hermes.release.clear()  # next run must wait long enough to need an ack
        spoken_before = len(audio.spoken_text)
        say(stt, 'An ordinary next request')
        start = asyncio.get_running_loop().time()
        await until(lambda: len(hermes.chats) == 2)
        assert asyncio.get_running_loop().time() - start < 0.5, \
            'a stale reclaim charged the next turn the post-barge-in quiet window'
        await until(lambda: "I'm working on your request."
                    in audio.spoken_text[spoken_before:])
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_status_under_a_claimed_floor_leaves_no_stale_ack(monkeypatch):
    # The floor claim must CANCEL the pending acknowledgement, not defer it:
    # a deferred ack lands after the status answer, filling the line with
    # filler the caller has already been answered past.
    monkeypatch.setattr(stream_call, 'UTTERANCE_END_FALLBACK', 0.1)
    from phonesys.hold import STATUS
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True, filler_delay_ms=60,
                             hold_offer_seconds=30, hold_feedback_seconds=30)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        say(stt, 'status')
        await until(lambda: STATUS in audio.spoken_text)
        await asyncio.sleep(0.25)  # past both the ack deadline and the floor window
        assert "I'm working on your request." not in audio.spoken_text, \
            'a deferred ack spoke stale filler after the status answer'
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_floor_cutoff_is_silent_over_the_caller_but_still_reported(monkeypatch, caplog):
    # Staying quiet over the caller must not cost the operator the timeout
    # record: the floor cutoff reports exactly what the other cutoff reports.
    import logging
    caplog.set_level(logging.INFO, logger='phonesys.hold')
    hermes, audio, stt = StalledHermes(), FakeAudio(), FakeStt()
    task = asyncio.create_task(_handler(hermes=hermes, stt=stt,
        server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1)).handle(
        'caller', '7003', _ext(profile='atlas', hold_enabled=True,
                             hermes_reply_timeout_seconds=0.07)))
    try:
        await until(lambda: bool(audio.sent))
        say(stt, 'Research this')
        await hermes.started.wait()
        stt.emit(SttEvent(kind='speech_started'))  # floor claimed, no final yet
        await asyncio.sleep(0.2)                   # the work cutoff passes underneath
        assert audio.spoken_text == ['Hello there.'], 'the work cutoff spoke over the caller'
        assert any(getattr(r, 'event', '') == 'hold.timeout' and
                   getattr(r, 'outcome', '') == 'reply_deadline' for r in caplog.records), \
            'the floor cutoff dropped the hold.timeout diagnostic'
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


async def test_floor_claimed_during_a_speech_attempt_keeps_hold_music():
    # Isolated floor callback: the caller starts speaking in the window
    # between the loop's floor check and the speech attempt itself. Speech
    # that is going to be suppressed must not tear down the music first and
    # leave the caller on a silent line.
    from test_hold import MusicAri
    from phonesys.hold import STATUS
    ari, hermes, audio = MusicAri(), StalledHermes(), FakeAudio()
    handler = _handler(ari=ari, hermes=hermes, stt=FakeStt(),
                       server=FakeServer(audio), tts=FakeTts(frames_per_sentence=1))
    pending, floor, ended = [''], [False], asyncio.Event()

    def read_utterance():
        if pending[0] == 'status':
            floor[0] = True  # the caller keeps talking as the control is read
        return pending[0]

    task = asyncio.create_task(handler._reply(
        audio, _ext(profile='atlas', hold_enabled=True, hold_offer_seconds=0.02,
                    hold_feedback_seconds=30),
        hermes, 'session', 'Research this',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=ended, channel_id='chan',
        deadline=asyncio.get_running_loop().time() + 10,
        read_utterance=read_utterance,
        consume=lambda: pending.__setitem__(0, ''),
        caller_has_floor=lambda: floor[0]))
    try:
        await until(lambda: any('Would you like hold music' in s for s in audio.spoken_text))
        pending[0] = 'yes'
        await until(lambda: ari.music)
        pending[0] = 'status'
        await until(lambda: floor[0])
        await asyncio.sleep(0.05)
        assert ari.music and not ari.stops, 'a suppressed speech attempt stopped the music'
        assert STATUS not in audio.spoken_text
        floor[0] = False
        hermes.release.set()
        await until(lambda: 'Late answer.' in audio.spoken_text)
    finally:
        ended.set()
        await asyncio.wait_for(task, 1)
