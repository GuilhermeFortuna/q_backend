# Q-013 implementation plan: Live market data publisher

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-013-live-market-data-publisher-spec.md`](../specs/Q-013-live-market-data-publisher-spec.md)  
**Depends on:** Q-011

## Current-system context

The Wine data gateway (`gateway/mt5_gateway.py`, 809 lines, stdlib plus
`MetaTrader5` and `numpy`) serves `/v1/health`, `/v1/symbol_info`,
`/v1/symbols/search`, `/v1/available_range`, `/v1/ohlcv`, and `/v1/ticks`.
The last two take naive Brasília ISO `start` and `end` values and return `npz`
archives, as captured in `q_contracts/schema/edge/data-gateway.yaml`. It runs as
the `mt5-gateway.service` user unit on `127.0.0.1:18812`. The Linux client is
`market_data/clients/remote.py::RemoteMt5Client`. It resolves its URL from
`Q_MT5_GATEWAY_URL` or the runtime config, checks schema major 1, and exposes:

- `get_ticks_columnar(symbol, start, end, flags, use_cache=True)`, which writes
  the on-disk tick cache through `store_tick_cache` unless `use_cache=False`, and
  returns `dict[str, np.ndarray]`
- `get_ohlcv(symbol, timeframe, start, end) -> list[OHLCV]`, which builds one
  `OHLCV` object per bar in `_npz_to_ohlcv` (line 331)
- `get_recent_ticks`, which maps rows to `Tick` objects

There is no columnar bars read, and nothing polls continuously.

`execution/quote_source.py::quote_source_from_market_data_service` reads ticks
by importing `MetaTrader5` on the Linux side, which is the pattern invariant 3
forbids and this task must not copy. Timezone helpers live in
`market_data/timezone.py` (`to_brasilia_naive`, `unix_seconds_to_brasilia_naive`).
`pyarrow>=24` is already a dependency, and Q-009 fixed the tick and bar Arrow
schemas (`time_msc: timestamp[ms]`, `time: timestamp[us]`, both naive Brasília).
After Q-011, `streaming/publisher.EphemeralPublisher` publishes Arrow bytes with
a routing key and a Redis-assigned sequence. The gap: no process turns the
gateway into stream entries.

## Interfaces produced

```python
# src/q_backend/market_data/clients/remote.py  (addition)
def get_ohlcv_columnar(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> dict[str, np.ndarray]: ...
    """Same /v1/ohlcv call as get_ohlcv; returns the npz arrays without row mapping."""
```

```python
# src/q_backend/streaming/market/arrow.py
def ticks_to_ipc(columns: Mapping[str, np.ndarray]) -> bytes: ...   # conforms to ticks Arrow schema
def bars_to_ipc(columns: Mapping[str, np.ndarray]) -> bytes: ...    # conforms to bars Arrow schema
TICKS_SCHEMA: pa.Schema   # built from the vendored contract, not written by hand
BARS_SCHEMA: pa.Schema
```

```python
# src/q_backend/streaming/market/cursor.py
@dataclass
class TickCursor:
    last_msc: int | None          # highest time_msc published
    seen_at_last_msc: int         # count of ticks already published at last_msc

def new_ticks(cursor: TickCursor, columns: Mapping[str, np.ndarray]) -> tuple[Mapping[str, np.ndarray], TickCursor]: ...
    """Vectorized: slice of columns not yet published, in order, and the advanced cursor."""

@dataclass
class BarState:
    forming_time: int | None                 # epoch seconds of the forming bar
    forming_values: tuple[float, ...] | None

def bar_transitions(state: BarState, columns: Mapping[str, np.ndarray]) -> tuple[Mapping | None, Mapping | None, BarState]: ...
    """(completed_bar_columns | None, forming_bar_columns_if_changed | None, new state)."""
```

```python
# src/q_backend/streaming/market/publisher.py
@dataclass(frozen=True)
class MarketPublisherConfig:
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]          # default ("M1",)
    tick_poll_interval_s: float = 0.25
    bar_poll_interval_s: float = 1.0
    tick_lookback_s: float = 5.0          # first poll and post-outage window
    max_backoff_s: float = 30.0

