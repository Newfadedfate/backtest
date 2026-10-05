"""System + debug/logging API endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.core.logging_setup import (
    get_logger,
    recent_logs,
    set_all_levels,
    set_level,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/system", tags=["System"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "backend"}


@router.get("/logs")
def get_recent_logs(
    min_level: str = Query("DEBUG", regex="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    limit: int = Query(100, ge=1, le=2000),
) -> dict:
    """Return recent in-memory log records (for UI debug panel live tail)."""
    records = recent_logs(min_level=min_level, limit=limit)
    # Strip internal _levelno field before returning over API
    cleaned = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
    return {"count": len(cleaned), "min_level": min_level, "records": cleaned}


@router.post("/log/level")
def update_log_level(
    logger_name: str = Query(..., description="Logger name, or 'ALL' for everything"),
    level: str = Query(..., regex="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
) -> dict:
    """Runtime toggle log level without restart.

    Use this to flip DEBUG on when a backtest looks wrong.
    """
    if logger_name == "ALL":
        set_all_levels(level)
    else:
        set_level(logger_name, level)
    return {"logger": logger_name, "new_level": level}
