"""runtime/sinks.py — observability sinks: structured event log, transcript
JSONL, JSON log formatter.

Purpose
    Where bus events land. Every sink here is a pure observer — removing
    any of them changes nothing about a call. Business actions (bookings,
    notifications) do NOT belong here; they are tools (M7).

File writes go through asyncio.to_thread so a slow disk never stalls the
event loop (the D8 pattern). Sinks run on the bus drain task, which is
already off the audio hot path; to_thread keeps them off the *loop* too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from runtime import events

log = logging.getLogger("runtime.sinks")


class EventLogSubscriber:
    """One structured JSON log line per bus event — the correlation layer:
    every line carries call_id (and turn_seq where the event has one)."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("events")

    async def __call__(self, event: events.Event) -> None:
        payload = {"event": type(event).__name__, **asdict(event)}
        self._log.info("%s", json.dumps(payload, ensure_ascii=False))


class TranscriptWriter:
    """Appends call lifecycle + per-turn transcript records as JSONL.

    Subscribes to CallStarted / TurnCompleted / CallEnded only — the
    minimal set that reconstructs what was said and how fast. Swap for a
    database subscriber in M11 without touching the runtime.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def __call__(self, event: events.Event) -> None:
        if not isinstance(event,
                          (events.CallStarted, events.TurnCompleted,
                           events.CallEnded)):
            return
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": type(event).__name__,
            **asdict(event),
        }
        line = json.dumps(record, ensure_ascii=False)
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class JsonFormatter(logging.Formatter):
    """Whole-process JSON log lines (LOG_FORMAT=json). Keeps the text
    format as the dev default; prod log pipelines get one shape."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)