class MarketDataPublisher:
    def __init__(self, client: RemoteMt5Client, publishers: Mapping[str, EphemeralPublisher],
                 config: MarketPublisherConfig, clock) -> None: ...
    def poll_ticks_once(self) -> int: ...     # ticks published
    def poll_bars_once(self) -> int: ...      # bar entries published
    def run_forever(self, stop: threading.Event) -> None: ...

# src/q_backend/storage/settings.py  (additions)
stream_symbols: str = ""                   # comma-separated
stream_bar_timeframes: str = "M1"
stream_tick_poll_interval_s: float = 0.25
stream_bar_poll_interval_s: float = 1.0

# src/q_backend/cli/q_market_publisher.py   (project script "q-market-publisher")
def main(argv: list[str] | None = None) -> int: ...
```

```
tests/fixtures/market/win_ticks_session.npz   recorded ticks with repeated time_msc
tests/streaming/fake_gateway.py               stdlib HTTP server replaying npz by range
scripts/measure_quote_latency.py              human criterion 1
```

## Implementation decisions

- **Ticks come from polling `/v1/ticks` over a short trailing window, and
  duplicates are dropped with a `(last_msc, seen_at_last_msc)` cursor.** The
  gateway has no push and no latest-tick call. MT5's range query is inclusive,
  and `time_msc` is not unique (several trades can print in one millisecond), so
  "greater than the last timestamp" would drop real ticks and "greater than or
  equal" would duplicate them. Counting how many ticks at the boundary
  millisecond were already published is exact, as long as the gateway returns a
  millisecond's ticks in a stable order, which `copy_ticks_range` does. A test
  on the recorded fixture pins this.

- **Each poll requests `[last published tick time − 1 s, now]`, not
  `[last poll time, now]`.** Wall-clock poll times and tick timestamps come from
  different clocks, the host and the broker. Anchoring the window on the last
  published tick's own timestamp means clock skew can only cause re-fetching,
  which the cursor removes, never a gap.

- **Bars come from `/v1/ohlcv` over the last two bars, read as MT5 reports them.**
  The newest row is the forming bar. When its open time advances, the previous
  row is the completed bar with final values. Aggregating ticks into bars in
  Python would be a second implementation of bar aggregation, which architecture
  §3.3 assigns to `q_core` and invariant 1 forbids even temporarily. The cost is
  that forming bars update at the bar poll rate (1 s) instead of per tick, and
  the spec's non-goals accept that.

- **`get_ohlcv_columnar` is added next to `get_ohlcv`, reusing `_get_npz`, and
  `get_ohlcv` is left untouched.** The publisher calls the bar endpoint every
  second for every symbol and timeframe, and `_npz_to_ohlcv`'s per-row object
  loop is what architecture §5 ("If Python iterates over market data, that code
  belongs in Rust") and §4.6 rule out on a streaming path. Changing `get_ohlcv`
  itself would touch every REST market-data caller.

- **`new_ticks` and `bar_transitions` are vectorized with NumPy (`searchsorted`
  and boolean masks) and are pure functions.** The spec forbids per-row objects,
  and pure functions over column dictionaries can be tested exhaustively against
  the recorded fixture without a gateway, Redis, or a clock.

- **`use_cache=False` on every publisher tick read.** The tick cache is keyed
  by exact range. A rolling window produces a new key every 250 ms, so caching
  would write an unbounded number of files for data that is never read again.

- **Arrow schemas are built from the vendored contract's Arrow schema
  definitions, and a test asserts equality with the IPC output's schema.** Two
  schemas written by hand, one in the contract and one in this module, are the
  hand-written mirror invariant 2 forbids, and they would drift on the first
  column change.

- **One quotes entry per symbol per poll that found new ticks, carrying all of
  them as one IPC batch.** Architecture §4.6 calls for columnar batches, not
  per-row objects. At a 250 ms poll, a batch is the natural unit, and it lets the
  endpoint's per-symbol coalescing keep the newest batch rather than the newest
  single tick. The quotes topic's coalesce semantics ("keep the latest") then
  mean "keep the latest batch". Q-016 and the terminal read the last row as the
  current quote.

- **Default poll rates are 250 ms for ticks and 1 s for bars, both configurable,
  and the gateway's CPU under them is a human criterion.** 250 ms keeps
  tick-to-append p95 well under the one-second requirement, allowing for
  gateway response time. The gateway runs under Wine beside the terminal, and
  its cost there has never been measured under continuous polling, so the
  default is confirmed on the real machine rather than assumed.

- **After an outage, the cursor is reset and the first poll uses
  `tick_lookback_s = 5`.** The spec rules out backfilling the stream. Republishing
  minutes of ticks into an ephemeral topic would flood clients with data they
  would coalesce away, and REST already serves the past.

- **Outage logging is by state transition (healthy → unavailable → healthy),
  not per attempt.** At four polls a second per symbol, per-attempt logging
  would write about 14,000 lines an hour during a terminal outage.

- **`scripts/measure_quote_latency.py` computes latency as stream entry append
  time minus the tick's `time_msc`, converted from Brasília wall-clock to UTC.**
  That measures what a client experiences, including gateway and poll delay.
  Using the publisher's own receive time would hide the poll interval.

## Ordered implementation

1. Create the branch `Q-013-live-market-data-publisher-spec` in `q_backend` from
   `development`, after Q-011 is merged.
2. Record `tests/fixtures/market/win_ticks_session.npz`: at least 2,000 real ticks
   of a B3 futures symbol from the gateway, including at least one millisecond
   with three or more ticks. Write `tests/streaming/fake_gateway.py`, which serves
   `/v1/health`, `/v1/ticks`, and `/v1/ohlcv` by slicing fixture arrays by the
   requested range, and advances a controllable "now". Commit.
3. Write a failing test in `tests/market_data/test_remote_ohlcv_columnar.py`:
   against the fake gateway, `get_ohlcv_columnar` returns arrays whose `close`
   equals the fixture's, and `_npz_to_ohlcv` is never called (patched to raise).
   Confirm it fails, implement, confirm it passes. Commit.
4. Write failing tests in `tests/streaming/test_market_cursor.py` for `new_ticks`:
   feeding the fixture in overlapping windows of 300, 300, and 300 rows with 50
   rows of overlap each yields the full 900 rows exactly once and in order; a
   window that ends in the middle of a millisecond holding three ticks, followed
   by a window starting at that millisecond, yields the remaining ticks and no
   duplicates; an empty window returns an empty slice and the unchanged cursor.
   Confirm they fail, implement, confirm they pass. Commit.
5. Write failing tests for `bar_transitions`: the same forming values twice give
   no forming output the second time; a changed `close` gives forming output; a
   two-row input whose newest `time` is greater than `state.forming_time` gives
   the completed columns equal to the older row and the forming columns equal to
   the newer row. Confirm they fail, implement, confirm they pass. Commit.
6. Write failing tests in `tests/streaming/test_market_arrow.py`: `ticks_to_ipc`
   on a 3-row slice decodes with `pyarrow.ipc.open_stream` to a batch whose schema
   equals `TICKS_SCHEMA`, and `TICKS_SCHEMA` equals the schema built from the
   vendored contract; likewise for bars. Confirm they fail, implement, confirm
   they pass. Commit.
7. Write failing tests for `MarketDataPublisher` against the fake gateway and
   `fakeredis`: `poll_ticks_once` repeated across fixture time publishes entries
   whose decoded rows concatenate to the fixture, routing key
   `{"symbol": "WIN$N"}`; `poll_bars_once` across a bar roll publishes one
   `bars.completed` entry before the first new `bars.forming` entry, routing key
   `{"symbol", "timeframe"}`; `store_tick_cache` is never called (patched to
   raise). Confirm they fail, implement, confirm they pass. Commit.
8. Write failing tests for degraded behavior: with the fake gateway stopped,
   `run_forever` for 2 s publishes nothing, does not raise, and emits exactly one
   "gateway unavailable" log record; restarted with fixture "now" advanced by 60 s,
   the next publish contains only ticks from the 5-second lookback onward. Confirm
   they fail, implement, confirm they pass. Commit.
9. Write a failing test that `python -c "import q_backend.cli.q_market_publisher,
   sys; print('MetaTrader5' in sys.modules)"` prints `False`. Confirm it fails if
   the CLI imports through `api.dependencies` or `MarketDataService` (both reach
   the MetaTrader client). Build the client directly. Confirm it passes. Commit.
