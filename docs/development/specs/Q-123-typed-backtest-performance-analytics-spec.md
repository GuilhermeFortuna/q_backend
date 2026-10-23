# Q-123: Typed backtest performance analytics

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 30 — Backtest performance analytics
**Depends on:** —
**Implementation plan:** [Plan](../plans/Q-123-typed-backtest-performance-analytics-plan.md)

## Purpose

The Backtests Performance tab shows four numbers and two charts. Two gaps make the backend's metrics hard to use:

- Every backtest result carries an untyped `metrics` dict from `TradeRegistry.get_performance_metrics` (`backtesting/registry.py`). The Research UI shows only four of its 16 keys.
- Risk-adjusted, duration and side-split measures are not computed anywhere in the backtest path. Sharpe exists only inside optimization (`optimization/metrics.py`).

The contract types `metrics` and `trades` as free-form objects, as recorded in `q_contracts/schema/api/FINDINGS.md`.

After this task, every backtest result carries a typed `performance` object computed by one backend function. It is the single source for the Performance tab (Q-124, Q-125). Stored runs get it without a migration.

## Behaviour

### Basis

- All measures use **closed trades only**. Equity is the closed-trade equity curve from `build_equity_curve`, starting at the run's `initial_capital` and spanning the run's start and end. There is no mark-to-market. This matches the Q-091 rule that research results do not present open-trade returns.
- Return series: the daily closed-trade equity, from resampling to calendar days and forward-filling, as `compute_extended_metrics` does today. Ratios are annualized with 252 periods, and the risk-free rate is 0.
- The response states this basis in `performance.basis` (`equity: "closed_trade"`, `periods_per_year: 252`, `risk_free_rate: 0`), so the UI can show it.

### Contents

`performance.portfolio` holds equity-curve measures:

| Field | Definition |
| --- | --- |
| `net_pnl`, `gross_profit`, `gross_loss`, `total_commission` | Sums over closed trades. `gross_loss` ≤ 0. |
| `return_pct` | `net_pnl / initial_capital`, as a fraction. |
| `cagr_pct` | `(final / initial) ^ (365.25 / span_days) − 1`. Null when the span is under one day or the final equity is ≤ 0. |
| `max_drawdown_value`, `max_drawdown_pct` | Equal to the legacy `metrics` values. |
| `max_drawdown_duration_days` | The longest peak-to-recovery span in calendar days. If the drawdown is unrecovered, it runs from the peak to the run end. |
| `avg_drawdown_pct` | Mean depth over all drawdown periods. |
| `recovery_factor` | `net_pnl / max_drawdown_value`. Null when there is no drawdown. |
| `calmar_ratio` | `cagr_pct / max_drawdown_pct`. Null when either is null or zero. |
| `sharpe_ratio`, `sortino_ratio` | From daily returns. Sortino uses downside deviation over all days. Null when there are fewer than two returns or the deviation is zero. |
| `annual_volatility_pct` | Daily return std × √252. |
| `time_in_market_pct` | The union of the trades' entry→exit intervals, divided by the run span. |
| `trades_per_month` | Closed trades per 30.4375 days of span. |

`performance.trades` has `all`, `long` and `short`, each a `TradeStats`. Long trades are `BUY` entries and short trades are `SELL` entries.

| Field | Definition |
| --- | --- |
| `total`, `winning`, `losing`, `breakeven` | `winning` is `pnl > 0`. `losing` keeps the legacy rule, `pnl ≤ 0`. `breakeven` (`pnl == 0`) is a subset of `losing` and is reported separately. |
| `win_rate` | `winning / total`, the same as the legacy `metrics.win_rate`. |
| `profit_factor` | `gross_profit / |gross_loss|`. Null when there are no losing P&L. |
| `expectancy` | Mean `pnl`. |
| `avg_win`, `avg_loss`, `payoff_ratio` | `payoff_ratio = avg_win / |avg_loss|`. Null without both sides. |
| `largest_win`, `largest_loss` | The extreme `pnl` values. |
| `max_consecutive_wins`, `max_consecutive_losses` | Streaks in exit order, with the legacy win and loss rule. |
| `avg_hold_seconds`, `avg_win_hold_seconds`, `avg_loss_hold_seconds` | Mean `exit_time − entry_time`. |

