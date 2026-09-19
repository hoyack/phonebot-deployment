"""Configuration loading. Secrets come from the environment, never from YAML."""
from __future__ import annotations

import math
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
    mode: str = "turn"
    barge_in_confirm_ms: int = 300
    audiosocket_host: str = "host.docker.internal"
    audiosocket_port: int = 9010
    # Optional local bind port when a host-side proxy fronts AudioSocket.
    # None preserves the historical behavior: listen on audiosocket_port.
    audiosocket_listen_port: int | None = None
    # Filler speech (streaming mode only). filler_delay_ms == 0 disables it,
    # which is the default: an extension that does not opt in behaves exactly
    # as it did before filler existed.
    filler_delay_ms: int = 0
    filler_repeat_ms: int = 3000
    # Tuples, not lists: ExtensionConfig is frozen and must stay hashable.
    filler_phrases: tuple[tuple[str, ...], ...] = ()
    # How long the caller may stay silent WHILE THE BRIDGE IS LISTENING before
    # the call is ended (streaming mode only -- see StreamingCallHandler.
    # handle()). 0 disables it, which is the default, so an extension that does
    # not opt in is untouched: extension 7000 must keep ending calls purely on
    # its own silence budget (call.py's SILENT_TURNS_BEFORE_PROMPT), and a
    # second mechanism would fight it.
    # Float, not int, so it can be tuned below a second if a live call ever
    # needs it -- the shipped value is 10. Negative values are normalised to
    # 0 by the loader (see _idle_hangup_seconds), so nothing downstream has to
    # defend against a clock deadline that has already passed.
    idle_hangup_seconds: float = 0.0
    full_name: str = ""
    receptionist: bool = False
    directory_listing: bool = False
    greeting_delay_ms: int = 0
    access_pin: str = ""
    # Absolute monotonic budget for obtaining reply deltas (not an HTTP
    # inactivity timeout). Checked between sentences, never cuts playback.
    hermes_reply_timeout_seconds: float = 30.0

    # Streaming-only, consent-based local call controls. Disabled by default.
    hold_enabled: bool = False
    hold_offer_seconds: float = 15.0
    hold_feedback_seconds: float = 15.0
    hold_moh_class: str = "phonesys_hold"
    # Nova-2 hints, not transcript substitutions. Opt-in per extension.
    stt_keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("hold_offer_seconds", "hold_feedback_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if (not math.isfinite(self.hermes_reply_timeout_seconds)
                or self.hermes_reply_timeout_seconds <= 0):
            raise ValueError("hermes_reply_timeout_seconds must be finite and positive")


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


def _idle_hangup_seconds(raw) -> float:
    """Read idle_hangup_seconds, treating a negative value as disabled.

    Unvalidated, a typo'd `-10` makes `loop.time() - idle_since >= -10` true on
    the very first pass of the streaming loop, so every call on that extension
    is hung up the instant the greeting finishes. Disabling is the safe reading
    of a nonsense value: it degrades to the ten-minute max_call_seconds hold
    this feature exists to shorten, rather than to a dead phone line. Raising
    instead would take the whole bridge down at startup over one extension's
    optional tuning knob.
    """
    value = float(raw)
    return value if value > 0 else 0.0


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
            hermes_reply_timeout_seconds=float(
                ext.get("hermes_reply_timeout_seconds", 30.0)
            ),
            hold_enabled=bool(ext.get("hold_enabled", False)),
            hold_offer_seconds=float(ext.get("hold_offer_seconds", 15.0)),
            hold_feedback_seconds=float(ext.get("hold_feedback_seconds", 15.0)),
            hold_moh_class=str(ext.get("hold_moh_class", "phonesys_hold")),
            stt_keywords=tuple(ext.get("stt_keywords") or ()),
            mode=ext.get("mode", "turn"),
            barge_in_confirm_ms=int(ext.get("barge_in_confirm_ms", 300)),
            audiosocket_host=ext.get("audiosocket_host", "host.docker.internal"),
            audiosocket_port=int(ext.get("audiosocket_port", 9010)),
            audiosocket_listen_port=(
                int(ext["audiosocket_listen_port"])
                if "audiosocket_listen_port" in ext
                else None
            ),
            filler_delay_ms=int(ext.get("filler_delay_ms", 0)),
            filler_repeat_ms=int(ext.get("filler_repeat_ms", 3000)),
            filler_phrases=tuple(
                tuple(str(phrase) for phrase in tier)
                for tier in (ext.get("filler_phrases") or [])
            ),
            idle_hangup_seconds=_idle_hangup_seconds(
                ext.get("idle_hangup_seconds", 0)
            ),
            full_name=str(ext.get("full_name", "")),
            receptionist=bool(ext.get("receptionist", False)),
            directory_listing=bool(ext.get("directory_listing", False)),
            greeting_delay_ms=max(0, int(ext.get("greeting_delay_ms", 0))),
            access_pin=(
                _require_env(ext["access_pin_env"])
                if ext.get("access_pin_env") else ""
            ),
        )
    return Config(ari=ari, extensions=extensions)
