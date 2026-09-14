# Q-017: Dataset catalog over the existing lake

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §4.7, §9 invariant 6](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#47-persistence-boundary-and-direct-parquet-access)  
**Depends on:** Q-006  
**Implementation plan:** [`../plans/Q-017-dataset-catalog-over-the-existing-lake-plan.md`](../plans/Q-017-dataset-catalog-over-the-existing-lake-plan.md)

## Purpose

The market-data lake is found by building a path from a symbol and a timeframe,
and it is updated by merging new bars or ticks into a year or month file and
rewriting that file in place. Its only index is a JSON file that holds one
mutable summary per series, with no identity, no version, and no checksums.
`q_contracts` Findings 1 to 3 record that this cannot support a second reader.
A process that holds a file open can see it replaced underneath it, and a reader
that guesses a path cannot know whether what it found is complete. This task
puts a Postgres catalog over the lake as it is laid out today. Every series
becomes a published dataset with an immutable identifier and a manifest that
conforms to the contract. Ingest stops rewriting published files and publishes
a new version instead. Deletion becomes tombstoning with a grace period. This is
the prerequisite for `q_terminal` reading Parquet directly. Without it, that read
path would be built on files that can change while they are open.

## Requirements

### Identity and versions

- Every bars series (symbol and timeframe) and every ticks series (symbol) in
  the lake is a subject. At most one published dataset is current for a subject
  at any time.
- A dataset's identifier is assigned when it is published, is not derived from
  any path, and is never reused, including after the dataset is tombstoned or
  deleted.
- Versions within a subject start at 1 and increase by one with each publish.
  Each version after the first records the identifier of the version it
  supersedes.
- Two ingests for the same subject that run at the same time never produce two
  datasets with the same version, and neither silently discards the other's rows.

### Immutable publication

- A file listed by a published manifest is never modified, truncated, renamed,
  or replaced for as long as any non-deleted dataset lists it.
- Ingesting new rows for a subject produces a new dataset version. Partitions
  whose contents did not change are listed again by the new version, not copied.
- A new or changed file becomes visible under its final name only when it is
  complete and durable on disk. A reader never sees a partially written file
  under a name that a manifest lists.
- A dataset is either absent from the catalog or complete. A crash at any point
  during publication leaves the previous version current and readable.
- Publishing a new version tombstones the version it supersedes. The superseded
  version's grace period starts at that moment.

### Manifests

- The catalog serves, for any dataset identifier it has issued, a manifest that
  validates against the `q_contracts` dataset manifest schema at the pinned
  contracts commit.
- The file sizes and checksums in a manifest are the values of the bytes on disk,
  computed when the file was written, and not recomputed from a later read.
- A manifest's time range is expressed as UTC instants that correspond to the
  lake's naive Brasília wall-clock values. Its Arrow schema describes the files'
  actual columns and types.
- File paths in a manifest are relative to the market-data lake root, and the
  root is available to callers separately from the manifest.

### Tombstoning and deletion

- Deleting a series through the existing storage API tombstones its current
  dataset instead of removing files. The files stay readable until the recorded
  grace period has passed.
- The grace period is configurable, defaults to seven days, and is recorded on
  each tombstoned dataset when it is tombstoned, not looked up later.
- A sweep deletes the files of tombstoned datasets whose grace period has
  passed and marks those datasets deleted. It never deletes a file that a
  published or still-in-grace dataset lists.
- A tombstoned or deleted dataset is never returned as the current dataset for
  its subject. Its manifest stays retrievable by identifier, showing its state.

### Adopting today's lake

- Running adoption against an existing lake catalogs every bars and ticks series
  as version 1 without rewriting, moving, or renaming any file. File modification
  times and bytes are unchanged afterward.
- Adoption is idempotent. Running it again catalogs only series that are not
  already catalogued and changes nothing else.

### Reads and existing behavior

- Backend reads of bars and ticks from the lake resolve files through the
  subject's current dataset and do not list directories to find data.
- The existing storage inventory, ingest, and delete endpoints keep their paths,
  request shapes, and response shapes, so the research UI needs no change.
- Reads return the same rows for the same range as before this task, on a lake
  that has been adopted.
- The JSON catalog file is no longer written or read, so there is only one
  catalog.

### Availability

- When Postgres is unavailable, catalog endpoints, ingest, and delete fail with a
  clear service-unavailable error, and nothing is written to the lake. No file is
  written to disk that no dataset will list.

## Constraints and non-goals

- **No direct-read client, no checksum verification on read, no Arrow-over-HTTP
  fallback.** Those belong to `q_core` and `q_terminal`. The backend wrote these
  files and does not re-verify them on every read.
- **No run artifacts in the catalog.** Backtest, walk-forward, search, feature
  matrix, and model artifacts under the data lake root stay as they are. The
  contract's dataset subject only describes bars and ticks, so cataloguing them
  first needs a `q_contracts` subject change and its own task.
- **No change to the partitioning scheme.** Bars stay partitioned by year and
  ticks by month. Repartitioning is tempting while files are being renamed
  anyway, but it would turn cataloguing into a migration.
- **No DuckDB.** Querying the lake through DuckDB is a separate phase 1 item.
- **No stream event on publish or tombstone.** No catalog topic exists in
  `q_contracts`. Adding one is a contract change for a later task.
- **No retention policy.** Nothing tombstones datasets automatically other than
  supersession and explicit deletion. How long history is kept is not decided
  here.
- **No garbage collection of unreferenced files** left by a crash between
  writing a file and committing its dataset. They are harmless because they are
  never listed, and they are reported rather than deleted.
- **No research UI change.** Showing dataset identifiers or versions in the
  storage workspace is a later `q_frontend` task.
- **No systemd unit or timer for the sweep.** Q-018 does not depend on this
  task, so scheduling the sweep is a later task. This task provides the command.

## Acceptance criteria

### Agent-verifiable

1. After adoption of a fixture lake with two bars series and one ticks series,
   each has exactly one published version 1 dataset. Every served manifest
   validates against the vendored contract schema. Every listed file's size and
   SHA-256 match the file on disk. Every file's bytes and modification time are
   unchanged.
2. Running adoption a second time creates no dataset and changes no row.
3. Ingesting bars that change only the latest year publishes version 2. It
   supersedes version 1, lists the unchanged years' files at the same paths, and
   lists a new file for the changed year. The version 1 file for that year is
   byte-identical to before, and version 1 is tombstoned with a deadline equal
   to the publish time plus the grace period.
4. A reader that opens a version 1 file before a concurrent version 2 publish
   reads the same bytes after the publish completes.
5. A publish interrupted after its file is written but before the catalog commit
   leaves version 1 current. A retry then publishes version 2 exactly once.
6. Two concurrent ingests for the same subject produce versions 2 and 3, and
   version 3 contains the rows of both ingests.
7. The storage delete endpoint tombstones the current dataset and removes no
   file. The sweep before the deadline deletes nothing. The sweep after the
   deadline deletes only files that no live dataset lists, and marks the dataset
   deleted.
8. Bars and ticks reads over an adopted fixture return exactly the rows returned
   before this task for the same ranges. No directory listing happens on the read
   path.
9. The inventory, ingest, and delete endpoints' response bodies validate against
   their existing response models, with existing tests unchanged except for
   fixture setup.
10. With the database unreachable, the catalog, ingest, and delete endpoints
    answer 503, and the fixture lake's file set is unchanged.
11. Manifest time ranges for a fixture bar at 09:00 naive Brasília serialize as
    12:00 UTC.
12. The captured OpenAPI document in `q_contracts` includes the catalog routes,
    and `q_contracts` Findings 1 to 3 are marked resolved by Q-017.
13. The full validation suite passes in `q_backend` and `q_contracts`.

### Human-verifiable

1. Adoption runs against the real local lake. The number of datasets per kind
   and the total bytes catalogued are reported, and a before-and-after file
   listing with sizes and modification times is compared and found identical.
   Command: `find "$Q_MARKET_DATA_ROOT" -type f -printf '%p %s %T@\n' | sort > /tmp/lake-before.txt && uv run q-catalog adopt && find "$Q_MARKET_DATA_ROOT" -type f -printf '%p %s %T@\n' | sort | diff /tmp/lake-before.txt -`
2. A real ingest of one liquid symbol's M15 bars is run from the research UI's
   storage workspace. The inventory view before and after is compared by eye,
   and the new dataset's manifest is inspected.
   Command: `curl -s localhost:8000/api/v1/catalog/datasets?kind=bars | jq`
3. A backtest over a catalogued series is run before and after adoption, and its
   headline metrics are confirmed identical.
   Command: `pnpm tauri:dev` (Backtests workspace, same config both runs)
