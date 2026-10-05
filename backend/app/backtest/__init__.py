"""Event-Driven Backtester — replay bars bar-by-bar through the SHARED STRATEGY ENGINE.

IMPORTANT: This driver and the live driver both call:
    1. PositionState.check_stops(current_bar) — stop/target fills
    2. evaluate_bar(strategy, context) — strategy.on_bar(context) through evaluate_bar()

Therefore behavior is identical between the two. Any divergence in trade outputs
between a backtest replay and a simulated-live replay of the same data is a
critical bug → CRITICAL-level log via parity harness.

Fill modes:
  - "next_bar_open"  (conservative, default): strategy orders from bar i are filled
     at bar i+1's open price. This is what you should trust for real results.
  - "same_bar_close": strategy orders filled at current bar close. Optimistic,
     useful for quick sanity but NOT trusted for real results.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

from app.core.logging_setup import get_logger, set_context, clear_context
from app.core.schemas import (
    Bar,
    BacktestRequest,
    BacktestResult,
    Order,
    OrderSide,
    PositionSide,
    Timeframe,
    Trade,
)
from app.strategy.context import StrategyContext
from app.strategy.engine import Strategy, evaluate_bar, get_strategy_class
from app.strategy.position import PositionState
from app.data.feed import DataFeed

log = get_logger(__name__)


@dataclass
class DriverOutput:
    """Raw output of running a strategy through bars via either backtest or live-sim driver."""
    trades: list[Trade]
    equity: list[dict]  # [{t, equity, drawdown}]
    bar_orders: dict[int, list[Order]]  # bar_idx -> orders emitted
    per_account_capital: dict[str, float]  # ending capital per account


def _run_driver(
    strategy: Strategy,
    bars: list[Bar],
    symbol: str,
    timeframe: Timeframe,
    initial_capitals: dict[str, float],
    commission: float,
    slippage_bps: float,
    fill_mode: str,
    dry_run_verbose: bool = False,
    # Warmup = min bar lookback strategy needs to start emitting signals
    min_warmup_bars: int = 60,
) -> DriverOutput:
    """Shared driver code used by BOTH event-driven backtest AND live-sim parity driver.

    This is the ONE driver. The only difference between "backtest" and "simulated live"
    is how bars are sourced (loop vs feed events) — the engine logic below is identical.
    """
    if not bars:
        raise ValueError("No bars passed to driver")

    # Per (account_id) state
    accounts: dict[str, PositionState] = {}
    cash: dict[str, float] = dict(initial_capitals)  # realized cash per account
    for acc_id in initial_capitals:
        accounts[acc_id] = PositionState(acc_id, symbol, strategy.id)

    trades: list[Trade] = []
    equity_curve: list[dict] = []
    bar_orders: dict[int, list[Order]] = defaultdict(list)

    # Rolling bar window (deep-enough for strategy indicator calc)
    rolling_bars: list[Bar] = []

    # Pending orders queue for next-bar fill
    pending_orders: list[tuple[int, Order]] = []  # (fill_bar_idx, order)

    total_bars = len(bars)
    t0_run = time.perf_counter()

    log.info(
        "Backtest/sim driver starting",
        extra={
            "strategy_id": strategy.id,
            "strategy_class": type(strategy).__name__,
            "symbol": symbol,
            "timeframe": timeframe.value,
            "bars": total_bars,
            "accounts": list(initial_capitals.keys()),
            "fill_mode": fill_mode,
            "commission_per_trade_qty": commission,
            "slippage_bps": slippage_bps,
            "dry_run_verbose": dry_run_verbose,
        },
    )

    for i in range(total_bars):
        bar = bars[i]
        rolling_bars.append(bar)
        if len(rolling_bars) > 5000:  # cap history to bound memory
            rolling_bars = rolling_bars[-5000:]

        # ── 1. Fill pending orders (next-bar-open or same-bar close) ──
        just_filled: list[tuple[int, Order]] = []
        for (target_bar_idx, order) in pending_orders:
            if target_bar_idx > i:
                continue
            just_filled.append((target_bar_idx, order))
            # Decide fill price based on fill_mode
            if fill_mode == "same_bar_close":
                # target_bar_idx is same bar that emitted order; fill at its close
                fill_bar = bars[target_bar_idx]
                fill_price = fill_bar.close
            else:
                # default next_bar_open — fill at current i's open
                fill_bar = bar
                fill_price = fill_bar.open
            acc = accounts[order.account_id]
            trade = acc.apply_fill(
                order, fill_price=fill_price,
                fill_ts=bar.timestamp,
                commission=commission,
                slippage_bps=slippage_bps,
            )
            if trade is not None:
                trades.append(trade)
                cash[order.account_id] += trade.pnl  # realized pnl added to cash
        pending_orders = [p for p in pending_orders if p not in just_filled]

        # ── 2. Stop / target check on current bar BEFORE on_bar ───────
        for acc_id, acc_state in accounts.items():
            trade_hit, stop_order = acc_state.check_stops(
                bar.high, bar.low,
                commission=commission, slippage_bps=slippage_bps,
                bar_ts=bar.timestamp,
            )
            if trade_hit is not None:
                trades.append(trade_hit)
                cash[acc_id] += trade_hit.pnl

        # ── 3. Warmup gate: don't call strategy until past min lookback ─
        if i < min_warmup_bars:
            continue

        # ── 4. For each account, construct context + run strategy.on_bar ─
        for acc_id, acc_state in accounts.items():
            pos_snap = acc_state.snapshot()
            ctx = StrategyContext(
                symbol=symbol,
                timeframe=timeframe,
                bars=list(rolling_bars),
                position=pos_snap,
                params=strategy.params,
                account_id=acc_id,
                strategy_id=strategy.id,
                current_bar_idx=i,
            )
            try:
                # Set logging context for full traceability during evaluate_bar
                set_context(symbol=symbol, timeframe=timeframe.value,
                            account_id=acc_id, strategy_id=strategy.id)
                orders_emitted = evaluate_bar(strategy, ctx,
                                              dry_run_verbose=dry_run_verbose)
            finally:
                clear_context()
            for o in orders_emitted:
                # Zero-qty set_stops signal → apply and don't enqueue as trade
                tags = o.tags or {}
                if tags.get("action") == "set_stops" and o.quantity == 0.0:
                    stop = tags.get("stop_loss")
                    tgt = tags.get("take_profit")
                    if stop is not None or tgt is not None:
                        accounts[acc_id].set_stops(stop_loss=stop, take_profit=tgt)
                    continue
                # Real trade order → enqueue for next-bar or same-bar fill
                fill_idx = i if fill_mode == "same_bar_close" else i + 1
                if fill_idx < total_bars or fill_mode == "same_bar_close":
                    pending_orders.append((fill_idx, o))
                    bar_orders[i].append(o)

        # ── 5. Mark-to-market equity snapshot per bar ──────────────────
        total_equity = 0.0
        for acc_id in accounts:
            acc = accounts[acc_id]
            unrealized = 0.0
            if acc.side == PositionSide.LONG:
                unrealized = (bar.close - acc._position.avg_entry_price) * acc.quantity
            elif acc.side == PositionSide.SHORT:
                unrealized = (acc._position.avg_entry_price - bar.close) * acc.quantity
            eq = cash[acc_id] + unrealized
            total_equity += eq
        equity_curve.append({
            "t": bar.timestamp.isoformat(),
            "ts": bar.timestamp,
            "equity": total_equity,
        })

    # ── Post-run: compute peak equity / drawdown curve ────────────────
    peak = -float("inf")
    max_dd = 0.0
    for pt in equity_curve:
        peak = max(peak, pt["equity"])
        dd = (peak - pt["equity"]) / peak if peak > 0 else 0.0
        pt["drawdown"] = dd
        if dd > max_dd:
            max_dd = dd
    # Final per-account ending capital (cash + final position mark-to-market)
    final_cap = {}
    last_bar = bars[-1]
    for acc_id in accounts:
        acc = accounts[acc_id]
        unrealized = 0.0
        if acc.side == PositionSide.LONG:
            unrealized = (last_bar.close - acc._position.avg_entry_price) * acc.quantity
        elif acc.side == PositionSide.SHORT:
            unrealized = (acc._position.avg_entry_price - last_bar.close) * acc.quantity
        final_cap[acc_id] = cash[acc_id] + unrealized

    elapsed = time.perf_counter() - t0_run
    log.info(
        "Driver finished",
        extra={
            "trades": len(trades),
            "bars_processed": total_bars,
            "warmup": min_warmup_bars,
            "elapsed_sec": round(elapsed, 4),
            "bars_per_sec": round(total_bars / max(elapsed, 1e-9), 1),
            "ending_total_equity": round(sum(final_cap.values()), 2),
        },
    )

    return DriverOutput(
        trades=trades,
        equity=equity_curve,
        bar_orders=dict(bar_orders),
        per_account_capital=final_cap,
    )


def summarize(output: DriverOutput) -> dict:
    """Compute summary statistics: net P&L, win rate, profit factor, avg win/loss, max DD."""
    trades = output.trades
    summary: dict[str, Any] = {
        "total_trades": len(trades),
        "net_pnl": 0.0,
        "gross_win": 0.0,
        "gross_loss": 0.0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": 0.0,
        "expectancy": 0.0,
        "max_drawdown": 0.0,
    }
    if trades:
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        summary["net_pnl"] = sum(pnls)
        summary["gross_win"] = sum(wins)
        summary["gross_loss"] = abs(sum(losses))
        summary["wins"] = len(wins)
        summary["losses"] = len(losses)
        summary["win_rate"] = len(wins) / len(pnls) if pnls else 0.0
        summary["avg_win"] = (sum(wins) / len(wins)) if wins else 0.0
        summary["avg_loss"] = (sum(losses) / len(losses)) if losses else 0.0
        summary["profit_factor"] = (
            summary["gross_win"] / summary["gross_loss"] if summary["gross_loss"] > 0 else float("inf")
            if summary["gross_win"] > 0 else 0.0
        )
        if summary["profit_factor"] == float("inf"):
            summary["profit_factor"] = 1e9
        summary["expectancy"] = (
            summary["win_rate"] * summary["avg_win"]
            - (1 - summary["win_rate"]) * abs(summary["avg_loss"])
        )
    if output.equity:
        dds = [pt.get("drawdown", 0.0) for pt in output.equity]
        summary["max_drawdown"] = max(dds) if dds else 0.0
    # Round numerics for readability
    for k, v in list(summary.items()):
        if isinstance(v, float):
            summary[k] = round(v, 6)
    return summary


def run_backtest(req: BacktestRequest, store=None, bars_by_symbol=None) -> BacktestResult:
    """Top-level backtest runner used by API endpoint.

    Args:
        req: BacktestRequest schema.
        store: Optional MarketDataStore to use. If None, uses global singleton
               via get_store() (production default). Pass a temp store for testing.
        bars_by_symbol: Optional {symbol: list[Bar]} override. When provided,
               those bars are used instead of querying the store (optimizer
               walk-forward windows). Store is still required as fallback for
               any symbol missing from the override.
    """
    # Resolve strategy class + instantiate
    try:
        cls = get_strategy_class(req.strategy_id)
    except ValueError as e:
        raise ValueError(f"Invalid strategy_id: {e}") from e
    strategy = cls(strategy_id=f"{req.strategy_id}-bt-{int(time.time())}",
                   params=req.strategy_params)

    # Gather bars per symbol. For single-symbol backtests we loop once per symbol
    # but for multi-symbol parity we want the same driver output per symbol.
    if store is None and bars_by_symbol is None:
        from app.data.store import get_store
        store = get_store()

    all_trades: list[Trade] = []
    equity_combined: Optional[list[dict]] = None
    per_account: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    per_symbol: dict[str, dict] = {}
    t0_all = time.perf_counter()

    # For this event-driven impl we run each symbol sequentially and combine outputs.
    # Multi-symbol concurrent engine can be added later (it's additive).
    start_str = req.start.isoformat() if req.start else None
    end_str = req.end.isoformat() if req.end else None
    log.info(
        "Starting event-driven backtest",
        extra={
            "strategy": req.strategy_id,
            "symbols": req.symbols,
            "timeframe": req.timeframe.value,
            "start": start_str, "end": end_str,
            "accounts": list(req.initial_capital.keys()),
        },
    )
    strategy.on_start(StrategyContext(
        symbol=req.symbols[0], timeframe=req.timeframe,
        bars=[], position=_empty_position(req.symbols[0], list(req.initial_capital.keys())[0], strategy.id),
        params=strategy.params,
        account_id=list(req.initial_capital.keys())[0],
        strategy_id=strategy.id,
    ))

    for sym in req.symbols:
        if bars_by_symbol is not None and sym in bars_by_symbol:
            bars = bars_by_symbol[sym]
        else:
            if store is None:
                from app.data.store import get_store
                store = get_store()
            bars = store.get_bars(sym, req.timeframe, req.start, req.end)
        if not bars:
            per_symbol[sym] = {"bars": 0, "trades": 0, "note": "NO BARS FOUND"}
            log.warning("Backtest symbol has NO bars in store — skipping",
                        extra={"symbol": sym, "timeframe": req.timeframe.value})
            continue

        output = _run_driver(
            strategy=strategy,
            bars=bars,
            symbol=sym,
            timeframe=req.timeframe,
            initial_capitals=req.initial_capital,
            commission=req.commission_per_trade,
            slippage_bps=req.slippage_bps,
            fill_mode=req.fill_mode,
            # Don't pass dry_run here; caller controls DEBUG via runtime log level toggles
        )
        all_trades.extend(output.trades)
        if equity_combined is None:
            equity_combined = list(output.equity)
        else:
            # Simplistic timestamp union for multi-symbol. Fine for multi-symbol
            # initial impl; improve later.
            eq_map = {pt["t"]: pt for pt in equity_combined}
            for pt in output.equity:
                if pt["t"] in eq_map:
                    eq_map[pt["t"]]["equity"] += pt["equity"]
                    eq_map[pt["t"]]["drawdown"] = 0.0  # recompute later
                else:
                    eq_map[pt["t"]] = pt
            equity_combined = sorted(eq_map.values(), key=lambda x: x["t"])
        # Per-account rollup
        for acc_id, end_cap in output.per_account_capital.items():
            start_cap = req.initial_capital.get(acc_id, 0.0)
            per_account[acc_id]["trades"] += sum(1 for t in output.trades if t.account_id == acc_id)
            per_account[acc_id]["pnl"] += (end_cap - start_cap)
        per_symbol[sym] = {
            "bars": len(bars),
            "trades": len(output.trades),
            "symbol_pnl": round(sum(t.pnl for t in output.trades), 2),
        }

    # Recompute drawdown on combined curve
    if equity_combined:
        peak = -float("inf")
        max_dd = 0.0
        for pt in equity_combined:
            peak = max(peak, pt["equity"])
            dd = (peak - pt["equity"]) / peak if peak > 0 else 0.0
            pt["drawdown"] = dd
            max_dd = max(max_dd, dd)

    # Correlated signal groups: trades with same timestamp
    corr_groups: list[list[str]] = []
    if req.dedupe_correlated_signals and all_trades:
        by_ts: dict[str, list[Trade]] = defaultdict(list)
        for t in all_trades:
            by_ts[t.entry_timestamp.isoformat()].append(t)
        for ts, grp in by_ts.items():
            if len(grp) > 1:
                corr_groups.append([t.id for t in grp])

    strategy.on_stop(StrategyContext(
        symbol=req.symbols[0], timeframe=req.timeframe,
        bars=[], position=_empty_position(req.symbols[0], list(req.initial_capital.keys())[0], strategy.id),
        params=strategy.params,
        account_id=list(req.initial_capital.keys())[0],
        strategy_id=strategy.id,
    ))

    # Build summary from all trades + curve
    fake_output = DriverOutput(
        trades=all_trades,
        equity=equity_combined or [],
        bar_orders={},
        per_account_capital={},
    )
    summary = summarize(fake_output)
    summary["runtime_sec"] = round(time.perf_counter() - t0_all, 3)

    # Sum actual total bars processed across all symbols (replace placeholder)
    actual_total_bars = sum(
        per_symbol.get("bars", 0) for per_symbol in per_symbol.values()
    )

    log.info(
        "Backtest complete",
        extra={
            "strategy": req.strategy_id,
            "total_trades": summary["total_trades"],
            "net_pnl": summary["net_pnl"],
            "win_rate": round(summary["win_rate"] * 100, 1),
            "max_drawdown_pct": round(summary["max_drawdown"] * 100, 2),
            "profit_factor": summary["profit_factor"],
            "correlated_groups": len(corr_groups),
            "runtime_sec": summary["runtime_sec"],
            "total_bars_processed": actual_total_bars,
        },
    )

    # Clean up private _ts field in equity for serialization
    cleaned_eq = [{"t": p["t"], "equity": round(p["equity"], 4),
                   "drawdown": round(p.get("drawdown", 0.0), 6)}
                  for p in (equity_combined or [])]
    cleaned_dd = [{"t": p["t"], "drawdown": round(p.get("drawdown", 0.0), 6)}
                  for p in (equity_combined or [])]

    start_ts = req.start or (pd.to_datetime(all_trades[0].entry_timestamp).to_pydatetime()
                            if all_trades else datetime(1970, 1, 1, tzinfo=timezone.utc))
    end_ts = req.end or (pd.to_datetime(all_trades[-1].exit_timestamp).to_pydatetime()
                        if all_trades else datetime(1970, 1, 1, tzinfo=timezone.utc))

    return BacktestResult(
        strategy_id=req.strategy_id,
        symbols=req.symbols,
        timeframe=req.timeframe.value,
        start=start_ts,
        end=end_ts,
        total_bars=actual_total_bars,
        summary=summary,
        equity_curve=cleaned_eq,
        drawdown_curve=cleaned_dd,
        trades=all_trades,
        per_account={k: {"ending_capital": round(req.initial_capital[k] + v["pnl"], 2),
                         "pnl": round(v["pnl"], 2),
                         "trades": v["trades"]} for k, v in per_account.items()},
        per_symbol=per_symbol,
        correlated_signal_groups=corr_groups,
        logs=[f"Ran {len(req.symbols)} symbol(s), {len(all_trades)} trade(s)"],
    )


def _empty_position(symbol: str, account_id: str, strategy_id: str):
    from app.strategy.position import PositionState
    return PositionState(account_id, symbol, strategy_id).snapshot()
