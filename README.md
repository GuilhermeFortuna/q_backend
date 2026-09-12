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
* **Provider abstraction:** `MarketDataService` routes all reads through a pluggable provider (`MarketDataProvider` protocol). **MetaTrader 5** (`MetaTraderClient`) is used when available; the **remote MT5 gateway** (`RemoteMt5Client`, WO183/WO184) fetches fresh futures data over HTTP from an MT5 terminal running under Wine or on another box; **local parquet** (`LocalParquetClient`, WO48) is the offline fallback. The active provider is chosen from a persisted runtime setting (`auto` | `mt5` | `remote` | `local`) in `data/runtime_config.json` (`Q_RUNTIME_CONFIG_PATH` overrides the file path). `GET`/`PUT /api/v1/system/data-source` expose and change the setting; `GET /api/v1/system/health` reports `mt5_available` and `active_provider`.
* **Remote MT5 gateway (WO183/WO184):** `RemoteMt5Client` speaks the gateway's versioned `/v1/` wire contract (JSON metadata + `np.savez_compressed` `.npz` bulk payloads) and returns the same naive-Brasília datetimes as `MetaTraderClient`. Gateway base URL and optional shared-secret token resolve from the runtime config keys `remote_gateway_url` / `remote_gateway_token`, with env vars `Q_MT5_GATEWAY_URL` / `Q_MT5_GATEWAY_TOKEN` taking precedence. `is_available()` health-probes `/v1/health` (~1s timeout, 30s cache) and refuses a mismatched schema major version. Service/routing wiring lands in WO185.
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
* **Pluggable Strategy Registry:** Built-in candle strategies (`MACrossover`, `RSIMeanReversion`, `BollingerReversion`, `MACD`, `DonchianBreakout`, `VMA`, `FMA`, `TRB`, `TSMOM`, `GatevPairs`, `HurstTrendBlend`) plus tick strategies (`TickMaBreakout`) and the genome interpreter entry (`CompositeStrategy`) register parameter schemas consumed by the optimization engine and frontend forms via `GET /api/v1/strategies`. `VMA`/`FMA`/`TRB` implement the Lai & Lau (2006) price-vs-MA and close-based trading-range rules. `TSMOM` implements the Moskowitz–Ooi–Pedersen (2012) time-series momentum SIGN rule with bar-count rebalancing (Baltas & Kosowski 2017). `GatevPairs` implements Gatev–Goetzmann–Rouwenhorst (2006) distance-based pairs trading. `HurstTrendBlend` blends trend and mean-reversion regimes using a Hurst exponent filter.

#### Composable exit-rule registry (`backtesting/exit_rules/`)

Candle strategies share a flat exit-parameter dict (fixed % SL/TP, ATR SL/TP, trailing %, etc.) merged into every registered candle strategy by `register_strategy`. Exits are no longer a monolithic class hard-wired in three places — they are **composable modules** behind an `ExitRule` protocol:

* **`ExitRule`** — each rule declares `param_specs()`, `is_enabled(params)`, optional `required_columns(params)`, optional `on_bar()` (per-trade state), and `should_exit()`. Rules emit full-position `CLOSE` signals only.
* **`exit_rules/registry.py`** — single source of truth: `EXIT_RULES`, `all_param_specs()`, `enabled_rules(params)`, `required_columns(params)`, `list_exit_rules()`, `shared_exit_params()`. `get_exit_strategy_params()`, `factory.build_strategy`, and the engine all read from here.
* **`exit_rules/presets.py`** — hand-curated `EXIT_PRESETS` catalog for one-click workbench combos.
* **`ExitStrategy(params)`** — coordinator that resolves enabled rules from the flat dict, owns per-trade state (`dict[trade_id → dict[rule_id → state]]`), runs `on_bar` then `should_exit` in registry order (first trigger wins), and prunes stale trade state each bar.
* **Engine column prep** — `_run_single_chunk` asks `exit_strategy.required_columns()` and vectorizes any missing columns via a column-name→compute-fn map (`atr_{period}` → `compute_atr`; `donchian_high_{period}` / `donchian_low_{period}` → `compute_donchian_channels`). No exit param names are hard-coded outside the registry.
* **Adding a new exit** — implement one `ExitRule` module and append it to `EXIT_RULES`; optional scalar params default to `0`/disabled so saved strategies, the optimizer, and genomes need no migration.

Legacy fixed/ATR/trailing behaviors live in `exit_rules/legacy.py` with byte-identical trigger math to the pre-refactor monolith. Specialized stop/trailing rules (WO62+):

* **Chandelier** (`chandelier.py`, `exit_group=trailing`) — `chandelier_atr_mult`: trailing stop at peak high − mult×ATR (mirror for shorts); reuses shared `atr_period` (`exit_group=general`).
* **Break-even** (`breakeven.py`, `exit_group=stop_loss`) — `breakeven_trigger_pct` arms once gain ≥ trigger; stop snaps to entry ± `breakeven_offset_pct`.
* **Parabolic SAR** (`parabolic_sar.py`, `exit_group=trailing`) — `psar_af_start` (0 disables), `psar_af_step`, `psar_af_max`: textbook Wilder SAR per trade — seeded from entry/first-bar extremes, advanced each bar, and clamped so SAR never penetrates the prior two bars' range.
* **Profit-target ratchet** (`profit_target_ratchet.py`, `exit_group=target`) — `target_ratchet_atr`: arms a trailing profit floor once price reaches entry ± ATR multiple; ratchet rises/falls with new extremes (reuses shared `atr_period`).
* **Time stop** (`time_stop.py`, `exit_group=time`) — `max_bars_in_trade`: closes after N bars in trade via a per-trade bar counter in rule state.
* **Donchian channel stop** (`donchian_stop.py`, `exit_group=trailing`) — `donchian_exit_period`: exits on cross of the opposite N-bar Donchian extreme; columns declared via `required_columns` and precomputed by the engine map.

#### Genome DSL / `CompositeStrategy`

Genetic strategy search (WO39+) evolves **structure** in a JSON genome document while Optuna optimizes numeric knobs per genome via the existing walk-forward path. The interpreter lives under `q_backend/backtesting/genome/`:

