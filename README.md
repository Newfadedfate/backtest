# PaperTrade — Local Trading & Backtesting Platform

Self-hosted, offline, single-user desktop trading platform. Replaces AmiBroker + TradingView.

## Architecture

```
┌──────────────────── DESKTOP SHELL ────────────────────┐
│  Tauri + React + TradingView lightweight-charts       │
│  Multi-pane charts, watchlists, backtest results      │
└───────────────────────┬───────────────────────────────┘
                        │ localhost:8000 (FastAPI + WS)
┌───────────────────────▼───────────────────────────────┐
│               APPLICATION SERVER (Python)             │
│                                                       │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ DataFeed │  │ Strategy     │  │ Backtest       │  │
│  │ Manager  │  │ Engine       │  │ Engine         │  │
│  │ adapters │  │ (shared)     │  │ event-driven   │  │
│  └────┬─────┘  └──────┬───────┘  └────────┬───────┘  │
│       │                │                   │          │
│  ┌────▼────────────────▼───────────────────▼───────┐ │
│  │         Market Data Store                        │ │
│  │  DuckDB/Parquet (historical) + ring buffers      │ │
│  └─────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────┘
          │
   ┌──────▼──────┐
   │ Adapters:   │  CSV / Mock / EqualRT (future)
   └─────────────┘
```

## Core Principles (Non-negotiable)

1. **One Strategy Engine**: Backtest and live trading use identical `on_bar(context)` code path.
2. **Adapter Pattern**: Pluggable data feeds via a single common interface.
3. **Event-Driven = Truth**: Vectorized mode only for fast parameter sweeps; never trust for results.
4. **Local-Only**: `127.0.0.1` bind only. No auth, no cloud, no encryption.

## Phases (strict order)

- **Phase 1 — Data Foundation** ✅ (DataFeed interface, CSV/mock adapters, DuckDB store, validation)
- **Phase 2 — Strategy + Backtester** (Engine, state machine, building blocks, event backtest, optimizer)
- **Phase 3 — Live Engine** (Feed → Engine wiring, paper trading, multi-account)
- **Phase 4 — UI** (Tauri shell, multi-pane charts, watchlists, backtest panel, replay)
- **Phase 5 — Extras** (Monte Carlo, correlation matrix, trade journal, alerting, strategy versioning)

## Run

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run tauri dev
```
