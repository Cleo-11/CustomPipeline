"""runtime/markers.py — [[MARKER k=v ...]] token parsing.

Since M7 this is the text-level half of the *marker dispatch strategy*
(runtime/tools.py): tools declare a marker token in their ToolSpec, the
model appends `[[TOKEN key=value ...]]` to replies, and this module strips
recognized tokens out of the spoken text and returns them as tool calls.

Markers a call's agent doesn't own are left in the text untouched — the
parser only speaks for the tools it was given.
"""
from __future__ import annotations

import re
from typing import Mapping

_MARKER_RE = re.compile(r"\[\[(\w+)([^\]]*)\]\]")
_KV_RE = re.compile(r"(\w+)\s*=\s*([^=\]]+?)(?=\s+\w+=|$)")


def extract_tool_calls(
    text: str, markers: Mapping[str, str]
) -> tuple[str, list[tuple[str, dict]]]:
    """Parse recognized markers out of `text`.

    `markers` maps an UPPERCASE marker token to the tool name it triggers.
    Returns (clean_spoken_text, [(tool_name, args), ...]) — args are the
    marker's k=v pairs, lowercased keys, {} for bare markers.
    """
    calls: list[tuple[str, dict]] = []

    def _consume(m: re.Match[str]) -> str:
        tool = markers.get(m.group(1).upper())
        if tool is None:
            return m.group(0)  # not ours: leave it in the text
        args = {k.lower(): v.strip() for k, v in _KV_RE.findall(m.group(2))}
        calls.append((tool, args))
        return ""

    clean = _MARKER_RE.sub(_consume, text).strip()
    return clean, calls
