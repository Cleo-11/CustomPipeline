# Voice Runtime Redesign

**From:** a single-tenant Hinglish real-estate calling agent (Northern Heights / Priya)
**To:** a multi-tenant conversational-voice **runtime** — the layer a company like OpenAI Realtime, ElevenLabs Conversational AI, Retell, or Bland ships as its core product.

The thing that makes this worth doing rather than adopting Pipecat/LiveKit: **you already own your turn-taking.** Retell's whole pitch is that it "runs its own turn-taking model rather than stitching together public APIs." Your `session.py` is a small, hand-built version of exactly that. The redesign's job is to keep that instinct and give it a real chassis: pluggable providers, agents-as-data, and a shape that survives thousands of concurrent calls.

---

## 1. The one thing we are preserving

Everything below can change. This cannot: **the orchestrator owns the conversation, not the vendors.**

Today that means `CallSession` personally decides when the caller has finished (`ENDPOINT_SILENCE_MS` + VAD), when to barge in (sustained RMS **and** webrtcvad, `BARGEIN_MIN_FRAMES`), and when to start speaking a clause before the LLM has finished writing (`stream_sentences`, `_MIN_FIRST_CHUNK`). That is your product's feel. Managed frameworks take those decisions away from you in exchange for less code. You chose control. The redesign formalizes that control into something testable and reusable instead of dissolving it — see §5, the Turn Engine.

The current README frames Pipecat/LiveKit as "the faster path if you'd rather not own the orchestration." This document is the other branch: own it, but industrialize it.

---

## 2. Honest audit of what exists

The current design is genuinely good for what it is, and about half of it is load-bearing platform code hiding inside a demo. Naming the seams matters before moving anything.

**What's already right (keep the ideas, generalize the code):**

- **Everything streams and pipelines.** Caller audio → STT runs continuously; LLM tokens are chunked into clauses at sentence boundaries and flushed to TTS early; TTS frames play while later clauses are still being synthesized. This is the correct latency architecture and most of the platform's value.
- **Turn-taking is explicit and local.** Endpointing, barge-in, and interrupt are real code you can reason about, not a vendor black box.
- **Provider-swap seams already exist in spirit.** `LLM_BASE_URL` pointing at any OpenAI-compatible server (Ollama → vLLM → Sarvam) is exactly the abstraction to generalize to STT/TTS/transport.
- **Audio bridge is pure and tested.** `audio.py` is stateless numpy G.711/resampling — precisely the kind of code that should stay a leaf utility.

**What blocks it from being a runtime (the platform-hostile parts):**

| Coupling | Where | Why it blocks a platform |
|---|---|---|
| **One hardcoded agent** | `config.py` — Priya persona, Northern Heights KB, greeting, booking markers all module-level constants | A platform runs N agents for M tenants. Agent identity must be **data**, resolved per call, not a Python import. |
| **Providers imported by name** | `session.py` imports `SarvamSTT`, `SarvamTTS`; `llm.py` hardcodes the OpenAI client; transport is Vobiz-shaped JSON inline in `session.py`/`server.py` | Adding Deepgram/ElevenLabs/Twilio means editing the orchestrator. Providers must sit behind interfaces the core never names. |
| **Naming lies** | `sarvam_stt.py` actually talks to **Deepgram** (`nova-2`); `SarvamSTT` class is a Deepgram client | Symptom of no abstraction boundary — the concrete vendor leaked into the interface name. An interface would have made this impossible. |
| **Business logic in the loop** | `booking.py` markers (`[[BOOK ...]]`) parsed out of the token stream in `llm.py` | Tool/function calling is a first-class runtime concern; per-agent tools can't be a regex on one company's brochure flow. |
| **No tenancy, no isolation, no lifecycle** | state lives in a `CallSession` object and a `bookings.jsonl` file | No per-tenant keys/quotas/data separation, no recording/transcript store, no session registry, no metrics. |
| **Config is global + import-time** | `load_dotenv()` at module load, `_get()` raising at import | Fine for one deployment; a control plane needs config resolved per-request from a store, not the process environment. |

The mental model shift: today **the process is the agent.** In the runtime, **the process is an engine; the agent is a row you load.**

