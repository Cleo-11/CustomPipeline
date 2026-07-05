"""
session.py — Per-call wiring: transport + providers + Turn Engine.

Since M4 the session makes no turn-taking decisions. It normalizes raw
signals into engine events (speech verdicts, transcripts, timer firings,
playback progress), executes the intents the engine returns (greeting,
commit, cancel, timers), and runs the LLM→clauses→TTS→transport reply
pipeline. All the *rules* — barge-in thresholds, greeting policy,
endpointing, staleness — live in runtime/turn_engine.py where they are
pure and replay-testable.
"""
from __future__ import annotations
import asyncio
import contextlib
import logging

import webrtcvad

import audio
import booking
import config
from runtime.clauses import stream_clauses
from runtime.endpointing import Endpointer, FixedSilenceEndpointer, ProviderEndpointer
from runtime.interfaces import LLM, TTS, STTFactory, Transport
from runtime.markers import extract_actions
from runtime.turn_engine import (
    ArmEndpointTimer,
    CancelOutput,
    CommitUserTurn,
    Intent,
    PlayGreeting,
    TurnEngine,
    TurnPolicy,
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
    def __init__(self, transport: Transport, *,
                 stt_factory: STTFactory, tts: TTS, llm: LLM,
                 engine: TurnEngine | None = None):
        self._transport = transport
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.caller_number: str = "unknown"
        self.caller_name: str = "unknown"

        self.messages: list[dict] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
        self.stt = stt_factory(self._on_stt_event)
        self.tts = tts
        self._llm = llm
        self._engine = engine if engine is not None else TurnEngine(
            policy=TurnPolicy(
                bargein_min_frames=config.BARGEIN_MIN_FRAMES,
                partial_interrupt_after_s=0.5,
                filler=config.THINKING_FILLER,
            ),
            endpointer=self._pick_endpointer(),
        )

        self._endpoint_timer: asyncio.Task | None = None
        self._speak_task: asyncio.Task | None = None
        self._vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)

    def _pick_endpointer(self) -> Endpointer:
        delay_s = config.ENDPOINT_SILENCE_MS / 1000
        if config.ENDPOINTER == "provider" and self.stt.emits_endpoint:
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
            await self._execute(self._engine.playback_finished())
        # OutputCleared: the engine already left AGENT_SPEAKING when it
        # emitted CancelOutput; the carrier ack carries no new information.

    async def _on_start(self, ev: CallStarted) -> None:
        self.stream_id = ev.stream_id
        self.call_id = ev.call_id
        self.caller_number = ev.caller
        log.info("Call start stream=%s call=%s", self.stream_id, self.call_id)
        await self.stt.start()
        await self._execute(self._engine.call_started())

    async def _on_media(self, frame: AudioFrame) -> None:
        # Always forward audio to STT — the recognizer needs a continuous stream.
        await self.stt.send_audio(frame)

        # Normalize the frame to a speech verdict; the engine owns what the
        # verdict *means* (barge-in counting, greeting immunity).
        pcm = audio.ulaw_to_pcm16(frame.payload)
        rms_high = audio.rms(pcm) > config.BARGEIN_RMS_THRESHOLD
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

    # ------------------------------------------------------------- intents
    async def _execute(self, intents: list[Intent]) -> None:
        for intent in intents:
            if isinstance(intent, PlayGreeting):
                self.messages.append({"role": "assistant", "content": config.GREETING})
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
                self._speak_task = asyncio.create_task(
                    self._generate_and_speak(intent.turn_seq, intent.play_filler))

    async def _fire_endpoint(self, generation: int, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        await self._execute(self._engine.endpoint_fired(generation))

    async def _cancel_output(self, turn_seq: int) -> None:
        task = self._speak_task
        if task and not task.done():
            task.cancel()
            # Wait for the pipeline to unwind so its history append (spoken
            # clauses only, D4) lands *before* any new turn's user message.
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._transport.clear()
        log.info("Cancelled output for turn %s", turn_seq)

    # ------------------------------------------------------------ pipeline
    async def _speak_greeting(self) -> None:
        frames = 0
        try:
            frames = await self._speak(config.GREETING, 0)
        finally:
            await self._execute(
                self._engine.speaking_finished(0, any_audio=frames > 0))

    async def _generate_and_speak(self, seq: int, play_filler: bool) -> None:
        spoken: list[str] = []
        frames = 0
        gen = stream_clauses(self._llm.stream(self.messages))

        async def _next_clause() -> str | None:
            return await anext(gen, None)

        # Start pulling the first clause *before* the filler plays, so the
        # filler genuinely masks LLM time-to-first-token instead of adding
        # to it.
        first_task = asyncio.create_task(_next_clause())
        try:
            if play_filler:
                frames += await self._speak(config.THINKING_FILLER, seq)
            chunk = await first_task
            while chunk is not None:
                if self._engine.is_stale(seq):
                    return
                clean, bk, brochure = extract_actions(chunk)
                if bk:
                    asyncio.create_task(
                        booking.save_booking(
                            self.call_id or "?", self.caller_name, bk))
                if brochure:
                    asyncio.create_task(
                        booking.send_brochure(self.caller_number))
                if clean:
                    log.info("LLM chunk: %s", clean)
                    frames += await self._speak(clean, seq)
                    # D4: history records what the caller actually heard —
                    # a clause counts only once fully played out.
                    spoken.append(clean)
                chunk = await _next_clause()
        except asyncio.CancelledError:
            log.info("Reply turn %s cancelled (barge-in)", seq)
            raise
        finally:
            first_task.cancel()
            if spoken:
                reply = " ".join(spoken)
                self.messages.append({"role": "assistant", "content": reply})
                log.info("PRIYA: %s", reply)
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
