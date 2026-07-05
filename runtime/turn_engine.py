"""runtime/turn_engine.py — the explicit turn-taking state machine.

This is the crown-jewel component (redesign §5): every rule about who may
speak, when the caller's turn commits, and when agent audio must die is a
named transition here — not a gap between `if` statements in the session.

The engine is pure and I/O-free. It consumes normalized facts (a media
frame's speech verdict, an STT partial/final, a timer firing, playback
progress) and returns *intents* — instructions the session executes with
real providers. Time is injected as a monotonic `now` where a rule needs
it. That makes every rule unit-testable with zero sockets, and lets a
recorded call's event log replay through the engine asserting the exact
state trace.

States (redesign §5):

    LISTENING ──speech──► USER_SPEAKING ──endpoint──► THINKING
        ▲                                                │ first clause
        └──── playback finished / cancelled ──── AGENT_SPEAKING
                          barge-in ► INTERRUPTED ─► LISTENING

Behavior rules this engine makes explicit (and fixes, per ROADMAP §1.2):

- D1: committing a new user turn while agent output is in flight emits
  CancelOutput *before* CommitUserTurn — a new turn always kills the old
  one's audio instead of talking over it.
- D3: the greeting is uninterruptible, period. Both interrupt paths
  (sustained-speech barge-in and the mid-speech partial rule) are gated
  by the same flag; a commit that becomes due during the greeting is
  deferred until the greeting's playback finishes.
- D6: AGENT_SPEAKING ends when the *transport reports playback finished*
  after the session has finished sending — not when the last frame was
  queued. Playback-finished signals that arrive mid-turn (buffer drain
  between clauses) are ignored. The draining tail (sent but still
  audible) keeps barge-in armed.
- THINKING_FILLER: whether a filler masks LLM latency is a property of
  the THINKING transition (CommitUserTurn.play_filler), not a side
  effect scattered in the pipeline.

Deviation from the roadmap sketch: clause playback does not round-trip
through the engine as a `speak(clause)` intent. The engine decides *when
output lives or dies* (staleness, cancellation); clause content is pure
data flow, so the session pipes LLM→TTS directly and consults
`is_stale(seq)`. Fewer intents, identical control authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from runtime.endpointing import Endpointer


class TurnState(Enum):
    LISTENING = auto()
    USER_SPEAKING = auto()
    THINKING = auto()
    AGENT_SPEAKING = auto()
    INTERRUPTED = auto()  # transient: recorded in the trace, never rested in


@dataclass(frozen=True)
class TurnPolicy:
    """Tunables for turn feel. Becomes AgentConfig.turn_policy in M5."""

    bargein_min_frames: int = 25
    partial_interrupt_after_s: float = 0.5
    filler: str = ""  # spoken at THINKING to mask LLM latency; "" disables


# ------------------------------------------------------------------ intents
@dataclass(frozen=True)
class PlayGreeting:
    """Speak the agent's greeting as turn 0."""


@dataclass(frozen=True)
class ArmEndpointTimer:
    """(Re)start the silence timer; report back via endpoint_fired(generation)."""

    generation: int
    delay_s: float


@dataclass(frozen=True)
class CommitUserTurn:
    """The caller's turn is complete: run the reply pipeline for turn_seq."""

    text: str
    turn_seq: int
    play_filler: bool


@dataclass(frozen=True)
class CancelOutput:
    """Kill in-flight agent audio for turn_seq: cancel the speak task and
    clear the transport's buffered output."""

    turn_seq: int


Intent = PlayGreeting | ArmEndpointTimer | CommitUserTurn | CancelOutput


