"""Tests for runtime/clauses.py — clause chunking over any LLM delta stream.

Same characterization as the pre-M2 llm.stream_sentences tests, now fed
through the LLMDelta seam with no monkeypatching.
"""
from typing import AsyncIterator

from runtime.clauses import _breakpoint, stream_clauses
from runtime.types import LLMDelta


async def _deltas(parts) -> AsyncIterator[LLMDelta]:
    for p in parts:
        yield LLMDelta(text=p)


async def _exploding_deltas(parts) -> AsyncIterator[LLMDelta]:
    for p in parts:
        yield LLMDelta(text=p)
    raise RuntimeError("connection dropped")


async def _collect(deltas) -> list[str]:
    return [c async for c in stream_clauses(deltas)]


# ------------------------------------------------------------------ _breakpoint
def test_breakpoint_finds_first_hard_break_past_min_len():
    assert _breakpoint("hello. world.", 3) == 5


def test_breakpoint_ignores_breaks_before_min_len():
    assert _breakpoint("hi. there", 5) is None


def test_breakpoint_devanagari_danda():
    text = "क" * 10 + "।" + "और"
    assert _breakpoint(text, 5) == 10


def test_breakpoint_none_without_break():
    assert _breakpoint("no break here", 3) is None


def test_breakpoint_decimal_point_is_a_break():
    # KNOWN LIMITATION: '.' inside "4.30pm" is treated as a sentence break,
    # so a clause can be cut mid-time-expression. Fixed by design in M4.
    assert _breakpoint("visit at 4.30pm", 5) == 10  # the '.' in "4.30"


# --------------------------------------------------------------- stream_clauses
async def test_short_reply_flushes_once_at_end():
    assert await _collect(_deltas(["नमस्ते", " जी।"])) == ["नमस्ते जी।"]


async def test_long_reply_flushes_at_hard_break():
    first = "क" * 125 + "।"
    assert await _collect(_deltas([first, " दूसरा वाक्य"])) == [first, "दूसरा वाक्य"]


async def test_empty_deltas_are_skipped():
    assert await _collect(_deltas(["", "ठीक", "", " है।"])) == ["ठीक है।"]


async def test_stream_error_still_yields_buffered_text():
    assert await _collect(_exploding_deltas(["आधा जवाब"])) == ["आधा जवाब"]


async def test_empty_stream_yields_nothing():
    assert await _collect(_deltas([])) == []
