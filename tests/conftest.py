"""Test configuration.

Integration tests run against the real Postgres + Redis from `docker compose up -d` (host ports
55436 / 56383), in a separate `slotwise_test` database. If they aren't reachable, integration
tests are skipped with a clear reason instead of silently passing.
"""

import os

os.environ.update(
    {
        "SLOTWISE_ENV": "test",
        "SLOTWISE_DATABASE_URL": os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://slotwise_app:slotwise_app@localhost:55436/slotwise_test",
        ),
        "SLOTWISE_MIGRATION_DATABASE_URL": os.getenv(
            "TEST_MIGRATION_DATABASE_URL",
            "postgresql+asyncpg://slotwise:slotwise@localhost:55436/slotwise_test",
        ),
        "SLOTWISE_WORKER_DATABASE_URL": os.getenv(
            "TEST_WORKER_DATABASE_URL",
            "postgresql+asyncpg://slotwise_worker:slotwise_worker@localhost:55436/slotwise_test",
        ),
        "SLOTWISE_REDIS_URL": os.getenv("TEST_REDIS_URL", "redis://localhost:56383/5"),
        "SLOTWISE_CELERY_BROKER_URL": "memory://",
        "SLOTWISE_WEBHOOK_ALLOW_PRIVATE_TARGETS": "true",
        "SLOTWISE_LOG_LEVEL": "WARNING",
    }
)

import socket  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

import pytest  # noqa: E402


def _reachable(url: str) -> bool:
    parsed = urlparse(url.replace("+asyncpg", ""))
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 1):
            return True
    except OSError:
        return False


SERVICES_UP = _reachable(os.environ["SLOTWISE_MIGRATION_DATABASE_URL"]) and _reachable(
    os.environ["SLOTWISE_REDIS_URL"]
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(
        reason="Postgres/Redis not reachable on 55436/56383: run `docker compose up -d`"
    )
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
            if not SERVICES_UP:
                item.add_marker(skip)
