# Runtime Implementation Roadmap

**Status:** presented for review — no implementation begins until approved
**Date:** 2026-07-03 · **Baseline:** commit `9c1f61c` · **Companion docs:** RUNTIME_AUDIT.md (current state), RUNTIME_REDESIGN.md (target state)

This document is the dependency-ordered plan for evolving the Northern Heights voice agent into the conversational AI runtime described in RUNTIME_REDESIGN.md. It amends the redesign where the code or repo state contradicts it, lists defects found during code review with file:line references, and fences off what we are deliberately *not* building yet.

---

## 0. Security incident — precedes all engineering

`.env` containing live **Sarvam, Vobiz (auth ID + token), Deepgram, and LLM** credentials is tracked in git (commits `bb7c75f` and `9c1f61c`) and pushed to a **public** GitHub repository (`Cleo-11/CustomPipeline` — confirmed publicly readable via unauthenticated API). `agentd.log`, also committed and public, exposes a live ngrok hostname. Public-repo secrets are harvested by scanners within minutes-to-hours; **all four credential sets must be treated as compromised now**, independent of any cleanup.

| Step | Owner | Action |
|---|---|---|
| 1 | **User** | Rotate all four credential sets in the provider dashboards (Sarvam, Vobiz, Deepgram, LLM key if paid). Nothing else matters until this is done. |
| 2 | **User** | Optionally make the repo private (reduces future exposure; does not un-leak). |
| 3 | Claude (M0) | Rewrite history to purge `.env`, `agent*.log`, `__pycache__/` (git-filter-repo), force-push, fix `.gitignore`, add `.env.example`. |

History rewrite is warranted *because* the repo is young (3 commits, no collaborators, no PRs) — the cost is one force-push. It is worthwhile only **after** rotation, never instead of it.

---

## 1. Verified state of the codebase

RUNTIME_AUDIT.md is accurate with two corrections: (1) the `agent*.log` files are **tracked** in git, not untracked; (2) the repo has a public remote, which the audit did not check. The runtime Python is 3.9.13 (per `__pycache__/*.cpython-39.pyc` and `python --version`), despite `audio.py` being written to survive 3.13's `audioop` removal.

### 1.1 Strengths to preserve (the asset list)

1. **Streaming pipeline with clause chunking** (`llm.py:30-63`) — TTS starts on the first completed clause while the LLM is still generating. Correct latency architecture; this is the product.
2. **Owned turn-taking** — two-signal barge-in (RMS + webrtcvad, `session.py:94-109`), silence endpointing (`session.py:125-133`), turn-sequence staleness guard (`session.py:147-148`). Exactly the IP the redesign says to industrialize.
3. **`audio.py` is a pure leaf** — stateless numpy codec/resampler. Keep as-is; do not wrap it in a service.
4. **Shallow dependency tree** — `session.py` is the only hub; the refactor surface is small and known.
5. **Side effects off the hot path** — booking/brochure fire-and-forget already follows the "tools never block the voice loop" rule.
6. **LLM is already provider-agnostic** via OpenAI-compatible base URL — the pattern to generalize to STT/TTS/transport.

### 1.2 Defects found in code review (beyond the audit)

