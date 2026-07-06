"""runtime/tools.py — tool registry, executor, and dispatch strategies.

Purpose
    Tools are registered capabilities; the runtime executes them without
    knowing what they do (CLAUDE.md tool rules). An agent lists tool names
    in its record; the composition root resolves them against the registry
    and wires the per-agent dispatch strategy.

Responsibilities
    - ToolSpec: name / description / JSON-schema parameters / handler /
      owner / timeout / retry — and the legacy marker token that triggers
      it under the marker strategy.
    - ToolRegistry: name → spec. Populated at the composition root by the
      agents' tool modules (dynamic loading from agent specs is the P3
      plugin SDK, not built until demanded).
    - ToolExecutor: fire-and-forget execution — its own task, per-spec
      timeout and bounded retries, ToolCalled/Succeeded/Failed audit
      events on the bus. It NEVER blocks the reply pipeline, and a tool
      crash becomes an event, not dead air. (Absorbs M6's session-level
      `_run_tool` seed.)
    - Dispatch strategies (one interface, two implementations, per-agent
      choice via LLMPolicy.tool_dispatch):
        * MarkerToolStrategy — the model writes [[MARKER k=v]] tokens in
          its text; we strip and dispatch them. DEFAULT: the observed
          production reality (small Hinglish-capable models, OpenAI-compat
          quirks) is exactly where native tool-calling is unreliable
          (ROADMAP §1.5 amendment 5).
        * NativeToolStrategy — passes OpenAI-format tool schemas to the
          LLM and dispatches the assembled tool_call deltas the adapter
          yields.

Tool results are not fed back to the model — both strategies keep the
fire-and-forget contract the markers always had. Feeding results back is
an extension point on the strategy interface, not a rewrite.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Protocol, Sequence

from runtime.agent import AgentConfig
from runtime.clauses import stream_clauses
from runtime.events import EventEmitter, ToolCalled, ToolFailed, ToolSucceeded
from runtime.interfaces import LLM
from runtime.markers import extract_tool_calls

log = logging.getLogger("runtime.tools")


@dataclass(frozen=True)
class ToolContext:
    """What the runtime knows about the call a tool runs inside."""

    call_id: str
    caller_number: str
    caller_name: str
    agent: AgentConfig


ToolHandler = Callable[[ToolContext, dict], Awaitable[None]]
OnToolCall = Callable[[str, dict], None]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict            # JSON Schema, used by the native strategy
    handler: ToolHandler
    owner: str                  # who owns the business logic
    marker: str | None = None   # UPPERCASE token for the marker strategy
    timeout_s: float = 10.0
    retries: int = 0            # additional attempts after the first


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def resolve(self, names: Iterable[str]) -> list[ToolSpec]:
        """Agent tool names → specs. Unknown names log and are skipped —
        a misconfigured agent loses a capability, not the call."""
        specs = []
        for name in names:
            spec = self._specs.get(name)
            if spec is None:
                log.warning("Agent references unregistered tool %r", name)
            else:
                specs.append(spec)
        return specs


class ToolExecutor:
    """Runs tools off the voice loop with audit events."""

    def __init__(self, registry: ToolRegistry, bus: EventEmitter) -> None:
        self._registry = registry
        self._bus = bus

    def dispatch(self, name: str, args: dict,
                 ctx: ToolContext) -> asyncio.Task | None:
        """Fire and forget: synchronous, returns immediately."""
        spec = self._registry.get(name)
        if spec is None:
            log.warning("Dispatch of unregistered tool %r dropped", name)
            self._bus.emit(ToolFailed(call_id=ctx.call_id, tool=name,
                                      error="unregistered tool"))
            return None
        return asyncio.create_task(self._run(spec, args, ctx),
                                   name=f"tool-{spec.name}")

    async def _run(self, spec: ToolSpec, args: dict, ctx: ToolContext) -> None:
        self._bus.emit(ToolCalled(call_id=ctx.call_id, tool=spec.name))
        attempts = spec.retries + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.wait_for(spec.handler(ctx, args),
                                       timeout=spec.timeout_s)
            except Exception as e:  # noqa: BLE001 — a tool may fail arbitrarily
                last_error = str(e) or type(e).__name__
                log.warning("Tool %s attempt %d/%d failed: %s",
                            spec.name, attempt, attempts, last_error)
            else:
                self._bus.emit(ToolSucceeded(call_id=ctx.call_id,
                                             tool=spec.name))
                return
        log.error("Tool %s failed after %d attempt(s): %s",
                  spec.name, attempts, last_error)
        self._bus.emit(ToolFailed(call_id=ctx.call_id, tool=spec.name,
                                  error=last_error))


# -------------------------------------------------------------- strategies
class ToolDispatchStrategy(Protocol):
    """Turns an LLM reply stream into speakable clauses, routing whatever
    tool calls it carries (markers or native deltas) to `on_tool`."""

    def clauses(self, llm: LLM, messages: list[Any],
                on_tool: OnToolCall) -> AsyncIterator[str]: ...


class MarkerToolStrategy:
    """Default/fallback: [[MARKER k=v]] tokens parsed out of clause text."""

    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        self._markers = {s.marker: s.name for s in specs if s.marker}

    async def clauses(self, llm: LLM, messages: list[Any],
                      on_tool: OnToolCall) -> AsyncIterator[str]:
        async for chunk in stream_clauses(llm.stream(messages)):
            clean, calls = extract_tool_calls(chunk, self._markers)
            for name, args in calls:
                on_tool(name, args)
            if clean:
                yield clean


class NativeToolStrategy:
    """Native LLM tool-calls: schemas go up with the request; the adapter
    yields assembled ToolCallRequest deltas alongside the text stream."""

    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        self._payload: list[dict] | None = [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters,
                },
            }
            for s in specs
        ] or None

    async def clauses(self, llm: LLM, messages: list[Any],
                      on_tool: OnToolCall) -> AsyncIterator[str]:
        async def _text_only():  # type: ignore[no-untyped-def]
            async for delta in llm.stream(messages, tools=self._payload):
                if delta.tool_call is not None:
                    on_tool(delta.tool_call.name, delta.tool_call.args)
                if delta.text:
                    yield delta

        async for clause in stream_clauses(_text_only()):
            yield clause
