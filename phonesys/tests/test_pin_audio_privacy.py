"""Offline PIN privacy regressions using real session queues and DTMF decoding.

All tones and credentials here are synthetic; no provider or PBX is contacted.
"""
import asyncio
from types import SimpleNamespace

import pytest

from phonesys.audiosocket import AudioSocketSession
from phonesys.stream_call import StreamingCallHandler
from phonesys.waiters import DtmfCollector
from test_dtmf import _tone_frames


def handler_with_prompt(prompt, collector=None):
    handler = object.__new__(StreamingCallHandler)
    handler._dtmf = collector if collector is not None else DtmfCollector()
    handler._speak = prompt
    return handler


def protected_ext():
    return SimpleNamespace(access_pin="112233", profile="synthetic")


async def drain(audio):
    return [frame async for frame in audio.frames()]


@pytest.mark.parametrize("active_consumer", [False, True], ids=["direct", "transfer"])
async def test_real_session_pin_never_reaches_stt(active_consumer):
    audio = AudioSocketSession("synthetic", None)
    # Already received before authentication (e.g. media settle window).
    audio.deliver(b"pre-auth-private-pcm")
    received = []

    async def consume():
        async for frame in audio.frames():
            received.append(frame)

    async def prompt(*args):
        for frame in _tone_frames("112233"):
            audio.deliver(frame)
            await asyncio.sleep(0)
        # Include non-tone audio after digit collection finishes, before the
        # prompt returns: the entire auth window, not just digits, is private.
        audio.deliver(b"\x01\x00" * 160)
        await asyncio.sleep(0)

    handler = handler_with_prompt(prompt)
    ext = protected_ext()
    # Start auth first so the consumer cannot legitimately forward pre-window
    # audio before the privacy boundary has been established.
    auth = asyncio.create_task(handler._authenticate_pin(audio, ext, ext, "caller"))
    consumer = asyncio.create_task(consume()) if active_consumer else None
    assert await asyncio.wait_for(auth, 1) is True  # real local decoder succeeds
    assert not handler._dtmf._queues
    assert not audio._inbound_listeners
    audio.deliver(b"normal-after-auth")
    audio.close()
    if consumer:
        await asyncio.wait_for(consumer, 1)
    else:
        received = await asyncio.wait_for(drain(audio), 1)
    assert len(received) == 1, "pre/auth-window PCM escaped to the STT consumer"
    assert received[0] == b"normal-after-auth"


@pytest.mark.parametrize("outcome", ["wrong", "cancel", "prompt_error", "missing_collector"])
async def test_unsuccessful_auth_discards_pcm_and_cleans_up(outcome):
    audio = AudioSocketSession("synthetic", None)
    audio.deliver(b"pre-auth-private-pcm")
    entered = asyncio.Event()
    attempts = 0

    async def prompt(*args):
        nonlocal attempts
        attempts += 1
        audio.deliver(b"\x01\x00" * 160)
        entered.set()
        if outcome == "cancel":
            await asyncio.Event().wait()
        if outcome == "prompt_error":
            raise RuntimeError("synthetic prompt failure")
        for frame in _tone_frames("445566"):
            audio.deliver(frame)
            await asyncio.sleep(0)

    handler = handler_with_prompt(prompt)
    collector = handler._dtmf
    if outcome == "missing_collector":
        handler._dtmf = None
    ext = protected_ext()
    auth = asyncio.create_task(handler._authenticate_pin(audio, ext, ext, "caller"))
    if outcome == "cancel":
        await asyncio.wait_for(entered.wait(), 1)
        auth.cancel()
        with pytest.raises(asyncio.CancelledError):
            await auth
    elif outcome == "prompt_error":
        with pytest.raises(RuntimeError, match="synthetic prompt failure"):
            await asyncio.wait_for(auth, 1)
    else:
        assert await asyncio.wait_for(auth, 1) is False
        assert attempts == (3 if outcome == "wrong" else 0)
    assert not audio._inbound_listeners
    assert not collector._queues
    assert not collector._audio_detectors
    # On a failed transfer the receptionist must be able to listen again.
    audio.deliver(b"normal-after-auth")
    audio.close()
    received = await asyncio.wait_for(drain(audio), 1)
    assert len(received) == 1, "failed/cancelled authentication retained private PCM"
    assert received[0] == b"normal-after-auth"


@pytest.mark.parametrize("close_before_auth", [False, True])
async def test_auth_queue_clear_preserves_eof(close_before_auth):
    audio = AudioSocketSession("synthetic", None)
    audio.deliver(b"pre-auth-private-pcm")
    if close_before_auth:
        audio.close()

    async def prompt(*args):
        audio.deliver(b"\x01\x00" * 160)
        if not close_before_auth:
            audio.close()
        raise RuntimeError("synthetic closed prompt")

    handler = handler_with_prompt(prompt)
    ext = protected_ext()
    with pytest.raises(RuntimeError, match="synthetic closed prompt"):
        await handler._authenticate_pin(audio, ext, ext, "caller")
    assert not handler._dtmf._queues
    assert not audio._inbound_listeners
    assert await asyncio.wait_for(drain(audio), 1) == []


