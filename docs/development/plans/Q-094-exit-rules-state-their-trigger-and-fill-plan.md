# Q-094 implementation plan: Exit rules state their trigger and fill

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-094 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** The exit-rule catalog says when each rule triggers and where it fills, and tests pin that behaviour.
**Architecture:** One shared sentence in the exit-rules package, appended to accurate per-rule descriptions; engine-level tests on synthetic frames.
**Tech stack:** Python 3.12, pandas, pytest, the existing candle engine bridge to `q_core`.
**Spec:** [Specification](../specs/Q-094-exit-rules-state-their-trigger-and-fill-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Strings and tests only. No change to fill semantics, the kernel bridge, rule parameters, ids, labels, groups or presets.
- The `/api/v1/exit-rules` response shape is unchanged, so no contract or frontend work.
- Describe only behaviour a test in this repository demonstrates. If a claim cannot be pinned, drop the claim.
- Tests use small synthetic frames. No `q_core` change or release, Wine, GPU, Docker or desktop run.
- Do not edit `docs/research-library.md`; Q-096 owns that section.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- Trigger sides are right for each rule: stops read the low for longs and the high for shorts, targets the reverse, and the time stop counts completed bars.
- The shared sentence exists once and every rule uses it.
- The pinned stop case really has a close above the stop level, so it distinguishes a high/low trigger from a close trigger.
- No existing golden or research backtest result changes.

## Ordered implementation

### 1. Pin the trigger and fill behaviour

**Files:** Modify `tests/backtesting/test_exit_rules.py`; reuse its existing engine helpers and frame builders.
**Interfaces:** None change; these tests document existing behaviour and are expected to pass before any source edit.

- [ ] Check which of the spec's pinned behaviours 1–5 an existing test or golden already demonstrates (for example `test_golden_fixed_stop_loss_long`, `test_golden_fixed_take_profit_long`, `test_time_stop_closes_on_max_bars`), and note the test name beside each in this plan.
- [ ] Add the missing cases: the long stop whose bar closes back above the level and exits at the next open at a price above the level; the long target exiting at the next open away from the level; the mirrored short cases; a trigger on the entry bar; the time stop's exit bar.
- [ ] Run `uv run pytest tests/backtesting/test_exit_rules.py -q` and confirm the new cases pass against the unmodified engine. A failure here means the spec's description of the engine is wrong: stop and report it instead of changing the engine.
- [ ] Commit the tests.

### 2. Rewrite the catalog text

**Files:** Modify `src/q_backend/backtesting/exit_rules/base.py` (shared sentence) and `legacy.py`, `breakeven.py`, `chandelier.py`, `donchian_stop.py`, `parabolic_sar.py`, `profit_target_ratchet.py`, `time_stop.py`; modify `tests/backtesting/test_exit_rules.py` and `tests/api/test_strategies_endpoint.py`.
**Interfaces:** A module-level constant in `exit_rules/base.py` holds the shared fill sentence. Each rule's `description` names its trigger basis and ends with that constant.

- [ ] Add a failing catalog test: every item from `list_exit_rules()` has a description ending with the shared sentence, and each stop, target and time rule names the bar value it reads. Extend `test_exit_rules_catalog_endpoint` to assert one rewritten description arrives through the route function.
- [ ] Run `uv run pytest tests/backtesting/test_exit_rules.py tests/api/test_strategies_endpoint.py -q` and confirm the new assertions fail on the old text.
- [ ] Rewrite the eleven descriptions and the Donchian period hint in plain sentences. Keep each description to two sentences: the trigger, then the shared fill sentence.
- [ ] Run the same command and confirm it passes.
- [ ] Commit this unit.

## Verification and handoff

- [ ] Run `uv run pytest tests/backtesting/test_exit_rules.py tests/backtesting/test_exit_strategy.py tests/backtesting/test_goldens.py tests/api/test_strategies_endpoint.py tests/api/test_strategy_builder_capabilities.py tests/research/test_backtest.py -q` and confirm no outcome changed.
- [ ] Run `uv run ruff check src/q_backend/backtesting/exit_rules tests/backtesting/test_exit_rules.py tests/api/test_strategies_endpoint.py` and `uv run black --check` on the same paths.
- [ ] Record the commands actually run and their results in this plan, including which pinned behaviours were already covered; do not claim unrun checks passed.
- [ ] Use `./work board set Q-094 in-review -m "<changes; checks and results; follow-ups>"`. Note in the message that `q_frontend`'s offline mock keeps the old strings.
