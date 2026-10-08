# Research strategy position context

Both research decision hooks may declare a `positions` parameter alongside `frame`.
Existing frame-only overrides remain supported. Positions are immutable snapshots of
actual open trades, after queued fills and intrabar protective fills, before current
bar decisions. Both hooks observe the same tuple; exit runs before entry. A requested
close remains pending until the next open. Empty holdings produce `()`.

Export frozen, slotted `ResearchPosition(symbol, side, entry_time, entry_price,
quantity)` from `q_backend.research`; side is `long` or `short`, and entry_time uses
the research frame timezone. Entries keep existing capacity and execution semantics.
`TradeOrder.close()` continues closing all positions for the backtest symbol.

Rust owns the simulation. Add an optional Python strategy callback projected through
PyO3 into the existing Rust bar loop. Legacy strategies retain static signal compilation;
when either overridden hook declares positions, both hooks run through the callback.
Hooks still receive separate owned closed-bar prefixes without frame attrs, once per
bar including suppressed/final bars. Suppressed decisions are discarded by the engine.
User indicators are prepared once. No future history or pending orders enter snapshots.

Errors abort the run with hook and bar context; no TypeError-based invocation retries.
No board task is required, by explicit user authorization. Work begins on feature
branches from development in both repositories. Core publication and backend release
pinning remain a separate human integration step.
