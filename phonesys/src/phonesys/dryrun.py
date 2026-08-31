"""Run one real turn against live services from a WAV file.

Usage: python -m phonesys.dryrun path/to/input.wav [extension]

This is the development loop. It proves STT -> Hermes -> TTS works before any
telephony is involved, so failures are attributable to a specific service.
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

ROOT = Path(__file__).resolve().parents[2]


async def main(wav_path: str, extension: str = "7000") -> int:
    cfg = load_config(ROOT / "config.yaml")
    ext = cfg.extensions[extension]
    stt = SttClient(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts = TtsClient(api_key=os.environ["ELEVENLABS_API_KEY"])
    hermes = HermesClient(base_url=ext.hermes_base_url, api_key=ext.hermes_api_key)
    try:
        await hermes.create_session("dryrun")
        result = await run_turn(stt, hermes, tts, "dryrun", Path(wav_path).read_bytes(), ext)
        print(f"transcript: {result.transcript!r}")
        print(f"reply     : {result.reply!r}")
        out = ROOT / "dryrun-reply.ulaw"
        out.write_bytes(result.audio)
        print(f"audio     : {len(result.audio)} bytes -> {out}")
        return 0 if result.reply else 1
    finally:
        await stt.aclose()
        await tts.aclose()
        await hermes.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*sys.argv[1:])))