* **`Genome` schema** — versioned DAG of typed nodes (`source.*`, `ind.*`, `cmp.*`, `logic.*`, `exit.*`) with `entry_long` / `entry_short` / `exit_long` / `exit_short` signal refs.
* **`CompositeStrategy`** — single registry entry (`"CompositeStrategy"`). Each candidate passes its genome in `fixed_params["genome"]`; trial params merge into the genome before interpretation.
* **Causal by construction** — only backward-looking indicators and `shift(1)` event detection; `transform.shift.bars` is hard-locked to `1`. Covered by `test_strategy_causality.py` (default MA-crossover genome) and `test_composite_genome_causality.py` (seeded random valid genomes).
* **`derive_genome_search_space(genome)`** — returns WO30-shaped `SearchSpaceConfig` + `fixed_params` from `GENOME_PARAM_BOUNDS`, including `exit_*` param refs when a genome carries an exit-rule policy (WO80).

Exit-rule policies (WO80) attach composable catalog exits to a genome via `metadata.exit_rule_policy`:

```json
{
  "metadata": {
    "exit_rule_policy": {
      "preset_id": "atr_stop_chandelier",
      "params": {
        "stop_loss_atr": { "param": "exit_stop_loss_atr" },
        "atr_period": { "param": "exit_atr_period" },
        "chandelier_atr_mult": { "param": "exit_chandelier_atr_mult" }
      }
    }
  }
}
```

`CompositeStrategy` hydrates `ExitStrategy` from the merged trial params (enable/magnitude ranges include `0`). Exit-rule exits run through the engine's standard `exit_strategy.check_exits` path before genome signal exits. This differs from WO79 registry preset expansion, which clones whole strategy candidates for Discovery sweep.

Full grammar and design rationale: [`docs/design/genetic-strategy-search.md`](../q_frontend/docs/design/genetic-strategy-search.md) (§2–§3).

#### Genetic synthesis (`optimization/genetic_search.py`)

WO39 adds evolution on top of the genome interpreter without changing WO31's evaluator:

* **`GeneticCandidateProvider`** — owns a population; `candidates()` yields `SearchCandidate(strategy="CompositeStrategy", fixed_params={"genome": ...})`; `report()` runs tournament selection, crossover, mutation, and elitism for the next generation.
* **`GeneticStrategySearchOrchestrator`** — loops `generations × population_size`, calling `evaluate_candidate` each time (same walk-forward + OOS gates as registry sweep), then `provider.report()`.
* **Fitness** — OOS `robustness_score` minus a parsimony penalty (`complexity_lambda × node_count + complexity_mu × param_count`). Gate-passing genomes occupy the top fitness band; near-viable failures can still breed via graded penalties (WO52).
* **Graded selection (WO52)** — tournament breeding uses a continuous selection fitness: robustness minus complexity minus *soft* gate penalties, with finite floors for `no_result` / `error` genomes. This gives the GA a gradient before any genome clears the hard gates; the published leaderboard still requires `passed_gates`.
* **Trade-viability (WO53)** — generation and repair bias toward genomes that fire in-sample (`genome_signal_activity`); a cheap pre-screen skips walk-forward for genomes with no entry signals (`prescreen_min_signals`, default `1`; set `0` to disable).
* **Parallel evaluation (WO54)** — one generation's genomes evaluate concurrently via `ProcessPoolExecutor` (`max_workers` on `GeneticSearchConfig`; `1` or unset single-core keeps the serial path). OHLCV is shipped once per worker process; walk-forward window parallelism is forced to `1` inside workers so candidate fan-out owns the CPU budget. Evolution (`provider.report`) stays single-threaded and deterministic. Defaults: `population_size=48`, `generations=12`.
* **Stronger operators (WO55)** — crossover exchanges whole compatible sub-DAGs (cut → splice → re-id → orphan prune), with single-node swap as fallback. Mutation rate adapts between `mutation_rate_min` / `mutation_rate_max` from a **structural** stagnation signal (node-kind fingerprints, no extra backtests); mutation operator weights nudge toward ops that recently improved fitness (`adaptive_operator_weights`). This is *variation* diversity and complements WO46's *selection* diversity (OOS-return decorrelation for a low-correlation book).
* **Exit-policy evolution (WO80)** — `GeneticSearchConfig` can seed a fraction of the initial population with curated exit presets (`seed_exit_policies`, `exit_policy_preset_ids`, `exit_policy_seed_fraction`). Exit-only mutation operators (`swap_exit_policy`, `add_exit_stop`, `replace_exit_with_preset`, `drop_exit_policy`, `nudge_exit_param_ref`) alter `metadata.exit_rule_policy` while preserving `entry_long` / `entry_short`. Candidate metadata includes `exit_policy_id`, `exit_policy_label`, `exit_param_names`, and `last_exit_mutation_op`. These fields are persisted on `strategy_search_candidates` and returned in live and DB-reloaded result payloads (WO83).
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

  **Exit-preset expansion (WO79, registry sweep only):** optional `exit_presets` on `StrategySearchConfig`:

  ```json
  {
    "enabled": false,
    "preset_ids": null,
    "include_baseline": true,
    "pin_non_preset_exits_off": true
  }
  ```

  When `exit_presets.enabled` is true, each selected candle strategy expands into one candidate per backend exit preset (plus the baseline strategy when `include_baseline` is true). Candidate ids follow `{StrategyName}__exit_{preset_id}` (e.g. `MACrossover__exit_fixed_pct_bracket`). Preset-owned exit params are searched with enable ranges whose `low` includes `0`, so the optimizer can explore entry-only and entry-plus-exit within the same candidate — exits are **search candidates, not forced on**. Non-preset applicable exit enable params are pinned to `0` when `pin_non_preset_exits_off` is true. With `enabled: false` (default), behavior is byte-compatible with the pre-WO79 registry sweep. `exit_presets` is rejected when `genetic` is set (WO80).

  **Exit-quality diagnostics (WO81):** completed candidates include optional `exit_quality` on the API payload (also stored in Postgres `diagnostics` JSON). Summaries are computed from stitched **OOS trades only** during `evaluate_candidate`:

  ```json
  {
    "exit_quality": {
      "total_closed_trades": 42,
      "by_reason": {
        "fixed_sl": {"trades": 12, "total_pnl": -4200.0, "win_rate": 0.0}
      },
      "holding_period": {"median_bars": 8, "p90_bars": 32},
      "path_quality": {
        "avg_mfe_capture_ratio": 0.47,
        "avg_profit_giveback": 310.0,
        "avg_mae": -180.0
      }
    }
  }
  ```

  Trade-only metrics (`by_reason`, timestamp-based `holding_period`) are always computed when closed OOS trades exist. Bar-path metrics (`median_bars`/`p90_bars`, `path_quality`) require OHLCV during evaluation or lake `oos_trades` on rebuild. **Ranking is unchanged by default**; optional `exit_quality_scoring` (`enabled: false`) adds soft diagnostic scores only.

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
| **Analytical** | Parquet lake at `Q_DATA_LAKE_ROOT` | Backtest, walk-forward, and strategy-search artifacts |
| **Market data** | Parquet store at `Q_MARKET_DATA_ROOT` | Local OHLCV/tick history (WO48/WO50); separate from the results lake |
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

