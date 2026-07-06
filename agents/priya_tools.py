"""agents/priya_tools.py — Priya's business tools (successor to booking.py).

Site-visit booking and WhatsApp brochure delivery are N Rose Developers'
business logic, not runtime code (M7). The runtime executes these through
the ToolRegistry/ToolExecutor and knows nothing about real estate; per-
agent knobs (bookings path, brochure URL/filename/caption) come from the
agent record's tool_config, so the same handlers serve any future agent.

NOTE: the brochure URL in agents/priya.json is still a placeholder domain —
the send works only once a real PDF URL is configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

import config
from runtime.tools import ToolContext, ToolRegistry, ToolSpec

log = logging.getLogger("agents.priya")


# ------------------------------------------------------------------ booking
def _append_line(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def book_site_visit(ctx: ToolContext, args: dict) -> None:
    """Append the appointment to the bookings JSONL (swap for a CRM in
    prod). File I/O runs in a worker thread, never on the event loop."""
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "call_id": ctx.call_id,
        "caller": ctx.caller_name,
        **args,
    }
    path = Path(ctx.agent.tool_config.get("bookings_path", "bookings.jsonl"))
    await asyncio.to_thread(_append_line, path, record)
    log.info("Booking saved: %s", record)


# ----------------------------------------------------------------- brochure
def _brochure_payload(to_number: str, cfg: dict) -> dict:
    return {
        "to": to_number,
        "type": "document",
        "document": {
            "link": cfg["brochure_url"],
            "filename": cfg.get("brochure_filename", "brochure.pdf"),
            "caption": cfg.get("brochure_caption", ""),
        },
    }


async def send_brochure(ctx: ToolContext, args: dict) -> None:
    """Send the project brochure to the caller's WhatsApp via Vobiz.
    Failures raise — the ToolExecutor owns retry and the ToolFailed event."""
    url = f"{config.VOBIZ_API_BASE}/Account/{config.VOBIZ_AUTH_ID}/whatsapp/messages"
    headers = {
        "Content-Type": "application/json",
        "X-Auth-ID": config.VOBIZ_AUTH_ID,
        "X-Auth-Token": config.VOBIZ_AUTH_TOKEN,
    }
    payload = _brochure_payload(ctx.caller_number, ctx.agent.tool_config)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=payload, headers=headers)
        log.info("Brochure -> %s (HTTP %s)", ctx.caller_number, r.status_code)
        r.raise_for_status()


# ------------------------------------------------------------- registration
def register(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        name="book_site_visit",
        description="Book a site-visit appointment for the caller at "
                    "Northern Heights.",
        parameters={
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "Day of the visit"},
                "time": {"type": "string", "description": "Time of the visit"},
                "name": {"type": "string", "description": "Caller's name"},
            },
            "required": ["day", "time", "name"],
        },
        handler=book_site_visit,
        owner="n-rose-developers",
        marker="BOOK",
        timeout_s=5.0,
        retries=1,
    ))
    registry.register(ToolSpec(
        name="send_brochure",
        description="Send the Northern Heights brochure to the caller's "
                    "WhatsApp.",
        parameters={"type": "object", "properties": {}},
        handler=send_brochure,
        owner="n-rose-developers",
        marker="BROCHURE",
        timeout_s=10.0,
        retries=1,
    ))
