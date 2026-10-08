# Q-098 implementation plan: Chart indicator declarations for research strategies

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-098 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** A research strategy declares which computed columns are drawn and in which pane, and the result carries the declaration.
**Architecture:** An optional strategy hook, validated by the adapter and mapped to the existing `ChartIndicatorSpec`.
**Tech stack:** Python 3.12, pandas, pytest.
**Spec:** [Specification](../specs/Q-098-chart-indicator-declarations-for-research-strategies-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The hook is optional; no existing strategy, example or test needs editing to keep passing.
- Reuse `ChartIndicatorSpec` and `serialize_chart_data`; add no second series format.
- Trades, metrics, equity and `result.data` are byte-for-byte unchanged for existing strategies.
- Tests use synthetic frames only.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- The declaration is validated against the frame `compute_indicators` returned, not the input frame.
- Errors name the strategy class and the offending column.
- Adding the `indicators` field does not break construction of `BacktestResult` anywhere, including the empty-frame path.
- The public export list and the import test agree.

## Ordered implementation

### 1. Add the declaration and the hook

**Files:** Create `src/q_backend/research/charting.py`; modify `src/q_backend/research/strategy.py`, `src/q_backend/research/__init__.py`, `tests/research/test_imports.py` and `tests/research/test_research_strategy.py`.
**Interfaces:** `ChartIndicator(column, pane="price", label=None, color=None)`; `ResearchStrategy.chart_indicators()`.

- [x] Add failing tests: defaults, an invalid pane, the default empty hook, and the public export.
- [x] Run `uv run pytest tests/research/test_research_strategy.py tests/research/test_imports.py -q` and confirm the new cases fail.
- [x] Implement and re-run until green.
- [x] Commit this unit.

### 2. Validate and map in the adapter, expose on the result

**Files:** Modify `src/q_backend/research/adapter.py`, `src/q_backend/research/engine.py`, `src/q_backend/research/results.py` and `tests/research/test_backtest.py`.
**Interfaces:** `ResearchStrategyAdapter.get_chart_indicators()` returns `ChartIndicatorSpec` values; `BacktestResult.indicators`.

- [x] Add failing tests for spec acceptance items 1 to 5, including each invalid declaration and a registered strategy run by name.
- [x] Run `uv run pytest tests/research/test_backtest.py -q` and confirm the new cases fail for the expected reason.
- [x] Implement validation after `compute_indicators`, the mapping, and the result field on both the normal and the empty-frame path.
- [x] Run `uv run pytest tests/research -q` and confirm it passes.
- [x] Commit this unit.

### 3. Document

**Files:** Modify `docs/research-library.md`, `examples/research/rsi_reversion.py` and `tests/research/test_examples.py` if it pins the example's output.

- [x] Update the guide and the example as the spec lists.
- [x] Run `uv run pytest tests/research/test_examples.py -q`.
- [x] Commit docs and example.

## Verification and handoff

Results of the checks actually run:

- `uv run pytest tests/research -q`: 142 passed.
- `uv run ruff check src/q_backend/research tests/research examples/research`: passed.
- `uv run black --check src/q_backend/research tests/research examples/research`: passed.
- `make contracts-check` was not run: this task changes no contract schemas.

- [x] Run `uv run pytest tests/research -q`, `uv run ruff check src/q_backend/research tests/research examples/research` and `uv run black --check` on the same paths.
- [x] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [x] Use `./work board set Q-098 in-review -m "<changes; checks and results; follow-ups>"`.
