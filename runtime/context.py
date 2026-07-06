"""runtime/context.py — conversation-context budgeting (M8).

The proto Context Compiler: today it only caps history growth so a long
call can't inflate LLM latency and cost without bound (the unbounded-
growth hole from the redesign). Oldest turns are evicted whole; the
system prompt always survives. M12 replaces eviction with summarization
and templated prompt assembly behind this same seam.
"""
from __future__ import annotations


def trim_history(messages: list[dict], *, max_messages: int,
                 max_chars: int) -> list[dict]:
    """Return messages within budget: messages[0] (the system prompt) plus
    the newest tail. Both budgets apply to the tail only — the system
    prompt is never counted against them, never evicted."""
    if not messages:
        return messages
    system, tail = messages[0], list(messages[1:])
    while tail and (
        len(tail) > max_messages
        or sum(len(m.get("content", "")) for m in tail) > max_chars
    ):
        tail.pop(0)
    return [system, *tail]
