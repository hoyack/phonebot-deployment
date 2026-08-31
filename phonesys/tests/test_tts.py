import json

import httpx
import pytest
from phonesys.tts import TtsClient, TtsError


def _client(handler) -> TtsClient:
    c = TtsClient(api_key="k")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.elevenlabs.io",
        headers={"xi-api-key": "k"},
    )
    return c


async def test_synthesize_returns_audio_bytes():
    def handler(request):
        assert request.url.path == "/v1/text-to-speech/voice-abc"
        assert request.url.params["output_format"] == "ulaw_8000"
        assert request.headers["xi-api-key"] == "k"
        assert json.loads(request.content)["text"] == "hello"
        return httpx.Response(200, content=b"\xff\xfe\xfd")

    assert await _client(handler).synthesize("hello", "voice-abc") == b"\xff\xfe\xfd"


async def test_error_status_raises():
    def handler(request):
        return httpx.Response(422, json={"detail": "bad voice"})

    with pytest.raises(TtsError):
        await _client(handler).synthesize("hello", "voice-abc")


async def test_empty_text_raises_without_calling_api():
    def handler(request):
        raise AssertionError("must not call the API for empty text")

    with pytest.raises(TtsError):
        await _client(handler).synthesize("   ", "voice-abc")
