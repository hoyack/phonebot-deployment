import httpx
import pytest
from phonesys.ari import AriClient, AriError
from phonesys.config import AriConfig

CFG = AriConfig(base_url="http://ari.test/ari", username="u", password="p", app="phonesys")


def _client(handler) -> AriClient:
    c = AriClient(CFG)
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ari.test/ari",
    )
    return c


async def test_answer_posts_to_channel():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(204)

    await _client(handler).answer("chan-1")
    assert seen["path"] == "/ari/channels/chan-1/answer"


async def test_play_returns_playback_id():
    def handler(request):
        assert request.url.params["media"] == "sound:phonesys/reply-1"
        return httpx.Response(201, json={"id": "pb-1"})

    assert await _client(handler).play("chan-1", "sound:phonesys/reply-1") == "pb-1"


async def test_play_sends_the_caller_supplied_playback_id():
    """The parameter is 'playbackId'. A wrong name here is silently ignored by
    ARI (the same trap as format/formats on origination): Asterisk allocates
    its own id, PlaybackFinished then never matches the waiter, and every
    prompt stalls until the timeout instead of failing visibly."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(201, json={"id": "pb-mine"})

    await _client(handler).play("chan-1", "sound:phonesys/reply-1", "pb-mine")
    assert seen["playbackId"] == "pb-mine"


async def test_play_omits_playback_id_when_not_given():
    def handler(request):
        assert "playbackId" not in request.url.params
        return httpx.Response(201, json={"id": "pb-1"})

    await _client(handler).play("chan-1", "sound:phonesys/reply-1")


async def test_record_sends_silence_and_duration():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(201, json={"name": "turn-1"})

    await _client(handler).record("chan-1", "turn-1", max_silence=2, max_duration=30)
    assert seen["maxSilenceSeconds"] == "2"
    assert seen["maxDurationSeconds"] == "30"
    assert seen["format"] == "wav"
    assert seen["name"] == "turn-1"


async def test_error_status_raises():
    def handler(request):
        return httpx.Response(404, json={"message": "Channel not found"})

    with pytest.raises(AriError):
        await _client(handler).answer("gone")


async def test_hangup_tolerates_already_gone_channel():
    def handler(request):
        return httpx.Response(404, json={"message": "Channel not found"})

    # A caller who hung up first must not turn cleanup into an error.
    await _client(handler).hangup("chan-1")


async def test_create_bridge_returns_id():
    def handler(request):
        assert request.url.path == "/ari/bridges"
        assert request.url.params["type"] == "mixing"
        return httpx.Response(200, json={"id": "br-1"})

    assert await _client(handler).create_bridge() == "br-1"


async def test_originate_audiosocket_pins_slin_format():
    """Measured: without formats=slin the driver negotiates slin192 and sends
    7680-byte frames at 375 KB/s. 'format' singular is silently ignored."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"id": "ch-1"})

    cid = await _client(handler).originate_audiosocket(
        "host.docker.internal", 9010, "abc-uuid", "phonesys"
    )
    assert cid == "ch-1"
    assert seen["endpoint"] == "AudioSocket/host.docker.internal:9010/abc-uuid"
    assert seen["formats"] == "slin"
    assert "format" not in seen
    assert seen["app"] == "phonesys"


async def test_originate_audiosocket_can_mark_a_preflight_channel():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"id": "ch-preflight"})

    cid = await _client(handler).originate_audiosocket(
        "host.docker.internal",
        19010,
        "preflight-uuid",
        "phonesys",
        app_args="__preflight__",
    )

    assert cid == "ch-preflight"
    assert seen["appArgs"] == "__preflight__"


async def test_add_to_bridge():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["channel"] = request.url.params["channel"]
        return httpx.Response(204)

    await _client(handler).add_to_bridge("br-1", "ch-1")
    assert seen["path"] == "/ari/bridges/br-1/addChannel"
    assert seen["channel"] == "ch-1"
