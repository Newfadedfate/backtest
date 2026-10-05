"""Position state machine — FLAT/LONG/SHORT mutual exclusivity.

Tracks one position per (account, symbol, strategy) tuple.
Applies fills, stop/target/breakeven updates, emits Trade records on exit.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.logging_setup import get_logger, set_context
from app.core.schemas import (
    Order,
    OrderSide,
    OrderType,
    Position,
    PositionSide,
    Trade,
)

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class PositionState:
    """Single-position state machine: FLAT <-> LONG, FLAT <-> SHORT.

    Mutually exclusive per key (account_id, symbol, strategy_id).
    """

    def __init__(self, account_id: str, symbol: str, strategy_id: str) -> None:
        self.account_id = account_id
        self.symbol = symbol
        self.strategy_id = strategy_id
        self._position = Position(
            symbol=symbol,
            side=PositionSide.FLAT,
            quantity=0.0,
            avg_entry_price=0.0,
            account_id=account_id,
            strategy_id=strategy_id,
        )
        self.current_trade_entry_price: Optional[float] = None
        self.current_trade_entry_ts: Optional[datetime] = None
        self.current_trade_side: Optional[PositionSide] = None

    # ── Public snapshot (deep-ish copy for strategy context) ──────────

    def snapshot(self) -> Position:
        """Copy of current position for StrategyContext (never leak internal state)."""
        return Position(
            symbol=self._position.symbol,
            side=self._position.side,
            quantity=self._position.quantity,
            avg_entry_price=self._position.avg_entry_price,
            stop_loss=self._position.stop_loss,
            take_profit=self._position.take_profit,
            entry_timestamp=self._position.entry_timestamp,
            account_id=self._position.account_id,
            strategy_id=self._position.strategy_id,
        )

    @property
    def side(self) -> PositionSide:
        return self._position.side

    @property
    def quantity(self) -> float:
        return self._position.quantity

    # ── Order execution (apply fills, return trades if closed) ────────

    def apply_fill(
        self,
        order: Order,
        fill_price: float,
        fill_ts: Optional[datetime] = None,
        commission: float = 0.0,
        slippage_bps: float = 0.0,
    ) -> Optional[Trade]:
        """Execute an order fill at given price.

        Returns a Trade if this fill closed a round-trip, else None.
        Applies slippage_bps to adjust fill price slightly for realism (slippage on cost).
        """
        ts = fill_ts or _now()
        set_context(symbol=self.symbol, account_id=self.account_id, strategy_id=self.strategy_id)

        side_after_fill = self._position.side
        if order.side == OrderSide.BUY:
            if self._position.side == PositionSide.FLAT:
                side_after_fill = PositionSide.LONG
            elif self._position.side == PositionSide.LONG:
                side_after_fill = PositionSide.LONG  # add to long
            else:  # SHORT
                # Closing short
                if order.quantity >= self._position.quantity:
                    side_after_fill = PositionSide.FLAT if order.quantity == self._position.quantity else PositionSide.LONG
                else:
                    side_after_fill = PositionSide.SHORT
        else:  # SELL
            if self._position.side == PositionSide.FLAT:
                side_after_fill = PositionSide.SHORT
            elif self._position.side == PositionSide.SHORT:
                side_after_fill = PositionSide.SHORT
            else:  # LONG
                if order.quantity >= self._position.quantity:
                    side_after_fill = PositionSide.FLAT if order.quantity == self._position.quantity else PositionSide.SHORT
                else:
                    side_after_fill = PositionSide.LONG

        # Apply slippage: BUY fills slightly higher, SELL slightly lower
        slip = abs(fill_price) * max(0.0, slippage_bps) / 10_000.0
        if order.side == OrderSide.BUY:
            effective_price = fill_price + slip
        else:
            effective_price = fill_price - slip

        # Compute qty to close vs add
        close_qty = 0.0
        if self._position.side != PositionSide.FLAT:
            opposing = (
                (self._position.side == PositionSide.LONG and order.side == OrderSide.SELL)
                or (self._position.side == PositionSide.SHORT and order.side == OrderSide.BUY)
            )
            if opposing:
                close_qty = min(self._position.quantity, order.quantity)
        add_qty = order.quantity - close_qty

        trade: Optional[Trade] = None

        # Close partial/full
        if close_qty > 0.0:
            exit_price_per_unit = effective_price
            entry_price_per_unit = self._position.avg_entry_price
            if self._position.side == PositionSide.LONG:
                pnl_per_unit = exit_price_per_unit - entry_price_per_unit
            else:
                pnl_per_unit = entry_price_per_unit - exit_price_per_unit
            raw_pnl = pnl_per_unit * close_qty
            total_commission = commission * close_qty  # per-unit commission caller convention
            trade_id = f"TR-{ts.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

            log.info(
                "Trade closed (round-trip filled)",
                extra={
                    "trade_id": trade_id,
                    "side": self._position.side.value,
                    "qty": close_qty,
                    "entry": round(entry_price_per_unit, 6),
                    "exit": round(exit_price_per_unit, 6),
                    "raw_pnl": round(raw_pnl, 2),
                    "commission": round(total_commission, 2),
                    "net_pnl": round(raw_pnl - total_commission, 2),
                    "exit_reason": order.tags.get("exit_reason", "order"),
                },
            )
            trade = Trade(
                id=trade_id,
                symbol=self.symbol,
                side=self._position.side,
                quantity=close_qty,
                entry_price=entry_price_per_unit,
                exit_price=exit_price_per_unit,
                entry_timestamp=self.current_trade_entry_ts or ts,
                exit_timestamp=ts,
                pnl=raw_pnl - total_commission,
                commission=total_commission,
                slippage=slip * close_qty,
                account_id=self.account_id,
                strategy_id=self.strategy_id,
                exit_reason=order.tags.get("exit_reason"),
                tags={"entry_ts": self.current_trade_entry_ts.isoformat() if self.current_trade_entry_ts else None,
                      "avg_entry": self._position.avg_entry_price},
            )

            # Update position: avg entry unchanged for remaining qty (if any)
            self._position.quantity -= close_qty
            if self._position.quantity <= 1e-12:
                self._position.quantity = 0.0
                self._position.avg_entry_price = 0.0
                self._position.stop_loss = None
                self._position.take_profit = None
                self._position.entry_timestamp = None
                self._position.side = side_after_fill
                self.current_trade_entry_price = None
                self.current_trade_entry_ts = None
                self.current_trade_side = None
            else:
                # partial close: keep side and avg
                pass

        # Add to / open position
        if add_qty > 0.0:
            # weighted avg entry
            new_total = self._position.quantity + add_qty
            if self._position.quantity <= 0:
                new_avg = effective_price
            else:
                new_avg = (
                    (self._position.avg_entry_price * self._position.quantity)
                    + (effective_price * add_qty)
                ) / new_total
            was_flat = self._position.side == PositionSide.FLAT
            self._position.quantity = new_total
            self._position.avg_entry_price = new_avg
            self._position.side = side_after_fill
            if was_flat:
                self._position.entry_timestamp = ts
                self.current_trade_entry_price = effective_price
                self.current_trade_entry_ts = ts
                self.current_trade_side = side_after_fill
                log.info(
                    "Position opened",
                    extra={
                        "side": side_after_fill.value,
                        "qty": add_qty,
                        "entry": round(effective_price, 6),
                        "avg": round(new_avg, 6),
                    },
                )
            else:
                log.debug(
                    "Position added to",
                    extra={
                        "side": self._position.side.value,
                        "add_qty": add_qty,
                        "total_qty": new_total,
                        "avg": round(new_avg, 6),
                    },
                )

        return trade

    # ── Stop/Target management (per-bar check before strategy runs) ──

    def check_stops(
        self, bar_high: float, bar_low: float,
        commission: float = 0.0, slippage_bps: float = 0.0,
        bar_ts: Optional[datetime] = None,
    ) -> tuple[Optional[Trade], Optional[Order]]:
        """Check if current bar touched stop_loss or take_profit; emit closing Order.

        Called BEFORE on_bar by drivers. If touched, the returned Trade is the
        fill (bar's high/low used as stop/target fill price).

        bar_ts: timestamp of the bar being checked (historical bar time).
                If None, falls back to wall-clock datetime.now() (discouraged
                for backtesting — breaks parity determinism).
        """
        if self._position.side == PositionSide.FLAT:
            return None, None

        exit_order: Optional[Order] = None
        exit_reason: Optional[str] = None
        hit_price: Optional[float] = None

        if self._position.side == PositionSide.LONG:
            if self._position.take_profit is not None and bar_high >= self._position.take_profit:
                hit_price = self._position.take_profit
                exit_reason = "take_profit"
            elif self._position.stop_loss is not None and bar_low <= self._position.stop_loss:
                hit_price = self._position.stop_loss
                exit_reason = "stop_loss"
        else:  # SHORT
            if self._position.take_profit is not None and bar_low <= self._position.take_profit:
                hit_price = self._position.take_profit
                exit_reason = "take_profit"
            elif self._position.stop_loss is not None and bar_high >= self._position.stop_loss:
                hit_price = self._position.stop_loss
                exit_reason = "stop_loss"

        if hit_price is None:
            return None, None

        fill_ts = bar_ts or _now()

        log.info(
            "Stop/Target HIT — closing position before on_bar",
            extra={
                "trigger": exit_reason,
                "hit_price": round(hit_price, 6),
                "side": self._position.side.value,
                "bar_high": round(bar_high, 6),
                "bar_low": round(bar_low, 6),
                "stop": self._position.stop_loss,
                "target": self._position.take_profit,
            },
        )
        from app.core.schemas import Order as _Order
        exit_order = _Order(
            id=f"STOP-{int(time.time()*1000)}",
            symbol=self.symbol,
            side=OrderSide.SELL if self._position.side == PositionSide.LONG else OrderSide.BUY,
            type=OrderType.STOP,
            quantity=self._position.quantity,
            stop_price=hit_price,
            timestamp=fill_ts,
            account_id=self.account_id,
            strategy_id=self.strategy_id,
            tags={"exit_reason": exit_reason, "auto": True},
        )
        trade = self.apply_fill(
            exit_order, fill_price=hit_price,
            fill_ts=fill_ts,
            commission=commission, slippage_bps=slippage_bps,
        )
        return trade, exit_order

    # ── Convenience setters for strategy (mutate via copies) ──────────

    def set_stops(self, stop_loss: Optional[float] = None, take_profit: Optional[float] = None) -> None:
        self._position.stop_loss = stop_loss
        self._position.take_profit = take_profit
        log.debug(
            "Stops updated",
            extra={"stop_loss": stop_loss, "take_profit": take_profit},
        )
