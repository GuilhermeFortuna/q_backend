# Using the research library

> **Documentation Notice**: Traditional Pandas-style interactive HTML documentation for the Q research library is available in `docs/research/`. Run `make docs-serve` from `q_backend` to view the full User Guide, tutorial, and API reference.

The public interface lives in `q_backend.research`. This guide covers market-data
import, tick resampling, and indicator helpers.

## Setup

Use the backend's Python environment. From the workspace root:

```bash
cd q_backend
uv sync
```

Have MetaTrader 5 connected to your broker and Q's MT5 gateway running. Configure
`Q_MT5_GATEWAY_URL` and, if your gateway requires authentication,
`Q_MT5_GATEWAY_TOKEN`. The library also reads the existing runtime configuration;
you can override either setting with `gateway_url=` or `gateway_token=` on a call.

The gateway can run on the same Linux workstation under Wine or on another machine.
Data import requires neither PostgreSQL nor Redis, the control API, or a worker.
The library does not start services for you.

## Choosing a price series

MetaTrader 5 lists each B3 continuous future in three forms:

| Symbol suffix | Broker description (Portuguese) | Prices | What it preserves |
| --- | --- | --- | --- |
| `$N` (e.g. `WIN$N`) | Sem Ajustes | As traded; a gap at each roll | Point differences and per-contract costs match the exchange |
| `$D` (e.g. `WIN$D`) | Ajuste por Diferença | Shifted by a constant at each roll | Point differences between bars (not absolute price levels) |
| `$` (e.g. `WIN$`) | Ajuste Proporcional | Multiplied by a roll factor at each roll | Percentage returns; many prices fall off the tick grid |

`backtest()` computes profit and loss as price difference × quantity × point value and
charges costs per contract. On the proportionally adjusted series (`WIN$`, `WDO$`, …)
historical price moves are scaled by the cumulative roll factor while costs are not, which
distorts point-based backtests.

**Recommendation**

- **Intraday backtests that close each session:** use the unadjusted series (`$N`).
- **Positions held across sessions:** difference-adjusted (`$D`) keeps point-based P&L coherent across rolls.
- **Percentage-return analysis only:** proportional (`$`) is appropriate when you neither use
  point value nor per-contract costs.

`load_bars` performs a best-effort tick-grid check after each load. When more than 1% of
open, high, low, and close values are not multiples of the symbol's `trade_tick_size`, it
emits `AdjustedSeriesWarning` and records `tick_size` and `off_tick_share` in
`bars.attrs["q_research"]`. Adjusted series remain loadable; the warning is informational.

Silence it deliberately with the standard `warnings` filters:

```python
import warnings
from q_backend.research import AdjustedSeriesWarning

warnings.filterwarnings("ignore", category=AdjustedSeriesWarning)
```

## Import market data

Save this in a Python script:

```python
from q_backend.research import load_bars

bars = load_bars("WIN$N", timeframe="M5", start="2026-09-01")

print(bars.tail())
print(bars["close"])
```

Fetch raw ticks and resample them into minute bars:

```python
from q_backend.research import load_ticks, resample_ticks

ticks = load_ticks("WIN$N", start="2026-09-01")
minute_bars = resample_ticks(ticks, timeframe="M1")
```

Tick imports fetch consecutive calendar-day chunks without overlapping millisecond
boundaries. On a terminal, progress displays completed chunks, ticks received, the
current date range, elapsed time, and estimated remaining download time. The timer
keeps updating while MT5 synchronizes history; MT5 does not expose progress inside a
native history call, so the estimate becomes available only after chunks complete
and may vary with broker downloads. DataFrame construction is shown as a separate
status. Use `progress=True` to force the display or `progress=False` to silence it.

Both candle and tick history requests allow a 10-minute gateway operation and an
11-minute client read wait, rather than timing out after 30 seconds during normal
first-session downloads. A stalled gateway operation still fails with a bounded
timeout.

`load_ticks` returns a DataFrame indexed by timezone-aware Brasília tick time, with
`bid`, `ask`, `last`, `volume`, and `flags` columns. Same-millisecond events remain
separate rows in stable order. It fetches all MT5 tick events by default; pass
`flags=` to select an MT5 tick category. Tick reads bypass the shared disk cache.

The returned `flags` column contains strings instead of numeric MT5 bit masks.
Labels are joined with ` | ` in this order: `bid update`, `ask update`,
`last-price update`, `volume update`, `buy trade`, `sell trade`. For example,
`1336` becomes `last-price update | volume update | buy trade | undocumented bits (1280)`.
Bits outside the public MT5 definitions are preserved as `undocumented bits (N)`,
where `N` is their combined numeric mask; no meaning is inferred for them. A zero
mask becomes `no flags`. Both side bits produce both `buy trade` and `sell trade`;
treat that combination as an unknown trade direction.

An update label does not guarantee a numerically different value from the preceding
row: successive trades can have the same price and size. Quote events carry the
previous `last` and `volume`, so do not sum volume across all rows. To select buy
trades with an unambiguous direction:

```python
buy = ticks["flags"].str.contains("buy trade", regex=False)
sell = ticks["flags"].str.contains("sell trade", regex=False)
buy_trades = ticks.loc[buy & ~sell]
```

Code that previously applied bitwise operations to the returned `flags` column
must use string matching instead. The input `flags=` filter remains numeric;
lower-level market-data clients continue to return numeric flags.

`resample_ticks(ticks, timeframe=...)` accepts the same canonical MT5 timeframe
names as `load_bars`. Bars use positive `last` prices for OHLC and count those trade
updates in `tick_volume`. Quote-only updates do not contribute prices or volume.
Empty intervals between the first and last eligible trade are forward-filled from
the prior close with zero `tick_volume`; no bars are added before the first eligible
trade. The result contains `open`, `high`, `low`, `close`, and `tick_volume`.

Run your script from `q_backend` with `uv run python your_script.py`.

`bars` is an ordinary pandas DataFrame. Each call fetches fresh history from the
connected MT5 terminal. Omitting `end` requests data through now; only completed
candles are returned, so the current forming candle is excluded. There is no stored
data or cache fallback if MT5 is unavailable.

For a fixed date range, provide `end`:

```python
bars = load_bars(
    "WIN$N",
    timeframe="M5",
    start="2026-09-01",
    end="2026-09-30T18:00:00-03:00",
)
```

## Arguments and dates

| Argument | Meaning |
|----------|---------|
| `symbol` | MT5 symbol (non-empty) |
| `timeframe` | Canonical name (`M1` … `MN1`), case-insensitive |
| `start` | Inclusive bar-open timestamp, as an ISO string or Python `datetime` |
| `end` | Inclusive bar-open timestamp; defaults to now |
| `gateway_url` / `gateway_token` | Override `Q_MT5_GATEWAY_URL` / `Q_MT5_GATEWAY_TOKEN` for this call only |

`load_ticks` uses the same `symbol`, `start`, `end`, and gateway override arguments;
it has no `timeframe` argument and accepts optional `flags` (default: all ticks).

Bounds use `America/Sao_Paulo`. Naive datetimes and date-only strings (`YYYY-MM-DD`) are
interpreted in Brasília; a date-only **end** is midnight on that day, not the full
calendar day. Aware inputs convert to exchange time.

## Returned data

- Bar index `time`: timezone-aware Brasília, sorted, unique. Tick index `time` is
  sorted and may contain duplicates when MT5 reports events in the same millisecond.
- Columns: `open`, `high`, `low`, `close` (`float64`); `tick_volume` (`int64`);
  `spread`, `real_volume` (`float64`, may be NaN).
