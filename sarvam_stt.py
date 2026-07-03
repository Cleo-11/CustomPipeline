"""
sarvam_stt.py — Streaming STT via Deepgram SDK v2 (Python 3.9 compatible).
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import Awaitable, Callable

import websockets
import json
import base64

log = logging.getLogger("stt")

OnText = Callable[[str], Awaitable[None]]

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-2"
    "&language=hi"
    "&encoding=mulaw"
    "&sample_rate=8000"
    "&channels=1"
    "&interim_results=true"
    "&utterance_end_ms=1000"
    "&vad_events=true"
    "&endpointing=300"
    "&smart_format=true"
)


class SarvamSTT:
    def __init__(self, on_partial: OnText, on_final: OnText):
        self._on_partial = on_partial
        self._on_final = on_final
        self._ws = None
        self._closed = False
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            self._ws = await websockets.connect(
                DEEPGRAM_URL,
                additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
            )
            self._reader = asyncio.create_task(
                self._read_loop(), name="dg-reader")
            log.info("STT connected (Deepgram nova-2)")
        except Exception as e:
            log.error("STT connect failed: %s", e)

    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                data = json.loads(message)
                msg_type = data.get("type", "")

                if msg_type == "Results":
                    alts = (data.get("channel", {})
                            .get("alternatives", [{}]))
                    transcript = alts[0].get("transcript", "").strip()
                    if not transcript:
                        continue
                    is_final = data.get("is_final", False)
                    if is_final:
                        log.info("[STT Final] %s", transcript)
                        await self._on_final(transcript)
                    else:
                        log.debug("[STT Partial] %s", transcript)
                        await self._on_partial(transcript)

                elif msg_type == "UtteranceEnd":
                    log.debug("Utterance end received")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.info("STT reader ended: %s", e)

    async def send_pcm16(self, pcm16_le: bytes) -> None:
        pass  # not used

    async def send_ulaw(self, ulaw_bytes: bytes) -> None:
        if self._closed or self._ws is None:
            return
        try:
            await self._ws.send(ulaw_bytes)
        except Exception as e:
            log.warning("STT send failed: %s", e)

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        self._closed = True
        if self._reader:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass