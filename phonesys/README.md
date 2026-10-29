# phonesys

A voice bridge between a local Asterisk/FreePBX PBX and [Hermes](https://github.com/NousResearch/hermes-agent).

Dial extension **7000** from a softphone and hold a spoken conversation with a
Hermes agent. Speech in, speech out.

```
Zoiper 1001 ──SIP/RTP──> Asterisk (FreePBX 17 / Asterisk 21, Docker)
                              │  dial 7000 → extensions_custom.conf
                              │  Stasis(phonesys,7000)
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

Each turn: Asterisk records the caller (using its own silence detection),
Deepgram transcribes, the Hermes `phonebot` profile replies, ElevenLabs
synthesizes, Asterisk plays it back.

## Requirements

- Asterisk with ARI enabled at `127.0.0.1:8088` (see `~/freepbx-docker/README.md`)
- A Hermes profile named `phonebot` with `api_server` enabled on `:8642`
- Deepgram and ElevenLabs API keys
- Python 3.11+

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # then fill in
```

For a full three-profile deployment, use the repository root README and its
private bundle generator rather than copying this example manually. A manual
phonebot-only development `.env` must define:

```
ARI_USER, ARI_PASSWORD
DEEPGRAM_API_KEY
ELEVENLABS_API_KEY
HERMES_PHONEBOT_API_KEY     # matches API_SERVER_KEY in the phonebot profile
```

The shipped four-route `config.yaml` additionally requires
`HERMES_JYODER_API_KEY`, `HERMES_ATLAS_API_KEY`, `JOHN_YODER_PIN`, and
`ATLAS_PIN`. ARI URL/app are YAML settings, not environment overrides. Startup
fails immediately and names any variable that is missing.

### Hermes profile

```bash
hermes profile create phonebot --clone-from <an-existing-working-profile>
```

Add to `~/.hermes/profiles/phonebot/.env`:

```
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=<same value as HERMES_PHONEBOT_API_KEY>
```

Then edit `SOUL.md` for voice (short replies, no markdown), clear
`memories/*.md` so the profile does not inherit another agent's memory, and
comment out any inherited platform tokens (`SLACK_*` etc.) — otherwise two
gateways fight over the same bot connection.

```bash
hermes --profile phonebot gateway run
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8642/health   # 200
```

### Asterisk

Two bind mounts are required in `~/freepbx-docker/docker-compose.yml`, because
ARI's `play` resolves `sound:` URIs against Asterisk's *own* filesystem — it
accepts neither a host path nor a URL:

```yaml
- /home/<you>/work/phonesys/media/sounds:/var/lib/asterisk/sounds/en/phonesys
- /home/<you>/work/phonesys/media/rec:/var/spool/asterisk/recording
```

**The recording directory must be group-writable.** Asterisk runs as uid 999 /
**gid 1000** and writes recordings itself:

```bash
mkdir -p media/sounds media/rec
chmod 775 media/rec        # mkdir alone leaves 755 because of umask
```

Without this, Asterisk logs `Permission denied`, produces no recording, and the
empty audio is indistinguishable from a caller who said nothing — the call
answers, greets, then hangs up after two "silent" turns. The bridge runs a
startup preflight for exactly this and will name the fix.

Do **not** verify the mount with `docker exec sh -c 'echo x > ...'`: that runs
as root, which bypasses permission checks and passes even when Asterisk cannot
write. Test as the real user:

```bash
docker compose exec -u asterisk freepbx \
  sh -c 'touch /var/spool/asterisk/recording/t && echo OK || echo DENIED'
```

And the dialplan in `/etc/asterisk/extensions_custom.conf` (FreePBX never
regenerates this file):

```
[phonesys-custom]
exten => 7000,1,NoOp(phonesys: handing call to Stasis app)
 same => n,Answer()
 same => n,Stasis(phonesys,7000)
 same => n,Hangup()

[from-internal-custom]
include => phonesys-custom
```

## Running

```bash
.venv/bin/python -m phonesys.main
```

Then dial `7000` from any registered extension.

## Development

**The dry-run is the main development loop.** It runs STT → Hermes → TTS from a
WAV file with no telephony involved:

```bash
.venv/bin/python -m phonesys.dryrun path/to/speech.wav
```

Use it whenever something is wrong with the *conversation*. Inside a live call,
a Deepgram auth failure, a dead Hermes gateway and a bad voice id all present
identically as silence. From the dry-run they are distinguishable.

```bash
.venv/bin/pytest        # unit tests, no network
```

Every external service sits behind a small interface with a fake in tests.

## Adding an extension

Config only — no code. Add to `config.yaml`, point it at a Hermes profile's
API, add the dialplan entry, restart the bridge.

```yaml
extensions:
  "7001":
    profile: another-profile
    hermes_base_url: http://127.0.0.1:8643
    hermes_api_key_env: HERMES_ANOTHER_API_KEY
    greeting: "..."
    voice_id: <elevenlabs voice>
    max_silence_seconds: 2
    max_turn_seconds: 30
    max_call_seconds: 600
```

Each profile needs its own gateway on its own port; `api_server` has no
per-request profile selection.

## Barge-in (extension 7001)

Dial extension **7001** for a streaming pipeline you can talk over: start
speaking while the agent is mid-sentence and it stops, the way a person
would. Extension **7000** is untouched — it keeps the turn-based
record-then-reply behaviour described above.

Selected per extension by `mode` in `config.yaml`:

```yaml
extensions:
  "7000":
    mode: turn          # default; record → transcribe → reply → play
  "7001":
    mode: streaming      # continuous duplex audio, interruptible
```

`mode` defaults to `turn` if omitted.

### Architecture

```
Zoiper 1001 ──SIP/RTP──> Asterisk (FreePBX 17 / Asterisk 21, Docker)
                              │  dial 7001 → extensions_custom.conf
                              │  Stasis(phonesys,7001)
                              ▼
                    ┌──────────────────────┐
                    │  phonesys bridge     │  StreamingCallHandler
                    │  ARI mixing bridge   │  Python, WSL host
                    └──────────────────────┘
                         │              ▲
                    originates    connects back
                         ▼              │
              AudioSocket/host.docker.internal:9010  (TCP, both directions)
```

Once the caller channel and the AudioSocket channel are both in the mixing
bridge, audio flows continuously in both directions for the life of the call
— unlike 7000, where record → transcribe → think → speak happens strictly
one step at a time:

```
caller ──PCM slin 8k──> AudioSocket ──> Deepgram live WS ──> transcript + VAD
                                                                   │
                                                                   ▼
                                                            Hermes /chat/stream
                                                                   │ per sentence
                                                                   ▼
caller <──PCM slin 8k── AudioSocket <── ElevenLabs (ulaw_8000) ────┘
```

Deepgram is live-streamed the whole call, Hermes replies come back over
`/chat/stream` and are synthesized by ElevenLabs sentence by sentence (not
one request per full reply), and each sentence's frames are written to the
caller as they're ready. Speaking over the agent while it plays a sentence
is what triggers the interrupt (see Tuning, below).

### Facts that will silently break this if changed

These were measured against this host's Asterisk/Docker setup, not assumed.
Each one fails quietly — the call still connects — so treat them as load-bearing:

- **ARI origination must request `formats=slin`, plural.** The channel is
  created with `POST /channels` and `endpoint=AudioSocket/<host>:<port>/<uuid>`.
  The singular `format` parameter belongs to `externalMedia`, not this
  endpoint — passing it here is silently ignored. Without `formats=slin`
  (plural), the channel driver negotiates its own highest capability instead,
  which on this host is **`slin192`**: 192 kHz audio in 7680-byte frames
  instead of 8 kHz in 320-byte frames. The call answers and audio "flows,"
  but it is the wrong sample rate throughout the pipeline and is unusable.

- **Outbound audio must be paced to real time: one 320-byte frame every
  20 ms.** Asterisk does not throttle writes on this socket, so writing
  frames as fast as ElevenLabs produces them makes Asterisk buffer speech the
  caller has not heard yet — while the bridge already considers it sent. That
  breaks two things at once: an interrupt can no longer stop what is about to
  play, and there is no accurate record of what the caller actually heard for
  history truncation. **If barge-in ever appears not to work, suspect pacing
  before suspecting detection** — this is the single most likely cause.

- **The inbound audio pump must keep forwarding to Deepgram in every state,
  including while the agent is speaking.** Deepgram's endpointing is
  VAD-based and only finalizes an utterance while audio keeps arriving. Pause
  the pump during playback (e.g. to "save" the interrupt logic the trouble of
  reacting mid-sentence) and no final transcript ever comes back — the
  conversation just hangs. Continuous forwarding is also what makes an
  interruption detectable in the first place.

### Tuning

`barge_in_confirm_ms` (default `300`, set in `config.yaml` per extension)
is how long speech must keep being reported before it counts as an
interruption rather than a cough, keyboard noise, or a backchannel like
"mhm". Raise it if background noise is cutting the agent off; lower it if
interrupting feels sluggish.

Be aware this is measured in **Deepgram interim (non-final) transcript
events, not milliseconds of audio** — each interim result while the caller
is talking increments a counter, and the interrupt fires once the counter
crosses `barge_in_confirm_ms // 20`. Interim results do not arrive on a
strict clock, so the real elapsed time before an interrupt fires is
approximate, not exact.

### How interrupted history works

When you interrupt the agent, its conversation history is corrected to match
only what you actually heard — not what it generated. Frames are tracked per
sentence as they're written to the socket; a sentence counts as "heard" only
once every one of its frames was sent. On interrupt, the reply sent back to
Hermes is truncated to the last **fully** heard sentence, rounded down — a
half-played sentence is dropped entirely rather than credited. This is
sentence-granularity, not word-granularity: there is no per-word timing from
TTS to truncate more finely.

### Troubleshooting

**Barge-in doesn't seem to interrupt the agent.** Suspect outbound pacing
before detection — if frames are being buffered ahead of what the caller has
heard, the sender can't stop in time even after an interrupt is confirmed.

**The call connects but nothing happens after your first sentence — no
reply, no error, it just hangs.** Suspect the inbound STT pump being
starved. If audio ever stops reaching Deepgram (including while the agent is
speaking), its VAD-based endpointing never finalizes an utterance and the
state machine waits forever.

**Audio connects but is garbled, silent, or wildly off-pitch.** Suspect the
ARI origination request missing `formats=slin` (plural). Check the request
actually sent to `/channels` — if `format` (singular) was used instead, the
channel silently negotiated `slin192` and every downstream consumer
(Deepgram, the frame codec) is reading the wrong sample rate.

**Extension 7001 doesn't work, but 7000 still does.** Check the startup log
for an AudioSocket bind failure on port 9010 (most commonly `EADDRINUSE`).
The bridge treats this as non-fatal by design: streaming calls are rejected
(answered and immediately hung up) but 7000 never touches that listener, so
it keeps working. Free the port or restart the bridge.

### Known limitations

- **The greeting is not interruptible.** It plays in full before the caller
  can barge in; the caller's speech during it is still transcribed and
  answered as soon as the greeting finishes, but it does not cut the greeting
  short.
- **Sentence-splitting is a regex heuristic, not an NLP tokenizer.** It
  special-cases decimals and version numbers (`$3.50`, `v1.2`), but
  abbreviations like "Dr." or "e.g." are not special-cased and can cause an
  early, mistimed sentence break in TTS.
- **Only one streaming extension is actually supported today.** There is a
  single AudioSocket listener and origination target per process; if more
  than one extension is configured with `mode: streaming`, only the first one
  (in `config.yaml` order) is honoured, and a second streaming extension
  would not work.
- **Teardown detaches on cancellation rather than blocking forever.** If
  cleanup (closing the STT socket, destroying the ARI bridge, hanging up
  channels) is still running when the call's task is cancelled, it keeps
  running in the background up to a bounded timeout instead of being cut off
  immediately — deliberately preferring a leaked coroutine over a leaked
  Asterisk bridge.

## Troubleshooting

**A call lasts exactly 30 seconds and dies.** Not a bridge bug — that is
Asterisk's `rtptimeout` firing because no inbound RTP arrived. Under Docker
Desktop every extension needs an explicit `media_address`. See the "One-way
audio" section of `~/freepbx-docker/README.md`.

**You hear the greeting but it never responds.** Check the bridge log. A
Deepgram `HTTP 400` means empty audio reached it — recording did not produce a
file. Verify the `media/rec` bind mount is live:

```bash
docker compose exec -T freepbx sh -c 'echo x > /var/spool/asterisk/recording/probe.txt'
cat media/rec/probe.txt
```

**Silence instead of the greeting.** The playback mount is not working. Check
`media/sounds` the same way. Files must be group-readable: the bridge writes as
the host user, Asterisk runs as uid 999 / gid 1000.

**The call ends immediately with "ending after 2 silent turns".** Nothing was
transcribed twice running. Either the caller said nothing, or inbound audio is
not reaching Asterisk at all — check `media_address` as above.

**Replies sound like a chat window read aloud** (markdown, URLs, long lists).
Edit `SOUL.md` in the `phonebot` profile, not this code.

## Known limitations (v1)

- **No barge-in.** You cannot interrupt the agent; wait for it to finish.
- Turn latency is roughly 2–5 s: STT, a Hermes turn, then TTS.
- One extension per Hermes profile, and one gateway process per profile.
- `media_address` in FreePBX is static; if the reachable address changes, every
  extension needs updating.
