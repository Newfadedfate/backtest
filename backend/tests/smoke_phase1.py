"""Phase 1 end-to-end smoke test.

Exercises:
1. DataFeed registry (mock adapter load)
2. MockAdapter.get_historical() deterministic generation
3. Data validation on generated clean bars
4. MarketDataStore.import_bars() → Parquet write + ring buffer
5. Store.get_bars() → merged hist + live retrieval
6. Store.list_symbols() metadata
7. Duplicate bar rejection in store + validation catch

Run from backend/ dir: `python tests/smoke_phase1.py`
"""

from __future__ import annotations

import os, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

# Ensure backend/ is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.schemas import Bar, Timeframe
from app.data.feed import list_adapters, get_adapter_class
from app.data.store import MarketDataStore
from app.data.validate import validate_bars


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="papertrade_smoke_")
    tmp_parquet = os.path.join(tmp, "parquet")
    passed = 0
    failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [OK]  {name} {detail}")
        else:
            failed += 1
            print(f"  [FAIL]  {name} FAILED  {detail}")

    try:
        # ── 1. Adapter registry ───────────────────────────────────────
        print("\n[1] DataFeed registry")
        adapters = list_adapters()
        check("mock adapter registered", "mock" in adapters, f"found={adapters}")
        check("csv adapter registered", "csv" in adapters)

        # ── 2. Mock feed deterministic generation ─────────────────────
        print("\n[2] MockAdapter.get_historical()")
        MockCls = get_adapter_class("mock")
        feed = MockCls(base_price=500.0)
        feed.connect()
        start = datetime(2024, 1, 2, 9, 15, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, 15, 30, 0, tzinfo=timezone.utc)
        bars = feed.get_historical("NIFTY_TEST", Timeframe.M5, start=start, end=end)
        check("bars generated", len(bars) >= 10, f"got {len(bars)} bars")
        check("start >= requested start", bars[0].timestamp >= start,
              f"start={bars[0].timestamp}")
        check("end <= requested end", bars[-1].timestamp <= end,
              f"end={bars[-1].timestamp}")
        check("bars sorted ASC", all(bars[i].timestamp < bars[i + 1].timestamp for i in range(len(bars) - 1)))
        check("first bar close > 0", bars[0].close > 0, f"close={bars[0].close:.2f}")
        # Determinism: same (sym, tf) → same seed → same output
        bars2 = feed.get_historical("NIFTY_TEST", Timeframe.M5, start=start, end=end)
        check("mock deterministic", [b.close for b in bars] == [b.close for b in bars2],
              "both runs identical")
        feed.disconnect()

        # ── 3. Validation on clean bars (should pass) ─────────────────
        print("\n[3] Data validation (clean mock bars)")
        report = validate_bars(bars, "NIFTY_TEST", Timeframe.M5)
        check("zero errors", report.error_count == 0, f"errors={report.error_count}")
        print(f"     warnings: {report.warning_count}, issues: {len(report.issues)}")

        # ── 3b. Validation on INTENTIONALLY bad data ─────────────────
        print("\n[3b] Data validation (INTENTIONALLY bad bars)")
        # Use model_construct to skip Pydantic's built-in OHLC validator —
        # this lets us deliver bad data to validate_bars() so we can test it.
        bad_bar = Bar.model_construct(
            symbol="BAD", timeframe=Timeframe.M5,
            timestamp=datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc),
            open=100.0, high=50.0, low=90.0, close=95.0, volume=1000,
        )
        bad_report = validate_bars([bad_bar], "BAD", Timeframe.M5)
        check("catches ohlc error", bad_report.error_count >= 1,
              f"errors={bad_report.error_count}  issue_types={[i.type for i in bad_report.issues]}")

        # Dupe
        dupe_ts = datetime(2024, 1, 2, 11, 0, tzinfo=timezone.utc)
        dupes = [
            Bar.model_construct(symbol="D", timeframe=Timeframe.M5, timestamp=dupe_ts,
                                open=100, high=101, low=99, close=100.5),
            Bar.model_construct(symbol="D", timeframe=Timeframe.M5, timestamp=dupe_ts,
                                open=100, high=101, low=99, close=100.8),
        ]
        dupe_report = validate_bars(dupes, "D", Timeframe.M5)
        check("catches duplicates", any(i.type == "duplicate" for i in dupe_report.issues))

        # ── 4. MarketDataStore ────────────────────────────────────────
        print("\n[4] MarketDataStore import + query")
        store = MarketDataStore(parquet_dir=tmp_parquet, ring_buffer_size=1000)
        stats = store.import_bars(bars)
        tf_val = Timeframe.M5.value  # "5m"
        key = f"NIFTY_TEST_{tf_val}"
        check("import stats correct", stats.get(key) == len(bars),
              f"stats={stats} expected key={key} value={len(bars)}")
        parquet_path = os.path.join(tmp_parquet, f"NIFTY_TEST_{tf_val}.parquet")
        file_exists = os.path.exists(parquet_path)
        check("parquet file written", file_exists,
              f"path exists={file_exists} size={os.path.getsize(parquet_path) if file_exists else 'N/A'}")

        # Ring buffer populated
        ring = store._ring_buffers.get(("NIFTY_TEST", tf_val))
        check("ring buffer populated", ring is not None and len(ring) == min(len(bars), 1000),
              f"ring len={len(ring) if ring else None}")

        # ── 5. Store.get_bars() ───────────────────────────────────────
        print("\n[5] Store.get_bars()")
        retrieved = store.get_bars("NIFTY_TEST", Timeframe.M5)
        check("retrieved == imported count", len(retrieved) == len(bars),
              f"retrieved={len(retrieved)} imported={len(bars)}")
        check("retrieved sorted ASC",
              all(retrieved[i].timestamp < retrieved[i + 1].timestamp
                  for i in range(len(retrieved) - 1)))

        # Time filter
        mid = bars[len(bars) // 2].timestamp
        filtered = store.get_bars("NIFTY_TEST", Timeframe.M5, start=mid)
        check("start filter works", len(filtered) > 0 and all(b.timestamp >= mid for b in filtered),
              f"got {len(filtered)} bars")

        # Limit
        limited = store.get_bars("NIFTY_TEST", Timeframe.M5, limit=10)
        check("limit works", len(limited) == 10, f"got {len(limited)}")

        # ── 6. list_symbols metadata ──────────────────────────────────
        print("\n[6] Store.list_symbols()")
        syms = store.list_symbols()
        my_sym = next((s for s in syms if s["symbol"] == "NIFTY_TEST"), None)
        check("symbol listed", my_sym is not None, f"items={len(syms)}")
        if my_sym:
            check("bar_count matches", my_sym["bar_count"] >= len(bars),
                  f"bar_count={my_sym['bar_count']} expected>={len(bars)}")
            check("first_ts populated", bool(my_sym.get("first_ts")))
            check("last_ts populated", bool(my_sym.get("last_ts")))

        # ── 7. Duplicate rejection on re-import (dedup) ───────────────
        print("\n[7] Re-import same bars → dedup")
        before = len(store.get_bars("NIFTY_TEST", Timeframe.M5))
        store.import_bars(bars)  # same exact bars
        after = len(store.get_bars("NIFTY_TEST", Timeframe.M5))
        check("no duplicates on reimport", after == before,
              f"before={before} after={after}")

        # ── 8. Validation catches gap if we skip timestamps ───────────
        print("\n[8] Gap detection in validation")
        # Intentionally skip a bar (delete one from middle)
        gapped = [bars[0], bars[1]] + bars[3:]  # skip bars[2] — one gap
        gap_report = validate_bars(gapped, "NIFTY_TEST", Timeframe.M5)
        gap_issues = [i for i in gap_report.issues if i.type == "gap"]
        check("gap detected", len(gap_issues) >= 1, f"gap_issues={len(gap_issues)}")

        # ── 9. Store flush + ring buffer clear + re-read from disk ────
        print("\n[9] Store.flush() persistence")
        store.flush()  # ring → parquet, ring cleared
        ring2 = store._ring_buffers.get(("NIFTY_TEST", tf_val))
        check("ring cleared after flush", ring2 is None or len(ring2) == 0,
              f"ring len after flush: {len(ring2) if ring2 else 'None'}")
        # Re-read should still work from parquet
        reread = store.get_bars("NIFTY_TEST", Timeframe.M5, include_ring=False)
        check("parquet reread intact", len(reread) == len(bars),
              f"reread={len(reread)} original={len(bars)}")

        print(f"\n{'=' * 60}")
        print(f"  SMOKE TEST RESULTS: {passed} passed, {failed} failed")
        print(f"{'=' * 60}")
        return 1 if failed else 0

    finally:
        # Cleanup
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
