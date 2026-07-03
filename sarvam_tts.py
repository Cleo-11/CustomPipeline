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

                raw = base64.b64decode(audios[0])

                # Read WAV header
                src_rate = struct.unpack_from('<I', raw, 24)[0]
                num_channels = struct.unpack_from('<H', raw, 22)[0]
                log.info("TTS WAV: %dHz, %dch, %d bytes",
                         src_rate, num_channels, len(raw))

                # Strip 44-byte WAV header
                pcm_raw = np.frombuffer(raw[44:], dtype="<i2")

                # If stereo, take left channel only
                if num_channels == 2:
                    pcm_raw = pcm_raw[::2]

                frames = audio.pcm16_to_vobiz_frames(pcm_raw, src_rate=src_rate)
                log.info("TTS producing %d frames", len(frames))

                for frame in frames:
                    yield frame

        except httpx.HTTPStatusError as e:
            log.error("TTS HTTP error %s: %s",
                      e.response.status_code, e.response.text)
        except Exception as e:
            log.error("TTS error: %s", e)