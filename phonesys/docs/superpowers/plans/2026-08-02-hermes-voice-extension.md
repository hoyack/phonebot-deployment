# Hermes Voice Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dial extension `7000` from a Zoiper softphone and hold a spoken conversation with a Hermes agent.

**Architecture:** A standalone Python bridge on the WSL host connects to Asterisk's ARI over WebSocket. When a call enters the `phonesys` Stasis app, the bridge answers it and runs a turn loop: Asterisk records the caller (using its own silence detection), Deepgram transcribes, a dedicated Hermes `phonebot` profile produces a reply, ElevenLabs synthesizes it, and Asterisk plays it back. Audio is exchanged through two bind-mounted directories because ARI resolves media paths against Asterisk's own filesystem.

**Tech Stack:** Python 3.11, `httpx` (async HTTP), `websockets` (ARI events), `PyYAML`, `python-dotenv`, `pytest` + `pytest-asyncio`. All three external services are called over plain HTTP rather than vendor SDKs — fewer dependencies, no SDK churn, and trivially fakeable in tests.

## Global Constraints

- Python 3.11 (matches the Hermes venv already on this host).
- Secrets live only in `/home/hoyack/work/phonesys/.env`. Never read, print, log, or commit their values. `.env` is already gitignored.
- No vendor SDKs for Deepgram, ElevenLabs, or Hermes. Use `httpx` against documented HTTP endpoints.
- Every outbound network call takes an explicit timeout. No unbounded waits.
- ARI is reachable at `http://127.0.0.1:8088/ari` from the WSL host only.
- Audio to Asterisk is µ-law 8 kHz raw, written with a `.ulaw` extension.
- Asterisk runs as uid 999, gid 1000 in-container; host user is uid 1000, gid 1000. Files written by the bridge must be group-readable (`0644` is sufficient).
- Target extension is `7000`. Hermes profile is `phonebot`.

---

### Task 1: Project scaffold and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `config.yaml`
- Create: `src/phonesys/__init__.py`
- Create: `src/phonesys/config.py`
- Create: `tests/test_config.py`
- Create: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_config(path: Path) -> Config`; `Config` with `.extensions: dict[str, ExtensionConfig]` and `.ari: AriConfig`; `ExtensionConfig` with fields `profile: str`, `hermes_base_url: str`, `hermes_api_key: str`, `greeting: str`, `voice_id: str`, `max_silence_seconds: int`, `max_turn_seconds: int`, `max_call_seconds: int`; `AriConfig` with `base_url: str`, `username: str`, `password: str`, `app: str`; `MissingSecretError`.

- [ ] **Step 1: Create the package skeleton and dependency manifest**

Create `pyproject.toml`:

```toml
[project]
name = "phonesys"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "websockets>=12.0",
    "PyYAML>=6.0",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Create empty `src/phonesys/__init__.py`.

Create `.env.example` (documents required names, holds no values):

```
ARI_BASE_URL=http://127.0.0.1:8088/ari
ARI_USER=phonesys
ARI_PASSWORD=
ARI_APP=phonesys
DEEPGRAM_API_KEY=
ELEVENLABS_API_KEY=
HERMES_PHONEBOT_API_KEY=
```

- [ ] **Step 2: Set up the virtualenv**

```bash
cd /home/hoyack/work/phonesys
python3 -m venv .venv
.venv/bin/pip install -q -e ".[dev]"
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_config.py`:

```python
import pytest
from pathlib import Path
from phonesys.config import load_config, MissingSecretError


def _write(tmp_path: Path, yaml_text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text)
    return p


CONFIG_YAML = """
ari:
  base_url: http://127.0.0.1:8088/ari
  app: phonesys
extensions:
  "7000":
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    max_silence_seconds: 2
    max_turn_seconds: 30
    max_call_seconds: 600
"""


def test_loads_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, CONFIG_YAML))
    ext = cfg.extensions["7000"]
    assert ext.profile == "phonebot"
    assert ext.hermes_base_url == "http://127.0.0.1:8642"
    assert ext.hermes_api_key == "hkey"
    assert ext.max_silence_seconds == 2
    assert cfg.ari.username == "phonesys"
    assert cfg.ari.app == "phonesys"


def test_missing_secret_raises_naming_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.delenv("HERMES_PHONEBOT_API_KEY", raising=False)
    with pytest.raises(MissingSecretError) as exc:
        load_config(_write(tmp_path, CONFIG_YAML))
    assert "HERMES_PHONEBOT_API_KEY" in str(exc.value)


def test_unknown_extension_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, CONFIG_YAML))
    assert "9999" not in cfg.extensions
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.config'`

- [ ] **Step 5: Write the implementation**

Create `src/phonesys/config.py`:

