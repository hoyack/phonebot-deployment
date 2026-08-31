"""Tests for the wiring/dispatch logic in main.py.

These exercise main() (and its _start_streaming helper) directly rather than
re-implementing their logic: AriClient/SttClient/TtsClient/CallHandler/
StreamingCallHandler/AudioSocketServer are all network- or credential-backed,
so each is replaced with a fake or spy via monkeypatch, and the real dispatch
code in main() runs unmodified against them.

Covers two fix-round-1 findings:

1. A port conflict on the AudioSocket listener (streaming, new) must not take
   extension 7000 (turn-based, old and stable) down with it -- see
   test_audiosocket_bind_failure_does_not_stop_turn_based_calls and
   test_audiosocket_bind_failure_rejects_streaming_calls_cleanly.
2. ExtensionConfig.audiosocket_port must actually reach the AudioSocket
   listener bind, not just sit unread next to a hardcoded constant -- see
   test_streaming_listener_binds_the_configured_port.
"""
from __future__ import annotations

import asyncio
import gc
import inspect

import pytest

import phonesys.main as main_mod
from phonesys.config import AriConfig, Config, ExtensionConfig
from phonesys.stream_call import StreamingCallHandler
from phonesys.tasks import BACKGROUND_TASKS

TURN_EXT = ExtensionConfig(
    profile="phonebot", hermes_base_url="http://h", hermes_api_key="k",
    greeting="hi", voice_id="v", max_silence_seconds=1, max_turn_seconds=30,
    max_call_seconds=600, mode="turn",
)


def _stream_ext(port: int = 9010, *, filler: bool = False) -> ExtensionConfig:
    # filler=False (the default here, matching ExtensionConfig's own default)
    # is what every pre-Task-3 test in this file exercises and must keep
    # exercising: _start_streaming's "not configured" branch (see
    # test_filler_disabled_extension_never_calls_prepare below) is only
    # covered because most callers of _cfg()/_stream_ext() get this path.
    # Only the two dedicated filler-prepare tests opt in with filler=True.
    filler_kwargs = {}
    if filler:
        # Mirrors config.yaml's real "7001" entry, so the opted-in tests
        # exercise the same shape of config production actually ships.
        filler_kwargs = dict(
            filler_delay_ms=800, filler_repeat_ms=3000,
            filler_phrases=(
                ("Let me think about that.", "Give me a second.", "Good question."),
                ("Still working on that.", "Bear with me."),
                ("Almost there.", "Nearly done."),
            ),
        )
    return ExtensionConfig(
        profile="phonebot", hermes_base_url="http://h", hermes_api_key="k",
        greeting="hi", voice_id="v", max_silence_seconds=1, max_turn_seconds=30,
        max_call_seconds=600, mode="streaming",
        audiosocket_host="host.docker.internal", audiosocket_port=port,
        **filler_kwargs,
    )


def _cfg(*, streaming_port: int = 9010, filler: bool = False) -> Config:
    ari_cfg = AriConfig(base_url="http://x", username="u", password="p", app="phonesys")
    return Config(
        ari=ari_cfg,
        extensions={"7000": TURN_EXT, "7001": _stream_ext(streaming_port, filler=filler)},
    )


class FakeAriClient:
    """Stands in for AriClient: no network. events() yields a scripted list
    and then ends, letting `async for event in ari.events()` in main()
    return instead of blocking forever -- that return is how these tests
    observe "the process reached the point of serving turn-based calls"."""

    def __init__(self, ari_cfg=None) -> None:
        self.cfg = ari_cfg
        self.answered: list[str] = []
        self.hungup: list[str] = []
        self.events_to_yield: list[dict] = []

    async def events(self):
        for event in self.events_to_yield:
            yield event

    async def answer(self, channel_id: str) -> None:
        self.answered.append(channel_id)

    async def hangup(self, channel_id: str) -> None:
        self.hungup.append(channel_id)


class RecordingHandler:
    """Stands in for both CallHandler and StreamingCallHandler: the
    constructor accepts and ignores whatever main.py passes it, and .handle()
    just records the call, so tests can assert dispatch decisions without
    exercising real STT/TTS/Hermes call logic."""

    def __init__(self, *args, **kwargs) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.calls: list[tuple] = []
        # Mirrors StreamingCallHandler.set_greeting_frames: the greeting is
        # installed AFTER construction now (main.py synthesizes it in the
        # background so the ARI event loop is not held up by ElevenLabs), so
        # a fake that only recorded constructor kwargs could no longer see
        # it arrive at all. Every install is recorded, not just the last, so
        # a test can tell "not installed yet" from "installed twice".
        self.greeting_installs: list = []

    def set_greeting_frames(self, frames) -> None:
        self.greeting_installs.append(frames)

    @property
    def greeting_frames(self):
        """Whatever _speak would find in the cache right now: None until the
        background preparation installs it."""
        return self.greeting_installs[-1] if self.greeting_installs else None

    async def handle(self, channel_id, extension, ext_cfg) -> None:
        self.calls.append((channel_id, extension, ext_cfg))