**Feature matrix artifact layout** (WO129):

```
{data_lake_root}/features/{matrix_id}/matrix.parquet
{data_lake_root}/features/{matrix_id}/manifest.json
```

Feature matrices are cached by deterministic `matrix_id` (symbol, timeframe, date range, feature set, `ENGINE_VERSION`). Bump `ENGINE_VERSION` in `features/matrix.py` when compute semantics change.

### 4. Feature Intelligence (`features`)

Phase 1–2 platform for first-class features: registry, point-in-time computation, lake-cached matrices, Postgres-backed store, evaluation metrics, and scoring. Design rationale and work-order status: [`docs/design/feature-intelligence.md`](../q_frontend/docs/design/feature-intelligence.md).

* **Registry (`features/registry.py`)** — code-defined feature catalog synced to Postgres on API startup (`sync_registry_to_db`, idempotent; never downgrades a human-promoted status).
* **Compute (`features/compute.py`, `leakage.py`)** — named feature series on OHLCV bars with lookback trim and leakage checks.
* **Targets (`features/targets.py`)** — label definitions (`fwd_return`, `fwd_log_return`, `fwd_vol_adj_return`, `fwd_direction`) with horizon expansion via `list_target_specs`.
* **Matrix builder (`features/matrix.py`)** — builds aligned feature matrices, writes Parquet + provenance manifest to the lake.
* **Evaluation (`features/evaluation.py`, `evaluation_service.py`)** — IC, Rank IC, MI, stability, regime robustness; persists `EvaluationRun` + `FeatureScoreRow` rows.
* **Scoring (`features/scoring.py`)** — redundancy clustering, global feature score, recommended sets.
* **Persistence** — `FeatureDefinition` / `FeatureVersion` tables (WO130); evaluation tables (WO135). Repositories in `storage/db/repositories.py`.

Lifecycle statuses: `experimental` → `candidate` → `production` (per feature version).

### Exception policy (silent-failure policy)

The failure mode this codebase fears most is not a crash but a **silently wrong number** — a
swallowed exception in a research/execution path that degrades results without anyone knowing
(WO179). Every `except Exception` site must fit one of three sanctioned, deliberate patterns:

1. **must-surface** — research/execution correctness depends on it. Raise a typed error that
   propagates to the job/run record (failed status + message), never just a log.

   ```python
   raise ProbeDataError(...) from exc   # reaches the job layer; the run is recorded FAILED
   ```

2. **best-effort** — optional enrichment where absence is acceptable (news, health probes,
   Redis progress caches, version stamps). Always log; **never a bare `except: pass`**.

   ```python
   logger.warning("news enrichment failed for %s: %s", symbol, exc)  # logged, then degrade
   ```

3. **already-handled** — a state machine or structured result absorbs the failure (e.g. an
   UNKNOWN order in WO178, or a per-candidate `status="error"` that rolls up into the run's
   `failure_reasons`). Log the traceback in addition to the existing transition.

   ```python
   logger.exception(...)   # plus the existing transition / structured failure record
   ```

**Enforcement:** `ruff` rules `BLE001` (blind `except`) and `S110` (`try`/`except`/`pass`) are
enabled for `src/q_backend` in `pyproject.toml` (scoped to just those two rules to stay a policy
guard, not a lint-the-world sweep). BLE001 permits handlers that re-raise (pattern 1); every
sanctioned best-effort/already-handled catch carries a per-line `# noqa: BLE001` with a one-line
justification. Run it with:

```bash
uv run ruff check src
```

### Golden backtest regressions (backtest↔live parity lock-down)

The backtest engine's numbers are the foundation every other system (optimization, discovery,
research acceptance, paper trading) builds on. A subtle change to indicator warm-up, exit-evaluation
order, sizing, or cost handling would pass the rest of the suite while silently shifting every
result. The golden suite (`tests/backtesting/test_goldens.py`, WO180) pins current behavior so drift
becomes a **loud, reviewable diff** instead of silent corruption.

**What the goldens cover.** Each canonical case runs a strategy config on committed-deterministic
synthetic OHLCV (fixed seed, generated in-test — no data files, no network) and compares the
*complete* output — every trade's entry/exit time, price, direction, size, pnl, commission, plus the
summary metrics — field-for-field against `tests/backtesting/goldens/<case>.json`:

* `ma_crossover_baseline` — classic MA crossover, long+short, next-bar-open fill timing.
* `ma_crossover_fixed_stops` / `ma_crossover_atr_exits` — stop + target families (percent and ATR).
* `ma_crossover_trailing` / `ma_crossover_donchian_stop` — trailing family (percent and Donchian).
* `ma_crossover_time_stop` — time family (max bars in trade).
* `rsi_mean_reversion_baseline` — a second registered strategy category (mean reversion).
* `composite_or_macd_macrossover` / `composite_and_macd_macrossover` / `composite_majority_three` —
  multi-entry OR/AND/Majority composition via the signal manager.
