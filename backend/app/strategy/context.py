"""Strategy Context — rolling window of bars + position snapshot (read-only).

Shared by backtest AND live drivers. Strategies ONLY see this shape;
they cannot know or care if they're being backtested or run live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from app.core.logging_setup import get_logger
from app.core.schemas import Bar, Position, PositionSide, Timeframe

log = get_logger(__name__)


@dataclass
class StrategyContext:
    """Read-only context passed to strategy.on_bar() — one per account/symbol/strategy per bar.

    Snapshots, no side effects from mutating context fields (strategies do NOT get write access to state).
    """
    symbol: str
    timeframe: Timeframe
    bars: list[Bar]
    position: Position
    params: dict[str, Any]
    account_id: str
    strategy_id: str
    current_bar_idx: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    # ── Quick accessors ────────────────────────────────────────────────

    @property
    def bar(self) -> Bar:
        if not self.bars:
            raise RuntimeError("StrategyContext.bars empty")
        return self.bars[-1]

    @property
    def timestamp(self) -> datetime:
        return self.bar.timestamp

    @property
    def position_side(self) -> PositionSide:
        return self.position.side

    @property
    def flat(self) -> bool:
        return self.position.side == PositionSide.FLAT

    @property
    def long(self) -> bool:
        return self.position.side == PositionSide.LONG

    @property
    def short(self) -> bool:
        return self.position.side == PositionSide.SHORT

    @property
    def close(self) -> float:
        return self.bar.close

    @property
    def lookback(self) -> int:
        return len(self.bars)

    # ── Rolling series helpers (pure, no side effects) ─────────────────

    def closes(self, n: Optional[int] = None) -> list[float]:
        if n is None:
            return [b.close for b in self.bars]
        return [b.close for b in self.bars[-n:]]

    def highs(self, n: Optional[int] = None) -> list[float]:
        if n is None:
            return [b.high for b in self.bars]
        return [b.high for b in self.bars[-n:]]

    def lows(self, n: Optional[int] = None) -> list[float]:
        if n is None:
            return [b.low for b in self.bars]
        return [b.low for b in self.bars[-n:]]

    def opens(self, n: Optional[int] = None) -> list[float]:
        if n is None:
            return [b.open for b in self.bars]
        return [b.open for b in self.bars[-n:]]

    def volumes(self, n: Optional[int] = None) -> list[Optional[float]]:
        if n is None:
            return [b.volume for b in self.bars]
        return [b.volume for b in self.bars[-n:]]

    # ── Indicator helpers (pure) ──────────────────────────────────────

    def _require_min_history(self, n: int) -> bool:
        return len(self.bars) < n

    def sma(self, period: int) -> Optional[float]:
        """Simple moving average of closes."""
        if self._require_min_history(period):
            return None
        try:
            closes = self.closes(period)
            return sum(closes) / len(closes)
        except Exception as e:
            log.warning("SMA calculation failed",
                        extra={"symbol": self.symbol, "period": period, "error": str(e)})
            return None

    def atr(self, period: int = 14) -> Optional[float]:
        """Average True Range (simple)."""
        if len(self.bars) < period + 1:
            return None
        try:
            trs: list[float] = []
            for i in range(len(self.bars) - period, len(self.bars)):
                cur = self.bars[i]
                prev = self.bars[i - 1]
                high_low = cur.high - cur.low
                high_prev_close = abs(cur.high - prev.close)
                low_prev_close = abs(cur.low - prev.close)
                trs.append(max(high_low, high_prev_close, low_prev_close))
            return sum(trs) / len(trs) if trs else None
        except Exception as e:
            log.warning("ATR calc failed",
                        extra={"symbol": self.symbol, "period": period, "error": str(e)})
            return None

    def df(self) -> pd.DataFrame:
        """Return bars as a pandas DataFrame for vectorized indicator work."""
        return pd.DataFrame([b.model_dump() for b in self.bars])
