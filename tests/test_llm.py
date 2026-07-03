"""Characterization tests for llm.py — clause chunking and marker parsing.

Pins current behavior, including known limitations (documented inline) that
later milestones fix deliberately.
"""
from types import SimpleNamespace

import pytest

import llm


# ------------------------------------------------------------------ _breakpoint
def test_breakpoint_finds_first_hard_break_past_min_len():
    assert llm._breakpoint("hello. world.", 3) == 5


def test_breakpoint_ignores_breaks_before_min_len():
    assert llm._breakpoint("hi. there", 5) is None


def test_breakpoint_devanagari_danda():
    text = "क" * 10 + "।" + "और"
    assert llm._breakpoint(text, 5) == 10


def test_breakpoint_none_without_break():
    assert llm._breakpoint("no break here", 3) is None


def test_breakpoint_decimal_point_is_a_break():
    # KNOWN LIMITATION: '.' inside "4.30pm" is treated as a sentence break,
    # so a clause can be cut mid-time-expression. Fixed by design in M4.
    assert llm._breakpoint("visit at 4.30pm", 5) == 10  # the '.' in "4.30"


# --------------------------------------------------------------- extract_actions
def test_plain_text_passes_through():
    clean, book, brochure = llm.extract_actions("नमस्ते, कैसे हैं आप?")
    assert clean == "नमस्ते, कैसे हैं आप?"
    assert book is None
    assert brochure is False


def test_book_marker_parsed_and_stripped():
    clean, book, brochure = llm.extract_actions(
        "ठीक है। [[BOOK day=Sunday time=4pm name=Rahul]]"
    )
    assert clean == "ठीक है।"
    assert book == {"day": "Sunday", "time": "4pm", "name": "Rahul"}
    assert brochure is False


def test_book_marker_multiword_value_and_decimal_time():
    _, book, _ = llm.extract_actions("[[BOOK day=Sunday time=4.30pm name=Rahul Kumar]]")
    assert book == {"day": "Sunday", "time": "4.30pm", "name": "Rahul Kumar"}


def test_brochure_marker():
    clean, book, brochure = llm.extract_actions("भेज देती हूं। [[BROCHURE]]")
    assert clean == "भेज देती हूं।"
    assert book is None
    assert brochure is True


def test_both_markers_case_insensitive():
    clean, book, brochure = llm.extract_actions("done [[book day=Mon time=5pm name=A]] [[brochure]]")
    assert clean == "done"
    assert book == {"day": "Mon", "time": "5pm", "name": "A"}
    assert brochure is True


def test_marker_split_across_chunks_is_not_recognized():
    # KNOWN LIMITATION: if clause chunking splits a marker across two chunks,
    # neither half parses and the partial marker text leaks into spoken output.
    # The tool-call dispatch in M7 removes this failure mode.
    clean, book, _ = llm.extract_actions("chalo [[BOOK day=Sun")
    assert book is None
    assert "[[BOOK" in clean


# ------------------------------------------------------------- stream_sentences
def _chunk(content):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])


class FakeStream:
    def __init__(self, parts, explode_at_end=False):
        self._parts = list(parts)
        self._explode = explode_at_end

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._parts:
            return _chunk(self._parts.pop(0))
        if self._explode:
            raise RuntimeError("connection dropped")
        raise StopAsyncIteration


def _patch_llm_stream(monkeypatch, parts, explode_at_end=False):
    async def create(**kwargs):
        return FakeStream(parts, explode_at_end)

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(llm, "_client", fake_client)


async def _collect(messages=None):
    return [c async for c in llm.stream_sentences(messages or [])]


@pytest.mark.asyncio
async def test_short_reply_flushes_once_at_end(monkeypatch):
    _patch_llm_stream(monkeypatch, ["नमस्ते", " जी।"])
    assert await _collect() == ["नमस्ते जी।"]


@pytest.mark.asyncio
async def test_long_reply_flushes_at_hard_break(monkeypatch):
    first = "क" * 125 + "।"
    _patch_llm_stream(monkeypatch, [first, " दूसरा वाक्य"])
    chunks = await _collect()
    assert chunks == [first, "दूसरा वाक्य"]


@pytest.mark.asyncio
async def test_none_deltas_are_skipped(monkeypatch):
    _patch_llm_stream(monkeypatch, [None, "ठीक", None, " है।"])
    assert await _collect() == ["ठीक है।"]


@pytest.mark.asyncio
async def test_stream_error_still_yields_buffered_text(monkeypatch):
    _patch_llm_stream(monkeypatch, ["आधा जवाब"], explode_at_end=True)
    assert await _collect() == ["आधा जवाब"]


@pytest.mark.asyncio
async def test_empty_stream_yields_nothing(monkeypatch):
    _patch_llm_stream(monkeypatch, [])
    assert await _collect() == []