---

## 3. Target architecture

Five planes. The arrows that matter are the ones that *don't* exist: the Orchestration Core never imports a vendor, and the vendors never see tenant/agent identity beyond an opaque handle.

```
                         ┌──────────────────────── CONTROL PLANE ───────────────────────┐
                         │  Agent registry (persona, KB, tools, voice, provider policy) │
                         │  Tenant / API keys / quotas / secrets   Config resolver      │
                         └───────────────▲───────────────────────────────┬──────────────┘
                                         │ resolve(agent_id)              │ (async, off hot path)
                                         │                                ▼
 PSTN / SIP / WebRTC ─► EDGE / TRANSPORT ─►  SESSION RUNTIME  ──────► DATA PLANE
   Twilio│Vobiz│Telnyx│      (adapter)    │   ┌─────────────────┐    recordings, transcripts,
   OpenAI-Realtime-WS       normalizes    │   │  TURN ENGINE    │    call events, tool audit,
                            to AudioFrame  │   │ (state machine) │    metrics / traces
                                          │   └───▲────┬────────┘
                                          │       │    │ speak() / cancel()
                          ┌───────────────┴───────┴────┴──────────────┐
                          │            PROVIDER PLANE                  │
                          │  Transport │ STT │ LLM │ TTS │ Tools       │
                          │  each behind a capability-typed interface  │
                          └───────────────────────────────────────────┘
```

**Edge / Transport plane.** Terminates the carrier connection (Vobiz WS today; Twilio Media Streams, Telnyx, raw WebRTC, or an OpenAI-Realtime-style client socket tomorrow) and normalizes it to a single internal contract: a duplex stream of `AudioFrame`s plus lifecycle events (`call.start`, `call.stop`, `dtmf`, `mark/checkpoint`). `server.py`'s `/answer` XML and `/ws` loop become **one transport adapter among several**, not the server.

**Session Runtime.** One `Session` per live call. Owns the Turn Engine instance, the resolved `AgentConfig`, the provider handles for this call, and the conversation state. This is today's `CallSession`, minus the vendor imports and the hardcoded persona.

**Orchestration Core (the Turn Engine).** Pure conversation-control state machine. Consumes normalized STT/VAD/transport events, emits intents (`start_speaking(text)`, `flush_clause`, `cancel_output`, `commit_user_turn`). Imports **no vendor and no I/O** — see §5. This is the crown jewel and the part you keep.

**Provider plane.** Swappable adapters behind capability-typed interfaces (§4). The core holds `self.stt`, `self.tts`, `self.llm`, `self.transport` as interfaces; a factory wires concretes from the agent's provider policy.

**Control plane.** Agent registry + tenant/auth/secrets/config. Given an inbound/outbound call, resolves *which agent* and *whose keys/limits* apply, and hands the Session a fully-formed `AgentConfig` + provider policy. Async, off the audio hot path.

**Data plane.** Recordings, transcripts, structured call events, tool-call audit, metrics/traces. `booking.jsonl` becomes one consumer of a general event stream, not a special case wired into `llm.py`.

---

## 4. Provider abstraction — the core sells this

The platforms differentiate on *turn-taking feel* and *provider breadth*. Breadth requires that the core name **capabilities**, never vendors. Four interfaces, each async-iterator shaped to preserve streaming.

```python
# transport.py — carrier adapters normalize to this
class Transport(Protocol):
    async def events(self) -> AsyncIterator[TransportEvent]: ...   # start/stop/dtmf/media→AudioFrame
    async def play(self, frame: AudioFrame) -> None: ...           # egress, codec-negotiated
    async def clear(self) -> None: ...                             # barge-in: drop buffered egress
    async def checkpoint(self, name: str) -> None: ...
    @property
    def audio_format(self) -> AudioFormat: ...                     # µlaw/8k, PCM/16k, Opus…

# stt.py — streaming recognizer + turn signals
class STT(Protocol):
    async def send(self, frame: AudioFrame) -> None: ...
    async def events(self) -> AsyncIterator[STTEvent]: ...  # Partial|Final|SpeechStarted|SpeechStopped
    @property
    def emits_endpoint(self) -> bool: ...   # does provider signal end-of-turn, or do WE?

# llm.py — token stream + native tool calls
class LLM(Protocol):
    async def stream(self, ctx: Conversation, tools: list[ToolSpec]) -> AsyncIterator[LLMDelta]:
        ...  # yields TextDelta | ToolCall | Done

# tts.py — text (or SSML) → audio frames in the transport's format
class TTS(Protocol):
    async def synthesize(self, text: str, fmt: AudioFormat) -> AsyncIterator[AudioFrame]: ...
    @property
    def supports_streaming_input(self) -> bool: ...   # can we feed clauses as they arrive?
```