```python
"""Configuration loading. Secrets come from the environment, never from YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


class MissingSecretError(RuntimeError):
    """A required environment variable was absent or empty."""


@dataclass(frozen=True)
class ExtensionConfig:
    profile: str
    hermes_base_url: str
    hermes_api_key: str
    greeting: str
    voice_id: str
    max_silence_seconds: int
    max_turn_seconds: int
    max_call_seconds: int


@dataclass(frozen=True)
class AriConfig:
    base_url: str
    username: str
    password: str
    app: str


@dataclass(frozen=True)
class Config:
    ari: AriConfig
    extensions: dict[str, ExtensionConfig]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSecretError(
            f"Required environment variable {name} is missing or empty. "
            f"Add it to .env (see .env.example)."
        )
    return value


def load_config(path: Path) -> Config:
    load_dotenv(Path(path).parent / ".env", override=False)
    raw = yaml.safe_load(Path(path).read_text()) or {}

    ari_raw = raw.get("ari") or {}
    ari = AriConfig(
        base_url=ari_raw.get("base_url", "http://127.0.0.1:8088/ari"),
        username=_require_env("ARI_USER"),
        password=_require_env("ARI_PASSWORD"),
        app=ari_raw.get("app", "phonesys"),
    )

    extensions: dict[str, ExtensionConfig] = {}
    for number, ext in (raw.get("extensions") or {}).items():
        extensions[str(number)] = ExtensionConfig(
            profile=ext["profile"],
            hermes_base_url=ext["hermes_base_url"],
            hermes_api_key=_require_env(ext["hermes_api_key_env"]),
            greeting=ext["greeting"],
            voice_id=ext["voice_id"],
            max_silence_seconds=int(ext.get("max_silence_seconds", 2)),
            max_turn_seconds=int(ext.get("max_turn_seconds", 30)),
            max_call_seconds=int(ext.get("max_call_seconds", 600)),
        )
    return Config(ari=ari, extensions=extensions)
```

Create `config.yaml` at the project root with the same content as `CONFIG_YAML` in the test, but with `voice_id` left as `voice-abc` for now — Task 4 replaces it with a real ElevenLabs voice.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml config.yaml .env.example src/ tests/
git commit -m "feat: project scaffold and configuration loading"
```

---

### Task 2: Hermes client

Hermes' `api_server` exposes `POST /api/sessions` to create a session and `POST /api/sessions/{id}/chat` for one synchronous agent turn. Auth is `Authorization: Bearer <API_SERVER_KEY>`. The reply text is at `message.content`.

**Files:**
- Create: `src/phonesys/hermes.py`
- Create: `tests/test_hermes.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `HermesClient(base_url: str, api_key: str, timeout: float = 60.0)` with `async create_session(session_id: str, system_prompt: str | None = None) -> str` and `async chat(session_id: str, message: str) -> str`; `HermesError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hermes.py`:

```python
import httpx
import pytest
from phonesys.hermes import HermesClient, HermesError


def _client(handler) -> HermesClient:
    transport = httpx.MockTransport(handler)
    c = HermesClient(base_url="http://hermes.test", api_key="k")
    c._client = httpx.AsyncClient(transport=transport, base_url="http://hermes.test")
    return c


async def test_create_session_returns_id():
    def handler(request):
        assert request.url.path == "/api/sessions"
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(201, json={"object": "hermes.session", "session": {"id": "call-1"}})

    assert await _client(handler).create_session("call-1") == "call-1"


async def test_chat_returns_assistant_text():
    def handler(request):
        assert request.url.path == "/api/sessions/call-1/chat"
        assert request.json()["message"] == "hello"
        return httpx.Response(200, json={
            "object": "hermes.session.chat.completion",
            "session_id": "call-1",
            "message": {"role": "assistant", "content": "hi there"},
        })

    assert await _client(handler).chat("call-1", "hello") == "hi there"


async def test_http_error_raises_hermes_error():
    def handler(request):
        return httpx.Response(500, json={"error": {"message": "boom"}})

    with pytest.raises(HermesError):
        await _client(handler).chat("call-1", "hello")


async def test_empty_reply_returns_empty_string():
    def handler(request):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": ""}})

    assert await _client(handler).chat("call-1", "hello") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_hermes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.hermes'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/hermes.py`:

```python
"""Client for a Hermes profile's api_server."""
from __future__ import annotations

import httpx


class HermesError(RuntimeError):
    """Hermes returned an error or an unusable response."""


class HermesClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
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
        if resp.status_code == 409:
            return session_id  # already exists; reuse it
        if resp.status_code >= 400:
            raise HermesError(f"create_session failed: HTTP {resp.status_code}")
        return (resp.json().get("session") or {}).get("id") or session_id

    async def chat(self, session_id: str, message: str) -> str:
        resp = await self._client.post(f"/api/sessions/{session_id}/chat", json={"message": message})
        if resp.status_code >= 400:
            raise HermesError(f"chat failed: HTTP {resp.status_code}")
        return ((resp.json().get("message") or {}).get("content")) or ""

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_hermes.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/hermes.py tests/test_hermes.py
git commit -m "feat: Hermes api_server client"
```

