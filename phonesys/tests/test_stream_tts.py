import httpx
import pytest

from phonesys.stream_tts import (
    ELEVENLABS_URL,
    SentenceBuffer,
    StreamTts,
    TtsStreamError,
    split_sentences,
)


def test_split_basic_sentences():
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_split_keeps_trailing_fragment():
    assert split_sentences("Done. And then") == ["Done.", "And then"]


def test_buffer_emits_only_complete_sentences():
    b = SentenceBuffer()
    assert b.feed("Hello") == []
    assert b.feed(" there.") == ["Hello there."]
    assert b.feed(" How are") == []
    assert b.feed(" you?") == ["How are you?"]


def test_buffer_flush_returns_remainder():
    b = SentenceBuffer()
    b.feed("Trailing words")
    assert b.flush() == "Trailing words"
    assert b.flush() == ""


def test_buffer_handles_multiple_sentences_in_one_delta():
    b = SentenceBuffer()
    assert b.feed("A. B. C") == ["A.", "B."]
    assert b.flush() == "C"


def test_frames_are_320_bytes_with_sentence_index():
    from phonesys.stream_tts import _frames_from_ulaw
    frames = _frames_from_ulaw(b"\xff" * 800, sentence_index=3)
    assert all(len(p) == 320 for p, _ in frames)
    assert all(i == 3 for _, i in frames)
    assert len(frames) == 5   # 800 ulaw bytes -> 1600 slin bytes -> 5 frames


def test_buffer_does_not_split_decimal_numbers_fed_token_by_token():
    # A language model emits tokens one at a time. "3", ".", "5" must not be
    # read as a finished sentence "3." followed by a stray "5" -- a phone
    # agent quoting a price ("$3.50") would sound broken.
    b = SentenceBuffer()
    assert b.feed("3") == []
    assert b.feed(".") == []
    assert b.feed("5") == []
    assert b.flush() == "3.5"


def test_buffer_does_not_split_version_numbers_fed_token_by_token():
    b = SentenceBuffer()
    assert b.feed("v1") == []
    assert b.feed(".") == []
    assert b.feed("2") == []
    assert b.flush() == "v1.2"


def test_buffer_still_emits_real_sentence_after_digit_delayed_one():
    # A digit-preceded period only *delays* emission; once more text with a
    # genuine sentence boundary arrives, both sentences come out and nothing
    # is lost.
    b = SentenceBuffer()
    assert b.feed("It costs 3") == []
    assert b.feed(".") == []
    assert b.feed("5 dollars. Thanks.") == ["It costs 3.5 dollars.", "Thanks."]
    assert b.flush() == ""


def _client(handler) -> StreamTts:
    c = StreamTts(api_key="k")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=ELEVENLABS_URL,
        headers={"xi-api-key": "k"},
    )
    return c


async def test_network_error_is_wrapped_as_tts_stream_error():
    def handler(request):
        raise httpx.TimeoutException("timed out", request=request)

    with pytest.raises(TtsStreamError):
        await _client(handler).synthesize_frames("Hello.", "voice-abc", 0)


async def test_a_json_body_with_a_200_is_rejected_instead_of_played():
    """ElevenLabs can answer 200 with a JSON envelope (quota notices, partial
    failures), and a proxy can answer 200 with an HTML error page. Run through
    the mu-law table, either becomes loud noise down the phone -- and the
    caller has no idea anything failed."""
    def handler(request):
        return httpx.Response(
            200, json={"detail": {"status": "quota_exceeded"}}
        )

    with pytest.raises(TtsStreamError, match="non-audio content-type"):
        await _client(handler).synthesize_frames("Hello.", "voice-abc", 0)


async def test_an_html_error_page_with_a_200_is_rejected():
    def handler(request):
        return httpx.Response(200, text="<html>gateway error</html>",
                              headers={"content-type": "text/html"})

    with pytest.raises(TtsStreamError, match="non-audio content-type"):
        await _client(handler).synthesize_frames("Hello.", "voice-abc", 0)


async def test_the_rejection_never_echoes_the_response_body():
    """An error envelope can quote back request fields, and this client is
    built with an API key in its headers. The message reports the type and a
    length, never the content."""
    def handler(request):
        return httpx.Response(200, json={"secret_echo": "sk-do-not-log-me"})

    with pytest.raises(TtsStreamError) as exc:
        await _client(handler).synthesize_frames("Hello.", "voice-abc", 0)
    assert "sk-do-not-log-me" not in str(exc.value)


async def test_a_ulaw_audio_body_is_converted_normally():
    """The check must not become an allow-list that rejects a valid but
    unfamiliar audio content-type -- that would break synthesis outright."""
    def handler(request):
        return httpx.Response(200, content=b"\xff" * 800,
                              headers={"content-type": "audio/basic"})

    frames = await _client(handler).synthesize_frames("Hello.", "voice-abc", 2)
    assert len(frames) == 5
    assert all(len(p) == 320 and i == 2 for p, i in frames)
