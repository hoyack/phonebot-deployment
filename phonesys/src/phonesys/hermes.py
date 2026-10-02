"""Client for a Hermes profile's api_server.

Shapes verified against gateway/platforms/api_server.py in the local Hermes
install: POST /api/sessions creates a session (409 when it already exists),
POST /api/sessions/{id}/chat runs one synchronous agent turn and returns the
reply at message.content. Auth is a bearer token (API_SERVER_KEY).
"""
from __future__ import annotations

import contextlib
import json
import logging

from phonesys.native_work import WorkCall, WorkError
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(frozen=True)
class HermesEvent:
    """Safe public stream surface: never retain raw tool payloads."""
    kind: str
    text: str = ""


def _safe_tool_text(kind: str, name: object) -> str:
    if kind == "tool.failed":
        return "One operation failed. I'm checking whether I can continue."
    if kind == "tool.completed":
        return "An operation has finished. I'm still working on your request."
    if name in ("web_search", "web_extract", "firecrawl_search", "firecrawl_scrape"):
        return "I'm searching for information."
    return "I'm running an operation for your request."

import httpx


class HermesError(RuntimeError):
    """Hermes returned an error or an unusable response."""


class HermesClient:
    # 30s, not the 60s this shipped with. Measured Hermes generation is 2-11s,
    # so anything beyond ~30s is a stall, not a slow answer -- and the caller
    # is sitting on an open line hearing nothing while we wait. A live call
    # proved the cost: one /chat never answered, httpx raised ReadTimeout 64s
    # later, and the caller heard 64s of silence before the call died.
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 *, native_work=None) -> None:
        self._work_settings = native_work
        self._work_key = getattr(native_work, "api_key", "") or api_key
        self._work = None
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def create_session(self, session_id: str, system_prompt: str | None = None) -> str:
        body: dict[str, object] = {"id": session_id}
        if system_prompt:
            body["system_prompt"] = system_prompt
        resp = await self._client.post("/api/sessions", json=body)
        if resp.status_code >= 400 and resp.status_code != 409:
            raise HermesError(f"create_session failed: HTTP {resp.status_code}")
        # This is called only after successful PIN in the streaming bridge. Native
        # work follows the CALL lifetime, never a chat stream/foreground turn.
        if self._work_settings and self._work is None:
            settings = self._work_settings
            work = WorkCall(session_id, settings.base_url, self._work_key,
                            settings.directory, settings.worker_config)
            try:
                await work.start()
            except (WorkError, OSError):
                await work.runs.client.aclose()
                logging.getLogger(__name__).warning("Native phone work unavailable (policy or transport)")
            else:
                self._work = work
        if resp.status_code == 409:
            return session_id
        return (resp.json().get("session") or {}).get("id") or session_id

    async def chat(self, session_id: str, message: str) -> str:
        resp = await self._client.post(
            f"/api/sessions/{session_id}/chat", json={"message": message}
        )
        if resp.status_code >= 400:
            raise HermesError(f"chat failed: HTTP {resp.status_code}")
        return ((resp.json().get("message") or {}).get("content")) or ""

    async def chat_stream(self, session_id: str, message: str) -> AsyncIterator[str]:
        """Compatibility text-only surface; reasoning is never reply text."""
        async with contextlib.aclosing(self.chat_events(session_id, message)) as events:
            async for event in events:
                if event.kind == "assistant.delta":
                    yield event.text

    async def chat_events(self, session_id: str, message: str) -> AsyncIterator[HermesEvent]:
        """Yield assistant text and sanitized tool lifecycle metadata only."""
        async with self._client.stream(
            "POST", f"/api/sessions/{session_id}/chat/stream",
            json={"message": message},
        ) as resp:
            if resp.status_code >= 400:
                raise HermesError(f"chat_stream failed: HTTP {resp.status_code}")
            event = None
            async for line in resp.aiter_lines():
                if not line:
                    event = None
                elif line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    try:
                        payload = json.loads(line.split(":", 1)[1].strip())
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if event == "assistant.delta":
                        delta = payload.get("delta")
                        if isinstance(delta, str) and delta:
                            yield HermesEvent(event, delta)
                    elif event in ("tool.started", "tool.completed", "tool.failed"):
                        yield HermesEvent(event, _safe_tool_text(event, payload.get("tool_name")))
                    elif event in ("error", "run.failed", "run.cancelled"):
                        raise HermesError("Hermes stream failed before completion")
                    elif event == "done":
                        yield HermesEvent("done")

    async def append_assistant(self, session_id: str, text: str) -> None:
        """Record what the caller actually heard after an interruption."""
        resp = await self._client.post(
            f"/api/sessions/{session_id}/chat",
            json={"message": f"[system] Your previous reply was interrupted. "
                             f"The caller only heard: {text}"},
        )
        if resp.status_code >= 400:
            raise HermesError(f"append_assistant failed: HTTP {resp.status_code}")

    def work_statuses(self) -> dict[str, str]:
        """Status-only snapshot for local audio notices; never polls the API."""
        return self._work.statuses() if self._work else {}

    def work_waiting_keys(self) -> set[str]:
        """No I/O: bounded work wait, separate from terminal confirmation."""
        return self._work.waiting_keys() if self._work else set()

    async def aclose(self) -> None:
        try:
            if self._work:
                await self._work.close()
        finally:
            await self._client.aclose()