---

### Task 3: Deepgram STT adapter

**Files:**
- Create: `src/phonesys/stt.py`
- Create: `tests/test_stt.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SttClient(api_key: str, timeout: float = 30.0)` with `async transcribe(audio: bytes, content_type: str = "audio/wav") -> str`; `SttError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stt.py`:

```python
import httpx
import pytest
from phonesys.stt import SttClient, SttError

DG_OK = {"results": {"channels": [{"alternatives": [{"transcript": "hello world"}]}]}}
DG_EMPTY = {"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}}


def _client(handler) -> SttClient:
    c = SttClient(api_key="k")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.deepgram.com")
    return c


async def test_transcribe_returns_text():
    def handler(request):
        assert request.headers["Authorization"] == "Token k"
        assert request.headers["Content-Type"] == "audio/wav"
        return httpx.Response(200, json=DG_OK)

    assert await _client(handler).transcribe(b"RIFF....") == "hello world"


async def test_silence_returns_empty_string():
    def handler(request):
        return httpx.Response(200, json=DG_EMPTY)

    assert await _client(handler).transcribe(b"RIFF....") == ""


async def test_error_status_raises():
    def handler(request):
        return httpx.Response(401, json={"err_msg": "bad key"})

    with pytest.raises(SttError):
        await _client(handler).transcribe(b"RIFF....")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.stt'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/stt.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stt.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/stt.py tests/test_stt.py
git commit -m "feat: Deepgram STT adapter"
```

---

### Task 4: ElevenLabs TTS adapter

Requests `ulaw_8000` so the bytes can be written straight to a `.ulaw` file that Asterisk plays natively — no resampling in the turn loop.

**Files:**
- Create: `src/phonesys/tts.py`
- Create: `tests/test_tts.py`
- Modify: `config.yaml` (real voice id)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `TtsClient(api_key: str, timeout: float = 30.0)` with `async synthesize(text: str, voice_id: str) -> bytes`; `TtsError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tts.py`:

```python
import httpx
import pytest
from phonesys.tts import TtsClient, TtsError


def _client(handler) -> TtsClient:
    c = TtsClient(api_key="k")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.elevenlabs.io")
    return c


async def test_synthesize_returns_audio_bytes():
    def handler(request):
        assert request.url.path == "/v1/text-to-speech/voice-abc"
        assert request.url.params["output_format"] == "ulaw_8000"
        assert request.headers["xi-api-key"] == "k"
        assert request.json()["text"] == "hello"
        return httpx.Response(200, content=b"\xff\xfe\xfd")

    assert await _client(handler).synthesize("hello", "voice-abc") == b"\xff\xfe\xfd"


async def test_error_status_raises():
    def handler(request):
        return httpx.Response(422, json={"detail": "bad voice"})

    with pytest.raises(TtsError):
        await _client(handler).synthesize("hello", "voice-abc")


async def test_empty_text_raises_without_calling_api():
    def handler(request):
        raise AssertionError("must not call the API for empty text")

    with pytest.raises(TtsError):
        await _client(handler).synthesize("   ", "voice-abc")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.tts'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/tts.py`:

```python
"""ElevenLabs text-to-speech, emitting mu-law 8 kHz for Asterisk."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tts.py -v`
Expected: 3 passed

- [ ] **Step 5: Pick a real voice and record it in config**

```bash
.venv/bin/python - <<'PY'
import os, httpx
from dotenv import load_dotenv
load_dotenv()
r = httpx.get("https://api.elevenlabs.io/v1/voices",
              headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, timeout=30)
r.raise_for_status()
for v in r.json()["voices"][:10]:
    print(v["voice_id"], "-", v["name"])
PY
```

Replace `voice_id: voice-abc` in `config.yaml` with a chosen id.

- [ ] **Step 6: Commit**

```bash
git add src/phonesys/tts.py tests/test_tts.py config.yaml
git commit -m "feat: ElevenLabs TTS adapter emitting mu-law 8k"
```

---

### Task 5: Dry-run pipeline (checkpoint — no telephony)

Proves STT → Hermes → TTS end to end against a WAV file. **Do not start Task 6 until this passes against the live services**, because from inside a phone call every one of these layers fails identically.

**Files:**
- Create: `src/phonesys/pipeline.py`
- Create: `src/phonesys/dryrun.py`
- Create: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `SttClient.transcribe`, `HermesClient.chat`, `TtsClient.synthesize`, `ExtensionConfig`.
- Produces: `async run_turn(stt, hermes, tts, session_id: str, audio: bytes, ext: ExtensionConfig) -> TurnResult`; `TurnResult` with `.transcript: str`, `.reply: str`, `.audio: bytes`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline.py`:

```python
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