| # | Defect | Where | Fixed in |
|---|---|---|---|
| D1 | **Overlapping playback race.** `_respond_to` starts a new speak task without cancelling the in-flight one; the `seq` guard is only checked *between* clauses, so a clause already inside `_speak` keeps streaming frames while the new turn also streams — interleaved audio. Trigger: STT final arrives during agent speech quietly enough not to barge in (audio always reaches STT), endpoint timer fires mid-reply. | `session.py:136-141`, guard at `146-148` | M4 |
| D2 | **Concurrent WebSocket writers.** `_interrupt()` runs on the STT-reader task and sends `clearAudio` while the speak task is concurrently sending `playAudio` — two tasks calling `websocket.send_text` unserialized. Starlette WS sends are not safe for concurrent writers. | `session.py:207-215` vs `170-205` | M3 |
| D3 | **Greeting interruptibility is inconsistent.** The RMS/VAD barge-in path is gated by `_greeting_active`, but the STT-partial path (`_on_partial`) can still cancel the greeting task. Redesign says the greeting is uninterruptible; the code disagrees with itself. | `session.py:91` vs `112-116` | M4 |
| D4 | **History records unspoken words.** On barge-in, the `finally` block appends the full generated text including clauses that were never played — the transcript drifts from what the caller heard, and the drift compounds turn over turn. | `session.py:164-168` | M4 |
| D5 | **Silent STT death.** If the Deepgram connect fails, the error is logged and the call proceeds deaf — greeting plays, nothing is ever transcribed, no recovery, no alert. | `sarvam_stt.py:43-53` | M8 (reconnect), M6 (alert event) |
| D6 | **"Speaking" state tracks frames sent, not frames played.** `_is_speaking` clears when the last frame is *queued to Vobiz*, and `playedStream` events also clear it mid-turn; barge-in and the 0.5s partial window re-arm while audio may still be audible from Vobiz's buffer. | `session.py:57-59, 204` | M4 |
| D7 | **Frame pacing drifts.** `sleep(0.02)` per frame ignores send time; over long utterances the schedule drifts rather than pacing against a monotonic deadline. | `session.py:189` | M3 |
| D8 | **Blocking file I/O on the event loop** in `save_booking`. | `booking.py:32` | M6 |
| D9 | **WAV parsing assumes a canonical 44-byte header** at fixed offsets; any extra RIFF chunk breaks it. | `sarvam_tts.py:53-59` | M2 |
| D10 | **No auth on any HTTP/WS route** — anyone with the hostname can drive the STT/LLM/TTS pipeline at the operator's expense. | `server.py` (all routes) | M0 (minimal), M8 (full) |

Known-broken by configuration (not code): `BROCHURE_URL` is a placeholder domain (`booking.py:21`); `bookings.jsonl` has never been produced by a real call.

### 1.3 Dead surface to remove (or finally wire up)

- **Dead code:** `config._get()` (never called — so "required" credentials silently default to empty strings), `audio.vobiz_to_pcm16_8k()`, `SarvamSTT.send_pcm16()`/`.flush()` stubs, `session._last_tts_duration` (written, never read — becomes a real metric in M6).
- **Dead config:** `LLM_NUM_CTX`, `STT_MODEL`/`STT_LANGUAGE`/`STT_ENCODING` (the real STT client hardcodes a Deepgram URL and reads `DEEPGRAM_API_KEY` outside `config.py`), `TTS_SRC_RATE`.
- **Promised-but-unwired config:** `THINKING_FILLER` — README documents it as a live feature; nothing plays it. Wire it in M4 (the THINKING state) rather than delete it.
- **Unused dependency:** `sarvamai` (pinned, never imported).
- **README corrections owed:** references a nonexistent `.env.example`; calls `audio.py` "unit-tested" (no tests exist); calls TTS "streaming WS" (it is one blocking REST POST per clause); calls STT "Sarvam Saaras" (it is Deepgram nova-2).

### 1.4 Missing subsystems vs. the CLAUDE.md vision

Exists informally today: Transport (Vobiz-shaped, inline), Session Runtime (`CallSession`), Turn Engine (implicit booleans), Provider Adapters (concrete classes, no interfaces), Tools (two regex markers). Absent entirely: Planner, Context Compiler, Memory Engine, Prompt Engine, Tool Registry/Executor, Workflow Engine, Agent Registry, Tenant Manager, Knowledge Layer, Event Bus, Scheduler, Analytics, Observability, Storage Layer, Control Plane, Plugin SDK.

**Most of these are correctly absent at ~940 lines.** The roadmap builds the ones a single-tenant production deployment needs (P1–P2) and fences the rest behind concrete demand (P3), per RUNTIME_REDESIGN §9's warning against rebuilding Pipecat badly.

### 1.5 Amendments to the redesign

