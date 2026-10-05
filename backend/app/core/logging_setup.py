"""Structured logging infrastructure — CORE INFRASTRUCTURE, not polish.

Every module MUST use get_logger(__name__) to emit messages. Never use print()
in any production path. This is non-negotiable: silent failures cost real money.

Features:
- Structured JSON-capable log records. Every line carries: timestamp, severity,
  module, message, plus optional context (trade_id, symbol, tf, account_id, ...).
- Leveled correctly: DEBUG (verbose bar-by-bar), INFO (opened/closed trades,
  connects, saves), WARNING (gaps, stale data, filtered signals, reconnects),
  ERROR (component failed to do its job), CRITICAL (state inconsistencies,
  backtest/live parity divergence, live feed down during market hours).
- Separate rotating log files per component + combined app.log.
- Per-record context injection via TradeIdFilter: once a trade/signal has an ID,
  every related log line carries it automatically (grep one ID → full lifecycle).
- Runtime toggleable debug level per logger (no restart needed).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.config import LOG_DIR


# ── Context variables (per-task state, threadsafe via ContextVar) ────────

_ctx_trade_id: ContextVar[Optional[str]] = ContextVar("trade_id", default=None)
_ctx_symbol: ContextVar[Optional[str]] = ContextVar("symbol", default=None)
_ctx_timeframe: ContextVar[Optional[str]] = ContextVar("timeframe", default=None)
_ctx_account_id: ContextVar[Optional[str]] = ContextVar("account_id", default=None)
_ctx_strategy_id: ContextVar[Optional[str]] = ContextVar("strategy_id", default=None)


def set_context(
    *,
    trade_id: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    account_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
) -> None:
    """Attach context to the current async/task context.

    Every subsequent log line in this context carries these fields.
    Pass None to leave a field unchanged; pass the sentinel _RESET to clear.
    """
    if trade_id is not None:
        _ctx_trade_id.set(trade_id)
    if symbol is not None:
        _ctx_symbol.set(symbol)
    if timeframe is not None:
        _ctx_timeframe.set(timeframe)
    if account_id is not None:
        _ctx_account_id.set(account_id)
    if strategy_id is not None:
        _ctx_strategy_id.set(strategy_id)


def clear_context() -> None:
    _ctx_trade_id.set(None)
    _ctx_symbol.set(None)
    _ctx_timeframe.set(None)
    _ctx_account_id.set(None)
    _ctx_strategy_id.set(None)


def new_trade_id(prefix: str = "T") -> str:
    """Generate a unique short ID for a trade/signal the moment it's evaluated.

    Use this BEFORE calling the engine, not after fill. Every log line about
    that decision (signal, filter pass/fail, entry, stop updates, exit, pnl)
    tags this same ID. Grep the ID → full lifecycle.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6].upper()
    tid = f"{prefix}-{ts}-{short}"
    _ctx_trade_id.set(tid)
    return tid


# ── In-memory ring buffer of recent log lines (for UI debug panel tail) ──

_RECENT_LOG_LOCK = threading.Lock()
_RECENT_LOGS: deque[dict] = deque(maxlen=1000)  # last 1000 log records, any level


def recent_logs(min_level: str = "DEBUG", limit: int = 100) -> list[dict]:
    """Return recent log records (for UI debug panel live tail)."""
    lvl = getattr(logging, min_level.upper(), logging.DEBUG)
    with _RECENT_LOG_LOCK:
        records = [r for r in list(_RECENT_LOGS) if r["_levelno"] >= lvl]
    return records[-limit:]