# ------------------------------------------------------------------- engine
class TurnEngine:
    """Pure turn-taking state machine. One instance per call."""

    def __init__(self, policy: TurnPolicy, endpointer: Endpointer) -> None:
        self.policy = policy
        self.endpointer = endpointer
        self.state = TurnState.LISTENING
        self.turn_seq = 0
        self.greeting_active = False
        self.trace: list[TurnState] = []  # states entered, for replay tests

        self._sending = False  # frames still being pushed for current turn
        self._pending_user = ""
        self._bargein_run = 0
        self._speaking_started_at = 0.0
        self._endpoint_generation = 0
        self._commit_deferred = False  # endpoint fired during the greeting

    # ------------------------------------------------------------- helpers
    def _to(self, state: TurnState) -> None:
        if state is not self.state:
            self.state = state
            self.trace.append(state)

    def is_stale(self, turn_seq: int) -> bool:
        """A newer turn has started; output tagged turn_seq must be dropped."""
        return turn_seq != self.turn_seq

    # -------------------------------------------------------------- events
    def call_started(self) -> list[Intent]:
        """Call is live: greeting plays as turn 0, uninterruptible (D3)."""
        self.greeting_active = True
        self._sending = True
        self._to(TurnState.AGENT_SPEAKING)
        return [PlayGreeting()]

    def media_frame(self, is_speech: bool) -> list[Intent]:
        """One inbound frame's speech verdict (RMS + VAD, computed by the
        session). Sustained speech while the agent is audible = barge-in."""
        if self.state is not TurnState.AGENT_SPEAKING or self.greeting_active:
            self._bargein_run = 0
            return []
        if not is_speech:
            self._bargein_run = 0
            return []
        self._bargein_run += 1
        if self._bargein_run >= self.policy.bargein_min_frames:
            return self._interrupt()
        return []

    def stt_partial(self, now: float) -> list[Intent]:
        if self.state is TurnState.AGENT_SPEAKING:
            # A cough shouldn't cut the agent off, but a real utterance
            # confirmed by the recognizer should — once the agent has held
            # the floor long enough to be sure it isn't echo.
            if (not self.greeting_active
                    and (now - self._speaking_started_at)
                    > self.policy.partial_interrupt_after_s):
                return self._interrupt()
            return []
        if self.state is TurnState.LISTENING:
            self._to(TurnState.USER_SPEAKING)
        return []

    def stt_final(self, text: str) -> list[Intent]:
        self._pending_user = (self._pending_user + " " + text).strip()
        if self.state in (TurnState.LISTENING, TurnState.THINKING):
            self._to(TurnState.USER_SPEAKING)
        self._endpoint_generation += 1
        return [ArmEndpointTimer(generation=self._endpoint_generation,
                                 delay_s=self.endpointer.silence_delay_s)]

    def endpoint_fired(self, generation: int) -> list[Intent]:
        """The silence timer elapsed. Stale generations (a newer final
        re-armed the timer) are no-ops, so replay is deterministic."""
        if generation != self._endpoint_generation:
            return []
        return self._maybe_commit()

    def stt_endpoint(self) -> list[Intent]:
        """The STT provider says the utterance ended (e.g. UtteranceEnd)."""
        if not self.endpointer.trusts_provider:
            return []
        # Invalidate the pending silence timer; the provider signal wins.
        self._endpoint_generation += 1
        return self._maybe_commit()

    def speaking_started(self, turn_seq: int, now: float) -> list[Intent]:
        if self.is_stale(turn_seq):
            return []
        self._speaking_started_at = now
        self._sending = True
        self._bargein_run = 0
        self._to(TurnState.AGENT_SPEAKING)
        return []

    def speaking_finished(self, turn_seq: int, any_audio: bool = True) -> list[Intent]:
        """The reply pipeline pushed its last frame. If audio was sent we
        stay AGENT_SPEAKING (draining — the carrier is still playing the
        tail, so barge-in stays armed, D6) until playback_finished. If
        nothing was ever sent (TTS failed), no playback signal will come,
        so close the turn out immediately."""
        if self.is_stale(turn_seq) or self.state is not TurnState.AGENT_SPEAKING:
            return []
        if any_audio:
            self._sending = False
            return []
        return self._finish_output()

    def playback_finished(self) -> list[Intent]:
        """The transport played everything sent so far. Only ends the turn
        once sending is done — mid-turn buffer drains are ignored (D6)."""
        if self.state is not TurnState.AGENT_SPEAKING or self._sending:
            return []
        return self._finish_output()

    # --------------------------------------------------------------- rules
    def _interrupt(self) -> list[Intent]:
        """Kill agent audio: transient INTERRUPTED, rest at LISTENING."""
        self._bargein_run = 0
        self._sending = False
        self._to(TurnState.INTERRUPTED)
        self._to(TurnState.LISTENING)
        return [CancelOutput(turn_seq=self.turn_seq)]

    def _finish_output(self) -> list[Intent]:
        was_greeting = self.greeting_active
        self.greeting_active = False
        self._sending = False
        self._to(TurnState.LISTENING)
        if was_greeting and self._commit_deferred:
            self._commit_deferred = False
            return self._maybe_commit()
        return []

    def _maybe_commit(self) -> list[Intent]:
        if not self._pending_user:
            return []
        if self.greeting_active:
            # D3: the greeting always finishes. Hold the commit; it flushes
            # from _finish_output when greeting playback completes.
            self._commit_deferred = True
            return []
        intents: list[Intent] = []
        if self.state is TurnState.AGENT_SPEAKING:
            # D1: the new turn kills the old turn's audio first.
            intents += self._interrupt()
        text = self._pending_user
        self._pending_user = ""
        self.turn_seq += 1
        self._to(TurnState.THINKING)
        intents.append(CommitUserTurn(text=text, turn_seq=self.turn_seq,
                                      play_filler=bool(self.policy.filler)))
        return intents
