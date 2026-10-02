# Q-080 implementation plan: Session trade ingestion and replay

> **For implementation agents:** Read the linked spec and repository instructions.
> Use superpowers:executing-plans when that skill is available. Start only through
> `./work start Q-080 --agent <agent> --worktree` after written-plan approval and
> completed dependencies. Implement this task natively; delegation requires separate authorization.

**Goal:** Serve the current session’s tape history and an ordered live trade stream without hidden gaps.
**Architecture:** Python coordinates source I/O, snapshot cache and stream delivery; q_core volume math remains in Q-081.
**Spec:** [Specification](../specs/Q-080-session-trade-ingestion-and-replay-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The linked spec defines the interface, defaults and acceptance criteria; do not widen scope.
- Use the existing repository toolchain and canonical checks without resource-slice wrappers.
- Commit focused changes on the task branch. Never push, merge or change protected branches.
- Report unavailable prerequisites with the documented board workflow; do not substitute shortcuts.

## Ordered implementation

- [x] 1. Add fake-source fixtures and tests/gateway/test_mt5_gateway.py cases for /v1/trades UTC ranges, trade eligibility, volume context and same-millisecond multiplicity. Add remote-client decoding assertions.
- [x] 2. Vendor Q-079 and implement the additive gateway and remote trade path. Register the generated trades.status control model in publisher validation and serialization.
- [x] 3. Build TradeSessionCoordinator with bounded chunk fetching, complete-group occurrence assignment, serialized overlap validation and failed-publish cursor retention. Add tests/streaming/test_trade_ingestion.py for boundary/retry/correction cases.
- [x] 4. Implement the immutable cache and Redis request/result service in trade_history.py. Add tests/streaming/test_trade_snapshot.py for generation, frozen watermark, resource limits, expiry and concurrent readers.
- [x] 5. Add generated API schemas/routes and publisher CLI wiring. Exercise subscribe → buffered live → paged history → watermark discard → replay with tests/api/test_trade_history.py and tests/streaming/test_trade_join.py.
- [x] 6. Document the session-day policy, cache settings, source completeness and restart runbook. Run focused suites, make contracts-check and ./scripts/ci.sh; commit and hand off contract pin plus scripted sequence evidence.

## Review focus

- Quote-only updates do not re-count the previous trade; flag-filter fixtures prove this.
- One millisecond can contain several identical trades; source-order fixtures survive chunk boundaries and retries.
- Publisher/API ownership is cross-process; request-service tests use independent clients, not a shared in-process singleton.
- A missing/truncated range cannot produce complete coverage; injected-source tests verify every public response.
- Active snapshots remain immutable under eviction and live append; cache tests compare page bytes and watermark.

## Validation and handoff

Run focused gateway, remote-client, trade-ingestion, snapshot, join and API suites with fake sources; then `make contracts-check` and `./scripts/ci.sh` using its disposable services. Full CI is justified by the new cross-process API/publisher path; GPU/Wine/live orders are unnecessary. A read-only B3 walkthrough is supplementary source evidence, not the deterministic test gate.

Record acceptance results, exact dependency pins, any manual evidence and open follow-ups.
Commit the final changes, then run `./work board set Q-080 in-review -m "<changes; checks and results; follow-ups>"`
from the workspace root. The human owns integration and any required release.
