"""Live test: Hermes is up and a session can be created and closed.

COST / SAFETY: this test is free and places no phone call. It only talks to
the local, self-hosted Hermes gateway (health check + POST /api/sessions);
it never touches Deepgram or ElevenLabs and never invokes a chat turn (which
would exercise whatever LLM backs the Hermes profile). If a future live test
in this directory calls HermesClient.chat / chat_stream, or anything ARI
that answers or bridges a real channel, or Deepgram/ElevenLabs directly,
its docstring must say so plainly -- this one doesn't need to, because it
doesn't.

Run with: .venv/bin/pytest -m live
"""
from __future__ import annotations

import uuid

import pytest

from phonesys.hermes import HermesClient

pytestmark = pytest.mark.live


async def test_hermes_session_create_and_close(hermes_extension):
    client = HermesClient(
        base_url=hermes_extension.hermes_base_url,
        api_key=hermes_extension.hermes_api_key,
    )
    session_id = f"phonesys-live-test-{uuid.uuid4().hex[:8]}"
    try:
        returned_id = await client.create_session(session_id)
        assert returned_id  # Hermes echoed back a usable session id
    finally:
        await client.aclose()
