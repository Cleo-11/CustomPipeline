"""
session.py — Per-call wiring: transport + providers + Turn Engine.

Since M4 the session makes no turn-taking decisions. It normalizes raw
signals into engine events (speech verdicts, transcripts, timer firings,
playback progress), executes the intents the engine returns (greeting,
commit, cancel, timers), and runs the LLM→clauses→TTS→transport reply
pipeline. All the *rules* — barge-in thresholds, greeting policy,
endpointing, staleness — live in runtime/turn_engine.py where they are
pure and replay-testable.

Since M6 the session also announces what happens as typed events on the
bus (runtime/events.py). Emission is a synchronous enqueue — never awaited
on the audio path — and the session is correct with a NullBus: events are
observations, not control flow.
"""
from __future__ import annotations
import asyncio
import contextlib
import logging

import webrtcvad

import audio
from runtime import events
from runtime.agent import AgentConfig
from runtime.context import trim_history
from runtime.endpointing import Endpointer, FixedSilenceEndpointer, ProviderEndpointer
from runtime.events import NULL_BUS, EventEmitter
from runtime.interfaces import LLM, TTS, STTFactory, Transport
from runtime.tools import (
    MarkerToolStrategy,
    ToolContext,
    ToolDispatchStrategy,
    ToolExecutor,
)
from runtime.turn_engine import (
    ArmEndpointTimer,
    CancelOutput,
    CommitUserTurn,
    Intent,
    PlayGreeting,
    TurnEngine,
    TurnState,
)
from runtime.types import (
    MULAW_8K,
    AudioFrame,
    CallEnded,
    CallStarted,
    MediaReceived,
    PlaybackFinished,
    STTEvent,
    TransportEvent,
)

log = logging.getLogger("session")


