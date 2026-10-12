# Q-108: Backtest run progress

The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
Owner: `q_backend`; consumers: `q_contracts`, `q_frontend`. Depends on Q-107.
Implementation: [plan](../plans/Q-108-backtest-run-progress-plan.md).

## Purpose

A Research backtest must explain what it is doing while it runs, especially during slow market-data reads. The revised display is a compact summary with expandable details. Unknown work must not look one-third complete, and historical progress must not imply remaining time.

## Reporting contract

`GET /api/v1/backtest/{run_id}` retains its existing status and optional progress fields. Database-only and older responses remain valid with missing/null telemetry. The database remains authoritative for terminal status.

- Phases: `queued`, `loading_data`, `preparing`, `simulating`, `saving`, in order.
- `started_at` is the API acceptance time; `updated_at` is the last published report.
- `bars_total` is the candle dataset size when known. Tick-engine runs do not pretend their tick count is a candle count.
- `progress` is historical coverage through the last completed replay: `(zero_based_bar + 1) / bars_total`, clamped to 0–1 and non-decreasing during simulation. It is null for phases and engines with no measurable extent. Replay requests are sparse and reads vary in duration; coverage is not a time estimate.
- `simulated_until` is the last completed replay candle's open time. `candles_replayed` and `candles_substituted` count completed replay work, never a pending read.
- `message` remains a short human-readable caption for existing stream consumers.
- Optional `activity` contains `kind`, `started_at`, and optional `range_start` / `range_end`. Kinds are `loading_candles`, `loading_ticks`, `computing`, `reading_m1`, `reading_ticks`, `saving`.
- Optional `phase_history` contains ordered `{phase, started_at, finished_at?}` entries. Queue timing begins at acceptance; completed entries retain finish times; the active entry has no finish time.

Publish phases and read boundaries immediately. Report the initial candle/tick read and each intrabar M1/tick read, including retries. Resume computation only after a successful read; report replay completion only after the replay returns. Cache hits do not duplicate completion. Ordinary replay reports retain the 500ms throttle and update state even when publication is skipped.

Reporting is best effort and must not change prices, trades, metrics, retries or provider selection. No database migration, kernel change or stream-envelope change. Generate contracts at the source, pin the contract commit and vendor generated outputs in consumers.

## Research display

Use Q's shared Panel, typography and brass tokens. Render a content-height summary centered horizontally, maximum width 960px; the results area owns scrolling. Keep the current activity prominent, with phases, elapsed time and update freshness visible. Unknown progress uses a small activity indicator, never a filled fraction bar. Numeric simulation progress is labeled **Historical range processed**, with a short explanation that it is not a time estimate.

**Run details** is a keyboard-accessible disclosure, initially closed for each run. Show available candle/replay/substitution counts, last completed replay, requested read range and phase timings. Omit unknown values. Read duration is visible while a reported read is pending.

After ten seconds with no changed `updated_at`, show **No new progress update for …** in any phase. Measure freshness on the client; preserve the observation time in the status query cache across navigation. Silence alone must not imply provider failure or waiting for market data.

A response with no phase displays **Detailed progress unavailable**, without inventing a percentage. Retain the same run's last known details through a missing/null telemetry response and explicitly mark them unavailable/stale. Retained read and active-phase durations freeze at the last server update rather than pretending the worker is still doing that work. New runs reset retained details and disclosure state. A repeated configuration may reuse its run ID; submission resets the status query and removes the old result cache so the new execution is observable. Terminal status overrides retained telemetry. The dock and collapsed teaser use the same activity and explicit historical-coverage meaning. Reduced motion disables spinner and width animations.

## Acceptance and boundary

Verify pending/successful/failed/retried reads, phase timings including queue time, completion-only counters, monotonic coverage, throttling, best-effort publication and unchanged results using fake providers, clocks and Redis. Verify HTTP response serialization and status-to-query-to-workbench delivery. Verify missing/null telemetry, navigation, new-run reset and terminal transitions. Browser QA covers desktop/narrow layouts, disclosure keyboard interaction, read duration, uneven progress and reduced motion with MSW.

Exclude ETA, artificial progress smoothing, cancellation, gateway-health diagnosis and other job kinds. No live GPU, Wine, Docker or desktop run is required for these changes. The original screenshot's absent phase data cannot be attributed to a specific runtime cause without capturing that run; automated coverage verifies the delivery path and explicit fallback behavior.
