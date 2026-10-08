# Batch 17 — Research script runs in the Research stack

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with a contract task in q_contracts and a desktop task in q_frontend.

## Outcome

A backtest run from a research script with `q_backend.research.backtest()` is published to the Research stack with `result.publish()`. It appears in Backtests history and opens into the same Performance, Monthly, Trade Chart and Trade List views as a run the stack executed.

## Findings this batch answers

Each was read from the code on 2026-10-08.

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | A script backtest ends at printed metrics. | `experiments/ccm_test.py` runs `SmartMaCrossover` over `CCM$` H1 from 2020 and prints `result.metrics`; the library has no way to show a chart, a trade list or a monthly breakdown. | Q-100 |
| 2 | Only a job can create a run. | `run_backtest_job` is the sole writer of `backtest_runs` rows and of the lake files `result.json`, `trades.parquet`, `equity.parquet` and `market_data.parquet`. The control API has no route that accepts a finished result. | Q-097, Q-099 |
| 3 | A custom research strategy cannot declare chart series. | `ResearchStrategyAdapter.get_chart_indicators()` returns an empty list, so the Trade Chart would show candles and markers only. | Q-098 |
| 4 | History cannot open a stored result. | `BacktestHistoryPanel` warns that "Trade charts and indicator series are not persisted" and offers only Re-run, although `GET /api/v1/backtest/{run_id}/result` serves the stored result of every completed run. Re-run is impossible for a strategy the stack does not hold. | Q-101 |
| 5 | The results views need nothing a script cannot supply. | `BacktestResultsTabs` renders from `BacktestResponse` (`metrics`, `trades`, `bars`, `indicators`); the equity curve and monthly statistics are computed in the desktop from the trades and the initial capital. | Q-099, Q-100, Q-101 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-097 — Imported backtest runs in the control API | q_contracts | none | `q_contracts/docs/development/specs/Q-097-imported-backtest-runs-in-the-control-api-spec.md` | `q_contracts/docs/development/plans/Q-097-imported-backtest-runs-in-the-control-api-plan.md` |
| Q-098 — Chart indicator declarations for research strategies | q_backend | none | [Spec](../specs/Q-098-chart-indicator-declarations-for-research-strategies-spec.md) | [Plan](../plans/Q-098-chart-indicator-declarations-for-research-strategies-plan.md) |
| Q-099 — Backtest run import endpoint | q_backend | Q-097 | [Spec](../specs/Q-099-backtest-run-import-endpoint-spec.md) | [Plan](../plans/Q-099-backtest-run-import-endpoint-plan.md) |
| Q-100 — Publish research backtests to the Research stack | q_backend | Q-098, Q-099 | [Spec](../specs/Q-100-publish-research-backtests-to-the-research-stack-spec.md) | [Plan](../plans/Q-100-publish-research-backtests-to-the-research-stack-plan.md) |
| Q-101 — Stored and script runs open from Backtests history | q_frontend | Q-097, Q-099 | `q_frontend/docs/development/specs/Q-101-stored-and-script-runs-open-from-backtests-history-spec.md` | `q_frontend/docs/development/plans/Q-101-stored-and-script-runs-open-from-backtests-history-plan.md` |

Implement Q-097, then Q-099, then Q-100. Q-098 has no prerequisite and can run at any point before Q-100. Q-101 can start once Q-099 is Done and runs alongside Q-100.

Q-098 and Q-100 both change `research/results.py`, `research/engine.py` and `docs/research-library.md`; serializing them keeps each branch free of overlapping edits. Q-096, still in progress in Batch 16, also edits `docs/research-library.md`, so Q-098 should start after it is Done.

## Shared decisions

- **Publish results; do not upload strategies.** The script runs the backtest and sends the finished result. The stack stores and shows it, and never executes script code.
- **Review-only runs.** A script run cannot be re-run, optimised, walk-forwarded or used as an ML filter source, because the stack does not hold its strategy or its data. Its origin is a column on the run, and every flow that starts a job from a past run checks it.
- **Same files, same views.** An imported run writes the row and the four lake files a job writes, so every existing read endpoint and every results view serves it unchanged. Nothing is rendered by the library.
- **Over the control API.** The library posts to the API; it does not write to PostgreSQL or the lake. Only `publish()` needs the stack; `backtest()` keeps working with nothing running.
- **Every publish is a new run.** There is no deduplication and no idempotency key. Runs are removed from history.
- **Declared chart series only.** A strategy names the columns to draw and their panes. No pane is inferred from values.
- **Provenance travels with the run.** Script path, strategy class and source, parameters and git revision are stored so a reviewer knows what produced the numbers.
- **Focused verification.** Synthetic frames, mocked HTTP, the API test client and MSW. No gateway, worker, Docker, GPU or desktop run for automated checks. One manual step closes the batch: publish from `experiments/ccm_test.py` against `./dev research` and open the run from history.

## Recorded, not in this batch

- **Uploading strategies to the stack.** The deliberate follow-up to this batch: the stack would store a `ResearchStrategy` and run it itself, which makes script strategies re-runnable and eligible for optimisation and walk-forward. It needs its own batch, with these prerequisites decided first:
  - how strategy code is stored, versioned and isolated when the worker executes it;
  - hook speed — a custom `ResearchStrategy` took 57.5 s on 70,000 bars (recorded in Batch 16), which rules out optimisation over uploaded strategies until the per-bar prefix copies are replaced;
  - data — a script chooses its own frame (a saved parquet file, resampled ticks), while the stack can only fetch from its market-data service.
  The `strategy_source` and parameters stored by this batch are the starting material.
- **Publishing tick, optimisation and walk-forward results.** `publish()` covers candle backtests only.
- **Open trades in published runs.** A stack run stores closed trades only, and published runs follow it; a position still open at the last bar is not shown unless `force_close_at_end` is set.
- **Experiment scripts.** `experiments/` lives on the `research/experiments` branch, not on `development`, so adding `chart_indicators()` and `publish()` to `ccm_test.py` is done on that branch rather than through a board task.

## Review and integration

Documentation is committed on local `docs/batch-17-research-script-runs` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-17-research-script-runs`. No branch is pushed or merged by the authoring session. The complete specification and plan text is embedded in each issue so remote review is possible before publication.

The human integrates these documentation branches before launching implementation tasks, and owns the initial board Status and the move to Todo. The authoring session adds the issues and their Batch and Depends on metadata only. No product code is changed by the authoring session.

Launch an approved task whose dependencies are Done from the workspace root with `./work start Q-NNN --agent <agent> --worktree`. Q-099 and Q-101 need the merged Q-097 commit to be reachable on the `q_contracts` remote, because `make contracts` fetches the pinned revision from there. Q-099 adds a database migration that the operator applies to the Research database after merge.
