"""Sentence-level TTS for streaming playback.

Hermes emits token deltas; speaking each token separately sounds terrible and
costs a request per token. SentenceBuffer accumulates deltas and releases
whole sentences, which is also the granularity at which interrupted speech is
tracked.

Known limitation: sentence-end detection is a cheap regex/heuristic, not an
NLP tokenizer (deliberately -- this is a phone agent, not a document
parser). It special-cases the one high-frequency false-positive a phone
agent actually hits -- a decimal or version number ("$3.50", "v1.2") arriving
token by token, where the period is mid-token rather than a real sentence
end. Other abbreviations (e.g. "Dr.", "e.g.") are NOT special-cased and may
still cause a premature split with a mistimed pause.
"""
from __future__ import annotations

import re

import httpx

from phonesys.audiosocket_codec import AUDIO_FRAME_BYTES
from phonesys.ulaw import ulaw_to_slin

ELEVENLABS_URL = "https://api.elevenlabs.io"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# A 200 whose body is not audio (a JSON error envelope, an HTML error page
# from a proxy, a plain-text quota notice) would otherwise be run through the
# mu-law table and played down the phone as loud noise. Deliberately a
# reject-list rather than an allow-list of audio types: ElevenLabs has used
# audio/basic, audio/mpeg and application/octet-stream for different formats,
# and refusing an unfamiliar-but-valid audio type would break synthesis
# outright, which is worse than the failure being guarded against.
_NON_AUDIO_CONTENT_TYPES = ("application/json", "text/", "application/xml")


class TtsStreamError(RuntimeError):
    """ElevenLabs failed for a sentence."""


def split_sentences(text: str) -> list[str]:
    return [p for p in (s.strip() for s in _SENTENCE_END.split(text)) if p]


class SentenceBuffer:
    """Accumulates streamed deltas, releasing complete sentences."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        parts = _SENTENCE_END.split(self._buf)
        sentences = [p.strip() for p in parts[:-1] if p.strip()]
        remainder = parts[-1]
        # The buffer may end exactly on sentence-ending punctuation with no
        # trailing whitespace yet (the next delta hasn't arrived), in which
        # case the regex split above never fires. Treat that as complete too
        # rather than waiting on text that may never come -- UNLESS the
        # character right before the punctuation is a digit ("3."), since
        # that is far more likely to be a decimal/version number still being
        # typed ("3.5", "v1.2") than a finished sentence. In that case we
        # just wait for more text; a real sentence boundary later still gets
        # picked up by the regex split above once whitespace follows it.
        stripped = remainder.strip()
        if stripped and stripped[-1] in ".!?":
            preceding = stripped[-2] if len(stripped) >= 2 else ""
            if not preceding.isdigit():
                sentences.append(stripped)
                remainder = ""
        self._buf = remainder
        return sentences

    def flush(self) -> str:
        out, self._buf = self._buf.strip(), ""
        return out


def _frames_from_ulaw(ulaw: bytes, sentence_index: int) -> list[tuple[bytes, int]]:
    # Asterisk expects whole 20ms frames -- a trailing partial frame (at most
    # 20ms of audio) is intentionally dropped rather than padded or emitted
    # short.
    pcm = ulaw_to_slin(ulaw)
    return [
        (pcm[i:i + AUDIO_FRAME_BYTES], sentence_index)
        for i in range(0, len(pcm) - AUDIO_FRAME_BYTES + 1, AUDIO_FRAME_BYTES)
    ]


class StreamTts:
    def __init__(self, api_key: str, timeout: float = 30.0,
                 model_id: str = "eleven_turbo_v2_5") -> None:
        self._model_id = model_id
        self._client = httpx.AsyncClient(
            base_url=ELEVENLABS_URL,
            headers={"xi-api-key": api_key},
            timeout=timeout,
        )

    async def synthesize_frames(self, text: str, voice_id: str,
                                sentence_index: int) -> list[tuple[bytes, int]]:
        if not text.strip():
            return []
        try:
            resp = await self._client.post(
                f"/v1/text-to-speech/{voice_id}/stream",
                params={"output_format": "ulaw_8000"},
                json={"text": text, "model_id": self._model_id},
            )
        except httpx.RequestError as exc:
            # Network-level failures (timeouts, connection resets) are
            # exactly the kind most likely on a flaky link, and a caller
            # catching TtsStreamError should not have to also catch httpx's
            # exception hierarchy separately.
            raise TtsStreamError(f"elevenlabs request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TtsStreamError(f"elevenlabs failed: HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if any(content_type.startswith(bad) for bad in _NON_AUDIO_CONTENT_TYPES):
            # Never log resp.text: an error envelope can echo back request
            # fields, and this client is constructed with an API key.
            raise TtsStreamError(
                f"elevenlabs returned 200 with non-audio content-type "
                f"{content_type!r} ({len(resp.content)} bytes)"
            )
        return _frames_from_ulaw(resp.content, sentence_index)

    async def aclose(self) -> None:
        await self._client.aclose()
