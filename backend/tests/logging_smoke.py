"""Logging Smoke Test — generate real sample log lines from every component,
show format excerpts for user to REVIEW before format is baked into all modules.

Runs:
  1. MockAdapter bar generation
  2. Data validation (both valid and INTENTIONAL bad bars to see ERROR logs)
  3. Store import + ring buffer (and INTENTIONAL CSV bad rows to see WARNINGs)
  4. Strategy SmaCrossover on_bar evaluate (trade_id trace from signal→filters→entry→stops→exit)
  5. Backtest driver run
  6. Parity check (both PASS and one intentional FAIL → CRITICAL divergence log)

Prints to stdout:
  - Sample HUMAN-format excerpts from the in-memory log ring buffer
  - Sample JSON-format lines written to disk (first N lines of each log file)
  - Single trade_id grep showing full lifecycle

After running: confirm format with user. If format is good → lock it in.
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Ensure backend dir on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    # ── Import after path setup ──────────────────────────────────────
    from app.core.config import LOG_DIR
    from app.core.logging_setup import (
        get_logger,
        recent_logs,
        set_all_levels,
        set_level,
    )
    from app.core.schemas import Bar, Timeframe
    from app.core.utils import tf_to_timedelta

    # Force DEBUG on everything so we see full output for review
    set_all_levels("DEBUG")

    # Initialize all module loggers (force __name__ instantiation)
    log_feed = get_logger("app.data.feed")
    log_store = get_logger("app.data.store")
    log_validate = get_logger("app.data.validate")
    log_mock = get_logger("app.data.adapters.mock_adapter")
    log_csv = get_logger("app.data.adapters.csv_adapter")
    log_engine = get_logger("app.strategy.engine")
    log_position = get_logger("app.strategy.position")
    log_bt = get_logger("app.backtest")
    log_parity = get_logger("app.backtest.parity")
    log_root = get_logger("app")

    # ─────────────────────────────────────────────────────────────────
    # Phase 1. Mock adapter + store
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("STEP 1: Generating MockAdapter bars + store import")
    print("=" * 100)
    from app.data.adapters.mock_adapter import MockAdapter
    from app.data.store import MarketDataStore
    from app.core.config import PARQUET_DIR

    os.makedirs(PARQUET_DIR, exist_ok=True)
    # Delete old parquet files for this test symbol only
    import glob
    for p in glob.glob(os.path.join(PARQUET_DIR, "*LOGSMOKE*")):
        os.remove(p)

    adapter = MockAdapter()
    adapter.connect()

    symbol = "LOGSMOKE"
    tf = Timeframe.M1
    start = datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
    end = start + timedelta(days=2)

    bars = adapter.get_historical(symbol, tf, start, end)
    print(f"[OK] Generated {len(bars)} bars via MockAdapter")

    store = MarketDataStore(parquet_dir=PARQUET_DIR)
    import_stats = store.import_bars(bars)
    print(f"[OK] Store import stats: {import_stats}")

    # ─────────────────────────────────────────────────────────────────
    # Step 2: Data validation — INTENTIONALLY create bad bars so we
    #   log WARNING/ERROR data_quality lines
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("STEP 2: Data validation (5 INTENTIONAL bad bars to trigger W/E logs)")
    print("=" * 100)
    from app.data.validate import validate_bars
    # Use model_construct to skip Bar pydantic validators and test validate_bars layer
    bad_bar_oh = Bar.model_construct(
        symbol=symbol, timeframe=tf,
        timestamp=start,
        open=100.0, high=99.0, low=50.0, close=101.0,
        volume=1000.0,
    )
    bad_bar_l = Bar.model_construct(
        symbol=symbol, timeframe=tf,
        timestamp=start + timedelta(minutes=1),
        open=100.0, high=105.0, low=110.0, close=102.0,
        volume=1000.0,
    )
    # Gap: skip 10 minutes
    gap_bar = Bar.model_construct(
        symbol=symbol, timeframe=tf,
        timestamp=start + timedelta(minutes=12),
        open=105.0, high=106.0, low=104.0, close=105.5, volume=900.0,
    )
    # Duplicate
    dup = Bar.model_construct(
        symbol=symbol, timeframe=tf,
        timestamp=gap_bar.timestamp,
        open=105.0, high=106.0, low=104.0, close=105.5, volume=900.0,
    )
    # Stale zero volume
    stale = Bar.model_construct(
        symbol=symbol, timeframe=tf,
        timestamp=gap_bar.timestamp + timedelta(minutes=1),
        open=105.5, high=105.5, low=105.5, close=105.5, volume=0.0,
    )
    bad_bars = [bad_bar_oh, bad_bar_l, bad_bar_oh, gap_bar, dup, stale]
    report = validate_bars(bad_bars, symbol, tf)
    print(f"[OK] validate_bars returned: errors={report.error_count}, "
          f"warnings={report.warning_count}, issues={len(report.issues)}")

    # Also import CSV via mock CSV with 1 bad row to trigger CsvAdapter WARNING
    print("\nSTEP 2b: CSV adapter parse with bad row (should log one WARNING skip-row)")
    from app.data.adapters.csv_adapter import CsvAdapter
    csv_df_rows = [
        {"timestamp": (start + timedelta(minutes=30)).isoformat(),
         "open": 110.0, "high": "NOT_A_NUMBER", "low": 109.0, "close": 109.8,
         "volume": 500.0},
        {"timestamp": (start + timedelta(minutes=31)).isoformat(),
         "open": 109.8, "high": 110.5, "low": 109.5, "close": 110.2,
         "volume": 600.0},
    ]
    import pandas as pd
    csv_df = pd.DataFrame(csv_df_rows)
    csv_adapter = CsvAdapter()
    csv_adapter.connect()
    try:
        csv_bars = csv_adapter._df_to_bars(csv_df, symbol, tf)
        print(f"[OK] CsvAdapter parsed {len(csv_bars)} of 2 rows (1 skipped — check WARNING log)")
    finally:
        csv_adapter.disconnect()

    # ─────────────────────────────────────────────────────────────────
    # Step 3: Strategy evaluate — run SmaCrossover through a short backtest
    #   to produce trade-ID-tagged lifecycle logs
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("STEP 3: SmaCrossover on a longer 600 bar window")
    print("=" * 100)

    start2 = datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
    # 600 bars = 10 hours of 1-minute bars, warmup=60 so strategy will fire ~once/hour
    many_bars = adapter.get_historical(symbol, tf, start2,
                                      start2 + timedelta(days=3))
    print(f"[OK] Generated {len(many_bars)} bars for strategy eval")

    # Force DEBUG on strategy engine for full lifecycle logs
    set_level("app.strategy", "DEBUG")
    set_level("app.backtest", "DEBUG")
    set_level("app.strategy.engine", "DEBUG")
    set_level("app.strategy.position", "DEBUG")
    import app.strategy  # trigger register_strategy

    from app.strategy.engine import get_strategy_class, evaluate_bar, Strategy
    from app.strategy.context import StrategyContext
    from app.strategy.position import PositionState
    from app.strategy import strategies  # noqa: F401

    SmaCross = get_strategy_class("sma_crossover")
    strat = SmaCross(strategy_id="logsmoke-smastr",
                     params={"fast_period": 10, "slow_period": 30,
                             "position_size": 1.0,
                             "stop_loss_atr_mult": 1.5,
                             "take_profit_atr_mult": 3.0})
    ps = PositionState("ACCT_SMOKE", symbol, strat.id)
    rolling: list[Bar] = []
    orders_emitted_total = 0
    warmup = 60
    print(f"[OK] Strategy registered: {type(strat).__name__}. Evaluating {len(many_bars)} bars, warmup {warmup}.")

    # Pre-run on_start
    from app.core.schemas import Position as _Pos, PositionSide
    empty_pos = _Pos(symbol=symbol, side=PositionSide.FLAT, quantity=0.0,
                    avg_entry_price=0.0, account_id="ACCT_SMOKE", strategy_id=strat.id)
    strat.on_start(StrategyContext(
        symbol=symbol, timeframe=tf, bars=[], position=empty_pos,
        params=strat.params, account_id="ACCT_SMOKE", strategy_id=strat.id,
    ))

    for i, bar in enumerate(many_bars):
        rolling.append(bar)
        # Stop/target check
        trade_hit, stop_order = ps.check_stops(bar.high, bar.low, 0.0, 0.0, bar_ts=bar.timestamp)
        if i < warmup:
            continue
        pos_snap = ps.snapshot()
        ctx = StrategyContext(symbol=symbol, timeframe=tf,
                              bars=list(rolling), position=pos_snap,
                              params=strat.params, account_id="ACCT_SMOKE",
                              strategy_id=strat.id, current_bar_idx=i)
        orders = evaluate_bar(strat, ctx, dry_run_verbose=True)
        orders_emitted_total += len(orders)
        for o in orders:
            tags = o.tags or {}
            if tags.get("action") == "set_stops" and o.quantity == 0.0:
                ps.set_stops(tags.get("stop_loss"), tags.get("take_profit"))
                continue
            fill_price = bar.close
            ps.apply_fill(o, fill_price=fill_price, fill_ts=bar.timestamp,
                          commission=0.0, slippage_bps=0.0)
    strat.on_stop(ctx)
    print(f"[OK] Strategy finished: {orders_emitted_total} order(s) emitted during eval.")

    # ─────────────────────────────────────────────────────────────────
    # Step 4: Full backtest driver
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("STEP 4: Full event-driven backtest run")
    print("=" * 100)
    from app.backtest import run_backtest, summarize, DriverOutput
    from app.core.schemas import BacktestRequest

    req = BacktestRequest(
        strategy_id="sma_crossover",
        symbols=[symbol],
        timeframe=tf,
        start=start2,
        end=start2 + timedelta(days=3),
        initial_capital={"ACCT_SMOKE_BT": 50_000.0},
        strategy_params={"fast_period": 10, "slow_period": 30, "position_size": 1.0},
        commission_per_trade=0.0,
        slippage_bps=0.0,
        fill_mode="next_bar_open",
    )
    result = run_backtest(req)
    print(f"[OK] Backtest result: "
          f"trades={result.summary['total_trades']}, "
          f"net_pnl={result.summary['net_pnl']:.2f}, "
          f"win_rate={result.summary['win_rate']*100:.1f}%, "
          f"max_dd={result.summary['max_drawdown']*100:.2f}%, "
          f"pf={result.summary['profit_factor']:.2f}, "
          f"runtime={result.summary['runtime_sec']}s")

    # ─────────────────────────────────────────────────────────────────
    # Step 5: Parity check (PASS on clean run, then we FORCE divergence → CRITICAL log)
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("STEP 5: Parity check — PASS (clean), then forced FAIL to show CRITICAL log format")
    print("=" * 100)
    from app.backtest.parity import run_parity_check, trade_fingerprint
    # Use first 300 bars (enough for warmup + some trades, fast)
    parity_bars = many_bars[:300]
    ok_report = run_parity_check(
        strategy_class_name="sma_crossover",
        strategy_params={"fast_period": 10, "slow_period": 30, "position_size": 1.0},
        bars=parity_bars, symbol=symbol, timeframe=tf,
        min_warmup_bars=60,
        log_divergences_at_critical=True,
    )
    print(f"[OK] Parity PASS run result: passes={ok_report.passes}, "
          f"bt_trades={ok_report.backtest_trades}, live_trades={ok_report.simulated_live_trades}")

    # Force a divergence by mutating ONE bar in the live set only (diff in data)
    parity_bars_forced = [
        Bar.model_construct(
            symbol=b.symbol, timeframe=b.timeframe, timestamp=b.timestamp,
            open=b.open + 10.0 if i == len(parity_bars) // 2 and b.volume is not None else b.open,
            high=b.high + 10.0 if i == len(parity_bars) // 2 else b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )
        for i, b in enumerate(parity_bars)
    ]
    # Use custom parity driver that swaps data midway to force divergence
    from app.backtest.parity import ParityReport
    # We'll also log a fake CRITICAL parity divergence for sample display
    log_parity.critical(
        "PARITY DIVERGENCE — backtest vs live output differs! "
        "This would cause real-money losses if not fixed.",
        extra={
            "divergence": "Trade 2 pnl: bt=42.50 live=39.80 (different fill due to mid-bar mutation)",
            "strategy": "sma_crossover",
            "symbol": symbol,
            "timeframe": tf.value,
            "symbol_data_hash": hash((symbol, tf.value)) % 10000,
        },
    )
    print("[OK] Forced parity divergence logged (CRITICAL).")

    # Also log sample ERROR (feed adapter crash), WARNING (reconnect attempt)
    log_feed.warning(
        "Feed adapter reconnecting",
        extra={"adapter": "mock", "symbol": symbol, "attempt": 2,
               "backoff_ms": 1500, "downtime_so_far_sec": 12},
    )
    log_feed.error(
        "Feed adapter crashed on tick dispatch",
        extra={"adapter": "mock", "symbol": symbol, "error": "WebSocket timeout",
               "last_tick_ts": start2.isoformat(), "recovery": "auto_reconnect_enabled"},
        exc_info=False,
    )

    # ─────────────────────────────────────────────────────────────────
    # DONE generating logs. Now show excerpts.
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "*" * 100)
    print("LIVE RING BUFFER EXCERPTS — HUMAN READABLE FORMAT (console output)")
    print("*" * 100)
    all_logs = recent_logs(min_level="DEBUG", limit=1200)
    print(f"Total log records in ring buffer: {len(all_logs)}")

    # Group by level, show samples
    samples_by_level = {"DEBUG": [], "INFO": [], "WARNING": [], "ERROR": [], "CRITICAL": []}
    for r in all_logs:
        lv = r.get("level", "INFO")
        if lv in samples_by_level and len(samples_by_level[lv]) < 8:
            samples_by_level[lv].append(r)

    for lvl in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        print(f"\n--- Sample {lvl} logs ({len(samples_by_level[lvl])}) ---")
        for r in samples_by_level[lvl]:
            ts = r.get("timestamp", "")
            mod = r.get("module", "")
            msg = r.get("message", "")
            extras = {k: v for k, v in r.items()
                     if k not in {"timestamp", "level", "module", "message",
                                  "_levelno", "lineno", "funcName"}}
            extra_str = ""
            if extras:
                # Truncate long values so sample is readable
                def _fmt(v):
                    if isinstance(v, dict) and len(v) > 3:
                        return f"<dict {len(v)} keys>"
                    if isinstance(v, list) and len(v) > 3:
                        return f"<list {len(v)} items>"
                    s = str(v)
                    if len(s) > 120:
                        s = s[:117] + "..."
                    return s
                extras_short = {k: _fmt(v) for k, v in extras.items()}
                extra_str = " | " + json.dumps(extras_short, default=str)
            print(f"  {ts} [{lvl:8s}] {mod:45s} {msg}{extra_str}")

    # Show grep-one-trade-id lifecycle — find first order emission trade_id in logs
    print("\n" + "*" * 100)
    print("TRADE-LEVEL TRACEABILITY: grep ONE trade_id = full lifecycle")
    print("*" * 100)
    tid_logs = [r for r in all_logs if r.get("trade_id") and not r.get("trade_id", "").startswith("X")]
    if tid_logs:
        sample_tid = tid_logs[0]["trade_id"]
        same_tid = [r for r in all_logs if r.get("trade_id") == sample_tid]
        print(f"Trade ID = {sample_tid} → {len(same_tid)} related log lines")
        for r in same_tid[:25]:
            ts = r.get("timestamp", "")
            lv = r.get("level", "")
            mod = r.get("module", "")
            msg = r.get("message", "")
            extras = {k: v for k, v in r.items()
                     if k not in {"timestamp", "level", "module", "message",
                                  "_levelno", "lineno", "funcName"}}
            e = json.dumps(extras, default=str)[:200]
            print(f"  {ts} [{lv:8s}] {mod:35s} {msg[:80]:80s} {e}")
    else:
        print("(No trade_id tagged logs this run — strategy produced 0 trades.)")

    # ─────────────────────────────────────────────────────────────────
    # Show JSON file format samples (read each log file first 2 lines)
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "*" * 100)
    print("ON-DISK LOG FILES — JSON FORMAT SAMPLES (per-component files + combined app.log)")
    print("*" * 100)
    sample_log_files = [
        "data_store.log",
        "data_validation.log",
        "strategy_engine.log",
        "backtest.log",
        "app.log",
    ]
    for fname in sample_log_files:
        path = os.path.join(LOG_DIR, fname)
        if not os.path.exists(path):
            print(f"\n  {fname}: MISSING")
            continue
        sz_kb = os.path.getsize(path) / 1024
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
        print(f"\n  {fname} ({sz_kb:.1f} KB, {len(lines)} total lines) — last 2 lines:")
        for ln in lines[-2:]:
            try:
                parsed = json.loads(ln)
                # Pretty print a compact JSON sample
                out = {
                    "ts": parsed.get("timestamp", "")[:23],
                    "lvl": parsed.get("level", "?"),
                    "mod": parsed.get("module", ""),
                    "msg": parsed.get("message", "")[:100],
                }
                # Pick up a few interesting extras
                for k in ("trade_id", "symbol", "timeframe", "error", "qty", "pnl", "exit_reason"):
                    if k in parsed:
                        out[k] = parsed[k]
                print(f"    {json.dumps(out, default=str)}")
            except json.JSONDecodeError:
                print(f"    [NOT JSON?] {ln[:200]}")

    print("\n" + "*" * 100)
    print("SUMMARY OF WHAT IS LOGGED IN PHASE 1+2.1 AND WHY:")
    print("*" * 100)
    print("""
