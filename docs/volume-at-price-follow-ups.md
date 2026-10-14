# Volume at price: follow-up ideas

**Status:** candidate work, not scheduled. Nothing here is on the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) until it has its own spec, plan and issue.
**Context:** [Batch 27 — Volume at price](development/batches/batch-27-volume-at-price.md) (Q-116 kernels, Q-117 research and Market chart).

Batch 27 makes research read stored trades by price. These ideas reuse what it builds (`TickStore.trades`, the shared service in `market_data/volume_at_price.py`, the Market chart's price-indexed layer) or reach `q_core` code that already exists but that research cannot call. They are ordered by value for effort.

| # | Idea | Builds on | Repositories | Suggested shape |
| --- | --- | --- | --- | --- |
| 1 | Order flow in research and on the Market chart | Q-081 kernels, `TickStore.trades` | q_core (binding), q_backend, q_contracts, q_frontend | Fold into Q-117, or one task after it |
| 2 | Session VWAP and deviation bands | `SessionVwapState` in `q-indicators` | q_core, q_backend, q_contracts, q_frontend | Fold into Q-117 with idea 1 |
| 3 | Volume-at-price features for discovery | Q-117 `value_area_levels` | q_backend | Own task after Q-117 |
| 4 | Footprint chart | Q-116 per-bar segments, Q-117 profile route | q_backend, q_contracts, q_frontend | Own task after Q-117 |
| 5 | Volume nodes, naked POCs and composite profiles | Q-116 kernels | q_core, q_backend, q_frontend | Own task, after idea 3 shows a use |
| 6 | Live volume profile in `q_terminal` | Q-116 formulas | q_core, q_terminal | Own batch |

## 1. Order flow in research and on the Market chart

**Gap.** `q_core` already computes per-bar buy, sell and unknown volume, delta, cumulative delta (reset per session), classified share, trade rate and large prints (`q_indicators::volume`, Q-081, with `batch_volume` for replay). `q_terminal` draws them live. Q-081 deliberately added no Python binding, so research and the Market chart cannot read any of them, and a strategy cannot be backtested on what the terminal shows.

**Proposal.**

- Bind `batch_volume` in `q_core.indicators`, sampling at bar closes as checkpoints. No new arithmetic.
- `research.indicators.order_flow(frame, *, ticks, timeframe, large_print_threshold=100.0)` returns `buy`, `sell`, `unknown`, `delta`, `cumulative_delta` and `classified_share` on the frame's index, read from `TickStore.trades` through the same session and bar-bound rules as Q-117.
- Catalog entries in the **Volume** category: delta as a histogram and cumulative delta as a line in the oscillator pane, and large prints as price-pane markers, using the Q-115 kinds.

**Why now.** It reads the same trades through the same service as Q-117, so the marginal cost is a binding, a helper and catalog rows. It also closes the gap between what the terminal shows and what research can test.

**Watch for.** The trade rate needs a continuous observation window; on bar checkpoints it is only meaningful at bar granularity, so leave it out of the catalog unless a use appears. Unknown-side trades are under 1% of volume on WDO, but the classified share should still be shown so a low-coverage symbol is not read as balanced flow.

## 2. Session VWAP and deviation bands

**Gap.** `SessionVwapState` exists in `q-indicators` and the terminal draws session VWAP, but research and the Market chart catalog have no VWAP.

**Proposal.**

- A batch `session_vwap` kernel over bars (typical price × real volume, reset on the session key), with volume-weighted standard-deviation bands at caller-chosen multiples. It must equal the streaming state's output on the same bars; a parity test pins that.
- `research.indicators.session_vwap(frame, *, bands=(1.0, 2.0))` and a catalog entry drawn as a price-pane line with fills between the bands.

**Why.** VWAP is the reference most intraday B3 participants watch, and it pairs naturally with the value area. Computed from bars and `real_volume` it needs no ticks, so it works wherever bars do.

**Watch for.** Bars without `real_volume` (symbols that only report tick volume) must make the series unavailable, as the terminal does, not fall back to tick volume silently.

## 3. Volume-at-price features for discovery

**Gap.** Q-117 gives bar-aligned POC, VAH and VAL, but the feature store, feature evaluation and the genome cannot use them, so discovery cannot test value-area hypotheses.

**Proposal.** Tick-store features with their own source (for example `trades`), handled like the quote features of Q-113: computed from the tick store, and not genome nodes until ticks enter the candle backtest frame.

- Distance from the close to POC, VAH and VAL, in ATR units, for both level modes.
- Inside the prior value area (`1`/`0`) and the bar's position within it (0 at VAL, 1 at VAH).
- Open location: whether the session opened above, inside or below the prior value area.
- Value-area width relative to ATR, as a balance/imbalance proxy.

**Why.** These encode the classic value-area setups (the "80% rule", value-area rejection, open-outside-value continuation) as testable features, instead of hand-written experiment scripts.

**Watch for.** The 2025-10-01 to 2026-10-05 WDO tick history was already used, holdout included, by an earlier order-flow study; a result on those dates is in-sample and needs later data to confirm. Each feature needs a causality test through the tick-backed harness of Q-113, and the latent gate's classical baseline must stay free of tick-dependent features.

## 4. Footprint chart

**Gap.** Q-116 already returns per-bar segments (`by="bar"`), but the chart only draws per-session and visible-range profiles.

**Proposal.** A footprint mode on the Market chart: for each bar in view, cells per price row showing sell × buy volume, with the bar's POC outlined and diagonal imbalances (buy at row `r` against sell at row `r − 1`) highlighted above a configurable ratio. It is only drawn when bars are wide enough to read; otherwise the chart falls back to candles and says so.

**Why.** It shows where inside each bar the aggression happened, which delta per bar hides.

**Watch for.** Payload size grows with bars × rows. The profile route's row limit must apply, and the client must request only the visible bars. Imbalance ratios are arithmetic, so they belong in a `q_core` kernel, not in TypeScript.

## 5. Volume nodes, naked POCs and composite profiles

**Proposal.**

- **High- and low-volume nodes:** local maxima and minima of the row totals after a deterministic smoothing defined in `q_core`.
- **Naked POCs:** prior session POCs that price has not traded through since, with their age, drawn as rays until touched.
- **Composite profiles:** one profile over the last N sessions, or a fixed date range, through the existing `range` segment plus a rolling option.

**Why.** These are common support/resistance references, and naked POCs are a simple, testable hypothesis.

**Watch for.** Node detection is sensitive to row size and smoothing; specify both before implementing, and prefer starting with naked POCs, which have no free parameters.

## 6. Live volume profile in `q_terminal`

**Proposal.** A streaming volume-at-price state in `q-indicators` (push a trade, read rows and levels), bound through `q-qt`, drawn by the terminal chart as a session or visible-range profile with live POC and value area. The batch kernels of Q-116 become the parity reference for the streaming state.

**Why.** The terminal is where the levels are used during the session; research and live would then show the same numbers.

**Watch for.** This touches the live operations boundary, the Qt binding and the terminal's study persistence. It needs its own batch with terminal design references, as the earlier terminal study work did.

## Recommended next step

Fold ideas 1 and 2 into Q-117 before it starts: they share its trade reader, service and **Volume** catalog category, and add no new architecture. Write idea 3 as Q-118, depending on Q-117. Keep ideas 4 to 6 recorded here until research shows a use for them.
