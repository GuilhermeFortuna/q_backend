# Q-089 implementation plan: Research market DataFrames

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-089 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** Load Q historical bars as pandas frames through a scoped, supported research facade.
**Architecture:** Research lazily selects a scoped catalog reader or existing RemoteMt5Client; shared frame normalization establishes a timezone-aware schema.
**Tech stack:** Python 3.12+, pandas, existing q_backend adapters and pinned q_core.
**Spec:** [Specification](../specs/Q-089-research-market-dataframes-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Python namespace inside q_backend; retain existing installation/dependency scope and service behavior.
- No alternative indicator/fill/PnL implementation, generated contract changes, GPU, live trades, desktop or optimizer integration.
- Use explicit instance configuration and lazy dependencies. No import-time startup or global environment/runtime-config mutation.
- Verify changed behavior with small fixtures and focused mocks. No benchmarks, full-stack runs, live gateway runs or blanket full CI requirement.
- Commit focused task changes locally. Human owns integration, publication and initial board Status/Todo approval.

## Review focus

- Aware and naive bounds agree after explicit Brasília normalization; date-only end is midnight.
- Missing local coverage selects remote only under auto; DB/file failures are not hidden as empty results.
- Two instances and close() never mutate global configuration or dispose shared resources.
- Missing optional volume fields, malformed bars and duplicates produce the specified schema/errors.
- Import/construction and each source touch only their required resources; forming bars are excluded.

## Ordered implementation

### 1. Establish the facade and frame contract

**Files:** Create src/q_backend/research/{__init__,errors,frame,data}.py; create tests/research/test_data.py and test_imports.py.
**Interfaces:** Research constructor, bars(), inventory(), close()/context manager, NoMarketDataError, and shared private frame/bounds validation exactly as specified. Later Q-091 reuses price/index validation while permitting empty frames and optional volumes.

- [ ] Add a compact fixture-driven test set for schema/index/dtypes, aware-versus-naive bounds (including historical DST), date-only end, invalid prices/duplicates and optional columns. Assert the original input/config is unchanged. Cover empty -> NoMarketDataError and typed empty inventory.
- [ ] Run `uv run pytest tests/research/test_data.py tests/research/test_imports.py -q` to identify missing behavior.
- [ ] Implement lazy exports, configuration ownership and frame normalization. Use the shared timezone contract, finite OHLC validation and metadata specified in Q-089. Constructor/import must not connect or create directories; fresh-process tests observe these boundaries without launching services.
- [ ] Run the same focused tests; commit the facade/frame unit.

### 2. Connect scoped local and remote reads

**Files:** Create src/q_backend/research/providers.py; extend data.py and tests/research/test_data.py. Reuse market_data/catalog/service.py, catalog/repository.py, lake_query.py and clients/remote.py. Factor a shared helper only when necessary.
**Interfaces:** local uses an owned catalog/session factory and immutable catalog-resolved file paths; remote uses explicit gateway config; auto selects by catalog envelope. Expose no new provider API.

- [ ] Add mocked catalog/HTTP cases for local, remote and auto routing, local DB/missing-file errors, remote unavailable/empty/short reads, and coverage failure. Assert remote never publishes/caches or opens DB; local never calls gateway/native MT5. Use two scoped instances and verify close ownership.
- [ ] Add frozen-clock cases for M5 and calendar MN1 completion; validate other supported timeframes through the same boundary helper. Assert a forming final row is removed, and a finished historical row remains.
- [ ] Run `uv run pytest tests/research/test_data.py -q`; implement provider isolation, inventory and forming-bar filtering without shared singleton mutation or managed-lake globbing.
- [ ] Run the research tests and `uv run pytest tests/market_data/test_timezone.py tests/market_data/test_remote_client.py tests/market_data/lake_query/test_lake_query.py -q`; keep fixture resources isolated. Commit the provider integration.

### 3. Document and demonstrate data loading

**Files:** Create docs/research-library.md and examples/research/load_market_data.py; modify README.md.

- [ ] Explain installation in the existing uv environment, local PostgreSQL/Parquet requirements, remote gateway requirements, read-only behavior, timeframe/date bounds and timezone schema. Example accepts configuration via arguments/environment, with no hard-coded host paths or sys.path edits.
- [ ] Exercise the example's conversion/configuration path through the same mocked data fixture; do not require a reachable catalog/gateway. Link the guide from README and commit documentation.

## Handoff

- [ ] Check the spec against the implementation and focused results; document actual commands and outcomes without claiming unrun checks passed.
- [ ] Commit the final documentation/examples and use `./work board set Q-089 in-review -m "<changes; focused checks and results; follow-ups>"`. If a required prerequisite blocks progress, use the documented blocked workflow.