* `genome_ma_session_gate` — a `CompositeStrategy` genome with a context feature
  (`feature.session_window` gating an MA crossover through `logic.and`).
* `ma_crossover_safety_margin_sizing` — a position-sizing variant (contracts derived from capital).
* `tick_ma_breakout` — the tick engine through `tick_backtest_runner` (pins its summary metrics).

The suite also asserts **determinism** (each case run twice in-process is byte-identical), a **tamper
guard** (mutating one trade field fails with a readable diff), and **backtest↔live parity**: every
golden candle strategy is driven bar-by-bar through the forward `StrategyEvaluator` and its queued
entry/exit signals must match the engine's section-D reference extractor at every bar close — with
and without an open position (the latter exercises the stateful exit rules).

**Regenerating goldens is a deliberate act.** Golden files are committed and reviewed like code. Only
regenerate when you *intend* to change engine behavior, and justify the resulting diff in the
commit/WO message:

```bash
uv run pytest tests/backtesting/test_goldens.py --regen-goldens
```

### Causality invariants (no-lookahead lock-down)

A lookahead bug is the most dangerous failure in the whole system: it produces beautiful
backtests and dead paper strategies. Two properties are enforced as **invariants** across
every computable feature source, so the coverage cannot silently rot as new sources are
added (WO181):

* **Feature specs are causal.** `tests/features/test_leakage.py` enumerates *every*
  `FeatureSpec` in the catalog and drives it through `assert_causal`: a value at bar `t`
  must be byte-identical whether computed on the full frame or on the `[:t+1]` prefix
  (removing future bars cannot change a present value).
* **Genome node kinds are causal.** `tests/backtesting/genome/test_node_causality.py`
  enumerates *all* `NODE_SPECS` kinds (indicators, transforms, comparators, logic gates,
  sources, `exit.middle_band`, context features) and asserts the same prefix-stability per
  output port — not just the `feature.*` family that was covered before.
* **Neural latents are OOS-only.** `tests/features/test_leakage_invariants.py` trains a
  model through the torch-free **PCA encoder path**, then runs `assert_causal` with the
  `train_end` semantics on each registered neural spec and on the `ind.latent` genome node.
* **Exogenous features respect publish time.** Exogenous specs are re-aligned on every
  prefix through the real backward as-of join, and a planted-spike test asserts a future
  observation is visible only *after* its bar has closed ("published-at", not
  "effective-at").
* **Targets are point-in-time.** `tests/features/test_targets.py` asserts each target at
  bar `t` depends only on bars up to its stated horizon (perturbing anything beyond
  `t + horizon` cannot move it) and that rows within `horizon` of `train_end` are purged
  from the training-eligible set, so labels never bleed across the split.

Each harness ships a **deliberately leaky fixture** (a `shift(-1)` feature/node/target)
proving the check actually fails when a leak is present.

**Exemptions live in exactly one place.** `tests/leakage_exemptions.py` holds
`EXEMPT_SPECS` and `EXEMPT_NODE_KINDS` — dicts mapping a name to the reason it is exempt.
This is the **only** place an exclusion may live; silent per-file filters are forbidden.
Hygiene meta-tests fail the suite if an exemption names something that no longer exists
(drift) or if any spec/node kind is neither tested nor exempted (a new source that slipped
through). To exempt something, add an entry with a real reason and get it reviewed; a
confirmed leak that cannot be fixed immediately is exempted with a `LEAK-CONFIRMED:` reason
and reported, never silently re-filtered. Current node exemptions: `ind.latent` (needs a
model — covered by the latent-node test), the `source.exog.*` family (pre-aligned columns —
covered by the exogenous invariant), and the `exit.fixed_holding` / `exit.opposite_signal`
/ `exit.rebalance` policy nodes (emit no data-derived series; pinned by the golden
backtests). There are currently no feature-spec exemptions.

### Discovery smoke suite (end-to-end pipeline health)

**What it proves.** `tests/integration_smoke/test_discovery_smoke.py` (WO182) exercises the *full*
production discovery path as one deterministic artifact — `start_job` → coordinator → per-candidate
genetic workers → generation barrier/breeding → finalizer → DB + lake persistence → results payload —
so cross-cutting wiring bugs (run records, the latents seam, cancellation, orphan reconciliation,
failure accounting) surface in one place. It covers: a run completes and persists a sane, non-empty
leaderboard with every genome valid and all metrics finite, and is **bit-identical across two runs
with the same seed**; a registered PRODUCTION PCA model seeds `ind.latent` genome nodes that survive
into the persisted population while `latents_enabled=False` (the WO153 seam) produces none; a
mid-generation cancellation ends the run `cancelled` with consistent partial results and no zombie
threads; every job family wired in `lifespan.py` moves an orphaned *running* job off `running` on
startup (parametrized over all seven `*_jobs` reconcilers); and injected per-candidate data-provider
failures are counted into the result summary's `failed_candidate_count` / `failure_reasons`.

**Determinism contract.** The production path evaluates each candidate in its own worker message and
merges via an order-independent fan-in (results are re-ordered by the stashed population, not arrival
order), so it is fully deterministic and does not exercise `genetic_parallel`'s process pool — that
lives only in the in-process orchestrator.

**How to run it.** `uv run pytest tests/integration_smoke/test_discovery_smoke.py` (~25s). It uses the
same in-process job harness (`run_jobs_sync`), in-memory SQLite, and `fakeredis` as the persistence
suites — no Postgres, Redis, MT5, or network — so it stays in the default suite.

