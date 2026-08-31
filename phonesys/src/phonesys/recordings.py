"""Bridges ARI recording events into the per-call turn loop.

ARI's POST /channels/{id}/record returns 201 as soon as recording *starts*.
The turn loop must wait for the RecordingFinished event before reading the
file, otherwise it reads a missing or partial recording -- which reaches
Deepgram as zero bytes and comes back HTTP 400.
"""
from __future__ import annotations

from phonesys.waiters import EventWaiter


class RecordingWaiter(EventWaiter):
    """Waits for Recording{Finished,Failed}, keyed by recording name."""
