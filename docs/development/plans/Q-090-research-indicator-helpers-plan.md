# Q-090 implementation plan: Research indicator helpers

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-090 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** Expose familiar pandas indicator functions backed by Q calculations.
**Architecture:** A small indicators module validates arguments and forwards to existing moving-average/technical-indicator wrappers; normal pandas assignment adds outputs to frames.
**Tech stack:** Python 3.12+, pandas, existing q_backend adapters and pinned q_core.
**Spec:** [Specification](../specs/Q-090-research-indicator-helpers-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Python namespace inside q_backend; retain existing installation/dependency scope and service behavior.
- No alternative indicator/fill/PnL implementation, generated contract changes, GPU, live trades, desktop or optimizer integration.
- Use lazy dependencies and explicit gateway overrides when fetching data. No import-time startup or global environment/runtime-config mutation.
- Verify changed behavior with small fixtures and focused mocks. No benchmarks, full-stack runs, live gateway runs or blanket full CI requirement.
- Commit focused task changes locally. Human owns integration, publication and initial board Status/Todo approval.

## Review focus

- Outputs retain exact index/timezone and NaN warm-up; inputs stay unchanged.
- Tuple ordering is upper/middle/lower, line/signal/histogram or upper/lower as specified.
- Unknown MA kinds, bool/fractional periods and infinity fail before native computation.
- ATR/Donchian/Yang-Zhang work without unrelated volume columns.
- Pure indicator use never initializes providers or services; annualization remains explicit.

## Ordered implementation

### 1. Add the documented indicator functions

**Files:** Create src/q_backend/research/indicators.py and tests/research/test_indicators.py; extend research/__init__.py lazy exports.
**Interfaces:** ma, rsi, atr, bollinger, macd, donchian, realized_vol and yang_zhang with the exact signatures/tuple ordering in Q-090. Delegate through existing backend bridges.

- [ ] Add one parametrized parity test over all eight functions using a small numeric frame: compare against the underlying wrapper with equal NaNs, identical index/tuple ordering and unchanged inputs. Include empty/index-with-timezone cases without duplicating mathematical goldens.
- [ ] Add a compact invalid-input table covering unknown MA kind, bool/fractional/nonpositive windows, Yang-Zhang window=1, infinity, nonnumeric Series, missing OHLC columns, invalid num_std and periods_per_year. Assert a clear exception before the delegate is called.
- [ ] Run `uv run pytest tests/research/test_indicators.py -q`; implement validation and delegation, preserving defaults and NaNs. No direct q_core imports or new formulas.
- [ ] Run `uv run pytest tests/research/test_indicators.py tests/backtesting/test_moving_averages.py tests/backtesting/test_indicator_baseline.py -q` to cover the public wrappers and their delegates. Commit the helper module.

### 2. Add the offline indicator example

**Files:** Create examples/research/add_indicators.py; extend docs/research-library.md and tests/research/test_indicators.py.

- [ ] Lead the guide with `load_bars("WIN$", timeframe="M5", start="2026-09-01")` followed by indicator assignments; fetching requires the MT5 gateway, while indicator helpers themselves require no gateway/database. Document normal column assignment and tuple unpacking, units/annualization, NaN warm-up and parameter validation. The example accepts a Parquet input path and output path and adds RSI/EMA/ATR, without global configuration or service startup.
- [ ] Exercise the example against one temporary frame/file and assert added columns, unchanged row count/index and no provider/service access. Reuse the focused test command; commit documentation/examples.

## Handoff

- [ ] Check the spec against the implementation and focused results; document actual commands and outcomes without claiming unrun checks passed.
- [ ] Commit the final documentation/examples and use `./work board set Q-090 in-review -m "<changes; focused checks and results; follow-ups>"`. If a required prerequisite blocks progress, use the documented blocked workflow.
