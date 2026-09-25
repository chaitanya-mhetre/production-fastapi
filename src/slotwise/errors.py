"""Application errors. Services raise these; one handler turns them into consistent JSON."""

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code.replace("_", " ")
        self.details = details or {}


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class SlotTaken(Conflict):
    code = "slot_taken"


class VersionConflict(Conflict):
    code = "version_conflict"


class IdempotencyInProgress(Conflict):
    code = "idempotency_in_progress"


class IdempotencyKeyReused(AppError):
    status_code = 422
    code = "idempotency_key_reused"


class IdempotencyKeyRequired(AppError):
    status_code = 400
    code = "idempotency_key_required"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str | None = None, *, retry_after: int, limit: int):
        super().__init__(message, details={"retry_after": retry_after, "limit": limit})
        self.retry_after = retry_after
        self.limit = limit


class QuotaExceeded(RateLimited):
    code = "quota_exceeded"
