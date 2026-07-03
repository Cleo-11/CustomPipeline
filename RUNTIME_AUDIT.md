The Northern Heights voice runtime
==================================

A from-scratch, single-tenant conversational-AI telephony agent: one FastAPI process bridges a Vobiz mu‑law audio stream to Deepgram STT, an OpenAI-compatible LLM, and Sarvam TTS to sell apartments in Hinglish over the phone. This document traces every file, every wire event, and every gap between what the code claims and what it does.

**12**tracked source files**~940**lines of Python**1**process, 1 event loop per call**0**tests, 0 CI, 0 containers**2026-07-03**audit date01 / 20

Project tree
------------

The repository is flat — no src/ layout, no packages. Everything lives at the root, which is consistent with the project's actual size (nine Python modules) but means the tree below is the entire architecture map.

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   D:\RealEstate  ├── server.py            # FastAPI app: Vobiz webhooks + /ws audio socket (entrypoint)  ├── session.py            # CallSession — per-call state machine, the orchestrator  ├── llm.py                # LLM streaming client, sentence chunker, action-marker parser  ├── audio.py               # G.711 mu-law codec + resampling — pure functions, no I/O  ├── sarvam_stt.py          # NAME IS STALE: actual client is Deepgram nova-2, not Sarvam  ├── sarvam_tts.py          # Sarvam Bulbul v3 REST client → mu-law frames  ├── booking.py             # JSONL booking log + WhatsApp brochure send via Vobiz  ├── config.py              # All env vars, the knowledge base, and Priya's system prompt  ├── make_call.py           # standalone CLI: places one outbound call via Vobiz REST  ├── requirements.txt       # pinned floors: fastapi, openai, httpx, webrtcvad, sarvamai…  ├── README.md              # architecture + latency budget + setup instructions  ├── .gitignore             # contains exactly one line: ".env" — added after .env was committed  ├── .env                   # TRACKED IN GIT — live Sarvam/Vobiz/Deepgram/LLM keys, see §19  ├── __pycache__/*.pyc  ×8   # compiled bytecode, tracked in git (should not be)  └── agent*.log / agentd.log / agentgemini*.log  ×8   # ad hoc dev-run transcripts, untracked, at repo root   `

