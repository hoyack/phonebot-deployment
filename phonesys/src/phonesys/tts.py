"""ElevenLabs text-to-speech, emitting mu-law 8 kHz for Asterisk.

ulaw_8000 is requested so the response bytes can be written straight to a
.ulaw file that Asterisk plays natively -- no resampling in the turn loop.
"""
from __future__ import annotations

import httpx

ELEVENLABS_URL = "https://api.elevenlabs.io"
OUTPUT_FORMAT = "ulaw_8000"


class TtsError(RuntimeError):
    """ElevenLabs returned an error, or the request was unusable."""


class TtsClient:
    def __init__(self, api_key: str, timeout: float = 30.0, model_id: str = "eleven_turbo_v2_5") -> None:
        self._model_id = model_id
        self._client = httpx.AsyncClient(
            base_url=ELEVENLABS_URL,
            headers={"xi-api-key": api_key},
            timeout=timeout,
        )

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        if not text or not text.strip():
            raise TtsError("refusing to synthesize empty text")
        resp = await self._client.post(
            f"/v1/text-to-speech/{voice_id}",
            params={"output_format": OUTPUT_FORMAT},
            json={"text": text, "model_id": self._model_id},
        )
        if resp.status_code >= 400:
            raise TtsError(f"elevenlabs failed: HTTP {resp.status_code}")
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()