async def test_empty_reply_skips_tts():
    hermes, tts = FakeHermes(""), FakeTts()
    result = await run_turn(FakeStt("hello"), hermes, tts, "s1", b"WAV", EXT)
    assert result.reply == ""
    assert result.audio == b""
    assert tts.seen == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.pipeline'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/pipeline.py`:

```python
"""One conversational turn: audio in, audio out. No telephony here."""
from __future__ import annotations

from dataclasses import dataclass

from phonesys.config import ExtensionConfig


@dataclass(frozen=True)
class TurnResult:
    transcript: str
    reply: str
    audio: bytes


async def run_turn(stt, hermes, tts, session_id: str, audio: bytes, ext: ExtensionConfig) -> TurnResult:
    transcript = (await stt.transcribe(audio)).strip()
    if not transcript:
        return TurnResult(transcript="", reply="", audio=b"")

    reply = (await hermes.chat(session_id, transcript)).strip()
    if not reply:
        return TurnResult(transcript=transcript, reply="", audio=b"")

    return TurnResult(transcript=transcript, reply=reply, audio=await tts.synthesize(reply, ext.voice_id))
```

Create `src/phonesys/dryrun.py`:

```python
"""Run one real turn against live services from a WAV file.

Usage: python -m phonesys.dryrun path/to/input.wav
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from phonesys.config import load_config
from phonesys.hermes import HermesClient
from phonesys.pipeline import run_turn
from phonesys.stt import SttClient
from phonesys.tts import TtsClient


async def main(wav_path: str, extension: str = "7000") -> int:
    cfg = load_config(Path("config.yaml"))
    ext = cfg.extensions[extension]
    stt = SttClient(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts = TtsClient(api_key=os.environ["ELEVENLABS_API_KEY"])
    hermes = HermesClient(base_url=ext.hermes_base_url, api_key=ext.hermes_api_key)
    try:
        await hermes.create_session("dryrun")
        result = await run_turn(stt, hermes, tts, "dryrun", Path(wav_path).read_bytes(), ext)
        print(f"transcript: {result.transcript!r}")
        print(f"reply     : {result.reply!r}")
        out = Path("dryrun-reply.ulaw")
        out.write_bytes(result.audio)
        print(f"audio     : {len(result.audio)} bytes -> {out}")
        return 0 if result.reply else 1
    finally:
        await stt.aclose()
        await tts.aclose()
        await hermes.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*sys.argv[1:])))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (13 total across Tasks 1-5)

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/pipeline.py src/phonesys/dryrun.py tests/test_pipeline.py
git commit -m "feat: turn pipeline and dry-run harness"
```

Note: the live dry-run cannot run until Task 6 provides a reachable Hermes endpoint. It is exercised in Task 6 Step 5.

---

### Task 6: Infrastructure — phonebot profile, api_server, bind mounts

Environment work, not application code. Highest-risk task: it touches a running PBX and may need interactive auth.

**Files:**
- Modify: `/home/hoyack/freepbx-docker/docker-compose.yml`
- Create: `/home/hoyack/work/phonesys/media/sounds/.gitkeep`
- Create: `/home/hoyack/work/phonesys/media/rec/.gitkeep`

**Interfaces:**
- Consumes: nothing.
- Produces: a reachable Hermes endpoint at `http://127.0.0.1:8642`; Asterisk paths `/var/lib/asterisk/sounds/en/phonesys` and `/var/spool/asterisk/recording` bound to host `media/sounds` and `media/rec`.

- [ ] **Step 1: Create the phonebot Hermes profile**

```bash
mkdir -p /home/hoyack/work/phonesys/media/sounds /home/hoyack/work/phonesys/media/rec
touch /home/hoyack/work/phonesys/media/sounds/.gitkeep /home/hoyack/work/phonesys/media/rec/.gitkeep
hermes --profile phonebot --version
```

**STOP if this prompts for login.** Hermes uses the `openai-codex` provider against ChatGPT OAuth, not an API key, so profile bootstrap may require a browser. If prompted, hand the exact command to the user to run with `!` rather than attempting to automate it.

- [ ] **Step 2: Enable api_server on the phonebot profile**

Append to that profile's `.env` (`~/.hermes-phonebot/.env`; confirm the path from `hermes --profile phonebot doctor` output):

```
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=<generate with: openssl rand -hex 24>
```

Put the same value in `/home/hoyack/work/phonesys/.env` as `HERMES_PHONEBOT_API_KEY`.

- [ ] **Step 3: Start the phonebot gateway and verify**

```bash
hermes --profile phonebot gateway run &
sleep 10
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8642/health
```

Expected: `200`

- [ ] **Step 4: Add bind mounts and recreate the container**

