# Batch 15 — Python research library

**Status:** Q-089 requirements revised during inspection; follow-on specs/plans updated to use the fresh MT5 loader. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend. No companion product/contract/kernel task is required.

## Outcome

Write a Python experiment that fetches fresh MT5 market data as a pandas DataFrame, adds existing Q indicators, defines a strategy class, and runs a local candle backtest. Use the existing backend installation and q_core computations, with no API/worker/desktop startup for calculations.

## Tasks and delivery order

| Task | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- |
| [Q-089](https://github.com/GuilhermeFortuna/q_backend/issues/28) — Research market DataFrames | none | [Spec](../specs/Q-089-research-market-dataframes-spec.md) | [Plan](../plans/Q-089-research-market-dataframes-plan.md) |
| [Q-090](https://github.com/GuilhermeFortuna/q_backend/issues/29) — Research indicator helpers | Q-089, Q-023 | [Spec](../specs/Q-090-research-indicator-helpers-spec.md) | [Plan](../plans/Q-090-research-indicator-helpers-plan.md) |
| [Q-091](https://github.com/GuilhermeFortuna/q_backend/issues/30) — Research strategy classes and local backtesting | Q-089, Q-090, Q-028 | [Spec](../specs/Q-091-research-strategy-backtesting-spec.md) | [Plan](../plans/Q-091-research-strategy-backtesting-plan.md) |

Implement Q-089, then Q-090, then Q-091. Q-023 and Q-028 are existing Done prerequisites; Q-089 no longer depends on catalog task Q-020. Each task ships its own guide/example updates; no separate scaffolding, benchmark or documentation-only implementation task.

## Shared decisions

- Supported namespace: q_backend.research inside the backend package. Keep existing dependency/install scope; extracting a standalone package is future work.
- Data: `load_bars(symbol, timeframe=..., start=..., end=None)` fetches through the existing MT5 gateway on every call; end defaults to now. Configuration uses existing gateway settings with optional overrides. No Research object, catalog/database, auto-selection or cache fallback. Return latest completed candles with an aware America/Sao_Paulo index. The terminal/gateway must already be running; calculations on supplied DataFrames need neither.
- Indicators: small Series/DataFrame functions delegate to the existing Q wrappers. Add columns with ordinary pandas assignment, retaining index and warm-up NaNs. No second indicator library or special DataFrame type.
- Strategies: ResearchStrategy ABC with compute_indicators(frame), entry_strategy(frame), exit_strategy(frame). entry_strategy is abstract; the other hooks have identity/None defaults. Entry/exit hooks see separate closed-bar history prefixes and return None or immutable TradeOrder.buy()/sell()/close().
- Orders initially select action only. Fixed quantity, capital, point multiplier, transaction costs and existing composable exits are run configuration. No position/fill state in callbacks, per-order quantity, partial closes or custom broker order types.
- Execution: prepare indicators once, compile callback decisions to existing signal columns, and use the current candle kernel for next-open fills, exits and exposure caps. Preserve the built-in vectorized strategy path. Prefix copies/callbacks favor readable experiments and are documented as slower; no performance target.
- Results: metrics, all trades (including open positions), prepared data and explicitly labeled realized equity. No new metric formulas, mark-to-market claims or implicit database persistence.
- Scope: historical candle experiments. No live trading, tick API, optimizer/discovery wiring, new wire schemas, GUI or GPU requirement. A public causality diagnostic subsystem is deferred; examples explain causal computations.

## Verification scope

Only checks that demonstrate the new behavior: small mocked MT5/HTTP fixtures and timezone/schema cases; parity against existing indicator wrappers; callback/prefix and existing-engine parity cases; runnable fixture-backed examples. Plans name the relevant existing regression modules. No benchmarks, blanket full CI, full-stack/Docker/GPU/Wine/desktop acceptance or live-feed prerequisites. Additional canonical checks are justified only if implementation changes shared behavior.

## Review and integration

This documentation revision is committed separately on docs/batch-15-mt5-loader, based on the inspected Q-089 implementation. Complete revised spec/plan text is embedded in the existing three issues. Product code, board Status and the active inspection checkout remain unchanged.

After ending inspection with the documented ./work inspect --restore workflow, the human can integrate the documentation-only revision into the Q-089 task branch before resuming its correction work. The original implementation is still subject to review; this authoring session does not merge it or mark it complete. Q-090/Q-091 remain dependent on Q-089 completion and their own plan approval. Human integration must make these revised follow-on documents available before launch.

The board metadata matches the new scopes: Batch=15; Q-089 has no catalog prerequisite; Q-090 depends on Q-089/Q-023 and Q-091 on Q-089/Q-090/Q-028. No Status changes are made in this authoring session. Implement natively; delegation requires separate authorization. Keep focused verification only, with no benchmarks/full-stack acceptance.
