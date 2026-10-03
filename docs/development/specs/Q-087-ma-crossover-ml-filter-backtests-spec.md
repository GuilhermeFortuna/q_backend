# Q-087: MA Crossover ML filter backtests

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 14 — ML entry filters for research
**Depends on:** Q-086
**Implementation plan:** [Plan](../plans/Q-087-ma-crossover-ml-filter-backtests-plan.md)

## Purpose

Expose MA Crossover · ML Filter as a separate selectable research strategy using Q-086 fitted versions and post-combination entry gate. Preserve original MACrossover, all existing saved runs and the existing q_core execution kernel. No neural inference inside q_core and no new order/fill semantics.

## Required behavior

- Register `MACrossoverMLFilter` with label `MA Crossover · ML Filter`, the original MA entry parameter specs and an explicit research-only/filter capability. Reuse original MACrossoverStrategy calculations and original exit defaults. Do not duplicate MA formulae, change crossover frequency or attach filtering to its raw buy/sell booleans.
- Add generated Q-085 ml_filter config to the existing candle backtest path. Build normal single-entry CompositeEntryStrategy first, then wrap the result with Q-086 EntryFilteredStrategy before indicator augmentation and engine execution. Both chart preparation and actual engine consume the same gated decisions; compatibility checks are centralized, not only UI checks.
- Variant requires exactly one entry, manager `or` with empty params, a ready model_version_id and finite threshold in [0,1]. Reject missing model, original strategy plus ml_filter, mixed/multiple entries, unsupported managers, tick runs and model mismatch. Filterless original configurations stay valid.
- Compatibility fingerprint matches Q-086 source semantics. Prediction/trading dates must be at or after train_end; historical warm-up bars before train_end are permitted only with entries suppressed. Default displayed evaluation range must not overlap training. Service returns 409 for mismatches instead of adapting model feature lists, costs or strategy parameters silently.
- At opposite crossover, preserve close-long/close-short signal and ordinary next-open timing. If opposite entry score passes, the existing kernel may reverse normally; if rejected or not-ready, close and remain flat. Stay flat until a fresh crossover; no deferred entry or score-driven exit. All original stop/target/trailing/day-trade exits remain active.
- Filter scoring is batched by candidate rows and the pipeline is loaded once per job. Do not use scores as signal strength or sizing multipliers. Record scored/accepted/rejected/not-ready candidate counts, model/version/threshold and source dataset identity. Failed model loading/scoring fails the backtest honestly; never silently fall back to original strategy.
- Persist/restore ml_filter in backtest history and include Q-085 summary in completed results. Artifacts preserve raw indicators and additive candidate score/acceptance diagnostics, without changing existing market-data exports. Restore a deleted/unavailable model reference as unavailable, not a new model. A user-selected model remains pinned across retraining.
- Exclude the variant from live execution capabilities, default registry discovery sweeps and optimize/walk-forward selectors. Those paths reject explicit use with an actionable unsupported message. Advertise additive registry capabilities through Q-085 types rather than scattering strategy-name exceptions across clients.

## Integration points

Add `backtesting/strategies/ma_crossover_ml_filter.py`; modify strategies/__init__.py, strategy_registry.py capability metadata, factory.py post-combination hydration, api/backtest_jobs.py, api/schemas/backtest.py generated integration, and run_service.py as needed. Reuse ml_filters/filter.py/service.py from Q-086. Update execution/strategy_build.py and optimization entry validation only to reject this research-only variant; do not alter original eligibility. Consumer vendoring stays pinned to Q-085's published commit.

## Acceptance criteria

1. Engine fixtures for accepted/rejected long and short reversals close at the expected next open; rejected reversals remain flat and exits occur regardless of score. A later high score without crossover does not enter.
2. Original MACrossover registry, goldens and saved request shapes remain unchanged; shared crossover outputs before gating are identical.
3. Model compatibility, date cutoffs, unsupported compositions/managers/engines/workflows and missing/corrupt models fail before misleading results are returned.
4. Preview/chart preparation and engine use equivalent causal scores; accepted entries retain original strength and position sizing.
5. Saved runs reload the exact version/threshold and summaries distinguish candidate signals from executed trades; runtime failures persist as failed runs.
6. Focused strategy/API/factory/capability/history tests, contracts-check and canonical backend CI pass. Existing q_core close-without-entry semantics require no kernel change or new release.

## Delivery boundary

Written specification and plan await human review. No implementation is authorized by this documentation session. Integrate/publish the documentation before launch; the human approves plans and sets Todo. Start only with `./work start Q-087 --agent <agent> --worktree` after dependencies are Done. Implement natively; delegation requires separate authorization. Never push, merge, edit vendored contracts, or change board status outside the workspace workflow.

Batch 14 is research-only: no live deployment, q_terminal integration, automatic walk-forward retraining, optimizer/discovery support, multi-entry ML combinations, custom uploads, or GPU requirement. Existing MACrossover behavior and saved configurations remain compatible. Python orchestrates model inference; existing q_core indicator and execution semantics are reused without another fill/indicator implementation.
