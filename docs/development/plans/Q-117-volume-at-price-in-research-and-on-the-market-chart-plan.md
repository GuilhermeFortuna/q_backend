# Q-117 implementation plan: Volume at price in research and on the Market chart

> **For implementation agents:** Read the linked spec and repository instructions.
> Use superpowers:executing-plans when that skill is available. Start only through
> `./work start Q-117 --agent <agent> --worktree` after written-plan approval.
> Implement this task natively; delegation requires separate authorization.

**Goal:** research scripts build volume-at-price profiles and bar-aligned value-area levels from stored trades, and the Market chart draws both from the same service.
**Architecture:** the backend pins the Q-116 wheel; one service, `market_data/volume_at_price.py`, reads trades from the tick store and calls the kernels for the research helpers, the catalog's level series and a new profile route; the frontend adds a price-indexed `VolumeProfileLayer` and draws the levels with the Q-115 step renderer.
**Spec:** [Specification](../specs/Q-117-volume-at-price-in-research-and-on-the-market-chart-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The linked spec defines behaviour and acceptance criteria; do not widen scope.
- No volume-at-price arithmetic in Python or TypeScript; Python segments and aligns, TypeScript scales and draws.
- Segments, session keys and bar bounds reuse `bar_bounds_msc` and the session rule of `market_data/quote_spread.py`; do not write a second clock conversion.
- Existing catalog entries, their identifiers, saved chart profiles and backtest goldens stay unchanged.
- Order across repositories: `q_contracts`, then `q_backend`, then `q_frontend`. Create the task branch in `q_contracts` and `q_frontend` with the same name; vendor an unpushed contract commit with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
- Focused tests only; no gateway, Docker, GPU, Wine or desktop run. Frontend tests run with `TZ=America/Sao_Paulo`.
- Commit focused changes on the task branch. Never push, merge or change protected branches.

## Ordered implementation

- [x] 1. `q_contracts`: add `POST /api/v1/market/volume-profile/{symbol}` and its schemas, the `trades` required-data value and the optional catalog output `kind` to `schema/api/openapi.yaml`; regenerate; extend the contract tests; `make check`. Commit.
- [x] 2. `q_backend` bridge: move the `q-core` tag to the Q-116 release and refresh `uv.lock`; extend `REQUIRED_KERNELS`; wrap both kernels in `backtesting/technical_indicators.py`; add `TickStore.trades(day)`. Bump `CONTRACTS_REV`, run `make contracts`. Tests for criteria 1 and 2. Commit.
- [x] 3. Add `market_data/volume_at_price.py`: session, bar and range segments; per-session kernel calls; prior-session alignment; `unavailable_reason`. Add `VolumeProfile`, `volume_at_price` and `value_area_levels` to `research/indicators.py` and `__all__`. Tests for criteria 3 to 7 against a synthetic store under `tmp_path`. Commit.
- [x] 4. Catalog and series: add the `trades` data kind, output `kind`, the **Volume** category and the `value_area_levels` entry; resolve the tick size from the market service's symbol info and report unavailable without it. Tests for criterion 8. Commit.
- [x] 5. Profile route in `api/routers/market.py` with its schemas in `api/schemas/market.py`, `Q_MARKET_VOLUME_PROFILE_MAX_ROWS` in `storage/settings.py`, and the `422` and `413` mapping. Tests for criterion 9 in `tests/api/`. Commit.
- [x] 6. `q_frontend`: vendor the contract; add the profile query to `src/api/queries/marketIndicators.ts`; draw catalog outputs of kind `step` through the existing step path; add the **Volume profile** picker entry and persist it in saved chart profiles. Commit.
- [x] 7. `q_frontend`: add `components/charts/layers/VolumeProfileLayer.tsx` (session and visible-range placement, value-area and POC styling, buy/sell split, visible-price culling, tooltip rows) and mount it beneath the candles in `CandlestickChart.tsx`; debounce visible-range refetches. Component tests for criteria 10 and 11. Commit.
- [x] 8. Document **Volume at price from local trades** in `docs/research-library.md` and add `examples/research/volume_at_price.py`. Run the focused tests, `make contracts-check` in `q_backend` and `q_frontend`, and each repository's lint and type checks. Commit.

## Review focus

- A developing level at bar `t` uses no trade at or after the bar's close; prior-session levels never include the current session.
- Session keys match `quote_spread.py`, so a bar's profile and its spread come from the same day's ticks.
- The tick size comes from the caller or frame metadata in research and from symbol info on the chart; it is never guessed from prices.
- The profile route is bounded by the row limit before serialising, and the layer renders only rows in view.
- Visible-range refetches are debounced and cannot draw a stale profile against a newer range.
- No existing catalog entry, saved profile or golden changed.

## Validation and handoff

Run the focused backend tests, `make contracts-check` in `q_backend` and `q_frontend`, and `pnpm lint`, `pnpm typecheck` and the focused `vitest` files. Record results and follow-ups,
commit, then run `./work board set Q-117 in-review -m "<changes; checks and results; follow-ups>"` from the
workspace root.