* **Core Runtime:** Python `>=3.12`
* **API Framework:** FastAPI, Uvicorn (ASGI web server), CORS Middleware
* **Numerical Stack:** Pandas, NumPy (Vectorized market-data calculations)
* **Broker & Data Clients:** MetaTrader 5 (MT5 Python package); local Parquet market store for offline/Linux dev
* **Optimization & Analytics:** Optuna, Numba (tick kernel), PyArrow (Parquet I/O)
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
├── configs/              # Example optimization YAML configs
├── scripts/              # Standalone utility & validation scripts
│   └── backtests/        # High-performance backtesting runners
│       └── run_ccm_backtest.py
├── src/
│   └── q_backend/        # Core packages
│       ├── api/          # FastAPI app assembly, routers, schemas, job dispatch
│       │   ├── routers/  # Per-domain route modules (market, backtest, optimize, …)
│       │   ├── schemas/  # Request/response Pydantic models
│       │   └── *_jobs.py # Async job managers (backtest, optimize, walk-forward, …)
│       ├── backtesting/  # Candle/tick engines, genome DSL, strategies, sizers
│       ├── cli/          # CLI entry points (`worker`, `q-optimize`)
│       ├── features/     # Feature registry, compute, matrix, evaluation, scoring
│       ├── market_data/  # MT5/local providers, tick cache, local store
│       ├── optimization/ # Optuna runner, walk-forward, genetic search, discovery
│       ├── tasks/        # Dramatiq broker, actors, fan-in, worker context
│       └── storage/      # Settings, Postgres models, Redis helpers, Parquet lake
│           ├── settings.py
│           ├── lake/     # Backtest artifact read/write (Parquet)
│           ├── db/       # SQLAlchemy models, engine, repositories
│           └── redis/    # Job progress helpers
└── tests/
    ├── api/              # HTTP router & persistence integration tests
    ├── backtesting/
    ├── features/
    ├── optimization/
    ├── market_data/
    └── storage/          # Storage unit + integration tests
```

---

## ⚡ Quickstart & Setup

### Prerequisites
1. **Python 3.12+** and **`uv`** (see install below).
2. **Windows + MetaTrader 5** for live broker data, tick ingestion, and storage ingest jobs. On **Linux/macOS**, the API and worker boot with an MT5 stub; use `data_source=local` and the Parquet market store for offline backtesting (copy `data/market/` from a Windows ingest machine).
3. **MetaTrader 5 Terminal** (Windows only): Download from your broker or [MetaQuotes](https://www.metatrader5.com/).
4. **`uv` Package Manager:** Install `uv` if you haven't already:
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
Q_MARKET_DATA_ROOT=data/market
Q_TICK_CACHE_DIR=data/tick_cache
Q_RUNTIME_CONFIG_PATH=data/runtime_config.json
Q_MT5_GATEWAY_URL=http://127.0.0.1:18812
Q_MT5_GATEWAY_TOKEN=
Q_WORKER_PROCESSES=14
```

`Q_MT5_GATEWAY_URL` / `Q_MT5_GATEWAY_TOKEN` (WO184) point the `remote` data source at an MT5 gateway (WO183); both override the `remote_gateway_url` / `remote_gateway_token` runtime-config keys and are unset by default. To stand up that gateway under Wine on Linux (setup script + systemd user units + manual E2E checklist), see [`docs/mt5-wine-gateway.md`](docs/mt5-wine-gateway.md).

Optional paths above default under `data/` when unset. `Q_WORKER_PROCESSES` defaults to **28** in code (`Settings.worker_processes`); the example value `14` suits a 16-core machine.

#### AI Strategy Builder Settings

All providers whose credentials are present are registered and returned together from
`GET /api/v1/strategy-builder/models`. `Q_AI_STRATEGY_PROVIDER` names the **default**
provider used by `POST /interpret` when the request omits `provider`. Gemini curated
models are marked available without a remote probe; local Ollama models are probed via
`list_models()` as before.

| Setting | Default | Purpose |
|---------|---------|---------|
| `Q_AI_STRATEGY_ENABLED` | `false` | Enable the AI strategy builder |
| `Q_AI_STRATEGY_PROVIDER` | `openai_compatible` | Default provider (`openai_compatible`, `gemini`) |
| `Q_AI_STRATEGY_BASE_URL` | `http://localhost:11434/v1` | Base URL for OpenAI-compatible API |
| `Q_AI_STRATEGY_MODEL` | `qwen2.5-coder:14b` | Default model ID used for interpretation |
| `Q_AI_STRATEGY_MODELS` | empty | OpenAI-compatible curated list: model ID\|Label pairs |
| `Q_AI_STRATEGY_GEMINI_MODELS` | empty (code default) | Gemini curated list: model ID\|Label pairs |
| `Q_AI_STRATEGY_API_KEY` | empty | API key for OpenAI-compatible provider |
| `Q_AI_STRATEGY_GEMINI_API_KEY` | empty | API key for Gemini provider |
| `Q_AI_STRATEGY_GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` | Base URL for Gemini API |
| `Q_AI_STRATEGY_TIMEOUT_SECONDS` | `60` | Request timeout in seconds |
| `Q_AI_STRATEGY_MAX_OUTPUT_TOKENS` | `4096` | Maximum output tokens from the model |

Worked `.env` example for Gemini-only:
```ini
Q_AI_STRATEGY_ENABLED=true
Q_AI_STRATEGY_PROVIDER=gemini
Q_AI_STRATEGY_MODEL=gemini-2.5-flash
Q_AI_STRATEGY_GEMINI_MODELS=gemini-2.5-flash|Gemini 2.5 Flash,gemini-2.5-pro|Gemini 2.5 Pro
Q_AI_STRATEGY_GEMINI_API_KEY="AIzaSyYourActualKeyHere"
```

Both providers (local + Gemini) can be configured in one `.env`; set
`Q_AI_STRATEGY_PROVIDER` to whichever should be the default for `/interpret`.

### 2. Install Dependencies
```bash
uv sync --group dev
```

### 3. Start Storage Services (local dev)
Start Postgres and Redis:
```bash
docker compose up -d
```
For a fully containerized API + worker stack (no live MT5), use `docker compose --profile containerized up --build`.
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

### API layout

`q_backend.api.main` is the assembly module: it creates the FastAPI app, registers
CORS middleware, wires the lifespan, and `include_router`s per-domain routers from
`q_backend.api.routers/`. Request/response models live in `q_backend.api.schemas/`.
Shared providers (the app-wide `market_data_service` singleton, database session
dependency, and cross-domain MT5 helpers) live in `q_backend.api.dependencies`.
Domain logic stays in the existing packages (`market_data/`, `backtesting/`,
`optimization/`, etc.) or in `api/services/` when API-specific.

