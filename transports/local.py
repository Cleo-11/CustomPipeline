"""transports/local.py — In-memory Transport for tests and replay.

Feed it TransportEvents with feed(); it records every outbound call in
`ops` as plain tuples so tests can assert exact play/clear/checkpoint
sequences. No pacing, no wire protocol — this is the proof that the session
is carrier-free.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from runtime.types import (
    MULAW_8K,
    AudioFormat,
    AudioFrame,
    CallEnded,
    TransportEvent,
)


class LocalTransport:
    """Scripted transport: events in via a queue, operations out via a list."""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[TransportEvent] = asyncio.Queue()
        self.ops: list[tuple] = []

    @property
    def audio_format(self) -> AudioFormat:
        return MULAW_8K

    def feed(self, ev: TransportEvent) -> None:
        self._inbound.put_nowait(ev)

    async def events(self) -> AsyncIterator[TransportEvent]:
        while True:
            ev = await self._inbound.get()
            yield ev
            if isinstance(ev, CallEnded):
                return

    async def play(self, frame: AudioFrame) -> None:
        self.ops.append(("play", frame))

    async def clear(self) -> None:
        self.ops.append(("clear",))

    async def checkpoint(self, name: str) -> None:
        self.ops.append(("checkpoint", name))
