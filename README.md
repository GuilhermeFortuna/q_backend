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
                             │  Enqueue jobs (Redis)
                             ▼
                  ┌──────────────────────┐
                  │  Dramatiq Worker Pool│
                  │ (Q_WORKER_PROCESSES) │
                  └──────────┬───────────┘
                             │  CPU-bound execution
       ┌─────────────────────┴─────────────────────┐
       ▼                                           ▼
┌──────────────┐                            ┌──────────────┐
│ Market Data  │                            │ Backtesting  │
│  Ingestion   │                            │  & Optuna    │
└──────┬───────┘                            └──────┬───────┘
       │                                           │
       ▼                                           ▼
┌──────────────┐                            ┌──────────────┐
│ MetaTrader 5 │                            │  Strategies  │
│   Terminal   │                            │ (Indicators, │
│  (B3/Forex)  │                            │ Crossovers)  │
└──────────────┘                            └──────────────┘
```

Heavy jobs — backtests (via `POST /api/v1/backtest`), Optuna studies, walk-forward runs, and strategy discovery — execute in a **Dramatiq worker pool** backed by Redis, not inside the API process. The API enqueues work, tracks progress in Redis (24h TTL) and Postgres, and serves results from the Parquet lake. Start the worker with `uv run worker` alongside the API (see [Running the Worker Pool](#6-running-the-worker-pool)).

### 1. Market Data Routing & Ingestion (`market_data`)
* **Provider abstraction:** `MarketDataService` routes all reads through a pluggable provider (`MarketDataProvider` protocol). **MetaTrader 5** (`MetaTraderClient`) is used when available; **local parquet** (`LocalParquetClient`, WO48) is the offline fallback. The active provider is chosen from a persisted runtime setting (`auto` | `mt5` | `local`) in `data/runtime_config.json` (`Q_RUNTIME_CONFIG_PATH` overrides the file path). `GET`/`PUT /api/v1/system/data-source` expose and change the setting; `GET /api/v1/system/health` reports `mt5_available` and `active_provider`.
* **Linux / no-MT5 development:** `MetaTrader5` is optional — on non-Windows platforms `uv sync` installs a no-op stub so the API and worker boot without the real terminal. With `data_source=auto` and MT5 absent, reads resolve to the local provider (empty until WO48 fills the store).
* **MetaTrader 5 Integration:** High-speed client (`MetaTraderClient`) communicating directly with a running MT5 Windows terminal when installed (`metatrader5` is installed automatically on Windows via `uv sync`; on Linux use the stub or set `data_source=local`).
* **Asset Support:** Tailored to ingestion of **B3 (Bovespa)** symbols (e.g. `PETR4`, `VALE3`, `ITUB4`) and liquid **B3 Futures** contracts (e.g. `WIN$` Mini-Index, `WDO$` Mini-Dollar, and `CCM$` Corn Futures).
* **Multi-Format Datatypes:** Optimized data schemas for Tick-by-Tick transactions and standardized OHLCV candle streams (from 1-minute `M1` to Monthly `MN1` intervals).
* **Columnar tick loader:** `MetaTraderClient.get_ticks_columnar` / `MarketDataService.get_ticks_columnar` fetch historical ticks as aligned NumPy arrays (no per-row Pydantic objects) for the tick backtest engine. Results are cached on disk as Parquet (see below).
* **Tick cache:** Parquet files under `data/tick_cache/` by default (`Q_TICK_CACHE_DIR` overrides). Key = `{symbol_slug}_{sha256(symbol|start|end|flags)[:12]}`. Delete files in that directory to force a refetch from MT5.
* **Local market store (WO48 / WO50):** Portable OHLCV and tick parquet under `data/market/` by default (`Q_MARKET_DATA_ROOT` / `Settings.market_data_root`). Layout: `ohlcv/{symbol_slug}/{timeframe}/{YYYY}.parquet`, `ticks/{symbol_slug}/{YYYY-MM}.parquet`, plus `catalog.json`. Catalog entries include a `kind` field (`"bars"` or `"ticks"`); existing bar rows default to `"bars"` when `kind` is omitted. Local tick mode serves **all stored ticks** (COPY_TICKS_ALL shape); requested `flags` are ignored. Copy the folder or repoint the root to move data between machines (e.g. Windows ingest → Linux backtest). **Storage API:** `GET /api/v1/storage/inventory`, `POST /api/v1/storage/ingest` with `kind: "bars" | "ticks"` (MT5 → local, Windows-only source), `GET /api/v1/storage/ingest/{job_id}`, `DELETE /api/v1/storage/{symbol}/{timeframe}`.
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
* **Pluggable Strategy Registry:** Built-in strategies (`MACrossover`, `RSIMeanReversion`, `BollingerReversion`, `MACD`, `DonchianBreakout`, `VMA`, `FMA`, `TRB`, `TSMOM`) register parameter schemas consumed by the optimization engine and frontend forms via `GET /api/v1/strategies`. `VMA`/`FMA`/`TRB` implement the Lai & Lau (2006) price-vs-MA and close-based trading-range rules. `TSMOM` implements the Moskowitz–Ooi–Pedersen (2012) time-series momentum SIGN rule with bar-count rebalancing (Baltas & Kosowski 2017).

#### Genome DSL / `CompositeStrategy`

Genetic strategy search (WO39+) evolves **structure** in a JSON genome document while Optuna optimizes numeric knobs per genome via the existing walk-forward path. The interpreter lives under `q_backend/backtesting/genome/`:

* **`Genome` schema** — versioned DAG of typed nodes (`source.*`, `ind.*`, `cmp.*`, `logic.*`, `exit.*`) with `entry_long` / `entry_short` / `exit_long` / `exit_short` signal refs.
* **`CompositeStrategy`** — single registry entry (`"CompositeStrategy"`). Each candidate passes its genome in `fixed_params["genome"]`; trial params merge into the genome before interpretation.
* **Causal by construction** — only backward-looking indicators and `shift(1)` event detection; `transform.shift.bars` is hard-locked to `1`. Covered by `test_strategy_causality.py` (default MA-crossover genome) and `test_composite_genome_causality.py` (seeded random valid genomes).
* **`derive_genome_search_space(genome)`** — returns WO30-shaped `SearchSpaceConfig` + `fixed_params` from `GENOME_PARAM_BOUNDS`.

Full grammar and design rationale: [`docs/design/genetic-strategy-search.md`](../q_frontend/docs/design/genetic-strategy-search.md) (§2–§3).

#### Genetic synthesis (`optimization/genetic_search.py`)

WO39 adds evolution on top of the genome interpreter without changing WO31's evaluator:

* **`GeneticCandidateProvider`** — owns a population; `candidates()` yields `SearchCandidate(strategy="CompositeStrategy", fixed_params={"genome": ...})`; `report()` runs tournament selection, crossover, mutation, and elitism for the next generation.
* **`GeneticStrategySearchOrchestrator`** — loops `generations × population_size`, calling `evaluate_candidate` each time (same walk-forward + OOS gates as registry sweep), then `provider.report()`.
* **Fitness** — OOS `robustness_score` minus a parsimony penalty (`complexity_lambda × node_count + complexity_mu × param_count`). Gate-passing genomes occupy the top fitness band; near-viable failures can still breed via graded penalties (WO52).
* **Graded selection (WO52)** — tournament breeding uses a continuous selection fitness: robustness minus complexity minus *soft* gate penalties, with finite floors for `no_result` / `error` genomes. This gives the GA a gradient before any genome clears the hard gates; the published leaderboard still requires `passed_gates`.
* **Trade-viability (WO53)** — generation and repair bias toward genomes that fire in-sample (`genome_signal_activity`); a cheap pre-screen skips walk-forward for genomes with no entry signals (`prescreen_min_signals`, default `1`; set `0` to disable).
* **Job seam** — `select_search_orchestrator(config, backtest_runner)` returns the genetic orchestrator when `StrategySearchConfig.genetic` is set, else the existing `StrategySearchRunner`.

See design doc §4–§5.3 for operator details and initial population mix (50% mutated registry fixtures / 50% random valid DAGs).

#### Overfitting defense (genetic runs)

When `StrategySearchConfig.genetic` is set, a post-evolution **finalize** step applies two screening layers before the run verdict is persisted:

* **Deflated Sharpe Ratio (DSR)** — Bailey & López de Prado correction over the effective trial count (`population × generations`). The champion's OOS Sharpe is deflated for how many genomes were tried; stored as `champion_dsr`, `n_trials_effective`, and `sr_observed` on the run summary.
* **Held-out lock-box** — optional `lockbox` config carves the final 10–15% of the date range **before** walk-forward windows; the champion is backtested once on that tail with no re-optimization. `lockbox_metrics` and `lockbox_passed` are persisted on the run summary.
* **Parsimony penalty** — fitness subtracts `complexity_lambda × node_count + complexity_mu × param_count` during evolution (WO39).

High DSR and a passing lock-box are **screening signals, not proof** of live edge. See [`docs/design/genetic-strategy-search.md`](../q_frontend/docs/design/genetic-strategy-search.md) §5.4.

* **Position sizers:** `fixed_quantity`, `fixed_safety_margin`, and `inverse_volatility` (vol targeting). The inverse-volatility sizer reads an annualized `volatility` column from the signal bar passed through the engine fill row — strategies such as `TSMOM` expose this column; without it the sizer skips the order. Sizing formula:

  ```
  contracts = floor((target_volatility_pct / 100) * capital / (volatility * price * point_value))
  ```

  clamped to `[min_contracts, max_contracts]`. `target_volatility_pct` is annualized (e.g. `10.0` = 10%). Yang–Zhang and close-to-close estimators live in `technical_indicators.py`.
* **Strategy search (`optimization/strategy_search.py`):** Automatic discovery sweep — for each registered candle strategy, derive an Optuna search space from the registry (WO30), optimize in-sample per walk-forward window, stitch out-of-sample equity, and rank candidates on OOS performance (never in-sample). Walk-forward **gates** flag weak results (too few windows/trades, low IS/OOS efficiency, suspiciously high efficiency). The `CandidateProvider` protocol is the extension seam: `RegistryCandidateProvider` sweeps built-in strategies; `GeneticCandidateProvider` (WO39) evolves composite genomes via `select_search_orchestrator` when `config.genetic` is set. Both reuse the same `evaluate_candidate` path.

* **Advanced Analytics Suite (`TradeRegistry`):** Aggregates execution history and computes comprehensive mathematical metrics:
  * Win Rate, Expectancy, and Profit Factor.
  * Cumulative PnL & Peak Equity Tracking.
  * Precise Maximum Drawdown (Value & Percentage).
  * Recovery Factor & Win/Loss streaks.
* **Transaction costs (candle engine):** Optional per-run `TransactionCostConfig` on `POST /api/v1/backtest/run` (`costs` field) and optimization backtest config. Both terms default to zero so existing runs stay gross-of-costs unless configured. Each side (entry and exit) pays once:

  ```
  side_cost = q * cost_per_contract + (cost_bps / 10_000) * p * q * pv
  ```

  where `q` is contract quantity, `p` is the fill price for that side, and `pv` is the symbol point value. Entry commission is set when the trade opens; exit commission is added at close. Net PnL and `total_commission` in performance metrics reflect both sides. The tick engine is out of scope (spread model only for now).

### 3. Storage Infrastructure (`storage`)

Three-tier storage keeps analytical payloads separate from operational metadata:

| Tier | Technology | Purpose |
|------|------------|---------|
| **Analytical** | Parquet lake at `Q_DATA_LAKE_ROOT` | Backtest trades/equity artifacts (present); OHLCV/ticks/features (future) |
| **Metadata** | PostgreSQL | Strategies, versions, backtest configs/runs, optimization studies/trials, ingestion run records |
| **Runtime** | Redis | Job progress (JSON, 24h TTL), cache, locks |

Postgres stores **metadata and lake pointers** (`lake_path`, `lake_paths`, `result_summary`) only. Market data is not stored in Postgres tables.

**Backtest artifact layout** (under `Q_DATA_LAKE_ROOT`, default `data/lake/`):

```
{data_lake_root}/backtests/{run_id}/trades.parquet
{data_lake_root}/backtests/{run_id}/equity.parquet
```

Completed runs from `POST /api/v1/backtest/run` write both files and store relative paths in `BacktestRun.lake_paths`.

**Walk-forward artifact layout:**

```
{data_lake_root}/walkforward/{run_id}/oos_equity.parquet
{data_lake_root}/walkforward/{run_id}/oos_trades.parquet
{data_lake_root}/walkforward/{run_id}/windows.parquet
```

Completed runs from `POST /api/v1/walkforward` store relative paths in `walkforward_runs.lake_paths`. The stitched OOS equity curve and trade list live in the lake only; Postgres holds run/window metadata and summary metrics.

**Strategy search artifact layout:**

```
{data_lake_root}/strategy_search/{run_id}/leaderboard.parquet
{data_lake_root}/strategy_search/{run_id}/candidates/{candidate_id}/oos_equity.parquet
{data_lake_root}/strategy_search/{run_id}/candidates/{candidate_id}/oos_trades.parquet  # when present
```

Completed runs from `POST /api/v1/strategy-search` store relative paths in `strategy_search_runs.lake_paths`. Per-candidate stitched OOS equity curves live in the lake; Postgres holds run metadata, per-candidate summary metrics, and the leaderboard summary.

---

## 🛠 Tech Stack

* **Core Runtime:** Python `>=3.12`
* **API Framework:** FastAPI, Uvicorn (ASGI web server), CORS Middleware
* **Numerical Stack:** Pandas, NumPy (Vectorized market-data calculations)
* **Broker & Data Clients:** MetaTrader 5 (MT5 Python package), Yahoo Finance (`yfinance`)
* **Serialization & Validation:** Pydantic `v2` (Declarative typesafe schemas)
* **Metadata Storage:** PostgreSQL, SQLAlchemy 2.x, Alembic
* **Runtime State:** Redis (job progress, cache, locks, Dramatiq broker)
* **Background Jobs:** Dramatiq (worker pool for backtests, optimization, walk-forward, discovery)
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
│       ├── api/          # FastAPI routes & async job managers (backtest, optimize, …)
│       ├── backtesting/  # Engine, Position Sizers, Performance Registry & Strategies
│       ├── cli/          # CLI entry points (`worker`, `q-optimize`)
│       ├── market_data/  # MT5 service wrappers & data pipelines
│       ├── optimization/ # Optuna runner, study storage, walk-forward, discovery
│       ├── tasks/        # Dramatiq broker, actors, fan-in, worker context
│       └── storage/      # Settings, Postgres models, Redis helpers, Parquet lake
│           ├── settings.py
│           ├── lake/     # Backtest artifact read/write (Parquet)
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
# or: uv run dev
```
The interactive API Swagger docs will be immediately accessible at [http://localhost:8000/docs](http://localhost:8000/docs).

For full-stack local development with the Quant desktop app, run the worker pool as well (step 6). Without it, async jobs stay queued and never complete.

### 6. Running the Worker Pool

Heavy jobs — backtests, optimizations, walk-forward analyses, and strategy
discovery — no longer execute inside the API process. They are dispatched to a
**Dramatiq worker pool** (backed by Redis) that runs as a **separate process**. Start
it alongside the API:

```bash
uv run worker
```

This launches `dramatiq q_backend.tasks --processes $Q_WORKER_PROCESSES --threads 1`.
The pool is the single CPU budget shared by every job: a job fans its work out into
many small messages (per Optuna trial batch, per walk-forward window, per discovery
candidate, per backtest) that drain into the fixed pool, so running several jobs at
once shares the cores fairly instead of oversubscribing the machine. Size it with
`Q_WORKER_PROCESSES` in `.env` (default **14**, leaving headroom on a 16-core box).

> Each worker process opens its own MetaTrader 5 connection on boot. The API and the
> worker must both be running for jobs to make progress — the API enqueues and serves
> status; the worker executes. Orphaned runs (worker killed mid-job) are reconciled to
> `cancelled` on the next API startup.

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
  * *Built-in strategies:* `MACrossover`, `RSIMeanReversion`, `BollingerReversion`, `MACD`, `DonchianBreakout`, `VMA` (Lai & Lau 2006 variable MA), `FMA` (fixed holding-period MA), `TRB` (close-based trading-range breakout), `TSMOM` (time-series momentum SIGN rule, MOP 2012).
* **`POST /api/v1/backtest`**
  * *Description:* Dispatch an async backtest to the Dramatiq worker pool (preferred for the desktop app). Poll status and fetch the full chart payload when complete.
  * *Request body:* Same fields as `BacktestJobRequest` (symbol, timeframe, strategy, `engine`, etc.).
  * *Response:* `{"run_id": "<uuid>", "status": "running"}`
* **`GET /api/v1/backtest/{run_id}`**
  * *Description:* Status of an async backtest (`running`, `completed`, `failed`, `cancelled`).
* **`GET /api/v1/backtest/{run_id}/result`**
  * *Description:* Full backtest response (metrics, trades, bars, indicators) once the run completes. Payload is read from the Parquet lake.
* **`POST /api/v1/backtest/run`**
  * *Description:* Runs a candle (`engine: "candle"`, default) or tick (`engine: "tick"`) strategy backtest **synchronously** in the API process (useful for scripts and quick one-offs).
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
* **`PATCH /api/v1/backtests/{run_id}`**
  * *Description:* Update run metadata (e.g. toggle `saved_only` bookmark flag).
* **`DELETE /api/v1/backtests/{run_id}`**
  * *Description:* Delete run metadata and lake artifacts (`204`).
* **`POST /api/v1/backtests/bulk-delete`**
  * *Description:* Delete multiple runs by id list.
* **`GET /api/v1/backtests/{run_id}/artifacts/equity`**
  * *Description:* Equity curve points for a completed run, read from the Parquet lake. Works without Postgres when artifact files exist on disk.
  * *Response:* `{"run_id": "<uuid>", "points": [{"time": "<ISO8601>", "equity": <float>}, ...]}`
* **`GET /api/v1/backtests/{run_id}/artifacts/trades`**
  * *Description:* Closed trades for a completed run, read from the Parquet lake. Trade objects match the shape returned by `POST /api/v1/backtest/run`.
  * *Response:* `{"run_id": "<uuid>", "trades": [<trade>, ...]}`
  * *Errors:* `404` when the run id is invalid or artifacts were never written (e.g. runs predating lake support).

### Optuna Parameter Optimization
* **`POST /api/v1/optimize`**
  * *Description:* Launch an asynchronous Optuna parameter optimization study.
  * *Request Body (JSON):* Specify study configurations, parameters, bounds, and strategy parameters. Dispatched to the Dramatiq worker pool.
  * *Tick engine:* set `backtest.engine` to `"tick"` to optimize tick-native strategies. Ticks for the study symbol and date range are loaded **once** via `MarketDataService.get_ticks_columnar` (cache-backed) when the job starts and reused in memory for every trial — the same load-once pattern as OHLCV for candle studies.
  * *Response:* `{"study_id": "3f9a1c8e7b0d4f6a9c2e1d8b5f4a3c2e", "status": "pending"}` (`study_id` is a 32-char hex string)
* **`GET /api/v1/optimize/{study_id}`**
  * *Description:* Retrieve the active status and current progress (completed trials, best values, error messages) of the optimization study.
  * *Response fields:* includes `workers` (resolved parallel process count for candle studies; `1` when sequential or tick).
* **`POST /api/v1/optimize/{study_id}/cancel`**
  * *Description:* Request cancellation of an active optimization study.
* **`GET /api/v1/optimize/{study_id}/results`**
  * *Description:* Fetch the completed Optuna trials list, Pareto front (for multi-objective studies), and the overall best parameters. When the in-memory job is gone (e.g. after server restart), results are rebuilt from the persisted study and trials in Postgres.
* **`GET /api/v1/optimizations`**
  * *Description:* Paginated list of persisted optimization studies, newest first.
  * *Parameters:* `limit` (default 50), `offset` (default 0).
  * *Response:* `{"items": [{"study_id": "3f9a...", "name": "WIN$ MA sweep", "status": "done", "best_value": 1.83, "n_trials": 100, "completed_trials": 100, "created_at": "2026-06-09T12:00:00Z"}], "total": 7, "limit": 50, "offset": 0}`

#### Parallel optimization

Candle-engine studies can fan trials out across worker processes when the caller passes an in-memory OHLCV frame to `OptimizationRunner` (see WO36 for API wiring). The main process owns the Optuna study and uses **ask/tell**; workers run only the backtest over a frame shipped once via a `ProcessPoolExecutor` initializer. Inner backtests force `parallel_mode=SEQUENTIAL` so DAY_TRADE subprocess pools do not oversubscribe.

- **TPE under parallelism:** the parallel path builds `TPESampler(constant_liar=True)` so batched `ask()` does not propose near-duplicate trials. Batch size equals worker count.
- **Candle-only:** tick studies stay on the sequential path (tick arrays are not shared across processes in this release).
- **Pruning:** median/hyperband intermediate-value pruning is disabled with a warning when parallel mode is active; exception-based pruning (zero trades, `ExpectedTrialFailure`) still applies.
- **Quality vs speed:** parallel results are not byte-identical to sequential runs (sampler sees completions in a different order). Compare comparable best objectives, not trial order.

Worker count is resolved by `q_backend.optimization.parallel.resolve_worker_count(max_workers, n_trials)`.

**API:** optional `study.max_workers` on `POST /api/v1/optimize` (omit for auto, `1` for sequential). Status responses include `"workers"` — the resolved process count for candle studies (`1` for tick or sequential).

### Walk-forward analysis
* **`POST /api/v1/walkforward`**
  * *Description:* Launch an asynchronous walk-forward analysis (optimize in-sample per window, test out-of-sample, stitch OOS equity).
  * *Request body:* `{"optimization": <OptimizationConfig>, "walkforward": {"train_days": 90, "test_days": 30, "mode": "rolling", "min_windows": 2}}`
  * *Response:* `{"run_id": "<32-char hex>", "status": "pending"}`
  * *Errors:* `422` when the date range is too short for `min_windows`, when `engine` is `"tick"`, or for other validation failures.
* **`GET /api/v1/walkforward/{run_id}`**
  * *Description:* Live progress (`current_window`, `total_windows`, `phase`, `windows_completed`) from memory/Redis, with DB fallback after restart.
* **`GET /api/v1/walkforward/{run_id}/results`**
  * *Description:* Per-window IS/OOS metrics, aggregate OOS metrics, efficiency ratio, and stitched equity curve points. Rebuilt from Postgres + lake when the in-memory job is gone.
* **`POST /api/v1/walkforward/{run_id}/cancel`**
  * *Description:* Cooperative cancellation between windows.
* **`GET /api/v1/walkforwards`**
  * *Description:* Paginated history list, newest first (`limit`, `offset`).
* **`DELETE /api/v1/walkforwards/{run_id}`**
  * *Description:* Delete run metadata and lake artifacts (`204`).
* **`GET /api/v1/walkforward/{run_id}/artifacts/equity`**
  * *Description:* Stitched OOS equity curve in the same point-list shape as backtest equity artifacts (`{"run_id", "points": [{"time", "equity"}, ...]}`).

### Strategy search (Discovery)
* **`POST /api/v1/strategy-search`**
  * *Description:* Launch an asynchronous strategy search (sweep registered candle strategies → optimize → walk-forward validate → OOS-ranked leaderboard).
  * *Request body:* WO31 `StrategySearchConfig` JSON (`backtest`, `objective`, `walkforward`, `study`, optional `strategies`, `include_risk_search`, `gates`).
  * *Response:* `{"run_id": "<32-char hex>", "status": "pending"}`
  * *Errors:* `422` for multi-objective mode, date range too short for `min_windows`, or other validation failures.
* **`GET /api/v1/strategy-search/{run_id}`**
  * *Description:* Live progress (`current_candidate`, `total_candidates`, `candidate_id`, `strategy`, `phase`, `window_index`, `total_windows`) from memory/Redis, with DB fallback after restart.
* **`GET /api/v1/strategy-search/{run_id}/results`**
  * *Description:* Full leaderboard (per-candidate records, summary, best candidate). Rebuilt from Postgres + lake when the in-memory job is gone.
* **`POST /api/v1/strategy-search/{run_id}/cancel`**
  * *Description:* Cooperative cancellation between candidates.
* **`GET /api/v1/strategy-searches`**
  * *Description:* Paginated history list, newest first (`limit`, `offset`).
* **`DELETE /api/v1/strategy-searches/{run_id}`**
  * *Description:* Delete run metadata and lake artifacts (`204`).
* **`GET /api/v1/strategy-search/{run_id}/candidates/{candidate_id}/artifacts/equity`**
  * *Description:* Stitched OOS equity for one candidate (`{"run_id", "candidate_id", "points": [{"time", "equity"}, ...]}`).

---

## 🔒 Security & Developer Notes
* **Local In-Memory Execution:** No trading keys or credentials are sent outside your local machine.
* **CORS Policy:** Programmed with open CORS middleware (`allow_origins=["*"]`) for easy interaction with the React/Tauri frontend during local development. Make sure to lock down origins when deploying outside a local staging environment.
