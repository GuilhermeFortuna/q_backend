# Research market data library

The `q_backend.research` package loads historical OHLCV bars into ordinary pandas
DataFrames for notebooks and batch scripts. It does **not** start the API, worker,
Redis, or desktop applications.

## Installation

Use the same `uv` environment as the rest of `q_backend`:

```bash
cd q_backend
uv sync
```

Import from the installed package (no `sys.path` edits):

```python
from q_backend.research import Research
```

## Configuration

Construct `Research` with explicit instance settings. Constructor arguments override
environment and runtime JSON for the fields they set.

| Parameter | Role |
|-----------|------|
| `source` | `"local"` (catalog + parquet), `"remote"` (MT5 gateway), or `"auto"` |
| `database_url` | PostgreSQL URL for the lake catalog (`Q_DATABASE_URL` when omitted) |
| `market_data_root` | Root directory containing catalog-addressed parquet (`Q_MARKET_DATA_ROOT`) |
| `gateway_url` / `gateway_token` | Remote MT5 gateway (`Q_MT5_GATEWAY_URL`, `Q_MT5_GATEWAY_TOKEN`) |

### Local reads

Requires a reachable PostgreSQL catalog and the immutable parquet files it references.
Reads use `lake_query.read_bars_table` on catalog-resolved absolute paths only (no
managed-lake globbing or legacy `catalog.json`).

### Remote reads

Uses `RemoteMt5Client` with the resolved gateway URL/token. No catalog writes, tick
cache writes, or native MT5 terminal.

### Auto selection

Resolves the local catalog first. When a published dataset envelope fully contains the
requested range, data is served locally; otherwise the **entire** requested range is
fetched from the gateway. If local coverage is insufficient and no gateway is
configured, `bars()` raises an actionable error (no silent truncation).

## `bars()` contract

```python
research = Research(
    source="local",
    database_url="postgresql+psycopg://q:q@localhost:5434/q",
    market_data_root="data/market",
)
frame = research.bars(
    "WIN$",
    timeframe="M5",
    start="2026-09-01",
    end="2026-09-30T18:00:00-03:00",
)
```

- **Timeframe:** canonical MT5 names (`M1` … `MN1`), case-insensitive.
- **Bounds:** inclusive bar-open timestamps in `America/Sao_Paulo`. A date-only bound
  (`YYYY-MM-DD`) means midnight in Brasília, not the full calendar day.
- **Index:** `time`, timezone-aware Brasília, sorted ascending, unique.
- **Columns:** `open`, `high`, `low`, `close` (`float64`); `tick_volume` (`int64`);
  `spread`, `real_volume` (`float64`, optional fields may be NaN).
- **Forming bar:** the last row still open at the current exchange-local time is
  removed before return.
- **Empty result:** `NoMarketDataError` (subclass of `ValueError`).
- **Metadata:** `frame.attrs["q_research"]` records symbol, timeframe, source, bounds,
  and `dataset_id` when served locally (informative only; do not rely on pandas
  preserving attrs).

`inventory()` lists published **bar** datasets from the local catalog (independent of
`source`): `symbol`, `timeframe`, `start`, `end`, `rows`, `bytes`, `dataset_id`.

Call `close()` or use a context manager to release owned database engines. Shared
injected resources are not disposed.

## Example script

`examples/research/load_market_data.py` accepts CLI flags and the environment variables
above. Run from the repository root:

```bash
uv run python examples/research/load_market_data.py --help
```
