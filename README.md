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
* **Robust Resiliency:** Smart automatic reconnection and local environment configuration mapping.

### 2. High-Performance Backtesting Engine (`backtesting`)
* **Multi-Execution Engines:**
  * `SEQUENTIAL`: Standard path tracking (ideal for swing-trading strategies).
  * `DAY_TRADE`: Concurrent chunked backtesting (utilizes standard Python `ProcessPoolExecutor` to process daily sessions across multi-core CPUs in parallel).
* **Signal & Order Pipeline:** Modular pipeline translating strategy `Signal` structures into executable `Order` definitions using pluggable `PositionSizer` logic.
* **Vectorized Computations:** Employs precomputed Technical Indicators via vectorized pandas operations, preventing lookahead bias while maintaining massive throughput.
* **Advanced Analytics Suite (`TradeRegistry`):** Aggregates execution history and computes comprehensive mathematical metrics:
  * Win Rate, Expectancy, and Profit Factor.
  * Cumulative PnL & Peak Equity Tracking.
  * Precise Maximum Drawdown (Value & Percentage).
  * Recovery Factor & Win/Loss streaks.

---

## 🛠 Tech Stack

* **Core Runtime:** Python `>=3.12`
* **API Framework:** FastAPI, Uvicorn (ASGI web server), CORS Middleware
* **Numerical Stack:** Pandas, NumPy (Vectorized market-data calculations)
* **Broker & Data Clients:** MetaTrader 5 (MT5 Python package), Yahoo Finance (`yfinance`)
* **Serialization & Validation:** Pydantic `v2` (Declarative typesafe schemas)
* **Dependency & Package Manager:** `uv` (Rust-powered modern Python package toolchain)
* **Unit Testing:** `pytest`

---

## 📂 Project Directory Structure

```txt
q_backend/
├── .env                  # Local secrets and connection paths (loaded automatically)
├── pyproject.toml        # Hatchling build configuration & dependency definitions
├── uv.lock               # Deterministic dependency lockfile
├── scripts/              # Standalone utility & validation scripts
│   └── backtests/        # High-performance backtesting runners
│       └── run_ccm_backtest.py
├── src/
│   └── q_backend/        # Core packages
│       ├── api/          # FastAPI App initialization & route definitions
│       │   └── main.py   # System Health, Realtime Snaps, and Market Data endpoints
│       ├── backtesting/  # Engine, Position Sizers, Performance Registry & Strategies
│       │   ├── engine.py
│       │   ├── models.py
│       │   ├── position_sizing.py
│       │   ├── registry.py
│       │   └── strategy.py
│       └── market_data/  # MT5 service wrappers & data pipelines
│           ├── clients/
│           │   ├── metatrader.py
│           │   └── yfinance.py
│           ├── models.py
│           ├── repository.py
│           └── service.py
└── tests/                # Comprehensive test suites
    └── backtesting/      # Strategy, Registry, and Engine unit tests
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
Clone or navigate to the repository and duplicate the template configuration:
```bash
cp .env.template .env
```
Open `.env` and fill in your MetaTrader 5 connection configuration details:
```ini
MT5_USER=12345678            # Your MetaTrader 5 Login account ID
MT5_PASSWORD="YourPassword"  # Your broker password
MT5_SERVER="Broker-Server"   # The MT5 Server (e.g. XP-Demo, Clear-PRD)
MT5_PATH="C:/Program Files/MetaTrader 5/terminal64.exe" # Path to terminal executable
```

### 2. Install Dependencies
Initialize a virtual environment and sync packages using `uv`:
```bash
uv sync
```

### 3. Running Unit Tests
Verify the complete backtesting pipeline and indicator calculations:
```bash
uv run pytest
```

### 4. Running the Dev Server
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
  * *Description:* Advanced system diagnostic dashboard info.
  * *Response:* `{"status": "healthy", "backendVersion": "0.1.0", "dataLakeStatus": "online", "lastSyncAt": "2026-05-30..."}`

### B3 Asset Directory & Realtime
* **`GET /api/v1/market/instruments`**
  * *Description:* Retrieves B3/Bovespa core instrument master definitions (`PETR4`, `VALE3`, `ITUB4`, `WIN$`, `WDO$`).
* **`GET /api/v1/market/snapshot/{symbol}`**
  * *Description:* Obtains high-speed real-time pricing and daily volume data directly from the active terminal.
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

---

## 🔒 Security & Developer Notes
* **Local In-Memory Execution:** No trading keys or credentials are sent outside your local machine.
* **CORS Policy:** Programmed with open CORS middleware (`allow_origins=["*"]`) for easy interaction with the React/Tauri frontend during local development. Make sure to lock down origins when deploying outside a local staging environment.
