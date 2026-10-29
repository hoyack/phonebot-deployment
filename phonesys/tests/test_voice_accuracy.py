"""Offline regressions for conservative voice accuracy changes."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from phonesys.stream_stt import LiveStt, parse_dg_message
from test_stream_call import FakeAudio, FakeStt, FakeTts, FakeHermes, FakeServer, _handler, _ext
from test_hold import say, until


class VocabularyStt(FakeStt):
    def __init__(self, audio):
        super().__init__(audio)
        self.keywords = ()
        self.connections = []

    def set_keywords(self, keywords):
        self.keywords = keywords

    async def connect(self):
        self.connections.append(self.keywords)
        await super().connect()


@pytest.mark.parametrize('transfer', [False, True])
async def test_vocabulary_follows_authenticated_extension_without_losing_audio(transfer):
    audio = FakeAudio()
    stt = VocabularyStt(audio)
    hermes = FakeHermes(['Answer.'])
    atlas = _ext(profile='atlas', full_name='Atlas Assistant', directory_listing=True,
                 stt_keywords=('OpenClaw:1',), greeting='Atlas online.')
    mary = _ext(profile='phonebot', receptionist=True, greeting='Mary speaking.')
    handler = _handler(stt=stt, hermes=hermes, server=FakeServer(audio),
                       tts=FakeTts(frames_per_sentence=1),
                       extensions={'7001': mary, '7003': atlas})
    task = asyncio.create_task(handler.handle('vocab-call', '7001' if transfer else '7003',
                                               mary if transfer else atlas))
    try:
        await until(lambda: bool(audio.spoken_text))
        if transfer:
            say(stt, 'transfer me to Atlas')
            await until(lambda: atlas.greeting in audio.spoken_text)
        say(stt, 'Compare OpenCart plugins.')
        await until(lambda: 'Answer.' in audio.spoken_text)
        assert hermes.chats[-1][1] == 'Compare OpenCart plugins.'
        assert stt.connections == ([()] if transfer else []) + [('OpenClaw:1',)]
        assert stt.sent
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)

async def test_nova2_keywords_are_repeated_query_hints_not_replacements(monkeypatch):
    urls = []

    async def connect(url, **kwargs):
        urls.append(url)
        return object()

    monkeypatch.setattr('phonesys.stream_stt.websockets.connect', connect)
    stt = LiveStt('fake-key')
    assert hasattr(stt, 'set_keywords'), 'per-extension vocabulary not supported'
    stt.set_keywords(('OpenClaw:1', 'Hermes:1'))
    await stt.connect()
    query = parse_qs(urlsplit(urls[0]).query)
    assert query['model'] == ['nova-2-phonecall']
    assert query['keywords'] == ['OpenClaw:1', 'Hermes:1']
    assert 'keyterm' not in query
    message = json.dumps({'type': 'Results', 'is_final': True,
                          'channel': {'alternatives': [{'transcript': 'Compare OpenCart plugins.'}]}})
    assert parse_dg_message(message).text == 'Compare OpenCart plugins.'


def test_shipped_vocabulary_supports_receptionist_directory_and_atlas(tmp_path, monkeypatch):
    from pathlib import Path
    from phonesys.config import load_config
    for name in ('ARI_USER', 'ARI_PASSWORD', 'HERMES_PHONEBOT_API_KEY',
                 'HERMES_JYODER_API_KEY', 'HERMES_ATLAS_API_KEY',
                 'ATLAS_PIN', 'JOHN_YODER_PIN'):
        monkeypatch.setenv(name, 'test-secret')
    # Isolate from the live .env file.
    path = tmp_path / 'config.yaml'
    path.write_text((Path(__file__).parents[1] / 'config.yaml').read_text())
    cfg = load_config(path)
    assert getattr(cfg.extensions['7001'], 'stt_keywords', ()) == ('Atlas:2',)
    assert getattr(cfg.extensions['7003'], 'stt_keywords', ()) == (
        'OpenClaw:1', 'Archon:2'
    )
    assert all(
        not ext.stt_keywords
        for number, ext in cfg.extensions.items()
        if number not in ('7001', '7003')
    )


@pytest.mark.parametrize('message, clarify', [
    ('Okay. Can you look into what you have been', True),
    ('Can you look into what you have been?', True),
    ('Can you look into what you have been researching about OpenClaw?', False),
    ('Compare OpenCart plugins.', False),
    ('What have you been doing?', False),
])
async def test_atlas_observed_hanging_request_clarifies_without_starting_research(message, clarify):
    hermes, audio = FakeHermes(['Answer.']), FakeAudio()
    await _handler()._reply(audio, _ext(profile='atlas', hold_enabled=True), hermes,
        'session', message, interrupt=asyncio.Event(), utterance_ready=asyncio.Event(),
        final_seen=asyncio.Event(), ended=asyncio.Event(),
        deadline=asyncio.get_running_loop().time()+1)
    if clarify:
        assert not hermes.chats, 'truncated request was sent for speculative research'
        assert any('finish your question' in s for s in audio.spoken_text)
    else:
        assert hermes.chats[-1][1] == message
        assert 'Answer.' in audio.spoken_text


@pytest.mark.parametrize('profile', ['atlas', 'phonebot', 'jyoder'])
async def test_incomplete_guard_is_atlas_only_and_followup_still_works(profile):
    audio = FakeAudio()
    stt, hermes = FakeStt(audio), FakeHermes(['Answer.'])
    handler = _handler(stt=stt, hermes=hermes, server=FakeServer(audio),
                       tts=FakeTts(frames_per_sentence=1))
    task = asyncio.create_task(handler.handle('incomplete', '7003', _ext(profile=profile)))
    clipped = 'Okay. Can you look into what you have been'
    try:
        await until(lambda: bool(audio.spoken_text))
        say(stt, clipped)
        if profile == 'atlas':
            await until(lambda: 'Please repeat the full request.' in audio.spoken_text)
            assert not hermes.chats
        else:
            await until(lambda: bool(hermes.chats))
            assert hermes.chats[-1][1] == clipped
        say(stt, 'Compare OpenCart plugins.')
        await until(lambda: any(m == 'Compare OpenCart plugins.' for _, m in hermes.chats))
    finally:
        audio.hangup.set()
        await asyncio.wait_for(task, 1)


@pytest.mark.parametrize('ending', ['hangup', 'deadline', 'cancel'])
async def test_local_clarification_respects_call_lifetime(ending):
    from contextlib import suppress
    audio, hermes, ended = FakeAudio(send_delay=0.01), FakeHermes(), asyncio.Event()
    task = asyncio.create_task(_handler(tts=FakeTts(frames_per_sentence=100))._reply(
        audio, _ext(profile='atlas'), hermes, 'session', 'Can you look into what you have been',
        interrupt=asyncio.Event(), utterance_ready=asyncio.Event(), final_seen=asyncio.Event(),
        ended=ended, deadline=asyncio.get_running_loop().time() + (0.03 if ending == 'deadline' else 1)))
    try:
        await until(lambda: audio.is_sending)
        if ending == 'hangup':
            ended.set()
        elif ending == 'cancel':
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.3)
        assert not hermes.chats
        assert not audio.is_sending and not audio.cancelled
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_keywords_reject_unsupported_models():
    stt = LiveStt('fake-key', model='nova-3')
    assert hasattr(stt, 'set_keywords')
    with pytest.raises(ValueError, match='Nova-2'):
        stt.set_keywords(('OpenClaw:1',))
