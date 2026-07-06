"""Tests for runtime/markers.py — generic [[MARKER k=v]] tool-call parsing."""
from runtime.markers import extract_tool_calls

MARKERS = {"BOOK": "book_site_visit", "BROCHURE": "send_brochure"}


def test_plain_text_passes_through():
    clean, calls = extract_tool_calls("नमस्ते, कैसे हैं आप?", MARKERS)
    assert clean == "नमस्ते, कैसे हैं आप?"
    assert calls == []


def test_book_marker_parsed_and_stripped():
    clean, calls = extract_tool_calls(
        "ठीक है। [[BOOK day=Sunday time=4pm name=Rahul]]", MARKERS)
    assert clean == "ठीक है।"
    assert calls == [("book_site_visit",
                      {"day": "Sunday", "time": "4pm", "name": "Rahul"})]


def test_book_marker_multiword_value_and_decimal_time():
    _, calls = extract_tool_calls(
        "[[BOOK day=Sunday time=4.30pm name=Rahul Kumar]]", MARKERS)
    assert calls == [("book_site_visit",
                      {"day": "Sunday", "time": "4.30pm", "name": "Rahul Kumar"})]


def test_bare_marker_yields_empty_args():
    clean, calls = extract_tool_calls("भेज देती हूं। [[BROCHURE]]", MARKERS)
    assert clean == "भेज देती हूं।"
    assert calls == [("send_brochure", {})]


def test_both_markers_case_insensitive():
    clean, calls = extract_tool_calls(
        "done [[book day=Mon time=5pm name=A]] [[brochure]]", MARKERS)
    assert clean == "done"
    assert calls == [
        ("book_site_visit", {"day": "Mon", "time": "5pm", "name": "A"}),
        ("send_brochure", {}),
    ]


def test_unrecognized_marker_is_left_in_text():
    # The parser only speaks for the tools it was given — a marker the
    # agent doesn't own is not silently swallowed.
    clean, calls = extract_tool_calls("ok [[TRANSFER dept=sales]]", MARKERS)
    assert clean == "ok [[TRANSFER dept=sales]]"
    assert calls == []


def test_no_markers_configured_is_passthrough():
    text = "ठीक है। [[BOOK day=Sunday]]"
    clean, calls = extract_tool_calls(text, {})
    assert clean == text
    assert calls == []


def test_marker_split_across_chunks_is_not_recognized():
    # KNOWN LIMITATION (unchanged from the pre-M7 parser): if clause
    # chunking splits a marker across two chunks, neither half parses and
    # the partial marker text leaks into spoken output. Native tool-call
    # dispatch (LLM_TOOL_DISPATCH=native) does not have this failure mode.
    clean, calls = extract_tool_calls("chalo [[BOOK day=Sun", MARKERS)
    assert calls == []
    assert "[[BOOK" in clean
