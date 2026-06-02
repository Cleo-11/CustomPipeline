"""
sarvam_stt.py — STT via Sarvam REST API (Saaras v3).
Switched from WebSocket to REST to avoid connection stability issues.
Buffers 2 seconds of audio then transcribes.
"""
from __future__ import annotations
import asyncio
import base64
import logging
import struct
from typing import Awaitable, Callable

import httpx

import config

log = logging.getLogger("stt")

OnText = Callable[[str], Awaitable[None]]

STT_URL = "https://api.sarvam.ai/speech-to-text"


def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 8000) -> bytes:
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm_data)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, num_channels,
        sample_rate, byte_rate, block_align,
        bits_per_sample, b'data', data_size
    )
    return header + pcm_data


class SarvamSTT:
    def __init__(self, on_partial: OnText, on_final: OnText):
        self._on_partial = on_partial
        self._on_final = on_final
        self._audio_buffer = bytearray()
        self._buffer_lock = asyncio.Lock()
        self._sender: asyncio.Task | None = None
        self._closed = False
        # 2 seconds of audio at 8kHz mono PCM16 = 32000 bytes
        self._send_interval = 3.0
        self._min_bytes = 8000 * 2 * 2  # 1 second minimum before sending

    async def start(self) -> None:
        self._sender = asyncio.create_task(
            self._send_loop(), name="stt-sender")
        log.info("STT started (REST mode, %s)", config.STT_MODEL)

    async def send_pcm16(self, pcm16_le: bytes) -> None:
        if self._closed:
            return
        async with self._buffer_lock:
            self._audio_buffer.extend(pcm16_le)

    async def _send_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._send_interval)
            async with self._buffer_lock:
                if len(self._audio_buffer) < self._min_bytes:
                    continue
                pcm_data = bytes(self._audio_buffer)
                self._audio_buffer.clear()

            await self._transcribe(pcm_data)

    async def _transcribe(self, pcm_data: bytes) -> None:
        try:
            wav = _pcm_to_wav(pcm_data, sample_rate=8000)
            headers = {
                "api-subscription-key": config.SARVAM_API_KEY,
            }
            files = {
                "file": ("audio.wav", wav, "audio/wav"),
            }
            data = {
                "model": config.STT_MODEL,
                "language_code": config.STT_LANGUAGE,
            }
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    STT_URL, headers=headers, files=files, data=data)
                r.raise_for_status()
                result = r.json()
                transcript = result.get("transcript", "").strip()
                if transcript:
                    log.info("[STT] %s", transcript)
                    await self._on_final(transcript)
        except Exception as e:
            log.warning("STT transcribe failed: %s", e)

    async def flush(self) -> None:
        async with self._buffer_lock:
            if not self._audio_buffer:
                return
            pcm_data = bytes(self._audio_buffer)
            self._audio_buffer.clear()
        await self._transcribe(pcm_data)

    async def close(self) -> None:
        self._closed = True
        if self._sender:
            self._sender.cancel()