| Domain | Router | Schemas | Service / jobs |
|--------|--------|---------|----------------|
| System | `routers/system.py` | `schemas/system.py` | `market_data/`, `storage/health` |
| Strategies | `routers/strategies.py` | (registry models) | `backtesting/strategy_registry.py` |
| Market | `routers/market.py` | `schemas/market.py` | `market_data/api_service.py` |
| Backtest | `routers/backtest.py` | `schemas/backtest.py` | `backtesting/run_service.py`, `api/backtest_jobs.py` |
| Optimization | `routers/optimization.py` | `schemas/optimization.py` | `api/optimization_jobs.py` |
| Walk-forward | `routers/walkforward.py` | `schemas/walkforward.py` | `api/walkforward_jobs.py` |
| Strategy search | `routers/strategy_search.py` | `schemas/strategy_search.py` | `api/strategy_search_jobs.py` |
| Features | `routers/features.py` | `schemas/features.py` | `features/evaluation_service.py`, `features/sync.py` |
| Storage | `routers/storage.py` | `schemas/storage.py` | `api/storage_jobs.py` |
| News | `routers/news.py` | `schemas/news.py` | `api/services/news.py` (injectable RSS fetch) |

After WO60, `main.py` is assembly-only: it wires routers and CORS and defines no business routes directly. Conventions for new routers are documented in `api/routers/__init__.py`.

For full-stack local development with the Quant desktop app, run the worker pool as well (step 6). Without it, async jobs stay queued and never complete.

### 6. Running the Worker Pool

Heavy jobs — backtests, optimizations, walk-forward analyses, and strategy
discovery — no longer execute inside the API process. They are dispatched to a
**Dramatiq worker pool** (backed by Redis) that runs as a **separate process**. Start
it alongside the API:

```bash
uv run worker
```

The worker uses the same optional `Q_SENTRY_DSN` configuration as the API. When
set, final actor failures are reported with safe actor and trading-ID tags; when
unset, worker telemetry remains dormant.

This launches `dramatiq q_backend.tasks --processes $Q_WORKER_PROCESSES --threads 1`.
The pool is the single CPU budget shared by every job: a job fans its work out into
many small messages (per Optuna trial batch, per walk-forward window, per discovery
candidate, per backtest) that drain into the fixed pool, so running several jobs at
once shares the cores fairly instead of oversubscribing the machine. Size it with
`Q_WORKER_PROCESSES` in `.env` (code default **28**; tune down on smaller machines — `.env.example` shows `14` for a 16-core box).

> Each worker process opens its own MetaTrader 5 connection on boot. The API and the
> worker must both be running for jobs to make progress — the API enqueues and serves
> status; the worker executes. Orphaned runs (worker killed mid-job) are reconciled to
> `cancelled` on the next API startup.

### 7. Forward execution worker (`q-execution`)

Paper/live forward execution runs in a **standalone process** — not inside the API lifespan and not in the Dramatiq pool. It is the only component authorized to poll completed bars, run the risk gate, commit order intents, and submit paper fills.

```bash
uv run q-execution run
```

Flatten one deployment explicitly (bypasses strategy signals, not lease or persistence safety):

```bash
uv run q-execution flatten <deployment-uuid>
```

| Setting | Default | Purpose |
|---------|---------|---------|
| `Q_EXECUTION_WORKER_ID` | `execution-worker-1` | Lease owner identity |
| `Q_EXECUTION_LEASE_TTL_SECONDS` | `30` | Deployment lease TTL / heartbeat |
| `Q_EXECUTION_POLL_INTERVAL_SECONDS` | `1.0` | Bar poll cadence |
| `Q_EXECUTION_MAX_QUOTE_AGE_SECONDS` | `30` | Executable quote freshness |
| `Q_EXECUTION_MAX_BAR_AGE_SECONDS` | `7200` | Completed-bar freshness |
| `Q_EXECUTION_BENCHMARK_P95_BUDGET_MS` | `500` | Closed-bar → fill p95 budget |

**Lifecycle commands** (via API in WO171; semantics today):

| Command | Effect |
|---------|--------|
| **Start** | `running` — evaluate new bars and submit orders |
| **Pause** | Stop evaluating new bars; retain open position |
| **Stop** | Terminate evaluation; retain position unless flatten requested |
| **Flatten** | Market close at current executable quote |
| **Kill switch** | Block all new entries; flatten still allowed |

**Measured phase timings** (synthetic local benchmark, M15/H1 MACrossover fixtures):

| Phase | CCM$ H1 p50 | WIN$ H1 p50 | WDO$ M15 p50 |
|-------|-------------|-------------|--------------|
| Indicators | ~2 ms | ~2 ms | ~3 ms |
| Evaluate | ~0.5 ms | ~0.5 ms | ~0.5 ms |
| Full path (eval + risk + broker + persist) | <50 ms p95 | <50 ms p95 | <50 ms p95 |

Run benchmarks: `uv run pytest tests/execution/test_evaluator_benchmark.py tests/execution/test_worker.py -k benchmark -s`

**Execution API contracts (WO171 → WO173/WO174):** control-plane routes under `/api/v1/execution/*` never submit broker orders. Key bodies:

