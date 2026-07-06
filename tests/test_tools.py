"""Tool registry, executor, and dispatch strategy tests."""
import asyncio

import pytest

from runtime import agent_registry
from runtime.tools import (
    MarkerToolStrategy,
    NativeToolStrategy,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolSpec,
)
from runtime.types import LLMDelta, ToolCallRequest


class Emitted:
    """Synchronous EventEmitter recording every event."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def kinds(self):
        return [type(e).__name__ for e in self.events]


def make_ctx():
    return ToolContext(call_id="c1", caller_number="+911234567890",
                       caller_name="Rahul", agent=agent_registry.resolve())


def spec_of(handler, *, name="t", timeout_s=1.0, retries=0, marker=None):
    return ToolSpec(name=name, description="d", parameters={"type": "object"},
                    handler=handler, owner="test", marker=marker,
                    timeout_s=timeout_s, retries=retries)


# ------------------------------------------------------------------ registry
def test_registry_register_get_and_duplicate():
    reg = ToolRegistry()

    async def h(ctx, args):
        return None

    reg.register(spec_of(h, name="a"))
    assert reg.get("a") is not None
    assert reg.get("missing") is None
    with pytest.raises(ValueError):
        reg.register(spec_of(h, name="a"))


def test_registry_resolve_skips_unknown_names(caplog):
    reg = ToolRegistry()

    async def h(ctx, args):
        return None

    reg.register(spec_of(h, name="known"))
    specs = reg.resolve(["known", "ghost"])
    assert [s.name for s in specs] == ["known"]


# ------------------------------------------------------------------ executor
async def test_executor_success_emits_called_then_succeeded():
    reg, bus = ToolRegistry(), Emitted()
    got = []

    async def h(ctx, args):
        got.append((ctx.call_id, args))

    reg.register(spec_of(h, name="save"))
    task = ToolExecutor(reg, bus).dispatch("save", {"day": "Sun"}, make_ctx())
    assert task is not None
    await task

    assert got == [("c1", {"day": "Sun"})]
    assert bus.kinds() == ["ToolCalled", "ToolSucceeded"]
    assert all(e.tool == "save" and e.call_id == "c1" for e in bus.events)


async def test_executor_unknown_tool_emits_failed_without_task():
    bus = Emitted()
    task = ToolExecutor(ToolRegistry(), bus).dispatch("ghost", {}, make_ctx())
    assert task is None
    assert bus.kinds() == ["ToolFailed"]
    assert "unregistered" in bus.events[0].error


async def test_executor_retries_then_succeeds():
    reg, bus = ToolRegistry(), Emitted()
    attempts = []

    async def flaky(ctx, args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")

    reg.register(spec_of(flaky, name="flaky", retries=1))
    task = ToolExecutor(reg, bus).dispatch("flaky", {}, make_ctx())
    assert task is not None
    await task

    assert len(attempts) == 2
    # ToolCalled once (per dispatch, not per attempt), then success.
    assert bus.kinds() == ["ToolCalled", "ToolSucceeded"]


async def test_executor_exhausted_retries_emit_failed_with_error():
    reg, bus = ToolRegistry(), Emitted()

    async def broken(ctx, args):
        raise RuntimeError("whatsapp api down")

    reg.register(spec_of(broken, name="broken", retries=1))
    task = ToolExecutor(reg, bus).dispatch("broken", {}, make_ctx())
    assert task is not None
    await task

    assert bus.kinds() == ["ToolCalled", "ToolFailed"]
    assert bus.events[1].error == "whatsapp api down"


async def test_executor_timeout_becomes_failed():
    reg, bus = ToolRegistry(), Emitted()

    async def hangs(ctx, args):
        await asyncio.sleep(5)

    reg.register(spec_of(hangs, name="hangs", timeout_s=0.01))
    task = ToolExecutor(reg, bus).dispatch("hangs", {}, make_ctx())
    assert task is not None
    await task

    assert bus.kinds() == ["ToolCalled", "ToolFailed"]
    assert "TimeoutError" in bus.events[1].error


# ---------------------------------------------------------------- strategies
class ScriptedLLM:
    """Yields the given LLMDeltas; records the tools kwarg it was given."""

    def __init__(self, deltas):
        self._deltas = deltas
        self.seen_tools = "UNSET"

    async def stream(self, messages, tools=None):
        self.seen_tools = tools
        for d in self._deltas:
            yield d


async def test_marker_strategy_strips_and_dispatches():
    async def h(ctx, args):
        return None

    specs = [spec_of(h, name="book_site_visit", marker="BOOK"),
             spec_of(h, name="send_brochure", marker="BROCHURE")]
    strategy = MarkerToolStrategy(specs)
    llm = ScriptedLLM([LLMDelta(
        text="ठीक है। [[BOOK day=Sun time=4pm name=R]] [[BROCHURE]]")])
    calls = []

    clauses = [c async for c in strategy.clauses(
        llm, [], lambda n, a: calls.append((n, a)))]

    assert clauses == ["ठीक है।"]
    assert calls == [
        ("book_site_visit", {"day": "Sun", "time": "4pm", "name": "R"}),
        ("send_brochure", {}),
    ]
    assert llm.seen_tools is None  # marker strategy sends no schemas


async def test_native_strategy_passes_schemas_and_routes_tool_deltas():
    async def h(ctx, args):
        return None

    specs = [spec_of(h, name="book_site_visit")]
    strategy = NativeToolStrategy(specs)
    llm = ScriptedLLM([
        LLMDelta(text="ठीक है, book कर देती हूं।"),
        LLMDelta(tool_call=ToolCallRequest(
            name="book_site_visit", args={"day": "Sun"})),
    ])
    calls = []

    clauses = [c async for c in strategy.clauses(
        llm, [], lambda n, a: calls.append((n, a)))]

    assert clauses == ["ठीक है, book कर देती हूं।"]
    assert calls == [("book_site_visit", {"day": "Sun"})]
    assert llm.seen_tools == [{
        "type": "function",
        "function": {"name": "book_site_visit", "description": "d",
                     "parameters": {"type": "object"}},
    }]
