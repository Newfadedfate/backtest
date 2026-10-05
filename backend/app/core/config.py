from pathlib import Path

BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
PARQUET_DIR: Path = DATA_DIR / "parquet"
LOG_DIR: Path = BASE_DIR / "logs"

for _d in (DATA_DIR, PARQUET_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000

RING_BUFFER_SIZE: int = 10_000  # bars per (symbol, timeframe) kept in memory
