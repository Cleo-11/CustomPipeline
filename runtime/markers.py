"""runtime/markers.py — [[BOOK ...]] / [[BROCHURE]] marker parsing.

Interim home: markers are the current tool-dispatch mechanism — the model
appends them to replies and the session strips them before TTS. The tool
registry (M7) demotes this to one dispatch strategy among several and
moves it behind that interface.
"""
from __future__ import annotations

import re

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
