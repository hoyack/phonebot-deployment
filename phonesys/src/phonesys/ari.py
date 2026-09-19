"""Asterisk REST Interface client: REST calls plus the event WebSocket."""
from __future__ import annotations

import json
from typing import AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets

from phonesys.config import AriConfig


class AriError(RuntimeError):
    """Asterisk returned an error.

    Carries the HTTP status so callers can distinguish an ordinary hangup
    (404 -- the channel is gone) from a real fault.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AriClient:
    def __init__(self, cfg: AriConfig, timeout: float = 15.0) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            auth=(cfg.username, cfg.password),
            timeout=timeout,
        )

    async def _post(self, path: str, params: dict | None = None) -> httpx.Response:
        resp = await self._client.post(path, params=params or {})
        if resp.status_code >= 400:
            raise AriError(f"{path} failed: HTTP {resp.status_code}", resp.status_code)
        return resp

    async def start_moh(self, channel_id: str, moh_class: str) -> None:
        """Play only to the caller, never into the AudioSocket bridge leg."""
        await self._post(f"/channels/{channel_id}/moh", {"mohClass": moh_class})

    async def stop_moh(self, channel_id: str) -> None:
        resp = await self._client.delete(f"/channels/{channel_id}/moh")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise AriError(f"stop_moh failed: HTTP {resp.status_code}", resp.status_code)

    async def answer(self, channel_id: str) -> None:
        await self._post(f"/channels/{channel_id}/answer")

    async def hangup(self, channel_id: str) -> None:
        """Hang up. A 404 is fine -- the caller may have hung up first."""
        resp = await self._client.delete(f"/channels/{channel_id}")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise AriError(f"hangup failed: HTTP {resp.status_code}")

    async def play(self, channel_id: str, media: str, playback_id: str | None = None) -> str:
        """Start playback. Returns as soon as playback BEGINS.

        playback_id is supplied by the caller rather than left to Asterisk so
        that a PlaybackFinished waiter can be registered before this call is
        made; a short prompt can otherwise finish before the id is known and
        the event is dropped.
        """
        params = {"media": media}
        if playback_id is not None:
            params["playbackId"] = playback_id
        resp = await self._post(f"/channels/{channel_id}/play", params)
        return resp.json().get("id", "")

    async def record(self, channel_id: str, name: str, max_silence: int, max_duration: int) -> None:
        """Record the caller. Asterisk itself detects end-of-speech via
        maxSilenceSeconds, so the bridge needs no VAD of its own."""
        await self._post(
            f"/channels/{channel_id}/record",
            {
                "name": name,
                "format": "wav",
                "maxSilenceSeconds": str(max_silence),
                "maxDurationSeconds": str(max_duration),
                "ifExists": "overwrite",
                "beep": "false",
            },
        )

    async def create_bridge(self) -> str:
        resp = await self._post("/bridges", {"type": "mixing"})
        return resp.json()["id"]

    async def add_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        await self._post(f"/bridges/{bridge_id}/addChannel", {"channel": channel_id})

    async def destroy_bridge(self, bridge_id: str) -> None:
        resp = await self._client.delete(f"/bridges/{bridge_id}")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise AriError(f"destroy_bridge failed: HTTP {resp.status_code}", resp.status_code)

    async def originate_audiosocket(
        self,
        host: str,
        port: int,
        uuid: str,
        app: str,
        app_args: str | None = None,
    ) -> str:
        """Originate an AudioSocket channel.

        formats=slin is mandatory. The parameter is plural: 'format' belongs to
        externalMedia, is ignored here, and the channel driver then negotiates
        its highest capability (slin192 -- 192kHz, 7680-byte frames).
        """
        params = {
            "endpoint": f"AudioSocket/{host}:{port}/{uuid}",
            "app": app,
            "formats": "slin",
        }
        if app_args is not None:
            params["appArgs"] = app_args
        resp = await self._post("/channels", params)
        return resp.json()["id"]

    async def events(self) -> AsyncIterator[dict]:
        base = self._cfg.base_url.replace("http://", "ws://").replace("https://", "wss://")
        query = urlencode({
            "app": self._cfg.app,
            "api_key": f"{self._cfg.username}:{self._cfg.password}",
            "subscribeAll": "true",
        })
        async with websockets.connect(f"{base}/events?{query}") as ws:
            async for raw in ws:
                yield json.loads(raw)

    async def aclose(self) -> None:
        await self._client.aclose()
