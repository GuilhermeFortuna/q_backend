# Q-106 implementation plan: Priced entry orders in research backtests

> **For implementation agents:** Read the linked spec and repository instructions.
> Use superpowers:executing-plans when that skill is available. Start only through
> `./work start Q-106 --agent <agent> --worktree` after written-plan approval.
> Implement this task natively; delegation requires separate authorization.

**Goal:** `TradeOrder.buy(price=...)` and `sell(price=...)` fill on the evaluated bar at that price in `backtest()`.
**Architecture:** `TradeOrder` gains `price`; the adapter writes it to a signal column; the engine passes it to the Q-105 kernel argument; the kernel owns the fill rule.
**Spec:** [Specification](../specs/Q-106-priced-entry-orders-in-research-backtests-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The linked spec defines behaviour and acceptance criteria; do not widen scope.
- Unpriced orders keep today's behaviour and outputs.
- No fill logic in the backend; the kernel supplies it.
- Pin the `q-core` tag published by Q-105 in `pyproject.toml`.
- Focused tests only; no gateway, Docker, GPU, Wine or desktop run.
- Commit focused changes on the task branch. Never push, merge or change protected branches.

## Ordered implementation

- [ ] 1. Add failing tests in `tests/research/` for spec criteria 1 to 6.
- [ ] 2. `research/orders.py`: `price` field, validation, `buy`/`sell` factories, `close` rejection.
- [ ] 3. `research/adapter.py`: new signal column beside `SIGNAL_STOP_PRICE`/`SIGNAL_TARGET_PRICE`, set in the batch loop, `_levels_of` and `_RuntimeHooks`; require `ticks` only when levels accompany the price.
- [ ] 4. `research/engine.py` (and `intrabar.py` if it owns the call): pass `entry_price` to `run_candle`; translate the kernel range error to an exception naming strategy, bar, price and range. Move the `q-core` tag in `pyproject.toml` to the Q-105 release and refresh `uv.lock`.
- [ ] 5. Update `docs/research-library.md` "Execution model" and `research/strategy.py` docstrings.
- [ ] 6. Run the focused tests, then `make check`. Commit.

## Review focus

- A priced order never also fills at the next open.
- The range error names the strategy and bar, and is not swallowed by the hook-error wrapper.
- A price outside `[low, high]` on a bar the screen skipped still raises.
- Runtime-hook and batch paths produce the same column.
- Documentation states the research-only limit and the causality caveat.

## Validation and handoff

Run the focused tests, then `make check`. Record results and follow-ups, commit, then run
`./work board set Q-106 in-review -m "<changes; checks and results; follow-ups>"` from the workspace root.