class FailingAudioSocketServer:
    """Simulates the AudioSocket port already being bound -- e.g. by the
    unrelated minio container that already holds port 9000 on this host,
    which is why streaming uses 9010 in the first place."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    async def start(self) -> None:
        raise OSError("[Errno 98] Address already in use")


class FakeAudioSocketServer:
    """Records the host/port it was asked to bind, without touching a real
    socket, so tests can assert the value reaching the constructor came from
    config rather than a hardcoded constant."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.started = False

    async def start(self) -> None:
        self.started = True


class FakeStreamTts:
    """Stands in for StreamTts: records synthesize_frames calls and returns
    deterministic frames, without a network round trip. Also usable
    wherever FillerBank.prepare needs a `tts` -- prepare() calls
    synthesize_frames the same way."""

    def __init__(self, frames_per_sentence: int = 3) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self._n = frames_per_sentence

    async def synthesize_frames(self, text, voice_id, sentence_index):
        self.calls.append((text, voice_id, sentence_index))
        return [(text.encode(), sentence_index) for _ in range(self._n)]


class ExplodingStreamTts(FakeStreamTts):
    """Simulates ElevenLabs being down for the greeting's startup synthesis,
    the same way test_a_failing_filler_bank_does_not_stop_streaming
    simulates it for filler."""

    async def synthesize_frames(self, text, voice_id, sentence_index):
        self.calls.append((text, voice_id, sentence_index))
        raise RuntimeError("elevenlabs is down")


class EmptyFramesStreamTts(FakeStreamTts):
    """Simulates a synthesis call that "succeeds" but is nonetheless useless
    -- e.g. a whitespace-only `greeting` in config, which ElevenLabs happily
    accepts and returns zero audio frames for. Distinct from
    ExplodingStreamTts: this does not raise, so the greeting's `except`
    branch never runs -- `frames` really is `[]`, not a stand-in for a
    synthesis failure."""

    async def synthesize_frames(self, text, voice_id, sentence_index):
        self.calls.append((text, voice_id, sentence_index))
        return []


class SlowStreamTts(FakeStreamTts):
    """A synthesis round trip that does not return until the test says so.

    Stands in for the real cost being modelled: seven filler phrases plus the
    greeting at 400-600ms of ElevenLabs each, measured at ~1.6s of startup.
    An asyncio.Event rather than a sleep so the tests assert on ordering
    ("the event loop was reached with synthesis still outstanding") rather
    than racing a wall clock."""

    def __init__(self, frames_per_sentence: int = 3) -> None:
        super().__init__(frames_per_sentence)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def synthesize_frames(self, text, voice_id, sentence_index):
        self.entered.set()
        await self.release.wait()
        return await super().synthesize_frames(text, voice_id, sentence_index)


def _capture_spawned(monkeypatch) -> dict[str | None, list[asyncio.Task]]:
    """Record every task main.py spawns, keyed by name.

    BACKGROUND_TASKS is not enough on its own: a task removes itself from
    that set as soon as it finishes, so a test that wants to assert the
    background preparation task ended cleanly (`.exception() is None`) could
    not find it any more. This keeps a handle regardless.
    """
    spawned: dict[str | None, list[asyncio.Task]] = {}
    real_spawn = main_mod.spawn

    def _spy(coro, *, name=None):
        task = real_spawn(coro, name=name)
        spawned.setdefault(name, []).append(task)
        return task

    monkeypatch.setattr(main_mod, "spawn", _spy)
    return spawned


def _prep_task(spawned) -> asyncio.Task:
    tasks = spawned.get(main_mod.PREPARE_TASK_NAME, [])
    assert len(tasks) == 1, f"expected one preparation task, got {tasks}"
    return tasks[0]


async def _drain_preparation(timeout: float = 2.0) -> None:
    """Let the background speech preparation finish.

    Startup synthesis is no longer awaited before the ARI event loop, so
    main() returning no longer means preparation has run. Any test asserting
    something about the FINISHED state of preparation has to wait for it
    here, or it asserts against a task that has not started.
    """
    pending = [t for t in BACKGROUND_TASKS
               if t.get_name() == main_mod.PREPARE_TASK_NAME]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=timeout)


