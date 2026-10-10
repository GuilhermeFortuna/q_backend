# Using the research library

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

| Function | Return | Description & Delegation |
|---|---|---|
| `ma(close, period, kind="sma")` | `Series` | Moving average (`sma`, `ema`, `wma`, `smma`, `hma`), case-insensitive |
| `rsi(close, period)` | `Series` | Wilder's Relative Strength Index |
| `atr(frame, period)` | `Series` | Wilder's Average True Range (requires `high`, `low`, `close`) |
| `bollinger(close, period, num_std=2.0)` | `(upper, middle, lower)` | Bollinger Bands with configurable standard deviation |
| `macd(close, fast_period=12, slow_period=26, signal_period=9)` | `(line, signal, hist)` | Moving Average Convergence Divergence |
| `donchian(frame, period)` | `(upper, lower)` | Donchian Channels (requires `high`, `low`) |
| `realized_vol(close, window, periods_per_year=252)` | `Series` | Annualized close-to-close realized volatility from log returns |
| `yang_zhang(frame, window, periods_per_year=252)` | `Series` | Annualized Yang-Zhang (2000) volatility (requires `open`, `high`, `low`, `close`, `window >= 2`) |

### Calculation rules & conventions

- **Index preservation:** All returned Series share the exact index (including timezone and name) of the input. Input objects are never mutated.
- **NaN warm-up:** Initial periods contain `NaN` according to standard indicator warm-up. No backfilling or zero-filling is applied.
- **Annualization:** Volatility helpers accept an explicit `periods_per_year` parameter (default `252` for daily bars). For intraday bars (e.g. M5), set `periods_per_year` explicitly according to trading sessions per year.
- **Validation:** Periods and windows must be integers $\ge 1$ (excluding booleans; Yang-Zhang window $\ge 2$). `num_std` must be finite and $> 0$. Non-numeric data, missing required columns, and infinite values fail immediately with descriptive errors. Volume columns are not required.

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
   - Returns `TradeOrder.buy()`, `TradeOrder.sell()`, or `None`.
   - `positions` is optional in your override's signature; existing `entry_strategy(self, frame)` methods remain supported.