- `POST /api/v1/execution/accounts` — `{"name":"desk-main","initial_balance":"100000.00","currency":"BRL"}`
- `POST /api/v1/execution/deployments` — paper account id + immutable `identity` (or `source_backtest_run_id` from a saved run)
- `POST /api/v1/execution/deployments/{id}/actions` — `{"action":"start|pause|stop|flatten","confirm":true}`
- `GET /api/v1/execution/health` — separate `api_status`, `worker_status`, `market_data_status`, `live_capability_locked`
- `PUT /api/v1/execution/kill-switch` — `{"enabled":true,"confirm":true,"reason":"…","updated_by":"operator"}`
- `GET /api/v1/execution/deployments/{id}/chart?bars=200` (WO175) — read-only live chart payload: the same bounded OHLCV window the forward evaluator consumes plus the strategy's own indicator series, computed through the identical `execution.indicator_frame.augment_indicator_frame` path the worker uses (never a re-implementation). `bars` is the display count (default 200, max 1000); the endpoint fetches `bars + compute_window_bound_bars(compiled_config)` completed bars (forming bar excluded), computes indicators, then trims to the last `bars` so warm-up NaNs never reach the display window. Read-only (no worker/evaluator state, no persistence). Payload is cached in-process on `(deployment_id, bars, last completed bar open time)` so 5-second polling recomputes only when a new bar lands. Degrades honestly: unknown deployment → 404; market data unavailable (MT5 offline in `mt5` mode, empty local store in `local` mode) → 503; strategy window that cannot be bounded → 422.

Money/price/quantity fields serialize as decimal strings in JSON responses.

**Deployment chart JSON contract (WO175):** shape mirrors the backtest chart (`bars`, `indicators`) plus additive metadata (`symbol`, `timeframe`, `window_bound_bars`, `last_bar_close_time`, `next_bar_close_time`). Warm-up NaNs serialize as `null`. Example (`bars` and `values` truncated for brevity; two indicators on `price` plus one on `oscillator`):

```json
{
  "symbol": "WIN$",
  "timeframe": "H1",
  "window_bound_bars": 65,
  "last_bar_close_time": "2023-01-06T10:00:00Z",
  "next_bar_close_time": "2023-01-06T11:00:00Z",
  "bars": [
    {"timestamp": "2023-01-06T08:00:00Z", "open": 75.99013025404263, "high": 76.2713901564827, "low": 75.60697727845468, "close": 76.01412987482365, "volume": 2526},
    {"timestamp": "2023-01-06T09:00:00Z", "open": 76.79759927190695, "high": 76.86363402451663, "low": 76.73837731260693, "close": 76.77086837748791, "volume": 3051}
  ],
  "indicators": [
    {"key": "ma_short", "label": "SMA Short (5)", "pane": "price", "color": "#c9a227", "values": [79.25087982945149, 78.44122533253503, 77.6819007991252, 77.30020546035281]},
    {"key": "ma_long", "label": "SMA Long (20)", "pane": "price", "color": "#6eb5ff", "values": [81.78716766001001, 81.51747117702958, 81.16929017357572, 80.86924261176772]},
    {"key": "delta", "label": "Delta", "pane": "oscillator", "color": "#c9a227", "values": [-2.5362878305585213, -3.076245844494551, -3.4873893744505153, -3.5690371514149035]}
  ]
}
```

### 8. MT5 live broker (`live_locked`)

The MT5 live adapter is **implemented, locked, and operationally unvalidated**. It translates
broker-neutral market orders into auditable MT5 requests and reconciles fills through deal history,
but cannot submit in the current environment.

| Gate | Setting | Default |
|------|---------|---------|
| Global enable | `Q_LIVE_EXECUTION_ENABLED` | `false` |
| Account allowlist | `Q_LIVE_EXECUTION_ACCOUNT_ALLOWLIST` | empty |
| Deployment flag | `live_activation_enabled` on deployment row | `false` |
| Controlled-account validation | `Q_LIVE_EXECUTION_VALIDATED` | `false` |
| Dry run (blocks `order_send` even when gates pass) | `Q_LIVE_EXECUTION_DRY_RUN` | `true` |

API/UI capability remains **`live_locked`** until a separate controlled-account validation record
exists (`Q_LIVE_EXECUTION_VALIDATED=true` plus allowlisted account).

**Retcode mapping (summary):**

| MT5 retcode family | Domain outcome |
|--------------------|----------------|
| `DONE` / `DONE_PARTIAL` | Reconcile via `history_deals_get`; fill only when deal ticket is found |
| `REQUOTE`, `REJECT`, `INVALID_VOLUME`, `MARKET_CLOSED` | `rejected` with `order_send_failed` / `order_check_failed` |
| `TIMEOUT`, `NO_CONNECTION`, `None` response | `unknown`; search deals/orders by magic+comment — **never resend** |

**Reconciliation procedure:**

1. Run `order_check()` before any `order_send()`.
2. Persist normalized request (passwords stripped) and raw retcode/order/deal tickets.
3. On ambiguous outcomes, call `recover_unknown()` which queries `orders_get()`, `history_orders_get()`,
   and `history_deals_get()` using Q intent magic/comment.
4. Deduplicate external deal tickets before ledger application.

Run contract tests: `uv run pytest tests/execution/test_metatrader_broker.py`

**Future controlled-account validation checklist (not complete):**

- [ ] Dedicated demo/live account provisioned and allowlisted
- [ ] Manual micro-order round trip on controlled account
- [ ] Reconciliation verified against terminal deal history
- [ ] Set `Q_LIVE_EXECUTION_VALIDATED=true` only after sign-off
- [ ] Set `Q_LIVE_EXECUTION_DRY_RUN=false` only on the execution worker host

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
  * *Response:* `{"status": "online", "service": "QuantLauncher Backend API", "mt5_connected": <bool>}`
* **`GET /api/v1/system/health`**
  * *Description:* Advanced system diagnostic dashboard info. Includes database (PostgreSQL) and cache/jobs (Redis) storage statuses, active market-data provider, and local store inventory.
  * *Response:* `{"status": "healthy"|"degraded", "backendVersion": "0.1.0", "dataLakeStatus": "online"|"offline", "lastSyncAt": "<ISO8601>", "storageStatus": {"postgres": {"status": "ok"}, "redis": {"status": "ok"}}, "mt5_available": <bool>, "active_provider": "mt5"|"local", "market_data_root": "<path>", "market_data_inventory_count": <int>}`
* **`GET /api/v1/system/data-source`**
  * *Description:* Read the persisted market-data routing setting (`auto` | `mt5` | `local`) plus live provider status.
* **`PUT /api/v1/system/data-source`**
  * *Description:* Update the market-data routing setting. Body: `{"source": "auto"|"mt5"|"local"}`.

