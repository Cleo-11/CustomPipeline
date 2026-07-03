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
