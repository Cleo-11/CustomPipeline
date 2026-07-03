"""runtime/clauses.py — Clause chunking: the runtime's core latency trick.

Re-chunks any LLM adapter's delta stream into speakable clauses, flushing
the first clause early so TTS starts long before the reply finishes.
This is runtime logic, not provider logic — it applies identically to
every LLM adapter.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from runtime.types import LLMDelta

log = logging.getLogger("runtime.clauses")

# Flush a chunk to TTS at these boundaries (Devanagari danda included).
HARD_BREAKS = "।?!.\n"
MIN_FIRST_CHUNK = 120    # start speaking fast: flush the first clause early
MIN_CHUNK = 180          # later chunks can be a touch longer for prosody


async def stream_clauses(deltas: AsyncIterator[LLMDelta]) -> AsyncIterator[str]:
    """Yield speakable chunks as they form.

    If the delta stream errors mid-reply, whatever is buffered is still
    yielded — the agent speaks what it has instead of going silent.
    """
    buf = ""
    first = True
    try:
        async for delta in deltas:
            if not delta.text:
                continue
            buf += delta.text
            threshold = MIN_FIRST_CHUNK if first else MIN_CHUNK
            # Flush only on a hard sentence break, never mid-sentence.
            while True:
                idx = _breakpoint(buf, threshold)
                if idx is None:
                    break
                chunk, buf = buf[: idx + 1], buf[idx + 1 :]
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
    """Index of a good place to cut `text`, or None.

    Known limitation (pinned in tests): '.' inside "4.30pm" counts as a
    sentence break. Smarter breaking is a Turn Engine (M4) concern.
    """
    for i, ch in enumerate(text):
        if ch in HARD_BREAKS and i >= min_len:
            return i
    return None