def _stasis_start(channel_id: str, extension: str) -> dict:
    return {"type": "StasisStart", "channel": {"id": channel_id}, "args": [extension]}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Common isolation for every test in this file: no filesystem preflight,
    no real credentials, and CallHandler/StreamingCallHandler swapped for the
    spy above so no real STT/TTS/Hermes client is ever constructed."""
    monkeypatch.setattr(main_mod, "CallHandler", RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", RecordingHandler)
    monkeypatch.setattr(main_mod, "SttClient", lambda **kw: object())
    monkeypatch.setattr(main_mod, "TtsClient", lambda **kw: object())
    monkeypatch.setattr(main_mod, "LiveStt", lambda **kw: object())
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: object())
    monkeypatch.setattr(main_mod, "HermesClient", lambda **kw: object())
    monkeypatch.setattr(main_mod, "preflight_media_dirs", lambda *a, **kw: [])
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")


@pytest.fixture(autouse=True)
async def _reap_preparation_task():
    """Tear down any background speech-preparation task the test left running.

    main() now spawns preparation instead of awaiting it, so a test that
    returns from main() without draining leaves a live task holding a
    coroutine into fakes owned by that test. Left alone it is destroyed
    pending at loop close ("Task was destroyed but it is pending!") and, worse,
    could still be running while the NEXT test's monkeypatches are in place.
    Cancelling rather than awaiting: a test may deliberately have left
    synthesis blocked forever."""
    yield
    leftover = [t for t in BACKGROUND_TASKS
                if t.get_name() == main_mod.PREPARE_TASK_NAME]
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)


def _capturing_factory(cls):
    """Wraps a fake handler class so the test can recover the specific
    instance main() constructed and inspect the calls it recorded."""
    instances: list = []

    def _make(*args, **kwargs):
        obj = cls(*args, **kwargs)
        instances.append(obj)
        return obj

    return _make, instances


async def test_audiosocket_bind_failure_does_not_stop_turn_based_calls(monkeypatch):
    """Finding 1: an OSError from as_server.start() (e.g. EADDRINUSE) must not
    propagate out of main() and kill the process before the ARI event loop
    starts -- extension 7000 never touches the AudioSocket listener and must
    keep being dispatched to CallHandler exactly as before."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FailingAudioSocketServer)

    call_handler_factory, call_handler_instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "CallHandler", call_handler_factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7000", "7000")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    # If the OSError from as_server.start() were left uncaught, this await
    # would raise and none of the assertions below would run.
    await main_mod.main()
    # main() dispatches via asyncio.create_task, which only schedules the
    # coroutine -- give it one loop iteration to actually run before
    # inspecting what it recorded.
    await asyncio.sleep(0)

    assert len(call_handler_instances) == 1
    assert call_handler_instances[0].calls == [("chan-7000", "7000", TURN_EXT)]
    # A turn-based call is dispatched to CallHandler, not rejected by main():
    # ari.answer/hangup here are only ever called by main() itself for calls
    # it rejects outright (unknown extension, or streaming with no listener).
    assert fake_ari.hungup == []
    assert fake_ari.answered == []


async def test_playback_finished_event_reaches_the_call_handler(monkeypatch):
    """The turn loop blocks on PlaybackFinished before it records. If main()
    does not route the event to the same PlaybackWaiter it handed the
    CallHandler, every prompt stalls until the timeout instead of failing
    visibly -- so assert the wiring, not just that the branch exists."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "CallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [
        {"type": "PlaybackFinished", "playback": {"id": "pb-xyz"}},
    ]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    # Registered before main() consumes the event, exactly as _speak does.
    waiters: list = []
    original = main_mod.PlaybackWaiter

    class CapturingPlaybackWaiter(original):
        def __init__(self):
            super().__init__()
            waiters.append(self)
            self._pending = self.expect("pb-xyz")

    monkeypatch.setattr(main_mod, "PlaybackWaiter", CapturingPlaybackWaiter)

    await main_mod.main()

    assert len(waiters) == 1
    assert waiters[0]._pending.done(), "PlaybackFinished never reached the waiter"
    assert waiters[0]._pending.result() is True
    # ...and it is the very same object the CallHandler was given.
    assert instances[0].init_kwargs["playbacks"] is waiters[0]


async def test_audiosocket_bind_failure_rejects_streaming_calls_cleanly(monkeypatch):
    """Finding 1, continued: once the listener has failed to bind, a call to
    a streaming-mode extension must be rejected cleanly (answered and hung
    up) rather than crashing main() or hanging the caller."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FailingAudioSocketServer)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()

    assert fake_ari.answered == ["chan-7001"]
    assert fake_ari.hungup == ["chan-7001"]


