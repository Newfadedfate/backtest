"""Deterministic mock data feed adapter.

Generates synthetic OHLC bars for any symbol/timeframe. Used for unit tests,
strategy prototyping, and UI development without needing a real data source.

The generator is seeded per (symbol, timeframe) so runs are reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from app.core.logging_setup import get_logger
from app.core.schemas import Bar, Tick, Timeframe
from app.core.utils import tf_to_timedelta, seed_for
from app.data.feed import DataFeed, register_adapter

log = get_logger(__name__)

_tf_to_timedelta = tf_to_timedelta
_seed_for = seed_for


@register_adapter("mock")
class MockAdapter(DataFeed):
    """Deterministic synthetic OHLCV feed.

    Generated data:
        - Random walk with 0.01% drift, 0.1% std per bar
        - OHLC derived from close, with 0.03% avg intrabar range
        - Uniform random volume 1k — 100k

    Usage::

        feed = MockAdapter()
        feed.connect()
        # 2024-01-01 -> 2024-02-01, 5-min bars for NIFTY
        bars = feed.get_historical("NIFTY", Timeframe.M5,
                                   start=datetime(2024,1,1),
                                   end=datetime(2024,2,1))
    """

    def __init__(self, base_price: float = 100.0) -> None:
        super().__init__()
        self.default_base_price = base_price
        self._bar_timer_task = None

    # ── Connection ────────────────────────────────────────────────────

    def connect(self, **kwargs) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.subscribed_symbols.clear()

    # ── Subscription ──────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: Optional[Timeframe] = None) -> None:
        self.subscribed_symbols.add(f"{symbol}:{timeframe or 'TICK'}")
        # TODO: implement async bar-close generation loop for live testing.
        # For now subscription is bookkeeping only.

    def unsubscribe(self, symbol: str, timeframe: Optional[Timeframe] = None) -> None:
        self.subscribed_symbols.discard(f"{symbol}:{timeframe or 'TICK'}")

    # ── Historical data generation ────────────────────────────────────

    def get_historical(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[Bar]:
        if not self.connected:
            raise RuntimeError("MockAdapter not connected. Call connect() first.")

        # Resolve boundaries
        now = datetime.utcnow().replace(tzinfo=__import__("datetime").timezone.utc)
        if end is None:
            end = now
        if start is None:
            start = end - timedelta(days=30)

        delta = _tf_to_timedelta(timeframe)
        if limit is None:
            # Calculate bar count from date range; cap at 200k to avoid explosion.
            limit = max(1, min(200_000, int((end - start) / delta)))
        else:
            limit = max(1, limit)

        rng = np.random.default_rng(_seed_for(symbol, timeframe))

        # Build timestamps
        # Anchor end to the last completed bar boundary. Always work in UTC.
        end_ts = pd.Timestamp(end)
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize("UTC")
        else:
            end_ts = end_ts.tz_convert("UTC")
        end_dt = end_ts.floor(delta)
        start_dt = end_dt - delta * (limit - 1)
        # start_dt is already tz-aware; don't pass tz= again to date_range or pandas will complain.
        timestamps = pd.date_range(start=start_dt, periods=limit, freq=delta)

        # Close prices: random walk with drift
        returns = rng.normal(loc=0.00001, scale=0.001, size=limit)
        close = self.default_base_price * np.exp(np.cumsum(returns))

        # Intrabar range: High > Max(O,C), Low < Min(O,C)
        open_ = np.concatenate([[close[0]], close[:-1]])
        mid_oc = (open_ + close) / 2.0
        spread = rng.uniform(0.0001, 0.001, size=limit) * mid_oc  # 0.01–0.1% range
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        volume = rng.uniform(1_000.0, 100_000.0, size=limit)

        bars: list[Bar] = []
        for i in range(limit):
            try:
                bars.append(Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=timestamps[i].to_pydatetime(),
                    open=float(open_[i]),
                    high=float(high[i]),
                    low=float(low[i]),
                    close=float(close[i]),
                    volume=float(volume[i]),
                ))
            except Exception as e:
                log.error(
                    "MockAdapter bar construction failed at index",
                    extra={"symbol": symbol, "timeframe": timeframe.value,
                           "index": i, "error": str(e)},
                    exc_info=True,
                )
                continue

        # Time filter (we may have over-generated above)
        bars = [b for b in bars if b.timestamp >= start and b.timestamp <= end]
        return bars