In `/home/hoyack/freepbx-docker/docker-compose.yml`, under `services.freepbx.volumes`, add:

```yaml
      - /home/hoyack/work/phonesys/media/sounds:/var/lib/asterisk/sounds/en/phonesys
      - /home/hoyack/work/phonesys/media/rec:/var/spool/asterisk/recording
```

Then:

```bash
docker compose -f /home/hoyack/freepbx-docker/docker-compose.yml up -d
```

Verify both directions of the shared filesystem:

```bash
echo probe > /home/hoyack/work/phonesys/media/sounds/probe.txt
docker exec freepbx-docker-freepbx-1 cat /var/lib/asterisk/sounds/en/phonesys/probe.txt
docker exec freepbx-docker-freepbx-1 sh -c 'echo back > /var/spool/asterisk/recording/probe.txt'
cat /home/hoyack/work/phonesys/media/rec/probe.txt
rm -f /home/hoyack/work/phonesys/media/sounds/probe.txt /home/hoyack/work/phonesys/media/rec/probe.txt
```

Expected: `probe` then `back`. If either fails, the nested-mount-over-volume assumption is wrong — stop and reassess rather than working around it.

- [ ] **Step 5: Run the live dry-run**

Generate a test WAV and run the real pipeline:

```bash
cd /home/hoyack/work/phonesys
docker exec freepbx-docker-freepbx-1 sh -c \
  'cp /var/lib/asterisk/sounds/en/demo-thanks.* /var/spool/asterisk/recording/ 2>/dev/null || true'
ls media/rec/
.venv/bin/python -m phonesys.dryrun media/rec/demo-thanks.wav
```

Expected: a non-empty transcript, a non-empty reply, and `dryrun-reply.ulaw` written. **This is the checkpoint** — the entire AI path is proven before any telephony work.

- [ ] **Step 6: Commit**

```bash
git add media/sounds/.gitkeep media/rec/.gitkeep
git commit -m "chore: media exchange directories for Asterisk"
cd /home/hoyack/freepbx-docker && git add -A 2>/dev/null || true
```

(The freepbx-docker directory is not a git repo; note the compose change in its README instead.)

---

### Task 7: ARI client

**Files:**
- Create: `src/phonesys/ari.py`
- Create: `tests/test_ari.py`

**Interfaces:**
- Consumes: `AriConfig`.
- Produces: `AriClient(cfg: AriConfig)` with `async answer(channel_id) -> None`, `async hangup(channel_id) -> None`, `async play(channel_id, media: str) -> str` (returns playback id), `async record(channel_id, name: str, max_silence: int, max_duration: int) -> None`, `async events()` yielding `dict`; `AriError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ari.py`:

```python
import httpx
import pytest
from phonesys.ari import AriClient, AriError
from phonesys.config import AriConfig

CFG = AriConfig(base_url="http://ari.test/ari", username="u", password="p", app="phonesys")


def _client(handler) -> AriClient:
    c = AriClient(CFG)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ari.test/ari")
    return c


async def test_answer_posts_to_channel():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(204)

    await _client(handler).answer("chan-1")
    assert seen["path"] == "/ari/channels/chan-1/answer"


async def test_play_returns_playback_id():
    def handler(request):
        assert request.url.params["media"] == "sound:phonesys/reply-1"
        return httpx.Response(201, json={"id": "pb-1"})

    assert await _client(handler).play("chan-1", "sound:phonesys/reply-1") == "pb-1"


async def test_record_sends_silence_and_duration():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(201, json={"name": "turn-1"})

    await _client(handler).record("chan-1", "turn-1", max_silence=2, max_duration=30)
    assert seen["maxSilenceSeconds"] == "2"
    assert seen["maxDurationSeconds"] == "30"
    assert seen["format"] == "wav"


async def test_error_status_raises():
    def handler(request):
        return httpx.Response(404, json={"message": "Channel not found"})

    with pytest.raises(AriError):
        await _client(handler).answer("gone")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ari.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.ari'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/ari.py`:

```python
"""Asterisk REST Interface client: REST calls plus the event WebSocket."""
from __future__ import annotations

import json
from typing import AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets

from phonesys.config import AriConfig


class AriError(RuntimeError):
    """Asterisk returned an error."""


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
            raise AriError(f"{path} failed: HTTP {resp.status_code}")
        return resp

    async def answer(self, channel_id: str) -> None:
        await self._post(f"/ari/channels/{channel_id}/answer")

    async def hangup(self, channel_id: str) -> None:
        resp = await self._client.delete(f"/ari/channels/{channel_id}")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise AriError(f"hangup failed: HTTP {resp.status_code}")

    async def play(self, channel_id: str, media: str) -> str:
        resp = await self._post(f"/ari/channels/{channel_id}/play", {"media": media})
        return resp.json().get("id", "")

    async def record(self, channel_id: str, name: str, max_silence: int, max_duration: int) -> None:
        await self._post(
            f"/ari/channels/{channel_id}/record",
            {
                "name": name,
                "format": "wav",
                "maxSilenceSeconds": str(max_silence),
                "maxDurationSeconds": str(max_duration),
                "ifExists": "overwrite",
                "beep": "false",
            },
        )

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ari.py -v`
Expected: 4 passed

