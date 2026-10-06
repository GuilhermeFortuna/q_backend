# Q-095: Adjusted price series warning

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 16 — Research data and backtest correctness
**Depends on:** Q-093
**Implementation plan:** [Plan](../plans/Q-095-adjusted-price-series-warning-plan.md)

## Purpose

MetaTrader 5 offers each B3 continuous future in three forms, and the research guide and examples default to the one that distorts a point-value backtest.

| Symbol | Broker description | Prices | Observed for WDO on the first M10 bar of 2021-10-04 |
| --- | --- | --- | --- |
| `WDO$N` | "Sem Ajustes" | As traded; a gap at each contract roll | close 5400.5 |
| `WDO$D` | "Ajuste por Diferença" | Shifted by a constant at each roll; point differences preserved | close 7242.5 (+1842.0) |
| `WDO$` | "Ajuste Proporcional" | Multiplied by a factor at each roll; percentage returns preserved | close 7646.53 (×1.416) |

`backtest()` computes profit and loss as price difference × quantity × point value and charges costs per contract. On the proportionally adjusted series every historical price move is scaled by the cumulative roll factor, so 2021 WDO profits and losses are overstated by 42% while costs are not. Five years of `WDO$` M10 closes sit on the instrument's 0.5 tick grid 0.4% of the time; `WDO$N` and `WDO$D` sit on it 100% of the time.

Nothing tells the user. `docs/research-library.md` and the examples use `WIN$` throughout. After this task `load_bars` warns when the prices it loaded are off the instrument's tick grid, the guide explains the three forms, and the research examples default to the unadjusted series.

## Behaviour

### Tick-grid check in `load_bars`

- After the frame is built and validated, `load_bars` asks the gateway client for the symbol's information (`RemoteMt5Client.get_symbol_info`). When it supplies a finite, positive `trade_tick_size`, `load_bars` computes the share of open, high, low and close values that are not multiples of that tick size, within a small numeric tolerance.
- The result is recorded in `attrs["q_research"]` as `tick_size` and `off_tick_share` (0.0 to 1.0).
- When `off_tick_share` exceeds 0.01, `load_bars` emits one `AdjustedSeriesWarning` attributed to the caller. The message names the symbol, the share and the tick size, says that point-based profit and loss and per-contract costs are distorted on price-adjusted history, and points to the guide section.
- The check is best effort and never fails a load. If symbol information is missing, lacks a usable tick size or the request fails, no warning is emitted and the two `attrs` keys are absent.
- It costs one extra gateway request per `load_bars` call. `load_ticks` is unchanged.
- The data returned is not altered, filtered or re-scaled, and no series is substituted.

### Public name

`AdjustedSeriesWarning`, a `UserWarning` subclass defined in `src/q_backend/research/errors.py` and exported from `q_backend.research`, so users can silence it deliberately with the standard `warnings` filters.

### Guide and examples

- `docs/research-library.md` gains a "Choosing a price series" section: the three forms and how MetaTrader 5 names them, what each preserves and distorts, a recommendation (unadjusted for intraday backtests that close each session; difference-adjusted when positions are held across sessions; proportional only for analysis in percentage returns that uses neither point value nor per-contract costs), the warning and how to silence it.
- The guide's code snippets and the commands in the research part of `README.md` use `WIN$N`.
- `examples/research/rsi_reversion.py`, `examples/research/mt5_backtest.py` and `examples/research/load_market_data.py` default to or demonstrate `WIN$N`.

## Focused acceptance

1. With a mocked client, on-grid bars produce no warning and record `tick_size` with `off_tick_share == 0.0`.
2. Bars with more than 1% of prices off the grid produce exactly one `AdjustedSeriesWarning` and record the share; bars at or below the threshold do not warn.
3. Symbol information that is `None`, lacks `trade_tick_size`, carries a zero or non-finite value, or raises `ConnectionError` produces no warning, no `tick_size` or `off_tick_share` key and an otherwise identical frame.
4. `from q_backend.research import AdjustedSeriesWarning` works and importing the package stays inert.
5. Example tests assert the new default symbol.

Verification uses mocked gateway responses. No live terminal, Wine, Docker, GPU or desktop run.

## Delivery boundary

- A warning, not a block: adjusted series stay loadable, because percentage-return analysis legitimately uses them.
- No tick-size or instrument registry, no automatic point value and no change to `backtest()`.
- Platform presets that name `WIN$` or `WDO$` are untouched: backend defaults in `market_data/api_service.py`, `features/registry.py`, `optimization/hypothesis.py` and `market_data/exogenous_config.py`, and the `q_frontend` alpha-research presets. Which series each of those workflows should use is a product decision recorded in the Batch 16 document.
