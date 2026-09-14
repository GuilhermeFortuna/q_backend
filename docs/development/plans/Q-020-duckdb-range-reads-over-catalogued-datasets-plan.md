# Q-020 implementation plan: DuckDB range reads over catalogued datasets

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-020-duckdb-range-reads-over-catalogued-datasets-spec.md`](../specs/Q-020-duckdb-range-reads-over-catalogued-datasets-spec.md)  
**Depends on:** Q-017

## Current-system context

`market_data/local_store.py` (342 lines) answers range reads through two public
functions. `read_ohlcv` (line 224) normalizes the bounds with `_to_naive_utc`
(line 217), asks `LakeCatalog.files_for_range` (`catalog/service.py` line 311)
for the current dataset's partition paths, and then loads each existing file
whole with `_read_year_parquet` (line 142, `pq.read_table(path).to_pandas()`). It
concatenates the frames, converts a timezone-aware `time` column to naive UTC,
masks `start_ts <= time <= end_ts`, sorts with `sort_values("time")`, and builds
`OHLCV` models in `_dataframe_to_bars` (line 119). `read_ticks_columnar` (line
257) converts the bounds with `_naive_local_to_time_msc`
(`clients/metatrader.py` line 71). It loads each month with
`_read_month_ticks` (line 149), which raises `ValueError("Unexpected tick columns
in {path}")` when the column set differs from `COLUMNAR_TICK_KEYS`. It then masks
on `time_msc` and returns arrays cast through `_TICK_DTYPE_MAP` in
`_dataframe_to_columnar` (line 159). Both paths skip listed files for which
`path.is_file()` is false. `files_for_range` returns paths in
`DatasetFile.ordinal` order, because `Dataset.files` is ordered by ordinal in
`storage/db/catalog_models.py`. Callers reach these functions through
`clients/local.py` (`LocalParquetClient`), `service.py` lines 235 to 256,
`read_through.py` line 68, and `coverage.py`. None of them change.

`tests/fixtures/lake/` holds PETR4 D1 (2024, 2025), WIN$N M15 (2025), and WIN$N
ticks (2025-01, 2025-02). All of them are `timestamp[us]` with integer spread and
no nulls. `expected_reads.json` records three ranges read before Q-017.
`tests/market_data/catalog/test_local_store_rewired.py` compares the reads with
that baseline and patches `Path.glob`, `Path.iterdir`, and `os.listdir` to show
that there is no listing on the read path. Such a patch cannot see a listing
done inside a native library. `duckdb` is not a dependency (`pyproject.toml`
and `uv.lock` have no entry). `market_data/tick_cache.py` `load` reads one whole
provider-cache file by request digest, outside the lake and the catalog. Contrary
to the batch notes, it is not a catalogued range read, and this plan leaves it
alone. The gap is that every range read materializes whole partitions in pandas
before filtering them.

## Interfaces produced

```python
# src/q_backend/market_data/lake_query.py   (new)
PATTERN_CHARACTERS: frozenset[str]            # frozenset("*?[]"): DuckDB glob syntax

class LakePathError(ValueError):
    """A path handed to the query engine is relative or contains a pattern character."""

BAR_COLUMNS: tuple[str, ...]                  # the eight OHLCV columns, in local_store._OHLCV_COLUMNS order

def query_cursor() -> duckdb.DuckDBPyConnection: ...
    """New cursor on this process's in-memory DuckDB instance (created lazily, recreated if os.getpid() changed)."""

def read_bars_table(paths: Sequence[Path], start: datetime, end: datetime) -> pa.Table: ...
    """start/end: naive, already normalized by the caller. Columns BAR_COLUMNS, time as timestamp[us] naive.
    Filter start <= time <= end inside the scan; ORDER BY time, listed-file position, file row number.
    Empty `paths` -> empty table with BAR_COLUMNS and no cursor opened."""

def read_ticks_arrays(paths: Sequence[Path], start_msc: int, end_msc: int) -> dict[str, np.ndarray]: ...
    """Keys COLUMNAR_TICK_KEYS, dtypes _TICK_DTYPE_MAP. Filter start_msc <= time_msc <= end_msc inside the scan;
    ORDER BY time_msc, listed-file position, file row number. Raises ValueError naming the first file whose
    top-level column set != COLUMNAR_TICK_KEYS. Empty `paths` -> _empty_ticks_columnar()."""
```

```python
# src/q_backend/market_data/local_store.py   (changed; public signatures unchanged)
def read_ohlcv(symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]: ...
def read_ticks_columnar(symbol: str, start: datetime, end: datetime) -> dict[str, np.ndarray]: ...
def _table_to_bars(table: pa.Table) -> list[OHLCV]: ...       # null spread/real_volume -> None
# removed: _dataframe_to_bars, _read_year_parquet, _read_month_ticks, _dataframe_to_columnar,
#          and the pyarrow.parquet import
# kept:    _to_naive_utc and the files_for_range call, so bound semantics are untouched
```

```python
# scripts/lake_read_digest.py   (new; runnable as a file or from stdin: `python - [args] < file`)
def planned_reads(inventory: list[dict[str, Any]]) -> list[ReadSpec]: ...   # full range + last calendar month per dataset
def digest_bars(bars: list[OHLCV]) -> str: ...                             # sha256 over model_dump_json lines
def digest_ticks(arrays: dict[str, np.ndarray]) -> str: ...                # sha256 over key, dtype.str, bytes, in key order
def run(runs: int, fixture: Path | None) -> dict[str, Any]: ...            # rows, digest, median wall s, peak RSS KiB (forked child)
def compare(before: Path, after: Path) -> int: ...                         # 0 iff every read's rows and digest match
def main(argv: list[str] | None = None) -> int: ...   # --runs N, --fixture PATH (SQLite catalog over a tmp copy), --compare A B

@dataclass(frozen=True)
class ReadSpec:
    kind: Literal["bars", "ticks"]; symbol: str; timeframe: str; start: datetime; end: datetime
```

```python
# scripts/generate_lake_edge_fixture.py   (new; pyarrow writes, then today's local_store reads)
def write_edge_lake(root: Path) -> None: ...
def record_expected_reads(root: Path) -> dict[str, Any]: ...   # each case: bars/ticks payload, or {"raises": "<ExceptionType>"}
```

```
pyproject.toml                                   dependencies += "duckdb>=1.5.5"; uv.lock relocked
tests/fixtures/lake_edge/                        legacy-named partitions + expected_reads.json (see step 3)
tests/market_data/lake_query/                    new test package
```

## Implementation decisions

- **DuckDB receives the catalog's paths as a list parameter to `read_parquet`,
  and each path is checked before the query.** A path must be absolute and must
  contain none of `*`, `?`, `[`, `]`, or the query raises `LakePathError`. DuckDB
  treats those characters in a path string as a glob and expands it by listing
  the directory. That is a listing Python patches cannot see, and it would
  violate invariant 6 silently. Written and adopted paths come from
  `_slug_symbol`, which already maps these characters to `_`, so the check never
  fires on real data and exists only to turn a future mistake into an error.

- **Invariant 6 is proven by making the lake's directories unlistable, with a
  negative control.** The test copies the fixture and sets every directory to
  mode `0o311` (write and traverse, no read), so `opendir` fails while opening a
  file by name succeeds. The reads must succeed. The same session asked for
  `<dir>/*.parquet` must return no file or fail, which shows the test would catch
  a listing. The test skips when `os.geteuid() == 0`, because root ignores
  directory modes. This was checked with DuckDB 1.5.5: the explicit list read the
  files, and the pattern failed with "No files found". The existing
  `Path.glob`/`os.listdir` patch test stays, because it still guards the Python
  side.

- **One in-memory DuckDB instance per process, with a new `cursor()` per read.**
  The instance is created lazily and recreated when `os.getpid()` differs from
  the pid that created it. Measured with DuckDB 1.5.5 on this machine, a fresh
  `duckdb.connect()` costs about 6.2 ms and a cursor on an existing instance about
  0.16 ms. Reads run in FastAPI's threadpool, and some callers read in loops. One
  cursor per read gives each thread its own connection, which DuckDB requires for
  concurrent use. The pid check stops a Dramatiq or `ProcessPoolExecutor` child
  from using an instance inherited across `fork`, which is unsafe.

- **The instance is created with `autoinstall_known_extensions=false` and
  `autoload_known_extensions=false`, and it runs `SET GLOBAL TimeZone = 'UTC'`.**
  The Parquet and ICU extensions are statically loaded in the Python wheel, so
  nothing needs to be fetched. A backend that downloaded code at read time would
  fail offline and would run unreviewed binaries. `SET TimeZone` without `GLOBAL`
  is session-scoped, and a cursor does not inherit it: a cursor on an instance
  where only the creating session was set reported the system zone,
  `America/Bahia`. Without `GLOBAL`, a timezone-aware `time` column would convert
  differently on different machines. Today's pandas path converts such a column
  to UTC.

- **Results are fetched as Arrow, not pandas.** pandas 3 is pinned, and Arrow
  fetch keeps the dtypes explicit. Tick arrays come out of Arrow columns with
  one contiguous copy each and are cast through `_TICK_DTYPE_MAP`, as today. Bars
  still become `OHLCV` models, because the public return type is `list[OHLCV]`.
  The `time` column is cast to `TIMESTAMP` (microseconds) in SQL, so every value
  is a Python `datetime`, whether a partition was written as `timestamp[ns]` by
  pandas or as `timestamp[us]`. Lake bars are whole seconds, so the truncation
  changes no value.

- **Bars use `union_by_name=true`, and ticks do not.** Real bar partitions
  written by pandas may carry spread or real volume as `double` with NaN beside
  `int64` partitions. Today `pd.concat` promotes these to float, and
  `_dataframe_to_bars` turns NaN into `None`. DuckDB's union promotes to `DOUBLE`
  with NULL, which `_table_to_bars` turns into `None`. Tick files must have
  exactly `COLUMNAR_TICK_KEYS`. `read_ticks_arrays` first reads the files'
  top-level column names from `parquet_schema(list)`, which reads footers only,
  and raises the same `ValueError` text for the first mismatching file. A union
  would silently fill a missing column with NULL.

- **Filters compare raw columns with bound parameters, and a test asserts
  pushdown with `EXPLAIN`.** For bars the filter is `time BETWEEN $start AND $end`
  with naive timestamps. For ticks it is `time_msc BETWEEN $start AND $end` with
  `BIGINT` parameters. Wrapping the filter column in a cast or a function can stop
  DuckDB from pushing it into the Parquet scan, and then this task would pay for
  a query engine and get pandas' behavior. The test asserts that the `EXPLAIN`
  text shows the filter inside the Parquet scan node, for one bars read and one
  ticks read. For a timezone-aware `time` column, the conversion to naive UTC is
  applied in the projection, and the comparison is written so the baseline in
  step 3 holds. That case may lose pushdown, which is acceptable because no lake
  writer produces such files.

- **The order is `time` (or `time_msc`), then the file's position in the listed
  paths, then `file_row_number`.** Year and month partitioning and the publish
  merge's `drop_duplicates` mean ties do not occur in data this backend wrote.
  pandas' default `sort_values` is not stable, so today's order for ties is
  undefined. A total order makes DuckDB's parallel scan deterministic.

- **`local_store` keeps its bound normalization, its `files_for_range` call, and
  its `path.is_file()` filter, and replaces only loading, filtering, and
  sorting.** The spec pins today's semantics for timezone-aware bounds.
  `read_ohlcv` compares aware bounds as naive UTC against Brasília values.
  `files_for_range` treats those naive UTC values as Brasília again.
  `read_ticks_columnar` converts aware bounds to Brasília. It also pins today's
  skipping of a missing listed file. The `is_file` check is a `stat` of a listed
  path, not a listing. A start later than the end still returns empty before the
  catalog is asked.

- **The regression baseline for edge cases is recorded from today's code into a
  separate fixture, `tests/fixtures/lake_edge/`, before `duckdb` is added.**
  Adding series to `tests/fixtures/lake/` would change the Q-017 adoption,
  inventory, and sweep counts, and the spec requires those tests to stay
  unmodified. The generator writes the partitions directly with pyarrow, because
  the publish path normalizes exactly the physical variety that must be pinned.
  It records each case as rows or as the exception type raised, so a case that
  fails today must fail the same way afterward.

- **The edge lake's contents:** `EDGE3` D1 with a `2023.parquet` file
  (`timestamp[ns]`, int64 spread and real volume, sorted) and a `2024.parquet`
  file (`timestamp[us]`, double spread and real volume with two nulls, rows
  unsorted), and `EDGETZ` H1 with one `2024.parquet` file whose `time` is
  `timestamp[us, tz=America/Sao_Paulo]`. It also has `EDGE3` ticks with
  `2024-12.parquet` and `2025-01.parquet`, holding ticks at millisecond offsets
  around midnight on the month boundary. The cases are: bars across 2023→2024
  with inclusive bounds on bar times; a window inside 2024 matching no row; a
  reversed window; an unknown symbol; UTC-aware bars bounds that straddle the
  year boundary; the `EDGETZ` full range; ticks with bounds exactly on the first
  and last tick; ticks with sub-millisecond bounds; UTC-aware tick bounds; and
  bars with the 2023 file deleted after adoption.

- **The digest script uses only `local_store` public functions and
  `list_inventory`, and it runs from stdin.** The spec's first human check runs
  the same script on `development`, where `lake_query` does not exist. Piping
  the branch's file into `development`'s interpreter compares the two
  implementations with one measuring program. Peak RSS is measured in a forked
  child per read, because `ru_maxrss` is a process high-water mark and cannot be
  reset between reads in one process.

- **`duckdb>=1.5.5`, the current release when this plan was written, is added
  with the repository's `>=` pin style, and `uv.lock` fixes the exact build.**
  The Dockerfile runs `uv sync --frozen`, so the lock is the reproducibility
  boundary, as for `pyarrow`.

## Ordered implementation

- [x] 1. Work on the branch `Q-020-duckdb-range-reads-over-catalogued-datasets` in
   `q_backend`, created from `development` by `./work start`.
- [x] 2. Write failing tests in `tests/market_data/lake_query/test_lake_read_digest.py`
   that load `scripts/lake_read_digest.py` by path. `planned_reads` over the
   Q-017 fixture inventory yields 6 reads (3 datasets × full range and last
   month). `run(runs=1, fixture=tests/fixtures/lake)` reports `rows` 10, 12, and
   10 for the full ranges. `digest_ticks` changes when one `flags` element
   changes. `compare` returns 0 for a file compared with itself and 1 when one
   digest differs. Confirm they fail, implement the script, and confirm they
   pass. Commit.
- [x] 3. Write `scripts/generate_lake_edge_fixture.py`, run it, and commit
   `tests/fixtures/lake_edge/` with its `expected_reads.json`. Add
   `tests/market_data/lake_query/test_edge_reads.py`, which copies the edge lake,
   adopts it into a SQLite `LakeCatalog`, sets `catalog_service._catalog_instance`
   as `test_local_store_rewired.py` does, and asserts every case equals the
   recorded payload or raises the recorded type. The generator is the only way to rewrite the baseline. The test passes on today's code, because this step is the
   baseline. Record the before measurement with
   `uv run python scripts/lake_read_digest.py --fixture tests/fixtures/lake --runs 50`
   in the commit message. Commit.
- [x] 4. Add `duckdb>=1.5.5` to `pyproject.toml`, run `uv lock`, and confirm that
   `uv run python -c "import duckdb; print(duckdb.__version__)"` works and that
   `uv run pytest tests/market_data -q` is still green. Commit.
5. Write failing tests in `tests/market_data/lake_query/test_lake_query.py`:
   - With `TZ=Asia/Tokyo` and `time.tzset()`, and the module instance reset,
     `query_cursor()` returns `UTC` for `current_setting('TimeZone')`, and a
     second cursor also returns `UTC`.
   - `current_setting('autoinstall_known_extensions')` and
     `current_setting('autoload_known_extensions')` are both false.
   - `read_bars_table([Path('/lake/ohlcv/A*/D1/2024.parquet')], …)` raises
     `LakePathError`, with `query_cursor` patched to fail if called. A relative
     path raises `LakePathError`.
   - `read_bars_table([], …)` returns 0 rows with `BAR_COLUMNS`, and
     `read_ticks_arrays([], 0, 1)` equals `_empty_ticks_columnar()`, without a
     cursor being opened.
   - Over the fixture's two PETR4 files, the `EXPLAIN` text for the bars query
     shows the `time` filter inside the Parquet scan node. The ticks query shows
     the `time_msc` filter.
   - Two tmp files that each contain a bar at `2025-01-02 10:00` with different
     `close` values return that bar in listed-file order, and reversing the list
     reverses it.
   - A tick file with an extra `foo` column raises `ValueError` whose message
     contains that file's path.
   - A PID change (monkeypatched `os.getpid`) causes a new instance.

   Confirm they fail, implement `lake_query.py`, and confirm they pass. Commit.
6. Write failing tests in `tests/market_data/lake_query/test_local_store_duckdb.py`:
   - With `pyarrow.parquet.read_table` patched to raise, `read_ohlcv` and
     `read_ticks_columnar` over the adopted Q-017 fixture still return the
     `expected_reads.json` payloads.
   - On a copy whose directories are chmod `0o311`, the same reads succeed. As
     the negative control, `read_parquet` of `<ohlcv dir>/*.parquet` on
     `query_cursor()` returns no rows or raises. The test skips as root.
   - Eight threads each run all three baseline reads 20 times and all results
     equal the baseline.
   - A `multiprocessing.get_context("fork")` child started after a parent read
     returns the baseline ticks through a queue.

   Confirm that the first two fail on today's code. The thread and fork tests
   may pass already, and they guard the switch. Rewire `read_ohlcv` and
   `read_ticks_columnar` onto `lake_query`, add `_table_to_bars`, and delete the
   four pandas read helpers and the `pq` import. Confirm that every test in
   `tests/market_data/lake_query`, `tests/market_data/catalog`, and
   `tests/market_data` passes. Commit.
7. Regression: confirm that
   `git diff development --stat -- tests/market_data/catalog tests/market_data/test_local_store.py tests/market_data/test_local_tick_store.py tests/market_data/test_local_provider.py tests/fixtures/lake`
   is empty, and that `test_edge_reads.py` passes with
   `tests/fixtures/lake_edge` unchanged since step 3. Fix code, never baselines. Commit any fixes.
8. Measurement: run
   `uv run python scripts/lake_read_digest.py --fixture tests/fixtures/lake --runs 50`
   on the branch, and record the median per-read time next to the step 3 value.
9. Human step, matching human-verifiable criterion 1: with `Q_MARKET_DATA_ROOT`
   and the database settings exported in the shell, run the spec's criterion 1
   command. Confirm that `--compare` exits 0, and report median wall time and
   peak RSS for the three largest tick reads on each side.
10. Human step, matching human-verifiable criterion 2: run the same candle
    backtest and the same tick backtest from the research UI on `development` and
    on the branch, and compare the headline metrics.
11. Run the full validation suite, `scripts/ci.sh`. Commit.

## Validation

- **Unit:** path checks and pattern refusal; empty inputs open no cursor; UTC
  `TimeZone` on every cursor under a non-UTC process zone; extension
  auto-install and auto-load off; tie order; tick column-set error; per-pid
  instance; filter pushdown visible in `EXPLAIN`; digest and compare behavior.
- **Integration:** `local_store` reads through DuckDB with
  `pq.read_table` unavailable; reads over unlistable directories with a
  pattern negative control; eight-thread concurrency; a forked child after a
  parent read.
- **Regression:** Q-017 `expected_reads.json` equality; edge-lake
  `expected_reads.json` recorded from the pandas implementation in step 3;
  Q-017 catalog and local-store tests unmodified (step 7).
- **Manual:** step 10, candle and tick backtest headline metrics on
  `development` and on the branch.
- **Measurement:** fixture per-read median over 50 runs before (step 3) and
  after (step 8); real-lake digests, median wall time, and peak RSS over five
  runs on each side (step 9).
- **Pins:** `CONTRACTS_REV` is unchanged, and no contract is touched.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/market_data/lake_query tests/market_data/catalog tests/market_data -v
uv run python scripts/lake_read_digest.py --fixture tests/fixtures/lake --runs 50

# human, against the real lake (Q_MARKET_DATA_ROOT and database settings exported)
git -C /home/gui/projects/q/q_backend worktree add /tmp/q020-dev development
(cd /tmp/q020-dev && uv run python - --runs 5 < /home/gui/projects/q/q_backend/scripts/lake_read_digest.py > /tmp/reads-dev.json)
(cd /home/gui/projects/q/q_backend && uv run python scripts/lake_read_digest.py --runs 5 > /tmp/reads-q020.json)
uv run --project /home/gui/projects/q/q_backend python /home/gui/projects/q/q_backend/scripts/lake_read_digest.py --compare /tmp/reads-dev.json /tmp/reads-q020.json
```

## Handoff

Report the locked DuckDB version. Report the fixture per-read median before and
after over 50 runs, and the resulting per-read overhead in milliseconds. Report
the list of edge cases and confirm that each matched its pandas-recorded
baseline, naming any case recorded as a raised exception. Confirm that the
unlistable-directory test ran (not skipped) and that its negative control
failed as expected. Paste the `EXPLAIN` fragment showing the pushed-down filter
for bars and ticks. Confirm the step 7 diff was empty. From the human steps,
report the number of datasets and reads compared, the `--compare` exit code, the
median wall time and peak RSS of the three largest tick reads on `development`
and on the branch, and the two backtests' headline metrics on each side.
