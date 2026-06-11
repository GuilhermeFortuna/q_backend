# Quant API Backend (`q_backend`)

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue?style=for-the-badge&logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.100%2B-green?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com/)
[![MetaTrader 5](https://img.shields.io/badge/MetaTrader-5-red?style=for-the-badge)](https://www.mql5.com/)
[![Package Manager](https://img.shields.io/badge/uv-project-purple?style=for-the-badge&logo=rust)](https://github.com/astral-sh/uv)

`q_backend` is a high-performance Python-based algorithmic trading backend. It functions as the data ingestion layer, backtesting server, and execution coordinator for the **Quant** ecosystem. Operating via FastAPI, it bridges local MetaTrader 5 (MT5) execution terminals with the React-based Tauri desktop frontend.

---

## 🏛 Architecture & Capabilities

`q_backend` is engineered around a clean separation of concerns:

```
                  ┌──────────────────────┐
                  │   Quant Frontend     │
                  │   (React + Tauri)    │
                  └──────────┬───────────┘
                             │  HTTP REST
                             ▼
                  ┌──────────────────────┐
                  │    q_backend API     │
                  │      (FastAPI)       │
                  └──────────┬───────────┘
                             │  Internal Service Calls
                             ▼
       ┌─────────────────────┴─────────────────────┐
       ▼                                           ▼
┌──────────────┐                            ┌──────────────┐
│ Market Data  │                            │ Backtesting  │
│  Ingestion   │                            │    Engine    │
└──────┬───────┘                            └──────┬───────┘
       │                                           │
       ▼                                           ▼
┌──────────────┐                            ┌──────────────┐
│ MetaTrader 5 │                            │  Strategies  │
│   Terminal   │                            │ (Indicators, │
│  (B3/Forex)  │                            │ Crossovers)  │
└──────────────┘                            └──────────────┘
```

### 1. Market Data Routing & Ingestion (`market_data`)
* **MetaTrader 5 Integration:** High-speed client (`MetaTraderClient`) communicating directly with a running MT5 Windows terminal.
* **Asset Support:** Tailored to ingestion of **B3 (Bovespa)** symbols (e.g. `PETR4`, `VALE3`, `ITUB4`) and liquid **B3 Futures** contracts (e.g. `WIN$` Mini-Index, `WDO$` Mini-Dollar, and `CCM$` Corn Futures).
* **Multi-Format Datatypes:** Optimized data schemas for Tick-by-Tick transactions and standardized OHLCV candle streams (from 1-minute `M1` to Monthly `MN1` intervals).
* **Columnar tick loader:** `MetaTraderClient.get_ticks_columnar` / `MarketDataService.get_ticks_columnar` fetch historical ticks as aligned NumPy arrays (no per-row Pydantic objects) for the tick backtest engine. Results are cached on disk as Parquet (see below).
* **Tick cache:** Parquet files under `data/tick_cache/` by default (`Q_TICK_CACHE_DIR` overrides). Key = `{symbol_slug}_{sha256(symbol|start|end|flags)[:12]}`. Delete files in that directory to force a refetch from MT5.
* **Robust Resiliency:** Smart automatic reconnection and local environment configuration mapping.

### 2. High-Performance Backtesting Engine (`backtesting`)

#### Tick engine (`backtesting/tick`)

A separate intrabar engine for MT5 tick arrays (alongside the candle `BacktestEngine`):

* **`TickStrategy`** — vectorized `compute_signals(ticks) -> TickSignals` (direction, SL/TP distances per tick). Periods count **ticks**, not milliseconds. Causality is enforced in `test_tick_strategy_causality.py`.
* **`TickBacktestEngine`** — loads columnar ticks, runs the kernel, returns a `TradeRegistry`. Supports `ParallelMode.DAY_TRADE` (split by UTC day from `time_msc`) and `SEQUENTIAL`.
* **Fill model** — long entries and short covers pay **ask**; long exits and short entries receive **bid**. Single open position; exits checked in order **stop-loss → take-profit → opposite signal**; end-of-chunk force-close at last tick.
* **Register tick strategies** via `register_strategy(..., engine="tick")` and build with `build_tick_strategy`. First-party example: `TickMaBreakout`.

### 2b. Candle backtesting (`backtesting`)
* **Multi-Execution Engines:**
  * `SEQUENTIAL`: Standard path tracking (ideal for swing-trading strategies).
  * `DAY_TRADE`: Concurrent chunked backtesting (utilizes standard Python `ProcessPoolExecutor` to process daily sessions across multi-core CPUs in parallel).
* **Signal & Order Pipeline:** Modular pipeline translating strategy `Signal` structures into executable `Order` definitions using pluggable `PositionSizer` logic.
* **Vectorized Computations:** Employs precomputed Technical Indicators via vectorized pandas operations, preventing lookahead bias while maintaining massive throughput.
* **Pluggable Strategy Registry:** Built-in strategies (`MACrossover`, `RSIMeanReversion`, `BollingerReversion`, `MACD`, `DonchianBreakout`) register parameter schemas consumed by the optimization engine and frontend forms via `GET /api/v1/strategies`.
* **Advanced Analytics Suite (`TradeRegistry`):** Aggregates execution history and computes comprehensive mathematical metrics:
  * Win Rate, Expectancy, and Profit Factor.
  * Cumulative PnL & Peak Equity Tracking.
  * Precise Maximum Drawdown (Value & Percentage).
  * Recovery Factor & Win/Loss streaks.

### 3. Storage Infrastructure (`storage`)

Three-tier storage keeps analytical payloads separate from operational metadata:

| Tier | Technology | Purpose |
|------|------------|---------|
| **Analytical (future)** | Parquet lake at `Q_DATA_LAKE_ROOT` | OHLCV, ticks, features, backtest series, optimization arrays |
| **Metadata** | PostgreSQL | Strategies, versions, backtest configs/runs, optimization studies/trials, ingestion run records |
| **Runtime** | Redis | Job progress (JSON, 24h TTL), cache, locks |

Postgres stores **metadata and lake pointers** (`lake_path`, `lake_paths`, `result_summary`) only. Market data is not stored in Postgres tables.

---

## 🛠 Tech Stack

* **Core Runtime:** Python `>=3.12`
* **API Framework:** FastAPI, Uvicorn (ASGI web server), CORS Middleware
* **Numerical Stack:** Pandas, NumPy (Vectorized market-data calculations)
* **Broker & Data Clients:** MetaTrader 5 (MT5 Python package), Yahoo Finance (`yfinance`)
* **Serialization & Validation:** Pydantic `v2` (Declarative typesafe schemas)
* **Metadata Storage:** PostgreSQL, SQLAlchemy 2.x, Alembic
* **Runtime State:** Redis (job progress, cache, locks)
* **Dependency & Package Manager:** `uv` (Rust-powered modern Python package toolchain)
* **Unit Testing:** `pytest`

---

## 📂 Project Directory Structure

```txt
q_backend/
├── .env                  # Local secrets (copy from .env.example)
├── .env.example          # Template for MT5 and storage env vars
├── alembic/              # Database migrations
├── alembic.ini
├── docker-compose.yml    # Local Postgres + Redis
├── pyproject.toml        # Hatchling build configuration & dependency definitions
├── uv.lock               # Deterministic dependency lockfile
├── scripts/              # Standalone utility & validation scripts
│   └── backtests/        # High-performance backtesting runners
│       └── run_ccm_backtest.py
├── src/
│   └── q_backend/        # Core packages
│       ├── api/          # FastAPI App initialization & route definitions
│       ├── backtesting/  # Engine, Position Sizers, Performance Registry & Strategies
│       ├── market_data/  # MT5 service wrappers & data pipelines
│       ├── optimization/ # Optuna runner, study storage, exporters
│       └── storage/      # Settings, Postgres models, Redis helpers
│           ├── settings.py
│           ├── db/       # SQLAlchemy models, engine, repositories
│           └── redis/    # Job progress helpers
└── tests/
    ├── backtesting/
    ├── optimization/
    ├── market_data/
    └── storage/          # Storage unit + integration tests
```

---

## ⚡ Quickstart & Setup

### Prerequisites
1. **Windows OS:** MetaTrader 5 local terminal only runs on Windows environments.
2. **MetaTrader 5 Terminal installed:** Download from your broker or [MetaQuotes](https://www.metatrader5.com/).
3. **`uv` Package Manager:** Install `uv` if you haven't already:
   ```powershell
   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

### 1. Configure the Environment
Copy the example environment file and fill in your values:
```bash
cp .env.example .env
```
Open `.env` and configure MetaTrader 5 credentials plus storage URLs (defaults match `docker-compose.yml`):
```ini
MT5_USER=12345678
MT5_PASSWORD="YourPassword"
MT5_SERVER="Broker-Server"
MT5_PATH="C:/Program Files/MetaTrader 5/terminal64.exe"

Q_DATABASE_URL=postgresql+psycopg://q:q@localhost:5432/q
Q_REDIS_URL=redis://localhost:6380/0
Q_DATA_LAKE_ROOT=data/lake
Q_TICK_CACHE_DIR=data/tick_cache
```

### 2. Install Dependencies
```bash
uv sync --group dev
```

### 3. Start Storage Services (local dev)
Start Postgres and Redis:
```bash
docker compose up -d
```
Apply database migrations:
```bash
uv run alembic upgrade head
```

`q_backend` Redis is mapped to host port **6380** (container port 6379) so it does not conflict with other local Redis instances on 6379. If port `5432` or `6380` is already in use, adjust `docker-compose.yml` port mappings and update `Q_DATABASE_URL` / `Q_REDIS_URL` accordingly.

### 4. Running Unit Tests
```bash
uv run pytest
```
Unit tests use in-memory SQLite and `fakeredis` and do not require Docker. Integration tests (`@pytest.mark.integration`) skip automatically when Postgres or Redis are unavailable or misconfigured.

### 5. Running the Dev Server
Spin up the FastAPI server with auto-reload enabled:
```bash
uv run uvicorn q_backend.api.main:app --reload --port 8000
```
The interactive API Swagger docs will be immediately accessible at [http://localhost:8000/docs](http://localhost:8000/docs).

---

## 📊 Backtesting Showcase

An executable demo running a **Moving Average Crossover Strategy** on B3 Corn Futures (`CCM$`) is located in the scripts directory. It fetches a year of historical H1 data directly from MT5, simulates trades using a 9/12 MA crossover delta, and prints advanced metrics.

To run it:
1. Ensure your local MetaTrader 5 terminal is open and connected to your broker.
2. Execute the runner script:
   ```bash
   uv run scripts/backtests/run_ccm_backtest.py
   ```

---

## 🔌 API Reference (v1)

### System Telemetry
* **`GET /`**
  * *Description:* Verify backend online status and check current MT5 connectivity.
* **`GET /api/v1/system/health`**
  * *Description:* Advanced system diagnostic dashboard info. Includes database (PostgreSQL) and cache/jobs (Redis) storage statuses.
  * *Response:* `{"status": "healthy", "backendVersion": "0.1.0", "dataLakeStatus": "online", "lastSyncAt": "2026-06-09...", "storageStatus": {"postgres": {"status": "ok"}, "redis": {"status": "ok"}}}`

### B3 Asset Directory & Realtime
* **`GET /api/v1/market/instruments`**
  * *Description:* Retrieves B3/Bovespa core instrument master definitions (`PETR4`, `VALE3`, `ITUB4`, `WIN$`, `WDO$`).
* **`GET /api/v1/market/snapshot/{symbol}`**
  * *Description:* Obtains a real-time quote snapshot directly from the active terminal.
  * *Response fields:* `symbol`, `last`, `changePct`, `volume` (legacy), plus `bid`, `ask`, `spread`, `changeAbs`, `dayOpen`, `dayHigh`, `dayLow`, `prevClose`, `digits`, `tickTime`.
* **`GET /api/v1/market/snapshots`**
  * *Description:* Batch quote snapshots for a watchlist (one MT5 session, unknown symbols skipped).
  * *Parameters:* `symbols` (required, comma-separated, max 50).
  * *Response:* `{"snapshots": [MarketSnapshotResponse, ...]}`
* **`GET /api/v1/market/ticks/{symbol}`**
  * *Description:* Recent time-and-sales ticks for a symbol (newest last).
  * *Parameters:* `limit` (default 200, max 1000).
  * *Response:* `{"ticks": [{"timestamp", "bid", "ask", "last", "volume", "side"}, ...]}`
* **`GET /api/v1/market/instrument-info/{symbol}`**
  * *Description:* Curated contract specification fields for charting and order sizing.
  * *Response fields:* `symbol`, `description`, `exchange`, `currencyBase`, `currencyProfit`, `digits`, `point`, `tickSize`, `tickValue`, `contractSize`, `volumeMin`, `volumeMax`, `volumeStep`, `spreadFloating`.
* **`GET /api/v1/market/ohlcv/{symbol}`**
  * *Description:* Fetches the historical daily OHLCV rates (past 30 sessions) for frontend charting.

### Fine-Grained Historical Market Data Queries
* **`GET /api/v1/market-data/symbol/{symbol}`**
  * *Description:* Pulls detailed instrument properties directly from the MetaTrader server.
* **`GET /api/v1/market-data/ohlcv`**
  * *Parameters:*
    * `symbol` (required)
    * `timeframe` (e.g. `M1`, `M5`, `H1`, `D1` - default `M1`)
    * `start` (ISO-8601 Datetime)
    * `end` (ISO-8601 Datetime)
* **`GET /api/v1/market-data/ticks`**
  * *Parameters:*
    * `symbol` (required)
    * `start` (ISO-8601 Datetime)
    * `end` (ISO-8601 Datetime)

### Algorithmic Backtesting
* **`GET /api/v1/strategies`**
  * *Description:* Returns registered strategy metadata and typed parameter schemas for dynamic UI forms and optimization bounds.
  * *Response:* `{"strategies": [{"name": "MACrossover", "label": "MA Crossover", "description": "...", "params": [{"name": "short_period", "type": "int", "default": 50, ...}]}]}`
  * *Built-in strategies:* `MACrossover`, `RSIMeanReversion`, `BollingerReversion`, `MACD`, `DonchianBreakout`.
* **`POST /api/v1/backtest/run`**
  * *Description:* Runs a candle (`engine: "candle"`, default) or tick (`engine: "tick"`) strategy backtest locally.
  * *Candle request (JSON):* `{"symbol": "WIN$", "timeframe": "M5", "start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z", "initial_capital": 100000.0, "point_value": 0.2, "strategy": "MACrossover", "strategy_params": {"short_period": 9, "long_period": 21}}`
  * *Tick request (JSON):* `{"symbol": "WIN$", "engine": "tick", "display_timeframe": "M1", "tick_flags": "all", "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z", "initial_capital": 100000.0, "point_value": 0.2, "strategy": "TickMaBreakout", "strategy_params": {"short_period": 50, "long_period": 200, "sl_points": 10.0, "tp_points": 20.0}}` — SL/TP live in `strategy_params` (not top-level fields). Tick runs persist with `timeframe: "TICK"` in history; `display_timeframe` only controls chart resampling (`M1`, `M5`, `H1`, …).
  * *Response:* `metrics`, `trades` (exact tick fill prices/times for tick runs), resampled `bars`, `indicators` aligned to bars, optional `run_id`. Strategies tagged `engine: "tick"` or `engine: "candle"` on `GET /api/v1/strategies`.
* **`GET /api/v1/backtests`**
  * *Description:* Paginated list of persisted backtest runs, newest first.
  * *Parameters:* `limit` (default 50), `offset` (default 0), optional `symbol`.
  * *Response:* `{"items": [{"run_id": "...", "symbol": "WIN$", "strategy": "MACrossover", "timeframe": "M5", "status": "completed", "created_at": "2026-06-09T12:00:00Z", "summary": {...}}], "total": 42, "limit": 50, "offset": 0}`
* **`GET /api/v1/backtests/{run_id}`**
  * *Description:* Full metadata for a single persisted backtest run (config + metrics summary). Does not include trades/bars/indicators.
  * *Response:* `{"run_id": "...", "symbol": "WIN$", "strategy": "MACrossover", "timeframe": "M5", "status": "completed", "config": {...}, "result_summary": {...}, "error_message": null, "started_at": "...", "finished_at": "...", "created_at": "..."}`

### Optuna Parameter Optimization
* **`POST /api/v1/optimize`**
  * *Description:* Launch an asynchronous Optuna parameter optimization study.
  * *Request Body (JSON):* Specify study configurations, parameters, bounds, and strategy parameters. Runs in a background thread worker.
  * *Response:* `{"study_id": "3f9a1c8e7b0d4f6a9c2e1d8b5f4a3c2e", "status": "pending"}` (`study_id` is a 32-char hex string)
* **`GET /api/v1/optimize/{study_id}`**
  * *Description:* Retrieve the active status and current progress (completed trials, best values, error messages) of the optimization study.
* **`POST /api/v1/optimize/{study_id}/cancel`**
  * *Description:* Request cancellation of an active optimization study.
* **`GET /api/v1/optimize/{study_id}/results`**
  * *Description:* Fetch the completed Optuna trials list, Pareto front (for multi-objective studies), and the overall best parameters. When the in-memory job is gone (e.g. after server restart), results are rebuilt from the persisted study and trials in Postgres.
* **`GET /api/v1/optimizations`**
  * *Description:* Paginated list of persisted optimization studies, newest first.
  * *Parameters:* `limit` (default 50), `offset` (default 0).
  * *Response:* `{"items": [{"study_id": "3f9a...", "name": "WIN$ MA sweep", "status": "done", "best_value": 1.83, "n_trials": 100, "completed_trials": 100, "created_at": "2026-06-09T12:00:00Z"}], "total": 7, "limit": 50, "offset": 0}`

---

## 🔒 Security & Developer Notes
* **Local In-Memory Execution:** No trading keys or credentials are sent outside your local machine.
* **CORS Policy:** Programmed with open CORS middleware (`allow_origins=["*"]`) for easy interaction with the React/Tauri frontend during local development. Make sure to lock down origins when deploying outside a local staging environment.
