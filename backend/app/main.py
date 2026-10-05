"""FastAPI application entry point.

Binds ONLY to 127.0.0.1 — no external exposure, no auth.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import SERVER_HOST, SERVER_PORT
from app.core.logging_setup import get_logger
from app.api import data as data_routes
from app.api import system as system_routes
from app.api import backtest as backtest_routes
from app.data.store import get_store

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize store (registers existing Parquet views)
    store = get_store()
    n_syms = len(store.list_symbols())
    log.info("Backend startup complete",
             extra={"server_host": SERVER_HOST, "server_port": SERVER_PORT,
                    "symbols_loaded": n_syms})
    yield
    # Shutdown: flush ring buffers to disk
    store = get_store()
    try:
        store.flush()
        log.info("Market data store flushed to disk on shutdown")
    except Exception as e:
        log.critical(
            "Store flush FAILED on shutdown — recent live data may be lost!",
            extra={"error": str(e)},
            exc_info=True,
        )


app = FastAPI(
    title="PaperTrade Backend",
    description="Local trading & backtesting platform. Bind localhost only.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: open for localhost UI (Vite dev server + Tauri webview)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Localhost only — safe. Easier than tracking ports.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(data_routes.router)
app.include_router(system_routes.router)
app.include_router(backtest_routes.router)


@app.get("/")
def root() -> dict:
    return {
        "name": "PaperTrade Backend",
        "version": "0.1.0",
        "bind": f"{SERVER_HOST}:{SERVER_PORT}",
        "status": "online",
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
