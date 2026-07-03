"""
llm.py — Local LLM (Qwen2 via Ollama) with token streaming + sentence chunking.

Ollama exposes an OpenAI-compatible endpoint, so we use the openai AsyncClient
pointed at it. That means you can later swap to vLLM / Sarvam / any
OpenAI-compatible server by changing LLM_BASE_URL only — no code change.

`stream_sentences()` is the latency trick: instead of waiting for the whole
reply, we emit each clause/sentence the moment it completes so TTS can start.
"""
from __future__ import annotations
import logging
import re
from typing import AsyncIterator

from openai import AsyncOpenAI

import config

log = logging.getLogger("llm")

_client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)

# Flush a chunk to TTS at these boundaries (Devanagari danda included).
_HARD_BREAKS = "।?!.\n"
_MIN_FIRST_CHUNK = 120    # start speaking fast: flush first clause early
_MIN_CHUNK = 180          # later chunks can be a touch longer for prosody


async def stream_sentences(messages: list[dict]) -> AsyncIterator[str]:
    """Stream the model's reply, yielding speakable chunks as they form."""
    buf = ""
    first = True
    try:
        stream = await _client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
            temperature=config.LLM_TEMPERATURE,
            stream=True,
            max_tokens=160,
        )
        async for part in stream:
            delta = part.choices[0].delta.content if part.choices else None
            if not delta:
                continue
            buf += delta
            threshold = _MIN_FIRST_CHUNK if first else _MIN_CHUNK
            # Flush only on a hard sentence break, never mid-sentence.
            while True:
                idx = _breakpoint(buf, threshold)
                if idx is None:
                    break
                chunk, buf = buf[:idx + 1], buf[idx + 1:]
                chunk = chunk.strip()
                if chunk:
                    first = False
                    yield chunk
        if buf.strip():
            yield buf.strip()
    except Exception as e:  # noqa: BLE001
        log.error("LLM stream error: %s", e)
        if buf.strip():
            yield buf.strip()


def _breakpoint(text: str, min_len: int) -> int | None:
    """Index of a good place to cut `text`, or None."""
    # Always cut at a hard sentence break.
    for i, ch in enumerate(text):
        if ch in _HARD_BREAKS and i >= min_len:
            return i
    return None


# ---------------------------------------------------------------------------
# Booking-marker parsing. The system prompt instructs the model to append
# [[BOOK day=.. time=.. name=..]] or [[BROCHURE]]. We strip those from the
# spoken text and return structured intents.
# ---------------------------------------------------------------------------
_BOOK_RE = re.compile(r"\[\[BOOK([^\]]*)\]\]", re.IGNORECASE)
_BROCHURE_RE = re.compile(r"\[\[BROCHURE\]\]", re.IGNORECASE)
_KV_RE = re.compile(r"(\w+)\s*=\s*([^=\]]+?)(?=\s+\w+=|$)")


def extract_actions(text: str) -> tuple[str, dict | None, bool]:
    """Return (clean_spoken_text, booking_dict_or_None, brochure_requested)."""
    booking = None
    m = _BOOK_RE.search(text)
    if m:
        booking = {k.lower(): v.strip() for k, v in _KV_RE.findall(m.group(1))}
    brochure = bool(_BROCHURE_RE.search(text))
    clean = _BROCHURE_RE.sub("", _BOOK_RE.sub("", text)).strip()
    return clean, booking, brochure
