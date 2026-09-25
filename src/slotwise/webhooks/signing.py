"""HMAC-SHA256 webhook signatures (same scheme for outbound webhooks and inbound mockpay).

Header: `t=<unix seconds>,v1=<hex hmac>` where hmac = HMAC_SHA256(secret, f"{t}.{raw_body}").

- Signing the raw body proves the payload came from someone holding the secret, unmodified.
- Including the timestamp in the signed string, and rejecting old timestamps, stops replay of a
  captured request days later.
- compare_digest is constant-time, so an attacker can't recover the signature byte by byte
  from response timing.
"""

import hashlib
import hmac
import time

from slotwise.errors import Unauthorized


def compute(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={compute(secret, ts, body)}"


def verify(
    secret: str, header: str | None, body: bytes, *, tolerance_seconds: int, now: int | None = None
) -> None:
    if not header:
        raise Unauthorized("missing signature")
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        signature = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise Unauthorized("malformed signature header") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise Unauthorized("signature timestamp outside tolerance")
    if not hmac.compare_digest(compute(secret, timestamp, body), signature):
        raise Unauthorized("invalid signature")
