"""providers/stt/deepgram.py — streaming STT via Deepgram's WebSocket API.

Successor to sarvam_stt.py, whose name was a lie: the class called
SarvamSTT always talked to Deepgram nova-2. The interface boundary makes
that impossible now — vendor names stop at this file.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import websockets

from runtime.interfaces import OnSTTEvent
from runtime.types import AudioFrame, STTEvent

log = logging.getLogger("providers.stt.deepgram")

def _build_url(model: str, language: str) -> str:
    """Model/language come from the agent's STT policy; the rest of the
    connection contract (mu-law 8k mono, interims, VAD/endpoint events) is
    fixed by how this runtime consumes Deepgram."""
    return (
        "wss://api.deepgram.com/v1/listen"
        f"?model={model}"
        f"&language={language}"
        "&encoding=mulaw"
        "&sample_rate=8000"
        "&channels=1"
        "&interim_results=true"
        "&utterance_end_ms=1000"
        "&vad_events=true"
        "&endpointing=300"
        "&smart_format=true"
    )


class DeepgramSTT:
    """Implements runtime.interfaces.STT."""

    # Deepgram emits UtteranceEnd/vad_events; the Turn Engine's
    # ProviderEndpointer starts trusting them in M4.
    emits_endpoint = True

    def __init__(self, *, api_key: str, on_event: OnSTTEvent,
                 model: str = "nova-2", language: str = "hi") -> None:
        self._api_key = api_key
        self._on_event = on_event
        self._url = _build_url(model, language)
        # Untyped: websockets' client class moved between major versions,
        # so pinning it here buys nothing.
        self._ws: Any = None
        self._closed = False
        self._reader: asyncio.Task | None = None

    async def healthy(self) -> bool:
        """SupportsHealth probe: Deepgram's REST /v1/projects with our key —
        validates both reachability and the credential."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    "https://api.deepgram.com/v1/projects",
                    headers={"Authorization": f"Token {self._api_key}"},
                )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def start(self) -> None:
        try:
            self._ws = await websockets.connect(
                self._url,
                additional_headers={"Authorization": f"Token {self._api_key}"},
            )
            self._reader = asyncio.create_task(self._read_loop(), name="dg-reader")
            log.info("STT connected (Deepgram nova-2)")
        except Exception as e:  # noqa: BLE001
            # D5: the call proceeds deaf if this fails — reconnect lands in M8.
            log.error("STT connect failed: %s", e)

    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                data = json.loads(message)
                msg_type = data.get("type", "")

                if msg_type == "Results":
                    alts = data.get("channel", {}).get("alternatives", [{}])
                    transcript = alts[0].get("transcript", "").strip()
                    if not transcript:
                        continue
                    if data.get("is_final", False):
                        log.info("[STT Final] %s", transcript)
                        await self._on_event(STTEvent(kind="final", text=transcript))
                    else:
                        log.debug("[STT Partial] %s", transcript)
                        await self._on_event(STTEvent(kind="partial", text=transcript))

                elif msg_type == "UtteranceEnd":
                    log.debug("Utterance end received")
                    await self._on_event(STTEvent(kind="endpoint", text=""))

        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.info("STT reader ended: %s", e)

    async def send_audio(self, frame: AudioFrame) -> None:
        if self._closed or self._ws is None:
            return
        try:
            await self._ws.send(frame.payload)
        except Exception as e:  # noqa: BLE001
            log.warning("STT send failed: %s", e)

    async def close(self) -> None:
        self._closed = True
        if self._reader:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