- `tick_volume` is MT5 tick volume; it is distinct from `real_volume`.
- `bars.attrs["q_research"]` contains source and query metadata. When symbol information
  is available, it may also include `tick_size` and `off_tick_share` from the adjusted-series
  tick-grid check (see [Choosing a price series](#choosing-a-price-series)).

You can save a fetched frame for later experiments using pandas:

```python
bars.to_parquet("win_m5.parquet")
```

`load_bars` returns the whole requested range in one call, automatically paging
through the gateway if the range exceeds the gateway's per-response limit (50,000 bars).
The available history depth still depends on the connected terminal and broker. A shorter
result is returned as supplied; missing candles are not filled in. No completed candles
raises `NoMarketDataError`, which you can import from `q_backend.research`.
If the gateway cannot finish a bounded history scan, it returns an error rather than
incomplete success; retry with a narrower range.
Missing gateway configuration or invalid arguments raise `ValueError`; an unavailable
gateway raises `ConnectionError`.

Offline work uses a pandas frame you already have; this module is for live MT5 history.

## Indicator helpers (`q_backend.research.indicators`)

The `indicators` module provides discoverable, functional helpers backed by Q's high-performance
Rust calculations (`q_core`) and existing backend bridges. Indicators operate on ordinary pandas
Series and DataFrames without requiring a database, Redis, Celery/Dramatiq, or an MT5 gateway.

```python
from q_backend.research import load_bars, indicators

# 1. Fetch fresh bars (requires running MT5 gateway)
bars = load_bars("WIN$N", timeframe="M5", start="2026-09-01")

# 2. Enrich with indicators (offline, pure calculations)
bars["rsi"] = indicators.rsi(bars["close"], period=14)
bars["ema_21"] = indicators.ma(bars["close"], period=21, kind="ema")
bars["atr"] = indicators.atr(bars, period=14)

# Multi-output indicators unpack tuples in standard order
upper, middle, lower = indicators.bollinger(bars["close"], period=20, num_std=2.0)
bars = bars.assign(bb_upper=upper, bb_middle=middle, bb_lower=lower)

macd_line, signal_line, hist = indicators.macd(bars["close"], fast_period=12, slow_period=26, signal_period=9)
donchian_high, donchian_low = indicators.donchian(bars, period=20)
bars["realized_vol"] = indicators.realized_vol(bars["close"], window=20, periods_per_year=252)
bars["yang_zhang_vol"] = indicators.yang_zhang(bars, window=20, periods_per_year=252)
```

### Supported functions

Every identifier in the Market chart catalog is available through these public
helpers. The five moving-average identifiers use `ma(kind=...)`; the remaining
identifiers have matching helper names. Existing helpers and their signatures
remain supported, including annualized `yang_zhang`.

| Function | Input / output | Meaning |
|---|---|---|
| `ma(close, period, kind="sma")` | Series → Series | SMA, EMA, WMA, SMMA, or HMA |
| `volume_ma(volume, period)` | Series → Series | Simple average of a chosen volume field |
| `vwap(frame)` | OHLCV frame → Series | Intraday VWAP of typical price, reset each calendar day |
| `rsi(close, period)` | Series → Series | Wilder RSI |
| `atr(frame, period)` | OHLC frame → Series | Wilder true-range average, in price units |
| `atr_fraction(frame, period)` | OHLC frame → Series | ATR / current close, as a fraction |
| `true_range(frame)` | OHLC frame → Series | Current range including the previous-close gap |
| `bollinger(close, period, num_std=2.0)` | Series → `(upper, middle, lower)` | Sample standard-deviation price bands |
| `macd(close, fast_period=12, slow_period=26, signal_period=9)` | Series → `(line, signal, histogram)` | Price EMA spread and signal |
| `donchian(frame, period)` | Frame → `(upper, lower)` | Trailing highest high and lowest low |
| `rolling_zscore(close, window)` | Series → Series | Sample z-score of closes |
| `rolling_rank(close, window)` | Series → Series | Fraction of trailing closes ≤ current close, including ties |
| `pct_change(close, change_bars=1)` | Series → Series | Simple return as a decimal: 0.01 means 1% |
| `log_return(close, horizon=1)` | Series → Series | Natural-log price return |
| `price_zscore_log(close, window)` | Series → Series | Sample z-score of log closes; window ≥ 2 |
| `ewma_trend_spread(close, *, fast_half_life=8.0, slow_half_life=32.0)` | Series → Series | Fast-minus-slow log-price EWMA |
| `normalized_momentum(frame, *, horizon=20, ...)` | OHLC frame → Series | Horizon log return / (per-bar volatility × √horizon) |
| `normalized_ewma_trend_spread(frame, *, fast_half_life=8.0, slow_half_life=32.0, ...)` | OHLC frame → Series | Log-price EWMA spread / per-bar volatility |
| `realized_vol(close, window, periods_per_year=252)` | Series → Series | Annualized centered sample log-return deviation |
| `yang_zhang(frame, window, periods_per_year=252)` | OHLC frame → Series | Annualized OHLC volatility including gaps |
| `per_bar_realized_vol(close, window)` | Series → Series | Unannualized root mean square of log returns |
| `per_bar_ewma_vol(close, half_life=32.0)` | Series → Series | Square root of an EWMA of squared log returns |
| `rogers_satchell(frame, window)` | OHLC frame → Series | Per-bar OHLC volatility excluding opening gaps |
| `per_bar_yang_zhang(frame, window)` | OHLC frame → Series | Yang–Zhang with `periods_per_year=1` |
| `volatility_log_ratio(frame, *, vol_estimator="realized", short_window=10, long_window=40, short_half_life=8.0, long_half_life=32.0)` | OHLC frame → Series | ln(short volatility / long volatility) |
| `quote_spread(frame, *, ticks, timeframe, ...)` | Timestamped frame + TickStore → Series | Quoted bid–ask spread in basis points |
| `spread_vs_volatility(frame, *, ticks, timeframe, ...)` | OHLC frame + TickStore → Series | Relative quoted spread / per-bar volatility |
| `top_bottom(frame, *, atr_period=14, reversal_atr=2.0)` | OHLC frame → DataFrame | Causal ATR-threshold zigzag: confirmed top and bottom prices, their pivot bars, and leg direction |

### Calculation rules and conventions

- **Index preservation:** Results retain the input labels, timezone, and index name. Inputs are never mutated.
- **Warm-up and missing values:** Initial history and undefined calculations remain NaN; no filling is applied. Recursive half-life kernels need enough observations for their own warm-up. These helpers never load extra history.
- **Units:** Simple returns and ATR fractions are decimals, not multiplied by 100. Rolling rank includes ties (a constant window ranks 1). Z-scores use sample deviation. `realized_vol` and `yang_zhang` are annualized; per-bar helpers are not. Per-bar realized volatility uses RMS returns, while annualized realized volatility uses centered sample deviation.
- **Validation:** Integer parameters reject booleans and fractional values; floats must be finite and satisfy their documented bounds. Required columns must contain real numeric data and be free of infinities; complex inputs are rejected before conversion. Missing and non-positive prices follow each kernel's documented behavior. Research lookbacks retain the existing unbounded convention without HTTP resource caps.

### Volatility composites

`normalized_momentum`, `normalized_ewma_trend_spread`, and
`spread_vs_volatility` share keyword-only options:
`vol_estimator="realized"`, `vol_window=20`, `vol_half_life=32.0`, and `vol_lag=1`.
Choose `realized`, `ewma`, `rogers_satchell`, or `yang_zhang`, case-insensitively.
EWMA uses a positive half-life and ignores the window; other estimators use a
window and ignore the half-life. Yang–Zhang requires a window ≥ 2. Unused inputs
may be None. A lag of 1 uses the previous bar's volatility, while 0 uses the
current bar. Missing or near-zero volatility leaves NaN ratios.

Fast half-lives must be smaller than slow half-lives. `volatility_log_ratio`
requires short < long for windows, or for half-lives when using EWMA.

```python
from q_backend.research import indicators

bars["rank"] = indicators.rolling_rank(bars["close"], window=20)
bars["return"] = indicators.log_return(bars["close"], horizon=5)
bars["volume_ma"] = indicators.volume_ma(bars["tick_volume"], period=20)
bars["momentum"] = indicators.normalized_momentum(
    bars, horizon=20, vol_estimator="ewma", vol_half_life=16.0,
)
bars["volatility_regime"] = indicators.volatility_log_ratio(
    bars, short_window=10, long_window=40,
)
```

### Liquidity from local quote history

Quote helpers require an explicit `TickStore` and fixed `timeframe` (M1, M5,
M10, M15, H1, H2, H4, or D1, matching the shared quote service). They read
already-stored files and never fetch or synchronize ticks. Frames need a
unique, chronological DatetimeIndex of non-overlapping bar-open times; aware
times are aligned in Brasília and naive times are interpreted as Brasília local
time. Returned Series retain the original index. Any `q_research` symbol or
timeframe metadata must match the explicit store and timeframe.

```python
from q_backend.research import TickStore, indicators

ticks = TickStore("WIN$N")  # Existing local quote history.
spread = indicators.quote_spread(bars, ticks=ticks, timeframe="M5", min_coverage=0.5)
ratio = indicators.spread_vs_volatility(bars, ticks=ticks, timeframe="M5", vol_window=20)
print(spread.attrs["unavailable_reason"])
```

Defaults are time-weighted mean spread, `max_quote_age_ms=60_000`, and
`min_coverage=0.0`. Select `output="last"` or `"max"` on `quote_spread`, or
`spread_output` on `spread_vs_volatility`. Last means a quote valid at bar close.
Coverage is a fraction between 0 and 1; quote age is a nonnegative integer.
Quotes never carry across sessions, and stale or under-covered bars remain NaN.

Quoted spread is in **basis points** (1 bp = 0.01%); spread versus volatility is
**dimensionless** and uses relative spread as its numerator. If no requested
session has ticks, the result is all NaN and `Series.attrs["unavailable_reason"]`
contains the service explanation. The attribute is None for empty results or
when at least one requested session has ticks, even if its quotes are unusable.

### Volume at price from local trades

Trade helpers use the same explicit `TickStore` and never synchronize or call the
gateway. A trade is a tick with the buy or sell bit set; volume is binned on the
instrument `tick_size` grid (from the caller or `frame.attrs["q_research"]["tick_size"]`).
`volume_at_price` returns `rows` and `segments` frames per session, bar, or range,
using the exclusive range `[start, end)`. Bar profiles require `timeframe`; their
grid aligns to local midnight, with boundary segments clipped to the query range.
`value_area_levels` aligns POC, VAH and VAL to bar opens: `developing` uses trades
from the session's beginning strictly before each bar's close, including trades
before the first requested bar. `prior_session` repeats the previous stored
session's final levels, including a predecessor outside the requested window
(NaN only when no earlier session is stored). Sync contiguous history before
`prior_session`. The Market chart profile route enforces
`Q_MARKET_VOLUME_PROFILE_MAX_ROWS` (default 200,000 rows).

```python
from q_backend.research import TickStore, indicators, load_bars

ticks = TickStore("WIN$N")
bars = load_bars("WIN$N", "M5", start="2026-10-01", end="2026-10-10")
profile = indicators.volume_at_price(
    ticks, start="2026-10-01", end="2026-10-10", tick_size=0.5, by="session"
)
levels = indicators.value_area_levels(bars, ticks=ticks, timeframe="M5", mode="prior_session")
```

## Strategy definition & local backtesting (`ResearchStrategy`, `backtest`)

The `ResearchStrategy` ABC and `backtest()` function allow defining custom strategy classes or running built-in candle strategies synchronously against pandas DataFrames using Q's deterministic execution engine (`q_core`).

### Writing a ResearchStrategy

A strategy class provides an indicator hook and two decision hooks. Both decision
hooks can receive actual open positions by declaring a `positions` parameter.
The following strategy enters long or short on RSI threshold crossings and exits
according to the filled position's direction. It may also declare chart series
with `chart_indicators` (see [Strategy hooks and causality](#strategy-hooks-and-causality)).

```python
from q_backend.research import ResearchPosition, ResearchStrategy, TradeOrder, indicators, backtest

class RSIReversion(ResearchStrategy):
    def __init__(self, period: int = 14):
        self.period = period

    def compute_indicators(self, frame):
        frame = frame.copy()
        frame["rsi"] = indicators.rsi(frame["close"], self.period)
        return frame

    def entry_strategy(self, frame, positions: tuple[ResearchPosition, ...]):
        if positions or len(frame) < 2:
            return None
        previous, current = frame["rsi"].iloc[-2:]
        if previous >= 30 and current < 30:
            return TradeOrder.buy()
        if previous <= 70 and current > 70:
            return TradeOrder.sell()
        return None

    def exit_strategy(self, frame, positions: tuple[ResearchPosition, ...]):
        rsi = frame["rsi"].iloc[-1]
        for position in positions:
            if position.side == "long" and rsi > 50:
                return TradeOrder.close()
            if position.side == "short" and rsi < 50:
                return TradeOrder.close()
        return None
```

### Strategy hooks and causality

1. **`compute_indicators(frame: DataFrame) -> DataFrame`**:
   - Called once on an owned copy of the entire historical frame.
   - Defaults to returning the input frame unchanged.
   - May add new indicator columns. Must NOT modify, drop, or reorder index or market columns (`open`, `high`, `low`, `close`), and must NOT introduce reserved columns (`q_signal_*`, `bar_index`).
   - Must be strictly **causal**: all calculations must only depend on rows $\le$ current row. Centered rolling windows, `shift(-1)`, or whole-frame statistics (global mean, min, max) leak future information and invalidate research results.
2. **`entry_strategy(frame: DataFrame, positions: tuple[ResearchPosition, ...]) -> TradeOrder | None`**:
   - Abstract method called on an isolated owned copy of closed-bar history up to the current bar.
   - Returns a buy or sell of any kind (see [Resting entry orders](#resting-entry-orders)), `TradeOrder.cancel()` to remove the working entry order, or `None`.
   - `positions` is optional in your override's signature; existing `entry_strategy(self, frame)` methods remain supported.
3. **`exit_strategy(frame: DataFrame, positions: tuple[ResearchPosition, ...], *, ticks=None) -> TradeOrder | None`**:
   - Called on an isolated owned copy of closed-bar history up to the current bar, evaluated **before** `entry_strategy`.
   - Returns `TradeOrder.close(price=...)`, `TradeOrder.close()`, `TradeOrder.amend(...)` to move the open position's stop or target, or `None`. Defaults to returning `None`. See [Priced closes and exits without phase](#priced-closes-and-exits-without-phase) and [Moving stops and targets](#moving-stops-and-targets).
   - `positions` is optional in your override's signature; existing `exit_strategy(self, frame)` methods remain supported.
   - The hook runs once per closed bar. Declaring `ticks` provides data through the bar's end; it does not change when the hook runs. A `phase` parameter is rejected because it was removed. `**kwargs` alone does not receive `ticks`.
4. **`chart_indicators() -> Sequence[ChartIndicator]`** (optional):
   - Declares the computed columns the Trade Chart draws, in order. Defaults to an empty sequence.
   - `ChartIndicator(column, pane="price", label="", color=None, line_style=None, line_width=None, kind="line", ...)`: `column` must be a numeric column added by `compute_indicators`; `pane` is `"price"` or `"oscillator"`; an empty `label` defaults to the column name.
   - Styling is optional: `color` is a CSS color string, `line_style` is `"solid"`, `"dashed"` or `"dotted"`, and `line_width` is a positive finite number in pixels. Unset values use the chart's defaults (solid, 1.2 px). An invalid `line_style` or `line_width` raises `ValueError` when the `ChartIndicator` is created.
   - `kind` chooses how the series is drawn on the Trade Chart: `"line"` (default), `"step"`, `"markers"` (one glyph per non-null value), `"histogram"` (oscillator pane only, bars from `baseline`), or `"segment"` (see below). The same kinds, fields and rules apply to studies published with `publish_study`, except `"segment"`.
   - Marker fields: `shape` is `"circle"` (default), `"square"`, `"diamond"`, `"triangle-up"`, `"triangle-down"` or `"cross"`; `size` is the glyph width in pixels (at most 32, default 8); `position` is `"value"` (default, at the value), `"above-bar"` or `"below-bar"` (a fixed gap above the high or below the low, skipping zeros, so a boolean column marks the bars where it is true). `above-bar` and `below-bar` are price-pane only.
   - Histogram and fill fields: `negative_color` colours bars below `baseline` (default `0`); `fill_to` names another `line` or `step` series in the same pane and shades the area between them at `fill_opacity` (default `0.15`).
   - Segment: `kind="segment"` draws a horizontal line at `column`'s value from the bar named by `start_column` to the bar holding the value. `start_column` is a column of bar labels, such as a pivot's index, and is required for `segment` and rejected for other kinds. Use it for pivots: the segment starts at the pivot bar and ends at the bar that confirmed it. A segment whose start label is not in the frame is not drawn. Segments are price-pane only.
   - Called once per backtest and does not receive the frame, so it cannot change results. A declared column that is missing, a market column (`open`, `high`, `low`, `close`), non-numeric or declared twice raises `ValueError` naming the strategy class and column. So does a `fill_to` that names no other same-pane line or step series, and a `start_column` missing from `compute_indicators`.
   - No pane is inferred: a column that is not declared is not drawn.

   ```python
   class SmartMaCrossover(ResearchStrategy):
       def chart_indicators(self):
           return [
               ChartIndicator("short_ma", label="EMA 9", color="#4da3ff", line_style="dashed"),
               ChartIndicator("long_ma", label="WMA 20", color="#ff9f43", line_width=2),
               ChartIndicator("ma_delta", pane="oscillator"),
               ChartIndicator("entry", kind="markers", shape="triangle-up", position="below-bar", color="#26a69a"),
           ]
   ```

   Pivot segments from `top_bottom` (the column holds the pivot price on its confirming bar, and `top_index` holds the pivot bar):

   ```python
   class MorningBreakout(ResearchStrategy):
       def compute_indicators(self, frame):
           return frame.join(top_bottom(frame), how="left")

       def chart_indicators(self):
           return [
               ChartIndicator("top", kind="segment", start_column="top_index", color="#26a69a", label="Top"),
               ChartIndicator("bottom", kind="segment", start_column="bottom_index", color="#ef5350", label="Bottom"),
           ]
   ```

#### Priced closes and exits without phase

- `TradeOrder.buy(*, stop_loss=None, take_profit=None, price=None)` and `TradeOrder.sell(...)` attach entry levels or a same-bar entry price. `TradeOrder.amend` moves protective levels later, and `buy_stop`, `buy_limit`, `sell_stop` and `sell_limit` place resting entries. Entry prices and levels must be finite and positive.
- `TradeOrder.close(price=None)` requests a close. Without `price`, it fills at the next bar's open as before. With a finite positive `price`, the kernel checks that the value lies within the current candle's low and high, then scans its stored trades after the position opened. The close fills at the requested price, for a long or a short, at the time of the first trade that reaches it from the side the scan started on, so the same order works as a stop-style or a target-style exit. A protective stop or target that fills earlier takes precedence.
- A priced close requires `ticks=` in `backtest()`. If no stored trade reaches the price after entry, the position stays open. A price outside the candle range raises `ValueError` with the bar and price.
- Exit hooks run once per closed bar, after intrabar protective fills. They receive the complete closed-bar frame and, when they declare `ticks`, a `TickData` cut off at that bar's end. Calculate the requested level from information available before the candle opened, then use the current bar's range and ticks to decide whether to return `TradeOrder.close(price=level)`. A phase-based hook must be migrated: remove `phase`, and explicitly pass the level.
- With `day_trade=True` and `workers` above 1, sessions run in worker processes. The strategy must be picklable, and its hooks must depend only on their arguments and state that resets within a session.

```python
class StopTargetBreakout(ResearchStrategy):
    def entry_strategy(self, frame):
        last = frame.iloc[-1]
        if last["close"] > last["upper"]:
            return TradeOrder.buy(stop_loss=last["close"] - 2 * last["atr"], take_profit=last["close"] + 4 * last["atr"])
        return None

    def exit_strategy(self, frame, positions=(), *, ticks):
        if not positions or len(frame) < 11:
            return None
        floor = frame["low"].iloc[-11:-1].min()
        if frame["low"].iloc[-1] <= floor and (ticks.bar()["last"] <= floor).any():
            return TradeOrder.close(price=float(floor))
        return None
```

#### Tick data in hooks

`compute_indicators`, `entry_strategy` and `exit_strategy` can declare a `ticks` parameter and read the backtest's tick store through `q_backend.research.TickData`. The parameter is passed by keyword and only when it is named explicitly; `**kwargs` alone does not receive it.

- **Optional:** `ticks=None` (or any default). The hook receives a `TickData` when `backtest()` has `ticks=`, and `None` otherwise.
- **Required:** `ticks` with no default. `backtest()` without `ticks=` raises `ValueError` before any hook runs, naming the strategy class and the hook.

`TickData` has three members:

- `ticks.bar(timestamp=None)` returns the stored ticks of the candle that opens at `timestamp`, a value of `frame.index`. `None` means the frame's last bar. A candle spans from its open to the next bar's open in the same session, or to the end of the session day for the session's last bar. A timestamp that is not a bar of the backtest, or that opens at or after the cutoff, raises `ValueError`.
- `ticks.between(start, end)` returns the stored ticks in `[start, end)` across sessions, with the same bounds as `TickStore.ticks`. `end` is clipped to the cutoff. A `start` at or after the cutoff raises `ValueError`.
- `ticks.cutoff` is the latest moment the instance can return ticks for.

Frames have the columns and index of `TickStore.ticks` (`bid`, `ask`, `last`, `volume`, `flags`, indexed in `America/Sao_Paulo`). Writing to a returned frame changes nothing that later calls see. A session the frame has bars in but the store does not hold raises `NoMarketDataError` naming the session; days without bars in the frame, such as weekends, are skipped. Nothing is read until a hook calls `bar` or `between`, and each session is read once per run.

| Call | Cutoff |
|---|---|
| `compute_indicators` | end of the frame's last bar |
| `entry_strategy`, `exit_strategy` | end of the current bar |

Every decision hook sees the same end-of-bar cutoff. `phase` is not accepted by `exit_strategy`.

With `check_causality=True`, each prefix re-run of `compute_indicators` receives a `TickData` cut off at the prefix's end, so a column that reads later ticks is reported by the causality check.

A per-bar entry filter, from the current candle's traded volume:

```python
from q_backend.research import ResearchStrategy, TradeOrder


class BusyCandleEntry(ResearchStrategy):
    def entry_strategy(self, frame, positions=(), ticks=None):
        if ticks is None or positions:
            return None
        return TradeOrder.buy() if ticks.bar()["volume"].sum() > 100 else None
```

A `compute_indicators` column built with `between` once per session. Each bar gets the traded volume of the previous session, which is known at the session's open:

```python
import pandas as pd

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import ResearchStrategy


class PriorSessionVolume(ResearchStrategy):
    def compute_indicators(self, frame, *, ticks):
        sessions = frame.index.tz_convert(BRASILIA_TZ).normalize()
        prior = {day: ticks.between(day - pd.Timedelta(days=1), day)["volume"].sum() for day in sessions.unique()}
        return frame.assign(prior_volume=[prior[day] for day in sessions])

    def entry_strategy(self, frame, positions=()):
        return None
```

The same `ticks` argument serves three smaller patterns. Reading an earlier candle, as long as it is a bar of the frame, with `bar(timestamp)`:

```python
from q_backend.research import ResearchStrategy, TradeOrder


class FollowsBusyPreviousCandle(ResearchStrategy):
    def entry_strategy(self, frame, positions=(), ticks=None):
        if ticks is None or len(frame) < 2 or positions:
            return None
        previous_trades = ticks.bar(frame.index[-2])["last"].size
        return TradeOrder.buy() if previous_trades > 50 else None
```

A running total from the current candle's open up to the cutoff. Here `between` reads the trades so far in the bar, so the value is known when the bar closes:

```python
class RunningBarVolume(ResearchStrategy):
    def entry_strategy(self, frame, positions=(), ticks=None):
        if ticks is None or positions:
            return None
        so_far = ticks.between(frame.index[-1], ticks.cutoff)["volume"].sum()
        return TradeOrder.buy() if so_far > 500 else None
```

A priced close requests a level calculated from prior bars, then checks whether a current-bar trade crosses it. The strategy needs `ticks=` in `backtest()`:

```python
class PricedStopExit(ResearchStrategy):
    def entry_strategy(self, frame, positions=()):
        return None

    def exit_strategy(self, frame, positions=(), *, ticks):
        if not positions or len(frame) < 2:
            return None
        stop = positions[0].entry_price - 2
        if frame["low"].iloc[-1] <= stop and (ticks.bar()["last"] <= stop).any():
            return TradeOrder.close(price=stop)
        return None
```

#### Trailing stops with `amend`

Use `amend` for a protective trailing stop that should persist from the next bar. Use a priced close for an exit level calculated before the current candle opens that should apply only to the current bar.

```python
class TrailingLow(ResearchStrategy):
    def entry_strategy(self, frame, positions=()):
        if positions:
            return None
        return TradeOrder.buy(stop_loss=frame["low"].iloc[-1] - 5.0)

    def exit_strategy(self, frame, positions=()):
        if not positions or positions[0].side != "long":
            return None
        new_stop = frame["low"].iloc[-1]
        if positions[0].stop_loss is None or new_stop > positions[0].stop_loss:
            return TradeOrder.amend(stop_loss=new_stop)
        return None
```

- An amendment decided on bar `i` applies from the first price of bar `i+1`, and the decision on bar `i+1` shows the new level in `positions[0].stop_loss`.
- The trailing stop closes at the first tick at or beyond the latest amended level, with reason `STOP_LOSS`.
- Levels and amendments need `ticks=` in `backtest()`.

### Open position context

After updating your checkout, run `uv sync` from `q_backend` before using these
hooks. The backend pins `q_core` release `v2026.10.08.2`, which supports position
context. An older core without this capability raises a descriptive error when
you run a strategy that declares `positions`.

Both decision hooks may declare a `positions` parameter. Existing `(self, frame)`
overrides continue working, and each hook can choose its signature independently.
A keyword-only parameter (`def exit_strategy(self, frame, *, positions)`) is also
supported. Parameters must explicitly be named `positions`; `*args` or `**kwargs`
alone do not opt into context. Type annotations are optional; the parameter name
selects the capability. `compute_indicators` and `chart_indicators` keep their
existing signatures and do not receive positions.

`positions` is a tuple of immutable `ResearchPosition` snapshots, exported from
`q_backend.research`:

| Field | Type | Meaning |
|---|---|---|
| `symbol` | `str` | Backtest instrument symbol. |
| `side` | `Literal["long", "short"]` | Direction of the filled position. |
| `entry_time` | `pandas.Timestamp` | Actual entry timestamp in the research frame's timezone. |
| `entry_price` | `float` | Actual filled entry price. |
| `quantity` | `float` | Actual filled quantity from engine sizing. |
| `stop_loss` | `float` or `None` | Stop level in force at this decision, after any amendment; `None` when unset. |
| `take_profit` | `float` or `None` | Target level in force at this decision, after any amendment; `None` when unset. |

The tuple is empty (`()`) when flat. With the research backtest's fixed-quantity
sizing, it contains at most one position for the backtest symbol. Pending entry
requests do not appear in it. Do not mutate snapshots or use an order you returned
as evidence that a position exists.

To migrate only your exit hook, keep your entry hook unchanged and add the named
parameter to the exit hook:

```python
def exit_strategy(self, frame, *, positions):
    close = frame["close"].iloc[-1]
    for position in positions:
        if position.side == "long" and close < position.entry_price:
            return TradeOrder.close()
        if position.side == "short" and close > position.entry_price:
            return TradeOrder.close()
    return None
```

This is a decision based on the completed bar's close; its order fills at the next
open. It does not place an intrabar stop at `entry_price`.

For a complete strategy using both hooks, this example avoids requesting new
entries while a position is open and chooses an exit based on its direction:

```python
from q_backend.research import ResearchPosition, ResearchStrategy, TradeOrder


class DirectionalExit(ResearchStrategy):
    def entry_strategy(self, frame, positions: tuple[ResearchPosition, ...]):
        if positions or len(frame) < 2:
            return None
        change = frame["close"].iloc[-1] - frame["close"].iloc[-2]
        if change > 0:
            return TradeOrder.buy()
        if change < 0:
            return TradeOrder.sell()
        return None

    def exit_strategy(self, frame, positions: tuple[ResearchPosition, ...]):
        if len(frame) < 2:
            return None
        falling = frame["close"].iloc[-1] < frame["close"].iloc[-2]
        rising = frame["close"].iloc[-1] > frame["close"].iloc[-2]
        for position in positions:
            if (position.side == "long" and falling) or (position.side == "short" and rising):
                return TradeOrder.close()
        return None
```

Exit runs before entry, and both receive the same position tuple. Returning a close
request leaves those positions visible to the entry hook until the close fills at
the next open. Returning an opposite entry alongside a close therefore still allows
a reversal at the next open. `TradeOrder.close()` closes all open positions for the
backtest symbol; it does not target individual snapshots.

At each bar, the engine first executes queued orders and any intrabar protective
fills, then builds the snapshot for that bar's decisions. For example, an entry
requested on bar A is absent from bar A's snapshot and becomes visible on bar B
if it fills at bar B's open. A close requested on bar B leaves the position in
both hooks' bar B snapshot; it disappears on bar C after that close fills.
If a protective fill closes the position before a bar's hooks, they receive an
empty tuple.

The examples' `if positions: return None` entry guard also prevents a same-bar
reversal: a pending close is still visible. To reverse at the next open, your
entry hook must deliberately return the opposite entry while your exit hook
returns `TradeOrder.close()` on the same decision bar.

Both hooks run on every bar, even while positions are open and on final or
session-gated bars. The engine applies its normal entry capacity and session gates;
a request need not fill. End-of-day closure at the final bar's close and terminal
force-close occur after that bar's hooks. Empty frames call no hooks.

Keep hooks deterministic functions of their history and position snapshot. Use
the supplied snapshot to determine whether a position is open; tracking assumed
fills in instance variables can produce incorrect decisions. Frame copying and
Python evaluation remain slower than built-in vectorized strategies.

### Running a backtest

```python
result = backtest(
    bars,
    strategy=RSIReversion(),
    symbol="WIN$N",
    quantity=1,
    point_value=0.20,
    initial_capital=10_000.0,
)

print(result.metrics)
print(result.trades)
print(result.equity)
```

### Configuration parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `frame` | `DataFrame` | Required | Timezone-aware OHLCV frame (`America/Sao_Paulo` or convertible). Naive datetimes fail closed. |
| `strategy` | `ResearchStrategy \| str` | Required | Custom strategy instance or registered candle strategy name (e.g. `"MACrossover"`). Tick or ML-dependent strategies fail explicitly. |
| `symbol` | `str` | Required | Traded instrument symbol. |
| `strategy_params` | `Mapping[str, Any]` | `None` | Parameters for built-in strategies. Rejected for custom strategy instances. |
| `quantity` | `int` | `1` | Fixed contract/share quantity per trade (positive integer). Mutually exclusive with `position_sizing`. |
| `position_sizing` | `PositionSizingConfig` | `None` | Sizing rule for each entry: `fixed_quantity`, `fixed_safety_margin` or `inverse_volatility`. Inverse volatility reads a `volatility` column from the indicator frame. |
| `point_value` | `float` | `1.0` | Value per point/multiplier (finite positive float). Futures (e.g. `WIN$`, `WDO$`) must supply their multiplier. |
| `initial_capital` | `float` | `100000.0` | Starting capital in account currency. |
| `costs` | `TransactionCostConfig` | `None` | Per-contract and basis-point transaction costs. `None` models zero transaction costs. See [Transaction costs](#transaction-costs). |
| `exit_params` | `Mapping[str, Any]` | `None` | Optional stop-loss, take-profit, or trailing parameters (e.g. `stop_loss_pct`, `take_profit_atr`). See [Execution model](#execution-model). |
| `day_trade` | `bool` | `False` | Enables intraday session filtering. |
| `day_trade_start_time` | `str` | `"09:00"` | Earliest entry time (`HH:MM`). |
| `day_trade_end_time` | `str` | `"16:00"` | Latest new entry time (`HH:MM`). |
| `day_trade_close_time` | `str` | `"17:00"` | Mandatory session close time (`HH:MM`). |
| `post_exit_bars` | `int` | `10` | Bars after exit included in post-exit MFE, MAE, and return measurements (1–500). |
| `excursions` | `bool` | `True` | Measure trade excursions. Set `False` to skip measurement for parameter searches that do not use excursion fields; those columns remain null. |
| `force_close_at_end` | `bool` | `False` | Whether to force-close any open position at the final bar of the dataset. |
| `ticks` | `TickStore` | `None` | Store that confirms protective orders and priced closes inside their candles. Its symbol must equal `symbol`. Required for those orders; see [Priced closes and exits without phase](#priced-closes-and-exits-without-phase). |
| `workers` | `int \| "auto"` | `1` | Processes that run the sessions of a day-trade backtest. `1` runs in-process. `"auto"` uses every CPU available to the process, limited to one worker per GiB of free memory. Runs that are not day-trade are sequential. Results do not depend on the value; see [Performance and progress](#performance-and-progress). |
| `check_causality` | `bool` | `False` | Recomputes `compute_indicators` on the history up to session ends before the run, and raises if an indicator reads a later bar. Custom strategies only; see [Checking causality](#checking-causality). |
| `progress` | `bool` | `None` | Shows a progress bar on stderr. `None` shows it only when stderr is a terminal; `True` and `False` force it on or off. |

### Performance and progress

A tick-confirmed backtest costs one Python decision per closed bar, plus Rust work that scans the ticks of candles with a protective order or priced close. Candle prices come from the store's session files inside the kernel, so they are not read into Python one bar at a time. Hook frames are views of the prepared history, so building one takes the same time whatever the history length.

Two options set how the work is spread:

- `workers` with `day_trade=True` runs the sessions of the run in worker processes. The sessions are split into up to `workers` contiguous blocks of whole sessions, each block runs in its own process, and the blocks are joined in session order. Trades, metrics, equity, rejected entries and data are the same whatever the number of workers. Each worker starts from the strategy as it was after `compute_indicators`, so attributes that hooks write are not shared between workers; this is why hook decisions must depend only on their arguments.
- With `day_trade=False` the run is sequential, whatever `workers` is.

```python
result = backtest(bars, strategy=TrailingStop(), symbol="WDO$N", point_value=10.0, ticks=store, day_trade=True, workers="auto")
```

Workers start with the first session of the run and stop when `backtest()` returns. They share the imported libraries with one another, so each adds little memory beyond its copy of the history. Pass an explicit number to leave CPUs free for other work. `compute_indicators` runs once per run, so its cost is paid once however many ticks are decided.

### Checking causality

`backtest(..., check_causality=True)` recomputes `compute_indicators` on the history up to up to twenty evenly spaced session ends, before the run. Each column is compared with the full run, and the first value that differs raises `ValueError` naming the column and the bar. An indicator that reads a later bar, such as `close.shift(-1)`, fails this way. A history of one session has no earlier session end to check. The check is off by default, and when it is off `compute_indicators` runs once.

The progress bar shows bars decided out of the total, the current bar, the open position, elapsed time and an estimate of the time left. A one-line summary remains when the run ends.

### Backtest results (`BacktestResult`)

- **`metrics: dict`**: Summary performance statistics computed across closed trades (`total_trades`, `total_pnl`, `win_rate`, `profit_factor`, `max_drawdown_value`, `max_drawdown_pct`, etc.).
- **`trades: DataFrame`**: Execution log with stable columns (`trade_id`, `symbol`, `side`, `status`, `entry_time`, `entry_price`, `exit_time`, `exit_price`, `pnl`, `quantity`, `commission`, `point_value`, `exit_reason`, `exit_tick_time`, `stop_loss`, `take_profit`, `order_kind`, `entry_tick_time`, `final_stop_loss`, `final_take_profit`). `exit_time` is the candle's timestamp. `exit_tick_time` is the time of the tick that closed a stop, target or priced close inside the candle, and `NaT` for other exits. `stop_loss` and `take_profit` are the levels the trade was opened with, `NaN` when unset. `order_kind` is `market`, `priced`, `stop` or `limit`. `entry_tick_time` is the tick that filled a resting order, `NaT` otherwise. `final_stop_loss` and `final_take_profit` are the levels in force when the trade closed, `NaN` when unset.
- **`rejected_entries: DataFrame`**: Entries not taken because a level was already on the wrong side of the fill, with columns `time`, `side`, `fill_price`, `stop_loss` and `take_profit`. Empty when there are none.
- **`equity: DataFrame`**: Time series indexed by bar timestamp containing `realized_equity` (initial capital plus cumulative net PnL from closed trades). Open positions are not marked to market.
- **`data: DataFrame`**: Prepared historical bars augmented with user and exit indicator columns (internal signal arrays omitted). Keeps every computed column, declared or not.
- **`indicators: tuple[ChartIndicator, ...]`**: Chart series in declaration order, with their kind and styling. For a custom strategy, the columns from `chart_indicators()`; for a registered strategy run by name, its own chart indicators; empty when none are declared.
- **`config: Mapping`**: Read-only mapping recording the run arguments (`symbol`, `strategy`, `strategy_params`, `position_sizing`, `quantity` for fixed-quantity sizing, `point_value`, `initial_capital`, `costs`, `exit_params`, `day_trade`, session times, `force_close_at_end`, and `timeframe`).

### Strategy files (`load_strategy`)

A `ResearchStrategy` kept as a file in `Q_STRATEGY_DIR` (default `~/q-strategies`) is listed, validated and backtested by the Research stack, and each run freezes the file's exact code into an immutable strategy version. Declare parameters with `IntParam`, `FloatParam`, `BoolParam`, `ChoiceParam` and `TimeParam` in a class attribute `parameters`, one entry per `__init__` argument. Scripts and Optuna drivers load the same code with `load_strategy()`:

```python
from q_backend.research import backtest, load_strategy

SessionBreakout = load_strategy("session_breakout")
result = backtest(bars, strategy=SessionBreakout(range_bars=6), symbol="WIN$N", day_trade=True)
```

The directory rules, parameter declarations, validation, versions and the Backtests workflow are in [`docs/research/user_guide/strategy_files.rst`](research/user_guide/strategy_files.rst). The example is in `examples/research/strategy_files/`.

### Publish a backtest to the Research stack

A research script can record its finished backtest in the Research stack for visual review:

```python
result = backtest(bars, strategy=SmartMaCrossover(), symbol="CCM$", quantity=1, point_value=450.0)
run_id = result.publish()
```

- **History and views:** Once published, the run appears in Backtests history on the Research desktop (`origin: script`). The Trade Chart, Performance, Monthly breakdown, and Trade List views are fully populated.
- **Chart indicators:** Chart series drawn in the Trade Chart come directly from `chart_indicators()`.
- **Review only:** A published run is a record for review and analysis. Because custom strategy code lives in the research script, the run cannot be re-run or optimised from the desktop.
- **API URL:** `publish()` sends the run to `POST /api/v1/backtests/import`. The base URL defaults to `http://127.0.0.1:8001` (the address `./dev research` serves) and can be configured via the `Q_API_URL` environment variable or overridden with `api_url=`.
- **Strategy name and timeframe:** `name` defaults to `result.config["strategy"]` and `timeframe` defaults to `result.config["timeframe"]` (from `bars.attrs["q_research"]["timeframe"]`). Both can be overridden explicitly: `result.publish(name="MyModel", timeframe="H1")`. If the input frame lacks timeframe metadata, `timeframe=` is required.
- **Run lifecycle:** Publishing never alters the `BacktestResult` and never retries automatically. Each call creates a new run in the stack.

### Publish figures to the Research stack

A research script can publish Matplotlib and Plotly figures to the Research stack, where they are stored in the lake and displayed in the desktop Figures tab:

```python
from q_backend import research

# Optional: apply Q dark theme to Matplotlib and Plotly
research.use_figure_style()

# 1. Matplotlib figure (PNG by default, or SVG)
fig, ax = plt.subplots()
ax.plot(returns.cumsum())
fig_id = research.publish_figure(fig, title="Cumulative Return", notes="Strategy run 2026-09")

# 2. Plotly figure (stored as interactive JSON)
p_fig = go.Figure(data=go.Scatter(x=df.index, y=df["equity"]))
p_fig.update_layout(title="Interactive Equity Curve")
fig_id_plotly = research.publish_figure(p_fig)
```

**One-call charts.** For the common plots, skip the plotting code entirely. Each function draws an interactive Plotly chart in the Q dark style and publishes it in one call, returning the figure id:

```python
from q_backend import research

research.line(equity, title="Equity", collection="regime_study", ylabel="BRL")
research.drawdown(equity, title="Drawdown", collection="regime_study")
research.bar(monthly_pnl, title="Monthly P&L", collection="regime_study")
research.histogram(returns, title="Return distribution", bins=50)
research.scatter(rsi, fwd_return, title="RSI vs forward return", xlabel="RSI", ylabel="Fwd return")
```

**Candlesticks with indicators.** `candlestick` takes OHLC candles and draws your indicators over the price (`overlays`) or in their own panes below it (`panels`), all sharing the time axis:

```python
research.candlestick(
    candles,                                   # DataFrame: open, high, low, close, time index
    title="WDO M10 — breakout study",
    overlays={
        "EMA 20": ema20,                       # a Series draws as a line
        "VWAP": vwap,
        "Entry": research.Indicator(entries, kind="scatter"),   # markers
    },
    panels={
        "RSI": rsi,
        "My score": research.Indicator(score, kind="scatter"),
    },
    collection="breakout_study",
)
```

- **Indicators** are pandas Series aligned with the candles. A numpy array also works, if its length matches the candles; it is aligned to their index. Wrap a value in `research.Indicator(...)` to choose `kind="line"` (default) or `kind="scatter"`, and to set a `color`.
- **Candles** need `open`, `high`, `low` and `close` columns in any case. Gains are emerald and losses rose.
- **Charts are interactive** in the Figures tab: zoom, pan and hover.
- **Data:** pandas Series or DataFrame, or numpy/list values. `line` plots a DataFrame as one line per column with a legend; `bar` draws grouped bars for a DataFrame. `drawdown`, `histogram` and single-series `bar` take one series.
- **Arguments:** `title` is required. `xlabel`, `ylabel`, `collection`, `notes`, `replace`, `api_url` and `client` work as in `publish_figure`. `histogram` also takes `bins`.
- **Colours:** a single-series `bar` colours gains gold and losses rose. `drawdown` plots the distance from the running peak as a percentage.
- **Empty, mismatched or incomplete data** raises `ValueError` before anything is sent.
- **Anything else** (other chart types, custom styling) is still a Matplotlib or Plotly figure passed to `publish_figure`.

- **Supported figure types:** A `matplotlib.figure.Figure`, any object with a `.figure` attribute (such as an `Axes` or a seaborn `FacetGrid`), or a `plotly.graph_objects.Figure`. Recognized without importing either library; unsupported objects raise `TypeError`.
- **Rendering & formats:** Matplotlib figures are rendered via `savefig` at `dpi` (default 200) with a tight bounding box and the figure's own face colour, as `"png"` (default) or `"svg"`. The caller's figure object is neither closed nor resized. Plotly figures are serialized via `fig.to_json()` and served interactively.
- **Title and collection:** `title` defaults to the figure's own title (`fig._suptitle`, single axes title, or `fig.layout.title.text`). When no title is found, `ValueError` is raised naming `title=`. `collection` groups figures in the desktop and defaults to the script file stem (or `"interactive"` when run in a REPL or notebook).
- **Replacement:** By default (`replace=True`), publishing a figure with an existing `(collection, title)` replaces the existing figure in place (`replaced: True`, revision incremented, old lake file removed), preventing duplicate accumulation when re-running a research script. Pass `replace=False` to add a new figure each time.
- **Style helper:** `research.use_figure_style("dark")` applies the Q dark theme (carbon background, silver text, gold/brass accent palette) to Matplotlib `rcParams` and registers the `q_dark` template as default in Plotly. It is opt-in; `publish_figure` never restyles a figure.
- **Size limit:** Decoded content is capped by `Q_RESEARCH_FIGURE_MAX_BYTES` (default 32 MiB). Exceeding this limit returns HTTP `413` and raises `ValueError`.
- **API URL and errors:** Like `publish()`, target URL defaults to `Q_API_URL` or `http://127.0.0.1:8001`. An unreachable API raises `ConnectionError` naming `./dev research`. HTTP 422 or 413 raises `ValueError` with the server detail.

### Publish studies to the Market chart

A pandas Series or DataFrame indexed by bar time can be published as a **study** for one symbol and timeframe. The Market chart lists it in its indicator menu, under **Studies**, while that symbol is on the chart, and draws it like a catalog indicator:

```python
from q_backend import research
from q_backend.research import ChartIndicator

bars = research.load_bars("WDO$N", timeframe="M10", start="2026-01-01")
study = keltner(bars)                                  # DataFrame on bars.index

research.publish_study(study, symbol="WDO$N", name="Keltner 20/2")
research.publish_study(
    study,
    symbol="WDO$N",
    name="Keltner 20/2 + width",
    series=[
        ChartIndicator("kc_upper", color="#6eb5ff"),
        ChartIndicator("kc_lower", color="#6eb5ff"),
        ChartIndicator("kc_width", pane="oscillator", label="Width"),
    ],
)
```

`examples/research/publish_study.py` loads bars, computes a Bollinger band and its width with `research.indicators`, and publishes them.

- **Data:** a `pandas.Series` or `DataFrame` with a `DatetimeIndex` that is strictly increasing. A timezone-aware index is converted to UTC. A naive index is read as Brasília exchange time, as `load_bars` reads naive input. Every column must be numeric or boolean; booleans become `0` and `1`. `NaN` and infinities become gaps. An unnamed Series is published under the key `value`.
- **Series:** without `series=`, every column is published in `pane` (`"price"` by default) with its name as key and label. With `series=`, a list of `ChartIndicator` publishes only the named columns, each with its own pane, label, colour, line style, width and plot kind (see **Chart indicators** under research strategies). A missing or duplicate column, an empty selection, or `kind="segment"` (which is for the backtest Trade Chart only) raises `ValueError`.
- **Timeframe:** `timeframe=` is one of `M1`, `M5`, `M10`, `M15`, `M30`, `H1`, `H2`, `H4` or `D1`. When omitted, it is the smallest gap between consecutive timestamps, which must be one of them. Session and weekend gaps are ignored when inferring. Either way, every gap must be a whole multiple of the timeframe and every timestamp must fall on a whole minute; otherwise `ValueError` names the gap found and `timeframe=`.
- **Symbol:** upper-cased. A study is shown only for that exact symbol: one published for `WDO$N` does not appear on `WDOV26`.
- **Replacement:** a study is identified by symbol, timeframe and name. By default (`replace=True`) publishing an existing identity replaces its values and styling in place, keeps its id, and increments its revision; the chart refetches it. Pass `replace=False` to get `ValueError` on a conflict.
- **Alignment:** values are placed on the bar with the same timestamp, and a bar with no value breaks the line. Nothing is shifted or resampled. When some values in the loaded range fall between bars, the chart notice reports how many, which usually means a timezone mismatch or timestamps that are not bar open times.
- **Size limit:** timestamps times series is capped by `Q_RESEARCH_STUDY_MAX_VALUES` (default 2,000,000). Exceeding it returns HTTP `413` and raises `ValueError`.
- **API URL and errors:** as for `publish_figure`. Unreachable API raises `ConnectionError` naming `./dev research`; HTTP 422, 409 or 413 raises `ValueError` with the server detail.

## Execution model

- Strategy hooks see completed bars only. An unpriced entry or close decided on a bar fills at the next bar's open; a priced entry fills on the deciding bar (see [Priced entry orders](#priced-entry-orders)).
- Exit rules follow the catalog text from Q-094: price-level rules evaluate each completed bar against its high or low, while time stops count completed bars. Triggered exits close at the next bar's open, so the exit price can differ from the level. A rule can trigger on the entry bar. Exit parameter values must match the registry's types and bounds; nonfinite values are rejected.
- One position per symbol under fixed-quantity sizing: repeated entry requests do not stack, and an opposite entry request is skipped while the position cap is full. Returning a close and an opposite entry on the same bar reverses at the next open.
- Entries with levels fill at the next bar's open. A level already on the wrong side of that fill (a long stop at or above it, a long target at or below it, or the reverse for a short) rejects the entry, which is reported in `rejected_entries`.

#### Priced entry orders

- `TradeOrder.buy(price=...)` and `TradeOrder.sell(price=...)` accept a keyword-only `price`, an optional finite positive number. `TradeOrder.close()` takes none.
- When `stop_loss` or `take_profit` is set, a buy requires `stop_loss < price < take_profit` and a sell `stop_loss > price > take_profit`. Construction raises `ValueError` otherwise.
- An order without `price` fills at the next bar's open as before.
- A priced order fills on the bar whose frame ended with the decision, at exactly `price`, charged the configured per-side cost.
- No order type is declared. The kernel fills at `price` when `low <= price <= high` of that bar, whether the level lies above the open (breakout) or below it (pullback). A priced entry fills on its deciding bar, so it is never queued for the next one.
- A price outside the bar's range raises `ValueError` naming the strategy, the bar, the price and the bar's range `[low, high]`. The backtest does not continue.
- A priced order with `stop_loss` or `take_profit` resolves them from ticks that trade after the touch and therefore needs `ticks=`. A priced order without levels needs no ticks.
- A rejected entry (levels on the wrong side of the price) is reported in `rejected_entries`.
- **Research only:** This fill model is for research only. Deployed strategies and the live forward evaluator decide on completed bars and do not reproduce same-bar fills.
- **Causality caveat:** The hook sees the whole bar. A condition on the bar's close or low combined with a priced fill can use information that was not yet known at the fill time. The kernel checks that the price lies in the bar's range; it cannot check the strategy's causality.
  The example below shows a causal pattern, checking that the bar traded through the previous high:

```python
class PreviousHighBreakout(ResearchStrategy):
    def entry_strategy(self, frame):
        if len(frame) >= 2 and frame["high"].iloc[-1] > frame["high"].iloc[-2]:
            return TradeOrder.buy(price=frame["high"].iloc[-2])
        return None
```

#### Stop and target orders

- A stop triggers on the first trade price at or beyond its level and fills at that price, so a gap through the level fills at the gap price. A target triggers on the first trade price strictly beyond its level and fills at the level, so a price equal to the target does not fill.
- When both levels lie inside one candle, the order in which prices traded decides. Protective and custom exits resolve in one chronological scan. At the same tick, a protective fill takes precedence over a custom exit. A trade cannot close on the tick that opened it.
- Existing `exit_params` rules and close requests without an explicit price keep their next-open execution. `force_close_at_end` and day-trade closes are unchanged.
- Candle prices load only when a protective order or priced close needs confirmation, at most once per candle. A candle's ticks run from its timestamp to the next bar in the same session, or to the end of that exchange day for its last bar, and never reach into the next day.
- The frame must be built from the same store with `TickStore.bars`, so that each loaded candle's first, highest, lowest and last prices equal its open, high, low and close. A mismatch raises `ValueError` naming the bar. A candle whose session is not stored raises `NoMarketDataError` naming the day and `TickStore.sync`. An empty interval fails with the bar's time; there is no fallback to next-open execution.
- Live trading decides on completed bars only, so a deployed strategy does not reproduce these intrabar fills.
- The history limit is the synced tick history. Old sessions the gateway no longer serves cannot be replayed.

- With `day_trade=True`: an entry is taken only from a signal bar whose time lies between the start and end times inclusive; open positions close at the open of the first bar at or after the close time; a position still open on the last bar of a calendar day closes at that bar's close.
- Without `day_trade`, positions carry across sessions. `force_close_at_end` closes at the last bar's close.
- `equity` is realized only, as the guide already says.

#### Resting entry orders

- `TradeOrder.buy_stop(price, ...)`, `buy_limit`, `sell_stop` and `sell_limit` place a working entry that fills from ticks. `stop_loss`, `take_profit` and `expire_bars` are keyword-only, and a stop or limit order needs a price. The same side rules as a priced entry apply to the levels.
- A stop fills at the first trade at or beyond its price, at that trade's price, so a gap fills at the gap price. A limit fills at its own price on the first trade strictly beyond it, so a trade equal to the limit does not fill.
- The order works from the first price of the bar after the one that placed it. `expire_bars` (default `1`) is how many bars after the placing bar it may fill on: an order placed on bar `i` with `expire_bars=k` fills on bars `i+1` to `i+k`. `expire_bars=None` keeps it until it fills, is replaced or is cancelled. In day-trade mode it also lapses at the force-close bar and after the session's last bar.
- One working entry at a time. Any new entry decision (market, priced or resting) replaces it, and `TradeOrder.cancel()` removes it. A replaced or cancelled order does not fill.
- Within one price, protective exits resolve first, then a custom exit, then the resting fill. A protective exit and a reversing resting entry at the same price therefore close, then open. A new trade never exits on the price that filled it.
- Resting orders need `ticks=` in `backtest()`, and a resting order returned without it raises `ValueError` naming the strategy, the bar and the `ticks` argument.
- A resting fill is recorded with `order_kind` `stop` or `limit` and `entry_tick_time`, the tick that filled it (see [Backtest results](#backtest-results-backtestresult)).

#### Moving stops and targets

- `TradeOrder.amend(*, stop_loss=None, take_profit=None)` returned from `exit_strategy` moves the stop and/or target of every open position from the next bar's first price. It needs at least one level, and a level left out keeps its current value. While flat it does nothing.
- An amended level already beyond the next bar's open fills at that open, by the gap rule. An amendment never changes an exit rule or an entry decided in the same call.
- `amend` is allowed from `exit_strategy`. Returning it from `entry_strategy` raises `ValueError` naming the strategy, hook and bar. `cancel` is allowed from `entry_strategy` only.
- Amendments need `ticks=` in `backtest()`.

## Transaction costs

- `costs=None` means zero cost. `TransactionCostConfig.cost_per_contract` is charged per contract on each side.
- Protective and custom tick exits are traded prices, and they pay the configured per-side cost like every other fill. A target is a resting order, so its fill is charged the same per-side cost, including the half-spread term.
- A realistic per-side cost for a market order is the exchange and broker fee per side plus half the spread: `fee_per_side + 0.5 × tick_size × point_value` when the spread is one tick.
- Worked example for the mini dollar future with `point_value=10.0`, a 0.5-point tick and an assumed fee of R$1.25 per side: `cost_per_contract=3.75`, R$7.50 per round trip, 0.75 points. The fee is an assumption the reader replaces with their own.
- Measurement behind the half-spread term: on 250 `WDO$N` sessions from 2025-10-01 to 2026-10-05, a market order sent within one to three seconds of a 10-minute bar's open paid 0.25 points per side beyond the bar's recorded open price, and the spread at those moments averaged 0.50 points.

## Reading tick rows

- Every row carries the terminal's current bid and ask, including trade rows. The flags say which fields changed on that row; an unchanged quote produces no new quote row.
- The time since the last quote-update row is therefore not a measure of staleness. To price a moment, use the bid and ask of the latest row at or before it.
- Rows can carry a zero bid or ask and must be filtered before computing a midpoint or spread.
- A row with both the buy and the sell flag has an unknown aggressor, as the guide already says.

## Tick store

Research scripts can keep gateway tick history on disk under `data/tick_store/` by default (`Q_RESEARCH_TICK_STORE` overrides the root). Layout: one zstd Parquet file per symbol slug and exchange calendar day (`<root>/<slug>/<YYYY-MM-DD>.parquet`) with gateway columns unchanged (`time_msc`, `bid`, `ask`, `last`, `volume`, `flags`). This store is separate from the API tick cache (`Q_TICK_CACHE_DIR`) and from the stack market catalog.

```python
from q_backend.research import TickStore, sync_ticks

store = TickStore("WDO$N")
# One-liner while developing (gateway must be up; skips days already on disk):
bars = store.bars("M10", start="2025-10-01", sync=True)
# Or sync explicitly, then read offline:
sync_ticks("WDO$N", start="2025-10-01")
bars = store.bars("M10", start="2025-10-01")
ticks = store.ticks(start="2026-10-05", end="2026-10-06")
```

CLI (from `q_backend`):

```bash
uv run q-sync-ticks --symbol 'WDO$N' --start 2025-10-01
```

To re-import sessions already on disk, for example after switching data sources, add `--overwrite` (or `overwrite=True` in Python). Each weekday in range is downloaded again and replaces its stored file, and its cached M1 bars are dropped. Any day that is not stored again, because the source reports it empty or the download fails, is removed rather than kept, so the store never mixes sources.

- `sync` / `sync_ticks` / `bars(..., sync=True)` consider weekdays from `start` through `end` (default end: yesterday in Brasília), skip sessions already on disk unless `overwrite=True`, and never store the current session. An entirely stored interval returns locally without a gateway. Missing intervals require `Q_MT5_GATEWAY_URL` and optional `Q_MT5_GATEWAY_TOKEN`.
- Missing intervals stream once through `/v1/ticks/page`, starting with 250,000-tick batches. The first request triggers MT5 history synchronization and supplies usable data. Cursors follow actual ticks across empty dates; they do not request each empty day separately. Timestamp groups are kept intact, including duplicate ticks. One producer thread downloads while the caller writes incremental Parquet row groups; at most two pages queue ahead, with one additional page held by the producer and one by the consumer. Native requests remain serialized.
- A daily file is committed atomically only after a successful cursor passes that day's end. An empty scanned weekday is **empty** (no file) and retried next time. On error, completed days remain stored; the unfinished day and remaining dates in that interval are **failed**, with reasons in `report.errors`. Temporary files are removed before draining any outstanding download, bounded by the existing transport timeout. Independent missing intervals continue after that producer terminates. `q-sync-ticks` exits with status 1 if any session failed. `unsettled` remains for compatibility and is always empty. `first_msc`, `last_msc`, and `ticks_received` report received data, including any unfinished day; they do not certify global broker retention.
- Progress shows preparation/download intervals, ticks received, throughput, writes, and completed sessions. Cold synchronization can still wait inside MT5. Native errors never become confirmed empty history. An older gateway raises an actionable update/restart error: restart the updated gateway with `./dev restart gateway` from the workspace root.

To compare the former daily double-fetch loop with streaming using temporary stores:

```bash
uv run python examples/research/benchmark_tick_sync.py \
  --symbol 'WDO$N' --start 2026-10-01 --end 2026-10-07 \
  --gateway-url http://127.0.0.1:18812 --output /tmp/tick-sync-benchmark.json
```

Use closed sessions with known ticks; `--empty-day YYYY-MM-DD` adds a known-empty case.
`--optimized-first` reverses execution order to reveal cache bias. The benchmark
compares every tick and reports wall time, HTTP, decoding, disk writes, requests,
bytes and cumulative process peak RSS (not separate memory peaks per case). The gateway logs native-fetch and encoding
times separately. It retains terminal caches, so these runs do not claim a
controlled cold-cache comparison. Neither path changes existing tick-store files.
- `ticks` returns the same frame as `load_ticks` for stored sessions. `bars` builds `load_bars`-shaped OHLCV from positive `last` trade prices only, per session, with no overnight bar synthesis. History is limited to synced sessions; use `sessions()` to see what is on disk.
- One-minute bars per session are cached under `<slug>/bars_M1/` the first time a session is read; coarser timeframes aggregate from that cache.
- Broker tick history is short and old sessions can stop being served after the terminal drops them. Sync regularly while the gateway still has the sessions you need.

## History depth and completeness

- `load_bars` and `load_ticks` return what the terminal has. Compare `attrs["q_research"]["returned_start"]` with the requested start before trusting a range.
- The terminal keeps a limited number of bars per symbol and timeframe (its "Max bars in chart" setting, 100,000 by default). At that setting on 2026-10-06 `WDO$N` held M1 from 2026-01-22, M5 from 2023-03-17 and M10 from 2021-10-04, the last being the start of broker history.
- The first tick request for a symbol can return nothing while the terminal downloads history, and tick history depth differs by symbol. MetaTrader 5 reports success with an empty result in both cases, so the gateway cannot tell them apart. `load_ticks` raises `NoMarketDataError` for an empty result and returns a shorter frame, without error, when only part of the range has ticks. Retry after a few seconds and check the returned range.

## Example scripts

### Sync tick history to the research store
```bash
uv run python examples/research/sync_ticks.py \
  --symbol 'WDO$N' --start 2025-10-01
```

### Fetch live market data
```bash
uv run python examples/research/load_market_data.py \
  --symbol 'WIN$N' --timeframe M5 --start 2026-09-01
```

### Offline indicator enrichment
```bash
uv run python examples/research/add_indicators.py \
  --input data/bars.parquet \
  --output data/enriched_bars.parquet
```

### Stop and target orders over a tick store
```bash
uv run python examples/research/stop_target_backtest.py \
  --symbol 'WDO$N' --start 2025-10-01 --point-value 10
```

Prints metrics, exit reasons, the tick time of each exit inside a candle, and rejected entries. Sessions in the window must already be synced.

### Tick backtest benchmark
```bash
uv run python examples/research/tick_backtest_benchmark.py --sessions 60 --workers 4
```

Writes a synthetic store to a temporary directory and times the same trailing-stop backtest in day-trade mode, twice: once with an `amend` exit and once with a priced close. Each runs with `workers=1` and with `--workers`, and the script prints the timings. Use it to compare two versions of the library on one machine.

### Resting stop entries and trailing amendments
```bash
uv run python examples/research/resting_orders_backtest.py
```

Runs a buy-stop break-out with a trailing stop moved by `amend`, over a synthetic session written to a temporary directory, so it needs no synced data. Prints each trade's `order_kind`, `entry_tick_time` and final levels.

### Offline RSI reversion backtest
```bash
uv run python examples/research/rsi_reversion.py \
  --input data/bars.parquet \
  --symbol 'WIN$N' \
  --period 14 \
  --quantity 1 \
  --point-value 0.20 \
  --cost-per-contract 1.00
```

### Live MT5 gateway backtest
```bash
uv run python examples/research/mt5_backtest.py \
  --symbol 'WIN$N' \
  --timeframe M5 \
  --start 2026-09-01 \
  --strategy custom \
  --cost-per-contract 1.00
```

## Market chart indicators (Q-112)

The Market workspace no longer computes indicators in the frontend. The control API
catalog at `GET /api/v1/market/indicators` lists every chartable kernel (identifier,
category, parameters, outputs, pane, and required data). The series route
`POST /api/v1/market/indicators/{symbol}/series` returns bar timestamps for the requested
range and one value array per output, aligned to those times; missing values are
`null`, and quote-based indicators mark the whole output unavailable when ticks are
absent.

- **Timestamps** are the UTC instants the OHLCV route returns for the same bars, so a
  client can join a series to its bars by timestamp.
- **Warm-up is loaded in bars.** Each catalog entry declares how many bars it needs before
  its first value; the service widens its lookback across overnight, weekend and holiday
  gaps until that many bars precede the range, or history runs out. A value therefore does
  not depend on where the requested range starts. A recursive average (EMA, SMMA, RSI, ATR,
  MACD) never fully forgets its seed: its warm-up ends when the seed weighs at most 1%, the
  rule `q_core` uses for half-life kernels.
- **Parameters are bounded.** Unknown names, non-integer periods, non-finite numbers and
  values outside the catalog's minimum and maximum return 422 naming the parameter.
- **Quoted spread** is aggregated by `q_backend.market_data.quote_spread`, which the feature
  store's `quote_spread` and `spread_vs_volatility` features also use. A bar covers
  `[open, open + timeframe)` on the tick store's clock, where the UTC wall clock of
  `time_msc` reads as Brasília local time. Each bar is aggregated from the ticks of its own
  calendar day, which is its session key, and a quote is carried for at most
  `max_quote_age_ms` (one hour at most), so no value depends on another day. A day without
  stored ticks gives missing values; the output is marked unavailable only when no day of
  the range has ticks. In the feature store a missing quote is a missing value and never
  moves a matrix's `valid_from`.

Research strategies, discovery, and the feature store use the same `q_core` values
through the Python wrappers in `q_backend.backtesting.technical_indicators` and the
genome/feature registries. When validating a chart overlay against a backtest, compare
against those backend series—not a local TypeScript implementation.