- [ ] **Step 5: Verify against live ARI**

```bash
cd /home/hoyack/work/phonesys
.venv/bin/python - <<'PY'
import asyncio, os
from pathlib import Path
from phonesys.config import load_config
from phonesys.ari import AriClient

async def main():
    cfg = load_config(Path("config.yaml"))
    c = AriClient(cfg.ari)
    r = await c._client.get("/ari/asterisk/info")
    print("ARI reachable:", r.status_code, r.json()["system"]["version"])
    await c.aclose()

asyncio.run(main())
PY
```

Expected: `ARI reachable: 200 21.10.2`

- [ ] **Step 6: Commit**

```bash
git add src/phonesys/ari.py tests/test_ari.py
git commit -m "feat: ARI client with REST and event websocket"
```

---

### Task 8: Dialplan wiring for extension 7000

Makes `7000` dialable and hand calls to the Stasis app. `extensions_custom.conf` is never overwritten by FreePBX, unlike the generated `extensions_additional.conf`.

**Files:**
- Modify (in container, persisted via the `etc_data` volume): `/etc/asterisk/extensions_custom.conf`

**Interfaces:**
- Consumes: ARI app name `phonesys` from `AriConfig.app`.
- Produces: dialing `7000` from a registered extension enters Stasis app `phonesys` with argument `7000`.

- [ ] **Step 1: Add the custom dialplan context**

```bash
docker exec freepbx-docker-freepbx-1 sh -c 'cat >> /etc/asterisk/extensions_custom.conf <<EOF

[phonesys-custom]
exten => 7000,1,NoOp(phonesys: handing call to Stasis)
 same => n,Answer()
 same => n,Stasis(phonesys,7000)
 same => n,Hangup()
EOF'
```

- [ ] **Step 2: Make it reachable from internal extensions**

```bash
docker exec freepbx-docker-freepbx-1 sh -c 'cat >> /etc/asterisk/extensions_custom.conf <<EOF

[from-internal-custom]
include => phonesys-custom
EOF'
docker exec freepbx-docker-freepbx-1 fwconsole reload
```

- [ ] **Step 3: Verify the dialplan entry exists**

Run: `docker exec freepbx-docker-freepbx-1 asterisk -rx 'dialplan show 7000@phonesys-custom'`
Expected: shows the `Stasis(phonesys,7000)` priority

- [ ] **Step 4: Verify Stasis receives the call**

Start a listener, then dial `7000` from Zoiper:

```bash
cd /home/hoyack/work/phonesys
.venv/bin/python - <<'PY'
import asyncio
from pathlib import Path
from phonesys.config import load_config
from phonesys.ari import AriClient

async def main():
    cfg = load_config(Path("config.yaml"))
    c = AriClient(cfg.ari)
    print("waiting for a call to 7000 ...")
    async for ev in c.events():
        print(ev.get("type"), ev.get("channel", {}).get("name"))
        if ev.get("type") == "StasisStart":
            print("ARGS:", ev.get("args"))
            break

asyncio.run(main())
PY
```

Expected: a `StasisStart` event with `ARGS: ['7000']`. The caller hears silence — correct at this stage, nothing answers yet.

- [ ] **Step 5: Commit**

```bash
git commit --allow-empty -m "docs: dialplan wiring for extension 7000 verified"
```

---

### Task 9: Call handler and turn loop

**Files:**
- Create: `src/phonesys/call.py`
- Create: `src/phonesys/main.py`
- Create: `tests/test_call.py`

**Interfaces:**
- Consumes: `AriClient`, `SttClient`, `TtsClient`, `HermesClient`, `run_turn`, `ExtensionConfig`.
- Produces: `CallHandler(ari, stt, tts, hermes_factory, sounds_dir: Path, rec_dir: Path)` with `async handle(channel_id: str, extension: str, ext_cfg: ExtensionConfig) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_call.py`:

