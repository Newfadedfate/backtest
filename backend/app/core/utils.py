"""Common utility helpers shared across modules."""

from __future__ import annotations

from datetime import timedelta

from app.core.schemas import Timeframe


def tf_to_timedelta(tf: Timeframe) -> timedelta:
    """Convert Timeframe enum to a timedelta representing bar cadence.

    Note: some timeframes (W1, MN1) are approximations. Only used for validation
    purposes like gap detection, where an approximation is acceptable.
    """
    mapping = {
        Timeframe.S1: timedelta(seconds=1),
        Timeframe.S5: timedelta(seconds=5),
        Timeframe.S15: timedelta(seconds=15),
        Timeframe.S30: timedelta(seconds=30),
        Timeframe.M1: timedelta(minutes=1),
        Timeframe.M3: timedelta(minutes=3),
        Timeframe.M5: timedelta(minutes=5),
        Timeframe.M10: timedelta(minutes=10),
        Timeframe.M15: timedelta(minutes=15),
        Timeframe.M30: timedelta(minutes=30),
        Timeframe.H1: timedelta(hours=1),
        Timeframe.H2: timedelta(hours=2),
        Timeframe.H4: timedelta(hours=4),
        Timeframe.D1: timedelta(days=1),
        Timeframe.W1: timedelta(weeks=1),
        Timeframe.MN1: timedelta(days=30),
        Timeframe.TICK: timedelta(milliseconds=1),
    }
    return mapping.get(tf, timedelta(minutes=1))


def seed_for(symbol: str, tf: Timeframe) -> int:
    """Deterministic RNG seed per (symbol, timeframe)."""
    return abs(hash(f"{symbol}:{tf.value}")) % (2**32)
