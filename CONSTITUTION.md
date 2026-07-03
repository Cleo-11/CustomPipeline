# The Runtime Constitution

This document defines the architectural philosophy of this project. It is the
first source of truth: every design decision, refactor, and feature must comply
with it. If a proposed change conflicts with an article, the conflict must be
surfaced and argued **before** any code is written. CLAUDE.md binds the
engineering process to this document; RUNTIME_REDESIGN.md describes the target
architecture; ROADMAP.md sequences the work. None of them overrides this
document — they implement it.

---

## Article I — The runtime owns the conversation

The product is a conversational AI runtime, not a voice bot. Turn-taking,
interruption, endpointing, context, memory, and dialogue policy are decided by
our code. No external service ever decides when the agent listens, thinks, or
speaks. The runtime — not any provider — is the core intellectual property.

## Article II — Transports are plugs

Voice over Vobiz is one transport, not the architecture. The conversation
engine must never depend on a specific transport, its wire format, its event
names, or its framing. Adding WhatsApp, SMS, WebRTC, browser chat, or any
future channel must require a new transport adapter and nothing else.

## Article III — Providers are capabilities, never behavior

External services supply telephony, STT, TTS, LLM inference, storage, and
notifications — nothing more. Every provider lives behind an interface owned
by the runtime; provider names never appear in orchestration code. Swapping a
provider changes one adapter. We never design around managed conversational
runtimes (Vapi, Retell, Bland, LiveKit Agents, Pipecat, LangGraph, or
successors): owning the orchestration layer is the point of the project.

## Article IV — The Turn Engine is the core IP

The heart of the runtime is an explicit, inspectable state machine over the
conversational turn. It must be pure — free of I/O, provider calls, and wall
clocks — consuming normalized events and emitting intents. Latency behavior
(streaming, clause chunking, barge-in, endpointing) is engineered here,
deliberately, not inherited from a vendor's defaults.

## Article V — Agents are data

The process is an engine; an agent is a record. Persona, prompts, knowledge,
voice, tools, provider policy, and turn policy are configuration loaded at
call start — never constants compiled into the runtime. Any change that
hardcodes one agent's identity into engine code is unconstitutional.

## Article VI — Business logic lives in tools and workflows

The runtime executes tools; it does not know what they do. Tools are
registered with a name, schema, permissions, timeout, and owner. Booking a
site visit is Priya's business, not the engine's. If deleting an agent would
leave dead business logic inside a runtime module, the logic is in the wrong
place.

## Article VII — Events over coupling

Everything important emits a typed event. Analytics, logging, recording,
metrics, and dashboards are subscribers — never inline concerns on the audio
path. A new consumer of runtime activity must require zero changes to runtime
logic.

## Article VIII — The core is deterministic and replayable

Given the same recorded event stream, the Turn Engine must reproduce the same
state transitions and intents. Every behavioral change to turn-taking must be
expressible as a failing-then-passing replay test. Nondeterminism (clocks,
network, randomness) is injected at the edges, never embedded in the core.

## Article IX — Evolve incrementally; stay deployable

No rewrites. Refactors move in small migrations, each leaving the repository
deployable and behavior preserved unless a change is named, justified, and
tested. Regressions in working conversational behavior are unacceptable. Tests
precede the refactors they protect.

## Article X — Observe everything

Latency is a feature and must be measured, not asserted: per-turn timings,
provider health, and failure events are first-class outputs of the runtime.
A claim about performance that no metric backs is a hypothesis, not a fact.

## Article XI — Security is a floor, not a feature

Secrets never enter version control; missing credentials fail startup loudly
rather than degrading silently; every externally reachable endpoint
authenticates its caller. Convenience never justifies an unauthenticated
surface.

## Article XII — Build nothing speculative

Subsystems earn their existence by a concrete, present need. Optimize for
maintainability, testability, extensibility, and latency — never for writing
the least code, and never for architecture that impresses before it serves.
When in doubt, build the smaller thing that preserves the seam.

---

## Amendments

This constitution changes only by explicit, recorded decision of the project
owner, with the reasoning captured in the amending commit. Code never amends
it implicitly.
