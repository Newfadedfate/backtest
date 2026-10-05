"""Reusable Strategy Building Blocks — Phase 2.2.

Parameterized defaults match the patterns the user described in their build spec
(price band filter, candle-range adaptive stop, breakeven shift, fixed TP,
level-proximity reversal exits, signal re-entry locks). Every value is
parameter-overridable so the user can plug in their exact rule constants.

Shared by backtest AND live strategies. Import in a strategy:
    from app.strategy.building_blocks import (
        price_band_filter, candle_range_adaptive_stop,
        breakeven_shift, fixed_take_profit, level_proximity_exit,
        SignalReentryLock,
    )

Then in on_bar(context):
    if not price_band_filter(context.close, lb, ub, mode="inside_ok"):
        return []
    stop = candle_range_adaptive_stop("LONG", bar.low, bar.high, rng_thresh=5, tight=5, loose=1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from app.core.logging_setup import get_logger
from app.core.schemas import PositionSide

log = get_logger(__name__)

BandMode = Literal["inside_ok", "outside_ok", "above_only", "below_only"]


# ── 1. Price Band Entry Filter ──────────────────────────────────────────────
def price_band_filter(
    price: float,
    lower_band: Optional[float] = None,
    upper_band: Optional[float] = None,
    mode: BandMode = "inside_ok",
) -> bool:
    """Is the price in an acceptable region for entry?

    Modes:
        inside_ok:  lower_band <= price <= upper_band → allow
        outside_ok: price <= lower_band OR price >= upper_band → allow
        above_only: price >= lower_band → allow (upper bound ignored if None)
        below_only: price <= upper_band → allow (lower bound ignored if None)

    If a band is None that bound is unconstrained.
    """
    if mode == "inside_ok":
        lo_ok = lower_band is None or price >= lower_band
        hi_ok = upper_band is None or price <= upper_band
        return lo_ok and hi_ok
    elif mode == "outside_ok":
        below = lower_band is not None and price <= lower_band
        above = upper_band is not None and price >= upper_band
        if lower_band is None and upper_band is None:
            return True
        return below or above
    elif mode == "above_only":
        return lower_band is None or price >= lower_band
    elif mode == "below_only":
        return upper_band is None or price <= upper_band
    raise ValueError(f"price_band_filter: unknown mode '{mode}'")


# ── 2. Candle-Range-Adaptive Stop Loss ──────────────────────────────────────
def candle_range_adaptive_stop(
    side: str | PositionSide,
    bar_low: float,
    bar_high: float,
    range_threshold_pts: float = 5.0,
    tight_offset_pts: float = 5.0,
    loose_offset_pts: float = 1.0,
) -> float:
    """Adaptive stop: tight offset for narrow-range bars, loose offset for wide bars.

    Exact pattern from user spec hint:
        if candle range (high-low) < threshold → stop placed tight (offset=5)
        else → stop placed loose (offset=1) from the reference level.

    side=LONG  → stop placed BELOW bar_low (offset subtracted)
    side=SHORT → stop placed ABOVE bar_high (offset added)

    Returns absolute stop price.
    """
    candle_range = bar_high - bar_low
    offset = tight_offset_pts if candle_range < range_threshold_pts else loose_offset_pts
    s = side.value if isinstance(side, PositionSide) else side
    if s.upper() in ("LONG", "BUY"):
        return bar_low - offset
    elif s.upper() in ("SHORT", "SELL"):
        return bar_high + offset
    raise ValueError(f"candle_range_adaptive_stop: unknown side '{side}'")


# ── 3. Breakeven Stop-Loss Shift ────────────────────────────────────────────
def breakeven_shift(
    entry_price: float,
    side: str | PositionSide,
    current_price: float,
    trigger_unrealized_pts: float,
    buffer_pts: float = 0.0,
    current_stop: Optional[float] = None,
) -> Optional[float]:
    """Move stop to entry (+buffer) once unrealized pnl >= trigger.

    Returns NEW stop price if breakeven trigger fired, else None.
    Won't move stop BACKWARDS — only returns a value if it's strictly better
    than current_stop (for longs: higher stop = better).
    """
    s = side.value if isinstance(side, PositionSide) else side
    if s.upper() in ("LONG", "BUY"):
        unrealized = current_price - entry_price
        new_stop = entry_price + buffer_pts
        if unrealized >= trigger_unrealized_pts:
            if current_stop is None or new_stop > current_stop:
                return new_stop
    elif s.upper() in ("SHORT", "SELL"):
        unrealized = entry_price - current_price
        new_stop = entry_price - buffer_pts
        if unrealized >= trigger_unrealized_pts:
            if current_stop is None or new_stop < current_stop:
                return new_stop
    else:
        raise ValueError(f"breakeven_shift: unknown side '{side}'")
    return None


# ── 4. Fixed-Point Take-Profit Targets ──────────────────────────────────────
def fixed_take_profit(
    entry_price: float,
    side: str | PositionSide,
    absolute_price: Optional[float] = None,
    risk_r_multiple: Optional[float] = None,
    risk_pts: Optional[float] = None,
) -> Optional[float]:
    """Absolute-price TP target OR risk-multiple TP. Supply one mode only.

    - absolute_price → direct target level
    - risk_r_multiple + risk_pts → target = entry ± (R * mult)
      (e.g. entry=100, risk=2 pts, R=3 → 106 for long)

    Returns target price or None if not enough info.
    """
    s = side.value if isinstance(side, PositionSide) else side
    if absolute_price is not None:
        return absolute_price
    if risk_r_multiple is not None and risk_pts is not None:
        width = risk_pts * risk_r_multiple
        if s.upper() in ("LONG", "BUY"):
            return entry_price + width
        elif s.upper() in ("SHORT", "SELL"):
            return entry_price - width
        raise ValueError(f"fixed_take_profit: unknown side '{side}'")
    return None


# ── 5. Level-Proximity Reversal Exits ───────────────────────────────────────
@dataclass
class LevelProximityResult:
    triggered: bool
    reason: str
    distance_pts: float
    body_ratio: float


def level_proximity_exit(
    side: str | PositionSide,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    level_price: float,
    tolerance_bps: float = 20.0,
    min_rejection_body_ratio: float = 0.6,
) -> LevelProximityResult:
    """Exit a position when price pushes to a key level and REVERSES.

    2-part condition:
        1. price pushed WITHIN tolerance_bps of level_price during this bar
           (high touched level from below for long / low touched from above for short)
        2. the bar body closed AWAY from the level by at least
           min_rejection_body_ratio of the bar's total range (wick-and-reverse)

    level_price: typically day high / day low / day open / weekly hi/lo.
    tolerance_bps: 100 bps = 1%. E.g. 20 bps = 0.2% window around level.
    min_rejection_body_ratio: 0.6 means 60%+ of bar range is body closing
       away from level → genuine rejection, not just touching.

    Returns LevelProximityResult(triggered, reason, distance_pts, body_ratio).
    """
    s = side.value if isinstance(side, PositionSide) else side
    tolerance_pts = level_price * (tolerance_bps / 10_000.0)
    full_range = bar_high - bar_low
    body = abs(bar_close - bar_open)
    body_ratio = (body / full_range) if full_range > 1e-9 else 0.0

    if s.upper() in ("LONG", "BUY"):
        # Price pushed UP near level (high touched), closed AWAY (down) = rejection
        touched = bar_high >= level_price - tolerance_pts
        closed_away = bar_close < bar_high - (full_range * min_rejection_body_ratio)
        distance_pts = abs(bar_high - level_price)
        triggered = bool(touched and closed_away and body_ratio >= min_rejection_body_ratio)
        reason = (
            f"long-proximity: high={bar_high:.4f} near level={level_price:.4f} "
            f"(tol±{tolerance_pts:.4f}), rejection_body_ratio={body_ratio:.2f}"
        )
    elif s.upper() in ("SHORT", "SELL"):
        touched = bar_low <= level_price + tolerance_pts
        closed_away = bar_close > bar_low + (full_range * min_rejection_body_ratio)
        distance_pts = abs(bar_low - level_price)
        triggered = bool(touched and closed_away and body_ratio >= min_rejection_body_ratio)
        reason = (
            f"short-proximity: low={bar_low:.4f} near level={level_price:.4f} "
            f"(tol±{tolerance_pts:.4f}), rejection_body_ratio={body_ratio:.2f}"
        )
    else:
        raise ValueError(f"level_proximity_exit: unknown side '{side}'")
    return LevelProximityResult(
        triggered=triggered,
        reason=reason,
        distance_pts=distance_pts,
        body_ratio=body_ratio,
    )


# ── 6. Signal Re-Entry Lock ─────────────────────────────────────────────────
@dataclass
class ReentryLock:
    """Per-side lock preventing re-entry until ORIGINAL exit signal's opposite
    condition happens — NOT just any flat state.

    Usage (inside your strategy on_start / on_bar):
        # in on_start:
        self.lock = SignalReentryLock()

        # in on_bar, after a STOP-LOSS exit for side=LONG with exit_reason="stop_loss":
        self.lock.acquire("LONG", wait_for="opposite_crossover", meta={"ts": ctx.timestamp})

        # later, after NEW opposite signal arrives (e.g. cross DOWN, which was
        # the opposite of what triggered the LONG entry):
        self.lock.release("LONG", when="opposite_crossover")

        # BEFORE emitting any new entry order, check:
        if not self.lock.allowed("LONG"):
            return []
    """

    locks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def acquire(
        self,
        side: str | PositionSide,
        wait_for: str,
        exit_reason: Optional[str] = None,
        meta: Optional[dict] = None,
        acquired_at: Optional[datetime] = None,
    ) -> None:
        """Lock re-entry for `side` until `wait_for` condition releases it.

        Pass `acquired_at` as the bar timestamp so locks stay deterministic
        across backtest vs live (never stamp with wall-clock now()).
        """
        s = (side.value if isinstance(side, PositionSide) else side).upper()
        ts = acquired_at.isoformat() if acquired_at is not None else None
        self.locks[s] = {
            "wait_for": wait_for,
            "exit_reason": exit_reason,
            "acquired_at": ts,
            "meta": meta or {},
        }
        log.info(
            "ReentryLock ACQUIRED",
            extra={"side": s, "wait_for": wait_for,
                   "exit_reason": exit_reason, "meta": meta},
        )

    def release(self, side: str | PositionSide, when: str) -> bool:
        """Release the lock IFF `when` matches the original `wait_for` condition.

        Returns True if released (or already unlocked). False if mismatched.
        """
        s = (side.value if isinstance(side, PositionSide) else side).upper()
        if s not in self.locks:
            return True
        lock = self.locks[s]
        if lock["wait_for"] == when:
            del self.locks[s]
            log.info("ReentryLock RELEASED", extra={"side": s, "when": when})
            return True
        log.warning(
            "ReentryLock NOT released — condition mismatch",
            extra={"side": s, "got_when": when, "expected": lock["wait_for"]},
        )
        return False

    def allowed(self, side: str | PositionSide) -> bool:
        """Is opening a NEW position on this side currently allowed?"""
        s = (side.value if isinstance(side, PositionSide) else side).upper()
        return s not in self.locks

    def state(self) -> dict[str, Any]:
        """Snapshot for UI/debugging."""
        return {s: dict(v) for s, v in self.locks.items()}


# Re-export the class name user's build-spec wording uses:
SignalReentryLock = ReentryLock
