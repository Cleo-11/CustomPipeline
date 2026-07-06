"""Route tests for server.py — webhook shape and /ws authentication."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import config
import server

client = TestClient(server.app)


def test_health_is_ok_and_leaks_no_ws_url():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


class _Probe:
    def __init__(self, ok):
        self._ok = ok

    async def healthy(self):
        return self._ok


def _fake_providers(monkeypatch, *, llm=True, tts=True, stt=True):
    fake = server._Providers(
        stt_factory=lambda on_event: _Probe(stt),
        tts=_Probe(tts), llm=_Probe(llm),
        tool_strategy=server.MarkerToolStrategy(()))
    monkeypatch.setattr(server, "_build_providers", lambda agent: fake)


def test_health_deep_all_ok(monkeypatch):
    _fake_providers(monkeypatch)
    r = client.get("/health?deep=true")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "providers": {"llm": True, "tts": True, "stt": True},
    }


def test_health_deep_degraded_when_a_provider_fails(monkeypatch):
    _fake_providers(monkeypatch, llm=False)
    body = client.get("/health?deep=true").json()
    assert body["status"] == "degraded"
    assert body["providers"]["llm"] is False
    assert body["providers"]["tts"] is True


def test_metrics_endpoint_renders_prometheus_text():
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "# TYPE calls_total counter" in r.text
    assert "# TYPE turn_first_audio_seconds histogram" in r.text
    assert 'bargein_reaction_seconds_bucket{le="+Inf"}' in r.text


def test_answer_returns_stream_xml_with_tokened_ws_url():
    r = client.post("/answer", data={"From": "+911234567890", "To": "+911100000000"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert f"wss://{config.PUBLIC_HOST}/ws?token={config.WS_AUTH_TOKEN}" in r.text
    assert 'bidirectional="true"' in r.text


def test_ws_rejects_missing_token():
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_ws_rejects_bad_token():
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong"):
            pass


def test_ws_accepts_valid_token():
    with client.websocket_connect(f"/ws?token={config.WS_AUTH_TOKEN}"):
        pass
