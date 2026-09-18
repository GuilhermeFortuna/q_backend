# Q-042 implementation plan: Execution worker on the edge

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-042-execution-worker-on-the-edge-spec.md`](../specs/Q-042-execution-worker-on-the-edge-spec.md)  
**Depends on:** Q-040, Q-041

## Current-system context

`cli/q_execution.py::_build_components` builds one `PaperBroker` with
`quote_source_from_market_data_service(market_data)` and hands the same broker
to `ExecutionService`, `ExecutionWorker` and, through
`ExecutionWorker._order_reconciler`, to `OrderReconciler`. `quote_source.py`
reads ticks by `import MetaTrader5 as mt5` after `client._ensure_connected()`.
On Linux the market-data client is `RemoteMt5Client`, so that import reaches
at best the stub. `ExecutionRecovery.recover` fails closed when a running
deployment has no quote.

`brokers/metatrader.py::MetaTraderBroker` drives `brokers/mt5_runtime.py::DefaultMt5Runtime`,
which imports `MetaTrader5`. It is exported from `brokers/__init__.py` and
tested in `tests/execution/test_metatrader_broker.py` with
`fake_mt5_runtime.py`, but **no production code constructs it**: live
execution has never been wired into the worker. Its gate logic is
`brokers/live_gates.py::evaluate_live_gates`, fed by `Settings.live_execution_*`.
`BrokerSubmissionOutcome` is `filled | rejected | unknown`, and
`BrokerOrderLookupStatus` is `filled | rejected | not_found | unavailable`.
`MarketOrderRequest` carries no broker mode and no intent creation time.
`reconciliation.lookup_request_for_order` builds one from the order row.

`service.process_completed_bar` commits the intent, calls
`self._broker.submit_market_order`, and maps `UNKNOWN` to
`ExecutionOrderStatus.UNKNOWN` with `ReconciliationState.PENDING`. The
`CrashInjector` checkpoints are `before_intent_commit`, `after_intent_commit`,
`after_broker_response` and `before_fill_commit`, and are settable only in
tests. `api/schemas/execution.py::DeploymentCreateRequest.broker_mode` is
`Literal["paper"]`.

After Q-040 the edge runs at `127.0.0.1:18813`, with the contract JSON vendored
under `contracts/schema/edge` and dataclasses in `contracts/edge.py`. After
Q-041 the worker processes bars one at a time and syncs the open trade after
every fill.

## Interfaces produced

```python
# src/q_backend/execution/edge_client.py   (new)
class EdgeUnavailable(Exception): ...
class EdgeClient:
    def __init__(self, base_url: str, *, timeouts: EdgeTimeouts, transport: httpx.BaseTransport | None = None): ...
    def health(self) -> EdgeHealthResponse: ...                 # raises EdgeUnavailable; checks schema major
    def account(self) -> AccountResponse: ...
    def quote(self, symbol: str) -> QuoteResponse: ...
    def check(self, intent_id: UUID, order: ExecutionOrder) -> CheckResponse: ...
    def submit(self, intent_id: UUID, order: ExecutionOrder) -> SubmitOutcome: ...   # never raises for transport:
                                                                                   # timeout/transport → indeterminate
    def lookup(self, intent_id: UUID, window_start: datetime, window_end: datetime) -> LookupOutcome: ...
@dataclass(frozen=True)
class EdgeTimeouts: connect_s: float = 1.0; read_s: float = 5.0; submit_read_s: float = 15.0

# src/q_backend/execution/quote_source.py   (rewritten)
class EdgeQuoteSource:                      # QuoteSource; timestamp = now - age_ms; None on any EdgeUnavailable
    def get_quote(self, symbol: str) -> Optional[ExecutableQuote]: ...
# removed: quote_source_from_market_data_service

# src/q_backend/execution/brokers/base.py   (changed)
class MarketOrderRequest: + broker_mode: BrokerMode; + intent_created_at: datetime
# src/q_backend/execution/brokers/edge.py   (new; replaces metatrader.py)
class EdgeBroker:                          # ExecutionBroker for mt5_live
    def health(self) -> BrokerHealth: ...
    def submit_market_order(self, request, *, cost_config) -> BrokerSubmissionResult: ...
    def lookup_order(self, request) -> BrokerOrderState: ...
# src/q_backend/execution/brokers/routing.py   (new)
class BrokerRouter:                        # ExecutionBroker; dispatches on request.broker_mode
    def __init__(self, *, paper: PaperBroker, mt5_live: EdgeBroker): ...
# deleted: brokers/metatrader.py, brokers/mt5_runtime.py, tests/execution/fake_mt5_runtime.py
# kept: brokers/live_gates.py, brokers/mt5_constants.py (retcode labels for messages)

