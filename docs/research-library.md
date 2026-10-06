# Research market data library

`q_backend.research` fetches **fresh** historical OHLCV candles from MetaTrader 5 through
Q's existing HTTP gateway. It does not start the API, worker, PostgreSQL, Redis, or
desktop applications.

## Prerequisites

- MetaTrader 5 terminal running and connected to your broker.
- Q MT5 gateway running and reachable (same machine under Wine or remote).
- Gateway URL configured via `Q_MT5_GATEWAY_URL` and optional `Q_MT5_GATEWAY_TOKEN`
  (or the matching runtime JSON keys used elsewhere in `q_backend`).

## Installation

```bash
cd q_backend
uv sync
```

```python
from q_backend.research import load_bars

bars = load_bars("WIN$", timeframe="M5", start="2026-09-01")
```

With an explicit end:

```python
bars = load_bars(
    "WIN$",
    timeframe="M5",
    start="2026-09-01",
    end="2026-09-30T18:00:00-03:00",
)
```

## `load_bars` parameters

| Argument | Meaning |
|----------|---------|
| `symbol` | MT5 symbol (non-empty) |
| `timeframe` | Canonical name (`M1` … `MN1`), case-insensitive |
| `start` | Inclusive bar-open bound |
| `end` | Inclusive bar-open bound; **`None` means “now” captured once per call** |
| `gateway_url` / `gateway_token` | Override `Q_MT5_GATEWAY_URL` / `Q_MT5_GATEWAY_TOKEN` for this call only |

Each call issues a new history request through `RemoteMt5Client`. There is no catalog,
Parquet, or cache fallback. Gateway failures surface as errors from the client.

Bounds use `America/Sao_Paulo`. Naive datetimes and date-only strings (`YYYY-MM-DD`) are
interpreted in Brasília; a date-only **end** is midnight on that day, not the full
calendar day. Aware inputs convert to exchange time.

## DataFrame contract

- Index `time`: timezone-aware Brasília, sorted, unique.
- Columns: `open`, `high`, `low`, `close` (`float64`); `tick_volume` (`int64`);
  `spread`, `real_volume` (`float64`, may be NaN).
- Only **completed** candles are returned; the current forming bar is removed using the
  same captured “now” as an omitted `end`.
- Empty result: `NoMarketDataError` (names symbol, timeframe, range, MT5 source).
- `frame.attrs["q_research"]` is informative metadata (`source` is always `"mt5"`).

Offline work uses a pandas frame you already have; this module is for live MT5 history.

## Example script

```bash
uv run python examples/research/load_market_data.py \
  --symbol WIN$ --timeframe M5 --start 2026-09-01
```

Optional `--end`, `--gateway-url`, and `--gateway-token` forward to `load_bars`.
