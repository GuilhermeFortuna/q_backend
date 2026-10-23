# Batch 30 — Backtest performance analytics

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend (typed analytics and contract) and q_frontend (Performance tab).

## Outcome

The Backtests **Simulation › Results › Performance** tab becomes a complete strategy summary, built from one backend source of truth:
- grouped return, risk and trade-quality metrics, with an All / Long / Short split;
- linked equity and underwater charts, and a worst-drawdowns table;
- rolling metrics;
- trade distribution by P&L, holding time, exit reason, weekday and hour.

## Findings this batch answers

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | Twelve backend metrics are computed but not shown in the UI, though the PDF prints several of them. | `TradeRegistry.get_performance_metrics` (`backtesting/registry.py`); `BacktestMetricsBar` shows 4 keys; `src-tauri/src/report.rs` | Q-123, Q-124 |
| 2 | No risk-adjusted, duration or side-split measures exist for backtests. Sharpe exists only in optimization. | `optimization/metrics.py`; `backtest_jobs.py` stores the registry metrics only | Q-123 |
| 3 | Backtest `metrics` and `trades` are untyped in the contract. | `q_contracts/schema/api/FINDINGS.md`, "untyped backtest payloads" | Q-123 |
| 4 | The equity tooltip prints drawdown 100× too small. | `q_frontend/src/components/backtests/EquityCurveChart.tsx` renders the `drawdownPct` fraction as a percent | Q-124 |
| 5 | Nothing shows edge decay, outlier dependence or time-of-day effects. | The Performance tab has only equity and drawdown | Q-125 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-123 — Typed backtest performance analytics | q_backend (with q_contracts, q_frontend vendoring) | — | [Spec](../specs/Q-123-typed-backtest-performance-analytics-spec.md) | [Plan](../plans/Q-123-typed-backtest-performance-analytics-plan.md) |
| Q-124 — Performance tab metrics panel and drawdown analysis | q_frontend | Q-123 | `q_frontend/docs/development/specs/Q-124-performance-tab-metrics-panel-and-drawdown-analysis-spec.md` | `q_frontend/docs/development/plans/Q-124-performance-tab-metrics-panel-and-drawdown-analysis-plan.md` |
| Q-125 — Performance tab rolling metrics and trade distribution | q_frontend | Q-124 | `q_frontend/docs/development/specs/Q-125-performance-tab-rolling-metrics-and-trade-distribution-spec.md` | `q_frontend/docs/development/plans/Q-125-performance-tab-rolling-metrics-and-trade-distribution-plan.md` |

The tasks run in sequence. Q-123 pins the new contract in both consumers, so the frontend tasks start with the `performance` type available.

## Shared decisions

- **The backend owns the metrics.** Every reported number comes from `compute_backtest_performance`. The frontend only derives display series (rolling windows, histogram bins, time buckets) from `trades`.
- **Closed-trade basis, stated.** Ratios use daily closed-trade equity, 252 periods a year and a risk-free rate of 0. The UI shows this basis.
- **Existing numbers do not change.** `metrics`, `result_summary`, the KPI tiles, the PDF, CSV exports and optimization metrics keep their values and definitions.
- **No migration.** Stored and imported runs get `performance` computed on read. Nothing is written back.
- **Null, not sentinel.** A ratio with a zero denominator is null, and the UI renders it as "—" or "∞".
- **Reference-driven UI.** Layouts follow named references (the TradingView Strategy Tester, QuantStats and the MT5 report) in the Q visual system.

## Recorded, not in this batch

- Mark-to-market and intrabar equity, MAE/MFE, R-multiples.
- Benchmark comparison and a configurable risk-free rate.
- Monte Carlo and bootstrap analysis.
- A year × month return heatmap on the Monthly tab.
- New metrics in the PDF report and the comparison view.
- History sorting by new metrics.

## Review and integration

Documentation is written on local `docs/batch-30-backtest-performance-analytics` branches, in isolated worktrees under `.worktrees/<repository>/docs-batch-30-backtest-performance-analytics`. The authoring session pushes or merges no branch.

The human integrates these branches before launching implementation tasks, and owns the initial board Status and the move to Todo. From the workspace root, launch an approved task whose dependencies are Done with `./work start Q-NNN --agent <agent> --worktree`.
