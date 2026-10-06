# Batch 15 — Python research library

**Status:** written specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend. No companion product/contract/kernel task is required.

## Outcome

Write a Python experiment that loads Q market data as a pandas DataFrame, adds existing Q indicators, defines a strategy class, and runs a local candle backtest. Use the existing backend installation and q_core computations, with no API/worker/desktop startup for calculations.

## Tasks and delivery order

| Task | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- |
| [Q-089](https://github.com/GuilhermeFortuna/q_backend/issues/28) — Research market DataFrames | Q-020 | [Spec](../specs/Q-089-research-market-dataframes-spec.md) | [Plan](../plans/Q-089-research-market-dataframes-plan.md) |
| [Q-090](https://github.com/GuilhermeFortuna/q_backend/issues/29) — Research indicator helpers | Q-089, Q-023 | [Spec](../specs/Q-090-research-indicator-helpers-spec.md) | [Plan](../plans/Q-090-research-indicator-helpers-plan.md) |
| [Q-091](https://github.com/GuilhermeFortuna/q_backend/issues/30) — Research strategy classes and local backtesting | Q-089, Q-090, Q-028 | [Spec](../specs/Q-091-research-strategy-backtesting-spec.md) | [Plan](../plans/Q-091-research-strategy-backtesting-plan.md) |

Implement Q-089, then Q-090, then Q-091. Q-020, Q-023 and Q-028 are existing Done prerequisites. Each task ships its own guide/example updates; no separate scaffolding, benchmark or documentation-only implementation task.

## Shared decisions

- Supported namespace: q_backend.research inside the backend package. Keep existing dependency/install scope; extracting a standalone package is future work.
- Data: explicit local, remote or auto selection on each Research instance. local reads immutable cataloged Parquet and requires PostgreSQL; remote reads the existing MT5 data gateway without PostgreSQL/Redis or persistence. No native terminal construction, silent data filling or global source changes. OHLCV has an aware America/Sao_Paulo index.
- Indicators: small Series/DataFrame functions delegate to the existing Q wrappers. Add columns with ordinary pandas assignment, retaining index and warm-up NaNs. No second indicator library or special DataFrame type.
- Strategies: ResearchStrategy ABC with compute_indicators(frame), entry_strategy(frame), exit_strategy(frame). entry_strategy is abstract; the other hooks have identity/None defaults. Entry/exit hooks see separate closed-bar history prefixes and return None or immutable TradeOrder.buy()/sell()/close().
- Orders initially select action only. Fixed quantity, capital, point multiplier, transaction costs and existing composable exits are run configuration. No position/fill state in callbacks, per-order quantity, partial closes or custom broker order types.
- Execution: prepare indicators once, compile callback decisions to existing signal columns, and use the current candle kernel for next-open fills, exits and exposure caps. Preserve the built-in vectorized strategy path. Prefix copies/callbacks favor readable experiments and are documented as slower; no performance target.
- Results: metrics, all trades (including open positions), prepared data and explicitly labeled realized equity. No new metric formulas, mark-to-market claims or implicit database persistence.
- Scope: historical candle experiments. No live trading, tick API, optimizer/discovery wiring, new wire schemas, GUI or GPU requirement. A public causality diagnostic subsystem is deferred; examples explain causal computations.

## Verification scope

Only checks that demonstrate the new behavior: small fake catalog/HTTP fixtures and timezone/schema cases; parity against existing indicator wrappers; callback/prefix and existing-engine parity cases; runnable fixture-backed examples. Plans name the relevant existing regression modules. No benchmarks, blanket full CI, full-stack/Docker/GPU/Wine/desktop acceptance or live-feed prerequisites. Additional canonical checks are justified only if implementation changes shared behavior.

## Review and integration

All seven documents are on local branch docs/batch-15-research-library in an isolated backend worktree. The normal backend checkout is left untouched. Complete spec/plan text is embedded in the three issues for review before publication; no documentation branch is pushed or merged by this authoring session.

Human integration must make the documentation available on development before launching tasks. The authoring session adds project membership, Batch=15 and Depends on metadata, and leaves Status uninitialized: workspace rules prohibit manual status changes, and ./work board set only transitions In Progress tasks. The human owns initial Blocked status and subsequent Todo approval.

After review/integration and completed dependencies, launch from the workspace root with ./work start Q-089 --agent <agent> --worktree, followed by Q-090 and Q-091 when their prerequisites are Done. Each plan uses native implementation; delegation requires separate authorization. Human ./work finish owns merging and completion.
