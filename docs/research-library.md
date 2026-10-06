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

## Import market data

Save this in a Python script:

```python
from q_backend.research import load_bars

bars = load_bars("WIN$", timeframe="M5", start="2026-09-01")

print(bars.tail())
print(bars["close"])
```

Fetch raw ticks and resample them into minute bars:

```python
from q_backend.research import load_ticks, resample_ticks

ticks = load_ticks("WIN$", start="2026-09-01")
minute_bars = resample_ticks(ticks, timeframe="M1")
```

`load_ticks` returns a DataFrame indexed by timezone-aware Brasília tick time, with
`bid`, `ask`, `last`, `volume`, and `flags` columns. Same-millisecond events remain
separate rows in stable order. It fetches all MT5 tick events by default; pass
`flags=` to select an MT5 tick category. Tick reads bypass the shared disk cache.

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
    "WIN$",
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
- `bars.attrs["q_research"]` contains source and query metadata.

You can save a fetched frame for later experiments using pandas:

```python
bars.to_parquet("win_m5.parquet")
```

The available history depends on the connected terminal and broker. A shorter result
is returned as supplied; missing candles are not filled in. No completed candles
raises `NoMarketDataError`, which you can import from `q_backend.research`.
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
bars = load_bars("WIN$", timeframe="M5", start="2026-09-01")

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

A strategy class provides exactly three hooks:

```python
from q_backend.research import ResearchStrategy, TradeOrder, indicators, backtest

class RSIReversion(ResearchStrategy):
    def __init__(self, period: int = 14):
        self.period = period

    def compute_indicators(self, frame):
        frame = frame.copy()
        frame["rsi"] = indicators.rsi(frame["close"], self.period)
        return frame

    def entry_strategy(self, frame):
        if len(frame) < 2:
            return None
        previous, current = frame["rsi"].iloc[-2:]
        if previous >= 30 and current < 30:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame):
        if frame["rsi"].iloc[-1] > 50:
            return TradeOrder.close()
        return None
```

### Strategy hooks and causality

1. **`compute_indicators(frame: DataFrame) -> DataFrame`**:
   - Called once on an owned copy of the entire historical frame.
   - Defaults to returning the input frame unchanged.
   - May add new indicator columns. Must NOT modify, drop, or reorder index or market columns (`open`, `high`, `low`, `close`), and must NOT introduce reserved columns (`q_signal_*`, `bar_index`).
   - Must be strictly **causal**: all calculations must only depend on rows $\le$ current row. Centered rolling windows, `shift(-1)`, or whole-frame statistics (global mean, min, max) leak future information and invalidate research results.
2. **`entry_strategy(frame: DataFrame) -> TradeOrder | None`**:
   - Abstract method called on an isolated owned copy of closed-bar history up to the current bar.
   - Returns `TradeOrder.buy()`, `TradeOrder.sell()`, or `None`.
3. **`exit_strategy(frame: DataFrame) -> TradeOrder | None`**:
   - Called on an isolated owned copy of closed-bar history up to the current bar, evaluated **before** `entry_strategy`.
   - Returns `TradeOrder.close()` or `None`. Defaults to returning `None`.

> [!IMPORTANT]
> **No fill or position state in hooks:** Decision hooks are pure functions of closed-bar price history up to the current bar. They receive no fill or position callbacks. Repeated signal conditions produce repeated order requests (the kernel risk model caps total exposure). Strategies must not attempt to track open positions in instance variables or assume prior orders were filled. Furthermore, per-prefix Python evaluation and frame copying is designed for research agility and is slower than built-in vectorized strategies.

### Running a backtest

```python
result = backtest(
    bars,
    strategy=RSIReversion(),
    symbol="WIN$",
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
| `costs` | `TransactionCostConfig` | `None` | Per-contract and basis-point transaction costs. `None` models zero transaction costs. |
| `exit_params` | `Mapping[str, Any]` | `None` | Optional stop-loss, take-profit, or trailing parameters (e.g. `stop_loss_pct`, `take_profit_atr`). |
| `day_trade` | `bool` | `False` | Enables intraday session filtering. |
| `day_trade_start_time` | `str` | `"09:00"` | Earliest entry time (`HH:MM`). |
| `day_trade_end_time` | `str` | `"16:00"` | Latest new entry time (`HH:MM`). |
| `day_trade_close_time` | `str` | `"17:00"` | Mandatory session close time (`HH:MM`). |
| `force_close_at_end` | `bool` | `False` | Whether to force-close any open position at the final bar of the dataset. |

### Backtest results (`BacktestResult`)

- **`metrics: dict`**: Summary performance statistics computed across closed trades (`total_trades`, `total_pnl`, `win_rate`, `profit_factor`, `max_drawdown_value`, `max_drawdown_pct`, etc.).
- **`trades: DataFrame`**: Execution log with stable columns (`trade_id`, `symbol`, `side`, `status`, `entry_time`, `entry_price`, `exit_time`, `exit_price`, `pnl`, `quantity`, `commission`, `point_value`, `exit_reason`).
- **`equity: DataFrame`**: Time series indexed by bar timestamp containing `realized_equity` (initial capital plus cumulative net PnL from closed trades). Open positions are not marked to market.
- **`data: DataFrame`**: Prepared historical bars augmented with user and exit indicator columns (internal signal arrays omitted).

## Example scripts

### Fetch live market data
```bash
uv run python examples/research/load_market_data.py \
  --symbol WIN$ --timeframe M5 --start 2026-09-01
```

### Offline indicator enrichment
```bash
uv run python examples/research/add_indicators.py \
  --input data/bars.parquet \
  --output data/enriched_bars.parquet
```

### Offline RSI reversion backtest
```bash
uv run python examples/research/rsi_reversion.py \
  --input data/bars.parquet \
  --symbol WIN$ \
  --period 14 \
  --quantity 1 \
  --point-value 0.20
```

### Live MT5 gateway backtest
```bash
uv run python examples/research/mt5_backtest.py \
  --symbol WIN$ \
  --timeframe M5 \
  --start 2026-09-01 \
  --strategy custom
```
