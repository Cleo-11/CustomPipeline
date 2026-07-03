"""runtime/types.py — Normalized data types the runtime speaks internally.

Provider adapters translate vendor wire formats into these and back;
orchestration code never sees a vendor payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AudioFormat:
    """Encoding + rate of an audio stream; adapters negotiate with this."""

    encoding: Literal["mulaw", "pcm16"]
    sample_rate: int
    channels: int = 1


# The only format the runtime speaks today (Vobiz telephony). PCM-native
# transports become possible once M3 negotiates formats per transport.
MULAW_8K = AudioFormat(encoding="mulaw", sample_rate=8000)


@dataclass(frozen=True)
class AudioFrame:
    """One chunk of audio on its way through the runtime."""

    payload: bytes
    format: AudioFormat


@dataclass(frozen=True)
class STTEvent:
    """Normalized recognizer event.

    Endpoint/speech-boundary kinds arrive with the Turn Engine (M4), which
    is what will consume them; today's session only reacts to transcripts.
    """

    kind: Literal["partial", "final"]
    text: str


@dataclass(frozen=True)
class LLMDelta:
    """One increment of a streamed model reply. Tool-call deltas land in M7."""

    text: str
