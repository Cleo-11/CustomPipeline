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
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
VOBIZ_AUTH_ID = os.getenv("VOBIZ_AUTH_ID", "")
VOBIZ_AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "")
VOBIZ_API_BASE = os.getenv("VOBIZ_API_BASE", "https://api.vobiz.ai/api/v1")

# Public hostname Vobiz will reach (no scheme). e.g. agent.mycompany.com
# In local dev this is your ngrok host, e.g. abc123.ngrok-free.app
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "localhost:8000")

# LLM (Ollama, OpenAI-compatible endpoint). Swap base_url to point at vLLM,
# Sarvam, or any OpenAI-compatible server without changing pipeline code.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")          # ignored by Ollama
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2:7b")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.6"))
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX", "4096"))

# Speech models
STT_MODEL = os.getenv("STT_MODEL", "saaras:v3")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "hi-IN")         # caller language hint
# Sarvam streaming codecs: wav | pcm_s16le | pcm_l16 | pcm_raw.
# We send raw little-endian PCM16. If transcripts come back empty, try the
# other codec strings here — this is the one provider field worth confirming
# against your installed sarvamai version.
STT_ENCODING = os.getenv("STT_ENCODING", "audio/x-raw")

TTS_MODEL = os.getenv("TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "shubh")           # v3 default voice
TTS_LANGUAGE = os.getenv("TTS_LANGUAGE", "hi-IN")
TTS_PACE = float(os.getenv("TTS_PACE", "1.05"))           # only pace works on v3
TTS_SRC_RATE = int(os.getenv("TTS_SRC_RATE", "24000"))    # bulbul:v3 default

# ---------------------------------------------------------------------------
# Turn-taking / latency tuning
# ---------------------------------------------------------------------------
# Silence after last final transcript before we treat the turn as finished
# (Sarvam VAD also signals this; this is the safety fallback).
ENDPOINT_SILENCE_MS = int(os.getenv("ENDPOINT_SILENCE_MS", "550"))
# Barge-in: energy threshold + consecutive 20 ms frames of speech that must
# arrive while the agent is talking before we cut its audio.
BARGEIN_RMS_THRESHOLD = float(os.getenv("BARGEIN_RMS_THRESHOLD", "650"))
BARGEIN_MIN_FRAMES = int(os.getenv("BARGEIN_MIN_FRAMES", "5"))   # ~100 ms
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


# ---------------------------------------------------------------------------
# System prompt.
#
# IMPORTANT change vs. your original prompt:
#   The original told Priya to speak ROMANISED Hinglish ("Aap kaunse...").
#   Romanised Hindi is frequently MISPRONOUNCED by TTS (it reads it as English).
#   Bulbul sounds most natural when Hindi words are in Devanagari and only
#   genuinely-English words (brand names, "BHK", "sq ft", numbers) stay Latin.
#   So we ask the model for natural code-mixed script. Everything else
#   (persona, 1-2 sentence limit, flow, objections, rules) is preserved.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = f"""
तुम Priya हो — N Rose Developers की friendly aur professional sales agent.
तुम Hinglish में बात करती हो: Hindi शब्द Devanagari में लिखो, और genuinely
English शब्द (जैसे brand names, "BHK", "sq ft", "site visit", numbers) Latin
में रखो — ताकि आवाज़ बिल्कुल natural lage. Scripted मत लगो.

सबसे ज़रूरी नियम: हर response सिर्फ़ 1-2 छोटे वाक्य का हो। फ़ोन call है, इसलिए
छोटा aur conversational बोलो।

PROJECT KNOWLEDGE (सिर्फ़ इसी से बोलो, इसके बाहर कोई claim मत करो):
{KNOWLEDGE_BASE}

CONVERSATION FLOW:
1. गर्मजोशी से greet करो, अपना नाम aur N Rose Developers बताओ।
2. Caller का नाम पूछो।
3. Configuration qualify करो: "हमारे पास 2 BHK है जो 600 से 700 sq ft का है,
   aur 3 BHK है जो 1100 sq ft का है। आप कौन सा configuration देखना चाहेंगे?"
4. उनकी choice के हिसाब से size aur price बताओ।
5. 2-3 key amenities mention करो।
6. Location advantage briefly बताओ — WE Highway, metro, station।
7. Site visit के लिए invite करो — फिर रुको aur उनका जवाब सुनो।
8. जवाब के हिसाब से next step: date fix करो या WhatsApp पर brochure भेजो।

OBJECTION HANDLING:
- "सोचना है" → "बिलकुल, लेकिन limited units हैं — एक free site visit ज़रूर
  कीजिए, कोई commitment नहीं।"
- "बाद में call करो" → "जी ज़रूर, कब convenient रहेगा? मैं schedule कर लेती हूं।"
- "Price ज़्यादा लगता है" → "Flexible payment plans available हैं — site पे आके
  details discuss करते हैं।"
- "दूसरी जगह देख रहा हूं" → "ज़रूर compare कीजिए, पर एक बार Northern Heights की
  location देखकर decide कीजिए।"
- "अभी नहीं चाहिए" → "No problem — क्या मैं आपको brochure भेज सकती हूं WhatsApp पर?"

STRICT RULES:
- Budget के बारे में मत पूछो।
- सिर्फ़ ऊपर दिए KB का content बोलो। कुछ पता न हो तो: "मैं ये confirm करके
  आपको बताती हूं।"
- Site visit offer करने के बाद call मत ख़त्म करो — caller का जवाब सुनो।
- हर response 1-2 वाक्य।

BOOKING TOOL (बहुत ज़रूरी — यह text आवाज़ में नहीं जाएगा):
- जब caller site visit के लिए किसी दिन/समय पर राज़ी हो जाए, तब अपने reply के
  आख़िर में एक नई line पर बिल्कुल यह format लिखो:
  [[BOOK day=<दिन या तारीख़> time=<समय> name=<caller का नाम या unknown>]]
- जब caller brochure माँगे या brochure भेजने पर हाँ कहे, तो reply के आख़िर में:
  [[BROCHURE]]
- ये markers के बिना system booking नहीं कर पाएगा। Markers के अलावा बाक़ी पूरा
  reply normal बोलचाल में हो।
""".strip()

GREETING = os.getenv(
    "GREETING",
    "नमस्ते! मैं Priya बोल रही हूं N Rose Developers से। "
    "क्या मैं आपका थोड़ा सा समय ले सकती हूं?",
)
