"""DeepgramSTT reconnect-with-backoff (M8, defect D5).

Drives the adapter with a scripted fake `websockets` module and compressed
backoff constants: initial-connect failure recovers in the background,
mid-call socket death reconnects, an exhausted budget reports "dead", and
close() cancels recovery cleanly.
"""
import asyncio
from types import SimpleNamespace

import pytest

from providers.stt import deepgram
from runtime.types import STTEvent


class FakeWS:
    def __init__(self):
        self.sent = []
        self._ended = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._ended.wait()
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self._ended.set()

    def kill(self):
        """Server-side death: the read loop ends while the call is live."""
        self._ended.set()


@pytest.fixture
def fast_backoff(monkeypatch):
    monkeypatch.setattr(deepgram, "RECONNECT_BASE_DELAY_S", 0.001)
    monkeypatch.setattr(deepgram, "RECONNECT_MAX_DELAY_S", 0.001)


def make_stt(monkeypatch, connect_script):
    """connect_script: list of FakeWS instances or Exceptions, consumed per
    connect attempt; exhausting it keeps raising."""
    attempts = []

    async def fake_connect(url, additional_headers=None):
        attempts.append(url)
        step = (connect_script.pop(0) if connect_script
                else ConnectionError("refused"))
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(deepgram, "websockets",
                        SimpleNamespace(connect=fake_connect))
    events = []

    async def on_event(ev: STTEvent):
        events.append(ev)

    stt = deepgram.DeepgramSTT(api_key="k", on_event=on_event)
    return stt, events, attempts


async def test_initial_connect_failure_recovers_in_background(
        monkeypatch, fast_backoff):
    ws = FakeWS()
    stt, events, attempts = make_stt(
        monkeypatch, [ConnectionError("refused"), ws])

    await stt.start()          # returns immediately — greeting must not wait
    assert stt._ws is None
    await asyncio.sleep(0.05)  # background reconnect lands

    assert stt._ws is ws
    assert len(attempts) == 2
    assert events == []        # no alarm: the call recovered
    await stt.close()


async def test_midcall_death_reconnects(monkeypatch, fast_backoff):
    ws1, ws2 = FakeWS(), FakeWS()
    stt, events, attempts = make_stt(monkeypatch, [ws1, ws2])

    await stt.start()
    assert stt._ws is ws1
    ws1.kill()                 # Deepgram drops the socket mid-call
    await asyncio.sleep(0.05)

    assert stt._ws is ws2
    assert len(attempts) == 2
    assert events == []
    await stt.close()


async def test_exhausted_budget_reports_dead(monkeypatch, fast_backoff):
    stt, events, attempts = make_stt(monkeypatch, [])  # every attempt fails

    await stt.start()
    await asyncio.sleep(0.2)   # all reconnect attempts elapse

    assert len(attempts) == 1 + deepgram.RECONNECT_ATTEMPTS
    assert events == [STTEvent(kind="dead", text="")]
    await stt.close()


async def test_close_cancels_reconnect_without_alarm(monkeypatch, fast_backoff):
    stt, events, attempts = make_stt(monkeypatch, [])

    await stt.start()
    await stt.close()          # caller hung up while STT was down
    await asyncio.sleep(0.05)

    assert events == []        # no dead alarm after an intentional close


async def test_frames_dropped_while_down_flow_after_reconnect(
        monkeypatch, fast_backoff):
    ws = FakeWS()
    stt, events, attempts = make_stt(
        monkeypatch, [ConnectionError("refused"), ws])
    frame = SimpleNamespace(payload=b"\x00" * 160)

    await stt.start()
    await stt.send_audio(frame)   # down: dropped, no crash
    await asyncio.sleep(0.05)     # reconnected
    await stt.send_audio(frame)

    assert ws.sent == [frame.payload]
    await stt.close()
