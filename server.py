"""
server.py — Single production server.

Exposes:
  POST /answer        -> returns <Stream> XML telling Vobiz to open a WS to /ws
  POST /hangup        -> call-ended webhook (logging / CRM hook)
  POST /stream-status -> stream lifecycle events
  GET  /health        -> liveness check; ?deep=true probes provider health
  GET  /metrics       -> Prometheus text exposition (per-turn latency, counts)
  WS   /ws            -> the bidirectional audio stream; one CallSession each.
                         Requires ?token=<WS_AUTH_TOKEN>, which /answer embeds
                         in the URL it hands to Vobiz.

Production note: this is ONE server with a native WebSocket route — no internal
localhost proxy hop (the dev reference uses two servers + ngrok; that extra hop
adds latency you don't want in prod). Put this behind TLS on a real domain and
set PUBLIC_HOST to that domain. For local testing, run ngrok and set
PUBLIC_HOST to the ngrok host.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1
(scale by running more single-worker processes behind a load balancer; each
call pins one event loop)
"""
from __future__ import annotations
import asyncio
import logging
import secrets
from dataclasses import dataclass

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response

import config
from agents import priya_tools
from providers.llm.openai_compat import OpenAICompatLLM
from providers.stt.deepgram import DeepgramSTT
from providers.tts.sarvam import SarvamTTS
from runtime.agent import AgentConfig
from runtime.agent_registry import resolve as resolve_agent
from runtime.events import EventBus
from runtime.interfaces import LLM, TTS, OnSTTEvent, STTFactory, SupportsHealth
from runtime.metrics import MetricsRegistry, TurnMetrics
from runtime.sinks import EventLogSubscriber, JsonFormatter, TranscriptWriter
from runtime.tools import (
    MarkerToolStrategy,
    NativeToolStrategy,
    ToolDispatchStrategy,
    ToolExecutor,
    ToolRegistry,
)
from runtime.types import STTEvent
from session import CallSession
from transports.vobiz import VobizTransport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
if config.LOG_FORMAT == "json":
    for _handler in logging.getLogger().handlers:
        _handler.setFormatter(JsonFormatter())
log = logging.getLogger("server")

app = FastAPI(title="Northern Heights Voice Agent")

# ---------------------------------------------------------------------------
# Observability wiring (M6): one process-wide bus; sinks subscribe here at
# the composition root, never inside runtime modules. Every subscriber is
# a pure observer — removing any of them changes nothing about a call.
# ---------------------------------------------------------------------------
BUS = EventBus()
METRICS = MetricsRegistry()
BUS.subscribe(EventLogSubscriber())
BUS.subscribe(TurnMetrics(METRICS))
BUS.subscribe(TranscriptWriter(config.TRANSCRIPTS_PATH))

# Tool wiring (M7): agents' tool modules register their specs here; agents
# reference tools by name in their records. Dynamic loading of tool modules
# from agent specs is the P3 plugin SDK — until then registration is an
# explicit line at this composition root.
TOOL_REGISTRY = ToolRegistry()
priya_tools.register(TOOL_REGISTRY)
TOOL_EXECUTOR = ToolExecutor(TOOL_REGISTRY, BUS)


# ---------------------------------------------------------------------------
# Composition root — the only place vendor names, agent policy, and secrets
# meet. Providers are built from the resolved AgentConfig's policy; secrets
# come from config (per-tenant secret resolution arrives with tenancy, P3).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Providers:
    stt_factory: STTFactory
    tts: TTS
    llm: LLM
    tool_strategy: ToolDispatchStrategy


# One provider set per agent, built lazily and reused across that agent's
# calls (LLM/TTS hold connection pools worth keeping warm; STT is still
# per-call via the factory). In tenancy this key becomes (tenant, agent).
_provider_cache: dict[str, _Providers] = {}


