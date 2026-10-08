# Research review fixes implementation plan

**Goal:** Correct the seven findings from the Q-089–Q-097 review.
**Authorization:** The user approved fixes directly on `development`, without a board task.
**Architecture:** Validate research inputs before the existing candle engine; preserve market columns and result timestamp dtypes. Fail incomplete MT5 scans explicitly. Preserve declared scalar defaults in contract generators.
**Tech stack:** Python, pandas, pytest, OpenAPI, Rust/Serde and the existing contract generator.
**Requirements:** Existing Q-091, Q-093 and Q-097 specs, plus the review findings.

## Constraints and review focus

- Keep shared trading math, wire shapes and default execution behavior unchanged.
- Use existing exit parameter specs; reject invalid values before hooks run.
- Empty input calls no strategy hooks; preserve every original market column.
- Incomplete history cannot be represented as a successful complete response.
- Missing origin means `stack`; explicitly supplied origins remain unchanged.
- Keep timestamp timezone behavior stable for empty, open and closed trades.
- Quote dollar-sign symbols in shell examples; no automated prose tests.
- Do not push or change board status.

## Implementation

1. Research facade (`research/engine.py`, `adapter.py`, `results.py`): add failing regressions for exit values, empty hooks, market-column mutation and open-trade timezone; implement the fixes and rerun research/engine regressions.
2. History (`gateway/mt5_gateway.py`, `market_data/clients/metatrader.py`): add failing bounded-scan tests for empty/partial data, implement explicit errors, and run gateway/client continuation tests. Document the existing gateway error shape in `q_contracts/schema/edge/data-gateway.yaml`.
3. Documentation (`README.md`, `docs/research-library.md`): quote shell symbols and clarify that time exits use completed-bar counts.
4. Contracts (`q_contracts/tools/emitters/{python,rust}.py`, `tests/test_generate.py`): test missing-origin runtime behavior, preserve scalar defaults, regenerate outputs, and run the contracts checks. Update compatibility notes for regeneration; do not hand-edit consumer vendored files.
5. Review final diffs, run focused checks, record results below, and commit focused changes in each repository on `development`.

## Verification results

- Research regressions first failed on the four reviewed defects; after fixes, the backtest and adapter files passed all 46 tests. Empty-input reserved-column validation was retained with two additional regressions.
- Gateway/native bounded-scan tests first failed for incomplete empty and partial reads; all six bounded-scan/boundary tests passed after the guards were added.
- Python origin-default behavior and Rust default generation tests first failed; all four focused origin tests passed after generator corrections.
- Backend: `.venv/bin/python -m pytest tests/research tests/gateway/test_mt5_gateway.py tests/market_data/test_get_ohlcv_chunked.py tests/market_data/test_remote_client.py tests/market_data/test_timezone.py tests/backtesting/test_engine.py tests/backtesting/test_exit_rules.py tests/backtesting/test_exit_strategy.py tests/backtesting/test_candle_kernel_bridge.py tests/backtesting/test_signal_columns.py -q --tb=short` — **276 passed**; two existing Pydantic deprecation warnings. Fake HTTP servers used ephemeral localhost ports.
- Backend changed Python files passed Black and Ruff. No full backend Docker CI or live terminal was needed for these changes.
- Contracts: canonical `make check` — **245 passed, 7 skipped, 1 deselected**, including generator drift, formatting, lint and schema validation. Black required local IPC support outside the restricted sandbox.
- A temporary offline Cargo project compiled the full generated Rust API and passed two runtime tests: omitted `stack` origins and explicit `script` origins in both run types, plus Unicode/control-character string default round-trips. Existing camel-case wire field names produce Rust naming warnings; new default helper functions use snake case.
- Generated files were changed only through the contracts generator. Consumer pins and vendored outputs remain at their existing revisions; adoption guidance is recorded in `q_contracts/COMPAT.md`.
- Final review found two additional edge cases: empty input duplicate columns and Rust string-literal escaping. Both gained regressions that failed first and passed after correction; the complete focused/backend and canonical/contracts checks above were rerun. No findings remain deferred. All seven original findings are addressed. Code commits: backend `d643a44` and `9c5bf69`; contracts `e06a3c9`. Normal pre-commit checks passed. Delivery is local on `development`, with no push or board changes.
