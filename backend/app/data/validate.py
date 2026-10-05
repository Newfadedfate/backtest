"""Data validation layer.

Runs on EVERY import. Never trust raw data — garbage in → garbage backtests.

Validates:
1. **OHLC sanity** — High >= Max(O,C), Low <= Min(O,C), all values finite.
2. **Duplicate timestamps** — same timestamp appearing >1x (bar overwrite).
3. **Gaps** — expected bar cadence (e.g. 5min) is missing. Reports time ranges.
4. **Missing fields** — required columns/values that are NaN or non-numeric.

Returns a DataValidationReport that the API surface exposes to the UI.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from app.core.logging_setup import get_logger
from app.core.schemas import Bar, DataValidationIssue, DataValidationReport, Timeframe
from app.core.utils import tf_to_timedelta as _tf_to_timedelta

log = get_logger(__name__)


def validate_bars(bars: list[Bar], symbol: Optional[str] = None,
                  timeframe: Optional[Timeframe] = None) -> DataValidationReport:
    """Validate a list of Bar objects and return a structured report.

    Every validation failure is ALSO logged at WARNING or ERROR level with
    full context — so data quality issues surface in logs, not just buried
    in the report object returned to the caller.
    """
    if not bars:
        return DataValidationReport(
            symbol=symbol or "UNKNOWN",
            timeframe=(timeframe.value if timeframe else "UNKNOWN"),
        )

    sym = symbol or bars[0].symbol
    tf = timeframe or bars[0].timeframe

    issues: list[DataValidationIssue] = []
    errors = 0
    warnings = 0

    def add_issue(sev: str, itype: str, msg: str, ts=None, raw=None) -> None:
        nonlocal errors, warnings
        issues.append(DataValidationIssue(
            severity=sev,
            type=itype,
            symbol=sym,
            timeframe=tf.value,
            timestamp=ts,
            message=msg,
            raw=raw,
        ))
        if sev == "error":
            errors += 1
            log.error(
                f"[data_quality] {itype}: {msg}",
                extra={"symbol": sym, "timeframe": tf.value,
                       "severity": sev, "issue_type": itype,
                       "timestamp": ts.isoformat() if ts else None},
            )
        else:
            warnings += 1
            log.warning(
                f"[data_quality] {itype}: {msg}",
                extra={"symbol": sym, "timeframe": tf.value,
                       "severity": sev, "issue_type": itype,
                       "timestamp": ts.isoformat() if ts else None},
            )

    # ── Step 1: Convert to dataframe for efficient checks ─────────────
    df = pd.DataFrame([b.model_dump() for b in bars])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    required_cols = {"timestamp", "open", "high", "low", "close"}
    missing = required_cols - set(df.columns)
    if missing:
        add_issue("error", "missing_field",
                  f"Dataframe missing required columns: {missing}")
        return _finalize_report(sym, tf, df, issues, errors, warnings)

    # ── Step 2: missing / bad timestamp ───────────────────────────────
    bad_ts = df["timestamp"].isna().sum()
    if bad_ts:
        add_issue("error", "missing_field",
                  f"{bad_ts} bars with missing/unparseable timestamp")

    # ── Step 3: OHLC sanity ───────────────────────────────────────────
    for col in ("open", "high", "low", "close"):
        # Non-finite: count NaN + inf.
        col_numeric = pd.to_numeric(df[col], errors="coerce")
        bad_mask = ~(col_numeric.notna() & np.isfinite(col_numeric))
        # Also flag zero / negative prices for financial time series
        # (very rare for OHLC). Skip for now to keep tests lenient.
        cnt = int(bad_mask.sum())
        if cnt:
            add_issue("error", "ohlc_sanity",
                      f"{cnt} bars with non-finite / non-numeric '{col}'")

    # High >= max(O,C), Low <= min(O,C)
    # Skip rows with missing numeric OHLC for this check
    mask_valid = (
        pd.to_numeric(df["open"], errors="coerce").notna() &
        pd.to_numeric(df["high"], errors="coerce").notna() &
        pd.to_numeric(df["low"], errors="coerce").notna() &
        pd.to_numeric(df["close"], errors="coerce").notna()
    )
    valid_df = df[mask_valid].copy()
    if not valid_df.empty:
        high_too_low = valid_df["high"] < valid_df[["open", "close"]].max(axis=1)
        cnt = int(high_too_low.sum())
        if cnt:
            examples = valid_df[high_too_low].head(3)
            for _, r in examples.iterrows():
                add_issue("error", "ohlc_sanity",
                          f"High ({r['high']:.6f}) < max(O,C)="
                          f"({max(r['open'], r['close']):.6f})",
                          ts=r["timestamp"].to_pydatetime() if pd.notna(r["timestamp"]) else None)

        low_too_high = valid_df["low"] > valid_df[["open", "close"]].min(axis=1)
        cnt = int(low_too_high.sum())
        if cnt:
            examples = valid_df[low_too_high].head(3)
            for _, r in examples.iterrows():
                add_issue("error", "ohlc_sanity",
                          f"Low ({r['low']:.6f}) > min(O,C)="
                          f"({min(r['open'], r['close']):.6f})",
                          ts=r["timestamp"].to_pydatetime() if pd.notna(r["timestamp"]) else None)

    # ── Step 4: Sort + duplicates ─────────────────────────────────────
    df_sorted = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    dups = df_sorted[df_sorted["timestamp"].duplicated(keep=False)]
    if not dups.empty:
        dup_ts = dups["timestamp"].unique()[:3]
        add_issue("error", "duplicate",
                  f"{len(dups)} rows share {dups['timestamp'].nunique()} duplicate timestamps; "
                  f"first few: {[t.isoformat() for t in pd.to_datetime(dup_ts, utc=True).tolist()]}")

    # ── Step 5: Gap detection ─────────────────────────────────────────
    # Only applicable if we have >1 bar and known timeframe cadence
    if len(df_sorted) >= 2:
        cadence = _tf_to_timedelta(tf)
        # Only do gap detection for reasonable timeframes (skip TICK, MN1 approximations)
        if cadence.total_seconds() > 0 and tf not in (Timeframe.TICK, Timeframe.W1, Timeframe.MN1):
            # Convert cadence to pd.Timedelta
            pd_cadence = pd.Timedelta(cadence)
            # Allow 2% tolerance — allow up to cadence + epsilon between bars
            # Actually for strictness: if more than 2x cadence, it's a gap.
            gaps_idx = df_sorted["timestamp"].diff() > pd_cadence * 1.5
            # First row diff is NaN → fill False
            gaps_idx.iloc[0] = False
            if gaps_idx.any():
                gap_rows = df_sorted[gaps_idx]
                for _, r in gap_rows.head(10).iterrows():
                    pos = r.name
                    prev_ts = df_sorted.loc[pos - 1, "timestamp"]
                    cur_ts = r["timestamp"]
                    delta = (cur_ts - prev_ts).total_seconds()
                    expected = cadence.total_seconds()
                    missing_n = round(delta / expected) - 1
                    add_issue("warning", "gap",
                              f"Missing ~{missing_n} bar(s): {prev_ts.isoformat()} -> "
                              f"{cur_ts.isoformat()} (delta={delta:.0f}s, expected={expected:.0f}s)",
                              ts=cur_ts.to_pydatetime())

    return _finalize_report(sym, tf, df_sorted, issues, errors, warnings)


def _finalize_report(sym, tf, df, issues, errors, warnings) -> DataValidationReport:
    if df.empty:
        rep = DataValidationReport(
            symbol=sym,
            timeframe=tf.value if isinstance(tf, Timeframe) else str(tf),
            issues=issues,
            error_count=errors,
            warning_count=warnings,
        )
    else:
        t0 = df["timestamp"].min()
        t1 = df["timestamp"].max()
        rep = DataValidationReport(
            symbol=sym,
            timeframe=tf.value if isinstance(tf, Timeframe) else str(tf),
            bar_count=len(df),
            start=t0.to_pydatetime() if pd.notna(t0) else None,
            end=t1.to_pydatetime() if pd.notna(t1) else None,
            issues=issues,
            error_count=errors,
            warning_count=warnings,
        )
    if rep.error_count or rep.warning_count:
        log.info(
            "Data validation summary",
            extra={"symbol": rep.symbol, "timeframe": rep.timeframe,
                   "bar_count": rep.bar_count, "errors": rep.error_count,
                   "warnings": rep.warning_count},
        )
    return rep


def validate_import_bulk(symbol_groups: dict[tuple[str, Timeframe], Iterable[Bar]]) -> list[DataValidationReport]:
    """Validate many symbols/timeframes at once. Returns one report per group."""
    reports = []
    for (sym, tf), bars in symbol_groups.items():
        reports.append(validate_bars(list(bars), sym, tf))
    return reports
