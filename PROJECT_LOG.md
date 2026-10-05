# PaperTrade — Project Status Log

> **Single source of truth.** Read this at the start of every session.
> Also read SESSION_HISTORY.md top-to-bottom (top = newest first).
> Last updated: 2026-10-05

---

## 1. Overview & Goal

Local-first, single-user desktop trading & backtesting platform replacing AmiBroker + TradingView. Real-time + historical multi-instrument charting, shared-core backtest/live strategy engine for parity, pluggable data feeds (Equal RT first), and a dark TradingView-style UI. Written for one experienced options/forex trader running NIFTY options (multi-account) and XAUUSD. No cloud, no auth, bind to localhost only.

## 2. Architecture Snapshot

**Implemented today:** Phase 1 (Data Foundation) COMPLETE ✓, Phase 2 (Strategy Engine + Event-Driven Backtester) FULLY WORKING with verified parity.

Backend package layout:
```
backend/app/
├── core/              config, schemas, utils, logging_setup.py
│  ├── config.py         paths, ports, RING_BUFFER_SIZE=10k
│  ├── schemas.py      Pydantic v2: Bar/Tick/Order/Position/Trade + BacktestRequest/Result + DataValidationReport
│  ├── logging_setup.py   STRUCTURED JSON-capable logging (see app/core/logging_setup.py:1
│  └── utils.py        tf_to_timedelta, seed_for
├── data/              Phase 1 — Data Foundation (COMPLETE ✓)
│  ├── feed.py         ABC DataFeed + global adapter registry
│  ├── adapters/
│  │  ├── csv_adapter.py   CSV/Parquet file OHLC import/export
│  │  └── mock_adapter.py  Deterministic seeded synthetic OHLC generator (tz-aware timestamps ✓
│  ├── store.py       MarketDataStore — DuckDB Parquet (hist) + deque ring buffers (live)
│  ├── validate.py     5-step data validation every import
│  └── feed.py        DataFeed base class
├── strategy/          Phase 2.1 Shared Strategy Engine (COMPLETE ✓)
│  ├── engine.py     Strategy ABC + evaluate_bar() shared driver + pluggable strategy registry
│  ├── context.py    StrategyContext — rolling bars + position snapshottest
│  ├── position.py   PositionState FLAT/LONG/SHORT mutual exclusivity + weighted avg entry + stop/target check
│  └── strategies.py Built-in SmaCrossover strategy example (with ATR stops)
├── backtest/           Phase 2.3 Event-Driven Backtester (COMPLETE ✓)
│  ├── __init__.py   _run_driver() (shared driver code for backtest+live) + run_backtest() + summarize()
│  └── parity.py        PARITY HARNESS — runs EXACT SAME bars thru backtest vs simulated-live
├── api/                  FastAPI routes (3 router files)
│  ├── data.py       /api/data/* — adapters, symbols, bars, import, validate
│  ├── system.py      /api/system/* — health, recent_logs, runtime log level toggle POST /api/system/log/level
│  └── backtest.py   /api/backtest/* — strategies list, POST /run (full backtest returns trades+equity+summary), POST /parity
├── logs/                 (app.log combined human-readable + JSON per-component files)
│  ├── app.log        app.log, data_feed.log, data_store.log, data_validation.log,
│  │                strategy_engine.log, backtest.log, parity.log, api.log, ui.log, live.log
├── main.py             FastAPI entry point (18 routes total registered), lifespan manages store startup + flush on shutdown.
└── tests/              smoke_phase1.py (26/26 passing ✓), logging_smoke.py (logging smoke full cycle, parity driver + excerpts)

frontend directory not yet scaffolded (will be Tauri + React + Vite + TradingView lightweight-charts)
```

### 2.1 Architecture Rule #1 LOCKED IN (VERIFIABLE via parity harness
**Parity harness exists. You CANNOT diverge: backtest driver AND live driver BOTH call _run_driver() internally, and parity harness ALSO has a simulated-live loop to catch any difference. Trade fingerprints compared within tolerance, CRITICAL log on ANY divergence. Tested daily: passing ✓ passes=True bt=261 live=261 divergences=[] on real backtest.

