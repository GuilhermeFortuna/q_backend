# Batch 16 — Research data and backtest correctness

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with one companion contract task in q_contracts.

## Outcome

A research backtest loads every bar it asked for, warns when the price series it loaded distorts point-based results, and runs against documentation that says when exits trigger and fill, what a trade costs, what a tick row means and how deep history goes.

## Findings this batch answers

Each was reproduced on 2026-10-06 against the running gateway, the MetaTrader 5 terminal or the candle engine.

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | A bar request over 50,000 bars is cut and returned as if complete, dropping the most recent data. | `load_bars("WDO$N", timeframe="M10", start="2021-10-01")` returned 50,000 bars ending 2025-05-13; the terminal held 70,032 through 2026-10-06. `_fetch_ohlcv_chunked` in `gateway/mt5_gateway.py` and `MetaTraderClient._fetch_ohlcv_range_chunked` stop at `_MAX_OHLCV_BARS`. | Q-092, Q-093 |
| 2 | Exit rules trigger on a completed bar's high or low and fill at the next bar's open; the catalog text implies a fill at the level. | A long with a 2% stop exited at +0.6% after the low pierced the level and the next bar opened higher. This is the specified kernel behaviour and equals live closed-bar execution. | Q-094, Q-096 |
| 3 | `WDO$` and `WIN$` are proportionally adjusted; point-value results on their history are scaled by the roll factor. The guide and examples default to `WIN$`. | 2021 `WDO$` prices are 1.42 times the traded price, and 0.4% of five years of M10 closes are on the 0.5 tick grid, against 100% for `WDO$N` and `WDO$D`. | Q-095 |
| 4 | Backtests cost nothing unless configured, and the examples do not configure it. | A market order within three seconds of a 10-minute bar's open paid 0.25 points per side beyond the recorded open on 250 `WDO$N` sessions. | Q-096 |
| 5 | The guide does not say that trade rows carry the current bid and ask. | An experiment that required a quote-update row within one second discarded 93% of its signal windows (4,026 of 4,331). | Q-096 |
| 6 | History depth is bounded by the terminal, and a first tick request can come back empty; neither is documented. | The terminal reports `maxbars` 100,000: `WDO$N` M1 starts 2026-01-22 and M5 2023-03-17. `BIT$N` ticks returned nothing on the first request and 263,338 rows eight seconds later; MetaTrader 5 reported success both times. | Q-096 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-092 — Bar completeness in the data gateway contract | q_contracts | none | `q_contracts/docs/development/specs/Q-092-bar-completeness-in-the-data-gateway-contract-spec.md` | `q_contracts/docs/development/plans/Q-092-bar-completeness-in-the-data-gateway-contract-plan.md` |
| Q-093 — Complete bar ranges from the MT5 gateway | q_backend | Q-092 | [Spec](../specs/Q-093-complete-bar-ranges-from-the-mt5-gateway-spec.md) | [Plan](../plans/Q-093-complete-bar-ranges-from-the-mt5-gateway-plan.md) |
| Q-094 — Exit rules state their trigger and fill | q_backend | none | [Spec](../specs/Q-094-exit-rules-state-their-trigger-and-fill-spec.md) | [Plan](../plans/Q-094-exit-rules-state-their-trigger-and-fill-plan.md) |
| Q-095 — Adjusted price series warning | q_backend | Q-093 | [Spec](../specs/Q-095-adjusted-price-series-warning-spec.md) | [Plan](../plans/Q-095-adjusted-price-series-warning-plan.md) |
| Q-096 — Research guide: costs, execution, ticks and history depth | q_backend | Q-094, Q-095 | [Spec](../specs/Q-096-research-guide-costs-execution-ticks-and-history-depth-spec.md) | [Plan](../plans/Q-096-research-guide-costs-execution-ticks-and-history-depth-plan.md) |

Implement Q-092, then Q-093, then Q-095, then Q-096. Q-094 has no prerequisite and can run at any point before Q-096.

The dependencies of Q-095 and Q-096 are partly about files, not only behaviour: Q-093 and Q-095 both change `research/data.py`, and Q-093, Q-095 and Q-096 all edit `docs/research-library.md`. Serializing them keeps each task's branch free of overlapping edits. Q-094 does not touch the guide for the same reason; its guide text is written in Q-096.

## Shared decisions

- **Additive contract.** `/v1/ohlcv` gains completeness metadata; no schema major bump and no generated binding change. The remote client fails closed against a gateway that does not send it.
- **Kernel fill semantics stay.** Rule exits fill at the next bar's open in backtests because live execution decides on completed bars with the same decision step. Filling stops at their level in backtests only would break the rule that backtests and live trading produce identical results. A level-fill model belongs with broker-side protective orders, in its own batch.
- **Warn, do not block.** Adjusted series remain loadable; percentage-return analysis uses them legitimately.
- **Measured numbers carry their context.** The guide gives the symbol, period and sample beside each measurement, and marks fees as assumptions.
- **Focused verification.** Fake MetaTrader 5 module, mocked HTTP and synthetic frames. No benchmark, full CI, Wine, Docker, GPU, desktop or live-feed requirement for automated checks. One manual operator step follows Q-093: restart the gateway and load more than 50,000 bars.

## Recorded, not in this batch

- **Platform presets on adjusted series.** `WIN$` and `WDO$` are the defaults in `market_data/api_service.py`, `features/registry.py`, `optimization/hypothesis.py`, `market_data/exogenous_config.py` and the `q_frontend` alpha-research presets. Which series each workflow should use is a product decision: unadjusted prices suit intraday point-value results, difference-adjusted prices suit multi-session point-value results, and proportional prices suit percentage-return features.
- **Data gateway contract drift.** The contract does not declare `/v1/ohlcv/recent`, and the gateway emits error codes the shared vocabulary lacks: `invalid_count`, `invalid_datetime`, `invalid_range`, `missing_parameter` and per-lane 503 codes. Two capture tests in `q_contracts` fail when `Q_BACKEND_PATH` is set and are skipped otherwise.
- **Unsynchronized tick history.** MetaTrader 5 reports success with an empty array for history it has not downloaded, so the gateway cannot distinguish that from an empty range. Detecting it needs a cross-check of ticks against bar volume.
- **Level fills for stops and targets.** See the shared decision above.
- **`experiments/wdo_order_flow.py`.** Its quote-age rule (finding 5) lives on the `research/experiments` branch, which is not part of `development`, so it is corrected on that branch rather than through a board task.
- **Research backtest run time on long frames.** A custom `ResearchStrategy` took 5.4 s on 20,000 bars and 57.5 s on 70,000 bars (five years of M10), because each bar's hooks receive a copy of the whole prefix; cost per bar grew from 0.27 ms to 0.82 ms. The built-in vectorized path ran 20,000 bars in 0.08 s. Batch 15 chose prefix copies deliberately and set no performance target, so this is recorded for a decision rather than treated as a defect. It matters more once deeper M1 or M5 history is available.

## Review and integration

Documentation is committed on local `docs/batch-16-research-backtest-correctness` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-16-research-backtest-correctness`. No branch is pushed or merged by the authoring session. The complete specification and plan text is embedded in each issue so remote review is possible before publication.

The human integrates these documentation branches before launching implementation tasks, and owns the initial board Status and the move to Todo. The authoring session adds the issues and their Batch and Depends on metadata only. No product code is changed by the authoring session.

Launch an approved task whose dependencies are Done from the workspace root with `./work start Q-NNN --agent <agent> --worktree`. Q-093 needs the merged Q-092 commit to be reachable on the `q_contracts` remote, because `make contracts` fetches the pinned revision from there.
