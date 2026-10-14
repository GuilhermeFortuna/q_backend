# Batch 27 — Volume at price

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with a kernel task in q_core.

## Outcome

Research reads where volume traded: volume-at-price profiles built from stored trades, per session, per bar or over a range, split by aggressor side, with point of control and value area. Value-area levels are aligned to bars without lookahead, so a strategy can use them. The Market chart draws the same profiles beside the candles and the same levels as indicator lines.

## Findings this batch answers

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | `q_core` classifies trades by aggressor side but keeps no per-price volume. | `q-indicators/src/volume.rs` (Q-081) accumulates per bar and session only. | Q-116 |
| 2 | The tick store holds every trade's price, volume and flags, but research cannot read them as trades. | `TickStore.quotes(day)` exists; no trade accessor; `real_volume` is only summed into bars. | Q-117 |
| 3 | The Market chart cannot draw anything indexed by price. | Indicator series are aligned to bar times; no layer in `components/charts/layers/` draws horizontally. | Q-117 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| [Q-116](https://github.com/GuilhermeFortuna/q_core/issues/17) — Volume-at-price kernels | q_core | — | `q_core/docs/development/specs/Q-116-volume-at-price-kernels-spec.md` | `q_core/docs/development/plans/Q-116-volume-at-price-kernels-plan.md` |
| [Q-117](https://github.com/GuilhermeFortuna/q_backend/issues/48) — Volume at price in research and on the Market chart | q_backend, with q_contracts and q_frontend | Q-116 | [Spec](../specs/Q-117-volume-at-price-in-research-and-on-the-market-chart-spec.md) | [Plan](../plans/Q-117-volume-at-price-in-research-and-on-the-market-chart-plan.md) |

Q-117 starts when Q-116 is Done; it pins the `q_core` tag that `./work finish Q-116` publishes. Inside Q-117 the order is contract, backend, frontend.

## Shared decisions

- **One trade rule.** A trade is a tick with the buy or sell bit, as in the tick store's `real_volume`; both bits mean unknown side. Volume at price therefore sums to the bars' real volume.
- **Rows on the tick grid.** Prices are binned by integer tick index from the instrument's tick size, never by float comparison; off-grid trades are counted, not dropped.
- **Deterministic levels.** POC ties go to the row nearest the centre of the range, then the lower row; the value area expands one row at a time from the POC, adding both rows on a tie.
- **No lookahead.** Developing levels at a bar include trades up to the bar's close only; prior-session levels come from the completed previous session.
- **One source of values.** Research and the chart call the same service and kernels; TypeScript computes no profile.
- **Capability-gated.** Without stored ticks or a tick size, profiles and levels are reported unavailable, never rebuilt from candles.

## Recorded, not in this batch

- **Footprint chart** (per-bar bid × ask cells); the kernels already produce per-bar segments.
- **Order-flow series in research**: per-bar buy, sell, delta and cumulative delta from the Q-081 kernels, which `q_terminal` shows but research cannot read.
- **Volume-at-price features** in the feature store and genome (distance to POC, VAH and VAL; inside-value flags).
- **High- and low-volume nodes, naked POCs and composite profiles** over several sessions.
- **Live profiles in `q_terminal`**, which need a streaming state and a Qt binding.

## Review and integration

Documentation is written on local `docs/batch-27-volume-at-price` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-27-volume-at-price`. No branch is pushed or merged by the authoring session.

The human integrates these branches before launching implementation tasks and owns the initial board Status and the move to Todo. Launch an approved task whose dependencies are Done from the workspace root with `./work start Q-NNN --agent <agent> --worktree`.
