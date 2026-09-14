# Q-020: DuckDB range reads over catalogued datasets

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.7, §9 invariant 6, §10 phase 1](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#47-persistence-boundary-and-direct-parquet-access)  
**Depends on:** Q-017  
**Implementation plan:** [`../plans/Q-020-duckdb-range-reads-over-catalogued-datasets-plan.md`](../plans/Q-020-duckdb-range-reads-over-catalogued-datasets-plan.md)

## Purpose

Since Q-017, a bars or ticks range read asks the catalog which partition files
the current dataset lists for the range, and then loads every one of those files
whole into pandas, concatenates them, and filters the rows in memory. A one-day
read of ticks therefore materializes the whole month, and a one-week read of M1
bars materializes the whole year, before a single row is discarded. The
architecture's phase 1 puts DuckDB over the lake for exactly this. This task
answers backend market-data range reads with DuckDB, over exactly the files the
current manifest lists, so the time filter is applied while the files are
scanned instead of after they are loaded. Reads must return what they return
today. It is the last phase 1 item, and it makes the catalog the only way the
backend addresses lake files for reading, with no directory listing added by a
query engine that is able to list directories on its own.

## Requirements

### What is read

- A bars range read (symbol, timeframe, time window) and a ticks range read
  (symbol, time window) are evaluated by DuckDB over the files of the subject's
  current dataset that the catalog selects for the window, and over no other
  file.
- Files are handed to the query engine as an explicit list of paths from the
  catalog. The engine is never given a directory, a wildcard, or any pattern it
  could expand, and it never lists a directory to find data. A read succeeds on
  a lake whose directories cannot be listed, provided the listed files can be
  opened.
- A path that the query engine would interpret as a pattern is refused with a
  clear error instead of being expanded.
- A subject with no current dataset, or a window that selects no file, returns
  an empty result without starting a query.

### Results equal today's reads

- For the same lake, catalog, subject, and window, bars reads return the same
  bars, field for field and in the same order, as before this task. Ticks reads
  return the same arrays, element for element, with the same dtypes.
- Window bounds keep their current meaning, including both ends being inclusive
  and the current handling of timezone-aware bounds, even where that handling is
  inconsistent between bars and ticks. Correcting it is not this task's decision.
- Partitions of one series whose files differ in physical types (nanosecond and
  microsecond timestamps, integer and nullable floating-point volume or spread
  columns) read as they do today, with missing values surfacing as missing.
- Rows are ordered by time. Where two rows share a time, their order is
  deterministic: listed-file order, then position within the file.
- A tick file whose columns are not exactly the tick columns still fails the read
  with a value error that names the file.
- A listed file that is absent from disk is still skipped, as it is today.
- The public read functions keep their names, parameters, and return types, so
  none of their callers change.

### Engine behavior

- The query engine never installs or loads an extension from the network at run
  time.
- The query engine's session time zone is UTC for every read, regardless of the
  machine's time zone, so no result depends on where the backend runs.
- Reads are safe to run concurrently from multiple threads in one process, and in
  worker processes created by forking a process that has already read.
- The added per-read overhead of the query engine is measured on the fixture lake
  and reported. The time and peak memory of large reads on the real lake are
  measured before and after, and reported.

### What stays as it is

- Writes, ingest, publication, merge, adoption, tombstoning, and the sweep are
  unchanged, including the partition reads the publish path performs to merge
  new rows into a changed partition.
- The catalog's selection of partition files for a window is unchanged.
- The Q-017 fixture lake, its read baseline, and the Q-017 tests are unchanged
  and pass.

## Constraints and non-goals

- **No SQL surface for research.** No endpoint, job, or notebook helper accepts
  SQL, and no DuckDB database file, view, or table mirrors the catalog. A second
  place that describes the lake is the thing Q-017 removed.
- **No change to the provider tick cache.** The MT5 and remote-gateway tick cache
  stores whole fetch results keyed by a request digest, outside the market-data
  lake and outside the catalog, and it has no range read to push down. Routing it
  through DuckDB would mean reading uncatalogued files.
- **No change to run-artifact reads** under the analytical lake (backtest,
  walk-forward, search, feature matrix). They are not catalogued datasets.
- **No columnar bars API.** Bars reads still return one model object per bar,
  because every caller consumes them that way. A columnar bars path belongs with
  the `q_core` bar frame work (Q-025 and the batch 05 kernels), not here.
- **No correction of timezone-aware bound handling** and **no change to how a
  listed but missing file is treated.** Both are pinned by tests as they are, so
  that this change is provably behavior-preserving. Changing either is a separate,
  deliberate task.
- **No DuckDB on the write path.** The publish merge keeps its pandas
  deduplication, because changing it would mix a data-quality decision into a
  query-engine change.
- **No checksum verification on read and no manifest-driven reader outside
  `q_backend`.** Manifest-verified Parquet reads belong to `q-io` in phase 3,
  with `q_terminal` as their first real reader.
- **No Rust, and no `q_core` dependency.**
- **No new partitioning, sorting, row-group sizing, or statistics on written
  files** to make pushdown more effective. That would rewrite files, which Q-017
  forbids for published datasets.

## Acceptance criteria

### Agent-verifiable

1. The Q-017 fixture read baseline is reproduced exactly by the DuckDB reads over
   the adopted fixture lake, and the Q-017 catalog and local-store tests pass
   unmodified.
2. An edge-case fixture lake, with its expected reads recorded from the pandas
   implementation before the switch, is reproduced exactly afterward. It covers
   mixed nanosecond and microsecond timestamp partitions, a partition with
   missing spread and real volume, an unsorted partition, inclusive bounds exactly
   on bar and tick times, a window crossing a year and a month boundary, a window
   inside a listed partition that matches no row, a reversed window, a subject
   with no dataset, timezone-aware bounds for bars and ticks, a timezone-aware
   time column, and a listed file missing from disk.
3. Reads succeed over a copy of the fixture lake whose directories are made
   unlistable but traversable, and the same reads fail if the file list is
   replaced by a directory pattern, which shows that the test would detect a
   listing.
4. The query plans of a bars read and a ticks read show the time-window filter
   applied inside the Parquet scan, not after it.
5. A path containing a pattern character is refused before any query runs.
6. A tick file with an extra column fails the read with a value error that names
   the file.
7. A fresh query session reports UTC as its time zone while the process time
   zone is set to another zone, and extension auto-install and auto-load are off.
8. Concurrent reads from eight threads return the baseline results, and a read in
   a forked child after a read in the parent returns the baseline results.
9. The median per-read time over 50 fixture reads is measured before and after
   the switch, and both values are reported.
10. The full validation suite passes in `q_backend`.

### Human-verifiable

1. Every current dataset on the real lake is read over its full range and over
   its last calendar month on `development` and on the task branch, and the
   per-read row counts and content digests are identical. Wall time and peak
   memory for the three largest tick reads are reported as medians of five runs
   on each side.
   Command: `git -C /home/gui/projects/q/q_backend worktree add /tmp/q020-dev development && (cd /tmp/q020-dev && uv run python - --runs 5 < /home/gui/projects/q/q_backend/scripts/lake_read_digest.py > /tmp/reads-dev.json) && (cd /home/gui/projects/q/q_backend && uv run python scripts/lake_read_digest.py --runs 5 > /tmp/reads-q020.json) && uv run --project /home/gui/projects/q/q_backend python /home/gui/projects/q/q_backend/scripts/lake_read_digest.py --compare /tmp/reads-dev.json /tmp/reads-q020.json`
2. A candle backtest and a tick backtest over catalogued series are run with the
   same configuration on `development` and on the task branch, and their headline
   metrics are identical.
   Command: `pnpm tauri:dev` (Backtests workspace, same config on both runs)
