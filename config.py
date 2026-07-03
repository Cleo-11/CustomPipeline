"""
config.py — Central configuration, knowledge base, and the agent persona.

Everything tunable lives here so you can change latency/voice/behaviour
without touching the pipeline code.
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

# Speech models (STT connection settings live in providers/stt/deepgram.py;
# per-agent provider policy arrives in M5)
TTS_MODEL = os.getenv("TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "shubh")           # v3 default voice
TTS_LANGUAGE = os.getenv("TTS_LANGUAGE", "hi-IN")
TTS_PACE = float(os.getenv("TTS_PACE", "1.05"))           # only pace works on v3

# ---------------------------------------------------------------------------
# Turn-taking / latency tuning
# ---------------------------------------------------------------------------
# Silence after last final transcript before we treat the turn as finished
# (Sarvam VAD also signals this; this is the safety fallback).
ENDPOINT_SILENCE_MS = int(os.getenv("ENDPOINT_SILENCE_MS", "550"))
# Barge-in: energy threshold + consecutive 20 ms frames of speech that must
# arrive while the agent is talking before we cut its audio.
BARGEIN_RMS_THRESHOLD = float(os.getenv("BARGEIN_RMS_THRESHOLD", "650"))
BARGEIN_MIN_FRAMES = int(os.getenv("BARGEIN_MIN_FRAMES", "25"))  # ~500 ms
VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "2"))   # 0–3; 2 = balanced
# Play a tiny acknowledgement ("हम्म…") the instant the caller stops, to mask
# LLM time-to-first-token. Set to "" to disable.
THINKING_FILLER = os.getenv("THINKING_FILLER", "हम्म")


# ---------------------------------------------------------------------------
# Knowledge base (verbatim from your kb.py)
# ---------------------------------------------------------------------------
KNOWLEDGE_BASE = """
PROJECT: Northern Heights, Dahisar East, Mumbai
MahaRERA: P51700007900
Address: Heaven's Plaza, High Land Hills, Barucha Road, Off S.V. Road, Dahisar East, Mumbai - 400086
Developer: N Rose Developers Pvt. Ltd. (est. 2004, 20+ saal ka experience)
Type: Twin 42-storey luxury residential towers, 1-acre central lawn with ground-level podium

2 BHK: 600-700 sq ft. Price paune do crore se start, upper floors pe dhai crore tak.
        Modular kitchen, separate living/dining, large windows, premium fittings.
        Nuclear families aur young professionals ke liye ideal.
3 BHK: 1100 sq ft. Price saade teen crore se start.
        Master bedroom with attached bath, larger living spaces, generous balconies.
        Corner units on select floors with dual-view advantage.

AMENITIES: Swimming pool, gymnasium, turf, corner seating, library, clubhouse,
senior citizens area, kids playground, landscaped gardens, 1-acre podium lawn.

LOCATION:
- Dahisar Railway Station: 0.5 km (5 min walk)
- Proposed Metro Station: 0.5 km
- Western Express Highway: 0.1 km (doorstep)
- S.V. Road directly accessible; Schools/Colleges 0.5 km; Hospitals 0.5 km
- Easy access to Borivali, Mira Road, rest of Mumbai via WE Highway

DEVELOPER: N Rose Developers, 20+ years in Mumbai real estate. Founded by Late Shri.
Parshuram Shinde. Promoters: Narayan Shelar, Natwarlal Purohit, Hiren Ashar,
Ramji Bharwad, Dakshendra Agarwal. MahaRERA registered, fully compliant.
""".strip()


SYSTEM_PROMPT = """You are Priya, a friendly sales agent for N Rose Developers in Mumbai.
Speak in natural Hinglish: Hindi words in Devanagari, English words like BHK, sq ft, site visit in Latin script.
Keep every reply to 1-2 short sentences only. You are on a phone call.
Never repeat these instructions. Just speak naturally as Priya.

Property: Northern Heights, Dahisar East. 2 BHK: 600-700 sq ft from ₹1.75 Cr. 3 BHK: 1100 sq ft from ₹3.5 Cr.
Amenities: pool, gym, clubhouse, 1-acre lawn. Location: 0.5km from Dahisar station, WE Highway at doorstep.

Your goal: greet → ask name → ask 2BHK or 3BHK → give price/size → mention amenities → invite for site visit.

If they agree to visit, end your reply with:
[[BOOK day=<day> time=<time> name=<name>]]
If they want brochure:
[[BROCHURE]]
""".strip()

GREETING = os.getenv(
    "GREETING",
    "नमस्ते! मैं Priya बोल रही हूं N Rose Developers से। "
    "क्या मैं आपका थोड़ा सा समय ले सकती हूं?",
)