async def test_streaming_listener_binds_the_configured_port(monkeypatch):
    """Finding 2: ExtensionConfig.audiosocket_port must actually reach the
    AudioSocket listener bind. A non-default port (9099, not the module's old
    hardcoded 9010 constant) must show up as the bound port -- proving the
    value flows from config.yaml, not a constant that silently ignores it."""
    cfg = _cfg(streaming_port=9099)
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)

    servers: list[FakeAudioSocketServer] = []

    def _make_server(host, port):
        s = FakeAudioSocketServer(host, port)
        servers.append(s)
        return s

    monkeypatch.setattr(main_mod, "AudioSocketServer", _make_server)

    fake_ari = FakeAriClient()
    # No events needed -- the listener is built and started before the
    # event loop is entered, so an empty event list is sufficient here.
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()

    assert len(servers) == 1
    assert servers[0].port == 9099
    assert servers[0].host == main_mod.AUDIOSOCKET_LISTEN_HOST  # "0.0.0.0", not host.docker.internal
    assert servers[0].started


async def test_streaming_handler_gets_each_dependency_in_the_right_slot(monkeypatch):
    """Finding: swapping in RecordingHandler(*args) means reordering
    StreamingCallHandler's constructor arguments fails nothing -- the ARI
    client would arrive where the STT factory is expected and only show up on
    a live call. This binds what main() actually passed against the REAL
    signature and checks each parameter BY NAME, so both a reorder and a
    rename are caught here."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    sentinels = {
        "LiveStt": object(), "StreamTts": object(), "HermesClient": object(),
    }
    monkeypatch.setattr(main_mod, "LiveStt", lambda **kw: sentinels["LiveStt"])
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: sentinels["StreamTts"])
    monkeypatch.setattr(main_mod, "HermesClient",
                        lambda **kw: sentinels["HermesClient"])

    # Wraps the REAL FillerBank (not a fake) so this test also proves
    # filler_bank arrives by name, not merely that *some* value did -- a
    # None default on that parameter means a dropped kwarg would otherwise
    # bind silently and this test would not notice.
    filler_instances: list = []
    real_filler_bank = main_mod.FillerBank

    def _make_filler_bank(tiers):
        bank = real_filler_bank(tiers)
        filler_instances.append(bank)
        return bank

    monkeypatch.setattr(main_mod, "FillerBank", _make_filler_bank)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()

    assert len(instances) == 1
    built = instances[0]
    # Bind against the real class's signature, not the fake's *args.
    bound = inspect.signature(StreamingCallHandler.__init__).bind(
        None, *built.init_args, **built.init_kwargs
    )
    got = bound.arguments

    assert got["ari"] is fake_ari
    assert got["ari_app"] == cfg.ari.app
    assert isinstance(got["audiosocket_server"], FakeAudioSocketServer)
    assert got["audiosocket_server"].port == 9010
    # The two factories are callables, so a swap between them (or with `tts`)
    # is only visible in what they produce.
    assert got["tts"] is sentinels["StreamTts"]
    assert got["stt_factory"]() is sentinels["LiveStt"]
    assert got["hermes_factory"](cfg.extensions["7001"]) is sentinels["HermesClient"]
    assert len(filler_instances) == 1
    assert got["filler_bank"] is filler_instances[0]


async def test_a_dispatched_call_survives_garbage_collection(monkeypatch):
    """main() used to drop the create_task handle. asyncio holds only a weak
    reference to a running task, so a collection cycle at the wrong moment
    cancels a live call."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    started, released, finished = asyncio.Event(), asyncio.Event(), []

    class SlowHandler(RecordingHandler):
        async def handle(self, channel_id, extension, ext_cfg) -> None:
            started.set()
            await released.wait()
            finished.append(channel_id)

    monkeypatch.setattr(main_mod, "CallHandler", SlowHandler)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7000", "7000")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()
    await asyncio.wait_for(started.wait(), timeout=2.0)

    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0)

    # Matched by name, not "any live task": main() also spawns the background
    # speech-preparation task now, and a pending one of those would satisfy a
    # bare any(not t.done()) even if the CALL task had been collected --
    # which is the only thing this test exists to catch.
    call_tasks = [t for t in BACKGROUND_TASKS if t.get_name() == "call-chan-7000"]
    assert call_tasks and not call_tasks[0].done(), "the call task was dropped"
    released.set()
    await asyncio.wait_for(asyncio.gather(*call_tasks), timeout=2.0)
    assert finished == ["chan-7000"]


