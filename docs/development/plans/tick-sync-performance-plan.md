# Tick sync performance

Approved directly by the user for `development`, without a board issue.

## Design and implementation

1. Add `/v1/ticks/page` in `q_contracts` and pin that commit in the backend.
   Use raw millisecond bounds and a forward cursor over `COPY_TICKS_ALL`.
   Replay the cursor's second, filter its prefix and withhold the complete final
   timestamp group of saturated batches. Expand dense batches only within the
   one-million-row native cap; reject source errors and failure to advance.
2. Replace daily double downloads with one scan per missing interval. Reuse the
   first synchronization response, follow actual ticks across empty dates and
   resolve fully stored ranges locally without contacting the gateway.
3. Overlap one serial downloader with incremental daily Parquet writes. Bound
   the queue to two pages. Preserve tick order and duplicates; atomically commit
   a day only after the successful cursor passes its exclusive end. Abort
   temporary files and drain an outstanding request before another interval.
4. Expose interval, received rows, throughput, completed sessions, failure reasons
   and returned coverage. Retain `unsettled` as an empty compatibility field.
   Require an updated gateway rather than silently using the old loop.
5. Compare legacy and streaming paths in temporary stores, checking exact tick
   equality. Measure a populated day, five weekdays and optional known-empty
   history. Target at least 2× for five days and under 10 seconds for a warm day;
   profile the remaining native cost if either target is missed.

Native MT5 requests remain serialized. Separate Python workers cannot guarantee
parallel terminal IPC; health probes use established state during occupied data
lanes. Symbol selection is reused within the paging worker's lifetime.

## Verification

- Cursor conservation, duplicate groups, second replay, dense-group bounds,
  source errors, remote protocol validation and actionable legacy-gateway errors.
- Fetch/write overlap, bounded prefetch, atomic session boundaries, disk failure,
  interruption cleanup, producer lifetime and offline repeat sync.
- Tick and derived bar equality; CLI failure exit status and progress lifecycle.
- Focused backend suite: 394 passed, 1 skipped. Existing Pydantic deprecation
  warnings remain. Research documentation builds with warnings treated as errors.
- Contract repository `make check`: 246 passed, 7 skipped, 1 deselected.
  Explicit gateway route parity: 7 passed. Backend `make contracts-check` passes.
- Ruff passes. Repository formatting checks must preserve the unrelated,
  untracked user experiment `experiments/wdo_test.py`, which fails Black.

## Local measurements

Measurements use the existing terminal cache, not a controlled cold cache. The
benchmark records cumulative process peak RSS, not independent per-case peaks.
Neither benchmark path changes existing tick-store sessions.

On 2026-10-10, `WDO$N` for October 7 returned 245,231 identical ticks:

| Path | Requests | Wall time | HTTP time | Decode | Write |
| --- | ---: | ---: | ---: | ---: | ---: |
| Legacy double fetch | 2 | 224.72 s | 224.65 s | 0.030 s | 0.044 s |
| Forward stream | 1 | 112.95 s | 112.88 s | 0.016 s | 0.041 s |

The warm-day target is not met. Nearly all measured time remains inside the
gateway/terminal operation; MT5 native reads in the five-day baseline repeatedly
take about 81 seconds. The five-day comparison was stopped at the user's request
to wrap up; neither its speedup nor the warm-day target is claimed as achieved.
The measured single-day improvement is 1.99×.

The unrelated untracked user experiment fails the repository-wide Black check.
It was temporarily isolated with a path-specific stash for the normal commit
hook and restored unchanged afterward. Contracts commit: `ce24058`.

Reproduce with `examples/research/benchmark_tick_sync.py`; see the research guide
for arguments and cache limitations. Restart an updated gateway with
`./dev restart gateway` from the workspace root before using the new protocol.
