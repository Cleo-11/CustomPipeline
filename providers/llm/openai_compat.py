"""providers/llm/openai_compat.py — adapter for any OpenAI-compatible endpoint.

Ollama, vLLM, OpenAI, Gemini's compat endpoint, Sarvam — anything speaking
/chat/completions. Which one is a config decision at the composition root,
never a code change.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from runtime.types import LLMDelta, ToolCallRequest

log = logging.getLogger("providers.llm.openai_compat")


class OpenAICompatLLM:
    """Implements runtime.interfaces.LLM.

    Errors are allowed to propagate: runtime.clauses.stream_clauses owns the
    speak-what-we-have fallback, so behavior on stream failure is unchanged
    from the old module-global client.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float,
        max_tokens: int = 160,
        client: Any | None = None,
    ) -> None:
        # `client` is injectable for tests; typed Any so fakes qualify.
        self._client = client if client is not None else AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def healthy(self) -> bool:
        """SupportsHealth probe: GET /models — validates reachability and,
        where the endpoint enforces it, the API key."""
        try:
            await self._client.models.list()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def stream(self, messages: list[Any],
                     tools: list[dict] | None = None) -> AsyncIterator[LLMDelta]:
        kwargs: dict[str, Any] = dict(
            model=self._model,
            messages=messages,
            temperature=self._temperature,
            stream=True,
            max_tokens=self._max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
        stream = await self._client.chat.completions.create(**kwargs)

        # Native tool calls arrive as fragments (index-correlated name +
        # argument-JSON pieces). Accumulate here and yield each call as ONE
        # assembled delta at stream end — the runtime never sees fragments.
        pending: dict[int, dict[str, str]] = {}
        async for part in stream:
            if not part.choices:
                continue
            delta = part.choices[0].delta
            for tc in getattr(delta, "tool_calls", None) or []:
                slot = pending.setdefault(tc.index, {"name": "", "args": ""})
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments
            if delta.content:
                yield LLMDelta(text=delta.content)
        for slot in pending.values():
            request = _assemble_tool_call(slot["name"], slot["args"])
            if request is not None:
                yield LLMDelta(tool_call=request)


def _assemble_tool_call(name: str, args_json: str) -> ToolCallRequest | None:
    """Parse an accumulated tool call; malformed output from small models
    is dropped with a log — the marker strategy exists for exactly them."""
    if not name:
        return None
    try:
        args = json.loads(args_json) if args_json.strip() else {}
    except ValueError:
        log.warning("Malformed tool-call arguments for %s: %r", name, args_json)
        return None
    if not isinstance(args, dict):
        log.warning("Tool-call arguments for %s not an object: %r", name, args)
        return None
    return ToolCallRequest(name=name, args=args)