async def test_unrecognized_mode_falls_back_to_turn_with_a_warning(monkeypatch, caplog):
    """Minor finding: a typo'd mode (e.g. "stream") must not run the wrong
    pipeline unnoticed -- it still falls back to turn-based handling (default
    behaviour is unchanged) but now logs a warning naming the bad value."""
    bad_ext = ExtensionConfig(
        profile="phonebot", hermes_base_url="http://h", hermes_api_key="k",
        greeting="hi", voice_id="v", max_silence_seconds=1, max_turn_seconds=30,
        max_call_seconds=600, mode="stream",  # typo for "streaming"
    )
    ari_cfg = AriConfig(base_url="http://x", username="u", password="p", app="phonesys")
    cfg = Config(ari=ari_cfg, extensions={"7002": bad_ext})
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7002", "7002")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    with caplog.at_level("WARNING"):
        await main_mod.main()

    assert any("unrecognized mode" in r.message for r in caplog.records)
    # Not rejected: an unrecognized mode still gets turn-based service, it is
    # just logged loudly instead of silently guessed at.
    assert fake_ari.hungup == []


async def test_filler_bank_is_prepared_at_startup_and_passed_to_the_handler(monkeypatch):
    """Synthesis must happen once at startup, not per call: an ElevenLabs
    round-trip is most of the gap filler exists to cover.

    "At startup" now means "in a task spawned at startup" rather than "before
    main() reaches the ARI event loop" (see _prepare_streaming_speech), so
    this waits for that task -- what it proves is unchanged: one preparation,
    for the bank the handler was actually given, never one per call."""
    cfg = _cfg(filler=True)  # opts the "7001" extension into filler_delay_ms > 0
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    prepared: list[tuple] = []

    class SpyBank:
        def __init__(self, tiers):
            self.tiers = tiers

        async def prepare(self, tts, voice_id):
            prepared.append((tts, voice_id))

        def is_empty(self):
            return False

    monkeypatch.setattr(main_mod, "FillerBank", SpyBank)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: FakeAriClient())

    await main_mod.main()
    await _drain_preparation()

    assert len(prepared) == 1, "the bank must be prepared exactly once"
    assert isinstance(instances[0].init_kwargs["filler_bank"], SpyBank)


async def test_a_failing_filler_bank_does_not_stop_streaming(monkeypatch):
    """Filler is a nicety. ElevenLabs being down at startup must not take
    extension 7001 down with it -- the same isolation as a bind failure."""
    cfg = _cfg(filler=True)  # must be opted in, or prepare() is never called at all
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    class ExplodingBank:
        def __init__(self, tiers):
            pass

        async def prepare(self, tts, voice_id):
            raise RuntimeError("elevenlabs is down")

        def is_empty(self):
            return True

    monkeypatch.setattr(main_mod, "FillerBank", ExplodingBank)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()
    # Drained so prepare() has actually been reached and raised: preparation
    # is a background task now, and without this the failure being isolated
    # would never happen in the first place.
    await _drain_preparation()
    await asyncio.sleep(0)

    # The streaming call was still dispatched, not rejected.
    assert instances and instances[0].calls == [("chan-7001", "7001", cfg.extensions["7001"])]
    assert fake_ari.hungup == []


async def test_a_failing_filler_bank_still_lets_the_greeting_install(monkeypatch):
    """The inner filler-prepare guard (main.py's `try: await
    filler_bank.prepare(...) except Exception: ...`) has no test of its own
    anywhere in this file: test_a_failing_filler_bank_does_not_stop_streaming
    above only proves the CALL survives a filler failure, never what happens
    to the greeting synthesis sequenced in the same background task. It is
    reachable to delete without the suite noticing, because the autouse
    `_isolate` fixture (above) patches `StreamTts` to `lambda **kw:
    object()`, which has no `synthesize_frames` for the greeting call to
    reach -- every existing filler-failure test is blind to the greeting by
    construction. This one supplies a real FakeStreamTts so the greeting
    call has something to land on and be observed.

    Without its own guard, `filler_bank.prepare()` raising is not contained
    locally: it escapes to whichever backstop is next in the coroutine, and
    that backstop does not resume the coroutine where it left off -- so
    whatever was sequenced after the unguarded call never runs at all, not
    merely "runs without a result". `main.py:214-219`'s guard is what stops
    that from reaching the greeting synthesis/install."""
    cfg = _cfg(filler=True)  # must be opted in, or prepare() is never called at all
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    class ExplodingBank:
        def __init__(self, tiers):
            pass

        async def prepare(self, tts, voice_id):
            raise RuntimeError("elevenlabs is down")

        def is_empty(self):
            return True

    monkeypatch.setattr(main_mod, "FillerBank", ExplodingBank)

    fake_tts = FakeStreamTts()
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: fake_tts)
    spawned = _capture_spawned(monkeypatch)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: FakeAriClient())

    await main_mod.main()
    await _drain_preparation()

    ext = cfg.extensions["7001"]
    assert instances[0].greeting_installs != [], (
        "a filler failure must not prevent the greeting from being cached "
        "-- otherwise 7001 pays a per-call greeting round trip for the "
        "life of the process, not merely while the failure lasts"
    )
    assert instances[0].greeting_installs == \
        [[(ext.greeting.encode(), 0)] * fake_tts._n]
    # ...and the failure stayed inside the background task rather than
    # becoming a never-retrieved task exception.
    assert _prep_task(spawned).exception() is None


