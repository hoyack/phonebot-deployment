from phonesys.config import ExtensionConfig
from phonesys.pipeline import run_turn

EXT = ExtensionConfig(
    profile="phonebot", hermes_base_url="http://h", hermes_api_key="k",
    greeting="hi", voice_id="v", max_silence_seconds=2,
    max_turn_seconds=30, max_call_seconds=600,
)


class FakeStt:
    def __init__(self, text): self.text = text
    async def transcribe(self, audio, content_type="audio/wav"): return self.text


class FakeHermes:
    def __init__(self, reply): self.reply, self.seen = reply, []
    async def chat(self, session_id, message):
        self.seen.append(message)
        return self.reply


class FakeTts:
    def __init__(self): self.seen = []
    async def synthesize(self, text, voice_id):
        self.seen.append(text)
        return b"AUDIO"


async def test_full_turn():
    hermes, tts = FakeHermes("I am well"), FakeTts()
    result = await run_turn(FakeStt("how are you"), hermes, tts, "s1", b"WAV", EXT)
    assert result.transcript == "how are you"
    assert result.reply == "I am well"
    assert result.audio == b"AUDIO"
    assert hermes.seen == ["how are you"]


async def test_silence_skips_hermes_and_tts():
    hermes, tts = FakeHermes("unused"), FakeTts()
    result = await run_turn(FakeStt("   "), hermes, tts, "s1", b"WAV", EXT)
    assert result.transcript == ""
    assert result.reply == ""
    assert result.audio == b""
    assert hermes.seen == []
    assert tts.seen == []


async def test_empty_audio_skips_stt_entirely():
    # Posting zero bytes to Deepgram is an HTTP 400, not a transcript.
    class ExplodingStt:
        async def transcribe(self, audio, content_type="audio/wav"):
            raise AssertionError("must not call STT with empty audio")

    hermes, tts = FakeHermes("unused"), FakeTts()
    result = await run_turn(ExplodingStt(), hermes, tts, "s1", b"", EXT)
    assert result.transcript == ""
    assert hermes.seen == []
    assert tts.seen == []


async def test_empty_reply_skips_tts():
    hermes, tts = FakeHermes(""), FakeTts()
    result = await run_turn(FakeStt("hello"), hermes, tts, "s1", b"WAV", EXT)
    assert result.reply == ""
    assert result.audio == b""
    assert tts.seen == []
