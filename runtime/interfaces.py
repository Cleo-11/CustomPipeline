"""runtime/interfaces.py — Capability-typed provider Protocols (redesign §4).

The orchestration core types against these and never names a vendor.
Swapping a provider means writing one adapter in providers/ and wiring it
at the composition root (server.py today); conversation logic is untouched.

Deliberate deviations from RUNTIME_REDESIGN.md §4:
- STT delivers events through an async callback instead of an `events()`
  iterator. Since M4 the callback is a thin bridge into the Turn Engine
  (which is where the rules live), so inverting to a pulled stream buys
  nothing yet. M6's event bus did not need it either (the session, not
  the STT, emits bus events); the remaining trigger is a second
  concurrent STT.
- LLM.stream takes plain chat messages; the `tools` parameter arrives with
  the tool registry (M7).
- Transport has no dtmf/mark events yet — Vobiz doesn't surface them in the
  current integration; they join TransportEvent when a carrier needs them.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable

from runtime.types import AudioFormat, AudioFrame, LLMDelta, STTEvent, TransportEvent

OnSTTEvent = Callable[[STTEvent], Awaitable[None]]


@runtime_checkable
class SupportsHealth(Protocol):
    """Optional adapter capability: a cheap liveness/auth probe for
    /health?deep=true. Not part of the core provider contracts — an
    adapter without it simply reports as unprobeable."""

    async def healthy(self) -> bool: ...


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


class Transport(Protocol):
    """Carrier adapter: normalizes a duplex media connection to TransportEvents.

    Owns the entire wire protocol — framing, pacing, message shapes. The
    session pumps events() and calls play/clear/checkpoint; it never sees
    carrier JSON.
    """

    @property
    def audio_format(self) -> AudioFormat: ...

    def events(self) -> AsyncIterator[TransportEvent]: ...

    async def play(self, frame: AudioFrame) -> None: ...

    async def clear(self) -> None: ...

    async def checkpoint(self, name: str) -> None: ...
