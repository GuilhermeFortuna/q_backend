# Q-017 implementation plan: Dataset catalog over the existing lake

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-017-dataset-catalog-over-the-existing-lake-spec.md`](../specs/Q-017-dataset-catalog-over-the-existing-lake-spec.md)  
**Depends on:** Q-006

## Current-system context

`market_data/local_store.py` (608 lines) owns the market-data lake under
`market_data_root()`. The root is `Q_MARKET_DATA_ROOT`, or
`settings.market_data_root` (default `data/market`) resolved against the source
tree. Bars live at `ohlcv/{slug}/{TF}/{YYYY}.parquet` and ticks at
`ticks/{slug}/{YYYY-MM}.parquet`. `write_ohlcv` and `write_ticks` read the
existing partition, merge and deduplicate it in pandas (`_merge_year_frame`,
`_merge_tick_frame`), and write it back to the same path with `pq.write_table`,
with no temporary file and no fsync. `_summarize_series` and `_summarize_ticks`
then glob the directory and reread every partition to update one entry in
`catalog.json`. `delete_ohlcv` and `delete_ticks` call `shutil.rmtree`. Callers
are `api/storage_jobs.py` (ingest, lines 150 and 215), `api/routers/storage.py`
(inventory and delete), `api/routers/system.py` (root and inventory count),
`market_data/service.py` (fetch-through writes and local reads),
`clients/local.py`, `routing.py`, `coverage.py`, `api_service.py`, and
`read_through.py`. The local store has no database access. Its tests in
`tests/market_data/test_local_store.py` and `test_local_tick_store.py` point
`Q_MARKET_DATA_ROOT` at `tmp_path`.

`q_contracts` at the pinned commit provides `schema/catalog/dataset-manifest.schema.json`,
`lifecycle.yaml` (`publishing → published → tombstoned → deleted`), and the
generated frozen dataclass `q_contracts.catalog.DatasetManifest`, which is
already vendored at `contracts/catalog.py`. The manifest JSON schema itself is
not vendored. `make contracts` copies only `schema/api/arrow/*.schema.json`.
`q_contracts/tools/describe_dataset.py` shows how a manifest is derived from
existing files. It formats naive timestamps with a `Z` suffix without converting
them, which mislabels Brasília wall-clock values as UTC, so this plan does not
reuse it. `schema/catalog/FINDINGS.md` records Findings 1 to 3 (in-place
mutation, unversioned catalog, no checksums) as migration tasks. The database
layer uses SQLAlchemy 2 models in `storage/db/`, module-level repository
functions that take a `Session`, Alembic revisions up to `20260913_0018`, and
SQLite `create_all` databases in tests. The gap is that nothing assigns dataset
identity, and nothing stops a published file from being rewritten.

## Interfaces produced

```python
# src/q_backend/storage/db/catalog_models.py
class Dataset(Base):
    __tablename__ = "lake_datasets"
    dataset_id: Mapped[uuid.UUID]            # PK, uuid4 at publish
    kind: Mapped[str]                        # "bars" | "ticks"
    symbol: Mapped[str]
    timeframe: Mapped[str]                   # "" for ticks, so the unique key has no NULL
    version: Mapped[int]                     # >= 1; unique with (kind, symbol, timeframe)
    supersedes: Mapped[uuid.UUID | None]     # FK lake_datasets.dataset_id
    state: Mapped[str]                       # lifecycle.yaml states; CHECK constraint
    published_at: Mapped[datetime]           # timestamptz
    checksum_algorithm: Mapped[str]          # "sha256"
    arrow_schema: Mapped[dict]               # JSONB, manifest arrow_schema form
    row_count: Mapped[int]
    time_start: Mapped[datetime]             # timestamptz, UTC instant
    time_end: Mapped[datetime]
    tombstoned_at: Mapped[datetime | None]
    deletable_after: Mapped[datetime | None]
    # partial unique index: one state='published' row per (kind, symbol, timeframe)

class DatasetFile(Base):
    __tablename__ = "lake_dataset_files"
    dataset_id: Mapped[uuid.UUID]            # PK part, FK
    path: Mapped[str]                        # PK part; lake-root-relative POSIX path
    ordinal: Mapped[int]                     # manifest order: partition time order
    size_bytes: Mapped[int]
    checksum: Mapped[str]                    # lowercase hex
    partition_start: Mapped[datetime]        # backend-only; not in the manifest
    partition_end: Mapped[datetime]
```

```python
# alembic/versions/20260915_0019_lake_dataset_catalog.py
revision = "20260915_0019"; down_revision = "20260913_0018"
```

```python
# src/q_backend/market_data/catalog/partitions.py
@dataclass(frozen=True)
class Subject:
    kind: Literal["bars", "ticks"]
    symbol: str
    timeframe: str                           # "" for ticks

@dataclass(frozen=True)
class WrittenFile:
    path: str                                # root-relative
    size_bytes: int
    checksum: str
    rows: int
    partition_start: datetime                # naive Brasília, from the data
    partition_end: datetime
    arrow_schema: dict[str, Any]

def write_partition_immutable(root: Path, subject: Subject, partition_key: str, table: pa.Table) -> WrittenFile: ...
    """Temp file in the target directory, fsync, content-addressed rename, fsync the directory."""
def describe_existing_partition(root: Path, rel_path: str) -> WrittenFile: ...
def lake_arrow_schema(schema: pa.Schema) -> dict[str, Any]: ...
```

```python
# src/q_backend/market_data/catalog/repository.py
def current_dataset(session: Session, subject: Subject) -> Dataset | None: ...
def get_dataset(session: Session, dataset_id: uuid.UUID) -> Dataset | None: ...
def list_current_datasets(session: Session, *, kind: str | None = None, symbol: str | None = None) -> list[Dataset]: ...
def lock_subject(session: Session, subject: Subject) -> None: ...          # pg_advisory_xact_lock; no-op on SQLite
def publish_version(session: Session, subject: Subject, files: Sequence[WrittenFile],
                    *, now: datetime, grace: timedelta) -> Dataset: ...     # inserts v(n+1), tombstones v(n)
def tombstone_current(session: Session, subject: Subject, *, now: datetime, grace: timedelta) -> Dataset | None: ...
def sweepable(session: Session, *, now: datetime) -> list[Dataset]: ...
def live_paths(session: Session) -> set[str]: ...                           # paths listed by published or in-grace datasets
def to_manifest(dataset: Dataset) -> DatasetManifest: ...                   # vendored q_contracts.catalog type
```

```python
# src/q_backend/market_data/catalog/service.py
class LakeCatalog:
    def __init__(self, session_factory: Callable[[], Session], root: Path, grace: timedelta, clock: Callable[[], datetime]) -> None: ...
    def publish_bars(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Dataset: ...
    def publish_ticks(self, symbol: str, arrays: Mapping[str, np.ndarray]) -> Dataset: ...
    def files_for_range(self, subject: Subject, start: datetime, end: datetime) -> list[Path]: ...
    def delete_subject(self, subject: Subject) -> Dataset | None: ...
    def adopt(self) -> AdoptionReport: ...
    def sweep(self) -> SweepReport: ...

@dataclass(frozen=True)
class AdoptionReport:
    adopted: list[Subject]; skipped_existing: list[Subject]; files: int; bytes: int

@dataclass(frozen=True)
class SweepReport:
    datasets_deleted: int; files_deleted: int; bytes_freed: int; unreferenced_files: list[str]

def get_lake_catalog() -> LakeCatalog: ...                                  # process-wide, built from settings
```

```python
# src/q_backend/market_data/local_store.py  (changed; public signatures unchanged)
def write_ohlcv(symbol: str, timeframe: str, bars: list[OHLCV]) -> dict[str, Any]: ...   # publishes via LakeCatalog
def write_ticks(symbol: str, arrays: dict[str, np.ndarray]) -> dict[str, Any]: ...
def read_ohlcv(symbol: str, timeframe: str, start: datetime, end: datetime) -> list[OHLCV]: ...
def read_ticks_columnar(symbol: str, start: datetime, end: datetime) -> dict[str, np.ndarray]: ...
def delete_ohlcv(symbol: str, timeframe: str) -> None: ...                  # tombstones
def delete_ticks(symbol: str) -> None: ...
def list_inventory() -> list[dict[str, Any]]: ...                           # same item shape, from the catalog
# removed: _read_catalog, _write_catalog, _upsert_catalog_entry, _remove_catalog_entry, _catalog_path
```

```python
# src/q_backend/api/schemas/catalog.py
class ManifestFile(BaseModel): path: str; size_bytes: int; checksum: str
class DatasetManifestResponse(BaseModel): ...    # field-for-field the contract manifest
class DatasetListResponse(BaseModel):
    root: str                                    # absolute market-data lake root
    datasets: list[DatasetManifestResponse]

# src/q_backend/api/routers/catalog.py
GET /api/v1/catalog/datasets?kind=&symbol=&timeframe=   -> DatasetListResponse   (current datasets only)
GET /api/v1/catalog/datasets/{dataset_id}                -> DatasetManifestResponse (any state; 404 unknown)
# both: 503 ErrorResponse(code="catalog_unavailable")

# src/q_backend/storage/settings.py  (addition)
catalog_tombstone_grace_s: int = 604_800

# src/q_backend/cli/q_catalog.py   (project script "q-catalog")
def main(argv: list[str] | None = None) -> int: ...   # subcommands: adopt, sweep [--dry-run], show <dataset_id>
```

```
Makefile                                         contracts / contracts-check also vendor schema/catalog/dataset-manifest.schema.json
tests/fixtures/lake/                             two bars series (PETR4 D1 2024 and 2025, WIN$N M15 2025) and one ticks series (two months)
tests/market_data/catalog/                       new test package
q_contracts: schema/api/openapi.yaml             recaptured
q_contracts: schema/catalog/FINDINGS.md          Findings 1-3 resolved by Q-017; describe_dataset time-range finding added
q_contracts: COMPAT.md                           q_backend row
```

## Implementation decisions

- **New partition files are content-addressed: `{YYYY}.{sha256[:16]}.parquet`
  and `{YYYY-MM}.{sha256[:16]}.parquet`, beside today's files.** A name derived
  from the bytes can never refer to different bytes, so "never rewritten" holds
  by construction and not only by discipline. A retried publish after a crash
  produces the same name and reuses the file instead of creating a duplicate.
  Version-numbered names (`2025.v3.parquet`) would need the version before the
  file is written, which ties the file write to the transaction and makes a
  crash-retry write a second copy.

- **Adoption catalogs today's `{YYYY}.parquet` files at their existing paths as
  version 1, and they are never written again.** Once catalogued, a legacy file
  is as immutable as a content-addressed one, because no code path writes to a
  catalogued path. This is what makes adoption "a cataloguing exercise rather
  than a migration", as Q-005 requires, and it means the real lake never has its
  bytes touched.

- **Unchanged partitions are listed again by the new version, so files are
  shared across versions.** Re-ingesting 2025 bars must not copy ten years of
  history. The cost is that deletion must be reference-aware. The sweep deletes a
  path only if no dataset in `published` or in-grace `tombstoned` state lists it,
  computed in the same transaction that marks datasets `deleted`.

- **A changed partition is the merge of the current version's partition with
  the incoming rows, with the same `drop_duplicates(keep="last")` semantics as
  today.** Keeping the existing merge rules is what makes reads return the same
  rows as before. Changing deduplication semantics here would mix a data-quality
  decision into a storage change.

- **Write order is: temp file in the destination directory, `fsync(file)`,
  `os.replace` to the content-addressed name, `fsync(directory)`, and only then
  the catalog transaction.** The temp file must be in the same directory
  because `rename` is only atomic within one filesystem. The directory fsync makes
  the rename durable, so a committed dataset never lists a name that a power loss
  could undo. Committing the catalog last means a crash leaves at worst an
  unreferenced file, never a dataset that lists a missing file.

- **`publish_version` takes `pg_advisory_xact_lock` on a 64-bit hash of the
  subject before reading the current version, and holds it through the merge,
  the file writes, and the commit.** Without the lock, two ingests both read
  version 2, both merge against it, and the second either fails the unique
  constraint or, if retried naively, drops the first ingest's rows. The lock is
  held for the length of one subject's merge, which is seconds. The lock
  function is a no-op on SQLite, and the concurrency test runs under the
  `integration` marker against Postgres.

- **The partial unique index `(kind, symbol, timeframe) WHERE state =
  'published'` and the `CHECK` on `state` enforce the lifecycle in the
  database.** A code bug that published twice or invented a state would
  otherwise be caught only by a reader. `to_manifest` also asserts the
  `lifecycle.yaml` invariants (tombstone iff tombstoned) before returning.

- **Checksums and sizes are computed from the bytes that were written, streamed
  through `hashlib.sha256` while writing the temp file. Adoption computes them
  by reading each file once.** Recomputing on read would hide a file that
  changed after publication, which is exactly what the checksum exists to detect.
  SHA-256 matches `q_contracts` Finding 3's standardization.

- **`time_start` and `time_end` are stored as UTC instants, converted from the
  naive Brasília values with `zoneinfo("America/Sao_Paulo")`. The Arrow schema
  keeps the contract's `tz: "naive-wallclock-America/Sao_Paulo"` marker.** The
  manifest's `date-time` fields are instants, and the lake values are wall-clock
  values. Writing the wall clock with a `Z` would be three hours wrong, as in
  `describe_dataset.py`. The comment in `read_ohlcv` says the stored time is
  "naive UTC", while `RemoteMt5Client` stores `unix_seconds_to_brasilia_naive`
  values. A test on the fixture pins the Brasília reading, and the comment is
  corrected.

- **The Arrow schema is derived from the written file's Parquet schema through
  `lake_arrow_schema`. A test compares it with the vendored
  `contracts/schema/api/arrow/*.schema.json` and records any difference as a
  `q_contracts` finding instead of coercing the file.** The manifest must
  describe what the file is (Q-005). Pandas writes `timestamp[ns]` where the
  API bar schema says `timestamp[us]`, and silently casting on write would change
  bytes that adoption promised not to touch.

- **`partition_start` and `partition_end` live on `lake_dataset_files` and are
  not part of the manifest.** Reads need to pick the partitions a range touches
  without parsing file names (which would be path-guessing again) and without
  opening every file. The manifest schema has `additionalProperties: false` on
  file entries, so per-file ranges stay backend-private.

- **`local_store` keeps its public function signatures and delegates to
  `LakeCatalog`. Private catalog-JSON helpers are deleted, and `catalog.json` is
  left on disk untouched.** Ten call sites depend on these functions, and the
  spec requires unchanged endpoint shapes. Leaving the old file avoids deleting
  user data in a task that promises not to. Nothing reads it again.

- **`list_inventory` maps current datasets to the existing item shape:
  `start`/`end` as naive Brasília ISO strings (as today), `rows = row_count`,
  `bytes = sum(size_bytes)`, `updated_at = published_at`.** The research UI's
  storage workspace renders these fields, and the spec rules out a UI change.

- **The manifest JSON schema is vendored by extending `make contracts` and
  `contracts-check` to copy `schema/catalog/dataset-manifest.schema.json`, and
  tests validate with `jsonschema` against that copy.** Validating against a
  schema pasted into a test is the hand-written mirror that invariant 2 forbids,
  and it would pass after a contract change that it should fail.

- **`DatasetManifestResponse` is a hand-written Pydantic model, with a test that
  its JSON output validates against the vendored schema.** `COMPAT.md` records
  that `q_backend` hand-writes API models, because the captured OpenAPI is
  generated from them. The vendored `DatasetManifest` dataclass is the internal
  type that `to_manifest` returns, so the two are checked against each other
  instead of trusted.

- **The list endpoint returns `root` beside the manifests, and the by-id
  endpoint returns the bare manifest.** The contract requires that the root
  reaches the reader separately from the manifest. A bare by-id response keeps
  it byte-for-byte a contract manifest that can be cached and validated as is.

- **Unknown identifiers return 404, and tombstoned or deleted datasets return
  200 with their state.** A reader that holds a cached manifest needs to learn
  that its dataset was tombstoned, and when deletion becomes permissible, instead
  of being told the dataset never existed.

- **Grace defaults to 604,800 s (7 days) through `Q_CATALOG_TOMBSTONE_GRACE_S`.
  `deletable_after` is written at tombstone time.** Architecture §4.7 names 7
  days. Recording the deadline means changing the setting later does not shorten
  the grace of datasets that open readers already depend on.

- **The sweep is a CLI command (`q-catalog sweep`), not a background thread in
  the API.** A deletion loop inside the API process would run once per API
  replica and die with the API. Q-018 owns process lifecycle and can add a timer
  unit. The sweep reports unreferenced content-addressed files but does not
  delete them (spec non-goal).

- **A database outage on ingest or delete raises before any file is written.
  The routers map `OperationalError` to 503 `catalog_unavailable`.** Checking
  the database first, by taking the subject lock, means an outage cannot leave
  orphan files. The ingest job records a failed timeframe, as it does today
  for provider errors.

- **The OpenAPI recapture and FINDINGS update are commits on a
  `Q-017-dataset-catalog-over-the-existing-lake` branch in `q_contracts`, made
  after the backend branch passes.** This follows the Q-015 precedent and §7.1:
  the captured document must come from the finished API, and `COMPAT.md` is
  updated in the same cross-repo change.

## Ordered implementation

1. Work on the branch `Q-017-dataset-catalog-over-the-existing-lake` in
   `q_backend`, created from `development` by `./work start`.
2. Extend `make contracts` and `make contracts-check` to vendor
   `schema/catalog/dataset-manifest.schema.json` into
   `contracts/schema/catalog/`. Run `make contracts` and confirm that
   `make contracts-check` is clean. Commit.
3. Build `tests/fixtures/lake/` with the synthetic fixture generator pattern
   from `scripts/generate_synthetic_market_fixture.py`: PETR4 D1 with 2024 and
   2025 partitions, WIN$N M15 with a 2025 partition, and WIN$N ticks with two
   months, including a 09:00 Brasília bar. Record the expected rows for three
   read ranges by running today's `read_ohlcv` and `read_ticks_columnar` over it,
   and store them as the regression baseline (`expected_reads.json`). Commit.
4. Write failing tests in `tests/market_data/catalog/test_models.py` (SQLite):
   inserting two `published` rows for one subject raises `IntegrityError`; a
   `state='archived'` row raises; `to_manifest` on a tombstoned row with
   `deletable_after = NULL` raises. Add the models and migration `20260915_0019`.
   Confirm the tests fail, implement, and confirm they pass. Run
   `uv run alembic upgrade head` against local Postgres. Commit.
5. Write failing tests in `test_partitions.py`: `write_partition_immutable`
   writes a file whose name contains the first 16 hex characters of its SHA-256,
   whose `size_bytes` equals `stat().st_size`, and whose checksum equals a fresh
   `hashlib.sha256` of the file; writing the same table twice returns the same
   path and leaves one file; with `os.replace` patched to raise, no file with the
   final name exists and the temp file is removed; `lake_arrow_schema` on the bars
   fixture has `time` with tz `naive-wallclock-America/Sao_Paulo`. Confirm they
   fail, implement, and confirm they pass. Commit.
6. Write failing tests in `test_adopt.py`: adoption over a copy of the fixture
   creates three version 1 datasets; every `to_manifest` output validates against
   the vendored schema; every file's `(bytes, mtime_ns)` is unchanged; the 09:00
   bar's dataset `time_range.start` is `…T12:00:00Z`; a second `adopt()` reports
   three `skipped_existing` and the row count is unchanged. Confirm they fail,
   implement, and confirm they pass. Commit.
7. Write failing tests in `test_publish.py`: after adoption, `publish_bars` with
   rows only in 2025 yields version 2 with `supersedes` equal to version 1's id;
   the 2024 path is identical in both manifests; the 2025 path differs; the
   version 1 2025 file's SHA-256 is unchanged; version 1 is `tombstoned` with
   `deletable_after = now + 7 days`. A reader that opened the version 1 2025 file
   before the publish reads identical bytes afterward. With the catalog commit
   patched to raise after the file write, `current_dataset` is still version 1,
   and a retry yields version 2 once, with one content-addressed file on disk.
   Confirm they fail, implement, and confirm they pass. Commit.
8. Write a failing `@pytest.mark.integration` test in
   `test_publish_concurrency.py` against Postgres: two threads publish disjoint
   2025 bars for one subject behind a barrier; the result is versions 2 and 3, and
   version 3's rows are the union. Confirm it fails without `lock_subject`,
   implement, and confirm it passes. Commit.
9. Write failing tests in `test_sweep.py`: tombstone a subject; `sweep()` at
   `now + 6 days` deletes nothing; at `now + 8 days` it deletes the tombstoned
   version's files that no live dataset lists, keeps shared files, marks the
   dataset `deleted`, and reports an unreferenced content-addressed file placed
   in the fixture without deleting it. Confirm they fail, implement, and confirm
   they pass. Commit.
10. Rewire `local_store`. First write failing tests: the stored
    `expected_reads.json` equals `read_ohlcv` and `read_ticks_columnar` output
    over the adopted fixture; with `Path.glob`, `Path.iterdir`, and `os.listdir`
    patched to raise inside the read path, reads still succeed; `delete_ohlcv`
    leaves every file present and the subject has no current dataset;
    `list_inventory` items validate as `StorageInventoryItem`. Confirm they fail.
    Implement the delegation, delete the JSON catalog helpers, fix the
    time-convention comment, and update `test_local_store.py` and
    `test_local_tick_store.py` fixture setup only (a SQLite session factory and
    `adopt()`). Confirm the tests pass. Commit.
11. Write failing API tests in `tests/api/test_catalog_api.py`: list returns
    `root` and three manifests that each validate against the vendored schema;
    by-id returns a tombstoned dataset with `state: "tombstoned"`; an unknown id
    returns 404; with the session factory raising `OperationalError`, the list,
    ingest, and delete endpoints return 503 with code `catalog_unavailable`, and
    the fixture's file set is unchanged. Confirm they fail, add the schemas and
    router, register the router, and confirm they pass. Commit.
12. Add `catalog_tombstone_grace_s` to settings and the `q-catalog` CLI with
    `adopt`, `sweep [--dry-run]`, and `show`. Write failing CLI tests for exit
    codes and the adopt report lines. Confirm they fail, implement, and confirm
    they pass. Commit.
13. Regression: run `uv run pytest tests/market_data tests/api -q` and confirm
    that no test other than the fixture-setup changes from step 10 was modified.
    Commit any fixes.
14. In `q_contracts`, on the branch `Q-017-dataset-catalog-over-the-existing-lake`:
    start the API from the backend branch, run `uv run python
    tools/capture_api.py`, and commit `schema/api/openapi.yaml`. Mark Findings 1
    to 3 resolved by Q-017. Add a finding for `describe_dataset.py` labelling
    naive wall-clock bounds as UTC, and a finding for any Arrow type difference
    that step 5 recorded. Run `make check`, update the `q_backend` row in
    `COMPAT.md`, and commit.
15. Human step, matching human-verifiable criterion 1: back up the local lake,
    snapshot the file listing, run `uv run q-catalog adopt`, and diff the
    listings. Record datasets per kind and total bytes.
16. Human step, matching human-verifiable criterion 2: ingest one liquid symbol's
    M15 bars from the storage workspace, compare the inventory view before and
    after, and inspect the new manifest with `curl … | jq`.
17. Human step, matching human-verifiable criterion 3: run the same backtest
    before step 15 and after step 16 on a series that step 16 did not touch, and
    compare the headline metrics.
18. Run the full validation suite in `q_backend`, then `make check` in
    `q_contracts`. Commit.

## Validation

- **Unit:** lifecycle constraints; content-addressed immutable writes and failure
  cleanup; checksum and size equality; Brasília-to-UTC time ranges; Arrow schema
  derivation; manifest schema validation.
- **Integration:** adoption leaves bytes and modification times unchanged and is
  idempotent; publish shares unchanged partitions and tombstones the superseded
  version; crash before commit leaves the previous version current; concurrent
  publishes serialize (Postgres); the sweep is reference-aware; the API serves
  valid manifests; database outages give 503 with no files written.
- **Regression:** fixture reads equal the pre-task baseline; storage endpoint
  shapes are unchanged; the existing `tests/market_data` and `tests/api` suites
  pass.
- **Manual:** steps 16 and 17.
- **Measurement:** step 15 datasets per kind and bytes catalogued.
- **Pins:** `CONTRACTS_REV` is unchanged, and the vendoring scope grew by one
  file. The `q_backend` row in `q_contracts/COMPAT.md` is updated with the
  recaptured OpenAPI commit (step 14).

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/market_data/catalog tests/api/test_catalog_api.py -v
uv run pytest -m integration tests/market_data/catalog/test_publish_concurrency.py -v

# human, against the real lake
find "$Q_MARKET_DATA_ROOT" -type f -printf '%p %s %T@\n' | sort > /tmp/lake-before.txt
uv run q-catalog adopt
find "$Q_MARKET_DATA_ROOT" -type f -printf '%p %s %T@\n' | sort | diff /tmp/lake-before.txt -
curl -s 'localhost:8000/api/v1/catalog/datasets?kind=bars' | jq

cd /home/gui/projects/q/q_contracts
make check
```

## Handoff

Report the adoption test's dataset count and confirm that bytes and modification
times were unchanged. Report the publish test's version 2 manifest: which paths
were shared with version 1 and which path is new. Report the concurrency test's
final versions and row counts. Report the sweep test's deleted and retained file
counts. Report whether the fixture read baseline matched exactly, and list every
existing test file that changed and why. Report the `q_contracts` commit with the
recaptured OpenAPI, the routes added, the findings changed or added (including
any Arrow type difference), and the `COMPAT.md` row. From the human steps, report
datasets per kind and bytes adopted on the real lake, the listing diff result,
the ingest's new version and manifest, and the backtest metrics before and after.
