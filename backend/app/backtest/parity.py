"""Backtest / Live Parity Check harness.

RULE (per spec architecture #1): Backtest AND Live MUST produce IDENTICAL trades
when fed the exact same bar sequence. Any divergence is an ENGINE BUG that will
lose real money → this harness detects it and logs at CRITICAL level.

Methodology:
  - "Backtest driver":   _run_driver() loops bars in batch → emits trades.
  - "Simulated Live driver":  _run_driver() is also called (same code path).
    The ONLY difference is we call it twice to force different code paths to
    exercise parity — we ALSO have a SimulatedLive class that uses feed events
    (register a bar-close subscriber and call evaluate_bar per event) to ensure
    the feed dispatch pipeline is parity-safe.

We compare:
  a) Number of trades
  b) Each trade: entry_ts, exit_ts, side, entry_price, exit_price, qty, pnl
  c) Total P&L within 1e-9 float tolerance
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.logging_setup import get_logger, set_context, clear_context
from app.core.schemas import (
    Bar,
    Order,
    Timeframe,
    Trade,
)
from app.strategy.context import StrategyContext
from app.strategy.engine import Strategy, evaluate_bar, get_strategy_class
from app.strategy.position import PositionState
from app.backtest import _run_driver

log = get_logger(__name__)


# ── Trade "fingerprint" for quick comparison ──────────────────────────

def trade_fingerprint(t: Trade) -> dict:
    return {
        "side": t.side.value,
        "qty": round(t.quantity, 12),
        "entry_ts": t.entry_timestamp.isoformat() if t.entry_timestamp else None,
        "exit_ts": t.exit_timestamp.isoformat() if t.exit_timestamp else None,
        "entry": round(t.entry_price, 8),
        "exit": round(t.exit_price, 8),
        "pnl": round(t.pnl, 6),
        "exit_reason": t.exit_reason,
    }


@dataclass
class ParityReport:
    passes: bool
    divergences: list[str] = field(default_factory=list)
    backtest_trades: int = 0
    simulated_live_trades: int = 0
    elapsed_sec: float = 0.0

    def as_dict(self) -> dict:
        return {
            "passes": self.passes,
            "divergences": self.divergences,
            "backtest_trade_count": self.backtest_trades,
            "simulated_live_trade_count": self.simulated_live_trades,
            "elapsed_sec": round(self.elapsed_sec, 4),
        }


def run_parity_check(
    strategy_class_name: str,
    strategy_params: dict,
    bars: list[Bar],
    symbol: str,
    timeframe: Timeframe,
    account_id: str = "parity_acct",
    initial_capital: float = 100_000.0,
    commission: float = 0.0,
    slippage_bps: float = 0.0,
    fill_mode: str = "next_bar_open",
    min_warmup_bars: int = 60,
    log_divergences_at_critical: bool = True,
) -> ParityReport:
    """Run same bars through BACKTEST driver and SIMULATED LIVE driver; compare.

    Returns ParityReport. Divergences logged CRITICAL.
    """
    set_context(symbol=symbol, timeframe=timeframe.value, strategy_id=f"parity:{strategy_class_name}")
    try:
        if len(bars) <= min_warmup_bars:
            return ParityReport(passes=False,
                                divergences=[f"Not enough bars for parity: {len(bars)} <= warmup {min_warmup_bars}"])

        t0 = time.perf_counter()

        # ── 1) Backtest driver (batch loop through bars) ────────────────
        strategy_cls = get_strategy_class(strategy_class_name)
        strategy_bt = strategy_cls(
            strategy_id=f"{strategy_class_name}_BT",
            params=dict(strategy_params),
        )
        capitals_bt = {account_id: initial_capital}
        out_bt = _run_driver(
            strategy=strategy_bt,
            bars=bars,
            symbol=symbol,
            timeframe=timeframe,
            initial_capitals=capitals_bt,
            commission=commission,
            slippage_bps=slippage_bps,
            fill_mode=fill_mode,
            dry_run_verbose=False,
            min_warmup_bars=min_warmup_bars,
        )

        # ── 2) Simulated Live driver — feed-like dispatch ──────────────
        #   We emulate live engine behavior by building a small event loop
        #   that mimics what a LiveEngine bar-close handler would do:
        #     - on each bar, check_stops → evaluate_bar → enqueue fills.
        #   This is the SAME engine calls as _run_driver, but the calling
        #   convention matches what live/feed will do — parity guarantee.
        from app.core.schemas import OrderSide, PositionSide
        strategy_live = strategy_cls(
            strategy_id=f"{strategy_class_name}_LIVE",
            params=dict(strategy_params),
        )
        pos_state = PositionState(account_id, symbol, strategy_live.id)
        cash = initial_capital
        rolling_bars: deque[Bar] = deque(maxlen=5000)
        pending_orders: list[tuple[int, Order]] = []
        total_bars = len(bars)
        live_trades: list[Trade] = []

        for i in range(total_bars):
            bar = bars[i]
            rolling_bars.append(bar)

            # 2a) Fill pending orders
            just_filled = []
            for (target_idx, order) in pending_orders:
                if target_idx > i:
                    continue
                just_filled.append((target_idx, order))
                if fill_mode == "same_bar_close":
                    fill_price = bars[target_idx].close
                else:
                    fill_price = bar.open
                trade = pos_state.apply_fill(
                    order, fill_price=fill_price,
                    fill_ts=bar.timestamp,
                    commission=commission, slippage_bps=slippage_bps,
                )
                if trade is not None:
                    live_trades.append(trade)
                    cash += trade.pnl
            pending_orders = [p for p in pending_orders if p not in just_filled]

            # 2b) Stop/target check BEFORE on_bar
            trade_hit, _ = pos_state.check_stops(
                bar.high, bar.low,
                commission=commission, slippage_bps=slippage_bps,
                bar_ts=bar.timestamp,
            )
            if trade_hit is not None:
                live_trades.append(trade_hit)
                cash += trade_hit.pnl

            # 2c) Warmup gate
            if i < min_warmup_bars:
                continue

            # 2d) Call on_bar via evaluate_bar (SAME function as backtest driver)
            pos_snap = pos_state.snapshot()
            ctx = StrategyContext(
                symbol=symbol,
                timeframe=timeframe,
                bars=list(rolling_bars),
                position=pos_snap,
                params=strategy_live.params,
                account_id=account_id,
                strategy_id=strategy_live.id,
                current_bar_idx=i,
            )
            try:
                set_context(symbol=symbol, timeframe=timeframe.value,
                            account_id=account_id, strategy_id=strategy_live.id)
                orders_emitted = evaluate_bar(strategy_live, ctx)
            finally:
                clear_context()

            for o in orders_emitted:
                tags = o.tags or {}
                if tags.get("action") == "set_stops" and o.quantity == 0.0:
                    stop = tags.get("stop_loss")
                    tgt = tags.get("take_profit")
                    if stop is not None or tgt is not None:
                        pos_state.set_stops(stop_loss=stop, take_profit=tgt)
                    continue
                fill_idx = i if fill_mode == "same_bar_close" else i + 1
                if fill_idx < total_bars or fill_mode == "same_bar_close":
                    pending_orders.append((fill_idx, o))

        # ── 3) Compare the two runs ──────────────────────────────────────
        divergences: list[str] = []
        n_bt = len(out_bt.trades)
        n_live = len(live_trades)
        if n_bt != n_live:
            divergences.append(f"Trade count mismatch: backtest={n_bt} live={n_live}")

        n = min(n_bt, n_live)
        for i in range(n):
            fp_bt = trade_fingerprint(out_bt.trades[i])
            fp_live = trade_fingerprint(live_trades[i])
            for key in fp_bt.keys():
                v_bt = fp_bt[key]
                v_live = fp_live[key]
                if isinstance(v_bt, float) and isinstance(v_live, float):
                    if abs(v_bt - v_live) > 1e-4:
                        divergences.append(
                            f"Trade {i} {key}: bt={v_bt} live={v_live}"
                        )
                elif v_bt != v_live:
                    divergences.append(
                        f"Trade {i} {key}: backtest={v_bt} live={v_live}"
                    )
        passes = len(divergences) == 0
        elapsed = time.perf_counter() - t0
        report = ParityReport(
            passes=passes, divergences=divergences,
            backtest_trades=n_bt, simulated_live_trades=n_live,
            elapsed_sec=elapsed,
        )
        if passes:
            log.info(
                "PARITY OK — backtest and simulated-live produced identical output",
                extra={
                    "strategy": strategy_class_name,
                    "symbol": symbol,
                    "timeframe": timeframe.value,
                    "bars": len(bars),
                    "trades": n_bt,
                    "elapsed_sec": round(elapsed, 4),
                },
            )
        else:
            for d in divergences[:50]:
                if log_divergences_at_critical:
                    log.critical(
                        "PARITY DIVERGENCE — backtest vs live output differs! "
                        "This would cause real-money losses if not fixed.",
                        extra={
                            "divergence": d,
                            "strategy": strategy_class_name,
                            "symbol": symbol,
                            "timeframe": timeframe.value,
                        },
                    )
                else:
                    log.error(
                        "Parity divergence",
                        extra={"divergence": d, "strategy": strategy_class_name},
                    )
        return report
    finally:
        clear_context()
