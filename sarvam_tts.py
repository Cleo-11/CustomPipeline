"""
sarvam_tts.py — Text to speech via Sarvam REST API (Bulbul v3).
"""
from __future__ import annotations
import base64
import logging
import struct
from typing import AsyncIterator

import httpx
import numpy as np

import audio
import config

log = logging.getLogger("tts")

TTS_URL = "https://api.sarvam.ai/text-to-speech"


class SarvamTTS:
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        text = text.strip()
        if not text:
            return

        headers = {
            "api-subscription-key": config.SARVAM_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": [text],
            "target_language_code": config.TTS_LANGUAGE,
            "speaker": config.TTS_SPEAKER,
            "model": config.TTS_MODEL,
            "pace": config.TTS_PACE,
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(TTS_URL, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()

                audios = data.get("audios", [])
                if not audios:
                    log.error("TTS returned no audio. Full response: %s", data)
                    return

                b64 = audios[0]
                raw = base64.b64decode(b64)

                # Read actual sample rate from WAV header
                src_rate = struct.unpack_from('<I', raw, 24)[0]
                log.info("TTS got %d bytes, sample rate %dHz", len(raw), src_rate)

                # Strip 44-byte WAV header, convert to vobiz frames
                pcm = np.frombuffer(raw[44:], dtype="<i2")
                frames = audio.pcm16_to_vobiz_frames(pcm, src_rate=src_rate)
                log.info("TTS producing %d frames", len(frames))

                for frame in frames:
                    yield frame

        except httpx.HTTPStatusError as e:
            log.error("TTS HTTP error %s: %s", e.response.status_code, e.response.text)
        except Exception as e:
            log.error("TTS error: %s", e)