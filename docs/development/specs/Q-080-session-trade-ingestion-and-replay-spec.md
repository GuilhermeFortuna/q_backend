# Q-080: Session trade ingestion and replay

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 13 — persistent terminal setup and live market analysis
**Depends on:** Q-079
**Implementation plan:** [Plan](../plans/Q-080-session-trade-ingestion-and-replay-plan.md)

## Purpose

Serve the current session’s tape history and an ordered live trade stream without hidden gaps.

## Current system

The market publisher polls raw ticks and publishes quotes using TickCursor. Gateway and RemoteMt5Client forward legacy ticks; the WebSocket queue already supports non-coalescing lag behavior. The terminal needs a separate history/live join rather than a quotes latest snapshot.

## Required behavior

- Vendor the merged Q-079 contracts. Add the read-only gateway /v1/trades endpoint and RemoteMt5Client method; the gateway stays stdlib + numpy + MetaTrader5 and never imports q_backend. Use timezone-aware UTC range inputs for the new endpoint only.
- Select one volume field per provider/symbol generation: positive finite volume_real when provided and usable for that source, otherwise the documented positive raw volume field. Carry the chosen field and source unit in context; never mix fields mid-generation. A unit/field change causes a new generation and rebuild, not a silent conversion.
- Capture the current B3 exchange-local calendar day (America/Sao_Paulo midnight to next midnight), matching the existing study session policy. This is a day key, not a holiday/open-hours calendar. Fetch from day start to a frozen UTC boundary using one-minute chunks, bounded to 50000 eligible rows per page; split dense ranges without dropping same-millisecond records. A single millisecond group is retrieved whole before paging it; if the source/resource limit cannot hold the group, report an incomplete range rather than truncating it.
- Serialize backfill and polling in one per-symbol TradeSessionCoordinator. Assign occurrence within complete millisecond groups in provider order. Hold the current millisecond group until a later observation confirms its end. Validate the complete overlap boundary group on each poll; append missing occurrences, retaining equal-looking prints.
- If overlap shrinks, reorders or changes, mark coverage partial and replace the source generation through a full session recapture. Gateway errors/truncation/invalid records mark the range incomplete. A failed publish never advances the ingestion cursor; retries reuse record identities.
- Prepare immutable paged snapshot files in a bounded temporary session cache, with shared references to frozen context/watermark. Default cache ceiling 1 GiB and two concurrent backfills, oldest expired tokens evicted first. If active tokens cannot fit, return explicit unavailable/resource-limit status; never truncate and call it complete.
- Snapshot/live coordination is exposed by q-market-publisher: API requests go through a Redis request/result channel and immutable shared cache files, not a separate API-side poller. Use opaque generated ids, config-root-contained paths and generation validation. The API never reconstructs a watermark independently of the publisher.
- Subscribe/buffer uses Q-079 snapshot/watermark semantics. Publish trades batches on the non-coalescing topic; route recovery through existing sequence history while retained, then full snapshot on expiry/epoch/generation change. Backfill progress/coverage is exposed by the snapshot/status endpoint.
- Source errors after a snapshot must reach consumers: publish the contracted trade-source status control message on trades.status; it advances that topic’s own sequence and marks coverage partial until recapture. A metadata mismatch pauses aggregates until a valid snapshot replaces them.
- API startup/publisher restart expires old cache tokens. Rebuild from the provider, never assume Redis latest is session history. No new Postgres per-trade ledger/outbox rows, filesystem discovery or changes to execution services.
- Expose diagnostic counts/timings for backfill rows, invalid records, overlap mismatches, retries, snapshot expiry, sequence replay and source gaps. Existing health reports publisher failure; do not introduce a monitoring service.

## Interfaces and ownership

Python coordinates source I/O, snapshot cache and stream delivery; q_core volume math remains in Q-081.
Create streaming/market/trades.py (TradeSessionCoordinator), streaming/market/trade_history.py (immutable cache/request service), api/routers/trades.py and API schemas generated from Q-079. Extend gateway/mt5_gateway.py, market_data/clients/remote.py, market publisher CLI wiring and stream status handling. Contracts paths, endpoint names and metadata are Q-079’s.

## Acceptance criteria

1. Fake gateway feeds a full-session backfill followed by live records: paged history plus buffered stream exactly matches one ordered source sequence, including equal records at the same millisecond and the cut group.
2. Poll overlap, publish failure, duplicate replay and restart create neither missing nor doubled accepted trades; a source correction changes generation and forces recapture.
3. Full/partial/empty successful ranges, invalid records, truncation, source outage, 202 progress, token expiry and cache-resource limits return honest coverage/status.
4. Native gateway and remote paths preserve UTC, volume field/unit and flag eligibility. Existing /v1/ticks behavior remains unchanged.
5. Snapshot ownership works across API and publisher processes; concurrent requests share backfill and never observe mixed pages/watermarks. Replay overflow/expiry emits a recovery path, not coalesced trades.
6. Focused gateway/market/stream/API tests, make contracts-check and the isolated backend CI pass.

## Implementation boundary

This issue authorizes only its listed deliverable after written-plan approval and
`./work start Q-080 --agent <agent> --worktree`. Dependencies must be Done.
Preserve the existing execution controls and research/operations ownership boundaries.
No live-order activation, new backend-process ownership or unrelated refactoring.
Use generated contracts and commit/tag pins; never edit vendored code by hand.
