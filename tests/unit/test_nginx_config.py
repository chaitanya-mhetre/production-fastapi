"""Static checks on nginx/nginx.conf for behaviour that only shows up under load."""

import re
from pathlib import Path

CONF = (Path(__file__).parents[2] / "nginx" / "nginx.conf").read_text()


def _directives(name: str) -> list[str]:
    return re.findall(rf"^\s*{name}\s+([^;]+);", CONF, flags=re.MULTILINE)


def test_rate_limited_requests_get_429_not_503() -> None:
    # Nginx answers limit_req rejections with 503 unless told otherwise. The k6 booking rush showed
    # thousands of 503s that were really rate limiting; a 503 pages on-call and invites retries.
    assert _directives("limit_req"), "per-IP limit_req is expected in front of the API"
    assert _directives("limit_req_status") == ["429"]


def test_upstream_is_re_resolved_at_runtime() -> None:
    # Without `resolve` + a resolver, Nginx pins the api container's IP and 502s after a redeploy.
    assert _directives("resolver")
    assert any("resolve" in s for s in _directives("server"))
