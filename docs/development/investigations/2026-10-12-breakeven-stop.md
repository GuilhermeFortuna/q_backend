# Breakeven Stop: latest Research backtest investigation

Inspected the local Research API's saved run configuration and the persisted trade and market
data artifacts for run `27982bcf-ac99-4ec6-8ec4-e8abceb4e396`, completed
2026-10-12 at 03:45:43 UTC. This was BollingerReversion on WSP$N, H1, with 83 trades.

The run set `breakeven_trigger_pct=0.02` (2%), `breakeven_offset_pct=0`, and disabled
all other catalog stops. Its 27 losing trades included 26 that never reached 2% favorable
movement. Those positions had no protective exit until the strategy signal closed them.
Of all 83 trades, only five reached the activation threshold. Exit reasons were 82 `SIGNAL`
and one `breakeven`.

Favorable movement was measured over bars from the entry fill through the candle preceding
the exit fill, converting stored market-data UTC timestamps to the trades' naive Brasília
timestamps. Long movement is `(max(high) - entry) / entry`; short movement is
`(entry - min(low)) / entry`.

| Entry (Brasília) | Side | Entry price | Best favorable move | Net PnL | Exit reason |
|---|---|---:|---:|---:|---|
| 2022-06-09 17:00 | Long | 4016.25 | 0.1805% | -2830.000 | SIGNAL |
| 2026-06-05 11:00 | Long | 7513.25 | 0.2795% | -1795.625 | SIGNAL |
| 2022-09-21 17:00 | Long | 3804.50 | 0.4731% | -1392.500 | SIGNAL |

The remaining losing trade entered long at 3731.25 on 2022-11-03 10:00. Its high reached
3807.25 on 2022-11-04 11:00, arming breakeven. The 12:00 candle fell to 3726.50, triggering
the stop. The historical engine filled at the 13:00 open of 3727.25, losing four points ×
12.5 point value plus five commission, or 55. This stop did activate; next-open execution
and costs explain the loss.

The instrument metadata endpoint reported `trade_tick_size=0.25`. The requested new default
of 100 emergency ticks therefore means 25 price points, independently of the run's 12.5
financial point value. This distance is a stop trigger, not a guaranteed maximum fill loss.

The change introduces tick-ordered activation and stops, with emergency protection before
activation. It does not alter historical results or claim an improved backtest outcome:
re-evaluation requires historical ticks consistent with the original candles. Release and
pin the matching `q_core` capability before restarting the Research stack.

## Verification

The Rust workspace's `make test`, `make fmt-check` and `make lint` passed. All 93 backend
golden backtests passed. An isolated newly built wheel passed 16 dedicated backend tests,
including stack-job execution, actual sliced optimization runners with injected and default
providers, fill timestamps, costs, default normalization and missing/mismatched history.
The follow-up selection covering optimization, ML comparisons, Research and publishing passed
65 tests. Independent review found and verified fixes for sliced-provider and ML replay gaps.

The broad backend non-integration suite reported 2658 passes and 13 skips. Its synthetic-bar
discovery persistence test initially sampled intrabar breakeven without tick history; it was
restricted to bar exits and passed on rerun. Seven remaining failures reproduce on an unchanged
baseline copy with the original engine, each reporting unavailable PostgreSQL at port 5434:

- `tests/optimization/test_genetic_search.py::test_select_search_orchestrator_returns_genetic_orchestrator`
- `tests/optimization/test_genetic_search.py::test_single_get_ohlcv_across_multi_generation_search`
- `tests/optimization/test_hypothesis.py::test_genetic_seeding_preserves_diversity`
- `tests/optimization/test_prescreen.py::test_single_get_ohlcv_with_prescreen_and_probe`
- `tests/optimization/test_parallel_genetic.py::test_parallel_smoke_completes_multi_generation_run`
- `tests/optimization/test_parallel_genetic.py::test_parallel_speedup_smoke_not_timing_threshold`
- `tests/api/test_stream_replay.py::test_job_snapshot_route_validates`

Integration and live-gateway tests were excluded; the running Research environment was not
changed to validate this work. Before rollout, release/pin the matching engine, synchronize the
backend environment and restart the Research services through the canonical launcher.
