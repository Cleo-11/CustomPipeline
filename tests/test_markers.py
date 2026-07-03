"""Tests for runtime/markers.py — [[BOOK]]/[[BROCHURE]] marker parsing."""
from runtime.markers import extract_actions


def test_plain_text_passes_through():
    clean, book, brochure = extract_actions("नमस्ते, कैसे हैं आप?")
    assert clean == "नमस्ते, कैसे हैं आप?"
    assert book is None
    assert brochure is False


def test_book_marker_parsed_and_stripped():
    clean, book, brochure = extract_actions("ठीक है। [[BOOK day=Sunday time=4pm name=Rahul]]")
    assert clean == "ठीक है।"
    assert book == {"day": "Sunday", "time": "4pm", "name": "Rahul"}
    assert brochure is False


def test_book_marker_multiword_value_and_decimal_time():
    _, book, _ = extract_actions("[[BOOK day=Sunday time=4.30pm name=Rahul Kumar]]")
    assert book == {"day": "Sunday", "time": "4.30pm", "name": "Rahul Kumar"}


def test_brochure_marker():
    clean, book, brochure = extract_actions("भेज देती हूं। [[BROCHURE]]")
    assert clean == "भेज देती हूं।"
    assert book is None
    assert brochure is True


def test_both_markers_case_insensitive():
    clean, book, brochure = extract_actions("done [[book day=Mon time=5pm name=A]] [[brochure]]")
    assert clean == "done"
    assert book == {"day": "Mon", "time": "5pm", "name": "A"}
    assert brochure is True


def test_marker_split_across_chunks_is_not_recognized():
    # KNOWN LIMITATION: if clause chunking splits a marker across two chunks,
    # neither half parses and the partial marker text leaks into spoken output.
    # The tool-call dispatch in M7 removes this failure mode.
    clean, book, _ = extract_actions("chalo [[BOOK day=Sun")
    assert book is None
    assert "[[BOOK" in clean