10. Add the settings and the `q-market-publisher` CLI, with `--symbols` and
    `--timeframes` overriding settings, SIGTERM handling, and a non-zero exit
    with the message "no symbols configured" for an empty set. Write the failing
    CLI test, implement, confirm it passes. Commit.
11. Write `scripts/measure_quote_latency.py`, which reads `q:stream:quotes` for
    the given symbol for N minutes, computes append-time minus tick time per row
    in UTC, prints p50, p95, and max, and compares the published tick count with
    a `/v1/ticks` range query for the same window. Commit.
12. Human step, matching human-verifiable criterion 1: during a B3 session, run
    the publisher and the measurement script for 10 minutes on one liquid
    futures symbol. Record the latency percentiles and both tick counts.
13. Human step, matching human-verifiable criterion 2: watch five M1 closes.
    Compare the `bars.forming` entry values just before each close, and the
    `bars.completed` entry values, with the MT5 chart.
14. Human step, matching human-verifiable criterion 3: observe the gateway
    process's CPU with `top` for two minutes under the default poll rates, with
    and without the publisher running. Record both.
15. Run the full validation suite. Commit.

## Validation

- **Unit:** tick cursor exactness across overlapping windows and same-millisecond
  boundaries; bar transitions; Arrow schema equality with the contract; columnar
  ohlcv without row mapping.
