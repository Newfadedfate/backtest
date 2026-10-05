"""Built-in strategy library. Extend this freely.

IMPORTANT: All strategies extend Strategy, implement only on_bar(context).
Never write backtest-specific or live-specific code in here.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.schemas import OrderSide, OrderType, PositionSide
from app.strategy.context import StrategyContext
from app.strategy.engine import Order, Strategy, register_strategy
from app.core.logging_setup import get_logger
from app.strategy.building_blocks import (
    SignalReentryLock,
    breakeven_shift,
    candle_range_adaptive_stop,
    fixed_take_profit,
    level_proximity_exit,
    price_band_filter,
)

log = get_logger(__name__)


@register_strategy("sma_crossover")
class SmaCrossoverStrategy(Strategy):
    """Simple Moving-Average Crossover.

    Params:
        fast_period (int):  fast SMA period (default 10)
        slow_period (int):  slow SMA period (default 30)
        position_size (float): fixed qty per trade (default 1.0)
        stop_loss_atr_mult (float): stop = entry - (ATR * mult) for long
        take_profit_atr_mult (float): target = entry + (ATR * mult) for long
    """

    def on_start(self, context: StrategyContext) -> None:
        # Parameter validation at startup — fail loud, not at bar 5000
        p = self.params
        if int(p.get("fast_period", 10)) >= int(p.get("slow_period", 30)):
            raise ValueError(
                f"sma_crossover: fast_period must be < slow_period "
                f"(got fast={p.get('fast_period')} slow={p.get('slow_period')})"
            )
        log.info(
            "sma_crossover strategy started",
            extra={"params": self.params,
                   "account_id": context.account_id,
                   "strategy_id": context.strategy_id,
                   "symbol": context.symbol},
        )

    def on_bar(self, context: StrategyContext) -> list[Order]:
        fast = int(self.params.get("fast_period", 10))
        slow = int(self.params.get("slow_period", 30))
        size = float(self.params.get("position_size", 1.0))
        sl_mult = float(self.params.get("stop_loss_atr_mult", 1.5))
        tp_mult = float(self.params.get("take_profit_atr_mult", 3.0))

        f_sma = context.sma(fast)
        s_sma = context.sma(slow)
        # Need at least (slow + 1) bars to check a crossover from prior bar
        need = slow + 1
        if context.lookback < need or f_sma is None or s_sma is None:
            return []

        # Previous bar SMAs (one bar earlier)
        prev_bars = context.bars[:-1]
        prev_ctx = StrategyContext(
            symbol=context.symbol, timeframe=context.timeframe,
            bars=prev_bars, position=context.position, params=self.params,
            account_id=context.account_id, strategy_id=context.strategy_id,
        )
        prev_fast = prev_ctx.sma(fast)
        prev_slow = prev_ctx.sma(slow)
        if prev_fast is None or prev_slow is None:
            return []

        atr14 = context.atr(14) or 0.0
        orders: list[Order] = []

        # Crossover = LONG entry
        if prev_fast <= prev_slow and f_sma > s_sma:
            log.debug(
                "Signal: FAST SMA crossed ABOVE SLOW — long candidate",
                extra={
                    "prev_fast": round(prev_fast, 4), "prev_slow": round(prev_slow, 4),
                    "fast": round(f_sma, 4), "slow": round(s_sma, 4),
                    "atr": round(atr14, 4),
                },
            )
            if context.flat:
                entry_price = context.bar.close
                stop = entry_price - atr14 * sl_mult
                tgt = entry_price + atr14 * tp_mult
                from app.strategy.engine import Strategy as _S
                buy = _S.buy_market(context, size, tags={
                    "entry_reason": "sma_cross_up",
                    "stop_loss_plan": round(stop, 4),
                    "take_profit_plan": round(tgt, 4),
                })
                orders.append(buy)
                # Also emit SET_STOPS tag signal (caller will apply to position)
                orders.append(_S._order(context, side=OrderSide.BUY, quantity=0.0,
                                        type=OrderType.MARKET,
                                        tags={"action": "set_stops",
                                              "stop_loss": stop,
                                              "take_profit": tgt}))
            elif context.short:
                # Flat + reverse on opposite crossover
                from app.strategy.engine import Strategy as _S
                orders.append(_S.buy_market(context, size + context.position.quantity,
                                            tags={"entry_reason": "sma_cross_up_reverse"}))

        # Crossunder = SHORT entry / long exit
        if prev_fast >= prev_slow and f_sma < s_sma:
            log.debug(
                "Signal: FAST SMA crossed BELOW SLOW — short candidate",
                extra={
                    "prev_fast": round(prev_fast, 4), "prev_slow": round(prev_slow, 4),
                    "fast": round(f_sma, 4), "slow": round(s_sma, 4),
                },
            )
            if context.flat:
                entry_price = context.bar.close
                stop = entry_price + atr14 * sl_mult
                tgt = entry_price - atr14 * tp_mult
                from app.strategy.engine import Strategy as _S
                sell = _S.sell_market(context, size, tags={
                    "entry_reason": "sma_cross_down",
                    "stop_loss_plan": round(stop, 4),
                    "take_profit_plan": round(tgt, 4),
                })
                orders.append(sell)
                orders.append(_S._order(context, side=OrderSide.SELL, quantity=0.0,
                                        type=OrderType.MARKET,
                                        tags={"action": "set_stops",
                                              "stop_loss": stop,
                                              "take_profit": tgt}))
            elif context.long:
                from app.strategy.engine import Strategy as _S
                orders.append(_S.sell_market(context, context.position.quantity + size,
                                             tags={"exit_reason": "sma_cross_down_reverse",
                                                   "entry_reason": "sma_cross_down_short"}))

        return orders


@register_strategy("building_blocks_demo")
class BuildingBlocksStrategy(Strategy):
    """Demo strategy that exercises ALL 6 of the Phase 2.2 reusable building blocks.

    Signal: SMA crossover (same as above).
    Every block is configurable via strategy params with sensible defaults matching
    the user's original rule-set hints.

    Blocks applied:
      1. price_band_filter()        — reject entries outside [lower, upper] band
      2. candle_range_adaptive_stop() — set stop using narrow-range tight offset rule
      3. breakeven_shift()          — move stop to entry+buffer when profit >= trigger
      4. fixed_take_profit()        — TP via risk multiple (3R default)
      5. level_proximity_exit()     — close if price touches day-high with reversal
      6. SignalReentryLock          — after stop loss, wait for opposite cross before re-entry
    """

    def __init__(self, strategy_id: str, params: Optional[dict] = None,
                 name: Optional[str] = None) -> None:
        super().__init__(strategy_id, params, name)
        self.lock: SignalReentryLock = SignalReentryLock()
        self._prev_day_high: Optional[float] = None
        self._entry_by_side: dict[str, dict[str, Any]] = {}

    def on_start(self, context: StrategyContext) -> None:
        p = self.params
        if int(p.get("fast_period", 10)) >= int(p.get("slow_period", 30)):
            raise ValueError(
                f"building_blocks_demo: fast must be < slow "
                f"(got fast={p.get('fast_period')} slow={p.get('slow_period')})"
            )
        log.info(
            "building_blocks_demo started",
            extra={"params": self.params, "account_id": context.account_id,
                   "strategy_id": context.strategy_id, "symbol": context.symbol},
        )

    def _update_day_levels(self, ctx: StrategyContext) -> None:
        bar = ctx.bar
        if self._prev_day_high is None:
            self._prev_day_high = bar.high
        else:
            # Rolling "previous day high" — simulate by tracking across all bars so far,
            # user can replace with proper calendar day high/low in real deployment.
            self._prev_day_high = max(self._prev_day_high, bar.high)

    def on_bar(self, context: StrategyContext) -> list[Order]:
        self._update_day_levels(context)
        fast = int(self.params.get("fast_period", 10))
        slow = int(self.params.get("slow_period", 30))
        size = float(self.params.get("position_size", 1.0))
        # Block 1 defaults — band is set very wide by default so it's essentially
        # a no-op unless the user explicitly configures tight bands.
        band_lower = self.params.get("band_lower")
        band_upper = self.params.get("band_upper")
        band_mode = self.params.get("band_mode", "inside_ok")
        # Block 2 defaults — per rule-set hints: range <5 → tight 5 pts, else loose 1 pt
        rng_thresh = float(self.params.get("range_threshold_pts", 5.0))
        tight_off = float(self.params.get("tight_offset_pts", 5.0))
        loose_off = float(self.params.get("loose_offset_pts", 1.0))
        # Block 3: breakeven trigger (pts of profit) + buffer
        be_trigger = float(self.params.get("be_trigger_pts", 2.0))
        be_buffer = float(self.params.get("be_buffer_pts", 0.25))
        # Block 4: TP as risk multiple (3R default)
        tp_r_mult = float(self.params.get("tp_r_multiple", 3.0))
        # Block 5: level-proximity exit near day high
        tol_bps = float(self.params.get("level_tolerance_bps", 20.0))
        rej_ratio = float(self.params.get("min_rejection_body_ratio", 0.6))

        need = slow + 1
        if context.lookback < need:
            return []
        f_sma = context.sma(fast)
        s_sma = context.sma(slow)
        if f_sma is None or s_sma is None:
            return []
        prev_bars = context.bars[:-1]
        prev_ctx = StrategyContext(
            symbol=context.symbol, timeframe=context.timeframe,
            bars=prev_bars, position=context.position, params=self.params,
            account_id=context.account_id, strategy_id=context.strategy_id,
        )
        prev_fast = prev_ctx.sma(fast)
        prev_slow = prev_ctx.sma(slow)
        if prev_fast is None or prev_slow is None:
            return []

        bar = context.bar
        orders: list[Order] = []
        cross_up = prev_fast <= prev_slow and f_sma > s_sma
        cross_down = prev_fast >= prev_slow and f_sma < s_sma

        # Release re-entry locks if the opposite cross to the original entry arrives.
        if cross_up:
            self.lock.release("SHORT", when="opposite_crossover")
        if cross_down:
            self.lock.release("LONG", when="opposite_crossover")

        # ── Block 5: Level-proximity exit check for positions we hold ──
        if context.long and self._prev_day_high is not None:
            lv = level_proximity_exit(
                side="LONG", bar_open=bar.open, bar_high=bar.high,
                bar_low=bar.low, bar_close=bar.close,
                level_price=self._prev_day_high,
                tolerance_bps=tol_bps, min_rejection_body_ratio=rej_ratio,
            )
            if lv.triggered:
                log.info("Level-proximity EXIT (LONG)",
                         extra={"reason": lv.reason, "dist_pts": round(lv.distance_pts, 4)})
                orders.append(Strategy.sell_market(
                    context, context.position.quantity,
                    tags={"exit_reason": "level_proximity_reversal"},
                ))
                self.lock.acquire("LONG", wait_for="opposite_crossover",
                                  exit_reason="level_proximity_reversal",
                                  meta={"ts": bar.timestamp.isoformat()},
                                  acquired_at=bar.timestamp)
        if context.short and self._prev_day_high is not None:
            lv = level_proximity_exit(
                side="SHORT", bar_open=bar.open, bar_high=bar.high,
                bar_low=bar.low, bar_close=bar.close,
                level_price=self._prev_day_high,
                tolerance_bps=tol_bps, min_rejection_body_ratio=rej_ratio,
            )
            if lv.triggered:
                log.info("Level-proximity EXIT (SHORT)",
                         extra={"reason": lv.reason, "dist_pts": round(lv.distance_pts, 4)})
                orders.append(Strategy.buy_market(
                    context, context.position.quantity,
                    tags={"exit_reason": "level_proximity_reversal"},
                ))
                self.lock.acquire("SHORT", wait_for="opposite_crossover",
                                  exit_reason="level_proximity_reversal",
                                  meta={"ts": bar.timestamp.isoformat()},
                                  acquired_at=bar.timestamp)

        # ── Block 3: Breakeven shift for positions we hold ──
        if context.long and context.position.avg_entry_price:
            new_stop = breakeven_shift(
                entry_price=context.position.avg_entry_price,
                side="LONG", current_price=bar.close,
                trigger_unrealized_pts=be_trigger, buffer_pts=be_buffer,
                current_stop=context.position.stop_loss,
            )
            if new_stop is not None:
                log.debug("Breakeven shift applied (LONG)",
                          extra={"new_stop": round(new_stop, 4)})
                orders.append(Strategy._order(
                    context, side=OrderSide.BUY, quantity=0.0, type=OrderType.MARKET,
                    tags={"action": "set_stops", "stop_loss": new_stop,
                          "take_profit": context.position.take_profit or 0.0,
                          "reason": "breakeven_shift"},
                ))
        if context.short and context.position.avg_entry_price:
            new_stop = breakeven_shift(
                entry_price=context.position.avg_entry_price,
                side="SHORT", current_price=bar.close,
                trigger_unrealized_pts=be_trigger, buffer_pts=be_buffer,
                current_stop=context.position.stop_loss,
            )
            if new_stop is not None:
                log.debug("Breakeven shift applied (SHORT)",
                          extra={"new_stop": round(new_stop, 4)})
                orders.append(Strategy._order(
                    context, side=OrderSide.SELL, quantity=0.0, type=OrderType.MARKET,
                    tags={"action": "set_stops", "stop_loss": new_stop,
                          "take_profit": context.position.take_profit or 0.0,
                          "reason": "breakeven_shift"},
                ))

        # ── Signal: cross-up = LONG candidate ──
        if cross_up:
            # Block 1: band filter
            if not price_band_filter(bar.close, band_lower, band_upper, mode=band_mode):
                log.debug("LONG candidate rejected by price_band_filter")
            # Block 6: re-entry lock check
            elif not self.lock.allowed("LONG"):
                log.debug("LONG candidate rejected by reentry_lock")
            elif context.flat:
                entry = bar.close
                # Block 2: candle-range adaptive stop
                stop = candle_range_adaptive_stop(
                    "LONG", bar_low=bar.low, bar_high=bar.high,
                    range_threshold_pts=rng_thresh, tight_offset_pts=tight_off,
                    loose_offset_pts=loose_off,
                )
                risk = abs(entry - stop)
                # Block 4: 3R fixed take-profit via risk multiple
                tgt = fixed_take_profit(entry, "LONG",
                                        risk_r_multiple=tp_r_mult, risk_pts=risk) or 0.0
                orders.append(Strategy.buy_market(context, size, tags={
                    "entry_reason": "bb_cross_up",
                    "stop_loss_plan": round(stop, 4),
                    "take_profit_plan": round(tgt, 4),
                    "risk_pts": round(risk, 4),
                    "r_ratio": tp_r_mult,
                }))
                orders.append(Strategy._order(
                    context, side=OrderSide.BUY, quantity=0.0, type=OrderType.MARKET,
                    tags={"action": "set_stops", "stop_loss": stop, "take_profit": tgt},
                ))
            elif context.short:
                orders.append(Strategy.buy_market(context, size + context.position.quantity,
                                                  tags={"entry_reason": "bb_cross_up_reverse"}))

        if cross_down:
            if not price_band_filter(bar.close, band_lower, band_upper, mode=band_mode):
                log.debug("SHORT candidate rejected by price_band_filter")
            elif not self.lock.allowed("SHORT"):
                log.debug("SHORT candidate rejected by reentry_lock")
            elif context.flat:
                entry = bar.close
                stop = candle_range_adaptive_stop(
                    "SHORT", bar_low=bar.low, bar_high=bar.high,
                    range_threshold_pts=rng_thresh, tight_offset_pts=tight_off,
                    loose_offset_pts=loose_off,
                )
                risk = abs(stop - entry)
                tgt = fixed_take_profit(entry, "SHORT",
                                        risk_r_multiple=tp_r_mult, risk_pts=risk) or 0.0
                orders.append(Strategy.sell_market(context, size, tags={
                    "entry_reason": "bb_cross_down",
                    "stop_loss_plan": round(stop, 4),
                    "take_profit_plan": round(tgt, 4),
                    "risk_pts": round(risk, 4),
                    "r_ratio": tp_r_mult,
                }))
                orders.append(Strategy._order(
                    context, side=OrderSide.SELL, quantity=0.0, type=OrderType.MARKET,
                    tags={"action": "set_stops", "stop_loss": stop, "take_profit": tgt},
                ))
            elif context.long:
                orders.append(Strategy.sell_market(context, context.position.quantity + size,
                                                   tags={"exit_reason": "bb_cross_down_reverse",
                                                         "entry_reason": "bb_cross_down_short"}))

        return orders