async def test_collection_timeout_discards_audio_and_removes_listener():
    class ShortTimeoutCollector(DtmfCollector):
        async def collect(self, channel_id, length, timeout=15.0):
            return await super().collect(channel_id, length, timeout=0.001)

    audio = AudioSocketSession("synthetic", None)
    audio.deliver(b"pre-auth-private-pcm")
    attempts = 0

    async def prompt(*args):
        nonlocal attempts
        attempts += 1
        audio.deliver(bytes(320))

    collector = ShortTimeoutCollector()
    handler = handler_with_prompt(prompt, collector)
    ext = protected_ext()
    assert await asyncio.wait_for(handler._authenticate_pin(audio, ext, ext, "caller"), 1) is False
    assert attempts == 3
    assert not collector._queues
    assert not collector._audio_detectors
    assert not audio._inbound_listeners
    audio.close()
    assert await asyncio.wait_for(drain(audio), 1) == []


async def test_no_pin_preserves_normal_queued_and_live_audio():
    audio = AudioSocketSession("synthetic", None)
    audio.deliver(b"normal-before")

    async def unexpected_prompt(*args):
        raise AssertionError("unprotected call prompted for a PIN")

    handler = handler_with_prompt(unexpected_prompt)
    ext = protected_ext()
    ext.access_pin = ""
    assert await handler._authenticate_pin(audio, ext, ext, "caller") is True
    audio.deliver(b"normal-after")
    audio.close()
    assert await asyncio.wait_for(drain(audio), 1) == [b"normal-before", b"normal-after"]


async def test_local_listener_failure_cannot_leak_auth_audio_or_exception(caplog):
    audio = AudioSocketSession("synthetic", None)

    def failing_listener(pcm):
        raise ValueError("synthetic-private-listener-detail")

    audio.add_inbound_listener(failing_listener)

    async def prompt(*args):
        for frame in _tone_frames("112233"):
            audio.deliver(frame)

    handler = handler_with_prompt(prompt)
    ext = protected_ext()
    assert await handler._authenticate_pin(audio, ext, ext, "caller") is True
    assert audio._inbound_listeners == {failing_listener}
    audio.close()
    assert await asyncio.wait_for(drain(audio), 1) == []
    assert "synthetic-private-listener-detail" not in caplog.text
    assert "audiosocket inbound listener failed" in caplog.text


async def test_protected_auth_requires_audio_privacy_boundary():
    async def prompt(*args):
        for digit in "112233":
            handler._dtmf.feed("caller", digit)

    handler = handler_with_prompt(prompt)
    ext = protected_ext()
    # A future session adapter must not silently bypass audio privacy.
    with pytest.raises(AttributeError, match="suppress_inbound_audio"):
        await handler._authenticate_pin(object(), ext, ext, "caller")


async def test_real_transfer_pump_sends_only_normal_audio():
    # Exercise handle()'s actual background pump, not merely a queue consumer.
    from test_stream_call import (
        FakeHermes, FakeServer, FakeStt, _ext, _final, _handler, _until, _utt_end,
    )

    audio = AudioSocketSession("synthetic", None)
    stt = FakeStt()
    mary_hermes, atlas_hermes = FakeHermes(), FakeHermes()
    mary = _ext(profile="phonebot", full_name="Mary Yoder", receptionist=True)
    atlas = _ext(profile="atlas", full_name="Atlas Assistant", directory_listing=True,
                 access_pin="112233", greeting="Synthetic target greeting.")
    collector = DtmfCollector()
    handler = _handler(
        stt=stt, server=FakeServer(audio), dtmf=collector,
        extensions={"7001": mary, "7003": atlas},
        hermes_factory=lambda ext: atlas_hermes if ext.profile == "atlas" else mary_hermes,
    )
    saw_auth = False

    async def prompt(session, ext, sentences, **kwargs):
        nonlocal saw_auth
        text = " ".join(sentences)
        if text == mary.greeting:
            audio.deliver(b"normal-before-transfer")
            await _until(lambda: len(stt.sent) == 1)
            stt.emit(_final("I need Atlas."))
            stt.emit(_utt_end())
        elif "telephone keypad" in text:
            saw_auth = True
            assert not atlas_hermes.sessions
            for frame in _tone_frames("112233"):
                audio.deliver(frame)
                await asyncio.sleep(0)
            audio.deliver(b"\x01\x00" * 160)
            await asyncio.sleep(0)
            assert len(stt.sent) == 1, "running transfer pump forwarded authentication PCM"
        elif text == atlas.greeting:
            audio.deliver(b"normal-after-transfer")
            await _until(lambda: len(stt.sent) >= 2)
            audio.close()

    handler._speak = prompt
    await asyncio.wait_for(handler.handle("synthetic-transfer", "7001", mary), 2)
    assert saw_auth
    assert atlas_hermes.sessions == ["call-synthetic-transfer-7003"]
    assert stt.sent == [b"normal-before-transfer", b"normal-after-transfer"]
    assert not collector._queues
    assert not audio._inbound_listeners
