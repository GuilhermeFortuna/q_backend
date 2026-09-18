# Q-040 implementation plan: Wine execution edge

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-040-wine-execution-edge-spec.md`](../specs/Q-040-wine-execution-edge-spec.md)  
**Depends on:** Q-039

## Current-system context

`gateway/mt5_gateway.py` (809 lines) is the pattern to follow. It is a
stdlib `http.server` process with `GatewayApp` (lazy `mt5.initialize`, one
lock, `health`), a `_GatewayHandler` that dispatches through a module-level
`_ROUTES` dict, `GatewayError(status, code, message)` mapped to JSON errors,
and `GatewayServer(ThreadingHTTPServer)`. It reads `MT5_GATEWAY_HOST` and
`MT5_GATEWAY_PORT` (default `18812`). `tests/market_data/test_mt5_gateway.py`
loads it by file path with `importlib`, after installing a fake `MetaTrader5`
into `sys.modules`, and drives it over real HTTP. `gateway/systemd/` holds
`mt5-terminal.service`, `mt5-gateway.service` and `mt5-gateway.env.example`.
The gateway unit runs `wine "${MT5_WINE_PYTHON}" "$(winepath -w …/mt5_gateway.py)"`
with a health-wait `ExecStartPre`. `tests/deploy/test_units.py` checks unit
structure. `docs/mt5-wine-gateway.md` is the operator guide.
`gateway/setup_wine.sh` installs Python 3.11.9, `MetaTrader5==5.0.5735` and
`numpy==1.26.4` in the prefix.

The order semantics to carry over are in
`src/q_backend/execution/brokers/metatrader.py` and `mt5_constants.py`:
`normalize_volume`, `normalize_price`, `select_filling_mode` (FOK, then IOC,
then RETURN, never hard-coded), `map_side_to_mt5`, the `TRADE_ACTION_DEAL` request,
`is_success_retcode`, `is_unknown_retcode` (`TIMEOUT`, `NO_CONNECTION`), and
deal matching by `magic` or comment prefix. `tests/execution/fake_mt5_runtime.py`
has a deterministic fake runtime with accounts, symbol info, ticks and deals.

The contract is `q_contracts/schema/edge/execution.yaml` with its JSON schemas.
After Q-039 it carries `intent_derivation` and the `intent_field_mismatch`
error. `q_backend/contracts/edge.py` holds the generated dataclasses, and
`make contracts` vendors only `api/arrow`, `catalog` and `stream` JSON schemas,
not `edge`.

## Interfaces produced

```
gateway/mt5_execution_edge.py
    SCHEMA_VERSION = "1.0"; SCHEMA_MAJOR = 1
    _ROUTES = {"/v1/health": ..., "/v1/quote": ..., "/v1/check": ..., "/v1/submit": ...,
               "/v1/lookup": ..., "/v1/positions": ..., "/v1/deals": ...,
               "/v1/account": ...}
    def intent_magic(intent_id: str) -> int            # contract formula, magic_base 0
    def intent_comment(intent_id: str) -> str
    class EdgeError(Exception): status, code, message
    class IntentTable: claim(intent_id) -> bool          # True once per id per process; lock-guarded
    class EdgeApp: health(), account(), quote(symbol), check(req), submit(req), lookup(req), positions(req), deals(req)
    class EdgeServer(ThreadingHTTPServer)
    main(argv) -> int                                     # --host (loopback only), --port (default 18813)
