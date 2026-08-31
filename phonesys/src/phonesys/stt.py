"""Deepgram speech-to-text over the prerecorded HTTP API."""
from __future__ import annotations

import httpx

DEEPGRAM_URL = "https://api.deepgram.com"


class SttError(RuntimeError):
    """Deepgram returned an error."""


class SttClient:
    def __init__(self, api_key: str, timeout: float = 30.0, model: str = "nova-2-phonecall") -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=DEEPGRAM_URL,
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout,
        )

    async def transcribe(self, audio: bytes, content_type: str = "audio/wav") -> str:
        """Return the transcript, or "" when the caller said nothing."""
        resp = await self._client.post(
            "/v1/listen",
            params={"model": self._model, "smart_format": "true", "punctuate": "true"},
            content=audio,
            headers={"Content-Type": content_type},
        )
        if resp.status_code >= 400:
            raise SttError(f"deepgram failed: HTTP {resp.status_code}")
        channels = (resp.json().get("results") or {}).get("channels") or []
        if not channels:
            return ""
        alternatives = channels[0].get("alternatives") or []
        if not alternatives:
            return ""
        return (alternatives[0].get("transcript") or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()
