"""Strategy Engine — pure function on_bar(context) → orders.

SHARED CORE (non-negotiable per spec architecture rule #1):
  The EXACT SAME on_bar(context) call runs in:
    - Backtest driver (loops historical bars → calls on_bar each)
    - Live engine (feed bar-close events → calls on_bar each)
  Divergence between backtest and live = ENGINE BUG = CRITICAL level log.

Strategy developers: extend Strategy base class, implement on_bar.
NEVER write backtest-specific or live-specific strategy code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from app.core.logging_setup import (
    clear_context,
    get_logger,
    new_trade_id,
    set_context,
)
from app.core.schemas import (
    Order,
    OrderSide,
    OrderType,
    PositionSide,
    Trade,
)
from app.strategy.context import StrategyContext

log = get_logger(__name__)


class Strategy(ABC):
    """Base Strategy class.

    Lifecycle:
        init(params, strategy_id)
        on_start(context)
        on_bar(context) -> orders (repeated for every bar-close)
        on_stop(context)

    Implement ONLY on_bar (and optional hooks). NEVER mutate context inside strategy.
    NEVER access network/disk. NEVER import feed or backtest modules.
    """

    id: str
    name: str
    params: dict

    def __init__(self, strategy_id: str, params: Optional[dict] = None, name: Optional[str] = None) -> None:
        self.id = strategy_id
        self.name = name or type(self).__name__
        self.params = params or {}

    # ── Optional hooks ─────────────────────────────────────────────────

    def on_start(self, context: StrategyContext) -> None:
        """Called once before first bar (both backtest + live)."""
        pass

    def on_stop(self, context: StrategyContext) -> None:
        """Called once after last bar (or before disconnect)."""
        pass

    # ── Core: override this. Pure function. ───────────────────────────

    @abstractmethod
    def on_bar(self, context: StrategyContext) -> list[Order]:
        """Return orders to emit for this bar.

        Return empty list to do nothing.
        Strategy must not mutate context.position (it's a snapshot).
        """
        raise NotImplementedError

    # ── Convenience order constructors ─────────────────────────────────

    @staticmethod
    def _order(
        context: StrategyContext,
        side: OrderSide,
        quantity: float,
        type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        tags: Optional[dict] = None,
    ) -> Order:
        """Create an Order stamped with current context's trade/account/strategy IDs."""
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        return Order(
            id=f"O-{now.strftime('%Y%m%d-%H%M%S')}-{id(context) % 10000:04d}",
            symbol=context.symbol,
            side=side,
            type=type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            timestamp=now,
            account_id=context.account_id,
            strategy_id=context.strategy_id,
            tags=tags or {},
        )

    @staticmethod
    def buy_market(context: StrategyContext, qty: float, tags: Optional[dict] = None) -> Order:
        return Strategy._order(context, OrderSide.BUY, qty, OrderType.MARKET, tags=tags)

    @staticmethod
    def sell_market(context: StrategyContext, qty: float, tags: Optional[dict] = None) -> Order:
        return Strategy._order(context, OrderSide.SELL, qty, OrderType.MARKET, tags=tags)


# ── Strategy registry (plug-and-play strategies) ──────────────────────────

_STRATEGY_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(name: str):
    """Decorator: register a Strategy subclass under `name`."""
    def _decorator(cls: type[Strategy]) -> type[Strategy]:
        _STRATEGY_REGISTRY[name] = cls
        log.debug("Strategy registered", extra={"strategy_name": name,
                                                "class": cls.__name__})
        return cls
    return _decorator


def get_strategy_class(name: str) -> type[Strategy]:
    if name not in _STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy '{name}'. "
                         f"Available: {list(_STRATEGY_REGISTRY)}")
    return _STRATEGY_REGISTRY[name]


def list_strategies() -> list[str]:
    return sorted(_STRATEGY_REGISTRY.keys())


# ── Engine driver: run one bar through a strategy with full tracing ────────

def evaluate_bar(
    strategy: Strategy,
    context: StrategyContext,
    dry_run_verbose: bool = False,
) -> list[Order]:
    """Evaluate `strategy.on_bar(context)` with full structured logging.

    This is the ONLY way strategies are invoked — backtest and live drivers
    both call this function; so parity is guaranteed at the code-path level.

    Behavior:
    - Attaches trade_id context for the duration of the call (for grepable logs).
    - Logs DEBUG (or INFO, on dry_run) every decision: signal → filter → order/no order.
    - Never silently catches strategy exceptions → re-raises ERROR level log.
    """
    # Generate unique trade ID for this bar's signal evaluation (matches user requirement:
    # trade ID the moment signal EVALUATED, not on fill — so grep covers whole lifecycle).
    trade_id = new_trade_id()
    set_context(
        trade_id=trade_id,
        symbol=context.symbol,
        timeframe=context.timeframe.value,
        account_id=context.account_id,
        strategy_id=context.strategy_id,
    )
    try:
        log.debug(
            "on_bar start",
            extra={
                "bar_idx": context.current_bar_idx,
                "close": context.close,
                "position_side": context.position_side.value,
                "qty": context.position.quantity,
                "stop_loss": context.position.stop_loss,
                "take_profit": context.position.take_profit,
                "verbose": dry_run_verbose,
            },
        )
        orders = strategy.on_bar(context)
        orders = list(orders or [])

        if orders:
            log.info(
                "Strategy emitted orders",
                extra={
                    "order_count": len(orders),
                    "orders": [
                        {"id": o.id, "side": o.side.value, "qty": o.quantity,
                         "type": o.type.value, "price": o.price, "stop_price": o.stop_price}
                        for o in orders
                    ],
                },
            )
        elif dry_run_verbose:
            log.debug("Strategy emitted NO orders for this bar")

        return orders
    except Exception as e:
        # CRITICAL: a strategy exception during on_bar is a missed signal.
        # Never silently continue. Log loud + re-raise.
        log.critical(
            "Strategy on_bar() THREW EXCEPTION — signal evaluation missed!",
            extra={"error": str(e), "bar_idx": context.current_bar_idx,
                   "ts": context.timestamp.isoformat()},
            exc_info=True,
        )
        raise
    finally:
        clear_context()