async def test_filler_disabled_extension_never_calls_prepare(monkeypatch):
    """The flip side of the two tests above: an extension that has not opted
    into filler (filler_delay_ms == 0, the ExtensionConfig default, and the
    only kind of extension every other test in this file uses) must not pay
    for a synthesis round-trip it will never use -- see _start_streaming's
    "not configured" branch."""
    cfg = _cfg()  # filler=False by default: nothing has opted in
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    prepared: list[tuple] = []

    class SpyBank:
        def __init__(self, tiers):
            self.tiers = tiers

        async def prepare(self, tts, voice_id):
            prepared.append((tts, voice_id))

        def is_empty(self):
            return True

    monkeypatch.setattr(main_mod, "FillerBank", SpyBank)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: FakeAriClient())

    await main_mod.main()
    # Drained, not merely "main() returned": preparation is a background task
    # now, so without this the assertion below would hold trivially -- it
    # would be checking that prepare() had not been reached YET, not that it
    # is never called.
    await _drain_preparation()

    assert prepared == [], "filler_delay_ms == 0 must skip prepare() entirely"
    # The bank is still constructed and passed through -- just never
    # prepared. An empty, unprepared bank behaves as "no filler", same as
    # None (see StreamingCallHandler.__init__).
    assert isinstance(instances[0].init_kwargs["filler_bank"], SpyBank)


async def test_greeting_is_prepared_at_startup_and_passed_to_the_handler(monkeypatch):
    """Task #3: synthesis must happen once at startup, not per call -- an
    ElevenLabs round trip is 400-600ms (see filler.py's docstring) that the
    caller would otherwise pay for on every single call while already
    connected. Unlike filler this is unconditional: the greeting is not
    opt-in, so there is no "not configured" branch to skip it under.

    The frames now reach the handler through set_greeting_frames() after
    construction rather than as a constructor argument, because the synthesis
    they come from is no longer awaited before the ARI event loop starts. The
    claim is otherwise identical: synthesized exactly once, and the resulting
    frames are what the handler's greeting cache ends up holding."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    fake_tts = FakeStreamTts()
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: fake_tts)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: FakeAriClient())

    await main_mod.main()
    await _drain_preparation()

    ext = cfg.extensions["7001"]
    assert fake_tts.calls == [(ext.greeting, ext.voice_id, 0)], \
        "the greeting must be synthesized exactly once at startup"
    assert instances[0].greeting_installs == \
        [[(ext.greeting.encode(), 0)] * fake_tts._n], \
        "the startup-synthesized frames must be installed on the handler once"


async def test_a_failing_greeting_synthesis_does_not_stop_streaming(monkeypatch):
    """The greeting cache is a nicety layered on top of a call that already
    works without it -- ElevenLabs being down at startup must not take
    extension 7001 down with it, same isolation as the filler bank and the
    bind failure. The handler's greeting cache must stay None so _speak()
    falls back to per-call synthesis (exercised end-to-end, not just on a
    mock, in test_stream_call.py's
    test_startup_synthesis_failure_falls_back_to_per_call_synthesis)."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: ExplodingStreamTts())
    spawned = _capture_spawned(monkeypatch)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()
    await _drain_preparation()
    await asyncio.sleep(0)

    # The streaming call was still dispatched, not rejected.
    assert instances and instances[0].calls == [("chan-7001", "7001", cfg.extensions["7001"])]
    assert fake_ari.hungup == []
    assert instances[0].greeting_installs == [], \
        "a failed synthesis must leave the cache unset, not install junk"
    assert instances[0].greeting_frames is None
    # ...and the failure stayed inside the background task rather than
    # becoming a never-retrieved task exception.
    assert _prep_task(spawned).exception() is None


