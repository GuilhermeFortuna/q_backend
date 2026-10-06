# Using the research library

The public interface lives in `q_backend.research`. This guide covers market-data
import, the functionality currently available on `development`.

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

## Included command-line example

```bash
uv run python examples/research/load_market_data.py \
  --symbol WIN$ --timeframe M5 --start 2026-09-01
```

Optional `--end`, `--gateway-url`, and `--gateway-token` forward to `load_bars`.