class CallSession:
    def __init__(self, transport: Transport, *, agent: AgentConfig,
                 stt_factory: STTFactory, tts: TTS, llm: LLM,
                 engine: TurnEngine | None = None,
                 bus: EventEmitter | None = None,
                 tool_strategy: ToolDispatchStrategy | None = None,
                 tool_executor: ToolExecutor | None = None):
        self._transport = transport
        self.agent = agent
        self._bus: EventEmitter = bus if bus is not None else NULL_BUS
        # No strategy wired (tests, toolless agents) → a marker strategy
        # with zero specs: plain clause streaming, nothing dispatched.
        self._tooling: ToolDispatchStrategy = (
            tool_strategy if tool_strategy is not None else MarkerToolStrategy(()))
        self._tool_executor = tool_executor
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.caller_number: str = "unknown"
        self.caller_name: str = "unknown"

        self.messages: list[dict] = [{"role": "system", "content": agent.system_prompt}]
        self.stt = stt_factory(self._on_stt_event)
        self.tts = tts
        self._llm = llm
        self._engine = engine if engine is not None else TurnEngine(
            policy=agent.turn.engine_policy(),
            endpointer=self._pick_endpointer(),
        )

        self._endpoint_timer: asyncio.Task | None = None
        self._speak_task: asyncio.Task | None = None
        self._vad = webrtcvad.Vad(agent.turn.vad_aggressiveness)

        # Per-turn latency bookkeeping (reset at each CommitUserTurn) —
        # measured here in the wiring, published as event payload.
        self._turn_t0: float | None = None
        self._thinking_s: float | None = None
        self._first_audio_s: float | None = None
        self._speech_started = False
        self._last_user = ""
        self._turn_had_tools = False

    def _cid(self) -> str:
        return self.call_id or "unknown"

    def _pick_endpointer(self) -> Endpointer:
        delay_s = self.agent.turn.endpoint_silence_ms / 1000
        if self.agent.stt.endpointer == "provider" and self.stt.emits_endpoint:
            return ProviderEndpointer(fallback_delay_s=delay_s)
        return FixedSilenceEndpointer(delay_s=delay_s)

    @staticmethod
    def _now() -> float:
        return asyncio.get_event_loop().time()

    # ---------------------------------------------------------------- events
    async def run(self) -> None:
        """Pump transport events until the call ends. The one loop per call."""
        try:
            async for ev in self._transport.events():
                await self._dispatch(ev)
                if isinstance(ev, CallEnded):
                    break
        finally:
            await self.cleanup()

    async def _dispatch(self, ev: TransportEvent) -> None:
        if isinstance(ev, CallStarted):
            await self._on_start(ev)
        elif isinstance(ev, MediaReceived):
            await self._on_media(ev.frame)
        elif isinstance(ev, PlaybackFinished):
            # If the engine leaves AGENT_SPEAKING here, the turn's audio
            # truly finished at the carrier (post-drain, D6) — the moment
            # SpeechEnded describes.
            was_speaking = self._engine.state is TurnState.AGENT_SPEAKING
            seq = self._engine.turn_seq
            await self._execute(self._engine.playback_finished())
            if was_speaking and self._engine.state is not TurnState.AGENT_SPEAKING:
                self._bus.emit(events.SpeechEnded(call_id=self._cid(), turn_seq=seq))
        elif isinstance(ev, CallEnded):
            self._bus.emit(events.CallEnded(call_id=self._cid()))
        # OutputCleared: the engine already left AGENT_SPEAKING when it
        # emitted CancelOutput; the carrier ack carries no new information.

    async def _on_start(self, ev: CallStarted) -> None:
        self.stream_id = ev.stream_id
        self.call_id = ev.call_id
        self.caller_number = ev.caller
        log.info("Call start stream=%s call=%s", self.stream_id, self.call_id)
        self._bus.emit(events.CallStarted(
            call_id=self._cid(), caller=ev.caller, agent_id=self.agent.agent_id))
        await self.stt.start()
        await self._execute(self._engine.call_started())

    async def _on_media(self, frame: AudioFrame) -> None:
        # Always forward audio to STT — the recognizer needs a continuous stream.
        await self.stt.send_audio(frame)

        # Normalize the frame to a speech verdict; the engine owns what the
        # verdict *means* (barge-in counting, greeting immunity).
        pcm = audio.ulaw_to_pcm16(frame.payload)
        rms_high = audio.rms(pcm) > self.agent.turn.bargein_rms_threshold
        # 20 ms frame at 8 kHz = 160 samples = 320 bytes — valid webrtcvad size
        try:
            vad_speech = self._vad.is_speech(pcm.tobytes(), 8000)
        except Exception:
            vad_speech = False
        await self._execute(self._engine.media_frame(rms_high and vad_speech))

    async def _on_stt_event(self, ev: STTEvent) -> None:
        if ev.kind == "partial":
            await self._execute(self._engine.stt_partial(self._now()))
        elif ev.kind == "final":
            log.info("STT FINAL: %s", ev.text)
            await self._execute(self._engine.stt_final(ev.text))
        elif ev.kind == "endpoint":
            await self._execute(self._engine.stt_endpoint())
        elif ev.kind == "dead":
            # D5 alarm: the recognizer is gone beyond its reconnect budget.
            # The call continues one-way; operators get a loud fact.
            log.error("STT is dead for call %s — the call is deaf", self._cid())
            self._bus.emit(events.ProviderFailed(
                call_id=self._cid(), provider="stt",
                error="recognizer lost; reconnect budget exhausted"))

    # ------------------------------------------------------------- intents
    async def _execute(self, intents: list[Intent]) -> None:
        for intent in intents:
            if isinstance(intent, PlayGreeting):
                self.messages.append({"role": "assistant", "content": self.agent.greeting})
                self._speak_task = asyncio.create_task(self._speak_greeting())
            elif isinstance(intent, ArmEndpointTimer):
                if self._endpoint_timer and not self._endpoint_timer.done():
                    self._endpoint_timer.cancel()
                self._endpoint_timer = asyncio.create_task(
                    self._fire_endpoint(intent.generation, intent.delay_s))
            elif isinstance(intent, CancelOutput):
                await self._cancel_output(intent.turn_seq)
            elif isinstance(intent, CommitUserTurn):
                log.info("USER: %s", intent.text)
                self.messages.append({"role": "user", "content": intent.text})
                self._last_user = intent.text
                self._turn_t0 = self._now()
                self._thinking_s = None
                self._first_audio_s = None
                self._speech_started = False
                self._turn_had_tools = False
                self._bus.emit(events.ThinkingStarted(
                    call_id=self._cid(), turn_seq=intent.turn_seq))
                self._speak_task = asyncio.create_task(
                    self._generate_and_speak(intent.turn_seq, intent.play_filler))

    async def _fire_endpoint(self, generation: int, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        await self._execute(self._engine.endpoint_fired(generation))

    async def _cancel_output(self, turn_seq: int) -> None:
        t0 = self._now()
        task = self._speak_task
        if task and not task.done():
            task.cancel()
            # Wait for the pipeline to unwind so its history append (spoken
            # clauses only, D4) lands *before* any new turn's user message.
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._transport.clear()
        # reaction_s is the real mouth-shut latency: CancelOutput intent →
        # pipeline unwound + carrier buffer cleared.
        self._bus.emit(events.AgentInterrupted(
            call_id=self._cid(), turn_seq=turn_seq,
            reaction_s=self._now() - t0))
        log.info("Cancelled output for turn %s", turn_seq)

    # ------------------------------------------------------------ pipeline
    async def _speak_greeting(self) -> None:
        frames = 0
        try:
            frames = await self._speak(self.agent.greeting, 0)
        finally:
            await self._execute(
                self._engine.speaking_finished(0, any_audio=frames > 0))

    def _dispatch_tool(self, name: str, args: dict) -> None:
        """Route a tool call from the dispatch strategy to the executor.
        Synchronous — the executor runs the tool in its own task."""
        self._turn_had_tools = True
        if self._tool_executor is None:
            log.warning("Tool call %r dropped: no executor wired", name)
            return
        self._tool_executor.dispatch(name, args, ToolContext(
            call_id=self._cid(),
            caller_number=self.caller_number,
            caller_name=self.caller_name,
            agent=self.agent,
        ))

    async def _generate_and_speak(self, seq: int, play_filler: bool) -> None:
        spoken: list[str] = []
        frames = 0
        interrupted = False
        # Proto Context Compiler (M8): cap history before it reaches the
        # model, so hour-long calls can't inflate latency without bound.
        self.messages = trim_history(
            self.messages,
            max_messages=self.agent.llm.history_max_messages,
            max_chars=self.agent.llm.history_max_chars)
        gen = self._tooling.clauses(self._llm, self.messages, self._dispatch_tool)

        async def _next_clause() -> str | None:
            return await anext(gen, None)

        async def _first_clause() -> str | None:
            # Timestamped at actual availability, inside the task, so the
            # filler playing in the foreground can't pollute the measurement.
            clause = await anext(gen, None)
            if clause is not None and self._turn_t0 is not None:
                self._thinking_s = self._now() - self._turn_t0
                self._bus.emit(events.ThinkingFinished(
                    call_id=self._cid(), turn_seq=seq,
                    thinking_s=self._thinking_s))
            return clause

        # Start pulling the first clause *before* the filler plays, so the
        # filler genuinely masks LLM time-to-first-token instead of adding
        # to it.
        first_task = asyncio.create_task(_first_clause())
        try:
            if play_filler:
                frames += await self._speak(self.agent.turn.filler, seq)
            chunk = await first_task
            while chunk is not None:
                if self._engine.is_stale(seq):
                    interrupted = True
                    return
                # The dispatch strategy already stripped tool calls and
                # routed them; chunks arriving here are pure speech.
                log.info("LLM chunk: %s", chunk)
                clause_frames = await self._speak(chunk, seq)
                frames += clause_frames
                # D4: history records what the caller actually heard — a
                # clause counts only if its audio went out (a TTS failure
                # yields zero frames and must not enter the transcript).
                if clause_frames:
                    spoken.append(chunk)
                chunk = await _next_clause()
            # Nothing spoken and no tool fired: the pipeline failed (LLM
            # died, TTS breaker open, empty stream). Degrade audibly (M8) —
            # a scripted apology beats dead air. Never enters history: the
            # model didn't say it.
            if (not spoken and not self._turn_had_tools
                    and not self._engine.is_stale(seq)
                    and self.agent.turn.fallback_line):
                log.warning("Turn %d produced nothing; speaking fallback line", seq)
                self._bus.emit(events.FallbackSpoken(
                    call_id=self._cid(), turn_seq=seq))
                frames += await self._speak(self.agent.turn.fallback_line, seq)
        except asyncio.CancelledError:
            interrupted = True
            log.info("Reply turn %s cancelled (barge-in)", seq)
            raise
        finally:
            first_task.cancel()
            if spoken:
                reply = " ".join(spoken)
                self.messages.append({"role": "assistant", "content": reply})
                log.info("PRIYA: %s", reply)
            self._bus.emit(events.TurnCompleted(
                call_id=self._cid(), turn_seq=seq,
                user_text=self._last_user, agent_text=" ".join(spoken),
                thinking_s=self._thinking_s,
                first_audio_s=self._first_audio_s,
                interrupted=interrupted))
            await self._execute(
                self._engine.speaking_finished(seq, any_audio=frames > 0))

    async def _speak(self, text: str, seq: int) -> int:
        """Synthesize and play one utterance; returns frames actually sent."""
        log.info("SPEAK called: %s", text[:60])
        await self._execute(self._engine.speaking_started(seq, self._now()))
        frame_count = 0
        try:
            async for frame in self.tts.synthesize(text, MULAW_8K):
                frame_count += 1
                if frame_count == 1 and not self._speech_started:
                    # First audible output of this turn (filler counts —
                    # this is when the caller hears the agent respond).
                    self._speech_started = True
                    if self._turn_t0 is not None:
                        self._first_audio_s = self._now() - self._turn_t0
                    self._bus.emit(events.SpeechStarted(
                        call_id=self._cid(), turn_seq=seq))
                await self._transport.play(frame)
            log.info("SPEAK done: %d frames sent", frame_count)
            # The transport no-ops this before the call starts.
            await self._transport.checkpoint(f"turn-{seq}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("speak error: %s", e)
        return frame_count

    # --------------------------------------------------------------- teardown
    async def cleanup(self) -> None:
        log.info("Call cleanup stream=%s", self.stream_id)
        for t in (self._endpoint_timer, self._speak_task):
            if t and not t.done():
                t.cancel()
        await self.stt.close()
        self._bus.emit(events.SessionClosed(call_id=self._cid()))