**Capability negotiation is the subtle, important part.** Providers disagree on who owns turn detection. Deepgram (`endpointing`, `utterance_end_ms`, `vad_events`) and OpenAI Realtime (`server_vad` vs `semantic_vad`, emitting `input_audio_buffer.speech_started/stopped`) will endpoint *for you*; a raw STT will not. The Turn Engine must adapt:

- `STT.emits_endpoint == True` → the engine **trusts provider VAD events** and treats its own silence timer as a safety fallback (this is what your Deepgram config already does with `endpointing=300` + `utterance_end_ms=1000`).
- `emits_endpoint == False` → the engine **runs its own endpointer** (silence timer + local webrtcvad), which is the `ENDPOINT_SILENCE_MS` path you built.

Same for barge-in: some transports/providers signal speech-start; where they don't, the engine falls back to the RMS+VAD detector in `_on_media`. The rule: **the engine always has a local fallback, and prefers provider signals when the capability flag says they exist.** That single rule is what lets one orchestrator drive both a "bring-your-own-models" pipeline and an OpenAI-Realtime-style all-in-one, which is a place most homegrown stacks fracture.

`audio.py` stays exactly as it is — a pure codec/resample leaf that adapters call. It's already the right shape. The only change: `AudioFormat` becomes explicit so a PCM-native transport skips the µlaw round-trip instead of assuming 8k telephony.

---

## 5. The Turn Engine — formalizing your philosophy

Today turn logic is implicit across booleans in `CallSession`: `_is_speaking`, `_greeting_active`, `_bargein_run`, `_pending_user`, `_speak_ended_at`, `_turn_seq`, and three timers/tasks. It works, but the rules live in the gaps between `if` statements, which makes them hard to test and easy to regress. The redesign makes the machine **explicit**: one enum, well-defined transitions, and I/O pushed to the edges so the whole thing is unit-testable with no sockets.

**States**

```
LISTENING ──speech detected──► USER_SPEAKING ──endpoint──► THINKING
    ▲                                                          │ first clause ready
    │                                                          ▼
    └──────── output finished / cancelled ───────────── AGENT_SPEAKING
                                                               │
                             barge-in (RMS+VAD or provider) ───┘  ► INTERRUPTED ─► LISTENING
```

- **LISTENING** — idle; forwarding audio to STT; watching for speech-start.
- **USER_SPEAKING** — accumulating partials into `_pending_user`; endpoint timer armed on each final.
- **THINKING** — user turn committed; LLM streaming; optional `THINKING_FILLER` ("हम्म") masks time-to-first-token. This is the *only* place a filler plays, and it's now a state property, not a side effect.
- **AGENT_SPEAKING** — clauses flushed to TTS and played; barge-in armed. Each turn carries a monotonically increasing `turn_seq`; a stale sequence means "a newer turn started, drop these frames" (your existing `if seq != self._turn_seq: return` guard, promoted to a first-class rule).
- **INTERRUPTED** — transient: cancel the speak task, `transport.clear()`, reset barge counters, return to LISTENING. (Your `_interrupt()`.)

**Why an explicit machine pays off here specifically:** the hard-won details in your current code — barge-in needs *sustained* energy **and** VAD confirmation so a cough doesn't cut the agent off; a partial transcript arriving mid-speech only interrupts if you've been speaking >0.5s; the greeting is uninterruptible until it finishes — are exactly the kind of rules that rot when they're scattered. As transition guards they become named, testable predicates: `should_barge_in(frame)`, `should_endpoint()`, `is_stale(turn_seq)`. You can replay a recorded call's event log through the engine in a test and assert the state trace, with zero network.

