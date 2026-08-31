import asyncio
import json

import pytest

import phonesys.stream_stt as stream_stt
from phonesys.stream_stt import LiveStt, parse_dg_message


def test_speech_started_event():
    ev = parse_dg_message(json.dumps({"type": "SpeechStarted", "timestamp": 1.5}))
    assert ev is not None and ev.kind == "speech_started"


def test_interim_transcript():
    ev = parse_dg_message(json.dumps({
        "type": "Results", "is_final": False, "speech_final": False,
        "channel": {"alternatives": [{"transcript": "hello wor"}]},
    }))
    assert ev.kind == "transcript" and ev.text == "hello wor" and ev.is_final is False


def test_final_transcript():
    ev = parse_dg_message(json.dumps({
        "type": "Results", "is_final": True, "speech_final": True,
        "channel": {"alternatives": [{"transcript": "hello world"}]},
    }))
    assert ev.kind == "transcript" and ev.text == "hello world" and ev.is_final is True


def test_utterance_end():
    ev = parse_dg_message(json.dumps({"type": "UtteranceEnd", "last_word_end": 3.2}))
    assert ev.kind == "utterance_end"


def test_empty_transcript_is_ignored():
    ev = parse_dg_message(json.dumps({
        "type": "Results", "is_final": True,
        "channel": {"alternatives": [{"transcript": ""}]},
    }))
    assert ev is None


def test_metadata_is_ignored():
    assert parse_dg_message(json.dumps({"type": "Metadata", "duration": 1.0})) is None


def test_malformed_json_is_ignored():
    assert parse_dg_message("not json") is None


def test_json_null_is_ignored():
    """Syntactically valid JSON that isn't an object must not crash the
    parser -- json.loads("null") returns None, and the old code called
    .get("type") on it directly, which would raise AttributeError and take
    down the event pump mid-call. Same defect class already fixed in
    hermes.chat_stream this session; same isinstance-dict guard here."""
    assert parse_dg_message("null") is None


def test_json_bare_number_is_ignored():
    assert parse_dg_message("42") is None


def test_json_list_is_ignored():
    assert parse_dg_message("[]") is None


def test_default_model_is_nova2_phonecall():
    stt = LiveStt(api_key="fake-key")
    assert stt._params["model"] == "nova-2-phonecall"


def test_custom_model_is_passed_through():
    stt = LiveStt(api_key="fake-key", model="nova-3")
    assert stt._params["model"] == "nova-3"


class _HangingSocket:
    """Stand-in for a stalled websocket connection: send() never returns.
    Used to prove send()/close() are bounded by an explicit timeout rather
    than hanging indefinitely, without touching the network."""

    async def send(self, data):
        await asyncio.sleep(10)

    async def close(self):
        return None


async def test_connect_passes_explicit_open_and_close_timeouts(monkeypatch):
    """connect() must pass explicit open_timeout/close_timeout to
    websockets.connect() rather than relying on library defaults, per the
    project rule that every network operation carries an explicit timeout."""
    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _HangingSocket()

    monkeypatch.setattr(stream_stt.websockets, "connect", fake_connect)

    stt = LiveStt(api_key="fake-key")
    await stt.connect()

    assert captured["open_timeout"] == stream_stt.CONNECT_TIMEOUT
    assert captured["close_timeout"] == stream_stt.CLOSE_TIMEOUT
    assert captured["additional_headers"] == {"Authorization": "Token fake-key"}


async def test_send_is_bounded_by_explicit_timeout(monkeypatch):
    monkeypatch.setattr(stream_stt, "SEND_TIMEOUT", 0.05)
    stt = LiveStt(api_key="fake-key")
    stt._ws = _HangingSocket()
    with pytest.raises(asyncio.TimeoutError):
        await stt.send(b"\x00" * 320)


async def test_close_stream_send_is_bounded_and_close_still_completes(monkeypatch):
    """close()'s best-effort CloseStream send must not be allowed to hang
    the shutdown path -- it should time out quietly (caught by the existing
    broad except) and close() must still finish and clear _ws."""
    monkeypatch.setattr(stream_stt, "SEND_TIMEOUT", 0.05)
    stt = LiveStt(api_key="fake-key")
    stt._ws = _HangingSocket()
    await stt.close()
    assert stt._ws is None
