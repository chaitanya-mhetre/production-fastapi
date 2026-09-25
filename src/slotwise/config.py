"""Application settings, read from the environment (12-factor). Secrets never have real defaults."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SLOTWISE_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    # Three roles, three URLs: see docker/postgres/10-roles.sql for why.
    database_url: str = "postgresql+asyncpg://slotwise_app:slotwise_app@localhost:55436/slotwise"
    migration_database_url: str = "postgresql+asyncpg://slotwise:slotwise@localhost:55436/slotwise"
    worker_database_url: str = (
        "postgresql+asyncpg://slotwise_worker:slotwise_worker@localhost:55436/slotwise"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20

    redis_url: str = "redis://localhost:56383/0"
    celery_broker_url: str = "redis://localhost:56383/1"

    jwt_secret: str = Field(default="dev-only-change-me-dev-only-change-me", min_length=32)
    jwt_issuer: str = "slotwise"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 3600

    # Fernet key used to encrypt webhook signing secrets at rest.
    # Dev default is a throwaway key; generate a real one with `Fernet.generate_key()`.
    fernet_key: str = "zVn1YcZ2PZ2kW1v0fS3m0mB8tE0yq0gq3cQ3m8m3X8Q="

    mockpay_webhook_secret: str = "dev-mockpay-secret"  # noqa: S105 — dev default
    webhook_timestamp_tolerance_seconds: int = 300
    webhook_allow_private_targets: bool = False  # SSRF guard; tests/dev may flip this

    smtp_host: str = "localhost"
    smtp_port: int = 51025
    mail_from: str = "Slotwise <no-reply@slotwise.local>"

    s3_bucket: str = "slotwise-uploads"
    s3_region: str = "ap-south-1"
    s3_endpoint_url: str | None = None  # set to MinIO locally
    upload_max_bytes: int = 5 * 1024 * 1024

    idempotency_ttl_seconds: int = 24 * 3600
    idempotency_lock_seconds: int = 30

    otel_exporter_otlp_endpoint: str | None = None
    service_name: str = "slotwise-api"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
