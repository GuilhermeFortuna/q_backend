# Q-114 implementation plan: Publish research studies to the Market chart

> **For implementation agents:** Read the linked spec and repository instructions.
> Use superpowers:executing-plans when that skill is available. Start only through
> `./work start Q-114 --agent <agent> --worktree` after written-plan approval.
> Implement this task natively; delegation requires separate authorization.

**Goal:** a research script publishes a time-indexed pandas Series or DataFrame as a study for one symbol and timeframe, and the Market chart offers and draws it while that symbol is on the chart.
**Architecture:** `publish_study` validates and serializes the frame and posts it to new `research/studies` routes; the backend stores metadata in `research_studies` and values as Parquet in the lake, mirroring Q-109's figures; the Market chart lists studies per symbol in its indicator menu and aligns their values to loaded bars by timestamp.
**Spec:** [Specification](../specs/Q-114-publish-research-studies-to-the-market-chart-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The linked spec defines behaviour and acceptance criteria; do not widen scope.
- Reuse the figures code paths (provenance, error mapping, atomic lake writes) instead of copying them; extract a shared helper where both need it.
- `ChartIndicator` is the only styling type in Python; do not add a second one.
- Order across repositories: `q_contracts`, then `q_backend`, then `q_frontend`. Create the task branch in `q_contracts` and `q_frontend` with the same name; vendor an unpushed contract commit with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
- Focused tests only; no gateway, Docker, GPU, Wine or desktop run. Frontend tests run with `TZ=America/Sao_Paulo`.
- Commit focused changes on the task branch. Never push, merge or change protected branches.

## Ordered implementation

- [x] 1. `q_contracts`: add the five `research/studies` routes and their schemas (camelCase, as the market indicator schemas) to `schema/api/openapi.yaml`; regenerate; `make check`. Commit.
- [x] 2. `q_backend`: bump `CONTRACTS_REV` and run `make contracts`. Add `research_study_max_values` to `storage/settings.py`, the `ResearchStudy` model and its Alembic migration, and a repository with get-by-identity, list-by-symbol, upsert, rename and delete.
- [x] 3. `research/study_service.py`: request validation (the `422` and `413` rules), Parquet write through the lake's temporary-file-and-rename helper, range reads with a Parquet filter on `time`, rename with the `409` check, and delete of row and file. Routes in `api/routers/research_studies.py`, registered next to the figures router. Tests for criteria 5 to 7 in `tests/api/`. Commit.
- [x] 4. `research/studies.py`: `publish_study` with index conversion, timeframe inference and checks, column selection through `ChartIndicator`, value conversion, and the HTTP call through the error mapping `publish_figure` uses (moved to a shared helper if needed). Export it lazily from `research/__init__.py`. Tests for criteria 1 to 4 with mocked HTTP. Commit.
- [x] 5. `q_frontend`: vendor the contract; add `api/queries/researchStudies.ts` (list by symbol with refetch on window focus, series by id, revision and range, rename and delete mutations) and MSW handlers and mocks.
- [x] 6. `q_frontend`: add the Studies group to `IndicatorsModal.tsx` with the disabled state, notes, rename and an in-app delete confirmation; store enabled ids per symbol under `quant:market-studies`, dropping ids missing from the list.
- [x] 7. `q_frontend`: align study values to loaded bars by timestamp, feed price series to `IndicatorLayer` and each study's oscillator series to one `OscillatorPane`, assign theme colours to unstyled series, label legends, and add the misalignment notice to the chart notice. Component tests for criteria 8 to 11. Commit.
- [x] 8. Document the feature in `docs/research-library.md` and the README API reference; add `examples/research/publish_study.py`. Run the focused tests, `make contracts-check` and each repository's lint and type checks. Commit.

## Review focus

- Timestamps cross the wire in UTC and are compared with bar times in UTC; no local-time parsing in the frontend.
- Timeframe inference ignores session and weekend gaps, and an explicit timeframe is still checked against the index.
- A replacement swaps values atomically: a reader sees the old or the new revision, never a mix, and the frontend never draws old values under a new revision.
- Studies of another symbol never reach the menu or the chart, including just after a symbol switch.
- Misaligned values are reported, never moved to a neighbouring bar.
- Catalog indicators, chart profiles and figures behave as before.

## Validation and handoff

Run the focused backend tests, `make contracts-check` in `q_backend` and `q_frontend`, and `pnpm lint`, `pnpm typecheck` and the focused `vitest` files. Record results and follow-ups,
commit, then run `./work board set Q-114 in-review -m "<changes; checks and results; follow-ups>"` from the
workspace root.
