# Market replay and simulated broker (Q-145)

The replay simulator runs a historical market session and a simulated **demo** broker as an external
process. A supported strategy can be deployed through the normal live stack against it. Replay controls
live only in `q-replay`; they are not in `q_terminal`, the live control API or any gateway or edge.

## Components

| Listener | Default | Interface |
| --- | --- | --- |
| Market source | `127.0.0.1:18912` | Existing gateway `/v1/` contract (health, symbols, ohlcv, ticks, pages, trades) and `GET /v1/clock` |
| Broker edge | `127.0.0.1:18913` | Execution edge 1.2 contract (submit, lookup, orders, positions, deals, account, quote, check) |
| Administrative | `127.0.0.1:18914` | `/replay/v1/runs` (create, status, commands, journal); optional `X-Replay-Token` |

All three bind loopback only. The simulator has no broker credentials, never imports `MetaTrader5`, and
never falls back to a real gateway or broker.

## Preparing a run

1. Freeze the inputs. Each symbol has one UTC tick NPZ with `time_msc, bid, ask, last, volume, flags`
   (the gateway's columnar tick layout). The manifest lists every file with its SHA-256 and size, the
   symbols it covers, the UTC coverage interval, the exchange timezone and per-symbol specs (point,
   digits, volume min/step/max, contract size, margin per contract, commission per contract, slippage
   ticks). Source ticks must be time-ordered; a file that is not must be sorted and re-frozen.
2. Write the run config (`run-config` contract): manifest path, symbols, UTC `[start, end)`, optional
   warmup start, exact initial balance, demo account login and currency, latency (integer market-time
   milliseconds, default 0), initial rate (0.1 to 10), and resource limits.
3. Verify offline: `q-replay prepare --config run.json --root <dataset-dir>`. It checks checksums,
   symbols, coverage and warmup, and prints the config hash and dataset checksums.

## Running

- `./dev replay --config run.json --root <dataset-dir>` verifies the run, starts the isolated
  `q-replay.service` on fresh run-scoped state and creates the run **paused**.
- Resume after deployment setup: `q-replay resume`. Speed, step, pause and stop use the same CLI.
- `./dev down replay` stops only the simulator. `./dev down live` and `./dev down research` leave it
  running. `./dev down all` includes it.
- `./dev status`, `./dev logs replay`, `./dev restart replay:simulator` operate on the same unit.
- A running simulator refuses a new run. Stop it, then start again: a new run never reuses cache,
  ledger or trade state from an earlier generation.

## Time model

- **Market time** is canonical UTC. It advances from a monotonic anchor at the run rate and freezes while
  paused. Historical timestamps keep their historical values.
- **Operational time** (leases, heartbeats, health, HTTP deadlines, retries) stays on wall time. A replay
  clock never changes it.
- The live stack reads market time through `Q_MARKET_CLOCK_URL` and `Q_MARKET_CLOCK_GENERATION`. The market
  data cache namespace (`Q_MARKET_DATA_CACHE_NAMESPACE`) must equal the generation. An unreachable,
  stale or other-generation clock fails market actions closed.

## Latency and the moving market

For a request ingressed at market time `t0`:

- broker arrival = `t0 + order_transport_ms`
- acceptance = arrival + `broker_accept_ms`
- a market order executes at acceptance + `broker_execution_ms`, at the quote then present
- the outcome becomes visible at outcome time + `broker_response_ms`

The market keeps advancing during every stage. A submit waiting for its visibility instant does not block
the market, quotes, bars or other requests. The HTTP response is delivered only at its visibility instant.

Example: ask 100 at ingress, 101 at +100 ms, 103 at +200 ms. With transport 50, acceptance 50, execution 150
and response 100, the buy fills at 103 at +250 ms and is visible at +350 ms.

## Order semantics

- Market, stop and limit entries; attached protections (stop loss and take profit) activate atomically
  with their fill.
- A triggered stop is committed as a market order even if the quote retreats before execution. A limit
  re-checks its bound at execution and keeps working if it is no longer executable.
- Expiry is exclusive: an expiry at the same instant as a trigger wins.
- Cancel and amend take effect after their transport and acceptance delay. A triggered stop cannot be
  cancelled. A triggered limit can, until it fills.
- Netting: reductions and reversals produce correct deals, realized profit and cost basis.
- Commissions use the `q_core` fee function; each deal carries its signed charge as `commission`. The edge
  broker maps `fee = -(commission + fee)`, with missing fields counted as zero and rebates not clamped.
- Margin: the initial balance and a fixed margin per contract. Insufficient funds are refused at acceptance.
- A quote older than 60 s of market time cannot price a fill.
- Duplicate intent ids are refused with `duplicate_intent` and never resubmitted. Restarts recover
  paused and reconcile durable fills to one fill.

## End of data and stop

At `end` (exclusive) nothing executes, including an execution scheduled exactly at `end`. Untriggered
pending orders are cancelled. Accepted but unexecuted requests are rejected with `end_of_data`. Fills and
open positions stay inspectable. No flatten price is invented. Stop uses the same cut-off at the current
cursor.

## Reproducibility and model limits

- Identical frozen inputs, configuration and recorded ingress produce identical broker outcomes
  (`tests/replay/test_reproduction.py`). Wall-clock timestamps and envelope ids are excluded.
- Two independent interactive runs need not match: real scheduling changes ingress, especially under
  acceleration.
- Not modeled: partial fills, order book depth, queue position, stochastic jitter, exchange margin or
  stop-out, and an undocumented broker's every rule. Client-managed OCO has no atomic broker guarantee.
- Support for a single account and per-symbol netting; a second active deployment for the same account and
  symbol is refused in this profile.
- Source preparation from an existing research TickStore is not implemented; manifests are prepared from
  NPZ files. The tick cache for the remote gateway is keyed by the generation namespace.

## Reproducible checks

```bash
cd q_backend
uv run pytest tests/replay -q -m 'not integration'          # simulator, gateway, broker, edge, admin, reproduction
uv run pytest tests/execution/test_replay_edge_stack.py -q   # real EdgeBroker through the replay edge
uv run pytest tests/execution/test_clock_domains.py -q       # clock domains and remote market clock
uv run pytest tests/api/test_execution_demo_account.py -q    # demo capability and live gates
make contracts-check CONTRACTS_REPO=<q_contracts checkout>
cd .. && tools/tests/test-replay                              # launcher lifecycle with fake services
```

## Not verified here

The end-to-end terminal inspection (deploy a fixture strategy while paused, resume, observe orders and
the delayed fill, pause and step, stop replay while live and research keep running) needs a person at the
terminal and a running replay live stack.
