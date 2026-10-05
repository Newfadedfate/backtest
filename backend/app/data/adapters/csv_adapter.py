"""CSV / Parquet file import adapter.

Reads OHLCV data from CSV or Parquet files on disk. Used for backtesting
and initial data onboarding before a live feed is connected.

Expected CSV columns (case-insensitive):
    timestamp, open, high, low, close, volume (optional)

Dates: accepts ISO8601 or Unix ms/s timestamps.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.core.logging_setup import get_logger
from app.core.schemas import Bar, Tick, Timeframe
from app.data.feed import DataFeed, register_adapter

log = get_logger(__name__)


@register_adapter("csv")
class CsvAdapter(DataFeed):
    """Read historical OHLCV from CSV / Parquet files.

    Usage::

        feed = CsvAdapter(data_dir="./data/")
        feed.connect()
        bars = feed.get_historical("NIFTY", Timeframe.M5, start=..., end=...)
    """

    def __init__(self, data_dir: Optional[str | Path] = None) -> None:
        super().__init__()
        self.data_dir = Path(data_dir) if data_dir else Path.cwd() / "data" / "raw"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[tuple[str, Timeframe], list[Bar]] = {}

    # ── Connection ────────────────────────────────────────────────────

    def connect(self, **kwargs) -> None:
        # File adapter has no network connection; just mark connected.
        if kwargs.get("data_dir"):
            self.data_dir = Path(kwargs["data_dir"])
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self._cache.clear()

    # ── Subscription (no-op for file adapter) ─────────────────────────

    def subscribe(self, symbol: str, timeframe: Optional[Timeframe] = None) -> None:
        self.subscribed_symbols.add(f"{symbol}:{timeframe or 'TICK'}")

    def unsubscribe(self, symbol: str, timeframe: Optional[Timeframe] = None) -> None:
        self.subscribed_symbols.discard(f"{symbol}:{timeframe or 'TICK'}")

    # ── Historical data ───────────────────────────────────────────────

    def get_historical(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[Bar]:
        if not self.connected:
            raise RuntimeError("CsvAdapter not connected. Call connect() first.")

        cache_key = (symbol, timeframe)
        if cache_key not in self._cache:
            self._cache[cache_key] = self._load_from_disk(symbol, timeframe)

        bars = self._cache[cache_key]

        # Time filter
        if start is not None:
            bars = [b for b in bars if b.timestamp >= start]
        if end is not None:
            bars = [b for b in bars if b.timestamp <= end]
        if limit is not None and limit > 0:
            bars = bars[-limit:]

        return list(bars)

    # ── Helpers ───────────────────────────────────────────────────────

    def _load_from_disk(self, symbol: str, timeframe: Timeframe) -> list[Bar]:
        """Load bars from disk, try Parquet first then CSV."""
        safe_symbol = symbol.replace("/", "_")
        parquet_path = self.data_dir / f"{safe_symbol}_{timeframe.value}.parquet"
        csv_path = self.data_dir / f"{safe_symbol}_{timeframe.value}.csv"

        df: Optional[pd.DataFrame] = None
        if parquet_path.exists():
            df = pd.read_parquet(parquet_path)
        elif csv_path.exists():
            df = pd.read_csv(csv_path)
        else:
            # No data — return empty, not an error. Allow user to import.
            return []

        return self._df_to_bars(df, symbol, timeframe)

    def _df_to_bars(self, df: pd.DataFrame, symbol: str, timeframe: Timeframe) -> list[Bar]:
        # Normalize column names to lowercase
        df.columns = [str(c).strip().lower() for c in df.columns]

        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}. Found: {list(df.columns)}")

        if "timestamp" not in df.columns and "datetime" not in df.columns and "date" not in df.columns:
            raise ValueError("CSV must have a 'timestamp', 'datetime', or 'date' column.")

        ts_col = next(c for c in ("timestamp", "datetime", "date") if c in df.columns)
        df["_ts"] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
        df = df.dropna(subset=["_ts"]).sort_values("_ts").reset_index(drop=True)

        volume_col = "volume" if "volume" in df.columns else None

        bars: list[Bar] = []
        for _, row in df.iterrows():
            try:
                bar = Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=row["_ts"].to_pydatetime(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row[volume_col]) if volume_col else None,
                )
                bars.append(bar)
            except Exception as e:
                # Skip bad rows but WARN — never silently drop data the user might think they imported.
                log.warning(
                    "Skipping invalid CSV row on import",
                    extra={"symbol": symbol, "timeframe": timeframe.value,
                           "row_idx": int(row.name), "error": str(e)},
                    exc_info=False,
                )
                continue

        return bars

    # ── Write paths (imports go through here) ─────────────────────────

    def write_bars(self, bars: list[Bar], overwrite: bool = False) -> Path:
        """Write list of Bar to Parquet. Returns file path."""
        if not bars:
            raise ValueError("Empty bar list.")
        symbol = bars[0].symbol
        tf = bars[0].timeframe
        safe_symbol = symbol.replace("/", "_")
        out = self.data_dir / f"{safe_symbol}_{tf.value}.parquet"
        df = pd.DataFrame([b.model_dump() for b in bars])
        df["timeframe"] = tf.value
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if overwrite and out.exists():
            out.unlink()
        # Append or write
        if out.exists():
            existing = pd.read_parquet(out)
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        df.to_parquet(out, index=False)
        # Drop cache so next get_historical picks it up
        self._cache.pop((symbol, tf), None)
        return out
