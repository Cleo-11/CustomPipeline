# Northern Heights — Hinglish Real-Estate Voice Agent

A low-latency, production-oriented outbound/inbound calling agent for **N Rose
Developers**. Stack:

| Layer | Choice | Why |
|------|--------|-----|
| Telephony | **Vobiz** (WebSocket audio streaming) | India DIDs, bulk outbound, bidirectional mu-law stream |
| STT | **Sarvam Saaras v3** (streaming WS) | best-in-class Indic + code-mix, sub-250ms first byte |
| LLM | **Qwen2:7B via Ollama** (OpenAI-compatible) | local, zero per-token cost |
| TTS | **Sarvam Bulbul v3** (streaming WS) | natural Hindi/Hinglish voice |

---

## How it fits together

```
PSTN ──► Vobiz ──(<Stream> bidirectional mu-law 8k)──► server.py /ws
                                                          │
                                                   CallSession (session.py)
   caller mu-law 20ms frames ─► ulaw→PCM16 ─┬─► local energy VAD ─► barge-in
                                            └─► Sarvam STT (8k PCM) ─► transcripts
                                                       │ endpoint (VAD + 550ms)
                                                       ▼
                                        Qwen2:7B (Ollama, token stream)
                                                       │ clause-by-clause
                                                       ▼
                                        Sarvam Bulbul TTS (stream)
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
| Endpointing (silence after speech) | 400–550 ms | `ENDPOINT_SILENCE_MS`, Saaras VAD |
| STT final transcript | ~100–200 ms | streaming, runs continuously |
| LLM time-to-first-token | 150–400 ms | warm Ollama (`keep_alive`), short prompt, GPU |
| First clause → TTS first byte | 150–300 ms | clause chunking + `min_buffer_size=1` |
| **Perceived total** | **~0.7–1.0 s** | + optional `THINKING_FILLER` to mask TTFT |

Two cheap tricks already wired in:
- **Clause chunking** (`llm.stream_sentences`): the first clause is flushed to
  TTS early (after ~12 chars), so audio begins long before the full reply.
- **Thinking filler**: a tiny "हम्म" plays the instant the caller stops, hiding
  LLM startup. Set `THINKING_FILLER=` to disable.

---

## The Hinglish naturalness problem (read this)

Your original prompt had Priya speak **romanised** Hinglish (`"Aap kaunse..."`).
TTS engines frequently mispronounce romanised Hindi — they read it as English.
So the system prompt in `config.py` instructs the model to write **Hindi words
in Devanagari** and keep only genuinely-English tokens (brand names, "BHK",
"sq ft", numbers) in Latin. Bulbul handles that code-mixed script far more
naturally. The persona, 1–2 sentence limit, flow, objections and KB are all
preserved from your script.

Two honest caveats:
1. **Qwen2:7B is the weakest link for "very, very human" Hinglish.** It works,
   but for noticeably better code-mix try `qwen2.5:7b-instruct`, or — since the
   LLM layer is just an OpenAI-compatible base URL — point `LLM_BASE_URL` at
   Sarvam's own `sarvam-m`/`sarvam-30b` (built for this) and compare. No code
   change, just `.env`.
2. **Confirm one Sarvam field.** `STT_ENCODING` defaults to `audio/x-raw` for
   raw PCM16. If transcripts come back empty, switch it to `pcm_s16le` (the
   streaming API accepts `wav | pcm_s16le | pcm_l16 | pcm_raw`).

---

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in keys

# LLM: keep the model warm so it never cold-starts mid-call
ollama pull qwen2:7b
OLLAMA_KEEP_ALIVE=-1 ollama serve
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
| `server.py` | FastAPI: Vobiz webhooks + `/ws` audio socket |
| `session.py` | Per-call orchestrator: turn-taking, barge-in, pipelining |
| `sarvam_stt.py` | Saaras v3 streaming client |
| `sarvam_tts.py` | Bulbul v3 streaming client → Vobiz mu-law frames |
| `llm.py` | Ollama streaming + clause chunker + booking-marker parser |
| `audio.py` | G.711 mu-law codec + resampling (unit-tested) |
| `booking.py` | Appointment store + WhatsApp brochure via Vobiz |
| `config.py` | All tunables + KB + Priya system prompt |
| `make_call.py` | Outbound dialer |

---

## Tuning guide

| Symptom | Fix |
|--------|-----|
| Agent talks over the caller | lower `BARGEIN_MIN_FRAMES` / `BARGEIN_RMS_THRESHOLD` |
| Agent cuts caller off too early | raise `ENDPOINT_SILENCE_MS` (e.g. 700) |
| Long pause before agent replies | warm Ollama, shorten KB, enable `THINKING_FILLER`, move to vLLM/GPU |
| Robotic / metallic voice | confirm 24k→8k path; don't double-resample |
| Empty transcripts | switch `STT_ENCODING` to `pcm_s16le` |
| Mispronounced Hindi | ensure model outputs Devanagari (see prompt) |

---

## Alternative: managed pipeline (Pipecat / LiveKit)

Both Vobiz and Sarvam ship first-party plugins for **Pipecat** and **LiveKit**,
which handle VAD, interruption and turn-taking for you. If you'd rather not own
the orchestration in `session.py`, that's the faster path to production — wire
`sarvam.STT(saaras:v3)` + an OpenAI-compatible LLM (your Ollama/vLLM URL) +
`sarvam.TTS(bulbul:v3)` into a Pipecat pipeline on a Vobiz SIP trunk. The
from-scratch version here gives you full control over latency and the booking
logic; the managed version gives you less code to maintain. Same models either
way.