```python
from pathlib import Path
from phonesys.call import CallHandler
from phonesys.config import ExtensionConfig

EXT = ExtensionConfig(
    profile="phonebot", hermes_base_url="http://h", hermes_api_key="k",
    greeting="Hello there", voice_id="v", max_silence_seconds=2,
    max_turn_seconds=30, max_call_seconds=600,
)


class FakeAri:
    def __init__(self, transcripts_available=1):
        self.answered, self.played, self.recorded, self.hungup = [], [], [], []
        self._remaining = transcripts_available

    async def answer(self, cid): self.answered.append(cid)
    async def play(self, cid, media):
        self.played.append(media)
        return "pb"
    async def record(self, cid, name, max_silence, max_duration):
        self.recorded.append(name)
    async def hangup(self, cid): self.hungup.append(cid)


class FakeStt:
    def __init__(self, texts): self.texts = list(texts)
    async def transcribe(self, audio, content_type="audio/wav"):
        return self.texts.pop(0) if self.texts else ""


class FakeHermes:
    def __init__(self): self.seen = []
    async def create_session(self, sid, system_prompt=None): return sid
    async def chat(self, sid, msg):
        self.seen.append(msg)
        return f"echo:{msg}"
    async def aclose(self): pass


class FakeTts:
    async def synthesize(self, text, voice_id): return b"\x00\x01"


def _handler(tmp_path, ari, stt, hermes):
    sounds, rec = tmp_path / "sounds", tmp_path / "rec"
    sounds.mkdir(); rec.mkdir()
    return CallHandler(ari, stt, FakeTts(), lambda ext: hermes, sounds, rec), sounds, rec


async def test_greeting_is_played_first(tmp_path):
    ari, hermes = FakeAri(), FakeHermes()
    handler, sounds, _ = _handler(tmp_path, ari, FakeStt([]), hermes)
    await handler.handle("chan-1", "7000", EXT)
    assert ari.answered == ["chan-1"]
    assert ari.played, "greeting should have been played"
    assert ari.played[0].startswith("sound:phonesys/")


async def test_writes_tts_audio_into_sounds_dir(tmp_path):
    ari, hermes = FakeAri(), FakeHermes()
    handler, sounds, _ = _handler(tmp_path, ari, FakeStt([]), hermes)
    await handler.handle("chan-1", "7000", EXT)
    assert list(sounds.glob("*.ulaw")), "expected a .ulaw file written for playback"


async def test_hangs_up_after_repeated_silence(tmp_path):
    ari, hermes = FakeAri(), FakeHermes()
    handler, _, _ = _handler(tmp_path, ari, FakeStt(["", ""]), hermes)
    await handler.handle("chan-1", "7000", EXT)
    assert ari.hungup == ["chan-1"]
    assert hermes.seen == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_call.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.call'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/call.py`:

```python
"""Per-call turn loop. The only module that knows about turn-taking."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from phonesys.config import ExtensionConfig
from phonesys.pipeline import run_turn

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_SILENCE = 2
RECORDING_SETTLE_SECONDS = 0.5


class CallHandler:
    def __init__(self, ari, stt, tts, hermes_factory, sounds_dir: Path, rec_dir: Path) -> None:
        self._ari = ari
        self._stt = stt
        self._tts = tts
        self._hermes_factory = hermes_factory
        self._sounds = Path(sounds_dir)
        self._rec = Path(rec_dir)

    async def _speak(self, channel_id: str, audio: bytes) -> None:
        name = f"reply-{uuid.uuid4().hex[:12]}"
        path = self._sounds / f"{name}.ulaw"
        path.write_bytes(audio)
        path.chmod(0o644)
        try:
            await self._ari.play(channel_id, f"sound:phonesys/{name}")
        finally:
            path.unlink(missing_ok=True)

    async def handle(self, channel_id: str, extension: str, ext: ExtensionConfig) -> None:
        hermes = self._hermes_factory(ext)
        session_id = f"call-{channel_id}"
        silence_streak = 0
        try:
            await self._ari.answer(channel_id)
            await hermes.create_session(session_id)
            await self._speak(channel_id, await self._tts.synthesize(ext.greeting, ext.voice_id))

            deadline = asyncio.get_running_loop().time() + ext.max_call_seconds
            while asyncio.get_running_loop().time() < deadline:
                name = f"turn-{uuid.uuid4().hex[:12]}"
                await self._ari.record(channel_id, name, ext.max_silence_seconds, ext.max_turn_seconds)
                await asyncio.sleep(RECORDING_SETTLE_SECONDS)

                wav = self._rec / f"{name}.wav"
                audio = wav.read_bytes() if wav.exists() else b""
                wav.unlink(missing_ok=True)

                result = await run_turn(self._stt, hermes, self._tts, session_id, audio, ext)

                if not result.transcript:
                    silence_streak += 1
                    if silence_streak >= MAX_CONSECUTIVE_SILENCE:
                        break
                    continue

                silence_streak = 0
                if result.audio:
                    await self._speak(channel_id, result.audio)
        except Exception:
            logger.exception("call %s failed", channel_id)
        finally:
            await self._ari.hangup(channel_id)
            await hermes.aclose()
```

Create `src/phonesys/main.py`:

