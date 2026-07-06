"""providers/llm/openai_compat.py — adapter for any OpenAI-compatible endpoint.

Ollama, vLLM, OpenAI, Gemini's compat endpoint, Sarvam — anything speaking
/chat/completions. Which one is a config decision at the composition root,
never a code change.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from runtime.types import LLMDelta


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

    async def stream(self, messages: list[Any]) -> AsyncIterator[LLMDelta]:
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=self._temperature,
            stream=True,
            max_tokens=self._max_tokens,
        )
        async for part in stream:
            delta = part.choices[0].delta.content if part.choices else None
            if delta:
                yield LLMDelta(text=delta)
