# Q-108: Backtest run progress implementation

Approved revised behavior: [spec](../specs/Q-108-backtest-run-progress-spec.md).
The project board is the status of record. Implement only on Q-108 task branches/worktrees.

## Changes

1. Extend the control contract with optional typed activity and phase-history objects. Generate outputs, run `q_contracts` `make check`, commit locally and vendor the pinned commit in both consumers.
2. Add observational read contexts and phase timing to the backend reporter. Instrument initial reads and intrabar M1/tick reads, including retries. Use a completion callback for replay progress and retain the old pre-read hook for existing callers. Keep reporting best effort, bounded to the existing transport and independent of numerical execution.
3. Retain same-run telemetry and client freshness timing in the shared backtest status query. Treat terminal responses as authoritative; reset status and result caches on repeat submissions that reuse an ID. Use one activity/coverage formatter for the panel, dock and teaser.
4. Replace the stretched active panel with a content-height summary and accessible details disclosure. Remove the fixed-fill unknown-progress indicator from the backtest panel and dock. Exercise activity and phase history in MSW fixtures.

## Verification

- Observe new contract validation, reporter/read-completion and UI regression tests fail before implementation, then pass.
- Backend: progress reporter, intrabar replay, status HTTP serialization, backtest router/persistence, tick API and Redis/stream progress suites; lint and format touched Python files; `make contracts-check` against the pinned local contract repository.
- Frontend: status delivery/navigation/fallback tests plus existing stream/workbench tests, complete unit suite, typecheck, lint, format, build and contracts drift checks through the canonical CI entrypoint.
- Browser: use the separate MSW web preview; check desktop and narrow rendering, measured versus unknown progress, missing/stale telemetry, disclosure keyboard operation, terminal transition and reduced motion. Keep screenshots and temporary QA scripts outside the repository.
- Review the whole change, correct important findings, commit focused changes, then use `./work board set Q-108 in-review` with results and remaining limitations. Do not push or merge. The new contract commit must be pushed with consumers by the human finishing Q-108.

## Implementation decisions

Retain kernel execution, data-provider selection, read/retry behavior and trading outputs. Historical coverage can jump; the display explains its meaning rather than smoothing it. Phase boundaries and read boundaries bypass the replay throttle so pending reads remain observable. No new persistence table or stream contract is needed. Visual inspection was stopped at the human's request; the human will perform the final visual review.
