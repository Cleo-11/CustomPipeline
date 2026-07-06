"""
make_call.py — Place an outbound call through Vobiz.

  python make_call.py --to +9198XXXXXXXX

Vobiz dials `to`, and when answered hits your /answer webhook, which returns the
<Stream> XML that connects the call to this agent. For bulk dialling, Vobiz
accepts up to 1000 comma-separated destinations in one request.
"""
from __future__ import annotations
import argparse
import os
import sys

import httpx

import config

FROM_NUMBER = os.getenv("FROM_NUMBER", "")


def make_call(to: str, from_: str, agent: str | None = None) -> None:
    if not (config.VOBIZ_AUTH_ID and config.VOBIZ_AUTH_TOKEN):
        sys.exit("Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN in .env")
    url = f"{config.VOBIZ_API_BASE}/Account/{config.VOBIZ_AUTH_ID}/Call/"
    headers = {
        "Content-Type": "application/json",
        "X-Auth-ID": config.VOBIZ_AUTH_ID,
        "X-Auth-Token": config.VOBIZ_AUTH_TOKEN,
    }
    # All webhook routes require the shared secret since M8 (D10).
    answer_url = f"https://{config.PUBLIC_HOST}/answer?token={config.WS_AUTH_TOKEN}"
    if agent:
        answer_url += f"&agent={agent}"
    payload = {
        "from": from_,
        "to": to,
        "answer_url": answer_url,
        "answer_method": "POST",
        "hangup_url": f"https://{config.PUBLIC_HOST}/hangup?token={config.WS_AUTH_TOKEN}",
        "hangup_method": "POST",
    }
    r = httpx.post(url, json=payload, headers=headers, timeout=20)
    print("HTTP", r.status_code)
    print(r.text)
    r.raise_for_status()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--to", required=True, help="Destination, e.g. +9198XXXXXXXX")
    p.add_argument("--from", dest="from_", default=FROM_NUMBER, help="Your Vobiz DID")
    p.add_argument("--agent", default=None,
                   help="Agent id to run the call as (default: the server's default agent)")
    a = p.parse_args()
    if not a.from_:
        sys.exit("Provide --from or set FROM_NUMBER in .env")
    make_call(a.to, a.from_, a.agent)