# src/q_backend/storage/settings.py   (changed)
mt5_edge_url: str = "http://127.0.0.1:18813"
mt5_edge_connect_timeout_s / mt5_edge_read_timeout_s / mt5_edge_submit_timeout_s
execution_lookup_window_lead_s: float = 60.0

# src/q_backend/cli/q_execution.py   (changed)
run --crash-at CHECKPOINT                  # validation only; logs a warning banner when set

# src/q_backend/api/schemas/execution.py   (changed)
DeploymentCreateRequest.broker_mode: Literal["paper", "mt5_live"]
```

```
tests/execution/fake_edge.py                   new: in-process HTTP fake of the edge contract (scripted outcomes, request log)
tests/execution/test_edge_client.py            new
tests/execution/test_edge_broker.py            new: criteria 1, 4, 6
tests/execution/test_worker_on_edge.py         new: criteria 2, 3, 5
tests/execution/test_no_mt5_import.py          new: criterion 8
tests/execution/test_metatrader_broker.py      deleted; behaviours moved to test_edge_broker.py or listed as edge-owned
q_contracts: schema/api/openapi.yaml           recaptured on branch Q-042-execution-worker-on-the-edge
q_contracts: COMPAT.md                         q_backend row
```

## Implementation decisions

- **`httpx` with explicit timeouts, and `submit` returns a value, never raises.**
  §6.2 makes a timeout on submit indistinguishable from an indeterminate
  outcome. Encoding that inside the client means no caller can forget it. The
  other operations raise `EdgeUnavailable`, which callers map to "no quote",
  "unavailable" or a broker-unavailable rejection. That is fail-closed by
  construction.

- **Acceptance is confirmed by one lookup, inline.** The contract's `accepted`
  carries a ticket, not a deal. `EdgeBroker.submit_market_order` performs one
  lookup immediately. `filled` becomes `FILLED` with the deal-derived fill, and
  anything else becomes `UNKNOWN`, left to `OrderReconciler` on the next poll.
  That is a lookup, not a resubmit, so §6.2 holds. It saves a poll on the common
  path, where the deal is already in history.

- **`duplicate_intent` maps to `UNKNOWN` and is logged at error level.** The
  worker never resubmits, so this answer means an invariant was broken
  somewhere. The order must not be treated as rejected, because the first
  attempt may have filled.

- **The lookup window starts `execution_lookup_window_lead_s` before
  `intent_created_at`.** The intent's row time is the earliest the order can
  exist. The lead covers clock skew between Linux and the Wine clock. The window
  ends at the worker's now, plus the same lead.

- **One router, one broker per mode.** `ExecutionService`, `ExecutionWorker` and
  `OrderReconciler` each take one `ExecutionBroker`. A `BrokerRouter` that
  dispatches on `request.broker_mode` keeps all three unchanged, apart from
  filling the two new request fields. `reconciliation.lookup_request_for_order`
  already reads `order.broker_mode`.

- **Quotes: one `EdgeQuoteSource` for everything.** The paper broker keeps its
  fill model and its own `validate_quote`, priced from the edge. The quote's
  `timestamp` is set to `now - age_ms`, so the existing freshness checks
  (`validate_quote`, the risk gate) keep their code and gain the edge's age
  semantics. Criterion 6 proves the terminal's own clock is no longer consulted.

- **The live gates move into `EdgeBroker` unchanged**, fed by `account().login`.
  `trade_allowed` false, from either the account or the terminal, gives
  `TRADING_DISABLED`, as `_preflight` does today. Symbol visibility, volume
  normalization, the filling mode and the quote check move to the edge (Q-040),
  and the edge's `rejected` reason is recorded verbatim.

- **Deleting `MetaTraderBroker` and its tests is safe because it has no
  production caller.** Each test in `test_metatrader_broker.py` either
  moves to `test_edge_broker.py` (gates, outcome mapping, fill aggregation from
  deals) or is listed in the handoff as covered by Q-040's edge tests (filling
  mode, normalization, preflight).

- **`--crash-at` is a CLI flag, not an environment variable,** so it cannot be
  left set in a unit file by accident. It prints a warning banner, and is
  refused together with `--log-level` below `INFO`, so that its use is always
  visible in the log.

- **The hygiene test walks `src/q_backend/execution`** and fails on any
  `import MetaTrader5` or `from MetaTrader5`. The research side's native client
  is recorded in FINDINGS against invariant 3, with the condition for removing
  it: the data gateway covering every call it makes.

- **The OpenAPI recapture follows the Q-015 pattern:** a commit on a branch of the
  same name in `q_contracts`, captured from this branch's running API with
  `tools/capture_api.py`. If another batch-07 recapture has merged first, rebase
  onto it and capture again.

## Ordered implementation

- [x] 1. Work on the branch `Q-042-execution-worker-on-the-edge` in `q_backend`,
   created from `development` by `./work start`. Confirm Q-040 and Q-041 are
   merged. Commit nothing.
- [x] 2. Write `tests/execution/fake_edge.py`: a threaded HTTP server serving the
   contract routes from scripted responses, recording every request, and able
   to delay or drop the connection. Validate its responses against the vendored
   schemas in its own test. Commit.
- [x] 3. Write failing tests for `EdgeClient`: schema-major refusal; submit
   timeout and connection reset return indeterminate with one request sent;
   other operations raise `EdgeUnavailable`. Implement `edge_client.py` and the
   settings. Confirm they pass. Commit.
- [x] 4. Add `broker_mode` and `intent_created_at` to `MarketOrderRequest`, and
   fill them in `service.py`, `reconciliation.lookup_request_for_order` and
   `flatten_deployment`. Confirm `tests/execution` passes unchanged. Commit.
- [x] 5. Write failing tests for `EdgeQuoteSource` (age-based timestamp; `None`
   on unavailability; criterion 6 through the risk gate). Implement it, and
   switch the CLI to it. Delete `quote_source_from_market_data_service`. Run the
   paper tests with the fake edge serving quotes (criterion 9). Commit.
- [x] 6. Write failing tests in `test_edge_broker.py` for criteria 1 and 4, and
   for the move of the `test_metatrader_broker.py` behaviours. Implement
   `brokers/edge.py` and `brokers/routing.py`. Confirm they pass. Commit.
- [x] 7. Wire the router in `cli/q_execution.py` (paper plus live), and add
   `--crash-at`. Write `test_worker_on_edge.py` for criteria 2, 3 and 5,
   including the restart with an unknown live order. Confirm they pass. Commit.
- [x] 8. Delete `brokers/metatrader.py`, `brokers/mt5_runtime.py`,
   `fake_mt5_runtime.py` and `test_metatrader_broker.py`, and update
   `brokers/__init__.py`. Add `test_no_mt5_import.py`. Confirm the full
   execution suite passes. Commit.
- [x] 9. Allow `mt5_live` in `DeploymentCreateRequest`, with an API test that
   creates one and shows its orders rejected as live-locked while the gates are
   closed. Commit.
- [x] 10. Update `README.md` (worker needs the edge; settings) and
   `docs/mt5-wine-gateway.md` (the worker's use of the edge), and add the
   invariant-3 residual to FINDINGS. Commit.
- [x] 11. Recapture the OpenAPI in `q_contracts` on the branch
   `Q-042-execution-worker-on-the-edge`, run `make check` there, and update the
   `q_backend` row of `COMPAT.md`. Commit in `q_contracts`.
- [x] 12. Run `scripts/ci.sh`. Fix, re-run, commit.
- [ ] 13. **Human:** human-verifiable criteria 1–3 on the demo account, with the
   gates set in `~/.config/q/backend.env` for the demo login only
   (`Q_LIVE_EXECUTION_ENABLED`, `Q_LIVE_EXECUTION_ACCOUNT_ALLOWLIST`,
   deployment live activation, `Q_LIVE_EXECUTION_VALIDATED`,
   `Q_LIVE_EXECUTION_DRY_RUN=false`), and reset to deny afterwards.

## Validation

- **Unit:** client timeouts and outcome mapping; quote age; gate evaluation with
  the edge account; router dispatch.
- **Integration:** worker, service and reconciler against the fake edge through
  every submit and lookup outcome, including restart with an unknown order.
- **Regression:** paper execution tests unchanged; `tests/execution`; the API
  suite; `make contracts-check`.
- **Hygiene:** no `MetaTrader5` import under `execution/`.
- **Manual:** demo-account entry, crash-and-reconcile, flatten.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/execution tests/api -q
grep -rn "MetaTrader5" src/q_backend/execution || echo "execution is MT5-free"

# human (step 13)
systemctl --user start mt5-terminal mt5-gateway mt5-edge
uv run q-execution --log-level INFO run --crash-at after_broker_response
uv run q-execution --log-level INFO run 2>&1 | tee worker.log
```

## Handoff

Give the outcome-to-state table that the tests cover. Confirm the request log
shows one submit per intent in every test. List every deleted test and where
its behaviour now lives. Report the recaptured OpenAPI commit and the
`COMPAT.md` row. Quote the FINDINGS entry for invariant 3. From the human
steps, report the demo deal ticket, the crash-and-reconcile outcome with the
number of terminal orders, and the flatten result, and confirm the gates were
reset to deny afterwards.
