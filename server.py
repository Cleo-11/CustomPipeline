"""
server.py — Single production server.

Exposes:
  POST /answer        -> returns <Stream> XML telling Vobiz to open a WS to /ws
  POST /hangup        -> call-ended webhook (logging / CRM hook)
  POST /stream-status -> stream lifecycle events
  GET  /health        -> health + the WS url Vobiz should use
  WS   /ws            -> the bidirectional audio stream; one CallSession each

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
import json
import logging

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

import config
from session import CallSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("server")

app = FastAPI(title="Northern Heights Voice Agent")


@app.post("/answer")
async def answer(request: Request):
    form = await request.form()
    log.info("answer webhook: From=%s To=%s Dir=%s",
             form.get("From"), form.get("To"), form.get("Direction"))
    ws_url = f"wss://{config.PUBLIC_HOST}/ws"
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
async def health():
    return {"status": "ok", "ws_url": f"wss://{config.PUBLIC_HOST}/ws"}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    log.info("Vobiz WebSocket connected")

    async def send_json(obj: dict):
        await websocket.send_text(json.dumps(obj))

    session = CallSession(send_json)
    try:
        while True:
            raw = await websocket.receive_text()
            await session.handle_event(raw)
    except WebSocketDisconnect:
        log.info("Vobiz WebSocket disconnected")
    except Exception as e:  # noqa: BLE001
        log.error("WS error: %s", e)
    finally:
        await session.cleanup()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
