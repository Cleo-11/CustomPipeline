"""runtime/interfaces.py — Capability-typed provider Protocols (redesign §4).

The orchestration core types against these and never names a vendor.
Swapping a provider means writing one adapter in providers/ and wiring it
at the composition root (server.py today); conversation logic is untouched.

Deliberate M2 deviations from RUNTIME_REDESIGN.md §4:
- STT delivers events through an async callback instead of an `events()`
  iterator. CallSession is callback-shaped today; the Turn Engine (M4) is
  the right moment to invert to a pulled event stream, not before.
- LLM.stream takes plain chat messages; the `tools` parameter arrives with
  the tool registry (M7).
- Transport stays inline in server.py/session.py until M3.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from runtime.types import AudioFormat, AudioFrame, LLMDelta, STTEvent

OnSTTEvent = Callable[[STTEvent], Awaitable[None]]


class STT(Protocol):
    """Streaming recognizer. Stateful per call — construct via STTFactory."""

    @property
    def emits_endpoint(self) -> bool:
        """True: the provider signals end-of-turn itself and the engine may
        trust it. False: the runtime must run its own endpointer."""
        ...

    async def start(self) -> None: ...

    async def send_audio(self, frame: AudioFrame) -> None: ...

    async def close(self) -> None: ...


# One live STT connection per call, with events bound to that call's session.
STTFactory = Callable[[OnSTTEvent], STT]


class TTS(Protocol):
    @property
    def supports_streaming_input(self) -> bool:
        """True: text can be fed incrementally. False: one request per clause."""
        ...

    def synthesize(self, text: str, fmt: AudioFormat) -> AsyncIterator[AudioFrame]: ...


class LLM(Protocol):
    def stream(self, messages: list[Any]) -> AsyncIterator[LLMDelta]: ...
