# Northern Heights — Hinglish Real-Estate Voice Agent

A low-latency, production-oriented outbound/inbound calling agent for **N Rose
Developers**. Stack:

| Layer | Choice | Why |
|------|--------|-----|
| Telephony | **Vobiz** (WebSocket audio streaming) | India DIDs, bulk outbound, bidirectional mu-law stream |
| STT | **Deepgram nova-2** (streaming WS, `language=hi`) | streaming interims, VAD + endpointing events |
| LLM | **Qwen2:7B via Ollama** (OpenAI-compatible) | local, zero per-token cost; any OpenAI-compatible URL works |
| TTS | **Sarvam Bulbul v3** (REST, per clause) | natural Hindi/Hinglish voice |

---

## How it fits together

```
PSTN ──► Vobiz ──(<Stream> bidirectional mu-law 8k)──► server.py /ws
                                                          │
                                                   CallSession (session.py)
   caller mu-law 20ms frames ─► ulaw→PCM16 ─┬─► local energy VAD ─► barge-in
                                            └─► Deepgram STT (mu-law 8k) ─► transcripts
                                                       │ endpoint (550ms silence)
                                                       ▼
                                        Qwen2:7B (Ollama, token stream)
                                                       │ clause-by-clause
                                                       ▼
                                        Sarvam Bulbul TTS (REST, per clause)
                                                       │ PCM16 24k → 8k → mu-law
                                                       ▼
                                        playAudio (160B / 20ms) ─► Vobiz ─► caller
```

Everything streams. The agent starts speaking a clause while the LLM is still
writing the next one, and TTS audio starts playing while it's still being
synthesised. That pipelining is what gets you a human-feeling response time.

---

## Latency budget (target: caller-stops-talking → agent-starts ≈ 700–900 ms)

| Stage | Typical | Lever (in `config.py`) |
|-------|---------|------------------------|
| Endpointing (silence after speech) | 400–550 ms | `ENDPOINT_SILENCE_MS`; or set `ENDPOINTER=provider` to trust Deepgram's UtteranceEnd events (silence window stays as fallback) |
| STT final transcript | ~100–200 ms | streaming, runs continuously |
| LLM time-to-first-token | 150–400 ms | warm Ollama (`keep_alive`), short prompt, GPU |
| First clause → TTS first byte | 150–300 ms | clause chunking |
| **Perceived total** | **~0.7–1.0 s** | |

Two cheap tricks wired in:
- **Clause chunking** (`runtime/clauses.py`): the first clause is flushed to
  TTS early, so audio begins long before the full reply.
- **Thinking filler** (`THINKING_FILLER`): the instant the caller's turn
  commits, a tiny "हम्म" plays while the LLM's first tokens are still in
  flight. Set to `""` to disable.

---

## The Hinglish naturalness problem (read this)

Your original prompt had Priya speak **romanised** Hinglish (`"Aap kaunse..."`).
TTS engines frequently mispronounce romanised Hindi — they read it as English.
So Priya's system prompt (in `agents/priya.json`) instructs the model to write
**Hindi words in Devanagari** and keep only genuinely-English tokens (brand
names, "BHK", "sq ft", numbers) in Latin. Bulbul handles that code-mixed script
far more naturally. The persona, 1–2 sentence limit, flow, objections and KB are
all preserved from your script.

One honest caveat: **Qwen2:7B is the weakest link for "very, very human"
Hinglish.** It works, but for noticeably better code-mix try
`qwen2.5:7b-instruct`, or — since the LLM layer is just an OpenAI-compatible
base URL — point `LLM_BASE_URL` at Sarvam's own `sarvam-m`/`sarvam-30b`
(built for this) and compare. No code change, just `.env`.

---

## Setup

Python **3.12+** is required (pinned in `pyproject.toml`).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
# Windows:  py -3.12 -m venv .venv ; .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env          # fill in keys
python -c "import secrets; print(secrets.token_urlsafe(32))"   # → WS_AUTH_TOKEN

# LLM: keep the model warm so it never cold-starts mid-call
ollama pull qwen2:7b
OLLAMA_KEEP_ALIVE=-1 ollama serve
```

### Checks (same as CI)
```bash
ruff check . && mypy . && pytest
```

### Local testing (ngrok)
```bash
ngrok http 8000               # copy the host, e.g. abc123.ngrok-free.app
# set PUBLIC_HOST=abc123.ngrok-free.app in .env
uvicorn server:app --host 0.0.0.0 --port 8000
python make_call.py --to +9198XXXXXXXX
```
For **inbound**, point your Vobiz Application's Answer URL at
`https://<PUBLIC_HOST>/answer` (POST).

### Production
- Run behind a real TLS domain; set `PUBLIC_HOST` to it (drop ngrok).
- `/ws` only accepts connections presenting `WS_AUTH_TOKEN` — it's embedded in
  the URL `/answer` hands to Vobiz, so callers need no extra setup.
- `uvicorn server:app --port 8000 --workers 1`, then scale **horizontally**
  (more single-worker processes behind a load balancer). Each live call pins
  one asyncio loop and holds 1 Vobiz WS + 1 Sarvam STT WS + short-lived TTS WS.

---

## Scaling the LLM (important for "production scale")

Ollama is great for dev and a handful of concurrent calls. For many simultaneous
calls it serialises badly. For scale, keep the exact same code and swap the
endpoint to **vLLM** (continuous batching, OpenAI-compatible):

```bash
vllm serve Qwen/Qwen2-7B-Instruct --port 8001 --max-model-len 4096
# .env:  LLM_BASE_URL=http://localhost:8001/v1   LLM_MODEL=Qwen/Qwen2-7B-Instruct
```
A single 7B on one modern GPU (q4/fp8) comfortably batches dozens of these short
chat turns. Budget ~5–6 GB VRAM at q4.

