# Q-100 implementation plan: Publish research backtests to the Research stack

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-100 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** `result.publish()` records a script's finished backtest in the Research stack.
**Architecture:** `backtest()` records its configuration on the result; a publishing module builds the Q-097 import request from the result and posts it with httpx.
**Tech stack:** Python 3.12, pandas, httpx, pydantic contract models, pytest.
**Spec:** [Specification](../specs/Q-100-publish-research-backtests-to-the-research-stack-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Build the request from the vendored Q-097 models; do not hand-write the wire shape.
- Reuse `serialize_chart_data` and the trade model's JSON dump so a published run and a stack run are serialized by the same code.
- Importing `q_backend.research` must not import storage, API or worker modules; import httpx and the publishing module's heavier dependencies inside `publish()`.
- Existing `BacktestResult` fields and values are unchanged.
- Tests use synthetic frames, mocked HTTP and the API test client.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- `NaN` and infinite values never reach the JSON body.
- Timestamps in bars and trades are the ones a stack run would write for the same bars.
- Provenance collection cannot fail a publish: every lookup degrades to null.
- No retry, no caching, no mutation of the result.
- Error messages tell the user what to do next.

## Ordered implementation

### 1. Record the run configuration on the result

**Files:** Modify `src/q_backend/research/engine.py`, `src/q_backend/research/results.py` and `tests/research/test_backtest.py`.
**Interfaces:** `BacktestResult.config`; private closed-trade records on the result.

- [x] Add failing tests for spec acceptance item 1, including a registered strategy, a frame without timeframe metadata and a strategy with a non-scalar attribute.
- [x] Run `uv run pytest tests/research/test_backtest.py -q` and confirm the new cases fail.
- [x] Implement on the normal and empty-frame paths; re-run until green.
- [x] Commit this unit.

### 2. Build and send the import request

**Files:** Create `src/q_backend/research/publishing.py` and `tests/research/test_publishing.py`; modify `src/q_backend/research/results.py`.
**Interfaces:** `BacktestResult.publish(*, name=None, timeframe=None, api_url=None) -> str`; `Q_API_URL`.

- [ ] Add failing tests for spec acceptance items 2 to 5 with mocked HTTP and a temporary git repository for provenance.
- [ ] Run `uv run pytest tests/research/test_publishing.py -q` and confirm the cases fail because `publish` is missing.
- [ ] Implement request building, provenance collection and error mapping.
- [ ] Run `uv run pytest tests/research -q` and confirm it passes, including `test_imports.py`.
- [ ] Commit this unit.

### 3. Prove the round trip against the endpoint

**Files:** Extend `tests/api/test_backtest_import.py`.

- [ ] Add a test that routes `publish()` to the API test client and reads the result back, per spec acceptance item 6.
- [ ] Run `uv run pytest tests/api/test_backtest_import.py -q`.
- [ ] Commit this unit.

### 4. Document

**Files:** Modify `docs/research-library.md`, `examples/research/mt5_backtest.py` and `tests/research/test_examples.py`.

- [ ] Write the guide section and the results entry as the spec lists; add `--publish` to the example with a test that mocks the call.
- [ ] Run `uv run pytest tests/research/test_examples.py -q`.
- [ ] Commit docs and example.

## Verification and handoff

- [ ] Run `uv run pytest tests/research tests/api/test_backtest_import.py -q`, `uv run ruff check src/q_backend/research tests/research examples/research` and `uv run black --check` on the same paths.
- [ ] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-100 in-review -m "<changes; checks and results; follow-ups>"`. State the manual step: publish from `experiments/ccm_test.py` against `./dev research` and open the run in Backtests history once Q-101 is merged.