gateway/systemd/mt5-edge.service
gateway/systemd/mt5-edge.env.example
tests/gateway/test_mt5_execution_edge.py               # HTTP conformance with a fake MetaTrader5
tests/gateway/fake_metatrader5.py                      # sys.modules fake shared by gateway and edge tests
tests/gateway/test_edge_hygiene.py                     # imports, routes vs contract
Makefile: contracts target also vendors schema/edge/**/*.schema.json
CONTRACTS_REV → Q-039 commit
docs/mt5-wine-gateway.md                               # edge section
```

## Implementation decisions

- **A separate file, not a mode of the gateway.** The contract makes them
  separate processes with separate majors, so that one can be replaced without
  the other. The gateway may bind non-loopback with a token, and the edge may
  not. Sharing a file would make that distinction a flag.

- **Code is copied from `metatrader.py`, not imported.** The edge cannot import
  `q_backend`. The helpers move by copy, and the Linux-side originals are deleted
  in Q-042. Until then two copies exist for one batch. The contract vectors and
  Q-042's hygiene test end that.

- **Claim-before-send.** `IntentTable.claim` inserts the id under a lock and
  returns whether it was new. `submit` claims before it validates the symbol,
  the quote or the check. An intent that fails any later step is therefore still
  spent, and a duplicate cannot slip in between check and send. The worker
  never resubmits an intent anyway (§6.2). A claimed intent that was never sent
  is harmless: lookup finds nothing and answers `not_found`.

- **No inline recovery in submit.** `MetaTraderBroker.submit_market_order` calls
  `recover_unknown` on an ambiguous send. That is a lookup, and the contract says
  lookup is the caller's job. The edge answers `indeterminate` and stops. The
  caller then decides when to look up.

- **`lookup` returns `rejected` only on evidence.** That means a history order
  for the intent in a rejected or cancelled state, with no deal. No order and no
  deal returns `not_found`. A terminal that is not initialized, or a
  history call that returns `None`, returns `unavailable`. `closes_intent` is
  true for filled, rejected and not found, and false for unavailable, as the
  contract states.

- **Deal matching requires `magic` and the comment prefix together.** Today's
  `_reconcile_fill` accepts either one. The edge requires both, because a comment
  truncated by the broker still carries the prefix, and a magic collision across
  2³¹ values is unlikely but possible. The difference goes in FINDINGS as a
  deliberate tightening.

- **Unsupported order fields are refused.** `sl`, `tp`, a non-market `type`, or
  `type_time` other than GTC get `invalid_request`. The contract allows them
  for the future. Silently ignoring them would submit an order the caller did
  not ask for.

- **Loopback is enforced in `main`.** Any host other than `127.0.0.1` or `::1`
  exits with status 78 (`EX_CONFIG`), the code the backend units already treat
  as non-restartable.

- **Port 18813**, one above the gateway, configured through
  `MT5_EDGE_PORT` in `mt5-edge.env.example`.

- **The unit is `BindsTo=mt5-terminal.service` and `After=mt5-terminal.service`,
  `Type=simple` with a health-wait `ExecStartPost`** that polls `/v1/health`
  until `mt5_connected` is true, or times out without failing, as §8 describes.
  Wine processes cannot `sd_notify`, so the post-start probe is the readiness
  signal.

- **Conformance validates against the vendored JSON schemas with
  `jsonschema`,** so `make contracts` starts vendoring `schema/edge`.
  Validating against the generated dataclasses would test the generator, not the
  wire.

- **One fake `MetaTrader5` module for both gateway and edge tests,** extracted
  from `test_mt5_gateway.py` into `tests/gateway/fake_metatrader5.py` and
  extended with `order_check`, `order_send` (scripted results, a call counter,
  an optional barrier for the concurrency test), `orders_get`, `positions_get`,
  `history_orders_get` and `history_deals_get`. The gateway tests move to
  `tests/gateway/` unchanged in substance.

## Ordered implementation

- [x] 1. Work on the branch `Q-040-wine-execution-edge` in `q_backend`, created
   from `development` by `./work start`. Confirm Q-039 is merged in
   `q_contracts`. Set `CONTRACTS_REV` to its commit, extend `make contracts` to
   vendor `schema/edge`, run `make contracts` and `make contracts-check`.
   Commit.
- [x] 2. Extract the fake `MetaTrader5` into `tests/gateway/fake_metatrader5.py` and
   move the gateway tests beside it. Confirm they pass unchanged. Commit.
- [x] 3. Write failing hygiene tests in `tests/gateway/test_edge_hygiene.py`:
   imports parsed from source are stdlib, `MetaTrader5` or `numpy`; the
   `_ROUTES` keys equal the contract's `endpoints`; `intent_magic` and
   `intent_comment` reproduce the contract vectors. Commit.
- [x] 4. Write `gateway/mt5_execution_edge.py`: server, handler, errors,
   schema-major check, loopback enforcement, `health`, derivation functions,
   `IntentTable`. Confirm the hygiene tests pass. Commit.
- [x] 5. Write failing conformance tests for `account`, `quote`, `check`,
   `positions` and `deals` (each outcome validates against its schema; a disconnected terminal
   gives the contract's error; schema major 2 is refused). Implement them.
   Confirm they pass. Commit.
- [x] 6. Write failing tests for `submit`: accepted, rejected and each
   indeterminate path, each with exactly one `order_send`; concurrent duplicate
   with a barrier inside the fake `order_send`; a duplicate after completion;
   `intent_field_mismatch` with zero terminal calls; refused unsupported
   fields. Implement `submit`. Confirm they pass. Commit.
- [x] 7. Write failing tests for `lookup`: filled from deals, rejected from a
   history order without a deal, not found, unavailable when not initialized
   and when history returns `None`, with `closes_intent` correct for each.
   Implement. Confirm they pass. Commit.
- [x] 8. Add `gateway/systemd/mt5-edge.service` and `mt5-edge.env.example`,
   extend `tests/deploy/test_units.py` for `BindsTo`/`After` and the loopback
   host, and document the edge in `docs/mt5-wine-gateway.md`: port, unit,
   logs, health check, what the intent table does and does not remember. Commit.
- [x] 9. Add the matching-rule tightening to `docs/development/FINDINGS.md`
   under a Batch 07 heading. Commit.
- [x] 10. Run `scripts/ci.sh`. Fix, re-run, commit.
- [ ] 11. **Human:** human-verifiable criteria 1 and 2 on the demo account.

## Validation

- **Unit:** derivation vectors; intent table claim under contention; loopback
  enforcement; error mapping.
- **Integration:** every operation over real HTTP with the fake module, each
  response validated against the vendored schemas.
- **Regression:** gateway tests unchanged after the move; unit-structure tests;
  `make contracts-check`.
- **Manual:** health, quote, positions and deals under Wine; one demo submit,
  its duplicate and its lookup.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/gateway tests/deploy/test_units.py -v
make contracts-check

# human (step 11)
cp gateway/systemd/mt5-edge.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user start mt5-edge && journalctl --user -u mt5-edge -f
curl -s 127.0.0.1:18813/v1/health | jq
```

## Handoff

List each operation with the outcomes its tests cover. Confirm the concurrent
duplicate test counted exactly one `order_send`. Quote the FINDINGS entry for
the matching rule. Give the unit file and the env example. From the human
steps, report the terminal build, one quote age, and the demo submit's ticket,
its duplicate response, and its lookup outcome.