---

## Files

| File | Role |
|------|------|
| `server.py` | FastAPI: Vobiz webhooks + `/ws` socket + composition root wiring providers/transport |
| `session.py` | Per-call wiring: engine events in, engine intents out, LLM→TTS reply pipeline |
| `runtime/turn_engine.py` | The turn-taking state machine — pure, I/O-free, replay-testable |
| `runtime/endpointing.py` | Pluggable end-of-turn strategies (fixed silence / provider-trusting) |
| `runtime/agent.py` | `AgentConfig`: the agent as a data record (persona + voice/STT/LLM/turn policy) |
| `runtime/agent_registry.py` | Resolves a call to its `AgentConfig`, defaulting to Priya |
| `runtime/events.py` | Typed conversation events + the non-blocking in-process event bus |
| `runtime/metrics.py` | Latency/counter registry, Prometheus text exposition, turn-metrics subscriber |
| `runtime/sinks.py` | Bus subscribers: structured event log, transcript JSONL, JSON log formatter |
| `runtime/tools.py` | Tool registry + executor (timeout/retry/audit events) + marker/native dispatch strategies |
| `agents/priya_tools.py` | Priya's business tools: site-visit booking, WhatsApp brochure (was `booking.py`) |
| `agents/priya.json` | Priya/Northern Heights — the reference agent, as data |
| `runtime/` | Provider-agnostic core: types, capability-typed interfaces, clause chunking, marker parsing |
| `transports/vobiz.py` | Vobiz WS adapter: event normalization, single-writer sends, deadline frame pacing |
| `transports/local.py` | In-memory scripted transport for tests/replay |
| `providers/stt/deepgram.py` | Deepgram nova-2 streaming STT adapter |
| `providers/tts/sarvam.py` | Sarvam Bulbul v3 REST TTS adapter → mu-law frames |
| `providers/llm/openai_compat.py` | Adapter for any OpenAI-compatible LLM endpoint |
| `audio.py` | G.711 mu-law codec + resampling (unit-tested) |
| `config.py` | Deployment engine defaults + credentials (no longer the agent) |
| `make_call.py` | Outbound dialer |
| `tests/` | Characterization tests: audio codec, clause chunking, scripted call flows, route auth |

---

## Observability (M6)

Everything important on a call is announced as a typed event on an
in-process bus (`runtime/events.py`); logs, metrics and transcripts are
subscribers, not inline concerns. Emission is a synchronous enqueue —
nothing on the audio path ever waits for an observer.

- **`GET /metrics`** — Prometheus text format. The README's latency budget
  is measured for real: `turn_thinking_seconds` (turn commit → first
  speakable clause), `turn_first_audio_seconds` (commit → first audio
  frame, filler included — perceived response time),
  `bargein_reaction_seconds` (cancel intent → audio dead + buffer cleared),
  plus call/turn/interruption/tool counters.
- **`GET /health?deep=true`** — probes provider reachability (Deepgram
  auth, LLM `/models`, Sarvam endpoint) concurrently, 5s time-box each.
- **`transcripts.jsonl`** — per-call lifecycle + per-turn records (what was
  actually spoken, with latencies), appended by a subscriber off the loop.
- **`LOG_FORMAT=json`** — one JSON object per log line for prod pipelines.
- Booking/brochure run through the tool executor: `ToolCalled/ToolSucceeded/
  ToolFailed` audit events, per-tool timeout + retry, file I/O off the
  event loop.

---

## Tools (M7)

Tools are registered capabilities (`runtime/tools.py`); the runtime
executes them without knowing the business logic. Priya's tools —
`book_site_visit` and `send_brochure` — live in `agents/priya_tools.py`
and are listed by name in `agents/priya.json`, alongside her `tool_config`
(bookings path, brochure URL — **still a placeholder URL; set a real PDF
link before relying on brochure delivery**).

Two dispatch strategies, chosen per agent via `LLM_TOOL_DISPATCH` (or the
agent's `llm.tool_dispatch`):

- **`marker`** (default): the model appends `[[BOOK day=… time=… name=…]]`
  / `[[BROCHURE]]` to its reply; the runtime strips and dispatches them.
  Reliable with small Hinglish-capable models.
- **`native`**: OpenAI-protocol tool calls. The adapter assembles streamed
  fragments and drops malformed calls — worth enabling on models that do
  tool-calling well; also removes the split-marker leak limitation.

Tool results are not fed back to the model (fire-and-forget), matching the
original marker contract.

---

## Tuning guide

| Symptom | Fix |
|--------|-----|
| Agent talks over the caller | lower `BARGEIN_MIN_FRAMES` / `BARGEIN_RMS_THRESHOLD` |
| Agent cuts caller off too early | raise `ENDPOINT_SILENCE_MS` (e.g. 700) |
| Long pause before agent replies | warm Ollama, shorten KB, keep `THINKING_FILLER` on, move to vLLM/GPU |
| Robotic / metallic voice | confirm 24k→8k path; don't double-resample |
| Empty transcripts | check `DEEPGRAM_API_KEY` and the Deepgram console logs |
| Mispronounced Hindi | ensure model outputs Devanagari (see prompt) |

---

## Project direction

This codebase is evolving from a single-agent voice bot into a
transport-agnostic conversational AI runtime. The architectural philosophy
lives in **CONSTITUTION.md**; the milestone plan lives in **ROADMAP.md**. The
orchestration layer is deliberately owned here — no managed conversational
runtime (Pipecat, LiveKit Agents, Vapi, …) sits underneath it.