1. **CONSTITUTION.md does not exist**, though CLAUDE.md names it the top source of truth. M0 writes it — a short codification of the rules already scattered across CLAUDE.md and RUNTIME_REDESIGN.md, so future decisions have a stable reference.
2. **Tests come before the refactors.** Redesign steps 1–2 claim "zero behavior change" — an unverifiable claim with zero tests. M1 adds characterization tests against the *current* code first; every subsequent milestone must keep them green.
3. **Transport seam before the Turn Engine** (redesign ordered it 4th). The engine consumes normalized transport events; extracting the Vobiz adapter first means the engine is born pure instead of refactored pure, and the single-writer send queue (fixes D2) naturally lives in that adapter.
4. **Python 3.9 → 3.12.** The code already writes 3.10+ idioms behind `from __future__ import annotations`, and `audio.py` exists specifically to survive post-`audioop` Python. Risk is `webrtcvad` (no cp312 wheels) → use the `webrtcvad-wheels` fork. Decision point for the user; 3.12 is the recommended default.
5. **Keep the marker protocol as a first-class fallback**, not a deprecation. The observed production reality (agentd.log: Gemini OpenAI-compat quirks, small Hinglish-capable models) is exactly the world where native tool-calling is unreliable. M7 adds native tool-calls where supported; the marker parser becomes one dispatch strategy behind the same ToolSpec interface.

---

## 2. Milestones

Complexity legend: **S** = hours · **M** = a focused day or two · **L** = several days.
Every milestone leaves the repo deployable and the characterization tests green.

### P0 — Foundational Architecture

#### M0 — Security remediation, repo hygiene, Constitution
- **Goal:** stop the credential leak; make the repo honest; give the project its missing constitution.
- **Why now:** live keys are public (see §0); every later milestone force-pushes more history on top if this waits.
- **Scope:** after user rotates keys — purge `.env`/`agent*.log`/`__pycache__` from history (git-filter-repo) and force-push; complete `.gitignore` (env, logs, pycache, `bookings.jsonl`); add `.env.example`; route required credentials through fail-fast `_get()` so a missing key kills startup instead of producing empty-string auth headers; add a minimal shared-secret token to the `wss://…/ws` URL issued by `/answer` and validate it on connect (closes D10 cheaply — Vobiz receives the token inside the URL we hand it); write **CONSTITUTION.md**; fix README's false claims (§1.3).
- **Files:** `.gitignore`, `.env.example` (new), `CONSTITUTION.md` (new), `config.py`, `server.py`, `README.md`; history rewrite touches all.
- **Dependencies:** user key rotation (blocking).
- **Risks:** force-push is destructive — safe here (solo repo, no forks/PRs); token-in-URL appears in Vobiz logs — acceptable interim, superseded in M8.
- **Complexity:** S–M.
- **Behavior:** startup now fails fast on missing creds; `/ws` rejects unauthenticated connects. Otherwise unchanged.

