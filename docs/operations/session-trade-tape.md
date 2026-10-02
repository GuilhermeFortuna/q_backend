# Session Trade Tape

`q-market-publisher` ingests the current exchange session's trades and serves them two ways:
an ordered live stream (`trades`, with coverage on `trades.status`) and an immutable, paged
history of everything before it. The two join without gaps or duplicates. Contracts are
owned by `q_contracts` (`schema/stream/README.md`, "Trade tape and session history").

## Session policy

The session is the **B3 exchange-local calendar day**: `America/Sao_Paulo` midnight to the next
midnight, converted to UTC. It is a day key, not a holiday or trading-hours calendar. The
publisher starts a new source generation when the day rolls over.

## How it works

* **Backfill.** For each symbol in `Q_STREAM_SYMBOLS` the publisher fetches the day from
  midnight to a frozen UTC boundary through the gateway's `GET /v1/trades`, in one-minute
  chunks. A chunk with more than 50,000 eligible rows is split on a millisecond edge; a single
  millisecond group is always fetched whole. If the source cannot return a group whole, the
  range is reported incomplete rather than truncated. At most `Q_TRADE_CACHE_MAX_BACKFILLS`
  (default 2) symbols backfill at once; the rest wait.
* **Identity.** A row is `(provider_id, symbol, source_generation, time_msc, occurrence)`.
  Occurrence counts eligible trades within a millisecond in provider order, so identical prints
  stay distinct. The newest millisecond group is held until a later row, or the clock passing it
  by `Q_TRADE_GROUP_SETTLE_MS` (default 2000), confirms it is complete.
* **Live polling.** Each poll re-reads the last published millisecond group and validates it.
  Missing occurrences are appended. If that group shrank, reordered or changed, the provider
  corrected its prefix: coverage turns `partial`, a `trades.status` message says so, and the
  session is recaptured under a new source generation.
* **Volume.** One raw field is selected per generation: positive finite `volume_real` when the
  terminal provides it, otherwise `volume`. Both raw columns are delivered; the field and unit
  travel in the delivery context. A field or unit change starts a new generation and a
  rebuild, never a conversion.
* **Failed publishes** never advance the cursor. The same rows are retried with the same
  identities, so a Redis outage causes neither gaps nor duplicates.

## Snapshot and join protocol

1. Subscribe to `trades` and buffer deliveries.
2. `GET /api/v1/market/trades/snapshot?symbol=` returns an opaque token, session bounds, the
   source context, coverage and the frozen `(epoch, seq)` watermark. While a backfill runs it
   answers `202` with `Retry-After: 1` plus `X-Q-Trade-Backfill-Rows` and
   `X-Q-Trade-Backfill-Covered-To` progress headers; it never reports an empty complete snapshot.
3. `GET /api/v1/market/trades/history?snapshot_id=&cursor=&limit=` pages the Arrow history.
4. Discard buffered deliveries at or below the watermark and apply the rest once, in order.

`410` means the token expired (ten minutes), the source generation was replaced, or the
publisher restarted: request a new snapshot. `503` means the publisher did not answer, the
trade source is unavailable, or the cache has no room (`trade_cache_resource_limit`).

The API holds no ingestion state. It sends a request over Redis (`q:trades:requests`) and the
publisher answers with a descriptor and writes the snapshot into the shared cache directory.
Pages are read straight from those immutable files after the API validates the token against
the publisher instance and generation recorded in Redis.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `Q_TRADE_CACHE_DIR` | `<Q_MARKET_DATA_ROOT>/trade_session_cache` | Snapshot cache shared by API and publisher. Must be the same path for both. |
| `Q_TRADE_CACHE_MAX_BYTES` | 1 GiB | Cache ceiling. Expired tokens are evicted oldest first; active tokens are never evicted or truncated. |
| `Q_TRADE_CACHE_MAX_BACKFILLS` | 2 | Concurrent symbol backfills. |
| `Q_TRADE_SNAPSHOT_TTL_S` | 600 | Snapshot token lifetime. |
| `Q_TRADE_REQUEST_TIMEOUT_S` | 5 | How long the API waits for the publisher. |
| `Q_TRADE_GROUP_SETTLE_MS` | 2000 | Clock margin after which the newest group counts as complete. |
| `Q_TRADE_POLL_INTERVAL_S` | 0.25 | Live poll period per symbol. |

## Source completeness

`coverage_state` is `complete` only when the provider served the whole range without truncation,
invalid records or a detected gap. It does not claim the exchange feed had no omissions. It is
`partial` for invalid trade records (counted in `invalid_trade_count`), truncation, a gateway
error or an overlap correction, and `unavailable` when the provider does not know the symbol.
After a gateway error the publisher keeps serving what it has as `partial` and recaptures the
session once the gateway answers again.

## Restart runbook

* **Publisher restart** (`systemctl --user restart q-market-publisher.service`): it clears its
  snapshot tokens, rebuilds the session from the provider and publishes under a new generation.
  Nothing is recovered from Redis; the `trades` stream may still hold older entries, which
  consumers discard when the generation or epoch changes.
* **API restart:** tokens issued by the running publisher keep working. Tokens whose publisher
  is gone are expired on startup.
* **Gateway:** `/v1/trades` is new. Redeploy `gateway/mt5_gateway.py` to the Wine prefix or
  Windows host and restart `mt5-gateway.service` (see `docs/mt5-wine-gateway.md`), otherwise
  backfill reports the source unavailable.
* **Stuck at 202:** `journalctl --user -u q-market-publisher.service` logs each generation and
  backfill completion. Diagnostics (backfill rows and chunks, invalid records, overlap
  mismatches, retries, source gaps, snapshot expiry and resource-limit counts) are in the Redis
  hashes `q:trades:diagnostics` and `q:trades:diagnostics:<symbol>`.
* **Disk pressure:** removing the cache directory is safe while the publisher is stopped.
