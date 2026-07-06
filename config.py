"""
config.py — Deployment engine defaults + credentials.

Since M5 this is *not* the agent. Priya's persona (system prompt, greeting,
knowledge, voice) lives in agents/priya.json and is resolved per call by
runtime.agent_registry. What remains here are the credentials and the
engine defaults an agent inherits for any policy its file doesn't pin —
the tunables of the deployment, not of any one agent.
"""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


# ---------------------------------------------------------------------------
# Credentials / endpoints
# ---------------------------------------------------------------------------
# Required — startup fails immediately if missing, instead of limping along
# and sending empty-string auth headers to providers.
SARVAM_API_KEY = _get("SARVAM_API_KEY")
DEEPGRAM_API_KEY = _get("DEEPGRAM_API_KEY")
VOBIZ_AUTH_ID = _get("VOBIZ_AUTH_ID")
VOBIZ_AUTH_TOKEN = _get("VOBIZ_AUTH_TOKEN")
VOBIZ_API_BASE = os.getenv("VOBIZ_API_BASE", "https://api.vobiz.ai/api/v1")

# Shared secret embedded in the wss:// URL that /answer hands to Vobiz and
# validated on every /ws connect. Any long random string:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
WS_AUTH_TOKEN = _get("WS_AUTH_TOKEN")

# Public hostname Vobiz will reach (no scheme). e.g. agent.mycompany.com
# In local dev this is your ngrok host, e.g. abc123.ngrok-free.app
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "localhost:8000")

# LLM (Ollama, OpenAI-compatible endpoint). Swap base_url to point at vLLM,
# Sarvam, or any OpenAI-compatible server without changing pipeline code.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")          # ignored by Ollama
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2:7b")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.6"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "160"))
# How agents dispatch tools by default: "marker" ([[TOKEN k=v]] in the
# reply text — reliable with small models) or "native" (OpenAI-protocol
# tool calls). An agent can pin its own in its llm section.
LLM_TOOL_DISPATCH = os.getenv("LLM_TOOL_DISPATCH", "marker")

# Default STT model/language for agents that don't pin their own. The rest
# of the Deepgram connection string is fixed in providers/stt/deepgram.py.
STT_MODEL = os.getenv("STT_MODEL", "nova-2")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "hi")

# Default TTS voice for agents that don't pin their own (Priya pins hers in
# agents/priya.json).
TTS_MODEL = os.getenv("TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "shubh")           # v3 default voice
TTS_LANGUAGE = os.getenv("TTS_LANGUAGE", "hi-IN")
TTS_PACE = float(os.getenv("TTS_PACE", "1.05"))           # only pace works on v3

# ---------------------------------------------------------------------------
# Turn-taking / latency tuning
# ---------------------------------------------------------------------------
# Silence after last final transcript before we treat the turn as finished.
ENDPOINT_SILENCE_MS = int(os.getenv("ENDPOINT_SILENCE_MS", "550"))
# End-of-turn strategy: "fixed" = the silence window above; "provider" =
# trust the STT's endpoint events (Deepgram UtteranceEnd) with the silence
# window kept armed as a safety fallback. Capability-gated: falls back to
# fixed when the STT doesn't emit endpoints.
ENDPOINTER = os.getenv("ENDPOINTER", "fixed")
# Barge-in: energy threshold + consecutive 20 ms frames of speech that must
# arrive while the agent is talking before we cut its audio.
BARGEIN_RMS_THRESHOLD = float(os.getenv("BARGEIN_RMS_THRESHOLD", "650"))
BARGEIN_MIN_FRAMES = int(os.getenv("BARGEIN_MIN_FRAMES", "25"))  # ~500 ms
VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "2"))   # 0–3; 2 = balanced
# A mid-speech partial only interrupts the agent after it has held the floor
# this long — below it, the partial is likely the agent's own audio echoing.
PARTIAL_INTERRUPT_AFTER_S = float(os.getenv("PARTIAL_INTERRUPT_AFTER_S", "0.5"))
# Tiny acknowledgement ("हम्म…") spoken the instant the caller's turn commits,
# masking LLM time-to-first-token (wired via the Turn Engine's THINKING
# state since M4). Set to "" to disable.
THINKING_FILLER = os.getenv("THINKING_FILLER", "हम्म")

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
# "text" (dev default) or "json" — one JSON object per log line for prod
# log pipelines. Structured conversation events are always logged as JSON
# payloads by the event-log subscriber regardless of this setting.
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")
# Where the transcript subscriber appends per-call JSONL records.
TRANSCRIPTS_PATH = os.getenv("TRANSCRIPTS_PATH", "transcripts.jsonl")
