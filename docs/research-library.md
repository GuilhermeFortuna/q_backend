# Using the research library

The public interface lives in `q_backend.research`. This guide covers market-data
import and indicator helpers.

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

Bounds use `America/Sao_Paulo`. Naive datetimes and date-only strings (`YYYY-MM-DD`) are
interpreted in Brasília; a date-only **end** is midnight on that day, not the full
calendar day. Aware inputs convert to exchange time.

## Returned data

- Index `time`: timezone-aware Brasília, sorted, unique.
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

## Example scripts

### Fetch live market data
```bash
uv run python examples/research/load_market_data.py \
  --symbol WIN$ --timeframe M5 --start 2026-09-01
```

Optional `--end`, `--gateway-url`, and `--gateway-token` forward to `load_bars`.

### Offline indicator enrichment
```bash
uv run python examples/research/add_indicators.py \
  --input data/bars.parquet \
  --output data/enriched_bars.parquet
```