- **Integration:** publisher against the fake gateway and `fakeredis` reproduces
  the fixture exactly; completed-before-forming ordering; outage publishes
  nothing and logs one transition; no tick cache writes; no `MetaTrader5` import;
  empty symbol set refused.
- **Regression:** existing `tests/market_data` suite passes, and `get_ohlcv` and
  `get_ticks_columnar` behavior is unchanged.
- **Manual:** steps 13 and 14.
- **Measurement:** tick-to-append p50, p95, and max over 10 live minutes; published
  versus gateway tick count; gateway CPU with and without the publisher.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/streaming/test_market_cursor.py tests/streaming/test_market_arrow.py \
  tests/market_data/test_remote_ohlcv_columnar.py -v
python -c "import q_backend.cli.q_market_publisher, sys; print('MetaTrader5' in sys.modules)"

# live, during a B3 session
uv run q-market-publisher --symbols 'WIN$N' --timeframes M1 &
uv run python scripts/measure_quote_latency.py --symbol 'WIN$N' --minutes 10
redis-cli -p 6380 XREVRANGE q:stream:bars.forming + - COUNT 5
```

## Handoff

Report the tick cursor test's result on the recorded fixture: rows in, rows
published, and the largest same-millisecond group it crossed. Report the live
10-minute measurement: symbol, p50, p95, and maximum tick-to-append latency, the
published tick count against the gateway's count for the same window, and any
difference explained. Report the five bar-close comparisons against the MT5
chart, with any value that differed. Report gateway CPU with and without the
publisher at the default poll rates, and say plainly if the default must be
lowered. Report the entry rate per topic observed during the session, which Q-014
needs to size its per-client queues.