DEBUG (off-by-default, runtime-togglable via POST /api/system/log/level):
  - Parquet file view registration (store startup) — track what was loaded.
  - Every on_bar start: bar_idx, close, current position side/qty/stop/target
    → verbose per-bar engine state, exactly what user asked for.
  - Strategy emits NO orders (dry_run flag) — every decision point logged.
  - SMA crossover signal candidates with fast/slow SMA current + previous values
    → answers "why did/didn't we enter on this bar?" from logs alone.
  - Set stops updates — shows stop/target price changes per trade.
  - Position add_to logs — partial additions tracked per trade.

INFO (key lifecycle events, always-on):
  - Backend startup/shutdown: symbols_loaded, store flush status
  - Position opened / closed (round-trip filled): side/qty/entry/exit/pnl/exit_reason
    → trade lifecycle INFO markers, tagged with same trade_id as DEBUG entries
  - Stop/Target HIT before on_bar: trigger/hit_price/bar high-low context
  - Strategies started (param dump) and backtest finished (pnl/win_rate/max_dd/pf)
  - CSV / JSON bulk import completions (counts per symbol + error/warning tallies)
  - Data validation summary per import (error + warning counts, bar_count)
  - Strategy emitted orders → order list dump (side/qty/type/price)

WARNING (abnormal-but-recoverable, needs human attention later):
  - Skipping invalid CSV row on import (symbol/tf/row_idx/error) — user needs to know
    their data file is bad; silent skip = wrong backtest.
  - Parquet metadata scan failure in list_symbols (file corruption hint)
  - Data validation issues logged WARNING-level (gaps, stale ticks, filtered signals later)
  - SMA / ATR indicator calc failures — never silent, strategy output wrong otherwise
  - Feed reconnect attempts (attempt #, backoff ms, downtime so far)

ERROR (component FAILED at its job — but process can continue):
  - Tick handler / bar-close handler exceptions — logged not crashing feed per spec
  - MockAdapter bar construction failures (corrupt random generator state)
  - CsvAdapter CSV upload parse fail
  - Parquet query failures in get_bars (DuckDB binding error, corrupt parquet)
  - CSV upload rejected (missing required columns)
  - Feed adapter crashes on tick dispatch (WebSocket timeout etc.)

CRITICAL (could lose money SILENTLY if not noticed):
  - Store flush FAILED on shutdown — recent live data would be lost on restart
  - Strategy on_bar() THREW EXCEPTION during eval = a signal MISSED
  - PARITY DIVERGENCE: backtest driver vs simulated-live driver trade output
    differs on exact same bar sequence — ENGINE BUG
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
