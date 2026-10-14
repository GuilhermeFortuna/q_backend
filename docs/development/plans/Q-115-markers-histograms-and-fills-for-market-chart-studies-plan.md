# Q-115 implementation plan: Markers, histograms and fills for Market chart studies

> **For implementation agents:** Read the linked spec and repository instructions.
> Use superpowers:executing-plans when that skill is available. Start only through
> `./work start Q-115 --agent <agent> --worktree` after written-plan approval.
> Implement this task natively; delegation requires separate authorization.

**Goal:** a study series can be drawn on the Market chart as a line, step, markers or histogram, lines and steps can fill to another series, and the study size limit counts non-null values.
**Architecture:** new optional styling fields travel through the existing study schema and the JSON `series` column, with no change to the value store; `ChartIndicator` gains the same fields; the frontend generalises the study render model into typed layers and shares one marker glyph module with `LiveStrategyChart`.
**Spec:** [Specification](../specs/Q-115-markers-histograms-and-fills-for-market-chart-studies-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The linked spec defines behaviour and acceptance criteria; do not widen scope.
- Kind rules are defined once per language: in `research/charting.py` for the library and in `api/schemas/research_studies.py` for the API. The library does not call the API's validators.
- No Alembic migration and no change to the Parquet layout.
- Order across repositories: `q_contracts`, then `q_backend`, then `q_frontend`. Create the task branch in `q_contracts` and `q_frontend` with the same name; vendor an unpushed contract commit with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
- Focused tests only; no gateway, Docker, GPU, Wine or desktop run. Frontend tests run with `TZ=America/Sao_Paulo`.
- Commit focused changes on the task branch. Never push, merge or change protected branches.

## Ordered implementation

- [x] 1. `q_contracts`: add the optional fields and their enums and bounds to `ResearchStudySeriesInput` and `ResearchStudySeriesMetadata`; regenerate; extend `tests/test_research_studies_contract.py`; `make check`. Commit.
- [x] 2. `q_backend`: bump `CONTRACTS_REV` and run `make contracts`. In `api/schemas/research_studies.py`, add the fields and a model validator for the per-kind, pane and `fillTo` rules; read a stored series without `kind` as `line` in `study_series_metadata`. Tests for criteria 1 and 4. Commit.
- [x] 3. `research/study_service.py`: count non-null values for `research_study_max_values`, add `research_study_max_timestamps` to `storage/settings.py`, and update the `413` details. Tests for criterion 5. Commit.
- [x] 4. `research/charting.py`: add the fields and rules to `ChartIndicator`; check `fill_to` in `research/studies.py` and serialise the new fields; make the strategy adapter's `_declare_chart_indicators` raise the studies-only `ValueError`. Tests for criteria 2 and 3 with mocked HTTP. Commit.
- [x] 5. `q_frontend`: vendor the contract. Extract the glyph shapes from `LiveStrategyChart.tsx` into a shared module under `components/charts/layers/` and switch `LiveStrategyChart` to it (criterion 9). Commit.
- [x] 6. `q_frontend`: generalise the price and oscillator render types in `buildIndicatorRenderModel.ts` to carry a kind, keeping catalog output as lines; map study series in `buildStudyRenderModel.ts`; draw steps, fills and markers in `IndicatorLayer.tsx` and steps, markers and histograms in `OscillatorPane.tsx`, limited to the visible range; update legend swatches. Component tests for criteria 6 to 8. Commit.
- [x] 7. Update `docs/research-library.md` and `examples/research/publish_study.py`. Run the focused tests, `make contracts-check` in `q_backend` and `q_frontend`, and each repository's lint and type checks. Commit.

## Review focus

- Studies published before this task draw exactly as before.
- `above-bar`/`below-bar` markers treat `0` as no marker; `value` markers draw at `0`.
- Marker rendering stays bounded by the visible range on long M1 studies.
- The backtest Trade Chart contract is untouched, and strategies cannot declare kinds it cannot draw.
- `LiveStrategyChart` looks the same after the glyph extraction.

## Validation and handoff

Run the focused backend tests, `make contracts-check` in `q_backend` and `q_frontend`, and `pnpm lint`, `pnpm typecheck` and the focused `vitest` files. Record results and follow-ups,
commit, then run `./work board set Q-115 in-review -m "<changes; checks and results; follow-ups>"` from the
workspace root.
