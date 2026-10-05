"""Data management API routes — data feeds, import, bars, validation."""

from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel

from app.core.logging_setup import get_logger
from app.core.schemas import Bar, DataValidationReport, Timeframe

log = get_logger(__name__)
from app.data.feed import list_adapters, get_adapter_class
from app.data.store import get_store
from app.data.validate import validate_bars

router = APIRouter(prefix="/api/data", tags=["Data"])


# ── Data feed adapters ──────────────────────────────────────────────

@router.get("/adapters")
def list_available_adapters() -> dict:
    """List registered data feed adapter names."""
    return {"adapters": list_adapters()}


# ── Symbol / bar queries ────────────────────────────────────────────

class ImportBarsRequest(BaseModel):
    bars: list[Bar]


@router.get("/symbols")
def list_symbols() -> dict:
    """List all symbols in the data store with metadata (bar_count, first/last ts)."""
    store = get_store()
    return {"symbols": store.list_symbols()}


@router.get("/bars")
def get_bars(
    symbol: str = Query(..., description="Instrument symbol, e.g. NIFTY, XAUUSD"),
    timeframe: Timeframe = Query(..., description="Bar timeframe"),
    start: Optional[datetime] = Query(None, description="Start datetime (UTC, inclusive)"),
    end: Optional[datetime] = Query(None, description="End datetime (UTC, inclusive)"),
    limit: Optional[int] = Query(None, description="Return last N bars only"),
    adapter: Optional[str] = Query(None, description="If set, fetch via this feed adapter first, then store"),
) -> dict:
    """Fetch bars from store (Parquet + ring buffer merged).

    If ``adapter`` is given, it fetches historical from that adapter, imports
    into the store, then returns the result.
    """
    store = get_store()

    # Optional: fetch via adapter (e.g. mock, CSV) first, then store
    if adapter is not None:
        try:
            cls = get_adapter_class(adapter)
            feed = cls()
            feed.connect()
            adapter_bars = feed.get_historical(symbol, timeframe, start, end, limit)
            feed.disconnect()
            if adapter_bars:
                # Validate before importing — IMPORTANT: never skip this step.
                report = validate_bars(adapter_bars, symbol, timeframe)
                if report.error_count > 0:
                    # Still import but WARN caller — don't silently swallow issues.
                    store.import_bars(adapter_bars)
                    return {
                        "symbol": symbol,
                        "timeframe": timeframe.value,
                        "bar_count": len(adapter_bars),
                        "validation": report.model_dump(),
                        "bars": [b.model_dump(mode="json") for b in adapter_bars],
                        "warning": "Imported with validation ERRORS. Check validation report.",
                    }
                store.import_bars(adapter_bars)
        except ValueError as e:
            raise HTTPException(400, str(e))

    bars = store.get_bars(symbol, timeframe, start, end, limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe.value,
        "bar_count": len(bars),
        "bars": [b.model_dump(mode="json") for b in bars],
    }


# ── Import: bulk bars via JSON ──────────────────────────────────────

@router.post("/import/bars")
def import_bars_endpoint(req: ImportBarsRequest) -> dict:
    """Bulk import OHLC bars via JSON body. Runs validation first.

    Returns import stats + full validation report. Never silently skips bad data.
    """
    if not req.bars:
        raise HTTPException(400, "No bars provided.")
    store = get_store()
    # Run validation per (symbol, tf) group
    from collections import defaultdict
    groups: dict[tuple[str, Timeframe], list[Bar]] = defaultdict(list)
    for b in req.bars:
        groups[(b.symbol, b.timeframe)].append(b)

    reports: list[dict] = []
    for (sym, tf), bars in groups.items():
        reports.append(validate_bars(bars, sym, tf).model_dump(mode="json"))

    stats = store.import_bars(req.bars)
    result = {
        "imported": stats,
        "total_bars": len(req.bars),
        "validation_reports": reports,
        "error_count": sum(r["error_count"] for r in reports),
        "warning_count": sum(r["warning_count"] for r in reports),
    }
    log.info("Bulk import via JSON completed",
             extra={"groups": len(groups), "total_bars": result["total_bars"],
                    "error_count": result["error_count"],
                    "warning_count": result["warning_count"]})
    return result


@router.post("/import/csv")
async def import_csv(
    file: UploadFile = File(...),
    symbol: str = Form(...),
    timeframe: Timeframe = Form(...),
) -> dict:
    """Upload a CSV file of OHLC bars.

    Expected columns: timestamp, open, high, low, close, volume(optional)
    """
    log.info("CSV upload request", extra={"filename": file.filename,
                                          "symbol": symbol, "timeframe": timeframe.value})
    content = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as e:
        log.error("CSV upload parse failed",
                  extra={"filename": file.filename, "error": str(e)}, exc_info=True)
        raise HTTPException(400, f"Failed to parse CSV: {e}")

    # Normalize via CsvAdapter path
    from app.data.adapters.csv_adapter import CsvAdapter
    feed = CsvAdapter()
    feed.connect()
    try:
        bars = feed._df_to_bars(df, symbol, timeframe)
    except ValueError as e:
        log.error("CSV upload rejected",
                  extra={"filename": file.filename, "symbol": symbol, "error": str(e)})
        raise HTTPException(400, str(e))
    finally:
        feed.disconnect()

    # Validate + import
    report = validate_bars(bars, symbol, timeframe)
    store = get_store()
    stats = store.import_bars(bars)
    log.info("CSV upload import done",
             extra={"filename": file.filename, "symbol": symbol,
                    "timeframe": timeframe.value, "bars": len(bars),
                    "errors": report.error_count, "warnings": report.warning_count})
    return {
        "filename": file.filename,
        "symbol": symbol,
        "timeframe": timeframe.value,
        "bar_count": len(bars),
        "imported": stats,
        "validation": report.model_dump(mode="json"),
    }


# ── Validation ──────────────────────────────────────────────────────

@router.get("/validate/{symbol}")
def validate_symbol(
    symbol: str,
    timeframe: Timeframe = Query(...),
) -> DataValidationReport:
    """Run validation against the currently stored bars for (symbol, tf)."""
    store = get_store()
    bars = store.get_bars(symbol, timeframe)
    return validate_bars(bars, symbol, timeframe)
