from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from slotwise.api.deps import DbDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Must not touch dependencies, or a DB blip restarts every pod."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(db: DbDep) -> JSONResponse:
    """Readiness: can we serve traffic? Load balancers stop routing here when this fails."""
    checks: dict[str, str] = {}
    try:
        async with db.session() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 — report, don't crash, the probe
        checks["postgres"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint. Nginx only allows it from the internal network."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
