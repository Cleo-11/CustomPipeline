"""
session.py — The brain of a single call.

Carrier-free since M3: the session pumps TransportEvents from a Transport
and speaks back through play/clear/checkpoint. It never sees wire JSON,
base64, or frame pacing — those belong to the transport adapter.
"""
from __future__ import annotations
import asyncio
import logging

import webrtcvad

import audio
import booking
import config
from runtime.clauses import stream_clauses
from runtime.interfaces import LLM, TTS, STTFactory, Transport
from runtime.markers import extract_actions
from runtime.types import (
    MULAW_8K,
    AudioFrame,
    CallEnded,
    CallStarted,
    MediaReceived,
    OutputCleared,
    PlaybackFinished,
    STTEvent,
    TransportEvent,
)

log = logging.getLogger("session")


class CallSession:
    def __init__(self, transport: Transport, *,
                 stt_factory: STTFactory, tts: TTS, llm: LLM):
        self._transport = transport
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.caller_number: str = "unknown"
        self.caller_name: str = "unknown"

        self.messages: list[dict] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
        self.stt = stt_factory(self._on_stt_event)
        self.tts = tts
        self._llm = llm

        self._pending_user = ""
        self._endpoint_timer: asyncio.Task | None = None
        self._speak_task: asyncio.Task | None = None
        self._is_speaking = False
        self._speak_ended_at: float = 0
        self._turn_seq = 0
        self._bargein_run = 0
        self._greeting_active: bool = False
        self._vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)

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
        elif isinstance(ev, (PlaybackFinished, OutputCleared)):
            self._is_speaking = False

    async def _on_start(self, ev: CallStarted) -> None:
        self.stream_id = ev.stream_id
        self.call_id = ev.call_id
        self.caller_number = ev.caller
        log.info("Call start stream=%s call=%s", self.stream_id, self.call_id)
        await self.stt.start()
        self.messages.append({"role": "assistant", "content": config.GREETING})
        self._greeting_active = True
        self._speak_task = asyncio.create_task(self._speak_greeting())

    async def _speak_greeting(self) -> None:
        try:
            await self._speak(config.GREETING)
        finally:
            self._greeting_active = False

    async def _on_media(self, frame: AudioFrame) -> None:
        # Always forward audio to STT — the recognizer needs a continuous stream.
        await self.stt.send_audio(frame)

        if self._is_speaking or self._greeting_active:
            return

        # Barge-in: requires sustained RMS *and* VAD confirmation
        pcm = audio.ulaw_to_pcm16(frame.payload)
        rms_high = audio.rms(pcm) > config.BARGEIN_RMS_THRESHOLD

        # 20 ms frame at 8 kHz = 160 samples = 320 bytes — valid webrtcvad size
        try:
            is_speech = self._vad.is_speech(pcm.tobytes(), 8000)
        except Exception:
            is_speech = False

        if rms_high and is_speech:
            self._bargein_run += 1
            if self._bargein_run >= config.BARGEIN_MIN_FRAMES:
                await self._interrupt()
        else:
            self._bargein_run = 0

    # ---------------------------------------------------------------- STT cb
    async def _on_stt_event(self, ev: STTEvent) -> None:
        if ev.kind == "partial":
            await self._on_partial(ev.text)
        elif ev.kind == "final":
            await self._on_final(ev.text)

    async def _on_partial(self, text: str) -> None:
        if self._is_speaking:
            loop_time = asyncio.get_event_loop().time()
            if (loop_time - self._speak_ended_at) > 0.5:
                await self._interrupt()

    async def _on_final(self, text: str) -> None:
        log.info("STT FINAL: %s", text)
        self._pending_user = (self._pending_user + " " + text).strip()
        if self._endpoint_timer and not self._endpoint_timer.done():
            self._endpoint_timer.cancel()
        self._endpoint_timer = asyncio.create_task(self._endpoint_after_silence())

    async def _endpoint_after_silence(self) -> None:
        try:
            await asyncio.sleep(config.ENDPOINT_SILENCE_MS / 1000)
        except asyncio.CancelledError:
            return
        user_text = self._pending_user.strip()
        self._pending_user = ""
        if user_text:
            await self._respond_to(user_text)

    # ------------------------------------------------------------ responding
    async def _respond_to(self, user_text: str) -> None:
        log.info("USER: %s", user_text)
        self.messages.append({"role": "user", "content": user_text})
        self._turn_seq += 1
        seq = self._turn_seq
        self._speak_task = asyncio.create_task(self._generate_and_speak(seq))

    async def _generate_and_speak(self, seq: int) -> None:
        full_reply = ""
        try:
            async for chunk in stream_clauses(self._llm.stream(self.messages)):
                if seq != self._turn_seq:
                    return
                clean, bk, brochure = extract_actions(chunk)
                full_reply += " " + clean
                if bk:
                    asyncio.create_task(
                        booking.save_booking(
                            self.call_id or "?", self.caller_name, bk))
                if brochure:
                    asyncio.create_task(
                        booking.send_brochure(self.caller_number))
                if clean:
                    log.info("LLM chunk: %s", clean)
                    await self._speak(clean)
        except asyncio.CancelledError:
            log.info("Reply turn %s cancelled (barge-in)", seq)
            raise
        finally:
            if full_reply.strip():
                self.messages.append(
                    {"role": "assistant", "content": full_reply.strip()})
                log.info("PRIYA: %s", full_reply.strip())

    async def _speak(self, text: str) -> None:
        log.info("SPEAK called: %s", text[:60])
        self._is_speaking = True
        self._speak_ended_at = asyncio.get_event_loop().time()
        self._bargein_run = 0
        frame_count = 0
        try:
            async for frame in self.tts.synthesize(text, MULAW_8K):
                frame_count += 1
                await self._transport.play(frame)
            log.info("SPEAK done: %d frames sent", frame_count)
            # The transport no-ops this before the call starts.
            await self._transport.checkpoint(f"turn-{self._turn_seq}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("speak error: %s", e)
        finally:
            self._is_speaking = False
            self._speak_ended_at = asyncio.get_event_loop().time()

    async def _interrupt(self) -> None:
        self._bargein_run = 0
        self._is_speaking = False
        self._speak_ended_at = asyncio.get_event_loop().time()
        await self._transport.clear()
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        log.info("Barge-in: cleared audio")

    # --------------------------------------------------------------- teardown
    async def cleanup(self) -> None:
        log.info("Call cleanup stream=%s", self.stream_id)
        for t in (self._endpoint_timer, self._speak_task):
            if t and not t.done():
                t.cancel()
        await self.stt.close()
