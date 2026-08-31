import inspect
import json

import httpx
import pytest
from phonesys.hermes import HermesClient, HermesError


def _client(handler) -> HermesClient:
    transport = httpx.MockTransport(handler)
    c = HermesClient(base_url="http://hermes.test", api_key="k")
    c._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://hermes.test",
        headers={"Authorization": "Bearer k"},
    )
    return c


async def test_default_timeout_is_a_stall_detector_not_a_patience_setting():
    """Measured Hermes generation is 2-11s. The 60s this shipped with was not
    a slow-answer allowance, it was 60s of dead air on the phone: a live call
    issued /chat, Hermes never answered, and the caller heard nothing until
    httpx.ReadTimeout fired 64s later and killed the call.

    Asserted on the httpx client too, not just the signature default: a
    default nobody passes to AsyncClient would be documentation, not a
    timeout.
    """
    default = inspect.signature(HermesClient.__init__).parameters["timeout"].default
    assert default == 30.0
    c = HermesClient(base_url="http://hermes.test", api_key="k")
    try:
        assert c._client.timeout.read == 30.0
        assert c._client.timeout.connect == 30.0
    finally:
        await c.aclose()


async def test_create_session_returns_id():
    def handler(request):
        assert request.url.path == "/api/sessions"
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(201, json={"object": "hermes.session", "session": {"id": "call-1"}})

    assert await _client(handler).create_session("call-1") == "call-1"


async def test_create_session_conflict_reuses_id():
    def handler(request):
        return httpx.Response(409, json={"error": {"message": "Session already exists"}})

    assert await _client(handler).create_session("call-1") == "call-1"


async def test_chat_returns_assistant_text():
    def handler(request):
        assert request.url.path == "/api/sessions/call-1/chat"
        assert json.loads(request.content)["message"] == "hello"
        return httpx.Response(200, json={
            "object": "hermes.session.chat.completion",
            "session_id": "call-1",
            "message": {"role": "assistant", "content": "hi there"},
        })

    assert await _client(handler).chat("call-1", "hello") == "hi there"


async def test_http_error_raises_hermes_error():
    def handler(request):
        return httpx.Response(500, json={"error": {"message": "boom"}})

    with pytest.raises(HermesError):
        await _client(handler).chat("call-1", "hello")


async def test_empty_reply_returns_empty_string():
    def handler(request):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": ""}})

    assert await _client(handler).chat("call-1", "hello") == ""


SSE = (
    b'event: run.started\ndata: {"session_id": "s"}\n\n'
    b'event: message.started\ndata: {"message": {"id": "m", "role": "assistant"}}\n\n'
    b'event: assistant.delta\ndata: {"delta": "Hello"}\n\n'
    b'event: assistant.delta\ndata: {"delta": " there."}\n\n'
    b'event: tool.progress\ndata: {"tool_name": "_thinking", "delta": "SECRET REASONING"}\n\n'
    b'event: assistant.delta\ndata: {"delta": " How are you?"}\n\n'
    b'event: assistant.completed\ndata: {"content": "Hello there. How are you?"}\n\n'
    b'event: done\ndata: {"seq": 9}\n\n'
)


async def test_chat_stream_yields_only_assistant_deltas():
    """tool.progress carries a delta field too. Filtering on the presence of
    'delta' rather than the event name would speak internal reasoning aloud."""
    def handler(request):
        assert request.url.path == "/api/sessions/s1/chat/stream"
        return httpx.Response(200, content=SSE,
                              headers={"content-type": "text/event-stream"})

    c = _client(handler)
    deltas = [d async for d in c.chat_stream("s1", "hi")]
    assert deltas == ["Hello", " there.", " How are you?"]
    assert "SECRET REASONING" not in "".join(deltas)


async def test_chat_stream_error_raises():
    def handler(request):
        return httpx.Response(500, json={"error": {"message": "boom"}})

    with pytest.raises(HermesError):
        async for _ in _client(handler).chat_stream("s1", "hi"):
            pass


SSE_MALFORMED = (
    b'event: assistant.delta\ndata: {"delta": "Hello"}\n\n'
    b'event: assistant.delta\ndata: null\n\n'
    b'event: assistant.delta\ndata: "x"\n\n'
    b'event: assistant.delta\ndata: 42\n\n'
    b'event: assistant.delta\ndata: {"delta": " there."}\n\n'
    b'event: done\ndata: {"seq": 1}\n\n'
)


async def test_chat_stream_skips_non_object_delta_payloads():
    """A valid-JSON but non-dict data payload (null, a bare string, a number)
    on an assistant.delta event must be skipped, not crash the generator --
    the rest of the reply still has to arrive."""
    def handler(request):
        return httpx.Response(200, content=SSE_MALFORMED,
                              headers={"content-type": "text/event-stream"})

    deltas = [d async for d in _client(handler).chat_stream("s1", "hi")]
    assert deltas == ["Hello", " there."]
