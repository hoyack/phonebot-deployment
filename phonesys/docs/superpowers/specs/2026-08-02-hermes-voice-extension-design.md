# Hermes Voice Extension — Design

**Date:** 2026-08-02
**Status:** Approved for planning

## Goal

Dial extension `7000` from a Zoiper softphone and hold a spoken conversation with
a Hermes agent. Speech in, speech out, backed by a dedicated Hermes profile whose
persona and memory are separate from the existing `perseus` profile.

## Scope

**v1 delivers:** one extension (`7000`) bound to one Hermes profile (`phonebot`),
turn-based conversation, greeting on answer, graceful hangup.

**v1 explicitly excludes:** barge-in, streaming STT/TTS, multiple simultaneous
profiles, outbound calls, call recording/transcript storage, and any real PSTN
trunk. Each is a follow-on.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Interaction model | Turn-based, no barge-in | Fewest moving parts; proves the whole path. The Hermes seam is identical under streaming, so upgrading transport later does not mean rewriting the integration. |
| Hermes seam | External bridge → `api_server` | No Hermes code changes. A live, working Hermes install stays untouched while the novel telephony code is developed. |
| Extension→profile binding | Bridge config | `api_server` has no per-request profile selection; a profile is a whole `HERMES_HOME` with one gateway each. Mapping to a per-profile base URL is the natural fit. |
| Profile | New `phonebot` | Voice-shaped persona (short, spoken, no markdown) and isolated memory, so call transcripts never enter `perseus`. |
| STT / TTS | Deepgram / ElevenLabs | Lower latency and better voices than Whisper+OpenAI TTS, and both stream — the path needed for barge-in later. |
| Turn detection | Asterisk `maxSilenceSeconds` | Asterisk already does end-of-speech detection. Hand-rolled VAD is a common source of bad turn-taking. |
| Audio format | ElevenLabs `ulaw_8000` → raw `.ulaw` | Asterisk reads µ-law 8k natively. No resampling or `ffmpeg` in the turn loop. |

## Architecture

```
Zoiper 1001 ──SIP/RTP──> Asterisk (FreePBX 17 / Asterisk 21, Docker)
                              │  dial 7000 → Custom Destination
                              │  Stasis(phonesys)
                              ▼
                    ┌──────────────────────┐
                    │  phonesys bridge     │  Python, WSL host
                    │  ARI REST + WebSocket│  127.0.0.1:8088
                    └──────────────────────┘
                       │         │         │
                  Deepgram    Hermes    ElevenLabs
                    STT      phonebot      TTS
                             :8642
```

## Call flow

1. Caller dials `7000`. FreePBX Custom Destination routes to `Stasis(phonesys,7000)`.
2. Bridge receives `StasisStart`, answers the channel.
3. Resolves `7000` → profile `phonebot` → Hermes base URL. Opens a Hermes session.
4. Synthesizes and plays the greeting.
5. Turn loop until `StasisEnd`:
   1. `POST /channels/{id}/record` with `maxSilenceSeconds` — Asterisk detects end of speech.
   2. `RecordingFinished` → read WAV (8 kHz mono).
   3. Deepgram → transcript.
   4. `POST /api/sessions/{sid}/chat` → reply text.
   5. ElevenLabs → µ-law audio → written to the shared sounds directory.
   6. `POST /channels/{id}/play` with `sound:phonesys/<name>`.
6. On hangup: cancel in-flight work, close the Hermes session, delete temp audio.

## Components

Each external service sits behind a small interface so a fake substitutes in tests.

| Module | Responsibility | Interface |
|---|---|---|
| `config.py` | Load `.env` + `config.yaml`; validate at startup | `load() -> Config` |
| `ari.py` | ARI REST + WebSocket client | `answer`, `record`, `play`, `hangup` |
| `stt.py` | Deepgram adapter | `transcribe(wav: bytes) -> str` |
| `tts.py` | ElevenLabs adapter | `synthesize(text: str) -> bytes` |
| `hermes.py` | Hermes `api_server` client | `Session.send(text) -> str` |
| `call.py` | Per-call turn loop | `handle(channel) -> None` |
| `main.py` | Connect ARI WebSocket, dispatch `StasisStart` | — |

`call.py` is the only module aware of turn-taking; the others are adapters. The
component most likely to need tuning is therefore isolated from the plumbing.

## Configuration

Secrets stay in `.env` and are never read by tooling or committed:

```
ARI_BASE_URL, ARI_USER, ARI_PASSWORD, ARI_APP
DEEPGRAM_API_KEY
ELEVENLABS_API_KEY
```

Adding an extension is config-only:

```yaml
extensions:
  "7000":
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    greeting: "Hi, this is Hermes. What can I do for you?"
    voice_id: "<elevenlabs voice>"
    max_silence_seconds: 2
    max_turn_seconds: 30
    max_call_seconds: 600
```

## Shared filesystem

The bridge runs on the WSL host; Asterisk plays and records inside the container.
ARI `play` takes a `sound:` URI resolved against Asterisk's own filesystem — it
accepts neither a host path nor a URL. Two bind mounts are therefore required:

```yaml
- ./media/sounds:/var/lib/asterisk/sounds/en/phonesys   # bridge writes TTS here
- ./media/rec:/var/spool/asterisk/recording             # Asterisk writes recordings here
```

Both paths sit under `/var`, currently the `var_data` named volume. Nested bind
mounts overlay the volume at those paths, which works but is the fiddliest part
of the setup and needs a container recreate. This is the highest-risk step.

## Error handling

The call must never go silent.

| Failure | Behavior |
|---|---|
| Empty transcript | Reprompt; after 2 consecutive, close politely |
| Hermes error or timeout | Spoken apology; call stays up |
| TTS failure | Play pre-rendered fallback prompt |
| ARI WebSocket drop | Reconnect with backoff; in-flight calls end |
| Hangup mid-turn | Cancel outstanding work, close session, clean temp audio |
| Runaway call | Hard cap via `max_call_seconds` |

Every external call carries an explicit timeout. No unbounded waits.

## Testing

- **Unit** — fakes for Deepgram, ElevenLabs, Hermes, and ARI. No network.
- **Dry-run mode** — run STT → Hermes → TTS against a WAV file with no telephony.
  Makes the entire AI path debuggable without dialing, and is the primary
  development loop.
- **Integration** — bridge against live ARI using an originated channel.
- **Live smoke test** — dial `7000` from Zoiper and hold a conversation.

## Prerequisites

These are real work, not footnotes, and some may need the user directly:

1. **`phonebot` profile creation may require interactive ChatGPT OAuth.** Hermes
   uses the `openai-codex` provider rather than an API key, so profile setup
   likely cannot be completed unattended.
2. `api_server` must be enabled on the `phonebot` profile and its gateway started
   on a dedicated port. It is currently neither enabled nor listening.
3. Bind mounts added and the FreePBX container recreated.
4. A FreePBX Custom Destination for `7000` pointing at `Stasis(phonesys,7000)`.

## Known constraints inherited from the PBX

- Docker Desktop masks source addresses; every peer appears as `10.77.0.1`. This
  is why each extension needs an explicit `media_address`. Any new SIP endpoint
  will hit it. See the FreePBX repo README.
- ARI is published on `127.0.0.1:8088` only — the bridge must run on the WSL host
  or be given explicit access.
- RTP is capped to `10000-10099` (~50 concurrent calls).

## Open questions

- Extension `7000` assumed; not explicitly confirmed.
- ElevenLabs voice not yet chosen.
- Whether `phonebot` should start empty or be seeded from `perseus`.