def _build_providers(agent: AgentConfig) -> _Providers:
    cached = _provider_cache.get(agent.agent_id)
    if cached is not None:
        return cached
    llm = OpenAICompatLLM(
        base_url=agent.llm.base_url,
        api_key=config.LLM_API_KEY,
        model=agent.llm.model,
        temperature=agent.llm.temperature,
        max_tokens=agent.llm.max_tokens,
    )
    tts = SarvamTTS(
        api_key=config.SARVAM_API_KEY,
        model=agent.voice.model,
        speaker=agent.voice.speaker,
        language=agent.voice.language,
        pace=agent.voice.pace,
    )

    def stt_factory(on_event: OnSTTEvent) -> DeepgramSTT:
        return DeepgramSTT(
            api_key=config.DEEPGRAM_API_KEY,
            on_event=on_event,
            model=agent.stt.model,
            language=agent.stt.language,
        )

    # Per-agent tool dispatch: the agent's tool names resolve to specs, and
    # its llm.tool_dispatch policy picks the strategy.
    specs = TOOL_REGISTRY.resolve(agent.tools)
    strategy: ToolDispatchStrategy
    if agent.llm.tool_dispatch == "native":
        strategy = NativeToolStrategy(specs)
    else:
        strategy = MarkerToolStrategy(specs)

    providers = _Providers(stt_factory=stt_factory, tts=tts, llm=llm,
                           tool_strategy=strategy)
    _provider_cache[agent.agent_id] = providers
    return providers


@app.post("/answer")
async def answer(request: Request):
    form = await request.form()
    log.info("answer webhook: From=%s To=%s Dir=%s",
             form.get("From"), form.get("To"), form.get("Direction"))
    # Agent selection rides through our own URLs, so it doesn't depend on the
    # carrier echoing custom params: make_call adds ?agent=, we forward it to
    # /ws, which resolves it. Absent → the default agent.
    agent_id = request.query_params.get("agent")
    ws_url = f"wss://{config.PUBLIC_HOST}/ws?token={config.WS_AUTH_TOKEN}"
    if agent_id:
        ws_url += f"&agent={agent_id}"
    status_url = f"https://{config.PUBLIC_HOST}/stream-status"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Stream bidirectional="true" keepCallAlive="true" '
        f'contentType="audio/x-mulaw;rate=8000" '
        f'statusCallbackUrl="{status_url}" statusCallbackMethod="POST">'
        f"{ws_url}"
        "</Stream>"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")

@app.post("/hangup")
async def hangup(request: Request):
    form = await request.form()
    log.info("hangup: UUID=%s Duration=%ss Cause=%s",
             form.get("CallUUID"), form.get("Duration"), form.get("HangupCause"))
    return PlainTextResponse("OK")


@app.post("/stream-status")
async def stream_status(request: Request):
    form = await request.form()
    log.info("stream-status: %s", dict(form))
    return PlainTextResponse("OK")


@app.get("/health")
async def health(deep: bool = False):
    # The tokened WS URL is a secret; only Vobiz (via /answer) gets it.
    if not deep:
        return {"status": "ok"}
    # Deep mode probes provider reachability with the default agent's
    # provider set. Probes are concurrent and individually time-boxed so a
    # hung vendor can't hang the health check.
    providers = _build_providers(resolve_agent())

    async def _ignore(ev: STTEvent) -> None:
        return

    probes: dict[str, object] = {
        "llm": providers.llm,
        "tts": providers.tts,
        "stt": providers.stt_factory(_ignore),
    }

    async def _probe(obj: object) -> bool | None:
        if not isinstance(obj, SupportsHealth):
            return None  # adapter offers no probe
        try:
            return await asyncio.wait_for(obj.healthy(), timeout=5.0)
        except Exception:  # noqa: BLE001
            return False

    results = dict(zip(probes, await asyncio.gather(*map(_probe, probes.values()))))
    ok = all(v is not False for v in results.values())
    return {"status": "ok" if ok else "degraded", "providers": results}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(
        METRICS.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if not secrets.compare_digest(token, config.WS_AUTH_TOKEN):
        log.warning("Rejected /ws connect: bad or missing token")
        # Closing before accept() makes the ASGI server reject the handshake.
        await websocket.close(code=1008)
        return
    await websocket.accept()

    agent = resolve_agent(agent_id=websocket.query_params.get("agent"))
    providers = _build_providers(agent)
    log.info("Vobiz WebSocket connected (agent=%s)", agent.agent_id)

    transport = VobizTransport(websocket)
    session = CallSession(
        transport, agent=agent,
        stt_factory=providers.stt_factory, tts=providers.tts, llm=providers.llm,
        bus=BUS,
        tool_strategy=providers.tool_strategy, tool_executor=TOOL_EXECUTOR,
    )
    try:
        await session.run()
    except Exception as e:  # noqa: BLE001
        log.error("WS session error: %s", e)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
