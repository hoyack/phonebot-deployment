import httpx
import pytest
from phonesys.stt import SttClient, SttError

DG_OK = {"results": {"channels": [{"alternatives": [{"transcript": "hello world"}]}]}}
DG_EMPTY = {"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}}
DG_NO_CHANNELS = {"results": {"channels": []}}


def _client(handler) -> SttClient:
    c = SttClient(api_key="k")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.deepgram.com",
        headers={"Authorization": "Token k"},
    )
    return c


async def test_transcribe_returns_text():
    def handler(request):
        assert request.url.path == "/v1/listen"
        assert request.headers["Authorization"] == "Token k"
        assert request.headers["Content-Type"] == "audio/wav"
        return httpx.Response(200, json=DG_OK)

    assert await _client(handler).transcribe(b"RIFF....") == "hello world"


async def test_silence_returns_empty_string():
    def handler(request):
        return httpx.Response(200, json=DG_EMPTY)

    assert await _client(handler).transcribe(b"RIFF....") == ""


async def test_missing_channels_returns_empty_string():
    def handler(request):
        return httpx.Response(200, json=DG_NO_CHANNELS)

    assert await _client(handler).transcribe(b"RIFF....") == ""


async def test_error_status_raises():
    def handler(request):
        return httpx.Response(401, json={"err_msg": "bad key"})

    with pytest.raises(SttError):
        await _client(handler).transcribe(b"RIFF....")