When a group has no trades, its counts are 0 and every other field is null.

`performance.drawdowns` lists up to the **10 deepest** drawdown periods of the closed-trade equity, deepest first. Each entry has:

- `peak_time`, `trough_time`, and `recovery_time` (null if not recovered by the run end);
- `depth_value` and `depth_pct`;
- `duration_days`;
- `trades`, the closed trades from the peak to the recovery or the run end.

`performance.exit_reasons` lists `{reason, trades, pnl, win_rate}` per `exit_reason`, ordered by trade count. A trade with no `exit_reason` falls under `"unspecified"`.

### Where it appears

- `BacktestResponse.performance` is optional in the contract. It is computed for every new candle and tick job and written into `result.json`.
- `GET /api/v1/backtest/{run_id}/result` returns it. When the stored result has no `performance`, as with runs stored before this task and imported script runs, the endpoint computes it on read. It uses the stored trades and the run row's `config` (`initial_capital`, `start`, `end`).
  - When the config lacks `initial_capital`, `performance` is null.
  - When start or end are missing, the first entry and the last exit are used.
  - Nothing is written back.
- `metrics`, `result_summary`, history sorting, imports, CSV and the PDF are unchanged.

## Interfaces

- `q_backend.backtesting.analytics.compute_backtest_performance(closed_trades, initial_capital, start, end) -> BacktestPerformance`.
  - The Pydantic models (`BacktestPerformance`, `PerformanceBasis`, `PortfolioStats`, `TradeStats`, `SideTradeStats`, `DrawdownPeriod`, `ExitReasonStats`) live in `api/schemas/backtest.py`.
  - It reuses `build_equity_curve`. The daily-returns Sharpe helper moves out of `optimization/metrics.py` into the analytics module, and optimization imports it from there, so optimization results do not change.
- `q_contracts`: the same schemas are added under `components/schemas`, plus an optional, nullable `performance` property on `BacktestResponse`. The generators are re-run, and the FINDINGS entry for untyped backtest payloads is updated to say that performance is typed while `metrics` and `trades` remain free-form.
- Consumers pin the new `CONTRACTS_REV`:
  - q_backend;
  - q_frontend, where the change is limited to re-vendoring and an optional `performance` field on `BacktestResponse` in `src/types/backtesting.ts`.
- The user guide `docs/research/user_guide/backtesting.rst` gains a metric definitions table with the basis.

## Focused acceptance

1. `compute_backtest_performance` matches hand-computed values on these fixtures:
   - no trades;
   - all winners (`profit_factor` null);
   - a long and short mix (side split);
   - an unrecovered drawdown (`recovery_time` null; duration runs to the end);
   - a known daily equity path (Sharpe, Sortino, CAGR, Calmar);
   - overlapping trades (time in market counts the union).
2. `max_drawdown_*`, `win_rate`, `expectancy` and the streaks equal the legacy `metrics` values on the same trades.
3. Candle and tick job results include `performance`, and `result.json` round-trips it through `BacktestResponse`.
4. A stored result without `performance` gets it on read from the run config. A run whose config has no `initial_capital` returns `performance: null` with status 200.
5. Optimization `compute_extended_metrics` returns the same values as before.
6. `make contracts-check` passes in q_backend and q_frontend. `make check` passes in q_contracts.
7. The backend lint and the focused pytest modules pass.

Tests use synthetic trades, the API test client and a temporary lake root. No worker, Redis, gateway, Docker, GPU or desktop run is needed.

## Delivery boundary

- No mark-to-market or intrabar equity, MAE/MFE, R-multiples, benchmark comparison or configurable risk-free rate.
- No change to `metrics`, `result_summary`, history sorting, script `BacktestResult`, the PDF or CSV exports.
- No backfill job and no write-back of recomputed results.
- No UI beyond the type addition; Q-124 and Q-125 own it.
