from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ──────────────────────────────────────────────────────────
# Market data schemas
# ──────────────────────────────────────────────────────────

class Timeframe(str, Enum):
    TICK = "T"
    S1 = "1s"
    S5 = "5s"
    S15 = "15s"
    S30 = "30s"
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"
    MN1 = "1mo"


class Tick(BaseModel):
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    volume: Optional[float] = None


class Bar(BaseModel):
    """One OHLCV bar. Single schema shared by live + historical data."""
    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None

    @field_validator("high")
    @classmethod
    def high_ge_others(cls, v: float, info) -> float:
        data = info.data
        if v < max(data.get("open", v), data.get("close", v), data.get("low", v)):
            raise ValueError("high must be >= open, close, low")
        return v

    @field_validator("low")
    @classmethod
    def low_le_others(cls, v: float, info) -> float:
        data = info.data
        if v > min(data.get("open", v), data.get("close", v), data.get("high", v)):
            raise ValueError("low must be <= open, close, high")
        return v


# ──────────────────────────────────────────────────────────
# Order / position schemas
# ──────────────────────────────────────────────────────────

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"

class PositionSide(str, Enum):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class Order(BaseModel):
    id: str
    symbol: str
    side: OrderSide
    type: OrderType
    quantity: float
    price: Optional[float] = None  # for LIMIT / STOP
    stop_price: Optional[float] = None
    timestamp: datetime
    account_id: str = "default"
    strategy_id: Optional[str] = None
    tags: dict = Field(default_factory=dict)


class Position(BaseModel):
    symbol: str
    side: PositionSide
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    entry_timestamp: Optional[datetime] = None
    account_id: str = "default"
    strategy_id: Optional[str] = None


class Trade(BaseModel):
    """Completed round-trip trade."""
    id: str
    symbol: str
    side: PositionSide
    quantity: float
    entry_price: float
    exit_price: float
    entry_timestamp: datetime
    exit_timestamp: datetime
    pnl: float
    commission: float = 0.0
    slippage: float = 0.0
    account_id: str = "default"
    strategy_id: Optional[str] = None
    exit_reason: Optional[str] = None
    tags: dict = Field(default_factory=dict)


class AccountState(BaseModel):
    account_id: str = "default"
    initial_capital: float
    current_capital: float
    available_capital: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    positions: list[Position] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────
# Strategy schemas
# ──────────────────────────────────────────────────────────

class StrategyConfig(BaseModel):
    id: str
    name: str
    parameters: dict = Field(default_factory=dict)
    symbols: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=lambda: ["default"])


# ──────────────────────────────────────────────────────────
# Data validation schemas
# ──────────────────────────────────────────────────────────

class DataValidationIssue(BaseModel):
    severity: str = "warning"  # warning | error
    type: str  # gap | duplicate | ohlc_sanity | missing_field
    symbol: str
    timeframe: str
    timestamp: Optional[datetime] = None
    message: str
    raw: Optional[dict] = None


class DataValidationReport(BaseModel):
    symbol: str
    timeframe: str
    bar_count: int = 0
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    issues: list[DataValidationIssue] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0


# ──────────────────────────────────────────────────────────
# Backtest schemas
# ──────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    strategy_id: str
    strategy_params: dict = Field(default_factory=dict)
    symbols: list[str]
    timeframe: Timeframe
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    initial_capital: dict[str, float] = Field(default_factory=lambda: {"default": 1_000_000.0})
    commission_per_trade: float = 0.0
    slippage_bps: float = 0.0  # 10 bps = 0.10%
    fill_mode: str = "next_bar_open"  # same_bar_close | next_bar_open
    dedupe_correlated_signals: bool = True


class OptimizeRequest(BaseModel):
    """Parameter-sweep request. Vectorized ranks are screening only.

    Official results always come from the event-driven backtester.
    """
    strategy_id: str
    param_grid: dict[str, list] = Field(default_factory=dict)
    symbols: list[str]
    timeframe: Timeframe
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    initial_capital: dict[str, float] = Field(default_factory=lambda: {"default": 1_000_000.0})
    commission_per_trade: float = 0.0
    slippage_bps: float = 0.0
    fill_mode: str = "next_bar_open"
    rank_by: str = "expectancy"  # net_pnl | profit_factor | expectancy | win_rate
    max_combinations: int = 64
    validate_top_n: int = 3
    walk_forward_folds: int = 0  # 0 disables WFO
    walk_forward_mode: str = "anchored"  # anchored | rolling
    test_fraction: float = 0.3
    use_vectorized_screen: bool = True


class OptimizeResult(BaseModel):
    strategy_id: str
    symbols: list[str]
    timeframe: str
    combinations_evaluated: int = 0
    combinations_skipped: int = 0
    rank_by: str = "expectancy"
    vectorized_is_not_truth: bool = True
    vectorized_ranking: list[dict] = Field(default_factory=list)
    official_event_driven: list[dict] = Field(default_factory=list)
    walk_forward: list[dict] = Field(default_factory=list)
    recommended_params: dict = Field(default_factory=dict)
    recommended_official_summary: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    runtime_sec: float = 0.0


class BacktestResult(BaseModel):
    strategy_id: str
    symbols: list[str]
    timeframe: str
    start: datetime
    end: datetime
    total_bars: int = 0
    summary: dict = Field(default_factory=dict)  # win_rate, profit_factor, max_dd, etc.
    equity_curve: list[dict] = Field(default_factory=list)  # [{t, equity}]
    drawdown_curve: list[dict] = Field(default_factory=list)
    trades: list[Trade] = Field(default_factory=list)
    per_account: dict = Field(default_factory=dict)
    per_symbol: dict = Field(default_factory=dict)
    correlated_signal_groups: list[list[str]] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
