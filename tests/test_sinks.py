"""Sink tests: transcript JSONL filtering/shape, structured event log,
JSON log formatter."""
import json
import logging

from runtime import events
from runtime.sinks import EventLogSubscriber, JsonFormatter, TranscriptWriter


async def test_transcript_writer_records_lifecycle_and_turns(tmp_path):
    path = tmp_path / "transcripts.jsonl"
    writer = TranscriptWriter(path)

    await writer(events.CallStarted(call_id="c1", caller="+91", agent_id="priya"))
    await writer(events.ThinkingStarted(call_id="c1", turn_seq=1))  # filtered out
    await writer(events.TurnCompleted(
        call_id="c1", turn_seq=1, user_text="2BHK?", agent_text="जी हां।",
        thinking_s=0.4, first_audio_s=0.6, interrupted=False))
    await writer(events.CallEnded(call_id="c1"))

    lines = [json.loads(line) for line in
             path.read_text(encoding="utf-8").splitlines()]
    assert [rec["event"] for rec in lines] == [
        "CallStarted", "TurnCompleted", "CallEnded"]
    turn = lines[1]
    assert turn["call_id"] == "c1"
    assert turn["agent_text"] == "जी हां।"   # Devanagari survives (no \u escapes)
    assert turn["thinking_s"] == 0.4
    assert all("ts" in rec for rec in lines)


async def test_event_log_subscriber_emits_parseable_json(caplog):
    sub = EventLogSubscriber()
    with caplog.at_level(logging.INFO, logger="events"):
        await sub(events.SpeechStarted(call_id="c9", turn_seq=3))
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload == {"event": "SpeechStarted", "call_id": "c9", "turn_seq": 3}


def test_json_formatter_shapes_a_record():
    record = logging.LogRecord(
        name="session", level=logging.INFO, pathname=__file__, lineno=1,
        msg="USER: %s", args=("hello",), exc_info=None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "session"
    assert payload["msg"] == "USER: hello"
    assert "ts" in payload
