"""Phase 2.4 — Parameter-sweep optimizer.

Architecture (non-negotiable per spec):
  Vectorized mode is a FAST SCREEN only. It is NEVER the source of truth.
  Official results always come from the event-driven backtester (`run_backtest`).

Flow:
  1. Expand the param grid (cartesian product, skip invalid combos).
  2. Optionally rank combos with a numpy SMA-crossover proxy (screening).
  3. Re-run the top-N combos through the event-driven engine (official IS).
  4. Optional walk-forward: pick params on TRAIN (vectorized screen),
     score TEST with event-driven engine (official OOS).
"""

from __future__ import annotations

import time
from itertools import product

import numpy as np
import pandas as pd

from app.core.logging_setup import get_logger
from app.core.schemas import (
    BacktestRequest,
    Bar,
    OptimizeRequest,
    OptimizeResult,
)

log = get_logger(__name__)

_RANK_KEYS = ("net_pnl", "profit_factor", "expectancy", "win_rate")
_VECTORIZED_STRATEGIES = {"sma_crossover", "building_blocks_demo"}


def expand_param_grid(
    grid: dict[str, list],
    max_combinations: int = 64,
) -> tuple[list[dict], int, int]:
    """Cartesian product of param lists.

    Returns (combos, skipped_invalid, truncated_remaining).
    Invalid: fast_period >= slow_period when both keys present.
    """
    if not grid:
        return [{}], 0, 0
    keys = list(grid.keys())
    values = [v if isinstance(v, list) else [v] for v in (grid[k] for k in keys)]
    combos: list[dict] = []
    skipped = 0
    truncated = 0
    for row in product(*values):
        params = dict(zip(keys, row))
        if "fast_period" in params and "slow_period" in params:
            try:
                if int(params["fast_period"]) >= int(params["slow_period"]):
                    skipped += 1
                    continue
            except (TypeError, ValueError):
                skipped += 1
                continue
        if len(combos) >= max_combinations:
            truncated += 1
            continue
        combos.append(params)
    return combos, skipped, truncated


