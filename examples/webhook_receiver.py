"""Minimal webhook receiver showing how a Slotwise customer verifies and dedupes deliveries.

SLOTWISE_WEBHOOK_SECRET=whsec_... uv run uvicorn examples.webhook_receiver:app --port 9000
"""

import hashlib
import hmac
import os
import time

from fastapi import FastAPI, HTTPException, Request

app = FastAPI()
SECRET = os.environ.get("SLOTWISE_WEBHOOK_SECRET", "")
TOLERANCE = 300
seen_events: set[str] = set()  # use a DB table with a unique constraint in real life


@app.post("/slotwise")
async def receive(request: Request) -> dict[str, str]:
    raw = await request.body()  # verify the RAW bytes, before parsing JSON
    header = request.headers.get("Slotwise-Signature", "")
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, sig = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "bad signature header") from exc
    if abs(time.time() - ts) > TOLERANCE:
        raise HTTPException(400, "stale")
    expected = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "bad signature")

    event_id = request.headers["Slotwise-Event-Id"]
    if event_id in seen_events:  # deliveries are at-least-once
        return {"status": "duplicate"}
    seen_events.add(event_id)
    return {"status": "ok"}
