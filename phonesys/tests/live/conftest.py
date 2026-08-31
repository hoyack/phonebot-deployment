"""Fixtures for the live test tier.

Everything under tests/live/ is gated behind the ``live`` marker (registered
in pyproject.toml) and is deselected by default -- see the ``-m "not live"``
addopts entry there. These tests talk to real infrastructure: Asterisk ARI,
the Hermes gateway, and -- if an individual test's docstring says so -- paid
providers like Deepgram or ElevenLabs. None of that may ever land in the
same run as extension 7000's suite: a provider outage must never be able to
turn that suite red.

Run the live tier explicitly:

    .venv/bin/pytest -m live

If a required service is not reachable, tests SKIP -- they never fail and
never silently pass. The skip reason names exactly which service is down
and what to start. Keep that message loud: a tier that skips quietly rots
into an E2E suite that always looks green without ever actually running,
which is the exact failure this tier exists to prevent.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from phonesys.config import Config, load_config

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

# The live tier exercises the streaming extension: it is the one with a
# dedicated audio pipeline and is the branch this work is happening on.
LIVE_EXTENSION = "7001"


async def _service_up(url: str, timeout: float = 2.0) -> bool:
    """True if *anything* answered at url -- even an HTTP error status.

    A 401/404/etc. means the TCP+HTTP stack is alive and answering, which is
    all "reachable" needs to mean here; auth and correctness are the live
    test's job, not the gate's. Only a connection failure (refused, no
    route, DNS, timeout) counts as "down".
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(url)
        return True
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def live_config() -> Config:
    """Load config.yaml, skipping loudly if it can't be loaded at all.

    This can only fail before any reachability check runs -- e.g. a required
    secret env var (see config.py's _require_env) is missing from .env.
    """
    try:
        return load_config(CONFIG_PATH)
    except Exception as exc:
        pytest.skip(
            f"live tier SKIPPED -- could not load {CONFIG_PATH}: {exc}. "
            f"Populate .env with the secrets config.yaml references "
            f"(see .env.example) to run live tests."
        )


@pytest.fixture(scope="session")
def hermes_extension(live_config: Config):
    """The streaming extension's config -- source of hermes_base_url."""
    return live_config.extensions[LIVE_EXTENSION]


@pytest.fixture(scope="session", autouse=True)
async def _require_live_services(live_config: Config, hermes_extension) -> None:
    """Gate the whole live tier on real infrastructure being reachable.

    Session-scoped and autouse: checked once per run, not once per test, so
    a down service produces one clear skip instead of N slow timeouts. Any
    test added under tests/live/ automatically inherits this gate just by
    living in this directory.
    """
    ari_url = live_config.ari.base_url
    hermes_url = f"{hermes_extension.hermes_base_url.rstrip('/')}/health"

    down: list[str] = []
    if not await _service_up(ari_url):
        down.append(
            f"Asterisk ARI ({ari_url}) is unreachable -- "
            f"start Asterisk/FreePBX and confirm the ARI app is enabled."
        )
    if not await _service_up(hermes_url):
        down.append(
            f"Hermes ({hermes_url}) is unreachable -- "
            f"start the Hermes gateway (api_server) locally on that port."
        )

    if down:
        pytest.skip(
            "live tier SKIPPED -- required service(s) unreachable:\n  "
            + "\n  ".join(down)
        )
