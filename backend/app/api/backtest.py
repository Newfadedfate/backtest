"""Backtest API endpoints — run strategies, fetch trade lists, parity checks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core.logging_setup import get_logger, set_context, clear_context
from app.core.schemas import BacktestRequest, BacktestResult, Timeframe, OptimizeRequest
from app.strategy.engine import list_strategies, get_strategy_class
from app.backtest import run_backtest
from app.backtest.parity import run_parity_check, ParityReport
from app.backtest.optimizer import load_bars_for_optimize, run_optimize

log = get_logger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["Backtest"])


class StrategyInfo(BaseModel):
    strategy_id: str
    class_name: str
    available_params_doc: dict


@router.get("/strategies")
def list_strategies_endpoint() -> dict:
    """List registered strategy IDs with basic class name info."""
    names = list_strategies()
    info = []
    for name in names:
        try:
            cls = get_strategy_class(name)
            info.append({
                "strategy_id": name,
                "class_name": cls.__name__,
                "description": getattr(cls, "__doc__", None),
            })
        except Exception:
            info.append({"strategy_id": name, "class_name": "Unknown"})
    return {"count": len(info), "strategies": info}


@router.post("/run", response_model_exclude_none=True)
def run_backtest_endpoint(req: BacktestRequest) -> dict:
    """Run an event-driven backtest for a strategy on stored bars.

    Requires bars for the requested (symbols, timeframe, date range) to already
    be imported in MarketDataStore (use /api/data/bars?adapter=mock first to
    generate test data if store is empty).
    """
    if not req.symbols:
        raise HTTPException(400, "At least one symbol is required.")
    try:
        get_strategy_class(req.strategy_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    log.info(
        "Backtest API request received",
        extra={
            "strategy": req.strategy_id,
            "symbols": req.symbols,
            "timeframe": req.timeframe.value,
            "start": req.start.isoformat() if req.start else None,
            "end": req.end.isoformat() if req.end else None,
            "accounts": list(req.initial_capital.keys()),
        },
    )
    try:
        set_context(strategy_id=f"api:{req.strategy_id}",
                    timeframe=req.timeframe.value)
        result: BacktestResult = run_backtest(req)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error(
            "Backtest run threw unhandled exception",
            extra={"strategy": req.strategy_id, "error": str(e)},
            exc_info=True,
        )
        raise HTTPException(500, f"Backtest failed: {e}")
    finally:
        clear_context()

    # Serialize Trade pydantic → dicts for JSON response
    trades_json = [t.model_dump(mode="json") for t in result.trades]
    return {
        "strategy_id": result.strategy_id,
        "symbols": result.symbols,
        "timeframe": result.timeframe,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "total_bars": result.total_bars,
        "summary": result.summary,
        "equity_curve": result.equity_curve,
        "drawdown_curve": result.drawdown_curve,
        "trades": trades_json,
        "trade_count": len(result.trades),
        "per_account": result.per_account,
        "per_symbol": result.per_symbol,
        "correlated_signal_groups": result.correlated_signal_groups,
    }


class ParityCheckRequest(BaseModel):
    strategy_id: str
    symbol: str
    timeframe: Timeframe
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    limit: Optional[int] = Query(None, ge=100, le=100000)
    strategy_params: dict = {}
    min_warmup_bars: int = 60
    account_id: str = "parity_api"
    initial_capital: float = 100_000.0


@router.post("/parity")
def run_parity_endpoint(req: ParityCheckRequest) -> dict:
    """Run parity check (backtest driver vs simulated-live driver).

    Passes if both produce identical trades — which they MUST for the engine
    to be trusted for live deployment.
    """
    from app.data.store import get_store
    store = get_store()

    # Fetch bars via store
    bars = store.get_bars(req.symbol, req.timeframe, req.start, req.end, req.limit)
    if len(bars) <= req.min_warmup_bars:
        raise HTTPException(
            400,
            f"Not enough bars: got {len(bars)}, need > {req.min_warmup_bars} (warmup). "
            f"Use /api/data/bars?adapter=mock first to generate data."
        )
    report: ParityReport = run_parity_check(
        strategy_class_name=req.strategy_id,
        strategy_params=req.strategy_params,
        bars=bars,
        symbol=req.symbol,
        timeframe=req.timeframe,
        account_id=req.account_id,
        initial_capital=req.initial_capital,
        min_warmup_bars=req.min_warmup_bars,
        log_divergences_at_critical=True,
    )
    return report.as_dict()


@router.post("/optimize")
def run_optimize_endpoint(req: OptimizeRequest) -> dict:
    """Grid / walk-forward parameter sweep.

    Vectorized ranks are screening only. Official scores are event-driven.
    """
    if not req.symbols:
        raise HTTPException(400, "At least one symbol is required.")
    if not req.param_grid:
        raise HTTPException(400, "param_grid must contain at least one parameter list.")
    try:
        get_strategy_class(req.strategy_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if req.rank_by not in ("net_pnl", "profit_factor", "expectancy", "win_rate"):
        raise HTTPException(400, "rank_by must be net_pnl, profit_factor, expectancy, or win_rate")
    if req.walk_forward_mode not in ("anchored", "rolling"):
        raise HTTPException(400, "walk_forward_mode must be anchored or rolling")

    bars_by_symbol = load_bars_for_optimize(req)
    if not any(bars_by_symbol.values()):
        raise HTTPException(
            400,
            "No bars in store for requested symbols. "
            "Use /api/data/bars?adapter=mock first to generate data.",
        )
    log.info(
        "Optimize API request received",
        extra={
            "strategy": req.strategy_id,
            "symbols": req.symbols,
            "grid_keys": list(req.param_grid.keys()),
            "rank_by": req.rank_by,
            "wfo_folds": req.walk_forward_folds,
        },
    )
    result = run_optimize(req, bars_by_symbol)
    return result.model_dump(mode="json")