**Endpointing strategy becomes pluggable** behind the engine, because it's the single biggest lever on conversational feel and the thing the big platforms are actively competing on:

- `FixedSilenceEndpointer` — today's `ENDPOINT_SILENCE_MS`. Cheap, predictable.
- `ProviderEndpointer` — trust Deepgram/OpenAI VAD events (capability-gated per §4).
- `SemanticEndpointer` — mirror OpenAI's `semantic_vad`: a lightweight classifier decides "did they actually finish, or are they mid-thought?" and stretches the timeout when the user sounds unfinished. This is the natural place your "very human Hinglish" ambition graduates from *word choice* to *timing*, which is where it's actually felt on a call.

The Turn Engine is provider-agnostic, I/O-free, and the piece worth writing tests and, eventually, a real turn-detection model against. It is the asset.

---

## 6. Agents as data, tenants as isolation

The single largest structural change: **`config.py` stops being the agent.** It splits into (a) *engine/runtime tunables* (latency knobs, defaults) and (b) an **`AgentConfig`** record loaded per call from the control plane.

```python
@dataclass
class AgentConfig:
    agent_id: str
    tenant_id: str
    system_prompt: str                 # was config.SYSTEM_PROMPT
    greeting: str                      # was config.GREETING
    knowledge: KnowledgeRef            # was KNOWLEDGE_BASE — inline, or a retrieval handle
    voice: VoiceConfig                 # tts model/speaker/language/pace
    stt_policy: STTPolicy              # model, language, who-endpoints
    llm_policy: LLMPolicy              # base_url/model/temp/max_tokens
    tools: list[ToolSpec]              # replaces the [[BOOK]]/[[BROCHURE]] regex
    turn_policy: TurnPolicy            # endpoint ms, barge-in thresholds, filler, endpointer type
```

Priya/Northern Heights becomes **one row** in the agent registry — the reference agent, not the runtime. A call's flow: transport reports `call.start` with a called/calling number or a client-supplied `agent_id` → control plane resolves `AgentConfig` + tenant secrets → Session constructs providers from the policy and hands the Turn Engine the persona. Nothing in the hot path reads process env or imports a persona module.

**Tenant isolation** rides along: provider API keys resolve per-tenant (not one global `SARVAM_API_KEY`), quotas/concurrency caps enforce per-tenant, and data-plane writes are tenant-scoped. This is the difference between "a deployment" and "a platform," and it's almost entirely a control-plane + config-resolution change — the audio loop doesn't care.

