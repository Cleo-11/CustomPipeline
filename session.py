"""
session.py — The brain of a single call.
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging

import webrtcvad

import audio
import config
import llm
import booking
from sarvam_stt import SarvamSTT
from sarvam_tts import SarvamTTS

log = logging.getLogger("session")


class CallSession:
    def __init__(self, send_json):
        self._send = send_json
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.caller_number: str = "unknown"
        self.caller_name: str = "unknown"

        self.messages: list[dict] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
        self.stt = SarvamSTT(on_partial=self._on_partial, on_final=self._on_final)
        self.tts = SarvamTTS()

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
    async def handle_event(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        event = data.get("event")
        if event == "start":
            await self._on_start(data)
        elif event == "media":
            await self._on_media(data)
        elif event == "playedStream":
            self._is_speaking = False
        elif event == "clearedAudio":
            self._is_speaking = False
        elif event == "stop":
            await self.cleanup()

    async def _on_start(self, data: dict) -> None:
        self.stream_id = (data.get("streamId") or data.get("StreamID")
                          or data.get("stream_id") or "default")
        self.call_id = (data.get("callId") or data.get("CallID")
                        or data.get("call_id") or "unknown")
        self.caller_number = (data.get("from") or data.get("From")
                              or self.caller_number)
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

    async def _on_media(self, data: dict) -> None:
        media_payload = (data.get("media") or {}).get("payload")
        if not media_payload:
            return
        ulaw = base64.b64decode(media_payload)

        # Always forward audio to STT — Deepgram needs a continuous stream.
        await self.stt.send_ulaw(ulaw)

        if self._is_speaking or self._greeting_active:
            return

        # Barge-in: requires sustained RMS *and* VAD confirmation
        pcm = audio.ulaw_to_pcm16(ulaw)
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
            async for chunk in llm.stream_sentences(self.messages):
                if seq != self._turn_seq:
                    return
                clean, bk, brochure = llm.extract_actions(chunk)
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
            async for frame in self.tts.synthesize(text):
                frame_count += 1
                await self._send({
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": base64.b64encode(frame).decode("ascii"),
                    },
                })
                await asyncio.sleep(0.02)
            log.info("SPEAK done: %d frames sent", frame_count)
            if self.stream_id:
                await self._send({
                    "event": "checkpoint",
                    "streamId": self.stream_id,
                    "name": f"turn-{self._turn_seq}",
                })
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
        if self.stream_id:
            await self._send({"event": "clearAudio", "streamId": self.stream_id})
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