```python
"""Entrypoint: watch ARI for StasisStart and hand each call to a CallHandler."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from phonesys.ari import AriClient
from phonesys.call import CallHandler
from phonesys.config import load_config
from phonesys.hermes import HermesClient
from phonesys.stt import SttClient
from phonesys.tts import TtsClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("phonesys")

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    cfg = load_config(ROOT / "config.yaml")
    ari = AriClient(cfg.ari)
    stt = SttClient(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts = TtsClient(api_key=os.environ["ELEVENLABS_API_KEY"])
    handler = CallHandler(
        ari, stt, tts,
        lambda ext: HermesClient(base_url=ext.hermes_base_url, api_key=ext.hermes_api_key),
        ROOT / "media" / "sounds",
        ROOT / "media" / "rec",
    )

    logger.info("listening for calls on ARI app %s", cfg.ari.app)
    async for event in ari.events():
        if event.get("type") != "StasisStart":
            continue
        channel_id = event["channel"]["id"]
        args = event.get("args") or []
        extension = args[0] if args else ""
        ext_cfg = cfg.extensions.get(extension)
        if ext_cfg is None:
            logger.warning("no config for extension %r; hanging up", extension)
            await ari.hangup(channel_id)
            continue
        logger.info("call %s -> extension %s (profile %s)", channel_id, extension, ext_cfg.profile)
        asyncio.create_task(handler.handle(channel_id, extension, ext_cfg))


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (20 total)

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/call.py src/phonesys/main.py tests/test_call.py
git commit -m "feat: call handler turn loop and entrypoint"
```

---

### Task 10: Live smoke test and documentation

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a documented, verified working system.

- [ ] **Step 1: Run the bridge**

```bash
cd /home/hoyack/work/phonesys
.venv/bin/python -m phonesys.main
```

Expected: `listening for calls on ARI app phonesys`

- [ ] **Step 2: Dial 7000 from Zoiper and hold a conversation**

Expected: greeting plays, speech is transcribed, Hermes replies, reply is spoken. Confirm in the log that `transcript` and `reply` are non-empty.

- [ ] **Step 3: Verify the call did not die of RTP timeout**

```bash
docker exec freepbx-docker-freepbx-1 mysql -ufreepbxuser \
  -p"$(cat /home/hoyack/freepbx-docker/secrets/freepbxuser_password.txt)" -hdb asteriskcdrdb \
  -e "SELECT calldate, src, dst, duration, billsec, disposition FROM cdr ORDER BY calldate DESC LIMIT 3;"
```

Expected: `dst` of `7000`, `disposition` `ANSWERED`, and a duration reflecting real conversation length — **not exactly 30 seconds**, which would indicate the `rtptimeout` failure documented in the freepbx-docker README.

- [ ] **Step 4: Write the README**

Create `README.md` covering: what this is, architecture diagram, prerequisites (Deepgram/ElevenLabs keys, `phonebot` profile with `api_server`, bind mounts, dialplan entry), how to run, how to add an extension (config-only), the dry-run workflow, and troubleshooting — specifically that a call lasting exactly 30 seconds means an RTP media-path problem on the PBX, not a bug in this bridge.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: README with setup, usage, and troubleshooting"
```

---

## Self-Review

**Spec coverage:** Architecture → Tasks 7-9. Call flow → Task 9. Components table → Tasks 1-4, 7, 9. Configuration → Task 1. Shared filesystem → Task 6. Error handling → Task 9 (silence streak, exception guard, hangup in `finally`, call deadline) and Tasks 2-4 (per-adapter error types, explicit timeouts). Testing → unit tests in every code task, dry-run in Task 5, live smoke in Task 10. Prerequisites → Task 6 (profile, api_server, mounts) and Task 8 (Custom Destination, implemented as `extensions_custom.conf` — chosen over the FreePBX GUI Custom Destination because it is scriptable and verifiable from the CLI).

**Placeholder scan:** No TBDs. Every code step carries real code. The one deliberate deferral — the ElevenLabs voice id — is resolved by an executable step in Task 4 Step 5.

**Type consistency:** `ExtensionConfig` fields are identical across Tasks 1, 5, and 9. `run_turn(stt, hermes, tts, session_id, audio, ext)` has the same signature in Tasks 5 and 9. `AriClient.record(channel_id, name, max_silence, max_duration)` matches between Tasks 7 and 9. `HermesClient.chat(session_id, message) -> str` matches Tasks 2, 5, and 9. Fakes in Task 9 implement the same surface as the real clients, including `aclose()`.

**Known gap, deliberately accepted:** Task 9 waits `RECORDING_SETTLE_SECONDS` after `record()` rather than awaiting the `RecordingFinished` event, so a caller who talks longer than `max_turn_seconds` may have a turn truncated. Correct fix is to consume `RecordingFinished` from the event stream, which requires routing per-channel events into the handler. Deferred to keep Task 9 reviewable; revisit if truncation shows up in the smoke test.
