"""SSRF guard for outbound webhooks.

Tenants choose webhook URLs, so without a guard a tenant could point one at
http://169.254.169.254/ (cloud metadata → IAM credentials) or http://localhost:5432 and use our
workers to probe the internal network. We resolve the hostname and refuse private, loopback,
link-local, multicast and reserved addresses.

Residual risk (documented in docs/security.md): DNS rebinding. The name can resolve to a public
IP here and a private one when httpx connects. The complete fix is to connect to the vetted IP
directly (or send egress through a proxy that enforces the policy).
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from slotwise.errors import ValidationFailed


class UnsafeWebhookTarget(ValidationFailed):
    code = "unsafe_webhook_target"


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_target(url: str, *, allow_private: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeWebhookTarget("webhook URL must be http(s) with a hostname")
    if parsed.username or parsed.password:
        raise UnsafeWebhookTarget("credentials in webhook URLs are not allowed")
    if allow_private:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise UnsafeWebhookTarget(f"cannot resolve {parsed.hostname}") from exc
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not _is_public(ip):
            raise UnsafeWebhookTarget(f"{parsed.hostname} resolves to non-public address {ip}")