### B3 Asset Directory & Realtime
* **`GET /api/v1/market/instruments`**
  * *Description:* Default B3 instrument master definitions (`PETR4`, `VALE3`, `ITUB4`, `WIN$`, `WDO$`) plus symbols present in the local Parquet store.
* **`GET /api/v1/market/symbols/search`**
  * *Description:* Search stored local market data and, when MT5 is available, the terminal symbol list.
  * *Parameters:* `q` (required query string).
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
  * *Description:* Historical OHLCV bars for charting from the active provider (local store or MT5).
  * *Parameters:* `timeframe` (default `D1`), `count` (default 500, max 5000), optional `start`/`end` (ISO-8601 range; both required when used).
* **`GET /api/v1/market/ohlcv/{symbol}/available-range`**
  * *Description:* Earliest and latest bar timestamps available for a symbol/timeframe, plus `bar_count`.

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
  * *Built-in strategies:* `MACrossover`, `RSIMeanReversion`, `BollingerReversion`, `MACD`, `DonchianBreakout`, `VMA`, `FMA`, `TRB`, `TSMOM`, `GatevPairs`, `HurstTrendBlend` (candle); `TickMaBreakout` (tick); `CompositeStrategy` (genome interpreter for genetic search).
* **`GET /api/v1/exit-rules`**
  * *Description:* Structured exit-rule catalog for the Strategy workbench toggle cards and presets. Additive to `/strategies`; the flat per-strategy `params` list is unchanged.
  * *Response:* `{"exit_rules": [{"id": "chandelier", "label": "Chandelier Exit", "exit_group": "trailing", "description": "...", "enable_param": "chandelier_atr_mult", "enable_value": 3.0, "param_names": ["chandelier_atr_mult"], "required_param_names": ["atr_period"]}, ...], "shared_exit_params": ["atr_period"], "exit_presets": [{"id": "atr_stop_chandelier", "label": "...", "description": "...", "parameters": {...}}, ...]}`
  * *`enable_value`:* Recommended value applied when an exit is toggled on in the Strategy workbench (aligned with preset values). Additive metadata only — backtest behavior is unchanged.
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
* **`DELETE /api/v1/optimizations/{study_id}`**
  * *Description:* Delete a persisted optimization study (`204`).
* **`POST /api/v1/optimizations/bulk-delete`**
  * *Description:* Delete multiple optimization studies by id list.

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
  * *Request body:* WO31 `StrategySearchConfig` JSON (`backtest`, `objective`, `walkforward`, `study`, optional `strategies`, `include_risk_search`, `gates`, optional `exit_presets`).
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

### Feature Store & evaluation (Research frontend)
* **`GET /api/v1/features`**
  * *Description:* Feature Store catalog list for the Research workspace.
  * *Parameters:* optional `category`, `status` (`experimental` | `candidate` | `production`).
  * *Response:* `{"features": [{"name", "category", "latest_version", "status", "usage_count", "score"}, ...]}`
* **`GET /api/v1/features/{name}`**
  * *Description:* Feature Passport — versions, provenance, evaluation history, and latest `global_score`.
* **`POST /api/v1/features/{name}/{version}/status`**
  * *Description:* Promote or demote a feature version's lifecycle status.
  * *Request body:* `{"status": "experimental"|"candidate"|"production"}`
  * *Response:* Updated Feature Passport.
* **`GET /api/v1/features/leaderboard`**
  * *Description:* Latest `global_score` per feature across evaluation runs.
  * *Response:* `{"features": [{"feature_name", "global_score"}, ...]}`
* **`POST /api/v1/feature-eval`**
  * *Description:* Run feature matrix build → evaluate → score → persist (synchronous in the API process).
  * *Request body:* `symbol`, `timeframe`, `start`, `end`, `target` (`name`, `horizon`), `features` (`name`, optional `version`, optional `params`).
  * *Response:* `{"run_id": "<uuid>", "status": "completed"|"failed"|...}`
* **`GET /api/v1/feature-eval/{run_id}`**
  * *Description:* Evaluation run status plus `leaderboard`, `clusters`, and `heatmap` payloads.

See [`docs/design/feature-intelligence.md`](../q_frontend/docs/design/feature-intelligence.md) for PIT/leakage contracts, target definitions, and scoring weights.

### Local market storage
* **`GET /api/v1/storage/inventory`**
  * *Description:* List OHLCV/tick series in the local Parquet store with row counts and byte sizes.
* **`POST /api/v1/storage/ingest`**
  * *Description:* Queue MT5 → local ingest for `kind: "bars" | "ticks"` (503 when MT5 is unavailable).
* **`GET /api/v1/storage/ingest/{job_id}`**
  * *Description:* Poll ingest job status and per-series results.
* **`DELETE /api/v1/storage/{symbol}/{timeframe}`**
  * *Description:* Delete a stored OHLCV series (`204`).

### News (RSS)
* **`GET /api/v1/news`**
  * *Description:* Latest finance headlines aggregated from Valor Econômico and CNBC RSS feeds (top 25, deduplicated).
* **`GET /api/v1/news/{article_id}`**
  * *Description:* Full article body for one feed item. `article_id` is a URL-safe base64 encoding of the source link.

---

## 🔒 Security & Developer Notes
* **Local In-Memory Execution:** No trading keys or credentials are sent outside your local machine.
* **CORS Policy:** Programmed with open CORS middleware (`allow_origins=["*"]`) for easy interaction with the React/Tauri frontend during local development. Make sure to lock down origins when deploying outside a local staging environment.

## ✅ Validation and Git Hooks

`./scripts/ci.sh` runs the full pipeline — vendored contract drift, migrations,
lint, format, tests — and is exactly what CI runs. The contract stage reaches
the `q_contracts` repository; when working offline, point it at a local clone:

```bash
CONTRACTS_REPO=/path/to/q_contracts ./scripts/ci.sh
```

Git hooks live in `.githooks/` and are inactive in a fresh clone until the hook
path is configured. Enable them once per checkout:

```bash
make hooks
```

`pre-commit` runs `ruff` and `black`; `pre-push` runs `scripts/ci.sh`.
