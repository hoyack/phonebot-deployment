"""Entrypoint: watch ARI for StasisStart and hand each call to a CallHandler."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from phonesys.ari import AriClient
from phonesys.audiosocket import AudioSocketServer
from phonesys.call import CallHandler
from phonesys.config import load_config
from phonesys.filler import FillerBank
from phonesys.hermes import HermesClient
from phonesys.recordings import RecordingWaiter
from phonesys.stream_call import StreamingCallHandler
from phonesys.stream_stt import LiveStt
from phonesys.stream_tts import StreamTts
from phonesys.stt import SttClient
from phonesys.tasks import spawn
from phonesys.tts import TtsClient
from phonesys.waiters import DtmfCollector, PlaybackWaiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("phonesys")

ROOT = Path(__file__).resolve().parents[2]


ASTERISK_GID = 1000  # asterisk runs uid 999 / gid 1000 in the freepbx image


def preflight_media_dirs(sounds: Path, rec: Path) -> list[str]:
    """Check Asterisk can actually use the shared directories.

    Asterisk writes recordings as uid 999 / gid 1000, so the recording
    directory must be group-writable. Verifying with `docker exec` alone is
    misleading: that runs as root, which bypasses permission checks entirely.
    """
    problems: list[str] = []
    for path, need_group_write in ((sounds, False), (rec, True)):
        if not path.is_dir():
            problems.append(f"{path} does not exist")
            continue
        mode = path.stat().st_mode
        if need_group_write and not mode & 0o020:
            problems.append(
                f"{path} is not group-writable (mode {oct(mode & 0o777)}); "
                f"Asterisk (gid {ASTERISK_GID}) cannot write recordings there. "
                f"Fix with: chmod 775 {path}"
            )
        if not mode & 0o044:
            problems.append(f"{path} is not group-readable (mode {oct(mode & 0o777)})")
    return problems


# The streaming AudioSocket listener binds every interface -- Asterisk
# reaches it from inside a Docker container, so this must not be narrowed to
# a single local address. This is genuinely a different value from
# ExtensionConfig.audiosocket_host (host.docker.internal): that one is the
# address Asterisk dials *back to*, which is meaningless as a bind address
# inside this container. The two must never be collapsed into one constant.
AUDIOSOCKET_LISTEN_HOST = "0.0.0.0"

# Extension modes CallHandler/StreamingCallHandler dispatch is aware of. Any
# other value is a likely typo (e.g. "stream") and is logged loudly rather
# than silently treated as "turn", which would run the wrong pipeline
# unnoticed.
KNOWN_MODES = frozenset({"turn", "streaming"})

# Name of the one background task that is not a call, so it can be picked out
# of a task dump (or a test's view of tasks.BACKGROUND_TASKS) rather than
# identified by elimination.
PREPARE_TASK_NAME = "streaming-speech-prep"


def _first_streaming_extension(cfg):
    """The extension whose audiosocket_port/host configure the one shared
    AudioSocket listener and origination target.

    There is exactly one AudioSocketServer/StreamingCallHandler pair for the
    whole process, so if more than one streaming-mode extension is ever
    configured, only the first (YAML/dict order) is honoured -- there is no
    other value to bind the shared listener to.
    """
    for ext in cfg.extensions.values():
        if ext.mode == "streaming":
            return ext
    return None


async def _start_streaming(cfg, ari: AriClient,
                           dtmf: DtmfCollector | None = None) -> StreamingCallHandler | None:
    """Build the streaming (barge-in) path, or None if it is unavailable.

    Returning None rather than raising is the point: a bind failure here
    (most commonly EADDRINUSE -- port 9000 was already taken by an unrelated
    minio container on this host, which is why streaming uses 9010) must not
    take extension 7000 down with it. 7000 never touches the AudioSocket
    listener, so an uncaught OSError propagating out of main() before the
    ARI event loop even starts would turn a streaming-only outage into a
    total one. The caller rejects streaming calls cleanly instead.

    The bind is still awaited here, and deliberately: it is a local
    socket operation, not a network round trip, and its result is what
    decides whether main() dispatches streaming calls or rejects them.
    Speech synthesis -- the part that actually cost startup ~1.6s -- is not;
    see _prepare_streaming_speech.
    """
    ext = _first_streaming_extension(cfg)
    if ext is None:
        logger.info("no streaming-mode extension configured; audiosocket listener not started")
        return None

    # By default the local listener uses audiosocket_port, preserving the
    # original direct-connect behavior. A distinct audiosocket_listen_port
    # supports a Windows-side proxy: Asterisk dials the Windows port while
    # this WSL process keeps its own stable local port. audiosocket_host is
    # deliberately NOT used here -- see AUDIOSOCKET_LISTEN_HOST above.
    #
    # Construction is INSIDE the try: asyncio.start_server is not the only
    # thing here that can raise OSError, and anything that escapes this
    # function takes extension 7000 down with it.
    listen_port = ext.audiosocket_listen_port or ext.audiosocket_port
    try:
        as_server = AudioSocketServer(
            host=AUDIOSOCKET_LISTEN_HOST, port=listen_port
        )
        await as_server.start()
    except OSError as exc:
        logger.error(
            "audiosocket listener failed to bind %s:%s (%s); streaming "
            "extensions will be rejected until this process restarts, "
            "turn-based extensions are unaffected",
            AUDIOSOCKET_LISTEN_HOST, listen_port, exc,
        )
        return None

    # Hoisted out of the constructor call below so the same client synthesizes
    # the filler phrases and the replies.
    stream_tts = StreamTts(api_key=os.environ["ELEVENLABS_API_KEY"])

    # Built empty and prepared in the background (below). An unprepared bank
    # is simply a bank with no audio -- is_empty() already means "no filler",
    # exactly like before filler existed -- so a call landing before
    # preparation finishes is served without it rather than delayed by it.
    filler_bank = FillerBank(ext.filler_phrases)

    # Passed by keyword, not position: this constructor takes eight arguments,
    # four of which are opaque callables/clients, so a reordering would be
    # accepted silently by both Python and any test that only counts them.
    #
    # greeting_frames is deliberately NOT passed: there is nothing to pass
    # yet. The startup synthesis now runs in the background and installs
    # itself with set_greeting_frames() when it lands.
    handler = StreamingCallHandler(
        ari=ari,
        stt_factory=lambda: LiveStt(api_key=os.environ["DEEPGRAM_API_KEY"]),
        tts=stream_tts,
        hermes_factory=lambda ext: HermesClient(
            base_url=ext.hermes_base_url, api_key=ext.hermes_api_key
        ),
        audiosocket_server=as_server,
        ari_app=cfg.ari.app,
        filler_bank=filler_bank,
        filler_profile=ext.profile,
        extensions=cfg.extensions,
        dtmf=dtmf,
    )

    # spawn(), not awaited and not a bare create_task: awaiting it here is
    # the whole bug this exists to avoid (see _prepare_streaming_speech), and
    # the event loop keeps only a weak reference to a task nobody holds, so a
    # discarded handle can be garbage-collected mid-synthesis.
    spawn(_prepare_streaming_speech(handler, filler_bank, stream_tts, ext),
          name=PREPARE_TASK_NAME)
    return handler


async def _prepare_streaming_speech(
    handler: StreamingCallHandler, filler_bank: FillerBank,
    stream_tts: StreamTts, ext,
) -> None:
    """Synthesize the greeting and the filler phrases, off the startup path.

    Everything in here is an ElevenLabs round trip -- 400-600ms each (see
    filler.py's module docstring), and the shipped config has seven filler
    phrases plus the greeting, measured at ~1.6s in total. Awaiting that
    before main() reaches `async for event in ari.events()` means the process
    is not consuming ARI events for those 1.6s, so extension 7000 -- which is
    turn-based, already live, and uses neither filler nor this greeting cache
    -- cannot accept a call for the sake of a feature it does not have. Hence
    a background task: the event loop starts immediately and this lands when
    it lands.

    The greeting goes FIRST, filler second. A 7001 call landing while this
    task is still running gets whatever has landed so far (see below), and
    the greeting is the one thing every 7001 caller hears immediately --
    filler only matters once the agent is already thinking. Greeting-first
    shrinks the caller-audible degraded window (the one where a call pays a
    per-call greeting round trip because the cache is not installed yet) from
    the full ~1.6s to the greeting's own ~0.5s.

    What a call arriving mid-preparation gets is the pre-existing degraded
    path, not a new one: an unprepared FillerBank is_empty(), which already
    means "no filler", and a greeting cache of None already means _speak()
    synthesizes the greeting for that call. Both were reachable before this
    change whenever ElevenLabs was down at startup.

    Nothing here may raise. This is a detached task, so an escaping exception
    is not merely unhandled -- it surfaces at garbage-collection time as
    "Task exception was never retrieved", long after the fact and attached to
    nothing. The two inner guards are not merely there to keep two log
    messages distinct: each keeps a failure in ONE of these two operations
    from escaping past the OTHER, which runs immediately afterward in the
    same coroutine. An operation without its own inner guard has its
    exception jump straight to the outer backstop below -- which does not
    resume the coroutine, so whichever operation was still pending simply
    never runs, not merely "runs without that step's result". Concretely:
    with the greeting synthesized first, it is the greeting's inner guard
    that keeps a greeting failure from also cancelling filler preparation;
    the filler's inner guard is symmetric insurance against the reverse. Skip
    either guard and a transient ElevenLabs failure in the first operation
    silently takes the second one down with it for the rest of the process,
    while a fully green test suite watches, because the outer backstop below
    still makes the coroutine itself "never raise". The outer guard is that
    backstop -- it makes "never raises" true of the whole coroutine, not just
    of the two calls someone remembered to wrap.
    """
    try:
        # ext.greeting is a frozen-dataclass config constant (config.py's
        # ExtensionConfig.greeting), so paying its round trip on every call
        # means the caller sits through 400-600ms while already connected,
        # for a string that never changes. Unlike filler this is not opt-in
        # -- handle() always speaks the greeting -- so there is no "not
        # configured" branch to skip it under. Synthesized first: see the
        # docstring for why.
        #
        # A failure must not take 7001 down: leaving the handler's cache
        # unset means _speak() synthesizes the greeting per call, exactly as
        # it did before this cache existed. Same isolation rule as the filler
        # bank and the bind failure above.
        try:
            frames = await stream_tts.synthesize_frames(
                ext.greeting, ext.voice_id, 0
            )
        except Exception:
            logger.exception(
                "greeting failed to pre-synthesize; falling back to per-call synthesis"
            )
        else:
            # frames == [] is not a successful result worth caching: _speak's
            # `cached is not None` check (stream_call.py) would accept it and
            # then send nothing, so every call would install a silent
            # greeting for the rest of the process. Treat it the same as the
            # except branch above -- fall back to per-call synthesis -- by
            # simply not installing it.
            if frames:
                handler.set_greeting_frames(frames)

        # Skipped entirely when filler is not configured: an extension that
        # has not opted in must not pay for a synthesis round trip it will
        # never use.
        #
        # A failure must not take 7001 down: an empty bank simply means no
        # filler, exactly like before it existed. Same isolation rule as the
        # bind failure above.
        if ext.filler_delay_ms > 0 and ext.filler_phrases:
            try:
                await filler_bank.prepare(stream_tts, ext.voice_id)
            except Exception:
                logger.exception(
                    "filler bank failed to prepare; continuing without filler speech"
                )
        else:
            logger.info(
                "filler speech not configured for extension in streaming mode"
            )
    except Exception:
        logger.exception(
            "background preparation of streaming speech failed; extension "
            "continues without filler and with per-call greeting synthesis"
        )


async def main() -> None:
    cfg = load_config(ROOT / "config.yaml")

    sounds_dir, rec_dir = ROOT / "media" / "sounds", ROOT / "media" / "rec"
    for problem in preflight_media_dirs(sounds_dir, rec_dir):
        logger.error("preflight: %s", problem)
    ari = AriClient(cfg.ari)
    stt = SttClient(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts = TtsClient(api_key=os.environ["ELEVENLABS_API_KEY"])
    recordings = RecordingWaiter()
    playbacks = PlaybackWaiter()
    dtmf = DtmfCollector()
    handler = CallHandler(
        ari, stt, tts,
        lambda ext: HermesClient(base_url=ext.hermes_base_url, api_key=ext.hermes_api_key),
        sounds_dir,
        rec_dir,
        recordings=recordings,
        playbacks=playbacks,
    )

    # Streaming (barge-in) path, kept entirely separate from the turn-based
    # handler above: same AriClient, disjoint everything else, so extension
    # 7000's behaviour cannot be touched by this addition -- including when
    # the AudioSocket listener fails to bind (see _start_streaming).
    streaming = await _start_streaming(cfg, ari, dtmf)

    # No shutdown path is added here (see module docstring / task-9 report):
    # StreamingCallHandler.handle() deliberately detaches its teardown on
    # cancellation rather than guaranteeing prompt completion, so cancelling
    # in-flight call tasks here and immediately closing `ari` or exiting the
    # loop would strand that detached teardown and leak the Asterisk bridge
    # -- exactly what the bounded-detach design in stream_call.py exists to
    # avoid. main() therefore keeps running forever, same as before this
    # task, and only exits (uncleanly, like today) via KeyboardInterrupt.

    logger.info("listening for calls on ARI app %s", cfg.ari.app)
    async for event in ari.events():
        etype = event.get("type")

        if etype == "ChannelDtmfReceived":
            channel = event.get("channel") or {}
            channel_id = channel.get("id")
            digit = str(event.get("digit", ""))
            if channel_id:
                dtmf.feed(channel_id, digit)
            continue

        # ARI returns 201 when recording STARTS; the turn loop blocks until the
        # matching finished/failed event lands here.
        if etype in ("RecordingFinished", "RecordingFailed"):
            name = (event.get("recording") or {}).get("name")
            if name:
                recordings.resolve(name, ok=(etype == "RecordingFinished"))
            continue

        # Likewise, ARI returns 201 when playback STARTS. The turn loop blocks
        # until this lands so that the recorder never runs while the caller is
        # still listening to the agent.
        if etype == "PlaybackFinished":
            playback_id = (event.get("playback") or {}).get("id")
            if playback_id:
                playbacks.resolve(playback_id, ok=True)
            continue

        if etype != "StasisStart":
            continue
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        args = event.get("args") or []
        extension = args[0] if args else ""

        # A Local channel's ;1 leg enters the app with no args; only the leg
        # that ran the dialplan carries the extension. Ignore the rest.
        if not extension:
            logger.debug("ignoring StasisStart with no extension arg (%s)", channel.get("name"))
            continue

        ext_cfg = cfg.extensions.get(extension)
        if ext_cfg is None:
            logger.warning("no config for extension %r; hanging up", extension)
            await ari.hangup(channel_id)
            continue

        logger.info("call %s -> extension %s (profile %s, mode %s)",
                    channel_id, extension, ext_cfg.profile, ext_cfg.mode)
        if ext_cfg.mode == "streaming":
            if streaming is None:
                # The listener failed to bind at startup (see
                # _start_streaming) -- answer and hang up rather than
                # leaving the caller connected to a Stasis app that will
                # never originate the AudioSocket leg.
                logger.warning(
                    "call %s: extension %s is streaming-mode but the "
                    "audiosocket listener is unavailable; rejecting the call",
                    channel_id, extension,
                )
                await ari.answer(channel_id)
                await ari.hangup(channel_id)
                continue
            # spawn(), not a bare create_task: the loop keeps only a weak
            # reference, so a discarded task can be garbage-collected
            # mid-call.
            spawn(streaming.handle(channel_id, extension, ext_cfg),
                  name=f"call-{channel_id}")
        else:
            if ext_cfg.mode not in KNOWN_MODES:
                logger.warning(
                    "call %s: extension %s has unrecognized mode %r; "
                    "falling back to turn-based handling",
                    channel_id, extension, ext_cfg.mode,
                )
            spawn(handler.handle(channel_id, extension, ext_cfg),
                  name=f"call-{channel_id}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("shutting down")
