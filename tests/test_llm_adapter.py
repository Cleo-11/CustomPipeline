"""Tests for providers/llm/openai_compat.py with an injected fake client."""
from types import SimpleNamespace

from providers.llm.openai_compat import OpenAICompatLLM
from runtime.types import LLMDelta


def _chunk(content):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])


class FakeStream:
    def __init__(self, parts):
        self._parts = list(parts)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._parts:
            return _chunk(self._parts.pop(0))
        raise StopAsyncIteration


def make_adapter(parts, seen_kwargs):
    async def create(**kwargs):
        seen_kwargs.update(kwargs)
        return FakeStream(parts)

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return OpenAICompatLLM(
        base_url="http://unused", api_key="unused", model="test-model",
        temperature=0.4, client=fake_client,
    )


async def test_yields_deltas_and_passes_request_params():
    seen: dict = {}
    adapter = make_adapter(["एक", " दो"], seen)
    messages = [{"role": "user", "content": "hi"}]

    out = [d async for d in adapter.stream(messages)]

    assert out == [LLMDelta(text="एक"), LLMDelta(text=" दो")]
    assert seen["model"] == "test-model"
    assert seen["temperature"] == 0.4
    assert seen["stream"] is True
    assert seen["max_tokens"] == 160
    assert seen["messages"] is messages


async def test_none_and_empty_deltas_are_skipped():
    adapter = make_adapter([None, "ठीक", None, ""], {})
    out = [d.text async for d in adapter.stream([])]
    assert out == ["ठीक"]
