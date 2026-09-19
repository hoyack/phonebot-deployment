"""Real protocol adapters used by consent-based hold."""
import json
import httpx
import pytest
from phonesys.ari import AriClient, AriError
from phonesys.config import AriConfig, load_config
from phonesys.hermes import HermesClient, HermesError


async def test_channel_moh_protocol_and_errors():
    requests = []
    code = 204
    def respond(request):
        requests.append(request)
        return httpx.Response(code)
    ari = AriClient(AriConfig('http://offline/ari', 'u', 'p', 'phonesys'))
    await ari._client.aclose()
    ari._client = httpx.AsyncClient(base_url='http://offline/ari',
                                    transport=httpx.MockTransport(respond))
    try:
        assert hasattr(ari, 'start_moh'), 'missing caller-channel MOH API'
        await ari.start_moh('caller', 'phonesys_hold')
        await ari.stop_moh('caller')
        assert [(r.method, r.url.path) for r in requests] == [
            ('POST', '/ari/channels/caller/moh'), ('DELETE', '/ari/channels/caller/moh')]
        assert requests[0].url.params['mohClass'] == 'phonesys_hold'
        code = 404
        await ari.stop_moh('caller')
        code = 500
        with pytest.raises(AriError):
            await ari.start_moh('caller', 'phonesys_hold')
        with pytest.raises(AriError):
            await ari.stop_moh('caller')
    finally:
        await ari.aclose()


def test_yaml_loads_hold_options(tmp_path, monkeypatch):
    for key in ('ARI_USER', 'ARI_PASSWORD', 'TEST_KEY'):
        monkeypatch.setenv(key, 'dummy')
    path = tmp_path / 'config.yaml'
    path.write_text('''extensions:
  7003:
    profile: atlas
    hermes_base_url: http://offline
    hermes_api_key_env: TEST_KEY
    greeting: Hi
    voice_id: atlas
    hold_enabled: true
    hold_offer_seconds: 12
    hold_feedback_seconds: 14
    hold_moh_class: custom_hold
    hermes_reply_timeout_seconds: 300
''')
    ext = load_config(path).extensions['7003']
    assert ext.hold_enabled
    assert (ext.hold_offer_seconds, ext.hold_feedback_seconds, ext.hold_moh_class) == (12, 14, 'custom_hold')


@pytest.mark.parametrize('terminal', ['error', 'run.failed', 'run.cancelled'])
async def test_sse_terminal_failure_is_not_silent(terminal):
    client = HermesClient('http://offline', 'dummy')
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url='http://offline', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=f'event: {terminal}\ndata: {{"message":"SECRET"}}\n\n')))
    try:
        with pytest.raises(HermesError, match='stream failed'):
            _ = [e async for e in client.chat_events('s', 'question')]
    finally:
        await client.aclose()


async def test_typed_sse_sanitizes_lifecycle_metadata_and_preserves_text():
    data = [('tool.started', {'tool_name': 'web_search', 'args': 'SECRET'}),
            ('tool.completed', {'tool_name': 'web_search', 'result': 'SECRET'}),
            ('tool.failed', {'tool_name': 'terminal', 'error': 'SECRET'}),
            ('tool.progress', {'tool_name': '_thinking', 'delta': 'SECRET'}),
            ('assistant.delta', {'delta': 'Answer.'}),
            ('done', {})]
    body = ''.join(f'event: {k}\ndata: {json.dumps(v)}\n\n' for k,v in data)
    client = HermesClient('http://offline', 'dummy')
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url='http://offline', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=body)))
    try:
        assert hasattr(client, 'chat_events'), 'typed safe SSE is missing'
        events = [e async for e in client.chat_events('s', 'question')]
        assert [e.kind for e in events] == ['tool.started', 'tool.completed', 'tool.failed', 'assistant.delta', 'done']
        assert 'SECRET' not in repr(events)
        assert events[0].text == "I'm searching for information."
        assert [x async for x in client.chat_stream('s', 'question')] == ['Answer.']
    finally:
        await client.aclose()
