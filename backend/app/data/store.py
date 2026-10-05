"""Market Data Store.

Two layers:
    1. DuckDB + Parquet on disk — historical data, fast range scans for backtests.
    2. In-memory ring buffers (last N bars per (symbol, tf)) — live data feed.

One unified schema for both. Query interface is identical regardless of source.

Data flow for live:
    Live feed → on_bar_close → ring buffer → periodic flush to Parquet.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from threading import Lock
from typing import Iterable, Optional

import duckdb
import pandas as pd

from app.core.config import PARQUET_DIR, RING_BUFFER_SIZE
from app.core.logging_setup import get_logger
from app.core.schemas import Bar, Timeframe
from app.core.utils import tf_to_timedelta as _tf_to_timedelta

log = get_logger(__name__)


class MarketDataStore:
    """Unified historical + live market data store.

    Thread-safe for the live paths; historical queries are read-only.
    """

    def __init__(
        self,
        parquet_dir: Optional[str] = None,
        ring_buffer_size: int = RING_BUFFER_SIZE,
    ) -> None:
        self.parquet_dir = parquet_dir or str(PARQUET_DIR)
        self.ring_buffer_size = ring_buffer_size

        # key=(symbol, timeframe.value) -> deque[Bar] (most recent on right)
        self._ring_buffers: dict[tuple[str, str], deque[Bar]] = {}
        self._lock = Lock()

        # DuckDB connection (in-process, no server)
        self._db = duckdb.connect(database=":memory:", read_only=False)
        try:
            self._db.execute("SET binary_as_string = false")
        except Exception:
            # Older DuckDB versions might not have this; ignore.
            pass
        self._ensure_parquet_dir()
        self._register_parquet_views()

    # ── Setup ─────────────────────────────────────────────────────────

    def _ensure_parquet_dir(self) -> None:
        import os
        os.makedirs(self.parquet_dir, exist_ok=True)

    def _register_parquet_views(self) -> None:
        """Register all existing Parquet files as views so DuckDB can query them."""
        import os
        import glob

        pattern = os.path.join(self.parquet_dir, "*.parquet")
        for path in sorted(glob.glob(pattern)):
            fname = os.path.basename(path)
            view_name = self._view_name_from_path(fname)
            try:
                self._db.execute(
                    f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet('{path}')"
                )
                log.debug("Registered parquet view", extra={"view": view_name, "path": path})
            except Exception as e:
                log.error(
                    "Failed to register parquet file as DuckDB view",
                    extra={"file": fname, "path": path, "error": str(e)},
                    exc_info=True,
                )

    @staticmethod
    def _view_name_from_path(fname: str) -> str:
        # e.g. "XAUUSD_1m.parquet" -> "bars_XAUUSD_1m" (SQL-safe chars only)
        stem = fname.replace(".parquet", "").replace(".", "_")
        return f"bars_{stem}"

    @staticmethod
    def _parquet_path(symbol: str, tf: Timeframe, parquet_dir: str) -> str:
        import os
        safe = symbol.replace("/", "_")
        return os.path.join(parquet_dir, f"{safe}_{tf.value}.parquet")

    @staticmethod
    def _key(symbol: str, tf: Timeframe) -> tuple[str, str]:
        return (symbol, tf.value)

    # ── Write / import ────────────────────────────────────────────────

    def import_bars(self, bars: Iterable[Bar]) -> dict:
        """Import bars into both Parquet (persistent) and ring buffers.

        Returns stats dict: {symbol_tf: bar_count, ...}.
        """
        from collections import defaultdict

        grouped: dict[tuple[str, str], list[Bar]] = defaultdict(list)
        for b in bars:
            grouped[(b.symbol, b.timeframe.value)].append(b)

        stats = {}
        for (sym, tf_val), sym_bars in grouped.items():
            # Sort by ts ASC
            sym_bars.sort(key=lambda x: x.timestamp)
            tf = Timeframe(tf_val)
            # Persist
            self._append_to_parquet(sym, tf, sym_bars)
            # Update ring buffer
            self._update_ring(sym, tf, sym_bars)
            stats[f"{sym}_{tf_val}"] = len(sym_bars)

        return stats

    def _append_to_parquet(self, symbol: str, tf: Timeframe, bars: list[Bar]) -> None:
        if not bars:
            return
        path = self._parquet_path(symbol, tf, self.parquet_dir)
        df = pd.DataFrame([b.model_dump() for b in bars])
        df["timeframe"] = tf.value
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        # Merge with existing if present, drop duplicate timestamps
        import os
        if os.path.exists(path):
            existing = pd.read_parquet(path)
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        df.to_parquet(path, index=False)

        # Register (re-register) view so live queries work
        view = self._view_name_from_path(os.path.basename(path))
        self._db.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{path}')")

    def _update_ring(self, symbol: str, tf: Timeframe, bars: list[Bar]) -> None:
        with self._lock:
            key = self._key(symbol, tf)
            buf = self._ring_buffers.get(key)
            if buf is None:
                buf = deque(maxlen=self.ring_buffer_size)
                self._ring_buffers[key] = buf
            for b in bars:
                # Don't push duplicates into ring.
                if not buf or buf[-1].timestamp < b.timestamp:
                    buf.append(b)

    # ── Read ──────────────────────────────────────────────────────────

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
        include_ring: bool = True,
    ) -> list[Bar]:
        """Return bars sorted by timestamp ASC (oldest first).

        Sources are merged: Parquet (historical) + ring buffer (latest live bars).
        """
        hist = self._query_parquet(symbol, timeframe, start, end)
        live: list[Bar] = []
        if include_ring:
            live = self._read_ring(symbol, timeframe, start, end)

        # Merge dedupe on timestamp
        merged: dict[datetime, Bar] = {b.timestamp: b for b in hist}
        for b in live:
            merged[b.timestamp] = b  # live wins on conflict

        result = sorted(merged.values(), key=lambda x: x.timestamp)
        if limit is not None and limit > 0:
            result = result[-limit:]
        return result

    def _query_parquet(
        self, symbol: str, tf: Timeframe, start: Optional[datetime], end: Optional[datetime]
    ) -> list[Bar]:
        import os
        path = self._parquet_path(symbol, tf, self.parquet_dir)
        if not os.path.exists(path):
            return []

        clauses = []
        params = []
        if start is not None:
            clauses.append("timestamp >= ?")
            ts = pd.Timestamp(start)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            params.append(ts)
        if end is not None:
            clauses.append("timestamp <= ?")
            ts = pd.Timestamp(end)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            params.append(ts)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        q = f"""
            SELECT symbol, timeframe, timestamp, open, high, low, close, volume
            FROM read_parquet('{path}')
            {where}
            ORDER BY timestamp ASC
        """
        try:
            df = self._db.execute(q, params).fetchdf()
        except Exception as e:
            log.error(
                "Parquet query failed",
                extra={"symbol": symbol, "timeframe": tf.value,
                       "start": str(start), "end": str(end), "error": str(e)},
                exc_info=True,
            )
            return []

        bars: list[Bar] = []
        for _, row in df.iterrows():
            ts = row["timestamp"].to_pydatetime()
            if not hasattr(ts, "tzinfo") or ts.tzinfo is None:
                from datetime import timezone
                ts = ts.replace(tzinfo=timezone.utc)
            try:
                bars.append(Bar(
                    symbol=str(row["symbol"]),
                    timeframe=Timeframe(str(row["timeframe"])),
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=None if pd.isna(row.get("volume")) else float(row["volume"]),
                ))
            except Exception as e:
                continue
        return bars

    def _read_ring(
        self, symbol: str, tf: Timeframe, start: Optional[datetime], end: Optional[datetime]
    ) -> list[Bar]:
        with self._lock:
            buf = self._ring_buffers.get(self._key(symbol, tf))
            if buf is None:
                return []
            result = list(buf)
        if start is not None:
            result = [b for b in result if b.timestamp >= start]
        if end is not None:
            result = [b for b in result if b.timestamp <= end]
        return result

    # ── Symbols / metadata ────────────────────────────────────────────

    def list_symbols(self) -> list[dict]:
        """Return list of {symbol, timeframe, bar_count, first_ts, last_ts}."""
        import os, glob

        pattern = os.path.join(self.parquet_dir, "*.parquet")
        rows = []
        for path in glob.glob(pattern):
            fname = os.path.basename(path)
            try:
                df_info = self._db.execute(f"""
                    SELECT ANY_VALUE(symbol) as symbol, ANY_VALUE(timeframe) as timeframe, count(*) as cnt, min(timestamp) as t0, max(timestamp) as t1
                    FROM read_parquet('{path}')
                """).fetchone()
                if df_info:
                    sym, tf, cnt, t0, t1 = df_info
                    # Merge ring buffer: only count bars NEWER than parquet max
                    # to avoid double-counting overlap.
                    with self._lock:
                        ring = self._ring_buffers.get((sym, tf))
                        if ring:
                            ring_newer = [b for b in ring if b.timestamp > t1]
                            if ring_newer:
                                cnt += len(ring_newer)
                                t1 = ring[-1].timestamp
                            if t0 is None and ring:
                                t0 = ring[0].timestamp
                    rows.append({
                        "symbol": sym,
                        "timeframe": tf,
                        "bar_count": int(cnt),
                        "first_ts": t0.isoformat() if t0 else None,
                        "last_ts": t1.isoformat() if t1 else None,
                    })
            except Exception as e:
                log.warning(
                    "Failed to read parquet metadata in list_symbols",
                    extra={"file": fname, "error": str(e)},
                    exc_info=False,
                )
        # Also include any symbols only in ring buffers (not yet persisted)
        with self._lock:
            ring_only_keys = set(self._ring_buffers.keys()) - set((r["symbol"], r["timeframe"]) for r in rows)
            for sym, tf in ring_only_keys:
                buf = self._ring_buffers[(sym, tf)]
                if buf:
                    rows.append({
                        "symbol": sym,
                        "timeframe": tf,
                        "bar_count": len(buf),
                        "first_ts": buf[0].timestamp.isoformat(),
                        "last_ts": buf[-1].timestamp.isoformat(),
                    })
        return sorted(rows, key=lambda r: (r["symbol"], r["timeframe"]))

    def flush(self) -> None:
        """Persist all in-memory ring buffers to Parquet. Call on shutdown."""
        with self._lock:
            keys = list(self._ring_buffers.keys())
            for (sym, tf_val) in keys:
                buf = self._ring_buffers[(sym, tf_val)]
                if not buf:
                    continue
                tf = Timeframe(tf_val)
                bars = list(buf)
                self._append_to_parquet(sym, tf, bars)
                # Clear ring after flush to avoid double-writing next time
                buf.clear()


# ── Module-level singleton (most callers use this) ─────────────────────────

_store: Optional[MarketDataStore] = None


def get_store() -> MarketDataStore:
    global _store
    if _store is None:
        _store = MarketDataStore()
    return _store
