# Q-112 implementation plan: Indicators in research and on the Market chart

> **For implementation agents:** Read the linked spec and repository instructions.
> Use superpowers:executing-plans when that skill is available. Start only through
> `./work start Q-112 --agent <agent> --worktree` after written-plan approval.
> Implement this task natively; delegation requires separate authorization.

**Goal:** every `q_core` indicator is available to strategies, discovery and the feature store, and can be inserted into the Market chart from backend-computed values.
**Architecture:** the backend pins the Q-111 wheel and wraps its kernels; one catalog module feeds a catalog route and a series route; the genome and feature registries gain the bar-based indicators; the Market chart renders endpoint series and drops its TypeScript indicator library.
**Spec:** [Specification](../specs/Q-112-indicators-in-research-and-on-the-market-chart-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The linked spec defines behaviour and acceptance criteria; do not widen scope.
- No indicator arithmetic in Python or TypeScript; both call or display `q_core` values.
- Existing node kinds, feature identifiers and backtest goldens stay unchanged.
- Order across repositories: `q_contracts`, then `q_backend`, then `q_frontend`. Create the task branch in `q_contracts` and `q_frontend` with the same name; vendor an unpushed contract commit with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
- Focused tests only; no gateway, Docker, GPU, Wine or desktop run. Frontend tests run with `TZ=America/Sao_Paulo`.
- Commit focused changes on the task branch. Never push, merge or change protected branches.

## Ordered implementation

- [ ] 1. `q_contracts`: add `GET /api/v1/market/indicators` and `POST /api/v1/market/indicators/{symbol}/series` with their schemas to `schema/api/openapi.yaml`; regenerate; `make check`. Commit.
- [ ] 2. `q_backend` bridge: move the `q-core` tag in `pyproject.toml` to the Q-111 release and refresh `uv.lock`; extend `REQUIRED_KERNELS`; add wrappers to `backtesting/technical_indicators.py` with tests for criteria 1 and 2. Bump `CONTRACTS_REV` and run `make contracts`. Commit.
- [ ] 3. Catalog: add `market_data/indicator_catalog.py` (entries, parameter specs, a `compute(entry, frame, params)` dispatch that calls the wrappers) and the test for criterion 3.
- [ ] 4. Series service and routes in `api/routers/market.py`: warm-up bars taken from the catalog entry, computation on the extended frame, trimming to the range, `null` for NaN, 422 mapping. Tests for criteria 4, 5 and 7 in `tests/api/`.
- [ ] 5. Quote indicators: load ticks from the research tick store, derive the session key and bar bounds, call `quote_spread_bars`, apply `min_coverage`, report unavailable ranges. Test for criterion 6. Commit.
- [ ] 6. Research: add the node kinds to `genome/node_specs.py`, their bounds to `genome/param_bounds.py`, evaluation in `genome/composite_strategy.py` and `features/compute.py`, entries in `features/registry.py`, and the warm-up rule. Add the spread features with tick loading in `features/matrix.py`. Tests for criteria 8 to 10; update the capability registry snapshot. Commit.
- [ ] 7. `q_frontend`: vendor the contract; add queries for the two routes; build the picker and parameter controls from the catalog; draw price and oscillator series from the endpoint; migrate saved `IndicatorConfig` entries to catalog identifiers on load.
- [ ] 8. `q_frontend`: delete `src/lib/indicators` and the computations in `useIndicatorSeries.ts`, `OscillatorPane.tsx`, `VolumeLayer.tsx`, `CandlestickChart.tsx` and `IndicatorsModal.tsx`; update mocks and tests for criteria 11 and 12. Commit.
- [ ] 9. Document the indicators in `docs/research-library.md`. Run the focused tests, `make contracts-check` and each repository's lint and type checks. Commit.

## Review focus

- The catalog is the only list: routes, picker and the exclusion test read it.
- Warm-up loading uses bars before `start`, never after `end`, and the response has exactly the requested bar times.
- A quote series is `null` or unavailable where ticks are missing, and never mixes a bar's quote aggregate into an earlier bar.
- New genome nodes validate parameter relationships at compile time and yield no signal on a missing value.
- No golden, feature identifier or existing node parameter changed.
- The chart refetches on bar backfill without redrawing stale series against new bars.
- No TypeScript file computes a moving average, band or oscillator.

## Validation and handoff

Run the focused backend tests, `make contracts-check` in `q_backend` and `q_frontend`, and `pnpm lint`, `pnpm typecheck` and the focused `vitest` files. Record results and follow-ups,
commit, then run `./work board set Q-112 in-review -m "<changes; checks and results; follow-ups>"` from the
workspace root.
