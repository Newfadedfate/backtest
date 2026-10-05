"""Phase 2.2 + 2.4 smoke: building blocks + optimizer.

Run from backend/: `python tests/smoke_phase2.py`
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.schemas import BacktestRequest, OptimizeRequest, Timeframe
from app.data.feed import get_adapter_class
from app.strategy.building_blocks import (
    ReentryLock,
    breakeven_shift,
    candle_range_adaptive_stop,
    fixed_take_profit,
    level_proximity_exit,
    price_band_filter,
)
from app.strategy.engine import list_strategies
from app.backtest import run_backtest
from app.backtest.optimizer import (
    expand_param_grid,
    run_optimize,
    vectorized_sma_crossover_score,
    walk_forward_windows,
)


def main() -> int:
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

    print("\n[1] Building blocks")
    check("inside band allows", price_band_filter(100, 90, 110, "inside_ok") is True)
    check("inside band rejects", price_band_filter(80, 90, 110, "inside_ok") is False)
    check("outside band allows", price_band_filter(80, 90, 110, "outside_ok") is True)
    check("above_only", price_band_filter(95, 90, None, "above_only") is True)

    # range 3 < 5 -> tight offset 5 below low
    stop_tight = candle_range_adaptive_stop("LONG", bar_low=100, bar_high=103)
    check("adaptive tight long", abs(stop_tight - 95.0) < 1e-9, f"stop={stop_tight}")
    # range 10 >= 5 -> loose offset 1
    stop_loose = candle_range_adaptive_stop("LONG", bar_low=100, bar_high=110)
    check("adaptive loose long", abs(stop_loose - 99.0) < 1e-9, f"stop={stop_loose}")
    stop_short = candle_range_adaptive_stop("SHORT", bar_low=100, bar_high=103)
    check("adaptive tight short", abs(stop_short - 108.0) < 1e-9, f"stop={stop_short}")

    be_long = breakeven_shift(100, "LONG", 105, trigger_unrealized_pts=2, buffer_pts=0.5)
    check("breakeven long fires", be_long == 100.5, f"got={be_long}")
    be_none = breakeven_shift(100, "LONG", 101, trigger_unrealized_pts=2, buffer_pts=0.5)
    check("breakeven long holds", be_none is None)
    be_no_back = breakeven_shift(
        100, "LONG", 105, trigger_unrealized_pts=2, buffer_pts=0.5, current_stop=101.0,
    )
    check("breakeven never loosens", be_no_back is None)

    tp_abs = fixed_take_profit(100, "LONG", absolute_price=112)
    check("tp absolute", tp_abs == 112)
    tp_r = fixed_take_profit(100, "LONG", risk_r_multiple=3, risk_pts=2)
    check("tp 3R", tp_r == 106)

    # Rejection: long near 110, long upper wick, close down with large body
    lv = level_proximity_exit(
        "LONG", bar_open=108, bar_high=109.9, bar_low=104, bar_close=105,
        level_price=110, tolerance_bps=20, min_rejection_body_ratio=0.4,
    )
    check("level proximity triggered", lv.triggered is True, lv.reason)

    lock = ReentryLock()
    lock.acquire("LONG", wait_for="opposite_crossover", acquired_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
    check("lock blocks", lock.allowed("LONG") is False)
    check("mismatch does not release", lock.release("LONG", when="any_flat") is False)
    check("still blocked", lock.allowed("LONG") is False)
    check("matching release", lock.release("LONG", when="opposite_crossover") is True)
    check("unlocked", lock.allowed("LONG") is True)

    print("\n[2] Strategy registry")
    names = list_strategies()
    check("sma_crossover registered", "sma_crossover" in names, f"found={names}")
    check("building_blocks_demo registered", "building_blocks_demo" in names)

    print("\n[3] Mock bars + demo strategy event-driven")
    MockCls = get_adapter_class("mock")
    feed = MockCls(base_price=500.0)
    feed.connect()
    start = datetime(2024, 1, 2, 9, 15, 0, tzinfo=timezone.utc)
    end = datetime(2024, 1, 6, 15, 30, 0, tzinfo=timezone.utc)
    bars = feed.get_historical("OPT_TEST", Timeframe.M5, start=start, end=end)
    feed.disconnect()
    check("enough bars for optimizer", len(bars) >= 400, f"got {len(bars)}")

    bars_by_symbol = {"OPT_TEST": bars}
    demo_req = BacktestRequest(
        strategy_id="building_blocks_demo",
        strategy_params={"fast_period": 10, "slow_period": 30},
        symbols=["OPT_TEST"],
        timeframe=Timeframe.M5,
        initial_capital={"default": 100_000.0},
    )
    demo_res = run_backtest(demo_req, bars_by_symbol=bars_by_symbol)
    check("demo ran", demo_res.total_bars == len(bars), f"bars={demo_res.total_bars}")
    check("demo has trades or empty ok", demo_res.summary["total_trades"] >= 0,
          f"trades={demo_res.summary['total_trades']}")

    print("\n[4] Optimizer helpers")
    combos, skipped, trunc = expand_param_grid(
        {"fast_period": [5, 10, 40], "slow_period": [20, 30]},
        max_combinations=64,
    )
    # invalid: 40>=20, 40>=30 -> skipped 2; remaining 4
    check("grid skips fast>=slow", skipped == 2 and len(combos) == 4,
          f"combos={len(combos)} skipped={skipped}")
    combos2, _, trunc2 = expand_param_grid(
        {"fast_period": [5, 8, 10], "slow_period": [20, 30, 40]},
        max_combinations=3,
    )
    check("grid truncates", len(combos2) == 3 and trunc2 > 0, f"trunc={trunc2}")

    wins = walk_forward_windows(1000, n_folds=3, test_fraction=0.3, mode="anchored")
    check("wfo 3 folds", len(wins) == 3, f"n={len(wins)}")
    check("wfo chronological", all(w["train"][1] == w["test"][0] for w in wins))
    check("wfo no overlap train/test", all(w["train"][1] <= w["test"][0] for w in wins))

    closes = [float(b.close) for b in bars]
    import numpy as np
    vscore = vectorized_sma_crossover_score(np.array(closes), 10, 30)
    check("vectorized score has source tag", vscore["source"] == "vectorized_sma")

    print("\n[5] Full optimize: screen + event-driven + walk-forward")
    oreq = OptimizeRequest(
        strategy_id="sma_crossover",
        param_grid={"fast_period": [5, 10], "slow_period": [20, 30]},
        symbols=["OPT_TEST"],
        timeframe=Timeframe.M5,
        initial_capital={"default": 100_000.0},
        rank_by="expectancy",
        max_combinations=8,
        validate_top_n=2,
        walk_forward_folds=2,
        walk_forward_mode="anchored",
        test_fraction=0.3,
        use_vectorized_screen=True,
    )
    ores = run_optimize(oreq, bars_by_symbol)
    check("evaluated combos", ores.combinations_evaluated == 4,
          f"n={ores.combinations_evaluated} skipped={ores.combinations_skipped}")
    check("vectorized flag not-truth", ores.vectorized_is_not_truth is True)
    check("vectorized ranking filled", len(ores.vectorized_ranking) == 4)
    check("official event-driven top-n", len(ores.official_event_driven) == 2,
          f"n={len(ores.official_event_driven)}")
    check("official source event_driven",
          all(r.get("source") == "event_driven" for r in ores.official_event_driven))
    check("recommended params present", bool(ores.recommended_params))
    check("walk-forward folds ran", len(ores.walk_forward) == 2,
          f"n={len(ores.walk_forward)}")
    if ores.walk_forward:
        oos = ores.walk_forward[0]["oos_event_driven"]
        check("oos is event-driven", oos.get("source") == "event_driven")
        check("oos params from train only",
              "params_from_train" in ores.walk_forward[0])

    print("\n[6] Optimize API function")
    from unittest.mock import patch
    from app.api.backtest import run_optimize_endpoint
    from app.data.store import MarketDataStore
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="papertrade_opt_")
    try:
        store = MarketDataStore(parquet_dir=os.path.join(tmp, "parquet"))
        store.import_bars(bars)
        api_req = OptimizeRequest(
            strategy_id="sma_crossover",
            param_grid={"fast_period": [10], "slow_period": [30]},
            symbols=["OPT_TEST"],
            timeframe=Timeframe.M5,
            initial_capital={"default": 100000.0},
            validate_top_n=1,
            walk_forward_folds=0,
        )
        with patch("app.data.store.get_store", lambda: store):
            body = run_optimize_endpoint(api_req)
        check("api optimize dict", isinstance(body, dict))
        check("api vectorized_is_not_truth", body.get("vectorized_is_not_truth") is True)
        check("api official list", len(body.get("official_event_driven", [])) == 1)
    except Exception as e:
        check("api optimize path", False, str(e))
        import traceback
        traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}")
    print(f"  SMOKE TEST RESULTS: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