#### M1 — Test harness, tooling, CI, Python 3.12
- **Goal:** a safety net that makes "zero behavior change" a checkable claim instead of a hope.
- **Why now:** every P1 milestone is a refactor of working real-time code; refactoring untested async orchestration is how regressions ship.
- **Scope:** `pyproject.toml` (pinned Python 3.12, ruff, mypy, pytest, pytest-asyncio); swap `webrtcvad` → `webrtcvad-wheels`; delete dead code/config (§1.3) and tracked `.pyc`s; characterization tests: `audio.py` round-trip/resample/framing properties, `llm._breakpoint` + `extract_actions` (including marker-split-across-clauses and `4.30pm`-style breaks inside markers, documented as known limitations), and a **scripted-call test** driving `CallSession` end-to-end with monkeypatched fake STT/TTS/LLM/socket — assert the playAudio/clearAudio event sequence for a normal turn and a barge-in turn. That scripted test is the seed of the M4 replay harness. GitHub Actions: ruff + mypy + pytest.
- **Files:** `pyproject.toml` (new), `tests/` (new), `.github/workflows/ci.yml` (new), `requirements.txt` (retired into pyproject), small deletions in `config.py`/`audio.py`/`sarvam_stt.py`/`session.py`.
- **Dependencies:** M0 (clean history first — don't bake tests into commits that get rewritten).
- **Risks:** `webrtcvad-wheels` behavioral parity (same C library, low); 3.12 asyncio timing differences in tests (use fake clocks, not sleeps).
- **Complexity:** M.
- **Behavior:** none at runtime (structural + tooling).

### P1 — Core Runtime

#### M2 — Provider interfaces: STT / TTS / LLM behind Protocols
- **Goal:** the orchestrator stops knowing vendor names (redesign §4, migration step 1).
- **Why now:** every subsequent subsystem (turn engine, agents, tools, fallbacks) types against these interfaces; and it fixes the naming lie at its root.
- **Scope:** `runtime/types.py` (`AudioFormat`, `AudioFrame`, `STTEvent`, `LLMDelta`) and `runtime/interfaces.py` (`STT`, `TTS`, `LLM` Protocols with capability flags per redesign §4). Move concretes: `sarvam_stt.py` → `providers/stt/deepgram.py` (class renamed `DeepgramSTT`; reads config via `config.py`, killing the direct `os.getenv`), `sarvam_tts.py` → `providers/tts/sarvam.py` (proper RIFF chunk walk, fixing D9), `llm.py` client → `providers/llm/openai_compat.py` (**module-global `_client` becomes an injected instance** — prerequisite for per-agent LLM policy and for tests without network). Clause chunking (`stream_sentences` re-chunking) is runtime logic, not provider logic → extract to `runtime/clauses.py` operating over any `LLM` adapter's delta stream. `CallSession.__init__` takes injected provider instances; `server.py` wires a factory. Package layout lands here (one import churn, caught by tests).
- **Files:** new `runtime/`, `providers/` packages; `session.py`, `server.py`, `config.py` edits; test imports updated.
- **Dependencies:** M1 (tests prove behavior identical).
- **Risks:** biggest pure-refactor diff of the plan — mitigated by doing STT/TTS/LLM one at a time, tests green after each.
- **Complexity:** M–L.
- **Behavior:** none intended (structural); D9 fix is a robustness improvement invisible on the happy path.

#### M3 — Transport adapter: Vobiz becomes one plug
- **Goal:** the Vobiz protocol (answer-XML, WS event names, `playAudio`/`clearAudio`/`checkpoint` shapes, µ-law framing, pacing) moves out of `server.py`/`session.py` into `transports/vobiz.py` implementing the `Transport` Protocol (redesign §4).
- **Why now:** before the Turn Engine is extracted, so the engine consumes normalized `TransportEvent`s from day one (§1.5 amendment 3).
- **Scope:** `Transport` Protocol in `runtime/interfaces.py`; `transports/vobiz.py` owning: event normalization (`start`/`media`/`playedStream`/`clearedAudio`/`stop` → typed events), **a single-writer outbound queue** (fixes D2), **deadline-based frame pacing** against the monotonic clock (fixes D7), checkpoint correlation. `server.py` becomes a thin FastAPI shell that hands the socket to the adapter. Also `transports/local.py` — a scripted in-memory transport for tests and a future console/dev mode (replaces monkeypatching from M1's scripted test).
- **Files:** `transports/` (new), `server.py`, `session.py`, tests.
- **Dependencies:** M2 (types/interfaces exist).
- **Risks:** pacing/queue changes touch real-time feel — validate with a live test call, not just unit tests.
- **Complexity:** M.
- **Behavior:** D2/D7 fixes (strictly better); wire format to Vobiz unchanged.

#### M4 — Turn Engine: the explicit state machine
- **Goal:** extract turn-taking into a pure, I/O-free, replay-testable state machine — LISTENING / USER_SPEAKING / THINKING / AGENT_SPEAKING / INTERRUPTED (redesign §5). This is the crown-jewel milestone.
- **Why now:** everything conversational after this (endpointing strategies, fillers, semantic VAD, A/B-ing turn feel) plugs into this component; and four known defects live in the implicit version.
- **Scope:** `runtime/turn_engine.py`: consumes normalized events (transport media/VAD, STT partial/final, playback progress), emits intents (`commit_user_turn`, `speak(clause)`, `cancel_output`, `play_filler`); named guard predicates (`should_barge_in`, `should_endpoint`, `is_stale`); pluggable endpointer (`FixedSilenceEndpointer` = today's 550ms; `ProviderEndpointer` trusting Deepgram `UtteranceEnd`/`vad_events`, capability-gated — the events are already arriving and currently ignored at `sarvam_stt.py:75-76`). `CallSession` shrinks to wiring: engine intents → provider calls. Replay harness: feed a recorded event log, assert the state/intent trace. **Intended behavior fixes, each with a test:** new turn cancels in-flight playback (D1); one explicit greeting policy — uninterruptible, both paths (D3); history truncated to clauses actually played (D4); `_is_speaking` derived from playback progress events, not send-completion (D6); `THINKING_FILLER` finally honored in the THINKING state (closes the README promise).
- **Files:** `runtime/turn_engine.py`, `runtime/endpointing.py`, `session.py` (major shrink), tests (the bulk of the diff should be tests).
- **Dependencies:** M3.
- **Risks:** highest-value, highest-subtlety milestone — turn feel can regress in ways only a live call reveals; mitigate with the replay harness plus scripted live-call checklist (greeting, normal turn, barge-in, double-barge-in, long reply).
- **Complexity:** L.
- **Behavior:** yes — deliberate fixes D1/D3/D4/D6 + filler; each is a named, tested change.

#### M5 — Agents as data: `AgentConfig` + one-row registry
- **Goal:** `config.py` stops being Priya (redesign §6, migration step 3). The process becomes an engine; the agent becomes a record.
- **Why now:** last structural prerequisite for tools-per-agent (M7) and tenancy (P3); after M2–M4, the persona constants are the only hardcoding left.
- **Scope:** `runtime/agent.py` (`AgentConfig`: system_prompt, greeting, knowledge, voice/STT/LLM policies, turn_policy, tools — per redesign §6); `agents/priya.json` seed; `runtime/agent_registry.py` (file-backed registry, `resolve(called_number | agent_id) → AgentConfig`, defaulting to Priya); `config.py` reduced to engine tunables + credentials; session receives its `AgentConfig` at `call.start`.
- **Files:** `runtime/agent.py`, `runtime/agent_registry.py`, `agents/` (new), `config.py` (shrinks), `session.py`, `make_call.py` (optional `--agent`).
- **Dependencies:** M2 (provider factory consumes policies), M4 (turn_policy consumed by engine).
- **Risks:** low — mechanical, behavior identical with the Priya row.
- **Complexity:** M.
- **Behavior:** none (structural).

### P2 — Platform Capabilities

#### M6 — Event bus + observability
- **Goal:** everything important emits a typed event; logs/metrics/sinks become subscribers instead of inline concerns (CLAUDE.md event rules).
- **Why now:** before tools (M7) so tool audit events land on an existing bus; before reliability (M8) so failures are visible. Cheapest of the P2 trio, pure-additive.
- **Scope:** `runtime/events.py` — in-process async pub/sub (no broker yet); events: CallStarted/Ended, SpeechStarted/Ended, TurnCompleted, ThinkingStarted/Finished, ToolCalled/Succeeded/Failed, AgentInterrupted, SessionClosed; structured JSON logging with `call_id`/`turn_seq` correlation; **per-turn latency metrics measuring the README's budget for real** (endpoint→first-token, first-token→first-audio-byte, barge-in reaction) — `_last_tts_duration` dead write becomes a real metric; Prometheus `/metrics`; `/health` deep mode probing provider reachability; transcript/booking JSONL writers become subscribers (via `asyncio.to_thread`, fixing D8's pattern).
- **Files:** `runtime/events.py`, `runtime/metrics.py`, emission points in session/engine/transport/providers, `server.py`.
- **Dependencies:** M4 (turn boundaries exist to instrument).
- **Risks:** hot-path overhead — emission must be non-blocking (queue + drain task), never awaited inline on the audio path.
- **Complexity:** M.
- **Behavior:** none conversationally; new endpoints/log shape.

#### M7 — Tool registry + executor: business logic leaves the runtime
- **Goal:** tools become registered, schema'd, per-agent capabilities (CLAUDE.md tool rules); booking/brochure become Priya's tools, not runtime code.
- **Why now:** the last piece of business logic inside runtime modules; requires AgentConfig (M5) and audits to the bus (M6).
- **Scope:** `runtime/tools.py` (`ToolSpec`: name/description/schema/timeout/retry/owner; registry; executor — `create_task` + timeout + ToolCalled/Succeeded/Failed events, still never blocking the voice loop); two dispatch strategies behind one interface: native LLM tool-calls (in the OpenAI-compat adapter) and the marker parser (demoted to fallback strategy, per §1.5 amendment 5); `agents/priya/tools.py` — booking + brochure handlers move out of the runtime; `BROCHURE_URL` becomes agent config (**user still owes a real PDF URL — it ships broken today**); optional: tool results can be fed back to the LLM (marker protocol stays fire-and-forget).
- **Files:** `runtime/tools.py`, `providers/llm/openai_compat.py`, `agents/priya/`, `booking.py` (dissolves), `llm.py` marker code relocates, tests.
- **Dependencies:** M5, M6.
- **Risks:** small Hinglish-capable models emit malformed native tool-calls — the fallback strategy is the mitigation, and per-agent policy picks the strategy.
- **Complexity:** M.
- **Behavior:** same conversational contract; booking writes gain audit events.

#### M8 — Reliability & graceful degradation
- **Goal:** one vendor blip degrades the call instead of silencing or killing it (redesign §7).
- **Why now:** needs interfaces (M2) to wrap and events (M6) to observe; last step before this is honestly "production-grade" for a single tenant.
- **Scope:** per-provider timeout/retry policy (bounded, jittered; at the adapter edge); scripted fallback line when LLM/TTS fail (no more dead air — the agent apologizes and continues); STT reconnect-with-backoff mid-call (fixes D5) + a "deaf call" alarm event if reconnect fails; circuit-breaker-lite (skip a dead provider for N seconds, emit event); conversation-history cap (turn-count/char budget with oldest-turn eviction — the proto Context Compiler, closes the unbounded-growth hole); full webhook auth — Vobiz signature verification if the platform supports it, else HMAC shared-secret on all webhook routes (supersedes M0's minimal token).
- **Files:** `runtime/resilience.py`, provider adapters, `transports/vobiz.py`, `server.py`, `session.py`, tests (failure-injection via fake providers).
- **Dependencies:** M2, M6 (M7 independent).
- **Risks:** retry storms adding latency on the hot path — budgets must be strict (e.g., one TTS retry ≤300ms, else fallback line).
- **Complexity:** M–L.
- **Behavior:** yes — failure paths change from silence/death to spoken degradation; happy path unchanged.

### P3 — Future Expansion (build on demand, not on speculation)

- **M9 — Second transport** (Twilio Media Streams or browser WebRTC) when a real need appears; the seam is proven by `transports/local.py` meanwhile. *Complexity M per transport.*
- **M10 — Control plane / tenancy:** tenant records, per-tenant provider keys/quotas/concurrency caps, session registry (in-memory + shared index), graceful drain on deploy. First milestone that needs shared infrastructure (Redis-class). *Complexity L.*
- **M11 — Data plane / storage:** SQLite→Postgres event/transcript/recording store with retention; `bookings.jsonl` becomes one subscriber among several. *Complexity M–L.*
- **M12 — Conversation intelligence:** Context Compiler proper (templated prompts, summarization replacing M8's crude eviction), Memory Engine (cross-call caller memory), Knowledge Layer (retrieval replacing the prompt-baked KB), `SemanticEndpointer` (the "did they actually finish?" classifier — the biggest conversational-feel lever after M4). *Complexity L, separable.*

**Explicitly not building until a concrete requirement exists:** Workflow Engine, Planner, Scheduler, Plugin SDK, Analytics dashboards (the M6 bus will feed them when they come). At ~940 lines of runtime, speculative subsystems are how this project would fail — RUNTIME_REDESIGN §9 and CLAUDE.md both say so.

---

## 3. Dependency graph & recommended order

```
M0 ─► M1 ─► M2 ─► M3 ─► M4 ─► M5 ─► M6 ─► M7 ─► M8   ─►  P3 (on demand)
(user key rotation blocks M0)         └──── M6 may start right after M4;
                                            M7 needs M5+M6; M8 needs M2+M6
```

Strictly serial through M4 — each milestone is the substrate of the next. After M4, M5/M6 could interleave; the listed order minimizes churn. Estimated behavioral-change milestones: **M0 (auth/fail-fast), M4 (four deliberate bug fixes), M8 (failure paths)** — everything else must leave a live call indistinguishable, and the tests exist to prove it.

## 4. Open decisions (defaults proposed)

1. **Python version:** recommend 3.12 (M1). Requires `webrtcvad-wheels`. Alternative: stay on 3.9 (EOL, blocks modern typing/asyncio) — not recommended.
2. **History rewrite:** recommend purge + force-push (M0) given solo public repo. Alternative: rotate-only and leave dead keys in history — acceptable but leaves noise for secret scanners forever.
3. **Repo visibility:** recommend private until M8 is done. User's call.