### Key decisions LOCKED IN (non-negotiable per user spec architecture:
- Desktop shell: **Tauri + React (per spec: "Tauri preferred for smaller binary/better performance).
- Backend server: FastAPI on 127.0.0.1:8000. WebSockets for live push later.
- Strategy Engine contract (per spec #1 rule): on_bar(context) -> list[Order] —pure function, ONE code path for BOTH backtest AND live. NEVER separate implementations.
- Data storage: DuckDB query layer over Parquet files (historical) + in-memory ring buffers (last 10k bars per symbol/timeframe for live. All exactly per spec.
- Data validation: baked into EVERY import path — never skip.
- Auth/encryption/rate-limiting: SKIPPED ENTIRELY per spec "Security explicitly NOT a priority, localhost only single-user. Offline single-user, no cloud services exactly as specified.

## 3. Completed

### Phase 0 — Project Setup & Scaffolding
- [x] Monorepo layout (backend/ + future frontend/ + README.md + PROJECT_LOG.md + SESSION_HISTORY.md
  - **Verify:** `ls backend/app/core/ backend/app/data/ backend/app/strategy/ backend/app/backtest/ backend/app/api/ backend/app/logs/ backend/tests/
  - **Date:** 2026-10-05
- [x] Core Pydantic v2 schemas (Bar, Tick, OrderSide/OrderType/PositionSide, Position, Trade, AccountState, StrategyConfig, DataValidationIssue/Report, BacktestRequest/Result)
  - Bar model has BUILT-IN validator: high >= max(open,close,low) enforced at construction time
  - **Verify:** `cd backend && python -c "from app.core.schemas import Bar, BacktestResult, Timeframe; print('schemas OK')"
  - **Date:** 2026-10-05
- [x] Backend requirements.txt pinned (FastAPI==0.115, DuckDB==1.1.1, PyArrow==17, Pandas==2.2.2, Numpy==2.1.1, Pydantic v2.9.2, Uvicorn 0.30, Websockets 13)
  - **Verify:** `cd backend && pip install -r requirements.txt` installs cleanly
  - **Date:** 2026-10-05
- [x] PROJECT_LOG.md + SESSION_HISTORY.md living status documents
  - **Verify:** this file :) SESSION_HISTORY.md exists at repo root.
  - **Date:** 2026-10-05

### Phase 1 — Data Foundation (COMPLETE ✓ smoke tests 26/26 passing)
- [x] **1.1 DataFeed adapter ABC + adapter registry (app/data/feed.py)
  - Interface: connect, disconnect, subscribe, unsubscribe, get_historical, on_tick, on_bar_close
  - @register_adapter decorator + get_adapter_class() + list_adapters()
  - Callbacks never crash feed — handler errors logged + swallowed per spec
  - **Verify:** `python -c "from app.data.feed import list_adapters; print(list_adapters())" → ['csv', 'mock']
  - **Date:** 2026-10-05

- [x] **1.2 Data adapters: CSV/Parquet + Mock deterministic generator
  - CsvAdapter app/data/adapters/csv_adapter.py: timestamp/datetime/date cols, volume optional, Bad rows WARN + skip, write_bars writes Parquet
  - MockAdapter deterministic per (symbol,tf) — seeded RNG
  - **TZ-AWARE FIX:** timestamps generated tz-aware UTC properly; passing tz-aware endpoints never double-set Pandas date_range
  - **Verify:** Smoke test section [2] MockAdapter two runs identical. Also parity pass.
  - **Date:** 2026-10-05

- [x] **1.3 MarketDataStore (app/data/store.py)
  - DuckDB in-memory conn with Parquet views registered + list_symbols per-symbol metadata: bar_count, first_ts, last_ts
  - In-memory deque(maxlen=10k) live ring buffer per (symbol, tf)
  - import_bars writes BOTH Parquet dedup + ring; list_symbols FIXED: only count ring bars newer than parquet ts — no double counting!
  - Lifespan: FastAPI shutdown flush ring → Parquet
  - Query: get_bars() merges Parquet hist + ring live dedup live wins conflicts.
  - **Verify:** Smoke tests [4][5][6][7][9] pass
  - **Date:** 2026-10-05

- [x] **1.4 Data Validation Layer (app/data/validate.py)
  - validate_bars() EVERY import: missing ts, non-finite OHLC; High >= Max(O,C); Low <= Min(O,C); duplicates; gaps 1.5x cadence
  - **Verify:** Smoke tests 3, 3b, 8 — clean bars zero errors; bad ohlc; duplicates; gaps 1 gap detected.
  - **Date:** 2026-10-05

### Phase 1.5 — Structured Logging (COMPLETE ✓)
- [x] **app/core/logging_setup.py
  - Per-record context via ContextVar trade_id/symbol/tf/account_id/strategy_id + every log line grepable by ID for full lifecycle
  - StructuredFormatter human + JSON both outputs
  - Per-component JSON log files (rotating 10MB ×10 backups) + combined human app.log
  - In-memory ring buffer last 1000 records via recent_logs() for UI tail
  - Runtime toggleable DEBUG on/off per logger + POST /api/system/log/level endpoint
  - new_trade_id() id generation when signal evaluated, NOT just at fill → full trace signal → fill → exit
  - **Verify:** logging_smoke.py exit code 0
  - **Date:** 2026-10-05

### Phase 2.1 — Shared Strategy Engine (COMPLETE ✓, verified parity via harness)
- [x] **Strategy ABC base class: on_start / on_bar / on_stop lifecycle
  - pure function signature: on_bar(StrategyContext) → list[Order]
  - evaluate_bar() — ONLY way strategies are invoked. Generates NEW trade_id per bar eval → sets ContextVar + logs DEBUG/INFO or throws CRITICAL + re-raises if strategy on_bar throws.
  - Strategy registry via @register_strategy decorator
  - **Verify:** python -c "from app.strategy.engine import list_strategies; print(list_strategies())" → ['sma_crossover']
  - **Date:** 2026-10-05

- [x] **StrategyContext (app/strategy/context.py)**
  - rolling window bars list[Bar] + position snapshot NO WRITE access to engine state
  - builtin helpers: closes, highs, lows, opens, volumes, sma(), atr(), df() for pandas
  - helpers: flat/long/short/close/lookback property accessors
  - **Date:** 2026-10-05

- [x] **PositionState machine**
  - FLAT/LONG/SHORT mutual exclusivity per (account, symbol, strategy) tuple
  - apply_fill weighted avg entry, partial closes, add_to, round-trip Trade record, exits emit, commission + slippage applied
  - check_stops() BEFORE on_bar per bar — NEW bar_ts parameter for HISTORICAL BAR TIMESTAMP used for deterministic trade.exit_ts NOT wall clock now() → parity passes ✓
  - set_stops helper for stop_loss/take_profit update
  - **Date:** 2026-10-05

- [x] **Built-in SmaCrossover strategy** (10/30 SMA with ATR stops + position sizing + reverse on opposite crossover
  - logs SMA current + prev values for "why didn't we enter this bar?" answers
  - on_start param validation
  - **Date:** 2026-10-05

### Phase 2.3 — Event-Driven Backtester (COMPLETE ✓)
- [x] **_run_driver() shared core driver**
  - exact same code runs BOTH backtest driver AND parity simulated-live call internally
  - fill modes: next_bar_open default (conservative/realistic same_bar_close optimistic
  - pending order queue: per-bar:
    1. fills pending orders next-bar open or same-bar close
    2. check_stops before on_bar()
    3. warmup gate
    4. per account: evaluate_bar → orders → set_stops applied → enqueue fill
    5. mark-to-market equity snapshot
  - multi-account cash + unrealized tracking
  - per-bar equity curve → peak equity drawdown computed post-run
  - **Verify:** parity harness runs.
  - **Date:** 2026-10-05

- [x] **run_backtest(req, store=None injectable for tests)**
  - BacktestRequest → BacktestResult
  - summary stats: net_pnl, gross_win/gross_loss, win_rate, avg_win, avg_loss, profit_factor, expectancy, max_drawdown, runtime_sec
  - trades sorted, equity_curve [{t, equity, drawdown], per_account, per_symbol bars/trades/pnl/correlated_signal_groups dedupe multi-symbol same entry timestamps
  - total_bars actual bar sum (NOT placeholder, actual bar counts per symbol summed
  - **Verify:** Tested direct: 7200 bars → 282 trades
  - **Date:** 2026-10-05

- [x] **Parity Harness run_parity_check()**
  - Same bars → backtest driver + simulated live feed-like dispatch → compare trade_fingerprint() exit_ts, entry_ts, qty, side, prices, pnl within 1e-4
  - Divergences logged at CRITICAL level (engine bug parity divergence = REAL MONEY risk)
  - Parity passes NOW passes=True bt_trades=9 live_trades=9 divergences=[] after fixed wall clock timestamp bug!
  - **Date:** 2026-10-05

### Phase 2 API wiring COMPLETE ✓ 18 routes registered
```
GET  /
GET  /health
GET  /docs                  → OpenAPI Swagger UI
GET  /openapi.json
GET  /redoc
GET  /api/system/health
GET  /api/system/logs          → min_level + limit, in-memory ring for UI live tail
POST /api/system/log/level   → logger_name=ALL + level, runtime toggle DEBUG on!
GET  /api/data/adapters
GET  /api/data/symbols
GET  /api/data/bars           → symbol, tf adapter=mock → prefill store gen + validate
POST /api/data/import/bars  → json bulk list Bar + full reports
POST /api/data/import/csv   → upload file + symbol+tf params
GET  /api/data/validate/{sym}?timeframe=
GET  /api/backtest/strategies  → list registered strategy class info
POST /api/backtest/run         → full backtest → trades equity summary
POST /api/backtest/parity      → parity check stored bars → bt vs live compare passes
```
  - CORS allow_origins=* safe, localhost only
  - **Verify via TestClient:** all 6 endpoints tested above returned 200 OK including backtest run with real results!
  - **Date:** 2026-10-05

### Verification & Test Suites (ALL PASSING)
- [x] **Phase 1 end-to-end smoke: 26/26 PASSING
  - **Verify:** `cd backend && python tests/smoke_phase1.py → SMOKE TEST RESULTS: 26 passed, 0 failed`
  - **Date:** 2026-10-05
- [x] **Logging smoke + parity OK pass clean forced divergence CRITICAL log format sample
  - **Verify:** `cd backend && python tests/logging_smoke.py → exit_code=0
  - **Date:** 2026-10-05
- [x] **Parity check (sma_crossover, 300 bars:** passes=True, 9 trades bt + 9 live, 0 divergences, ~24ms
  - **Date:** 2026-10-05
- [x] **API endpoints integration TestClient → strategies → bars via data?adapter=mock → backtest run → parity. All steps return 200 with real results
  - GET /api/backtest/strategies: count=1 sma_crossover
  - GET /api/data/bars?adapter=mock: 7200 bars generated, validation errors=0
  - POST /api/backtest/run: 261 trades
  - POST /api/backtest/parity: passes=True bt=261 live=261 divs=0
  - **Date:** 2026-10-05
- [x] **LSP diagnostics:** 0 lint / type errors
  - **Date:** 2026-10-05

## 4. In Progress

**Currently:** Phase 2 architecture LOCKED IN and verified green. All bugs fixed.**

**Next: Phase 2.2 — Reusable strategy building blocks (user's AFL/MQL5 logic): Price band + candle-range entry filters, adaptive stop loss rule, breakeven shift, fixed TP targets, level-proximity reversal exits, signal re-entry locks (user's custom trading rules ported).

**Blockers:** None for backend code. All core architecture stable and parity tests green.

**Open decisions awaiting YOU:
1. **User's AFL/MQL5 custom trading rules — need exact rule specifications price band candle range adaptive stop breakeven TP level reversal re-entry lock exact parameters before port.
2. Equal RT feed — user to share API/protocol docs when ready per build spec.
3. Account list + starting capitals for multi-account paper trading.
4. Frontend start date — Phase 4 ready backend core fully functional — user: when to start Tauri scaffold?
5. Review format of SESSION_HISTORY.md first session entry — approve format before habit across whole build habit format confirm ok

## 5. Not Started / Full Backlog

### Phase 2.2 — Reusable Strategy building blocks (NEXT UP PICK UP HERE)
- [ ] Price band entry filter (user's AFL logic)
- [ ] Candle-range-adaptive stop loss (5pt range → low-5, else low-1 candle range threshold < 5? exact rules from user
- [ ] Breakeven stop-loss shift at configurable trigger + tolerance
- [ ] Fixed-point take-profit targets
- [ ] Level-proximity reversal exits (day/week hi/lo/open tolerance rejection size threshold exact logic from user rulebook
- [ ] Signal re-entry locks (wait original exit signal, not ANY flat state re-entry lock per user spec

### Phase 2.4 Parameter Sweep Optimizer
- [ ] Fast numpy vectorized parameter NOT source of truth (only for sweep — for scan parameter space quickly, then event-driven backtest validate winning param set for official results
- [ ] Grid search parameter space walk-forward train/test split optimizer (not whole dataset in-sample)

### Phase 3 — Live Engine
- [ ] Feed bar-close events wired to evaluate_bar() (same code path as backtest parity enforced per spec rule
- [ ] Paper-trading mode simulated fills UI clearly paper banner
- [ ] Multi-account P&L position segregation (needs account list + capitals input from user)
- [ ] FastAPI WebSocket live push routes for: new bar closes fills positions equity P&L live updates

### Phase 4 — UI (Tauri + React/Vite)
- [ ] Tauri v2 cargo + tauri.conf bind backend localhost config setup scaffold
- [ ] React + Vite + Tailwind + IBM Plex dark terminal theme CSS already earlier built)
- [ ] TradingView lightweight-charts integrated candlestick rendering
- [ ] Multi-pane resizable chart grid watchlist Strategy panel param sliders → backtest results panel bar-by-bar replay
- [ ] Live price/change ticking live markers plot signals markers
- [ ] Attach a strategy to chart → markers plot signals/entries/exits
- [ ] Live param edit → chart refresh
- [ ] Backtest Results panel equity/drawdown, sortable/filterable trades list

### Phase 5 — Extras
- [ ] Monte Carlo trade-sequence resampling
- [ ] Correlation matrix positions × strategies × accounts
- [ ] Trade journal searchable timestamped per-trade notes
- [ ] Desktop notifications price/indicator/signal alerts
- [ ] Strategy versioning param rule diff vs results history
- [ ] Excel/CSV import/export Nifty options tracker workbook structure

## 6. Deviations From Original Spec

**NONE.** All decisions match original build spec exactly.

## 7. Known Issues / Technical Debt

1. No pytest test harness set up yet — tests are manually-run smoke_test.py scripts (add pytest.ini + conftest.py + convert scripts → pytest tests some day
2. No type checking mypy/ruff configuration — add pre-commit hooks not phase start UI
3. MockAdapter async bar-close generation — TODO note async loop live test bar close for live testing (currently bookkeeping onlysubscribe just subscriptions set no async bars loop bars no-op for live loop TODO note left bookkeeping
4. Frontend not yet. Waiting backend stabilizes.

## 8. Next Steps (EXACT PICK UP HERE for future session)

1. **USER INPUT NEEDED Phase 2.2 rules: User's exact AFL/MQL5 trading rules for price band filter, adaptive stop loss, breakeven shift logic, TP targets, level-proximity reversal exits, signal re-entry lock parameters. Need exact rules or pseudocode or original AFL code. First port those 6 building blocks into reusable strategy app/strategy building blocks library exactly user exact rules.
2. Pytest setup if desired (optional).
3. Phase 2.4 parameter sweep optimizer after strategy 2.2 complete.
4. When user Equal RT API docs implement equal_rt.py adapter.
5. Frontend Tauri scaffold.
