"""One conversational turn: audio in, audio out. No telephony here.

Keeping this free of ARI is deliberate -- it lets the whole AI path be
exercised from a WAV file (see dryrun.py), where a Deepgram auth failure, a
dead Hermes gateway and a bad voice id look different from each other. Inside
a live call they all present identically as silence.
"""
from __future__ import annotations

from dataclasses import dataclass

from phonesys.config import ExtensionConfig


@dataclass(frozen=True)
class TurnResult:
    transcript: str
    reply: str
    audio: bytes


async def run_turn(stt, hermes, tts, session_id: str, audio: bytes, ext: ExtensionConfig) -> TurnResult:
    # A missing or empty recording is silence, not an error. Posting zero bytes
    # to Deepgram returns HTTP 400, which would surface as a failed turn.
    if not audio:
        return TurnResult(transcript="", reply="", audio=b"")

    transcript = (await stt.transcribe(audio)).strip()
    if not transcript:
        return TurnResult(transcript="", reply="", audio=b"")

    reply = (await hermes.chat(session_id, transcript)).strip()
    if not reply:
        return TurnResult(transcript=transcript, reply="", audio=b"")

    return TurnResult(transcript=transcript, reply=reply, audio=await tts.synthesize(reply, ext.voice_id))