# ── JSON-capable formatter ───────────────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    """Format LogRecord → structured dict → JSON or human line.

    Every message carries at minimum:
        ts (ISO 8601 UTC), level, logger (component), msg,
        + context (trade_id, symbol, tf, account, strategy if present),
        + any extra fields the caller attached via extra={}.
    """

    def __init__(self, use_json: bool = False) -> None:
        super().__init__()
        self.use_json = use_json

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        now = datetime.fromtimestamp(record.created, tz=timezone.utc)
        data: dict[str, Any] = {
            "ts": now.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "func": record.funcName,
            "lineno": record.lineno,
            "msg": record.getMessage(),
        }

        # Context variables (per-task)
        tid = _ctx_trade_id.get()
        if tid:
            data["trade_id"] = tid
        sym = _ctx_symbol.get()
        if sym:
            data["symbol"] = sym
        tf = _ctx_timeframe.get()
        if tf:
            data["timeframe"] = tf
        acc = _ctx_account_id.get()
        if acc:
            data["account_id"] = acc
        strat = _ctx_strategy_id.get()
        if strat:
            data["strategy_id"] = strat

        # Any extras the caller passed via logger.info(..., extra={...})
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in {
                "name", "msg", "args", "levelname", "levelno",
                "pathname", "filename", "module", "exc_info", "exc_text",
                "stack_info", "lineno", "funcName", "created", "msecs",
                "relativeCreated", "thread", "threadName", "processName",
                "process", "message", "asctime", "taskName",
            }:
                continue
            data[key] = val

        # Attach exception if present
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)

        # Also store for in-memory ring buffer
        rec_for_buf = dict(data)
        rec_for_buf["_levelno"] = record.levelno
        with _RECENT_LOG_LOCK:
            _RECENT_LOGS.append(rec_for_buf)

        if self.use_json:
            try:
                return json.dumps(data, default=str, ensure_ascii=False)
            except Exception:
                # Fallback if something is non-serializable — NEVER drop the log line.
                data["msg"] = data.get("msg", "") + " [json_encode_failed]"
                return json.dumps({k: str(v) for k, v in data.items()}, ensure_ascii=False)

        # Human-friendly one-liner (for console / combined app.log)
        ctx_parts = []
        for k in ("trade_id", "symbol", "timeframe", "account_id", "strategy_id"):
            if k in data:
                ctx_parts.append(f"{k}={data[k]}")
        ctx_str = (" | " + " ".join(ctx_parts)) if ctx_parts else ""
        extra_parts = []
        for k, v in data.items():
            if k.startswith("_") or k in {
                "ts", "level", "logger", "module", "func", "lineno", "msg",
                "trade_id", "symbol", "timeframe", "account_id", "strategy_id",
                "exception",
            }:
                continue
            extra_parts.append(f"{k}={v}")
        extra_str = (" | " + " ".join(extra_parts)) if extra_parts else ""
        exc_str = f"\n{data['exception']}" if "exception" in data else ""
        return (
            f"{data['ts']}  {data['level']:<8}  {data['logger']:<22}  "
            f"{data['msg']}{ctx_str}{extra_str}{exc_str}"
        )


# ── Log file setup ───────────────────────────────────────────────────────

_COMPONENTS = {
    "app": "app.log",
    "data_feed": "data_feed.log",
    "strategy_engine": "strategy_engine.log",
    "backtest": "backtest.log",
    "live": "live.log",
    "data_store": "data_store.log",
    "data_validation": "data_validation.log",
    "ui": "ui.log",
    "api": "api.log",
}

_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def _ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


def _setup_root_and_files(default_level: int = logging.INFO) -> None:
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        _ensure_log_dir()

        # Root logger baseline
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)  # accept all; handlers filter
        if root.handlers:
            root.handlers.clear()

        # ── Console handler: human readable, INFO+ by default ────────
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(default_level)
        console.setFormatter(StructuredFormatter(use_json=False))
        root.addHandler(console)

        # ── Combined app.log: human, DEBUG+, rotating 10MB × 10 ──────
        app_path = Path(LOG_DIR) / _COMPONENTS["app"]
        combined = logging.handlers.RotatingFileHandler(
            app_path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8",
        )
        combined.setLevel(logging.DEBUG)
        combined.setFormatter(StructuredFormatter(use_json=False))
        root.addHandler(combined)

        # ── Per-component JSON log files (for programmatic grep/parsing) ──
        for comp, fname in _COMPONENTS.items():
            if comp == "app":
                continue  # combined is human; app.log is above
            fpath = Path(LOG_DIR) / fname
            h = logging.handlers.RotatingFileHandler(
                fpath, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8",
            )
            h.setLevel(logging.DEBUG)
            h.setFormatter(StructuredFormatter(use_json=True))
            # Route only matching logger subtree via filter
            prefix = {"data_feed": "app.data",
                      "strategy_engine": "app.strategy",
                      "backtest": "app.backtest",
                      "live": "app.live",
                      "data_store": "app.data.store",
                      "data_validation": "app.data.validate",
                      "api": "app.api",
                      "ui": "app.ui"}.get(comp, comp)

            class _Filter:
                def __init__(self, prefix: str) -> None:
                    self.prefix = prefix
                def filter(self, rec: logging.LogRecord) -> bool:
                    return rec.name.startswith(self.prefix) or rec.name == prefix

            h.addFilter(_Filter(prefix))
            root.addHandler(h)

        # Silence extremely noisy third-party libs
        for name in ("uvicorn", "uvicorn.access", "fastapi", "httpx", "httpcore",
                     "duckdb", "pyarrow"):
            logging.getLogger(name).setLevel(logging.WARNING)

        _INITIALIZED = True


def get_logger(name: str, default_level: int = logging.INFO) -> logging.Logger:
    """Return a component logger with structured format pre-wired.

    Usage (every module, top-level)::

        from app.core.logging import get_logger
        log = get_logger(__name__)  # → e.g. "app.data.feed"

        log.info("Feed connected", extra={"adapter": "equal_rt", "symbols": 42})
        log.warning("Data gap detected", extra={"gap_seconds": 625})
        log.critical("Engine state mismatch", extra={"live_trades": 7, "bt_trades": 6})
    """
    _setup_root_and_files(default_level=default_level)
    logger = logging.getLogger(name)
    if logger.level == logging.NOTSET:
        logger.setLevel(default_level)
    return logger


def set_level(logger_name: str, level: str | int) -> None:
    """Toggle log level at runtime — DEBUG logging on/off without restart."""
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger(logger_name).setLevel(level)
    log = get_logger("app.core.logging")
    log.info("Log level changed at runtime", extra={"logger": logger_name,
                                                    "new_level": logging.getLevelName(level)})


def set_all_levels(level: str | int) -> None:
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)
    for name in list(logging.Logger.manager.loggerDict.keys()):
        logging.getLogger(name).setLevel(level)
    log = get_logger("app.core.logging")
    log.info("All log levels changed", extra={"new_level": logging.getLevelName(level)})