3. **`exit_strategy(frame: DataFrame, positions: tuple[ResearchPosition, ...], *, phase="bar") -> TradeOrder | None`**:
   - Called on an isolated owned copy of closed-bar history up to the current bar, evaluated **before** `entry_strategy`.
   - Returns `TradeOrder.close()` or `None`. Defaults to returning `None`.
   - `positions` is optional in your override's signature; existing `exit_strategy(self, frame)` methods remain supported.
   - A `phase` keyword-only parameter opts the exit into intrabar evaluation (see [Stop and target orders](#stop-and-target-orders)). Without it, an exit is called once per closed bar, as before. `**kwargs` alone does not opt in.
4. **`chart_indicators() -> Sequence[ChartIndicator]`** (optional):
   - Declares the computed columns the Trade Chart draws, in order. Defaults to an empty sequence.
   - `ChartIndicator(column, pane="price", label="", color=None, line_style=None, line_width=None)`: `column` must be a numeric column added by `compute_indicators`; `pane` is `"price"` or `"oscillator"`; an empty `label` defaults to the column name.
   - Styling is optional: `color` is a CSS color string, `line_style` is `"solid"`, `"dashed"` or `"dotted"`, and `line_width` is a positive finite number in pixels. Unset values use the chart's defaults (solid, 1.2 px). An invalid `line_style` or `line_width` raises `ValueError` when the `ChartIndicator` is created.
   - Called once per backtest and does not receive the frame, so it cannot change results. A declared column that is missing, a market column (`open`, `high`, `low`, `close`), non-numeric or declared twice raises `ValueError` naming the strategy class and column.
   - No pane is inferred: a column that is not declared is not drawn.

   ```python
   class SmartMaCrossover(ResearchStrategy):
       def chart_indicators(self):
           return [
               ChartIndicator("short_ma", label="EMA 9", color="#4da3ff", line_style="dashed"),
               ChartIndicator("long_ma", label="WMA 20", color="#ff9f43", line_width=2),
               ChartIndicator("ma_delta", pane="oscillator"),
           ]
   ```

#### Entry levels and phase-aware exits

- `TradeOrder.buy(*, stop_loss=None, take_profit=None, price=None)` and `TradeOrder.sell(...)` attach price levels fixed at entry or a same-bar fill price. A buy's stop lies below its target and a sell's above it; when `price` is set, `stop_loss < price < take_profit` for a buy and `stop_loss > price > take_profit` for a sell; levels and prices must be finite and positive. `TradeOrder.close()` takes no levels or price. Invalid values raise `ValueError` when the order is built.
- An exit that declares `phase` is called three times per candle with open positions:
  - `phase="screen"` on the whole candle after queued fills. Return `TradeOrder.close()` to ask for the candle's ticks, not to exit. Screen with the candle's full high and low so that a crossing that reverses before the close still qualifies. Flat candles are never screened.
  - `phase="tick"` for the trade prices of a screened candle, in stored order. The frame ends with the candle observed so far: its `open` is the first trade price, `high` and `low` the extremes so far, `close` the current price and `tick_volume` the count of prices. Other columns of that row are `NaN`, and the indicators are recomputed on the raw history with that row, so they never see the completed candle. Returning `TradeOrder.close()` exits at that tick's trade price.
  - The tick call runs for the candle's first price and then only when the price changes. A print at an unchanged price leaves the forming candle's `open`, `high`, `low` and `close` as they were, so the exit is not asked again; `tick_volume` still counts every print. An exit rule that depends only on `tick_volume` growing at an unchanged price is therefore decided at the next price change.
  - `phase="bar"` is the ordinary closed-bar call after intrabar execution. A close here fills at the next bar's open.
- Hooks must be deterministic. Nothing computed in a screen call is carried into tick calls, and later information never justifies an earlier fill. A screen that qualifies but is not confirmed by any tick does not fill.
- With `workers` above 1 in `backtest()`, tick calls run in other processes and may be evaluated ahead of the tick that finally exits. The strategy must be picklable (a class defined at module level, or in a script guarded by `if __name__ == "__main__":`), and its hooks must not rely on side effects such as counters or recorded calls.
- A strategy with a phase-aware exit needs `ticks=` in `backtest()`.

```python
class StopTargetBreakout(ResearchStrategy):
    def entry_strategy(self, frame):
        last = frame.iloc[-1]
        if last["close"] > last["upper"]:
            return TradeOrder.buy(stop_loss=last["close"] - 2 * last["atr"], take_profit=last["close"] + 4 * last["atr"])
        return None

    def exit_strategy(self, frame, positions=(), *, phase="bar"):
        if phase == "bar" or not positions:
            return None
        floor = frame["exit_floor"].iloc[-1]
        reached = frame["low"].iloc[-1] <= floor if phase == "screen" else frame["close"].iloc[-1] <= floor
        return TradeOrder.close() if reached else None
```

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
| `quantity` | `int` | `1` | Fixed contract/share quantity per trade (positive integer). |
| `point_value` | `float` | `1.0` | Value per point/multiplier (finite positive float). Futures (e.g. `WIN$`, `WDO$`) must supply their multiplier. |
| `initial_capital` | `float` | `100000.0` | Starting capital in account currency. |
| `costs` | `TransactionCostConfig` | `None` | Per-contract and basis-point transaction costs. `None` models zero transaction costs. See [Transaction costs](#transaction-costs). |
| `exit_params` | `Mapping[str, Any]` | `None` | Optional stop-loss, take-profit, or trailing parameters (e.g. `stop_loss_pct`, `take_profit_atr`). See [Execution model](#execution-model). |
| `day_trade` | `bool` | `False` | Enables intraday session filtering. |
| `day_trade_start_time` | `str` | `"09:00"` | Earliest entry time (`HH:MM`). |
| `day_trade_end_time` | `str` | `"16:00"` | Latest new entry time (`HH:MM`). |
| `day_trade_close_time` | `str` | `"17:00"` | Mandatory session close time (`HH:MM`). |
| `force_close_at_end` | `bool` | `False` | Whether to force-close any open position at the final bar of the dataset. |
| `ticks` | `TickStore` | `None` | Store that confirms stop, target and phase-aware exits inside their candles. Its symbol must equal `symbol`. Required for those strategies; see [Stop and target orders](#stop-and-target-orders). |
| `workers` | `int \| "auto"` | `1` | Processes that share the tick phase of a phase-aware exit. `1` runs in-process. `"auto"` uses every CPU available to the process, limited to one worker per GiB of free memory. Results do not depend on the value; see [Performance and progress](#performance-and-progress). |
| `progress` | `bool` | `None` | Shows a progress bar on stderr. `None` shows it only when stderr is a terminal; `True` and `False` force it on or off. |

### Performance and progress

A phase-aware exit recomputes `compute_indicators` on the full history for every decided tick, so tick replay dominates the run time of a tick-confirmed backtest. Two things keep it short:

- Prints at an unchanged price are not decided again (see [Entry levels and phase-aware exits](#entry-levels-and-phase-aware-exits)), which usually removes most of a candle's ticks.
- `workers` spreads the remaining decisions of a candle over worker processes in waves and takes the earliest exit, so the trades are the same as with one process.

```python
result = backtest(bars, strategy=TrailingStop(), symbol="WDO$N", point_value=10.0, ticks=store, workers="auto")
```

Workers start on the first replayed candle and stop when `backtest()` returns. They share the imported libraries with one another, so each adds little memory beyond its copy of the history. Pass an explicit number to leave CPUs free for other work. Keep `compute_indicators` light: it is the unit of work that is repeated.

The progress bar shows bars decided out of the total, the current bar, the open position, the number of candles replayed from ticks, elapsed time and an estimate of the time left. A second line follows the ticks of the candle being replayed. A one-line summary remains when the run ends.

### Backtest results (`BacktestResult`)

- **`metrics: dict`**: Summary performance statistics computed across closed trades (`total_trades`, `total_pnl`, `win_rate`, `profit_factor`, `max_drawdown_value`, `max_drawdown_pct`, etc.).
- **`trades: DataFrame`**: Execution log with stable columns (`trade_id`, `symbol`, `side`, `status`, `entry_time`, `entry_price`, `exit_time`, `exit_price`, `pnl`, `quantity`, `commission`, `point_value`, `exit_reason`, `exit_tick_time`, `stop_loss`, `take_profit`). `exit_time` is the candle's timestamp. `exit_tick_time` is the time of the tick that closed a stop, target or phase-aware exit inside the candle, and `NaT` for other exits. `stop_loss` and `take_profit` are the levels the trade was opened with, `NaN` when unset.
- **`rejected_entries: DataFrame`**: Entries not taken because a level was already on the wrong side of the fill, with columns `time`, `side`, `fill_price`, `stop_loss` and `take_profit`. Empty when there are none.
- **`equity: DataFrame`**: Time series indexed by bar timestamp containing `realized_equity` (initial capital plus cumulative net PnL from closed trades). Open positions are not marked to market.
- **`data: DataFrame`**: Prepared historical bars augmented with user and exit indicator columns (internal signal arrays omitted). Keeps every computed column, declared or not.
- **`indicators: tuple[ChartIndicator, ...]`**: Chart series in declaration order. For a custom strategy, the columns from `chart_indicators()`; for a registered strategy run by name, its own chart indicators; empty when none are declared.
- **`config: Mapping`**: Read-only mapping recording the run arguments (`symbol`, `strategy`, `strategy_params`, `quantity`, `point_value`, `initial_capital`, `costs`, `exit_params`, `day_trade`, session times, `force_close_at_end`, and `timeframe`).

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
- Levels are fixed at entry. Existing `exit_params` rules, close requests without `phase`, and the `phase="bar"` close keep their next-open execution. `force_close_at_end` and day-trade closes are unchanged.
- Candle prices load only when a level's range is reached or a phase-aware screen qualifies, at most once per candle. Candles with no open trade read nothing. A candle's ticks run from its timestamp to the next bar in the same session, or to the end of that exchange day for its last bar, and never reach into the next day.
- The frame must be built from the same store with `TickStore.bars`, so that each loaded candle's first, highest, lowest and last prices equal its open, high, low and close. A mismatch raises `ValueError` naming the bar. A candle whose session is not stored raises `NoMarketDataError` naming the day and `TickStore.sync`. An empty interval fails with the bar's time; there is no fallback to next-open execution.
- Live trading decides on completed bars only, so a deployed strategy does not reproduce these intrabar fills.
- The history limit is the synced tick history. Old sessions the gateway no longer serves cannot be replayed.

- With `day_trade=True`: an entry is taken only from a signal bar whose time lies between the start and end times inclusive; open positions close at the open of the first bar at or after the close time; a position still open on the last bar of a calendar day closes at that bar's close.
- Without `day_trade`, positions carry across sessions. `force_close_at_end` closes at the last bar's close.
- `equity` is realized only, as the guide already says.

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

- `sync` / `sync_ticks` / `bars(..., sync=True)` walk weekdays from `start` through `end` (default end: yesterday in Brasília), skip days already on disk, and never store the current session. Each call needs the MT5 gateway (`Q_MT5_GATEWAY_URL`, optional `Q_MT5_GATEWAY_TOKEN`). Each missing day is fetched twice; a day is written only when both responses return the same row count. An empty pair is reported as **empty** (no file) and retried on the next sync. A mismatched pair is **unsettled**. Gateway errors are **failed** for that day only.
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
