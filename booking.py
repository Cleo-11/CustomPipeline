"""
booking.py — Capture appointments and send the brochure on WhatsApp.

Bookings are appended to a JSONL file (swap for Postgres/your CRM in prod).
Brochure delivery uses Vobiz's WhatsApp Business API. Both are best-effort
and never block the voice loop — the session runs them through its tool
runner (fire-and-forget task + ToolCalled/Succeeded/Failed audit events).

Since M6, failures RAISE instead of being swallowed here: the tool runner
owns the catch, so every failure becomes a ToolFailed event on the bus.
This module dissolves into Priya's registered tools in M7.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

import config

log = logging.getLogger("booking")

_STORE = Path("bookings.jsonl")
BROCHURE_URL = "https://your-cdn.example.com/northern-heights-brochure.pdf"


def _append_line(record: dict) -> None:
    with _STORE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def save_booking(call_id: str, caller: str, booking: dict) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "call_id": call_id,
        "caller": caller,
        **booking,
    }
    # D8: the file write runs in a worker thread, never on the event loop.
    await asyncio.to_thread(_append_line, record)
    log.info("Booking saved: %s", record)


async def send_brochure(to_number: str) -> None:
    """Send the project brochure to the caller's WhatsApp via Vobiz."""
    url = f"{config.VOBIZ_API_BASE}/Account/{config.VOBIZ_AUTH_ID}/whatsapp/messages"
    headers = {
        "Content-Type": "application/json",
        "X-Auth-ID": config.VOBIZ_AUTH_ID,
        "X-Auth-Token": config.VOBIZ_AUTH_TOKEN,
    }
    payload = {
        "to": to_number,
        "type": "document",
        "document": {
            "link": BROCHURE_URL,
            "filename": "Northern-Heights-Brochure.pdf",
            "caption": "Northern Heights, Dahisar East — N Rose Developers",
        },
    }
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=payload, headers=headers)
        log.info("Brochure -> %s (HTTP %s)", to_number, r.status_code)
        r.raise_for_status()
