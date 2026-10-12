# Batch 23 — Research indicators

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with a kernel task in q_core.

## Outcome

Research gains nine indicator families (log returns, normalized momentum, normalized EWMA trend spread, log-price z-score, per-bar volatility, Rogers–Satchell and Yang–Zhang, ATR fraction, volatility-regime ratio and quoted bid-ask spread). Every indicator `q_core` computes can be inserted into the Market chart, and the chart shows the same values a backtest uses.

## Findings this batch answers

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | `q_core` has no log return, exponentially weighted volatility, Rogers–Satchell or quote statistic. | `q-indicators` exports 16 pandas-parity kernels; `tick_bars` builds OHLC only. | Q-111 |
| 2 | Yang–Zhang and the rolling z-score already match the intended formulas. | `yang_zhang(..., periods_per_year=1)` returns the reference value `0.02896649`; `rolling_zscore` gives `1` on `[0, 1, 2]`. | Q-111 (reused) |
| 3 | The Market chart computes its ten indicators in TypeScript. | `q_frontend/src/lib/indicators`, `useIndicatorSeries.ts`. | Q-112 |
| 4 | ATR, realized volatility, Yang–Zhang and the z-score cannot be charted. | The picker in `IndicatorsModal.tsx` lists ten types; no indicator route in `api/routers/market.py`. | Q-112 |
| 5 | Feature warm-up is one integer parameter. | `features/compute.py` `_warmup_bars`. | Q-112 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-111 — Research indicator kernels | q_core | — | `q_core/docs/development/specs/Q-111-research-indicator-kernels-spec.md` | `q_core/docs/development/plans/Q-111-research-indicator-kernels-plan.md` |
| Q-112 — Indicators in research and on the Market chart | q_backend, with q_contracts and q_frontend | Q-111 | [Spec](../specs/Q-112-indicators-in-research-and-on-the-market-chart-spec.md) | [Plan](../plans/Q-112-indicators-in-research-and-on-the-market-chart-plan.md) |

Q-112 starts when Q-111 is Done; it pins the `q_core` tag that `./work finish Q-111` publishes. Inside Q-112 the order is contract, backend, frontend.

## Shared decisions

- **One ATR.** The existing kernel stays; it sets the stops, so chart, features and exits show one number. ATR fraction divides it by the close.
- **Matching kernels are reused.** Yang–Zhang and the rolling z-score are not reimplemented.
- **Volatility is per bar.** Annualization stays a property of the existing `realized_vol` only.
- **Half-lives, with a 1% warm-up.** An exponentially weighted value is withheld until its seed weighs at most 1%; the chart service loads that much history before the requested range.
- **NaN is the only missing value.** No reason codes.
- **One source of values.** The chart draws backend series; TypeScript computes no indicator.
- **Quoted spread is capability-gated.** It needs cached ticks and is reported unavailable without them; it is never rebuilt from candles.
- **Continuous series, one segment.** Indicators do not reset at contract rolls.

## Recorded, not in this batch

- **Roll and gap segmentation**, which needs contract identity in the bar data.
- **Streaming states** for the new kernels, and their use in `q_terminal`.
- **Quote indicators as genome nodes**, which need ticks inside the candle backtest frame.
- **`logic.not` during warm-up.** `composite_strategy.py` evaluates `~source.fillna(False)`, so a negated comparison is true while its indicator is missing. Longer warm-ups make this matter; it needs its own decision because it can move goldens.
- **Annualized and variance outputs** of the new estimators; they are scalings of the per-bar values.

## Review and integration

Documentation is written on local `docs/batch-23-research-indicators` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-23-research-indicators`. No branch is pushed or merged by the authoring session.

The human integrates these branches before launching implementation tasks and owns the initial board Status and the move to Todo. Launch an approved task whose dependencies are Done from the workspace root with `./work start Q-NNN --agent <agent> --worktree`.
