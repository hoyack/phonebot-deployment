"""Deepgram live transcription over WebSocket.

Audio is slin 8kHz mono straight off AudioSocket, which Deepgram accepts
natively as encoding=linear16&sample_rate=8000 -- no conversion needed.

What actually drives barge-in is INTERIM TRANSCRIPTS sustained over
barge_in_confirm_ms of wall clock (see stream_call.SpeechBurst).
SpeechStarted (vad_events=true) only RESETS that window: Deepgram does not
reliably emit one per utterance, so it cannot be the trigger. UtteranceEnd
(utterance_end_ms) is the separate signal that the caller has finished a
thought and is owed a reply -- is_final alone is not, because endpointing
finalises at every ~300ms pause, mid-thought.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

DEEPGRAM_WS = "wss://api.deepgram.com/v1/listen"

# websockets.connect() has its own default open_timeout (10s), but we pass it
# explicitly to satisfy the project rule that every network operation carries
# an explicit timeout, and so this value can't silently drift with a library
# upgrade.
CONNECT_TIMEOUT = 10.0

# Likewise for the close handshake: bound how long close() can block waiting
# for the server's close frame, rather than inheriting whatever the library
# defaults to.
CLOSE_TIMEOUT = 10.0

# Bound for a single send() call. Inbound audio frames arrive every 20ms, so
# this must be generous relative to that cadence to avoid tripping on normal
# jitter -- but it still needs a ceiling so a stalled TCP connection during a
# live call doesn't hang the frame-forwarding loop indefinitely (the library's
# ping keepalive alone wouldn't bound an individual send).
SEND_TIMEOUT = 5.0


@dataclass(frozen=True)
class SttEvent:
    kind: str          # "speech_started" | "transcript" | "utterance_end"
    text: str = ""
    is_final: bool = False


def parse_dg_message(raw: str) -> SttEvent | None:
    """Parse one Deepgram WebSocket text frame into an SttEvent, or None.

    Returns None for message types we don't act on (e.g. Metadata) and for
    Results messages whose transcript is empty (interim silence). Also
    returns None for syntactically valid JSON that isn't an object (null, a
    bare number, a list, ...) -- same guard as hermes.chat_stream, since
    json.loads happily returns those and .get() on a non-dict would raise.
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "SpeechStarted":
        return SttEvent(kind="speech_started")
    if kind == "UtteranceEnd":
        return SttEvent(kind="utterance_end")
    if kind == "Results":
        alts = (msg.get("channel") or {}).get("alternatives") or []
        text = (alts[0].get("transcript") if alts else "") or ""
        if not text.strip():
            return None
        return SttEvent(kind="transcript", text=text.strip(),
                        is_final=bool(msg.get("is_final")))
    return None


class LiveStt:
    """A single streaming session against Deepgram's /v1/listen WebSocket."""

    def __init__(self, api_key: str, sample_rate: int = 8000,
                 model: str = "nova-2-phonecall") -> None:
        self._api_key = api_key
        self.call_id = ''
        self._params: dict[str, str | tuple[str, ...]] = {
            "model": model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
            "interim_results": "true",
            "vad_events": "true",
            "endpointing": "300",
            "utterance_end_ms": "1000",
            "punctuate": "true",
            "smart_format": "true",
        }
        self._ws: websockets.ClientConnection | None = None

    def set_keywords(self, keywords: tuple[str, ...]) -> None:
        """Set next connection's Nova-2 vocabulary; never rewrite transcripts.

        https://developers.deepgram.com/docs/keywords documents keywords for
        Nova-2 (including phonecall), not Nova-3's unweighted keyterm API.
        """
        if keywords and self._params['model'] not in ('nova-2', 'nova-2-phonecall'):
            raise ValueError('Vocabulary hints require a supported Nova-2 model')
        self._params.pop('keywords', None)
        if keywords:
            self._params['keywords'] = keywords

    async def connect(self) -> None:
        url = f"{DEEPGRAM_WS}?{urlencode(self._params, doseq=True)}"
        # additional_headers is the websockets>=14 keyword; older releases
        # used extra_headers instead. pyproject.toml pins websockets>=14.0,
        # so this call is valid for every installable version -- if this
        # ever raises TypeError on the keyword, the installed websockets
        # version has drifted below that floor and this call needs updating.
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {self._api_key}"},
            open_timeout=CONNECT_TIMEOUT,
            close_timeout=CLOSE_TIMEOUT,
        )

    async def send(self, pcm: bytes) -> None:
        if self._ws is not None:
            await asyncio.wait_for(self._ws.send(pcm), timeout=SEND_TIMEOUT)

    async def events(self) -> AsyncIterator[SttEvent]:
        if self._ws is None:
            return
        ws = self._ws
        outcome = 'eof'
        try:
            async for raw in ws:
                ev = parse_dg_message(raw if isinstance(raw, str) else raw.decode())
                if ev is not None:
                    yield ev
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except Exception:
            outcome = 'error'
            raise
        finally:
            # Free-form close reasons can contain credentials or reflected
            # input. Retain only Deepgram's diagnostic code, never its prose.
            reason = getattr(ws, 'close_reason', '') or ''
            match = re.match(r'^(?:NET|DATA)-[0-9]{4}(?=$|[\s:])', reason)
            safe_reason = match.group(0) if match else ('redacted' if reason else 'empty')
            code = getattr(ws, 'close_code', None)
            code = code if isinstance(code, int) and 1000 <= code <= 4999 else None
            call_id = self.call_id if re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', self.call_id) else 'redacted'
            fields = dict(event='stt.closed', call_id=call_id, outcome=outcome,
                          close_code=code, close_reason=safe_reason)
            logger.info('event=stt.closed call=%s outcome=%s code=%s reason=%s',
                        call_id, outcome, code, safe_reason, extra=fields)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                # Deepgram expects CloseStream as a text (JSON) frame, not
                # binary, so this goes straight to the socket rather than
                # through send() (which is typed for binary PCM audio) --
                # but it still carries the same explicit SEND_TIMEOUT bound.
                await asyncio.wait_for(
                    self._ws.send(json.dumps({"type": "CloseStream"})),
                    timeout=SEND_TIMEOUT,
                )
            except Exception:
                pass
            await self._ws.close()
            self._ws = None
