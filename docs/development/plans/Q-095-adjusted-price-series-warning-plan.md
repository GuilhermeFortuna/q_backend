# Q-095 implementation plan: Adjusted price series warning

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-095 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** `load_bars` warns when loaded prices are off the instrument's tick grid, and the guide and examples steer users to the unadjusted series.
**Architecture:** A best-effort check in `research/data.py` using the gateway's symbol information, a dedicated warning category, and documentation.
**Tech stack:** Python 3.12, pandas, numpy, pytest, the existing remote gateway client.
**Spec:** [Specification](../specs/Q-095-adjusted-price-series-warning-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The check never raises and never changes the returned data. Its only effects are two `attrs` keys and at most one warning per call.
- Threshold 0.01 and the `attrs` key names `tick_size` and `off_tick_share` are exactly as the spec states.
- No new dependency, registry or configuration. One extra `get_symbol_info` request per `load_bars` call and none for `load_ticks`.
- Tests mock the gateway client. No live terminal, Wine, Docker, GPU or desktop run.
- Leave platform presets and `backtest()` alone.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- Floating-point tolerance: prices such as 5400.5 with a 0.5 tick, and 0.01-tick equities, must count as on-grid.
- The warning is attributed to the caller of `load_bars`, not to library internals.
- Every failure of the symbol-information lookup leaves the load result identical to today's.
- The guide's recommendation is accurate about what each series preserves.

## Ordered implementation

### 1. Add the warning category and the tick-grid check

**Files:** Modify `src/q_backend/research/errors.py`, `src/q_backend/research/__init__.py`, `src/q_backend/research/data.py`, `tests/research/test_data.py` and `tests/research/test_imports.py`. Put a small pure helper for the off-grid share in `src/q_backend/research/frame.py` if that keeps `data.py` readable.
**Interfaces:** `AdjustedSeriesWarning(UserWarning)` exported from `q_backend.research`. `load_bars` signature unchanged. `attrs["q_research"]` may gain `tick_size: float` and `off_tick_share: float`.

- [x] Add failing tests for spec acceptance 1 to 4: on-grid bars; a frame just under and just over the 1% threshold; each lookup failure mode (returns `None`, missing key, zero, NaN, raises `ConnectionError`); the lazy export and inert import. Use `pytest.warns` and `warnings.catch_warnings` to assert presence and absence.
- [x] Run `uv run pytest tests/research/test_data.py tests/research/test_imports.py -q` and confirm the new cases fail because the behaviour is missing.
- [x] Implement the warning class, the export (lazy `__getattr__`, `__all__` and the `TYPE_CHECKING` block) and the check.
- [x] Run the same command and confirm it passes, including the existing request-count test.
- [x] Commit this unit.

### 2. Document the series and change the example defaults

**Files:** Modify `docs/research-library.md`, the research commands in `README.md`, `examples/research/rsi_reversion.py`, `examples/research/mt5_backtest.py`, `examples/research/load_market_data.py`, `tests/research/test_examples.py` and `tests/research/test_load_market_data_example.py`.

- [x] Update the example tests to expect `WIN$N` where a default or documented symbol is asserted; confirm they fail.
- [x] Change the example defaults and help text.
- [x] Write the "Choosing a price series" section from the spec's table and recommendation, and switch the guide's snippets and the README research commands to `WIN$N`. Leave other README sections as they are.
- [x] Run `uv run pytest tests/research -q` and confirm it passes.
- [x] Commit docs and examples.

## Verification and handoff

- [x] Run `uv run pytest tests/research -q`, `uv run ruff check src/q_backend/research examples/research tests/research` and `uv run black --check` on the same paths.
- [x] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-095 in-review -m "<changes; checks and results; follow-ups>"`.

### Verification log (2026-10-08)

- `uv run pytest tests/research -q` — **86 passed** (2 pydantic deprecation warnings).
- `uv run ruff check src/q_backend/research examples/research tests/research` — **All checks passed**.
- `uv run black --check src/q_backend/research examples/research tests/research` — **22 files unchanged**.
