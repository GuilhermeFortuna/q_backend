# Q-112: Indicators in research and on the Market chart

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 23 — Research indicators
**Depends on:** Q-111
**Implementation plan:** [Plan](../plans/Q-112-indicators-in-research-and-on-the-market-chart-plan.md)

## Purpose

Make every `q_core` indicator usable in research and insertable into the Market chart, with one source of values. An indicator that cannot be drawn cannot be inspected.

## Current system

- The Market chart offers ten indicators and computes them in TypeScript (`q_frontend/src/lib/indicators`). No endpoint serves indicator series for it, so its values can differ from a backtest's.
- `atr`, `realized_vol`, `yang_zhang`, the rolling z-score, rank and percent change exist in `q_core` and in the genome and feature registries, and are absent from the chart.
- The Q-111 kernels have no backend caller.
- The feature store takes a feature's warm-up from one integer parameter, which cannot describe a half-life.

## Required behavior

### Kernel bridge

- `pyproject.toml` pins the `q-core` tag published by Q-111, and `REQUIRED_KERNELS` lists the new kernels.
- `backtesting/technical_indicators.py` wraps each new kernel as a pandas function with the existing index and validation conventions. No indicator arithmetic is written in Python.

### Indicator catalog

One backend catalog describes every chartable indicator: identifier, label, category, parameters (name, type, default, bounds), outputs (key and label), pane (`price` or `oscillator`), unit and required data (`bars` or `quotes`).

| Group | Indicators |
| --- | --- |
| Moving averages | SMA, EMA, WMA, HMA, SMMA, volume moving average |
| Bands and channels | Bollinger bands, Donchian channels |
| Oscillators | RSI, MACD, rolling z-score, rolling rank, percent change |
| Returns and trend | log return, volatility-normalized momentum, normalized EWMA trend spread, log-price z-score |
| Volatility and range | ATR, ATR fraction, true range, realized volatility (annualized), per-bar realized volatility, per-bar EWMA volatility, Rogers–Satchell, Yang–Zhang, volatility log ratio |
| Liquidity | quoted spread in basis points (time-weighted mean, max, last), spread against volatility |

`clip` is not chartable: it is a bound applied to another series and has no reading of its own. The catalog test names it as the only excluded kernel, so a kernel added later must be charted or excluded on purpose.

Normalized scores take their volatility as flat parameters: `vol_estimator` (`realized`, `ewma`, `rogers_satchell`, `yang_zhang`), its window or half-life, and `vol_lag`.

### Market chart endpoints

- `GET /api/v1/market/indicators` returns the catalog.
- `POST /api/v1/market/indicators/{symbol}/series` takes a timeframe, a start and end, and a list of indicator requests (identifier and parameters). It returns the bar times of the range and, per requested output, its key, pane and values aligned to those times. A missing value is `null`.
- The service loads enough bars before `start` to cover each indicator's warm-up, computes on the extended frame and returns only the requested range. A value therefore does not depend on where the range starts, whenever that history exists.
- Quote indicators read the research tick store. Each bar's session key is its trading date; `max_quote_age_ms` and `min_coverage` are request parameters with catalog defaults, and a bar below the minimum coverage is `null`. A range the tick store does not cover returns the series marked unavailable with a reason, never zeros.
- Unknown identifiers and out-of-domain parameters return 422 naming the parameter.

### Research

- The genome node registry, the feature registry and the capability registry gain the bar-based indicators of Q-111: log return, normalized momentum, EWMA trend spread, log-price z-score, per-bar volatility with its four estimators, ATR fraction and volatility log ratio. Each has typed parameters with bounds; fast and slow, and short and long, are validated before computing.
- The feature store gains quoted spread and spread against volatility as features computed from the tick store. A matrix whose range has no ticks reports them unavailable.
- A feature's warm-up is the number of leading missing values of its kernel output when it has no single lookback parameter.
- Existing nodes, features, their identifiers and every backtest golden are unchanged.

### Market chart

- The indicator picker lists the catalog, grouped by category, with parameter controls built from it.
- Every drawn series comes from the series endpoint and is refetched when the loaded bars or the selection change. Price-pane series overlay the candles; oscillator-pane series draw below with their unit. A missing value breaks the line. An unavailable series shows its reason.
- `src/lib/indicators` and every client-side indicator computation in the chart components are removed.
- Saved chart profiles that reference the ten existing indicators load unchanged.

### Contract

`q_contracts` `schema/api/openapi.yaml` gains the two routes and their schemas. Backend and frontend vendor the new revision and `make contracts-check` passes in both.

## Acceptance criteria

Synthetic bars and ticks, a mocked gateway and stores under `tmp_path`.

1. Importing the bridge with a `q_core` lacking a Q-111 kernel raises `ImportError` naming it.
2. Each new wrapper returns the kernel's values on the input's index.
3. The catalog test fails when a `q_core.indicators` kernel is neither charted nor listed as excluded.
4. The series endpoint returns, for SMA, RSI, MACD and ATR, the values the existing backend functions give on the same bars.
5. Per-bar EWMA volatility over a range equals the same bars' values from a request that starts 500 bars earlier.
6. A quote indicator over a range with ticks returns the Q-111 aggregate; over a range without ticks it is marked unavailable with a reason.
7. An unknown identifier and an inverted fast and slow pair return 422.
8. A genome using normalized momentum, and one using volatility log ratio, compile, backtest and take no trade before the indicator's first value.
9. A feature matrix with per-bar EWMA volatility (`half_life = 32`) reports a warm-up of 214 bars.
10. The backtest goldens and the existing feature identifiers are unchanged.
11. The Market chart picker shows every catalog entry; selecting ATR fraction draws an oscillator pane from the endpoint's values, and an unavailable spread shows its reason (component tests against mocked endpoints).
12. No module under `q_frontend/src` computes an indicator; `src/lib/indicators` is gone and a saved profile with SMA, Bollinger and RSI still renders.
13. `make contracts-check` passes in `q_backend` and `q_frontend`; the focused tests and each repository's lint and type checks pass.

## Implementation boundary

This issue authorizes only its listed deliverable after written-plan approval and
`./work start Q-112 --agent <agent> --worktree`.
The task branch also exists in `q_contracts` and `q_frontend`. No change to the live terminal, the execution worker, walk-forward logic or the cost model; no quote indicators as genome nodes; no roll or gap handling; no reason codes for missing values beyond the unavailable series.
