"""DataFeed adapter abstract base class.

Every data source (CSV import, Equal RT, broker API, mock) implements this
single interface. The rest of the system (Strategy Engine, Backtester, UI)
never talks to vendors directly — they only talk to this protocol.

This is the single most important architectural decision in the codebase.
Change this interface ONLY with extreme caution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Optional

from app.core.logging_setup import get_logger
from app.core.schemas import Bar, Tick, Timeframe

log = get_logger(__name__)


TickCallback = Callable[[Tick], None]
BarCloseCallback = Callable[[Bar], None]


class DataFeed(ABC):
    """Abstract base class for all data feed adapters.

    Lifecycle:
        1. Instantiate adapter with config.
        2. ``connect()`` — open connection to the feed.
        3. ``subscribe(symbol)`` — start receiving ticks/bars for a symbol.
        4. Register callbacks via ``on_tick`` / ``on_bar_close``.
        5. ``get_historical()`` — request historical bars for backtesting.
        6. ``unsubscribe(symbol)`` / ``disconnect()`` when done.
    """

    name: str = "base"
    connected: bool = False
    subscribed_symbols: set[str]

    def __init__(self) -> None:
        self.subscribed_symbols = set()
        self._tick_handlers: list[TickCallback] = []
        self._bar_close_handlers: list[BarCloseCallback] = []

    # ── Connection lifecycle ──────────────────────────────────────────

    @abstractmethod
    def connect(self, **kwargs) -> None:
        """Open connection to the data source. Must be idempotent."""
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection. Must be idempotent."""
        raise NotImplementedError

    # ── Subscription ──────────────────────────────────────────────────

    @abstractmethod
    def subscribe(self, symbol: str, timeframe: Optional[Timeframe] = None) -> None:
        """Subscribe to real-time updates for symbol.

        Args:
            symbol: Instrument identifier (e.g. "NIFTY25OCTC20000", "XAUUSD").
            timeframe: If given, subscribe to bar-close events at this TF.
                       If None, subscribe to tick stream only.
        """
        raise NotImplementedError

    @abstractmethod
    def unsubscribe(self, symbol: str, timeframe: Optional[Timeframe] = None) -> None:
        """Unsubscribe from symbol updates."""
        raise NotImplementedError

    # ── Historical data ───────────────────────────────────────────────

    @abstractmethod
    def get_historical(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[Bar]:
        """Return historical bars for [start, end] (inclusive).

        Returns bars sorted by timestamp ASCENDING. Oldest first.
        """
        raise NotImplementedError

    # ── Callbacks ─────────────────────────────────────────────────────

    def on_tick(self, callback: TickCallback) -> Callable[[], None]:
        """Register a callback fired on every tick. Returns unsubscribe fn."""
        self._tick_handlers.append(callback)
        return lambda: self._tick_handlers.remove(callback)

    def on_bar_close(self, callback: BarCloseCallback) -> Callable[[], None]:
        """Register a callback fired on every bar close. Returns unsubscribe fn."""
        self._bar_close_handlers.append(callback)
        return lambda: self._bar_close_handlers.remove(callback)

    # ── Internal: fire events (adapters call these) ───────────────────

    def _emit_tick(self, tick: Tick) -> None:
        for handler in self._tick_handlers:
            try:
                handler(tick)
            except Exception as e:
                # Never let a bad handler crash the feed. Log + continue.
                log.error(
                    "Tick handler raised exception",
                    extra={"symbol": getattr(tick, "symbol", None), "error": str(e),
                           "handler": getattr(handler, "__name__", repr(handler))},
                    exc_info=True,
                )

    def _emit_bar_close(self, bar: Bar) -> None:
        for handler in self._bar_close_handlers:
            try:
                handler(bar)
            except Exception as e:
                log.error(
                    "Bar-close handler raised exception",
                    extra={"symbol": bar.symbol, "timeframe": bar.timeframe.value,
                           "ts": bar.timestamp.isoformat(), "error": str(e),
                           "handler": getattr(handler, "__name__", repr(handler))},
                    exc_info=True,
                )


# ── Global adapter registry ────────────────────────────────────────────────

_ADAPTER_REGISTRY: dict[str, type[DataFeed]] = {}


def register_adapter(name: str):
    """Decorator: register a DataFeed subclass under ``name``."""
    def _decorator(cls: type[DataFeed]) -> type[DataFeed]:
        cls.name = name
        _ADAPTER_REGISTRY[name] = cls
        return cls
    return _decorator


def get_adapter_class(name: str) -> type[DataFeed]:
    if name not in _ADAPTER_REGISTRY:
        raise ValueError(f"Unknown data feed adapter: {name}. "
                         f"Available: {list(_ADAPTER_REGISTRY)}")
    return _ADAPTER_REGISTRY[name]


def list_adapters() -> list[str]:
    return sorted(_ADAPTER_REGISTRY.keys())


# Import adapters AFTER registry definitions to avoid circular imports
from .adapters import csv_adapter, mock_adapter  # noqa: E402, F401  (registers themselves)