def walk_forward_windows(
    n_bars: int,
    n_folds: int,
    test_fraction: float = 0.3,
    mode: str = "anchored",
    min_train: int = 80,
    min_test: int = 40,
) -> list[dict]:
    """Chronological train/test index windows. Never shuffles (no look-ahead)."""
    if n_folds < 1:
        return []
    if n_bars < min_train + min_test:
        return []
    test_fraction = min(max(test_fraction, 0.1), 0.5)
    test_len = max(min_test, int(n_bars * test_fraction / n_folds))
    train_len = n_bars - n_folds * test_len
    if train_len < min_train:
        test_len = max(min_test, (n_bars - min_train) // n_folds)
        train_len = n_bars - n_folds * test_len
    if test_len < 1 or train_len < min_train:
        return []

    windows: list[dict] = []
    if mode == "rolling":
        for i in range(n_folds):
            train_start = i * test_len
            train_end = train_start + train_len
            test_end = train_end + test_len
            if test_end > n_bars:
                break
            windows.append({
                "fold": i + 1,
                "mode": mode,
                "train": (train_start, train_end),
                "test": (train_end, test_end),
            })
    else:
        # anchored: train always starts at 0 and grows; test is the next chunk
        for i in range(n_folds):
            train_end = train_len + i * test_len
            test_end = train_end + test_len
            if test_end > n_bars:
                break
            windows.append({
                "fold": i + 1,
                "mode": "anchored",
                "train": (0, train_end),
                "test": (train_end, test_end),
            })
    return windows


def vectorized_sma_crossover_score(
    closes: np.ndarray,
    fast: int,
    slow: int,
) -> dict[str, float]:
    """Always-in-market SMA cross proxy. SCREENING ONLY — not official PnL."""
    empty = {
        "net_pnl": 0.0, "trade_count": 0, "win_rate": 0.0,
        "profit_factor": 0.0, "expectancy": 0.0, "source": "vectorized_sma",
    }
    n = len(closes)
    if n < slow + 2 or fast >= slow or fast < 1:
        return empty
    s = pd.Series(closes, dtype=float)
    fast_sma = s.rolling(int(fast)).mean().to_numpy()
    slow_sma = s.rolling(int(slow)).mean().to_numpy()
    raw = np.where(fast_sma > slow_sma, 1.0, np.where(fast_sma < slow_sma, -1.0, np.nan))
    pos = pd.Series(raw).ffill().fillna(0.0).to_numpy()
    # Signal on bar i close → realize bar i→i+1 close-to-close move (optimistic screen)
    rets = np.diff(closes.astype(float))
    pnl = pos[:-1] * rets
    changes = np.flatnonzero(np.diff(pos) != 0)
    # Approximate round-trips: each position flip
    trade_pnls: list[float] = []
    last = 0
    for idx in list(changes) + [len(pnl) - 1]:
        chunk = float(np.sum(pnl[last:idx + 1]))
        if idx > last or idx == len(pnl) - 1:
            if abs(chunk) > 1e-12 or idx == len(pnl) - 1:
                trade_pnls.append(chunk)
        last = idx + 1
    if not trade_pnls:
        trade_pnls = [float(np.sum(pnl))]
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    net = float(np.sum(trade_pnls))
    gw = float(sum(wins))
    gl = float(abs(sum(losses)))
    wr = (len(wins) / len(trade_pnls)) if trade_pnls else 0.0
    avg_w = (sum(wins) / len(wins)) if wins else 0.0
    avg_l = (sum(losses) / len(losses)) if losses else 0.0
    pf = (gw / gl) if gl > 0 else (1e9 if gw > 0 else 0.0)
    exp = wr * avg_w - (1 - wr) * abs(avg_l)
    return {
        "net_pnl": round(net, 6),
        "trade_count": len(trade_pnls),
        "win_rate": round(wr, 6),
        "profit_factor": round(min(pf, 1e9), 6),
        "expectancy": round(exp, 6),
        "source": "vectorized_sma",
    }


def _rank_value(metrics: dict, rank_by: str) -> float:
    key = rank_by if rank_by in _RANK_KEYS else "expectancy"
    val = metrics.get(key, 0.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _closes_from_bars(bars: list[Bar]) -> np.ndarray:
    return np.array([b.close for b in bars], dtype=float)


def _screen_combo(params: dict, closes: np.ndarray, strategy_id: str) -> dict:
    if strategy_id in _VECTORIZED_STRATEGIES:
        fast = int(params.get("fast_period", 10))
        slow = int(params.get("slow_period", 30))
        return vectorized_sma_crossover_score(closes, fast, slow)
    return {
        "net_pnl": 0.0, "trade_count": 0, "win_rate": 0.0,
        "profit_factor": 0.0, "expectancy": 0.0, "source": "no_vectorized_proxy",
    }


def _event_driven_summary(
    req: OptimizeRequest,
    params: dict,
    bars_by_symbol: dict[str, list[Bar]],
) -> dict:
    from app.backtest import run_backtest

    bt_req = BacktestRequest(
        strategy_id=req.strategy_id,
        strategy_params=params,
        symbols=req.symbols,
        timeframe=req.timeframe,
        start=req.start,
        end=req.end,
        initial_capital=req.initial_capital,
        commission_per_trade=req.commission_per_trade,
        slippage_bps=req.slippage_bps,
        fill_mode=req.fill_mode,
        dedupe_correlated_signals=False,
    )
    result = run_backtest(bt_req, store=None, bars_by_symbol=bars_by_symbol)
    summary = dict(result.summary)
    summary["source"] = "event_driven"
    summary["trade_count"] = summary.get("total_trades", len(result.trades))
    summary["total_bars"] = result.total_bars
    return summary


def run_optimize(req: OptimizeRequest, bars_by_symbol: dict[str, list[Bar]]) -> OptimizeResult:
    """Run the sweep. `bars_by_symbol` must already be loaded by the caller."""
    t0 = time.perf_counter()
    notes = [
        "Vectorized rankings are a SCREENING heuristic only. "
        "Never trust them as official results.",
        "Official in-sample and walk-forward out-of-sample scores use the "
        "event-driven backtester (same engine as live).",
    ]
    combos, skipped, truncated = expand_param_grid(req.param_grid, req.max_combinations)
    if truncated:
        notes.append(
            f"Grid truncated: {truncated} extra combo(s) dropped "
            f"(max_combinations={req.max_combinations})."
        )
    primary_sym = req.symbols[0]
    all_bars = bars_by_symbol.get(primary_sym, [])
    if not all_bars:
        return OptimizeResult(
            strategy_id=req.strategy_id,
            symbols=req.symbols,
            timeframe=req.timeframe.value,
            combinations_skipped=skipped,
            rank_by=req.rank_by,
            notes=notes + ["No bars for primary symbol — nothing to optimize."],
            runtime_sec=round(time.perf_counter() - t0, 3),
        )

    closes = _closes_from_bars(all_bars)
    ranking: list[dict] = []
    can_screen = bool(req.use_vectorized_screen and req.strategy_id in _VECTORIZED_STRATEGIES)
    if req.use_vectorized_screen and not can_screen:
        notes.append(
            f"No vectorized proxy for strategy '{req.strategy_id}'. "
            "Screening skipped; event-driven validation used for all combos "
            f"(capped at validate_top_n={req.validate_top_n})."
        )

    if can_screen:
        for params in combos:
            metrics = _screen_combo(params, closes, req.strategy_id)
            ranking.append({"params": params, **metrics})
        ranking.sort(key=lambda r: _rank_value(r, req.rank_by), reverse=True)
    else:
        ranking = [{"params": p, "source": "unscreened"} for p in combos]

    top_n = max(1, min(req.validate_top_n, len(ranking)))
    to_validate = ranking[:top_n]
    official: list[dict] = []
    for row in to_validate:
        params = row["params"]
        log.info(
            "Optimizer event-driven validation",
            extra={"strategy": req.strategy_id, "params": params},
        )
        summary = _event_driven_summary(req, params, bars_by_symbol)
        official.append({"params": params, **summary})
    official.sort(key=lambda r: _rank_value(r, req.rank_by), reverse=True)

    wfo: list[dict] = []
    if req.walk_forward_folds and len(all_bars) > 0:
        windows = walk_forward_windows(
            n_bars=len(all_bars),
            n_folds=req.walk_forward_folds,
            test_fraction=req.test_fraction,
            mode=req.walk_forward_mode,
        )
        if not windows:
            notes.append("Walk-forward skipped: not enough bars for the requested folds.")
        for win in windows:
            tr0, tr1 = win["train"]
            te0, te1 = win["test"]
            train_bars = {primary_sym: all_bars[tr0:tr1]}
            test_bars = {primary_sym: all_bars[te0:te1]}
            train_closes = _closes_from_bars(train_bars[primary_sym])
            best_params = combos[0] if combos else {}
            if can_screen:
                train_rank = [
                    {"params": p, **_screen_combo(p, train_closes, req.strategy_id)}
                    for p in combos
                ]
                train_rank.sort(key=lambda r: _rank_value(r, req.rank_by), reverse=True)
                best_params = train_rank[0]["params"] if train_rank else best_params
            oos = _event_driven_summary(req, best_params, test_bars)
            wfo.append({
                "fold": win["fold"],
                "mode": win["mode"],
                "train_bars": tr1 - tr0,
                "test_bars": te1 - te0,
                "train_idx": [tr0, tr1],
                "test_idx": [te0, te1],
                "params_from_train": best_params,
                "oos_event_driven": oos,
            })

    recommended = official[0]["params"] if official else (ranking[0]["params"] if ranking else {})
    rec_summary = {k: v for k, v in (official[0].items() if official else []) if k != "params"}

    return OptimizeResult(
        strategy_id=req.strategy_id,
        symbols=req.symbols,
        timeframe=req.timeframe.value,
        combinations_evaluated=len(combos),
        combinations_skipped=skipped,
        rank_by=req.rank_by,
        vectorized_is_not_truth=True,
        vectorized_ranking=ranking,
        official_event_driven=official,
        walk_forward=wfo,
        recommended_params=recommended,
        recommended_official_summary=rec_summary,
        notes=notes,
        runtime_sec=round(time.perf_counter() - t0, 3),
    )


def load_bars_for_optimize(req: OptimizeRequest, store=None) -> dict[str, list[Bar]]:
    if store is None:
        from app.data.store import get_store
        store = get_store()
    out: dict[str, list[Bar]] = {}
    for sym in req.symbols:
        out[sym] = store.get_bars(sym, req.timeframe, req.start, req.end)
    return out