async def test_an_empty_greeting_synthesis_falls_back_instead_of_poisoning_the_cache(
    monkeypatch,
):
    """`set_greeting_frames([])` would still pass `_speak`'s `cached is not
    None` check (stream_call.py:586) and then send nothing -- silently
    poisoning the greeting cache for the rest of the process, on every call,
    forever, rather than degrading once. A whitespace-only `greeting` in
    config is a real way to reach this: ElevenLabs returns audio for it,
    just zero frames of it, so this is a "successful" synthesis, not the
    exception case test_a_failing_greeting_synthesis_does_not_stop_streaming
    covers.

    The cache must stay unset here exactly as it does on a raised exception,
    so _speak() falls back to synthesizing the greeting per call instead of
    going permanently mute."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: EmptyFramesStreamTts())
    spawned = _capture_spawned(monkeypatch)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()
    await _drain_preparation()
    await asyncio.sleep(0)

    # The streaming call was still dispatched, not rejected.
    assert instances and instances[0].calls == \
        [("chan-7001", "7001", cfg.extensions["7001"])]
    assert fake_ari.hungup == []
    assert instances[0].greeting_installs == [], (
        "an empty synthesis result must not be installed -- it would pass "
        "_speak's `cached is not None` check and then send silence forever"
    )
    assert instances[0].greeting_frames is None
    assert _prep_task(spawned).exception() is None


async def test_a_failing_greeting_synthesis_still_lets_filler_prepare(monkeypatch):
    """The mirror image of
    test_a_failing_filler_bank_still_lets_the_greeting_install: the greeting
    is now synthesized FIRST (see main.py's _prepare_streaming_speech), so
    it is the greeting's OWN inner guard that is load-bearing for filler
    preparation actually running, not the other way around. Without it, a
    greeting failure would escape to the outer backstop and filler
    preparation -- sequenced after the greeting in the same coroutine --
    would never even be attempted, silently regressing every 7001 call to
    "no filler" for the life of the process rather than just the one
    transient ElevenLabs failure.

    Uses a real SpyBank (as
    test_filler_bank_is_prepared_at_startup_and_passed_to_the_handler does)
    so "filler preparation was attempted" is observed directly, not inferred
    from is_empty()."""
    cfg = _cfg(filler=True)  # must be opted in, or prepare() is never called at all
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: ExplodingStreamTts())

    prepared: list[tuple] = []

    class SpyBank:
        def __init__(self, tiers):
            self.tiers = tiers

        async def prepare(self, tts, voice_id):
            prepared.append((tts, voice_id))

        def is_empty(self):
            return False

    monkeypatch.setattr(main_mod, "FillerBank", SpyBank)
    spawned = _capture_spawned(monkeypatch)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()
    await _drain_preparation()
    await asyncio.sleep(0)

    # The streaming call was still dispatched, not rejected.
    assert instances and instances[0].calls == \
        [("chan-7001", "7001", cfg.extensions["7001"])]
    assert fake_ari.hungup == []
    assert instances[0].greeting_installs == [], "the failed synthesis must not install junk"
    assert len(prepared) == 1, (
        "a greeting failure must not prevent filler from being prepared -- "
        "otherwise 7001 loses filler for the life of the process, not just "
        "while the failure lasts"
    )
    assert _prep_task(spawned).exception() is None


# --------------------------------------------------------------------------
# Startup speech synthesis must not hold up the ARI event loop.
#
# Extension 7000 is turn-based, already live, and uses neither filler nor the
# streaming greeting cache. Awaiting ~1.6s of ElevenLabs before `async for
# event in ari.events()` meant 7000 could not accept a call for that window,
# for a feature it does not have.
# --------------------------------------------------------------------------

async def test_ari_event_loop_serves_7000_while_startup_synthesis_is_pending(
    monkeypatch,
):
    """The headline requirement. Synthesis is pinned open (SlowStreamTts never
    returns until released), and a StasisStart for extension 7000 must still
    be consumed and dispatched.

    Revert the change and this fails rather than hangs: main() sits inside
    _start_streaming awaiting synthesis, never reaches ari.events(), and
    wait_for times out.
    """
    cfg = _cfg(filler=True)  # the real shape: filler phrases AND the greeting
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    slow_tts = SlowStreamTts()
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: slow_tts)
    spawned = _capture_spawned(monkeypatch)

    call_handler_factory, call_handlers = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "CallHandler", call_handler_factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7000", "7000")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await asyncio.wait_for(main_mod.main(), timeout=2.0)
    await asyncio.sleep(0)

    # The event loop ran and the turn-based call was dispatched...
    assert len(call_handlers) == 1
    assert call_handlers[0].calls == [("chan-7000", "7000", TURN_EXT)]
    assert fake_ari.hungup == [] and fake_ari.answered == []
    # ...with synthesis demonstrably still outstanding, so this cannot pass by
    # the fake simply having been fast.
    assert slow_tts.entered.is_set(), "preparation never started"
    assert not _prep_task(spawned).done(), \
        "synthesis had already finished; this test proved nothing"

    slow_tts.release.set()
    await _drain_preparation()


async def test_a_7001_call_before_preparation_finishes_is_served_without_cache(
    monkeypatch,
):
    """A streaming call landing mid-preparation must be served, not delayed
    and not rejected -- on the degraded path that already existed for an
    ElevenLabs outage: an empty (unprepared) filler bank, and no cached
    greeting, so _speak() synthesizes it for that call.

    The filler bank here is the REAL FillerBank, not a spy, because the claim
    is about what an unprepared bank reports to the handler (is_empty()), not
    about whether main() called something."""
    cfg = _cfg(filler=True)
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    slow_tts = SlowStreamTts()
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: slow_tts)
    spawned = _capture_spawned(monkeypatch)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await asyncio.wait_for(main_mod.main(), timeout=2.0)
    await asyncio.sleep(0)

    assert not _prep_task(spawned).done(), "preparation was not still pending"
    # Dispatched to the streaming handler, not answered-and-hung-up.
    assert instances[0].calls == [("chan-7001", "7001", cfg.extensions["7001"])]
    assert fake_ari.hungup == [] and fake_ari.answered == []
    # No filler: an unprepared bank is an empty bank, which the handler
    # already treats as "filler disabled".
    bank = instances[0].init_kwargs["filler_bank"]
    assert isinstance(bank, main_mod.FillerBank)
    assert bank.is_empty()
    assert bank.take(0) is None
    # No cached greeting: _speak() falls back to per-call synthesis.
    assert instances[0].greeting_frames is None

    slow_tts.release.set()
    await _drain_preparation()


async def test_preparation_finishing_leaves_the_handler_ready_with_filler_and_the_cache(
    monkeypatch,
):
    """The other side of the same coin: once the background task lands, the
    state a LATER call would read is exactly what the blocking version used
    to provide. No call is actually dispatched in this test -- fake_ari
    yields no StasisStart events -- so this checks the handler's own state
    directly rather than a call outcome. Same real FillerBank instance the
    handler was handed at construction -- prepare() mutates it in place, so
    nothing has to be re-plumbed for filler; the greeting, being a snapshot
    value, arrives via set_greeting_frames."""
    cfg = _cfg(filler=True)
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    fake_tts = FakeStreamTts()
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: fake_tts)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()
    await _drain_preparation()

    ext = cfg.extensions["7001"]
    handler = instances[0]
    bank = handler.init_kwargs["filler_bank"]
    # Filler: the very bank the handler already holds now has audio, without
    # main() handing it over a second time.
    assert not bank.is_empty()
    assert bank.take(0) == [(ext.filler_phrases[0][0].encode(), -1)] * fake_tts._n
    # Greeting: installed on the handler, exactly the startup-synthesized
    # frames, exactly once.
    assert handler.greeting_installs == [[(ext.greeting.encode(), 0)] * fake_tts._n]


async def test_background_preparation_failure_is_logged_and_never_escapes(
    monkeypatch, caplog,
):
    """Preparation is a detached task, so an exception escaping it is not
    merely unhandled -- it resurfaces at garbage-collection time as "Task
    exception was never retrieved" and can take the process's logs with it.
    This drives a failure through the OUTER backstop specifically (installing
    the frames blows up, which is outside both per-operation guards) and
    proves the task still ends cleanly and says why."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)
    monkeypatch.setattr(main_mod, "StreamTts", lambda **kw: FakeStreamTts())
    spawned = _capture_spawned(monkeypatch)

    class BrokenInstallHandler(RecordingHandler):
        def set_greeting_frames(self, frames):
            raise RuntimeError("wiring bug in the greeting install")

    factory, instances = _capturing_factory(BrokenInstallHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    with caplog.at_level("ERROR"):
        await main_mod.main()
        await _drain_preparation()
    await asyncio.sleep(0)

    task = _prep_task(spawned)
    assert task.done() and task.exception() is None, \
        "the preparation task must never end with an exception"
    assert any("background preparation of streaming speech failed" in r.message
               for r in caplog.records), "the failure was swallowed silently"
    # ...and the process kept serving calls.
    assert instances[0].calls == [("chan-7001", "7001", cfg.extensions["7001"])]
    assert fake_ari.hungup == []
