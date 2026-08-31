"""Per-call turn loop. The only module that knows about turn-taking.

Turn detection is delegated to Asterisk via maxSilenceSeconds on the recording,
so there is no VAD here. Hand-rolled voice activity detection is a common
source of bad turn-taking and Asterisk already does it well.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import httpx

from phonesys.ari import AriError
from phonesys.config import ExtensionConfig
from phonesys.hermes import HermesError
from phonesys.pipeline import run_turn
from phonesys.recordings import RecordingWaiter
from phonesys.tts import TtsError
from phonesys.waiters import PlaybackWaiter

logger = logging.getLogger(__name__)

# Consecutive silent turns tolerated before the agent asks whether anyone is
# still there, and again before it gives up. At max_silence_seconds=1 a silent
# turn costs roughly 1.5s, so this is about 6s of quiet either side of the
# prompt.
#
# It is deliberately not 2. One silent turn is STRUCTURAL: recording starts the
# instant playback ends, and a caller who takes a breath before answering
# spends the whole first window saying nothing. A budget of 2 therefore gave
# real callers about 1.5s of hesitation before the line dropped -- and gave a
# caller who talked over the agent none at all, since 7000 is half-duplex and
# cannot record while it plays, so those words are never heard.
SILENT_TURNS_BEFORE_PROMPT = 4

# Asked once per lull, not once per call: a caller who answers and later goes
# quiet again is asked again rather than dropped.
STILL_THERE_PROMPT = "Are you still there?"
# Said aloud when the turn pipeline dies on a Hermes or transport failure.
# Deliberately a copy of stream_call.HERMES_FAILURE_MESSAGE rather than an
# import of it: this module is extension 7000, the live turn-based path, and
# it must not acquire a dependency on the streaming handler (and through it on
# audiosocket/websockets) to say one sentence.
TURN_FAILURE_MESSAGE = "Sorry, I lost my train of thought there. Could you say that again?"
# Consecutive failed turns tolerated before the call is ended.
#
# The arm below exists for a BLIP -- one stalled /chat that the next request
# survives -- and its `continue` deliberately bypasses the silence budget, so
# nothing else in this loop can ever stop it. A provider that is genuinely
# down therefore loops it for the whole max_call_seconds (600s live): the
# caller holds an open, silent line for ten minutes while every turn's
# apology fails to synthesize too. run_turn's TTS leg is the easiest way in
# -- a transport error out of ElevenLabs is an httpx.HTTPError like any
# other, so an outage that returns HTTP 500 drops the call while the same
# outage refusing connections loops it.
#
# 3, not 2: a real blip must still cost a turn rather than the call, and one
# retry after it is what proves the line recovered. Bounded either way, which
# is the point.
MAX_CONSECUTIVE_TURN_FAILURES = 3
# Grace on top of max_turn_seconds before giving up on a RecordingFinished.
RECORDING_GRACE_SECONDS = 10
# Grace on top of the audio's own duration before giving up on a
# PlaybackFinished. The caller may hang up mid-prompt, in which case the event
# never arrives and waiting forever would hold the channel to max_call_seconds.
PLAYBACK_GRACE_SECONDS = 10
# Replies are synthesised as 8 kHz mono mu-law (ulaw_8000), i.e. one byte per
# sample, so the byte count *is* the duration in samples.
ULAW_BYTES_PER_SECOND = 8000


class CallHandler:
    def __init__(self, ari, stt, tts, hermes_factory, sounds_dir: Path, rec_dir: Path,
                 recordings: RecordingWaiter | None = None,
                 playbacks: PlaybackWaiter | None = None) -> None:
        self._ari = ari
        self._stt = stt
        self._tts = tts
        self._hermes_factory = hermes_factory
        self._sounds = Path(sounds_dir)
        self._rec = Path(rec_dir)
        self._recordings = recordings if recordings is not None else RecordingWaiter()
        self._playbacks = playbacks if playbacks is not None else PlaybackWaiter()

    async def _speak(self, channel_id: str, audio: bytes) -> None:
        """Write mu-law audio where Asterisk can see it, play it to completion,
        then remove it.

        The file must be group-readable: the bridge writes as the host user
        while Asterisk runs as uid 999 / gid 1000.

        This waits for PlaybackFinished rather than returning when playback
        merely starts. The turn loop records straight after speaking, and a
        recorder running while the caller is still listening charges the
        agent's own talking time to the caller as silence -- two of those in a
        row end the call.
        """
        name = f"reply-{uuid.uuid4().hex[:12]}"
        path = self._sounds / f"{name}.ulaw"
        path.write_bytes(audio)
        # This try covers EVERYTHING after the file exists, so no exit path can
        # leave it behind. It used to wrap only the PlaybackFinished wait, which
        # meant a play() that raised -- a 404 from a caller who hung up
        # mid-prompt is the documented normal case, see handle() below -- leaked
        # a ~tens-of-KB .ulaw into the sounds mount that nothing ever sweeps.
        try:
            path.chmod(0o644)
            # Registered before the REST call: ARI returns 201 when playback
            # BEGINS and PlaybackFinished can land immediately after.
            playback_id = f"pb-{uuid.uuid4().hex[:12]}"
            finished = self._playbacks.expect(playback_id)
            try:
                await self._ari.play(channel_id, f"sound:phonesys/{name}", playback_id)
            except BaseException:
                self._playbacks.discard(playback_id)
                raise
            try:
                timeout = len(audio) / ULAW_BYTES_PER_SECOND + PLAYBACK_GRACE_SECONDS
                await asyncio.wait_for(finished, timeout=timeout)
            except asyncio.TimeoutError:
                # Not fatal: carry on with the turn loop rather than stranding
                # the caller on a silent line.
                #
                # TimeoutError ONLY. CancelledError must propagate: swallowing a
                # cancellation at an await point loses it permanently and drops
                # control back to the top of handle()'s turn loop, which then
                # POSTs a new /channels/{id}/record after the operator has
                # already asked the process to stop -- and then blocks for
                # max_turn_seconds + RECORDING_GRACE_SECONDS waiting for an
                # event nobody is left to deliver. Ctrl-C during a reply cost
                # ~40s of hang with the shipped config.
                logger.warning("call %s: playback %s never finished", channel_id, playback_id)
            finally:
                # Deregister on every exit, cancellation included: resolve()
                # already popped it on the happy path, so this is a no-op there
                # and the only thing that stops a dropped waiter accumulating
                # otherwise.
                self._playbacks.discard(playback_id)
        finally:
            path.unlink(missing_ok=True)

    async def handle(self, channel_id: str, extension: str, ext: ExtensionConfig) -> None:
        hermes = self._hermes_factory(ext)
        session_id = f"call-{channel_id}"
        silence_streak = 0
        prompted = False
        failure_streak = 0
        try:
            await self._ari.answer(channel_id)
            await hermes.create_session(session_id)
            await self._speak(channel_id, await self._tts.synthesize(ext.greeting, ext.voice_id))

            loop = asyncio.get_running_loop()
            deadline = loop.time() + ext.max_call_seconds
            while loop.time() < deadline:
                name = f"turn-{uuid.uuid4().hex[:12]}"
                # Register before starting: ARI returns 201 when recording
                # BEGINS, and the finished event can arrive immediately after.
                finished = self._recordings.expect(name)
                try:
                    await self._ari.record(
                        channel_id, name, ext.max_silence_seconds, ext.max_turn_seconds
                    )
                except AriError as exc:
                    self._recordings.discard(name)
                    if exc.status_code == 404:
                        # The channel is gone: the caller hung up. Ordinary end
                        # of call, not a failure -- do not log a traceback.
                        logger.info("call %s: caller hung up", channel_id)
                        break
                    raise
                try:
                    ok = await asyncio.wait_for(
                        finished, timeout=ext.max_turn_seconds + RECORDING_GRACE_SECONDS
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._recordings.discard(name)
                    logger.warning("call %s: recording %s never finished", channel_id, name)
                    break

                # A failed recording is NOT silence. Treating it as silence
                # makes a permissions or disk problem look like a caller who
                # said nothing, which is near-impossible to diagnose by ear.
                if not ok:
                    logger.error(
                        "call %s: recording %s FAILED. Asterisk could not write it -- "
                        "check that the recording directory is writable by the asterisk "
                        "user (uid 999/gid 1000), not just by root.",
                        channel_id, name,
                    )
                    break

                wav = self._rec / f"{name}.wav"
                audio = wav.read_bytes() if wav.exists() else b""
                wav.unlink(missing_ok=True)

                try:
                    result = await run_turn(
                        self._stt, hermes, self._tts, session_id, audio, ext
                    )
                except (HermesError, httpx.HTTPError) as exc:
                    # A single stalled Hermes request must not kill the call.
                    # Live evidence: /chat was issued at 15:24:34, Hermes never
                    # answered, httpx.ReadTimeout fired 64s later and this
                    # handler's generic `except Exception` logged a traceback
                    # and hung up -- with the caller hearing nothing for the
                    # whole 64s. Hermes was not even down: another call chatted
                    # successfully while that one hung.
                    #
                    # So: apologize aloud and go back to listening, the same
                    # thing extension 7001 already does (stream_call.py's
                    # HermesError/httpx.HTTPError arm in _reply). The exception
                    # types are what run_turn can actually raise from the
                    # Hermes/transport half of the pipeline -- HermesError for
                    # any status >= 400, and httpx's own errors (ReadTimeout,
                    # ConnectError, ...) unwrapped, since neither hermes.py nor
                    # stt.py wraps them. NOT bare Exception: a TypeError in
                    # this file is a bug that must still surface, not something
                    # to apologize for on every turn.
                    failure_streak += 1
                    if failure_streak >= MAX_CONSECUTIVE_TURN_FAILURES:
                        # Past this point "recoverable blip" is no longer a
                        # description of what is happening, and the apology
                        # cannot be spoken either -- whatever broke run_turn
                        # breaks the apology's own synthesis on the way out.
                        # Ending the call is the only outcome that does not
                        # leave the caller holding a silent line. ERROR, not
                        # warning: nothing above logs above WARNING on this
                        # path, so an outage that ate a whole call currently
                        # leaves no error in the log at all.
                        logger.error(
                            "call %s: %d consecutive failed turns (%s: %s); "
                            "ending the call",
                            channel_id, failure_streak, type(exc).__name__, exc,
                        )
                        break
                    logger.warning(
                        "call %s: turn failed (%s: %s); apologizing and "
                        "returning to listening",
                        channel_id, type(exc).__name__, exc,
                    )
                    try:
                        await self._speak(
                            channel_id,
                            await self._tts.synthesize(TURN_FAILURE_MESSAGE, ext.voice_id),
                        )
                    except (TtsError, httpx.HTTPError) as apology_exc:
                        # The apology's own synthesis can fail too, and a
                        # failed apology must not turn a recoverable blip into
                        # a dropped call -- which is the entire point of this
                        # arm. An AriError is deliberately NOT caught here: a
                        # 404 means the caller hung up, which handle()'s outer
                        # arm already reads correctly.
                        logger.warning("call %s: could not speak the failure "
                                       "message: %s", channel_id, apology_exc)
                    # Reset, not left alone: run_turn reaches Hermes and TTS
                    # only AFTER transcribing something, so on the usual way
                    # into this arm the caller demonstrably spoke this turn.
                    # Carrying the streak forward lets the call end on
                    # "no answer to 'Are you still there?' after N more silent
                    # turns" -- a log line that is simply false about a caller
                    # who was talking. `prompted` is deliberately left as it
                    # was: a failed turn is no evidence the lull it was asked
                    # about is over.
                    silence_streak = 0
                    continue

                if not result.transcript:
                    silence_streak += 1
                    if silence_streak >= SILENT_TURNS_BEFORE_PROMPT:
                        if prompted:
                            logger.info(
                                "call %s: no answer to %r after %d more silent "
                                "turns; ending the call",
                                channel_id, STILL_THERE_PROMPT, silence_streak,
                            )
                            break
                        logger.info(
                            "call %s: %d silent turns; asking whether the "
                            "caller is still there", channel_id, silence_streak,
                        )
                        prompted = True
                        silence_streak = 0
                        await self._speak(
                            channel_id,
                            await self._tts.synthesize(STILL_THERE_PROMPT, ext.voice_id),
                        )
                    continue

                # A turn that reached Hermes is what clears the failure
                # budget -- not merely a run_turn call that returned without
                # raising. run_turn returns successfully on an empty
                # transcript too, without ever touching Hermes or TTS (see
                # pipeline.run_turn), and the except arm above always
                # `continue`s past the silence budget. Resetting up there
                # unconditionally let a caller interleave one silent turn
                # between failures and clear the cap every other turn,
                # reproducing -- with the cap in place -- the exact 600s
                # silent-line symptom the cap exists to prevent.
                failure_streak = 0

                # A real answer clears the prompt as well as the streak, so a
                # later lull is prompted again rather than ending the call.
                silence_streak = 0
                prompted = False
                logger.info("call %s: %r -> %r", channel_id, result.transcript, result.reply)
                if result.audio:
                    await self._speak(channel_id, result.audio)
        except AriError as exc:
            # A 404 anywhere (greeting, playback, answer) means the same thing:
            # the caller hung up mid-operation. Not worth a traceback.
            if exc.status_code == 404:
                logger.info("call %s: caller hung up", channel_id)
            else:
                logger.exception("call %s failed", channel_id)
        except Exception:
            logger.exception("call %s failed", channel_id)
        finally:
            await self._ari.hangup(channel_id)
            await hermes.aclose()