**Tools replace booking markers.** The `[[BOOK day=… name=…]]` convention parsed in `llm.py` is a private function-calling protocol for one company. Generalize to declared per-agent tools dispatched from the LLM stream (native tool-calls where the model supports them; the marker-parser demoted to a fallback adapter for models that don't). `booking.save_booking` / `send_brochure` become **one tenant's tool handlers**, registered against their agent — not runtime code. Tool execution stays off the voice loop exactly as you already do it (`asyncio.create_task`, best-effort), and every call is audited to the data plane.

---

## 7. Scale, lifecycle, observability

Your scaling instinct is already right and should be stated as the model, not an afterthought in the README: **one call pins one event loop; scale horizontally by running many single-worker processes behind a load balancer.** Voice sessions are sticky and stateful — a call cannot be load-balanced mid-stream — so the unit of scale is the session, and the router's job is to place a *new* call on a warm worker with headroom, then leave it there.

What the runtime adds around that model:

- **Session registry.** An addressable directory of live sessions (`session_id → worker`) so control operations (transfer, supervisor listen/whisper, force-hangup, "push an update to this call") can find a call. In-memory per worker + a shared index (Redis-class) across workers.
- **Lifecycle & draining.** Explicit `Session` states (starting → live → draining → closed), graceful drain on deploy (stop accepting new calls, let live ones finish), idempotent teardown. Today `cleanup()` runs on disconnect; formalize it so a rolling deploy never kills a live call.
- **Data plane / recording.** A structured event stream per call — `user.transcript`, `agent.transcript`, `barge_in`, `tool.call`, `turn.latency` — that fans out to recording, transcript storage, and analytics. `bookings.jsonl` becomes one subscriber, not a hardcoded sink.
- **Observability, hot-path-aware.** The metrics that matter here are the latency-budget stages the README already enumerates — endpoint→first-token, first-token→first-TTS-byte, barge-in reaction time, per-turn round-trip. Emit them as spans/metrics per turn (async, never blocking playback), so you can watch the 700–900 ms budget per tenant/agent/provider in production and catch a provider regressing. This is also how you'd A/B two endpointers or two TTS vendors on live traffic.
- **Backpressure & failure.** Per-provider timeouts, circuit-breakers, and fallbacks (primary TTS 5xx → secondary), so one vendor blip degrades instead of dropping the call. The provider interfaces of §4 are what make a fallback a config choice rather than a rewrite.

---

## 8. Migration path (keep the call loop alive the whole way)

Incremental, each step shippable, no big-bang rewrite. The call keeps working after every step.

1. **Extract provider interfaces** (§4) and make the existing Deepgram/Sarvam/Ollama/Vobiz code the first concrete adapters. *Also fixes the `sarvam_stt.py`-is-actually-Deepgram naming lie* — rename to `providers/stt/deepgram.py` behind the `STT` interface. Behavior identical; imports inverted.
2. **Lift the Turn Engine out of `CallSession`** into a pure state machine with the current booleans as explicit states, I/O injected. Add the replay test harness. `CallSession` becomes a thin adapter wiring transport+providers+engine.
3. **Introduce `AgentConfig`** and load Priya from a one-row registry (a JSON/DB seed) instead of `config.py` constants. Runtime now technically multi-agent even with one agent.
4. **Add the transport adapter seam** so Vobiz is one adapter; stub a Twilio Media Streams or OpenAI-Realtime-client adapter to prove the boundary.
5. **Generalize tools** — booking/brochure become registered tool handlers; marker-parser demoted to a fallback adapter.
6. **Control + data planes** — tenant keys/quotas, session registry, event stream, per-turn metrics. This is where it becomes a platform rather than a well-factored app.

Steps 1–2 are pure refactors with zero behavior change and unlock most of the testability win; do them first.

---

## 9. What *not* to build

The failure mode is rebuilding Pipecat/LiveKit badly. Guardrails:

- **Don't reinvent the media server.** Keep leaning on the carrier's WebSocket/SIP media (Vobiz/Twilio/Telnyx). The runtime's value is orchestration + provider breadth + tenancy, not RTP plumbing.
- **Don't let the core learn a vendor's name.** The day the orchestrator imports `deepgram` or `elevenlabs` directly, the abstraction is dead. The `sarvam_stt.py` naming slip is the canary — an interface makes that class of mistake impossible.
- **Don't put business logic in the loop.** Booking flows, CRM writes, brochure sends are tenant tools behind the tool interface, never `if` branches in the turn code.
- **Don't over-abstract the audio.** `audio.py` is perfect as a pure leaf. Resist wrapping it in an "AudioService."
- **Don't adopt the managed framework "to save time" if owning turn-taking is the point.** That trade is the whole reason this document exists. Own the Turn Engine; rent everything genuinely commoditized around it (media transport, model inference).

---

## The redesign in one line

Turn *"the process is Priya"* into *"the process is a turn engine; Priya is a row"* — keep your hand-owned turn-taking as the asset, put pluggable providers and per-tenant agents around it, and industrialize the latency pipeline you already got right.

---

*Sources for the reference-platform patterns cited above:*
- [OpenAI Realtime — Voice activity detection (server_vad / semantic_vad)](https://platform.openai.com/docs/guides/realtime-vad)
- [Developer notes on the Realtime API — OpenAI](https://developers.openai.com/blog/realtime-api)
- [Retell AI vs Bland AI — turn-taking / owned turn model](https://www.retellai.com/blog/retell-ai-vs-bland-ai-choose-the-right-voice-agent-for-your-business)
- [Retell vs Vapi — Bland AI](https://www.bland.ai/blog/retell-vs-vapi)
