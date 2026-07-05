"""runtime/endpointing.py — pluggable end-of-turn detection strategies.

Endpointing (deciding "the caller is done, respond now") is the single
biggest lever on conversational feel, so it is a strategy the Turn Engine
consults, never logic baked into it (redesign §5).

Two strategies today; SemanticEndpointer (a classifier that stretches the
timeout when the caller sounds mid-thought) is the planned third.
"""
from __future__ import annotations

from typing import Protocol


class Endpointer(Protocol):
    @property
    def trusts_provider(self) -> bool:
        """True: a provider endpoint signal (e.g. Deepgram UtteranceEnd)
        commits the turn immediately. False: it is ignored."""
        ...

    @property
    def silence_delay_s(self) -> float:
        """Seconds of silence after a final transcript before committing.
        For provider-trusting strategies this is the safety fallback."""
        ...


class FixedSilenceEndpointer:
    """Today's behavior: a fixed silence window after the last final."""

    trusts_provider = False

    def __init__(self, delay_s: float) -> None:
        self.silence_delay_s = delay_s


class ProviderEndpointer:
    """Trust the STT provider's endpoint events (capability-gated on
    STT.emits_endpoint). The silence timer stays armed as a safety net in
    case the provider signal never arrives."""

    trusts_provider = True

    def __init__(self, fallback_delay_s: float) -> None:
        self.silence_delay_s = fallback_delay_s