No tests/, docker/, .github/, migrations/, or infra/ directory exists anywhere in the tree — there is no test suite, no CI pipeline, and no infrastructure-as-code. Deployment is the manual uvicorn invocation documented in the README (see [§19](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#health), [§20](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#summary)).

##### Runtime core

server.py, session.py, audio.py — the request lifecycle and the audio math. Everything the call path touches on every turn.

##### Provider clients

llm.py, sarvam\_stt.py, sarvam\_tts.py — thin wrappers around three external speech/text services.

##### Business logic

booking.py — the only file that writes anything durable or talks to a "CRM" (a flat file).

##### Configuration & ops

config.py, make\_call.py, README.md — tunables, persona/KB text, and the outbound dialing CLI.

02 / 20

Architecture overview
---------------------

One FastAPI process owns the whole pipeline. There is no gateway service, no message broker, no separate STT/TTS worker pool — every call is a single WebSocket connection that pins one Python asyncio event loop for its duration, per the docstring in server.py.

### System diagram

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML   `PSTN caller         │         ▼   ┌───────────┐   POST /answer            ┌─────────────────────────────┐   │   Vobiz   │ ─────────────────────────▶│   server.py  (FastAPI)      │   │ telephony │ ◀─────────────  XML│   4 HTTP routes + 1 WS route │   └─────┬─────┘                           └──────────────┬───────────────┘         │ WS: bidirectional mu-law 8k                     │ one CallSession         ▼                                                  ▼   ┌───────────────────────────────────────────────────────────────────┐   │                      session.py — CallSession                     │   │   barge-in VAD  ⇄  STT bridge  ⇄  turn buffer  ⇄  speak scheduler  │   └────────┬───────────────────┬────────────────────┬──────────────────┘            │ raw mu-law         │ chat messages       │ clause text            ▼                    ▼                     ▼   ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────────┐   │  sarvam_stt.py   │  │      llm.py       │  │    sarvam_tts.py     │   │  (= Deepgram WS) │  │ OpenAI-compat SDK │  │  Sarvam Bulbul REST  │   │  nova-2, hi       │  │ Ollama by default │  │  → mu-law frames     │   └─────────────────┘  └──────────────────┘  └─────────────────────┘                                  │                          [[BOOK]] / [[BROCHURE]]                                  ▼                            booking.py                      JSONL file  +  Vobiz WhatsApp API`

### Voice pipeline (per audio frame)

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   Vobiz "media" event (base64 mu-law, 160B/20ms)    └─▶ base64 decode          └─▶ forward raw bytes to Deepgram WS  (always — STT needs continuous audio)          └─▶ ulaw_to_pcm16()                              [audio.py]                └─▶ rms() energy check          ┐                └─▶ webrtcvad.is_speech()        ├─▶ 25 consecutive true frames ⇒ _interrupt()                      (skipped while agent speaks)┘   `

### AI / conversation pipeline (per turn)

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   STT final transcript    └─▶ _pending_user buffer += text    └─▶ restart 550ms silence timer  (config.ENDPOINT_SILENCE_MS)          └─▶ timer fires ⇒ _respond_to(text)                └─▶ messages.append({role:"user", ...})                └─▶ llm.stream_sentences(messages)     token stream, OpenAI-compat                      └─▶ buffer until a hard break (।?!.\n) past a length threshold                            └─▶ yield clause                                  └─▶ llm.extract_actions(clause)  → clean text, booking?, brochure?                                        └─▶ booking.save_booking() / booking.send_brochure()  (fire-and-forget)                                  └─▶ CallSession._speak(clean text)                                        └─▶ SarvamTTS.synthesize()  → mu-law frames                                              └─▶ "playAudio" WS events, paced 1 per 20ms   `

### Deployment architecture (as documented / as it exists)

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   Documented (README):                          Actually present in repo:    TLS domain ─▶ uvicorn (1 worker)              uvicorn run manually from a shell    scale = N processes behind an LB              no LB config, no process manager,    ollama serve --keep-alive=-1                  no systemd unit, no Dockerfile    optional: swap to vLLM for concurrency         no docker-compose, no CI, no IaC                                                    eight ad hoc *.log files show the                                                    actual LLM backend used in testing                                                    was Google Gemini, not Ollama   `

Every arrow above is a real code path traced from the source, not an assumption — see [§18](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#runtime) for the fully narrated walkthrough with function names.

03 / 20

Directory responsibilities
--------------------------

There are no subdirectories in the source tree — the "directories" worth documenting are the functional groupings of files at the root, and the two generated directories (\_\_pycache\_\_/, and the runtime-created bookings.jsonl which does not yet exist on disk — no booking has ever been completed).

**GroupFilesPurposeDepends onDepended on by**Entrypointserver.pyHTTP/WS surface; owns the FastAPI app object and the uvicorn run commandconfig, sessionuvicorn process manager (manual)Orchestratorsession.pyPer-call state machine: turn-taking, barge-in, streaming pipeline glueaudio, config, llm, booking, sarvam\_stt, sarvam\_tts, webrtcvadserver.pyProvider clientsllm.py, sarvam\_stt.py, sarvam\_tts.pyIsolate each external speech/text API behind a small async interfaceconfig, openai/websockets/httpx SDKssession.pySignal processingaudio.pyPure mu-law↔PCM16 codec + linear resampler + RMS; no network, no statenumpy onlysession.py, sarvam\_tts.pyBusiness logicbooking.pyThe only durable side effect in the system: append a booking, POST a brochureconfig, httpxsession.py (via fire-and-forget tasks)Configurationconfig.pyEnv var loading, all latency/VAD tunables, the KB text, the system prompt, the greetingpython-dotenvevery other module except sarvam\_stt.py (see §19 — it reads DEEPGRAM\_API\_KEY directly)Ops toolingmake\_call.pyStandalone CLI to trigger one outbound call; not imported by the serverconfig, httpxa human operatorGenerated\_\_pycache\_\_/Compiled bytecode caches — accidentally tracked in gitwhatever module was last importednothing; safe to remove

**Initialization order** is simple because there is exactly one entrypoint: uvicorn imports server.py → server.py imports config (which calls load\_dotenv() at import time and raises if a truly-required var is missing, though today no caller uses config.\_get's strict path — every var goes through os.getenv with a default) → imports session.CallSession (which imports everything else). Nothing is instantiated until a WebSocket connects; CallSession and its SarvamSTT/SarvamTTS/webrtcvad.Vad members are constructed fresh per call, so there is no shared cross-call state anywhere in the process.

04 / 20

File-by-file analysis
---------------------

### server.py **entrypoint**

**Purpose:** the only network-facing surface. Defines the FastAPI app and every route.

**SymbolKindBehavior**answer(request)async route, POST /answerReads Vobiz form fields From/To/Direction, builds and returns  XML pointing at wss://{PUBLIC\_HOST}/ws. No validation of the caller identity.hangup(request)async route, POST /hangupLogs CallUUID/Duration/HangupCause; no CRM/analytics write occurs here despite the docstring calling it a "CRM hook."stream\_status(request)async route, POST /stream-statusLogs the entire form dict verbatim. This is the only place stream lifecycle events (StartStream, PlayedStream, ClearedAudio) are observed, and only as an INFO log line.health()async route, GET /healthReturns {"status":"ok","ws\_url":...}. No dependency checks (Deepgram/Sarvam/LLM reachability are not probed).ws(websocket)WS route, /wsAccepts the socket, constructs one CallSession, loops receive\_text → session.handle\_event until disconnect, always calls session.cleanup() in finally.

**Side effects:** logging only, plus whatever CallSession triggers downstream. **Error handling:** a bare except Exception around the WS loop logs and falls through to cleanup — a single malformed frame or provider exception never crashes the process, but it also never surfaces to any alerting system. **Auth:** none of the five routes check a signature, token, or IP allowlist.

### session.py **orchestrator**

**Purpose:** "the brain of a single call," per its own docstring — the only stateful class in the system.

**MethodTriggerResponsibility**handle\_eventevery WS text frameDispatches on event: start / media / playedStream / clearedAudio / stop\_on\_startevent=="start"Captures stream/call IDs, starts the STT socket, speaks the greeting\_on\_mediaevent=="media"Forwards audio to STT unconditionally; runs the two-signal barge-in detector when not already speaking\_on\_partial / \_on\_finalSTT callbacksPartial: a second, independent barge-in trigger. Final: appends to the pending-utterance buffer and (re)starts the endpoint timer\_endpoint\_after\_silencetimer fires after ENDPOINT\_SILENCE\_MSTreats the buffered text as a complete turn and calls \_respond\_to\_respond\_to → \_generate\_and\_speaknew user turnAppends to history, streams the LLM reply clause-by-clause, extracts booking/brochure markers, speaks each clean clause, appends the full reply to history when the turn completes or is cancelled\_speakeach clauseStreams TTS mu-law frames to Vobiz as playAudio events, paced at one frame per 20ms; sends a checkpoint event when done\_interruptbarge-in detectedSends clearAudio, cancels the in-flight speak taskcleanupWS disconnect / stop eventCancels outstanding tasks, closes the STT socket

**Concurrency:** a monotonically increasing \_turn\_seq guards against a superseded LLM/TTS task writing stale output after a barge-in — \_generate\_and\_speak checks seq != self.\_turn\_seq before every speak call. **State lives only in the instance** — nothing is written to disk or a shared store, so a process restart loses every in-flight conversation.

### llm.py **provider client**

**Purpose:** stream a chat completion from any OpenAI-compatible endpoint and re-chunk it into speakable clauses; also owns the inline-action-marker mini-language.

*   stream\_sentences(messages) — opens a streaming chat.completions.create call (max\_tokens=160, temperature=config.LLM\_TEMPERATURE), buffers deltas, and yields a clause every time a hard-break character (। ? ! . \\n) appears past a length floor (120 chars for the first clause, 180 after) — the core latency trick described in the README.
    
*   \_breakpoint(text, min\_len) — pure helper, first qualifying hard-break index or None.
    
*   extract\_actions(text) — regexes \[\[BOOK ...\]\] and \[\[BROCHURE\]\] out of a clause, returning clean spoken text plus a parsed booking dict and a brochure flag.
    

**Error handling:** any exception from the streaming call is caught, logged, and whatever partial buffer exists is yielded — the caller never sees the exception, so a mid-call LLM outage degrades to a truncated or empty reply rather than a crash (see the Gemini 429/400 evidence in [§19](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#health)). **No retries, no backoff, no timeout override** — whatever the openai SDK's defaults are is what applies.

### audio.py **pure functions**

**Purpose:** G.711 mu-law codec and resampling, written to avoid Python 3.13's removed audioop module. Vectorized with numpy.

*   pcm16\_to\_ulaw / \_build\_decode\_table + ulaw\_to\_pcm16 — encode/decode, decode is table-driven for speed.
    
*   resample(pcm, src\_rate, dst\_rate) — linear interpolation via np.interp; documented as "cheap, good enough for 8kHz voice," not a proper polyphase/sinc resampler.
    
*   pcm16\_to\_vobiz\_frames — resamples TTS output down to 8kHz, mu-law encodes, and slices into 160-byte (20ms) frames matching Vobiz's ingress framing.
    
*   rms(pcm) — root-mean-square energy, one leg of the barge-in detector.
    

No I/O, no globals mutated after module load, no exceptions raised for malformed input beyond whatever numpy itself throws. The README calls this module "unit-tested" — no test file exists anywhere in the repository (see [§19](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#health)).

### sarvam\_stt.py **misnamed**

**Purpose (actual):** a Deepgram nova-2 streaming client, despite the filename, the class name SarvamSTT, and the module docstring ("Streaming STT via Deepgram SDK v2" — the docstring itself is honest; the name is not).

*   Connects to a hardcoded Deepgram WS URL with query params model=nova-2&language=hi&encoding=mulaw&sample\_rate=8000&...&endpointing=300&smart\_format=true, authenticated via Authorization: Token {DEEPGRAM\_API\_KEY} — an env var read directly with os.getenv, bypassing config.py entirely.
    
*   \_read\_loop — consumes Deepgram's Results/UtteranceEnd messages, routes final vs. interim transcripts to the two callbacks passed in by CallSession.
    
*   send\_ulaw — forwards raw mu-law bytes (Deepgram accepts mu-law natively via the encoding query param, so audio.py's PCM conversion is not needed on this leg).
    
*   send\_pcm16 and flush are no-op stubs (pass) — dead interface surface, never called by session.py.
    

None of the Sarvam-branded config vars (STT\_MODEL=saaras:v3, STT\_LANGUAGE, STT\_ENCODING) are read by this file — they are entirely unused dead configuration. See [§19](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#health).

### sarvam\_tts.py **not actually streaming**

**Purpose:** text→speech via Sarvam's bulbul:v3 model, over the plain REST endpoint https://api.sarvam.ai/text-to-speech — a single blocking POST per clause, not the "streaming WS" the README's architecture table claims.

*   Builds a JSON payload (inputs, target\_language\_code, speaker, model, pace), POSTs with a 15s timeout.
    
*   Response is one base64 WAV. The code manually parses the WAV header via struct.unpack\_from (sample rate at byte 24, channel count at byte 22) instead of trusting config.TTS\_SRC\_RATE, strips the 44-byte header, downmixes stereo to mono by taking every other sample.
    
*   Delegates to audio.pcm16\_to\_vobiz\_frames for resampling + mu-law framing, then yields each 160-byte frame.
    

**Error handling:** HTTP errors and generic exceptions are caught and logged; on failure the generator simply yields nothing, so CallSession.\_speak sends zero frames — dead air, not a fallback phrase.

### booking.py **brochure link is a stub**

*   save\_booking(call\_id, caller, booking) — appends one JSON line (timestamp + call\_id + caller + parsed fields) to bookings.jsonl in append mode. No schema validation on the booking dict's keys.
    
*   send\_brochure(to\_number) — POSTs a WhatsApp "document" message via Vobiz's WhatsApp Business API. BROCHURE\_URL is hardcoded to https://your-cdn.example.com/northern-heights-brochure.pdf — a placeholder domain that does not resolve to a real asset.
    

Both functions are invoked via asyncio.create\_task(...) from session.py, i.e. fire-and-forget — their success or failure never affects what the agent says next, by design (per the module docstring, "never block the voice loop").

### config.py **central config**

Loads .env via python-dotenv, exposes ~25 tunables (credentials, endpoints, LLM/STT/TTS model selectors, latency/VAD thresholds), plus three large string constants: KNOWLEDGE\_BASE (the Northern Heights project facts), SYSTEM\_PROMPT (Priya's persona, told to output \[\[BOOK ...\]\]/\[\[BROCHURE\]\] markers), and GREETING. A helper \_get() exists for env vars that should hard-fail if missing, but every actual lookup in the file uses the soft os.getenv(name, default) form — \_get is defined but never called.

### make\_call.py **CLI**

Standalone script, not imported anywhere else. python make\_call.py --to +9198XXXXXXXX POSTs to Vobiz's Call/ endpoint with answer\_url/hangup\_url pointed back at this server's own PUBLIC\_HOST. No async, uses sync httpx.post. Exits via sys.exit on missing credentials rather than raising.

05 / 20

Request & execution flows
-------------------------

### Inbound call

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   Vobiz POST /answer    server.answer()      → build  XML (wss://PUBLIC_HOST/ws)    Vobiz opens WS → server.ws()      → CallSession(send_json)      → loop: receive_text() → session.handle_event()          "start" → session._on_start()              → SarvamSTT.start()            [opens Deepgram WS]              → messages.append(GREETING as assistant turn)              → asyncio.create_task(_speak_greeting)   `

### Outbound call

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   operator: python make_call.py --to +91...    make_call.make_call()      → POST {VOBIZ_API_BASE}/Account/{ID}/Call/  (answer_url = this server's /answer)    Vobiz dials → callee answers → Vobiz hits /answer  (same flow as Inbound, above)   `

### Conversation turn (the core loop)

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   caller speech (mu-law frames) → STT partials/finals    on final: _pending_user += text; restart 550ms timer    timer fires, no new speech in that window:      _respond_to(text)        → messages.append(user)        → _generate_and_speak(seq)            for clause in llm.stream_sentences(messages):                if seq stale: return                    # superseded by barge-in                clean, booking, brochure = llm.extract_actions(clause)                if booking:  create_task(booking.save_booking(...))                if brochure: create_task(booking.send_brochure(...))                if clean:    await self._speak(clean)            messages.append(assistant, full_reply)        # in finally, even if cancelled   `

### Call termination

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   Vobiz sends "stop" event  →  session.cleanup()     — or —  WS disconnects            →  server.ws() catches WebSocketDisconnect → session.cleanup()     cleanup(): cancel endpoint timer + speak task; SarvamSTT.close() (cancels reader, closes Deepgram WS)  separately, Vobiz POST /hangup  →  server.hangup()  →  log CallUUID/Duration/HangupCause only  separately, Vobiz POST /stream-status (StartStream / PlayedStream / ClearedAudio)  →  logged only   `

### Booking / brochure ("tool" execution)

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   LLM emits clause containing "[[BOOK day=Sat time=4pm name=Rohan]]"    llm.extract_actions() → booking={"day":"Sat","time":"4pm","name":"Rohan"}, clean text stripped of the marker    session.py: asyncio.create_task(booking.save_booking(call_id, caller_name, booking))      booking.save_booking() → append one JSON line to bookings.jsonl   (fire-and-forget, no confirmation to caller)  LLM emits "[[BROCHURE]]"    session.py: asyncio.create_task(booking.send_brochure(caller_number))      booking.send_brochure() → POST Vobiz WhatsApp API with a placeholder PDF URL (non-functional as shipped)   `

There is no CRM update, calendar booking, or analytics-write flow beyond the JSONL append above — those steps from the audit prompt's checklist do not exist in this codebase.

06 / 20

AI orchestration
----------------

**ConcernHow it's actually handled**Conversation managerCallSession itself — there is no separate "conversation manager" class; turn sequencing lives in \_turn\_seq/\_respond\_to/\_generate\_and\_speak.Prompt builderNone — config.SYSTEM\_PROMPT is a single static f-string built once at import time; there is no per-call templating, no variable substitution (caller name, time of day, etc. are never injected into the prompt).System promptDefines persona "Priya," language rules (Hindi in Devanagari, English terms in Latin script), a 1–2 sentence reply cap, the sales flow (greet → name → BHK preference → price/size → amenities → invite to visit), and the \[\[BOOK\]\]/\[\[BROCHURE\]\] output contract.Conversation historyself.messages: list\[dict\] on the CallSession instance — plain list of {role, content}, grows unbounded for the life of the call, never truncated or summarized.Memory injection / summariesNone implemented — see [§08](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#memory).Tool callingNot the LLM-native tool-call API — actions are plain-text markers (\[\[BOOK ...\]\]) the model is instructed to emit inline, parsed with regex in llm.extract\_actions. See [§07](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#tools).Context management / tokensmax\_tokens=160 caps each reply; LLM\_NUM\_CTX exists in config but is never passed to the completion call, so it's unused dead configuration.Fallback responses / guardrailsNone — if the LLM call fails entirely, stream\_sentences logs and yields whatever partial buffer it had (possibly empty), and the agent simply says nothing for that turn. No scripted fallback line, no profanity/hallucination filter.StreamingToken-level streaming from the LLM, re-chunked into sentence-ish clauses; each clause is spoken as soon as it's ready while the next clause is still being generated — genuine pipelining.Interruptions (barge-in)Two independent triggers: (1) sustained RMS + WebRTC VAD agreement for 25 consecutive 20ms frames while the agent is speaking; (2) an STT partial-result arriving more than 500ms after the agent finished speaking. Either calls \_interrupt(), which clears Vobiz's playback buffer and cancels the speak task.Response generationDirectly the OpenAI-compatible chat.completions.create(stream=True) call in llm.py — no intermediate reasoning step, no retrieval, single LLM call per turn.**Latency design, credited where due**

The clause-chunking threshold (flush the first clause after ~120 chars, later ones after ~180, but always waiting for a hard sentence-break) is a deliberate, working latency optimization: it lets TTS begin on the first clause while the LLM is still generating the rest of the reply. This is the single most sophisticated piece of engineering in the codebase.

07 / 20

Tool system
-----------

The system implements exactly **two** tools, both invoked the same way: the LLM appends a bracketed marker to its reply text, llm.extract\_actions regex-parses it out, and session.py fires an asyncio.create\_task for the corresponding handler. There is no tool registry, no JSON-schema-typed tool definitions, and no LLM-native function-calling — this is a hand-rolled, single-shot text protocol.

#### Category: Booking / CRM

**FieldValue**Tool nameBOOK (marker: \[\[BOOK day=.. time=.. name=..\]\])PurposeRecord a site-visit appointment the caller agreed toInput schemaFreeform key=value pairs parsed by regex (\_KV\_RE) — no schema enforcement; any keys the model writes are accepted verbatimOutput schemaNone returned to the model or the caller — fire-and-forgetValidationNone — a malformed or empty day/time/name is stored as-isInvoked bysession.\_generate\_and\_speak, immediately on detecting the marker in a streamed clauseExecution pathbooking.save\_booking() → append one JSON line to bookings.jsonlExternal dependencyLocal filesystem onlyRetry / timeout / authNone of the three — a write failure is caught, logged, and silently droppedProduction ready?**No** — flat file with no rotation, no backup, no concurrent-write protection beyond the OS append guarantee, no confirmation surfaced to the caller or an agent dashboard

#### Category: WhatsApp / Notifications

**FieldValue**Tool nameBROCHURE (marker: \[\[BROCHURE\]\])PurposeSend the project brochure PDF to the caller over WhatsAppInput schemaNone — takes only the caller's number, already known from the callOutput schemaNone returned; HTTP status is logged onlyInvoked bysession.\_generate\_and\_speakExecution pathbooking.send\_brochure() → POST {VOBIZ\_API\_BASE}/Account/{ID}/whatsapp/messagesExternal dependencyVobiz WhatsApp Business APIAuthX-Auth-ID / X-Auth-Token headers from configRetry / timeoutNo retry; 10s httpx timeoutProduction ready?**No** — BROCHURE\_URL is a hardcoded placeholder domain (your-cdn.example.com) that does not host a real file; every brochure send will deliver a broken link or fail outright

No tools exist in the categories the audit prompt anticipates: Calendar, Knowledge Base (retrieval), Payments, Analytics, or Internal/Utility tools. The "knowledge base" is static text baked directly into the system prompt (config.KNOWLEDGE\_BASE), not a queryable tool.

08 / 20

Memory system
-------------

**LayerStatus**Session / conversation memoryPresent — CallSession.messages, a plain in-process list, seeded with the system prompt and appended to on every turn.Short-term memory (pending utterance)Present — \_pending\_user accumulates STT finals between silence-timer resets.Long-term / cross-call memory**Absent** — nothing about a caller persists between calls. A repeat caller starts from zero every time.Conversation summaries**Absent** — history grows unbounded for the call's duration; nothing trims or summarizes it, so very long calls would eventually hit the LLM's context/token limit with no handling for that case.Facts / user preferences / lead infoOnly what \[\[BOOK ...\]\] captures, and only if the model remembers to emit it — there's no structured slot-filling for name, BHK preference, or budget outside that marker.Caching**Absent** — no LLM response cache, no TTS audio cache (the same clause spoken twice is synthesized twice).PersistenceOnly bookings.jsonl survives a process restart; conversation memory does not.Eviction / synchronizationN/A — nothing is shared across sessions, so there's nothing to synchronize; eviction happens implicitly when the CallSession object is garbage-collected after cleanup.09 / 20

Database
--------

There is no database engine — no Postgres, SQLite, Mongo, or Redis anywhere in requirements.txt or the source. The system's entire persistence layer is one flat file.

**Storebookings.jsonl**FormatNewline-delimited JSON, append-onlySchemaNone enforced — each line is {ts, call\_id, caller, \*\*arbitrary keys from the LLM's marker}IndexesNoneRelationshipsNone — no foreign keys, no link to a separate "caller" or "property" recordCRUDCreate only (append). No read, update, or delete path exists anywhere in the codebase — nothing ever reads this file back.Transactions / constraintsNone — a crash mid-write could in theory leave a partial line; not handledMigration strategyN/A — there is no schema to migrate

The README's comment ("swap for Postgres/your CRM in prod") signals this was always intended as a placeholder, not a finished data layer.

10 / 20

API reference
-------------

**MethodPathPurposeAuthRequestResponse**POST/answerVobiz answer webhook — returns the  XML that opens the audio WSNoneform-encoded: From, To, Directionapplication/xml — POST/hangupVobiz call-ended webhookNoneform: CallUUID, Duration, HangupCausetext/plain "OK"POST/stream-statusVobiz stream lifecycle events (StartStream/PlayedStream/ClearedAudio)Noneform, entire body loggedtext/plain "OK"GET/healthLiveness probe + advertises the WS URLNone—{"status":"ok","ws\_url":"wss://..."}WS/wsBidirectional mu-law audio stream, one CallSession per connectionNoneJSON text frames: start / media / playedStream / clearedAudio / stopJSON text frames: playAudio / clearAudio / checkpoint**No authentication on any route**

None of the five routes verify a Vobiz request signature, shared secret, or IP allowlist. Anyone who learns PUBLIC\_HOST can POST forged /answer//hangup webhooks, or open /ws directly and drive the LLM/TTS pipeline at the operator's expense without ever placing a real call.

11 / 20

External integrations
---------------------

**ServiceUsed forProtocolAuthRetry logicNotes**VobizTelephony (inbound/outbound), bidirectional audio WS, WhatsApp Business APIREST + WS + webhooksX-Auth-ID / X-Auth-Token headersNoneAlso the caller of /answer//hangup//stream-statusDeepgramSpeech-to-text (nova-2 model, Hindi)WebSocketAuthorization: Token header, key read directly via os.getenv("DEEPGRAM\_API\_KEY")NoneImplemented in the file named sarvam\_stt.py — see [§19](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#health)Sarvam AIText-to-speech (Bulbul v3)REST (one POST per clause)api-subscription-key headerNone, 15s timeoutsarvamai SDK is a listed dependency but never imported — this integration is hand-rolled over httpxLLM backendReply generationOpenAI-compatible REST (streaming)Bearer key (LLM\_API\_KEY, default "ollama")NoneDocumented default is local Ollama (Qwen2:7B); log evidence (agentd.log) shows live testing against Google's Gemini OpenAI-compat endpoint, which returned 429 quota errors and a 400 for an unrecognized "options" payload field

No integrations exist for email, a CRM SaaS, Google Calendar, cloud storage, analytics/monitoring platforms, or payments — none are imported, configured, or referenced anywhere in the source.

12 / 20

Session management
------------------

*   **Creation:** one CallSession is instantiated per accepted WebSocket connection, inside server.ws().
    
*   **Lifecycle:** lives exactly as long as the WS connection; there is no session ID that outlives the socket, no reconnect/resume support.
    
*   **State transitions:** driven entirely by inbound WS event names (start → media\* → stop) and internal flags (\_is\_speaking, \_greeting\_active, \_bargein\_run, \_turn\_seq) — there's no explicit state-machine enum, just a handful of booleans and counters.
    
*   **Cleanup:** cleanup() cancels the endpoint timer and any in-flight speak task, then closes the STT socket. Always called from a finally block in server.ws(), so it runs on both clean and errored disconnects.
    
*   **Expiration / heartbeat / reconnect:** none implemented — a dropped WS simply ends the call; there is no ping/pong keepalive logic beyond whatever websockets/FastAPI do by default.
    
*   **Resource cleanup:** the Deepgram WS is explicitly closed; the TTS client is a fresh httpx.AsyncClient per synth call (context-managed, so no leak); pending asyncio tasks are cancelled but not awaited for cancellation to finish before cleanup() returns.
    

13 / 20

Event system
------------

**MechanismWhereRole**WS event dispatchsession.handle\_eventThe only "internal event bus" in the system — a plain if/elif on data\["event"\], not a pub/sub or emitter pattern.Fire-and-forget tasksasyncio.create\_task(...) in \_generate\_and\_speakUsed for booking save and brochure send so they never block the speech loop. This is the closest thing to a background worker in the codebase.Silence/endpoint timer\_endpoint\_after\_silenceA single-shot asyncio.sleep-based timer, restarted on every STT final; functions as an ad hoc debounce, not a scheduler.Webhooks (inbound)/hangup, /stream-statusReceived and logged; not re-emitted as internal events or forwarded anywhere.

There is no message queue, no pub/sub broker (Redis/Kafka/RabbitMQ), no cron scheduler, and no background worker process separate from the request-handling event loop. Everything happens inline within the single asyncio loop that owns the call.

14 / 20

Configuration
-------------

#### Environment variables (defined in .env / read in config.py unless noted)

**VariableDefaultGoverns**PUBLIC\_HOSTlocalhost:8000Hostname Vobiz reaches this server on; used to build the WS/webhook URLs it returnsSARVAM\_API\_KEY—TTS authDEEPGRAM\_API\_KEY—STT auth — read directly in sarvam\_stt.py, _not_ via config.pyVOBIZ\_AUTH\_ID / VOBIZ\_AUTH\_TOKEN / VOBIZ\_API\_BASEbase defaults to api.vobiz.ai/api/v1Telephony + WhatsApp API authLLM\_BASE\_URL / LLM\_API\_KEY / LLM\_MODEL / LLM\_TEMPERATURE / LLM\_NUM\_CTXOllama localhost, qwen2:7b, 0.6, 4096Which LLM backend to call; LLM\_NUM\_CTX is unused dead config (never passed to the SDK call)STT\_MODEL / STT\_LANGUAGE / STT\_ENCODINGsaaras:v3, hi-IN, audio/x-rawDead config — the real STT client (Deepgram) hardcodes its own model/language/encoding in a URL string insteadTTS\_MODEL / TTS\_SPEAKER / TTS\_LANGUAGE / TTS\_PACE / TTS\_SRC\_RATEbulbul:v3, shubh, hi-IN, 1.05, 24000Sarvam TTS request params — TTS\_SRC\_RATE is also unused (the WAV header's actual rate is parsed at runtime instead)ENDPOINT\_SILENCE\_MS550Silence duration before a turn is considered finishedBARGEIN\_RMS\_THRESHOLD / BARGEIN\_MIN\_FRAMES / VAD\_AGGRESSIVENESS650, 25, 2Barge-in sensitivityTHINKING\_FILLER"हम्म"Declared but never referenced by session.py — dead configuration; the filler sound is not actually played anywhere in the current codeGREETINGHindi greeting stringFirst line spoken on every callFROM\_NUMBER—Default outbound caller ID, read only by make\_call.py

**Feature flags:** none. **Runtime vs. build config split:** none — everything is one flat .env. **Docker / CI / infra config:** none exist in the repository.

**.env is committed to git**

.env was added in the initial commit (bb7c75f) before .gitignore started excluding it, and git status shows it is still **tracked and modified** today — meaning the live Sarvam, Vobiz, Deepgram, and LLM credentials are in this repository's git history regardless of what the working copy currently contains. Adding a filename to .gitignore does not untrack a file that was already committed. See [§19](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#health) for the remediation note.

15 / 20

Error handling
--------------

**PatternPresent?Detail**Retries**No**Not implemented anywhere for STT, TTS, LLM, or Vobiz calls — a transient network blip is treated the same as a permanent failureCircuit breakers**No**A provider that is down stays "tried" on every single turn/callFallbacks**No**No scripted fallback line when the LLM or TTS fails — the agent goes silent for that turnTimeoutsPartialExplicit on TTS (15s) and brochure send (10s) via httpx; STT/LLM rely on library defaultsValidationMinimalNo schema validation on inbound webhook form data or WS event JSON beyond a bare json.loads try/except in handle\_eventException handling shapeConsistentNearly every external call is wrapped in a broad except Exception that logs and continues — safe against process crashes, but it means failures are invisible unless someone is watching logs liveDead-letter handling**No**A failed save\_booking or send\_brochure task is simply lost — no retry queue, no alertGraceful degradationYes, by omissionThe WS loop and CallSession.cleanup() guarantee a bad turn doesn't crash the whole call — the call just goes quiet rather than dropping16 / 20

Logging & observability
-----------------------

A single logging.basicConfig(level=INFO) call in server.py configures the root logger; every module gets its own named logger (server, session, llm, stt, tts, booking) writing plain-text lines to stdout. That's the entire observability stack.

*   **Metrics:** none — no Prometheus/StatsD/OpenTelemetry counters anywhere.
    
*   **Tracing:** none — no request/span IDs correlating a call across STT→LLM→TTS beyond the human-readable call\_id/stream\_id baked into log message text.
    
*   **Health checks:** GET /health exists but only confirms the process is up — it does not probe Deepgram, Sarvam, or the LLM backend.
    
*   **Call/turn/LLM logs:** present as INFO lines — STT FINAL, USER, LLM chunk, PRIYA, SPEAK called/done, booking saves. Genuinely useful for manually replaying a call from logs.
    
*   **Cost tracking / latency measurement:** none — no token counts, no per-call cost, no measured end-to-end latency, despite the README publishing a target latency budget.
    
*   **Audit logs:** none beyond the above INFO stream — no immutable audit trail, no PII redaction (phone numbers and transcripts are logged in full).
    

**Ad hoc log files at the repo root**

Eight files — agent.log, agent1-4.log, agentd.log, agentgemini.log, agentgemin1i.log — are stdout captures from manual dev runs, sitting untracked in the project root rather than in a logs/ directory or .gitignored. One (agentd.log) contains a live ngrok hostname and documents the LLM backend actually being tested was Gemini, not the documented Ollama default (see [§19](https://4dba832e-7272-4232-a2b3-3ea83db3208c.frame.claudeusercontent.com/_f/1783046845-1018/#health)).

17 / 20

Dependency graph
----------------

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   server.py   ├─▶ config.py   └─▶ session.py        ├─▶ audio.py            (numpy only — leaf)        ├─▶ config.py        ├─▶ llm.py ─────────────▶ config.py, openai SDK        ├─▶ booking.py ─────────▶ config.py, httpx        ├─▶ sarvam_stt.py ──────▶ websockets   (reads DEEPGRAM_API_KEY directly — NOT via config.py)        ├─▶ sarvam_tts.py ──────▶ audio.py, config.py, httpx, numpy        └─▶ webrtcvad            (third-party, direct)  make_call.py  ─▶ config.py, httpx        # standalone; not reachable from server.py   `

The graph is a shallow tree, not a mesh — session.py is the sole hub; no module other than config.py and audio.py is imported by more than one caller. sarvam\_stt.py's direct os.getenv call is the one break in the "all config flows through config.py" convention every other module follows.

18 / 20

Runtime execution — one full conversation turn
----------------------------------------------

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML `1. Vobiz "media" event arrives on /ws           → server.ws() → session.handle_event(raw)   2. event == "media"                              → CallSession._on_media(data)   3. base64.b64decode(payload)                      → raw mu-law bytes   4. await self.stt.send_ulaw(ulaw)                 → forwarded to Deepgram WS  [sarvam_stt.py]   5. audio.ulaw_to_pcm16(ulaw)                       → PCM16 for local analysis only   6. audio.rms(pcm)  +  self._vad.is_speech(...)     → barge-in signal (skipped while agent speaks)        ⋮ (repeats for every 20ms frame until STT emits a final)   7. Deepgram sends a "Results" message, is_final=true   8. SarvamSTT._read_loop → await self._on_final(transcript)   [session.py callback]   9. CallSession._on_final: _pending_user += text; cancel + restart 550ms timer  10. asyncio.sleep(0.55) completes uninterrupted     → _endpoint_after_silence()  11. user_text = pending buffer, cleared             → self._respond_to(user_text)  12. messages.append({"role":"user", "content":user_text}); _turn_seq += 1  13. asyncio.create_task(_generate_and_speak(seq))  14. llm.stream_sentences(messages)                  → opens streaming chat.completions.create()  [llm.py]  15. tokens accumulate in buf; on hitting a hard-break past threshold → yield clause  16. llm.extract_actions(clause)                     → clean text (+ optional booking/brochure)  17. if booking: asyncio.create_task(booking.save_booking(...))       → appends bookings.jsonl  18. if brochure: asyncio.create_task(booking.send_brochure(...))     → POST Vobiz WhatsApp API  19. await self._speak(clean)                        → sets _is_speaking = True  20. SarvamTTS.synthesize(clean)                      → POST api.sarvam.ai/text-to-speech  [sarvam_tts.py]  21. WAV parsed, header stripped, audio.pcm16_to_vobiz_frames() → 8kHz mu-law, 160B frames  22. for frame in frames: send {"event":"playAudio", media:{payload: base64(frame)}}, sleep(0.02)  23. steps 15–22 repeat for each subsequent clause while the LLM keeps streaming  24. stream ends → if buf remains, yield final clause (same 16–22 path)  25. finally: messages.append({"role":"assistant","content":full_reply})   → conversation memory updated  26. self._is_speaking = False; a "checkpoint" event sent for this turn  27. (concurrently) Vobiz emits "playedStream"/"clearedAudio" WS events and a /stream-status webhook,       both only logged — nothing in-process consumes them except _on_media's is_speaking gate  28. call ends: Vobiz "stop" event or WS disconnect → session.cleanup() → cancel timers/tasks, close STT WS  29. separately: Vobiz POST /hangup                  → logged, no further action`

19 / 20

Code health
-----------

**SeverityFindingCritical.env is tracked in git** — committed in bb7c75f (Initial Commit) and still tracked/modified today. .gitignore was added afterward and only lists .env, which does not retroactively remove it from history. Live SARVAM\_API\_KEY, VOBIZ\_AUTH\_TOKEN, DEEPGRAM\_API\_KEY, and any LLM key are exposed to anyone with repo access or clone history.**CriticalNo authentication on any HTTP or WS route** — /answer, /hangup, /stream-status, and /ws accept unsigned requests from anyone who knows PUBLIC\_HOST, exposing the LLM/TTS pipeline to abuse and cost drain.**Naming / docs mismatchsarvam\_stt.py is a Deepgram client**, not Sarvam. The filename, class name (SarvamSTT), and every Sarvam-branded config var (STT\_MODEL, STT\_LANGUAGE, STT\_ENCODING) are dead — the real client hardcodes its own model/language/encoding in a Deepgram URL and reads DEEPGRAM\_API\_KEY directly.**Docs mismatchTTS is not streaming** despite the README architecture table calling it "Sarvam Bulbul v3 (streaming WS)." sarvam\_tts.py makes one blocking REST POST per clause and waits for the complete WAV.**Non-functional stubBROCHURE\_URL** in booking.py is a hardcoded placeholder (your-cdn.example.com) — every brochure send is broken as shipped.**Dead code**SarvamSTT.send\_pcm16() and .flush() are no-op stubs, never called. config.\_get() is defined but never used — every env lookup uses the non-failing os.getenv path instead, so "required" credentials silently default to empty strings rather than failing fast at startup.**Dead configuration**LLM\_NUM\_CTX, TTS\_SRC\_RATE, and THINKING\_FILLER are all defined in config.py and documented in the README's tuning guide, but none are actually read/used in the current code paths (TTS re-derives the sample rate from the WAV header at runtime; the thinking-filler sound is never played).**Unused dependency**sarvamai SDK is pinned in requirements.txt but never imported — both Sarvam integrations (STT-that's-actually-Deepgram, and TTS) are hand-rolled over raw websockets/httpx instead of the vendor SDK.**Repo hygiene**Eight compiled \_\_pycache\_\_/\*.pyc files are tracked in git. Eight ad hoc agent\*.log transcript files sit untracked at the repo root, one of which recorded a live ngrok URL and evidence of a real backend swap to Gemini.**Untested claim**README calls audio.py "unit-tested"; audio.py's own docstring says it's "easy to unit test." No test file exists anywhere in the repository.**Observed production drift**Log evidence (agentd.log, 2026-07-01) shows real calls run against Google Gemini's OpenAI-compatible endpoint rather than the documented local-Ollama default — and hitting both a 429 free-tier quota error and a 400 "Unknown name 'options'" schema-rejection error mid-call, meaning at least one real call went silent partway through.Incompletebookings.jsonl has never been created — no booking has been completed end-to-end in any logged run to date.

No experimental/scratch modules, no duplicate implementations of the same logic, and no half-migrated abstractions were found — the codebase's honesty problem is entirely in _naming and README claims drifting from a still-small, still-readable implementation_, not in structural rot.

20 / 20

Executive summary
-----------------

#### Subsystem ratings (1–10)

**Architecture**7**Project organization**5**Scalability**4**Reliability**4**Maintainability**6**Observability**2**Security**2**Performance**7**Extensibility**6**Developer experience**6**Testing**1**Deployment readiness**3

### 1\. What currently exists

A working, single-process, single-tenant voice agent that genuinely handles the hard real-time problems of telephony AI — mu-law codec math, VAD-gated barge-in, silence-based turn endpointing, and clause-level pipelining between a streaming LLM and TTS — in under a thousand lines. It places and answers real Vobiz calls, transcribes and responds in Hinglish, and logs a full conversation trail. It has, on the evidence in the log files, been run against real phone numbers and a real (if quota-limited) LLM backend.

### 2\. What appears incomplete

Everything past the voice loop itself: persistence (one unused JSONL file), the brochure delivery tool (points at a fake URL), authentication (none), retries/fallbacks (none), observability beyond stdout logs (none), and packaging for repeatable deployment (no Docker, no CI, no process manager). Several config knobs and one entire provider integration (Sarvam STT) are vestigial — present in name and documentation but not in the executed code path.

### 3\. Modules central to the runtime

session.py is the load-bearing wall — every other module exists to serve it. server.py, audio.py, llm.py, sarvam\_tts.py, and sarvam\_stt.py (as a Deepgram client) are all on the hot path of every call.

### 4\. Modules that are peripheral

booking.py and make\_call.py sit off the real-time path — booking is fire-and-forget and non-blocking, and the outbound dialer is a CLI a human runs, not code the server ever imports.

### 5\. Architecture in one paragraph

One FastAPI process terminates a Vobiz WebSocket per call and runs a hand-built, fully custom orchestration loop — no Pipecat, no LiveKit, no managed conversational framework — that bridges caller audio to Deepgram STT, a streaming OpenAI-compatible LLM, and Sarvam TTS, with an inline text-marker protocol standing in for tool-calling and a flat JSONL file standing in for a CRM. The engineering investment is concentrated almost entirely in shaving latency out of that single loop; almost none of it has gone into the operational scaffolding — auth, persistence, retries, observability, deployment — that would be needed to run this as a real multi-tenant production service rather than one operator's live-tested prototype.

Northern Heights Voice Runtime — reverse-engineered from source, no code modified during this audit.