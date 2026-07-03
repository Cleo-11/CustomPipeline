"""Scripted-call characterization tests for CallSession.

Drives the orchestrator end-to-end and asserts the outbound Vobiz event
sequence. Since M2, fakes implement the runtime.interfaces Protocols and
are injected through the constructor — no module monkeypatching. This is
the seed of the M4 replay harness.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import numpy as np
import pytest

import audio
import booking
import config
from runtime.types import MULAW_8K, AudioFrame, LLMDelta, STTEvent
from session import CallSession


# ---------------------------------------------------------------------- fakes
class FakeSTT:
    emits_endpoint = True

    def __init__(self, on_event):
        self.on_event = on_event
        self.frames = []
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def send_audio(self, frame):
        self.frames.append(frame.payload)

    async def close(self):
        self.closed = True


class FakeTTS:
    supports_streaming_input = False
    N_FRAMES = 3

    def __init__(self):
        self.texts = []

    async def synthesize(self, text, fmt):
        assert fmt == MULAW_8K
        self.texts.append(text)
        for _ in range(self.N_FRAMES):
            yield AudioFrame(payload=b"\xff" * audio.FRAME_BYTES, format=MULAW_8K)


class FakeLLM:
    """Yields scripted delta streams, one list per user turn."""

    def __init__(self):
        self.replies: list[list[str]] = []

    async def stream(self, messages):
        for part in self.replies.pop(0):
            yield LLMDelta(text=part)


# -------------------------------------------------------------------- helpers
START_EVENT = json.dumps(
    {"event": "start", "streamId": "s1", "callId": "c1", "from": "+911234567890"}
)

LOUD_ULAW = audio.pcm16_to_ulaw(np.full(160, 8000, dtype=np.int16))


def media_event(ulaw: bytes) -> str:
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(ulaw).decode("ascii")}}
    )


def events_of(sent, kind):
    return [e for e in sent if e.get("event") == kind]


@pytest.fixture
def sess(monkeypatch):
    monkeypatch.setattr(config, "ENDPOINT_SILENCE_MS", 20)
    monkeypatch.setattr(config, "BARGEIN_MIN_FRAMES", 3)
    sent: list[dict] = []

    async def send_json(obj):
        sent.append(obj)

    s = CallSession(send_json, stt_factory=FakeSTT, tts=FakeTTS(), llm=FakeLLM())
    # Deterministic VAD: always classify audio as speech
    s._vad = SimpleNamespace(is_speech=lambda pcm_bytes, rate: True)
    s.sent = sent
    return s


async def run_user_turn(s, text):
    """Deliver an STT final and wait for the endpoint + full reply."""
    await s.stt.on_event(STTEvent(kind="final", text=text))
    await asyncio.sleep(0.1)  # > patched ENDPOINT_SILENCE_MS
    assert s._speak_task is not None
    await s._speak_task


# ---------------------------------------------------------------------- tests
async def test_greeting_plays_on_start(sess):
    await sess.handle_event(START_EVENT)
    await sess._speak_task

    plays = events_of(sess.sent, "playAudio")
    checkpoints = events_of(sess.sent, "checkpoint")
    assert len(plays) == FakeTTS.N_FRAMES
    assert len(checkpoints) == 1
    assert checkpoints[0]["streamId"] == "s1"
    # Frames are mu-law 8k, base64-encoded
    payload = base64.b64decode(plays[0]["media"]["payload"])
    assert len(payload) == audio.FRAME_BYTES
    assert plays[0]["media"]["contentType"] == "audio/x-mulaw"
    # Greeting is recorded in history; STT is running
    assert sess.messages[1] == {"role": "assistant", "content": config.GREETING}
    assert sess.stt.started


async def test_normal_turn_event_sequence(sess):
    # First delta ends with a danda past MIN_FIRST_CHUNK so chunking splits
    # the reply into two spoken clauses, exactly as with a real token stream.
    first_clause = "प" * 125 + "।"
    sess._llm.replies = [[first_clause, " दूसरा वाक्य।"]]
    await sess.handle_event(START_EVENT)
    await sess._speak_task
    n_greeting_events = len(sess.sent)

    await run_user_turn(sess, "mujhe 2BHK chahiye")

    turn_events = sess.sent[n_greeting_events:]
    # Current behavior: each clause is spoken separately, each followed by
    # its own checkpoint — 3 frames + 1 checkpoint, twice.
    kinds = [e["event"] for e in turn_events]
    assert kinds == ["playAudio"] * 3 + ["checkpoint"] + ["playAudio"] * 3 + ["checkpoint"]
    assert sess.tts.texts[1:] == [first_clause, "दूसरा वाक्य।"]
    # History gained the user turn and the joined assistant reply
    assert sess.messages[-2] == {"role": "user", "content": "mujhe 2BHK chahiye"}
    assert sess.messages[-1] == {
        "role": "assistant",
        "content": first_clause + " दूसरा वाक्य।",
    }


async def test_media_during_greeting_never_barges_in(sess):
    await sess.handle_event(START_EVENT)
    # Greeting task is scheduled; loud speech arrives while it plays
    for _ in range(5):
        await sess.handle_event(media_event(LOUD_ULAW))
    await sess._speak_task

    assert events_of(sess.sent, "clearAudio") == []
    # Audio was still forwarded to STT continuously
    assert len(sess.stt.frames) == 5


async def test_sustained_loud_speech_triggers_clear_audio(sess):
    sess._llm.replies = [["एक वाक्य।"]]
    await sess.handle_event(START_EVENT)
    await sess._speak_task
    await run_user_turn(sess, "haan boliye")

    # Send-complete: _is_speaking is False (playback tail, D6), so the
    # RMS+VAD barge-in path is active. BARGEIN_MIN_FRAMES is patched to 3.
    for _ in range(3):
        await sess.handle_event(media_event(LOUD_ULAW))

    clears = events_of(sess.sent, "clearAudio")
    assert len(clears) == 1
    assert clears[0]["streamId"] == "s1"


async def test_booking_and_brochure_markers_dispatch(sess, monkeypatch):
    sess._llm.replies = [
        ["ठीक है, book कर देती हूं। [[BOOK day=Sunday time=4pm name=Rahul]] [[BROCHURE]]"]
    ]
    saved, brochures = [], []

    async def fake_save(call_id, caller, bk):
        saved.append((call_id, caller, bk))

    async def fake_brochure(number):
        brochures.append(number)

    monkeypatch.setattr(booking, "save_booking", fake_save)
    monkeypatch.setattr(booking, "send_brochure", fake_brochure)

    await sess.handle_event(START_EVENT)
    await sess._speak_task
    await run_user_turn(sess, "Sunday 4 baje aaunga, Rahul bol raha hoon")
    await asyncio.sleep(0)  # let the fire-and-forget booking tasks run

    assert saved == [("c1", "unknown", {"day": "Sunday", "time": "4pm", "name": "Rahul"})]
    assert brochures == ["+911234567890"]
    # Markers never reach TTS
    assert all("[[" not in t for t in sess.tts.texts)


async def test_stop_event_cleans_up(sess):
    await sess.handle_event(START_EVENT)
    await sess._speak_task
    await sess.handle_event(json.dumps({"event": "stop"}))
    assert sess.stt.closed
