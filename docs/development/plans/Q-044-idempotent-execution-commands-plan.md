# Q-044 implementation plan: Idempotent execution commands

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-044-idempotent-execution-commands-spec.md`](../specs/Q-044-idempotent-execution-commands-spec.md)  
**Depends on:** Q-039

## Current-system context

The mutating execution routes are in `api/routers/execution.py`:
`POST /api/v1/execution/accounts`, `POST /api/v1/execution/deployments`,
`POST /api/v1/execution/deployments/{id}/actions`,
`POST /api/v1/execution/orders/{id}/resolve` and
`PUT /api/v1/execution/kill-switch`. Each takes
`session: Session = Depends(_session_or_503)` and calls a function in
`api/services/execution.py` (`create_account`, `create_deployment`,
`apply_deployment_action`, `resolve_order`, `update_kill_switch`). Those
functions commit themselves and raise `HTTPException` for business refusals.
The API error body follows `q_contracts/schema/api/error.schema.json`
(`ErrorResponse` in the backend). Migrations are Alembic, the latest being
`20260915_0019_lake_dataset_catalog.py`. `cli/q_outbox.py` has a `prune`
subcommand. The relay also prunes periodically, which is the model for a
periodic prune.

After Q-039, `q_contracts/schema/api/idempotency.yaml` declares the
`Idempotency-Key` header (UUID), `ttl: PT24H`, the replay rule, the
`required_for` list of those five operations, and the error codes
`idempotency_key_required` and `idempotency_key_reused`.

## Interfaces produced

```python
# alembic/versions/<date>_<next>_command_idempotency.py   (new; next free revision at implementation time)
# table command_idempotency(key uuid PK, method text, path text, body_sha256 text,
#                           status_code int, response_body jsonb, created_at timestamptz, index on created_at)

# src/q_backend/api/idempotency.py   (new)
class IdempotentCommand:                         # FastAPI dependency
    key: UUID | None
    def replay_or_claim(self, session: Session) -> Response | None: ...
    def store(self, session: Session, status_code: int, body: Any) -> None: ...   # same transaction as the command
def idempotent(route_fn): ...                    # decorator applying the protocol to one route
def prune_idempotency(session: Session, *, older_than: timedelta = timedelta(hours=24)) -> int: ...

# src/q_backend/storage/settings.py   (changed)
execution_idempotency_enforced: bool = False     # Q-050 flips the default to True

# src/q_backend/api/services/execution.py   (changed)
# the five command functions stop committing; the route commits once, after storing the result

# src/q_backend/cli/q_outbox.py   (changed)
q-outbox prune also prunes command_idempotency   # and the relay's periodic prune calls prune_idempotency
```

```
tests/api/test_execution_idempotency.py   new: criteria 1–8
q_contracts: schema/api/openapi.yaml, tools/validate.py check (header on every required_for op),
             schema/api/FINDINGS.md Finding 3, COMPAT.md    on branch Q-044-idempotent-execution-commands
```

Replay responses carry the header `Idempotency-Replayed: true`.

## Implementation decisions

- **Store the result in the command's own transaction.** The spec requires a
  stored result if and only if the command committed. The five service
  functions therefore stop calling `commit()`. The route runs the command,
  stores the result with `IdempotentCommand.store`, and commits once. Business
  refusals raise `HTTPException` today. The decorator catches them, stores
  their status and body, commits the stored result alone (the command changed
  nothing), and re-raises.

- **Claim with an insert, not a lock-and-check.** `replay_or_claim` inserts a
  placeholder row (`status_code = 0`) with `ON CONFLICT DO NOTHING`. If the
  insert took, this request owns the key. If not, it reads the row. A completed
  row is replayed, or refused as reuse. A placeholder means another request is
  in flight, and the answer is `409` with code `idempotency_in_progress` and
  `Retry-After: 1`. The placeholder commits with the command and vanishes with
  its rollback, so a crashed request frees its key.

- **`503` stores nothing,** because `_session_or_503` refuses before the route
  body runs, and a `SQLAlchemyError` during the command rolls back the claim
  with everything else.

- **Body identity is the SHA-256 of the canonical JSON body.** Keys are sorted,
  there is no whitespace, and an empty body hashes as `{}`. The same logical
  request from a different client then serializes to the same hash.

- **Enforcement starts off.** `q_frontend`'s execution workspace sends no key,
  and it stays in service until Q-050 removes it. With the flag off, a missing
  key is accepted and logged at warning level, and a present key gets full
  semantics. Q-048's terminal always sends keys. Q-050 turns enforcement on by
  default, so that it arrives the moment the last keyless client is gone.

- **Prune in two places:** the operator CLI `q-outbox prune`, and the relay's
  existing periodic prune loop, which already runs as a service. A separate
  timer unit would be one more unit for a table that grows by a few rows an
  hour.

- **The `q_contracts` side adds a validator check**, so that the captured
  OpenAPI declares the header parameter on every `required_for` operation. From
  then on the policy file and the capture cannot drift.

## Ordered implementation

- [ ] 1. Work on the branch `Q-044-idempotent-execution-commands` in `q_backend`,
   created from `development` by `./work start`. Confirm Q-039 is merged and
   pin it. Run `make contracts-check`. Commit.
- [ ] 2. Write the migration and model. Confirm upgrade and downgrade in
   `tests/storage`' migration test. Commit.
- [ ] 3. Write failing tests for criteria 1–8 in
   `tests/api/test_execution_idempotency.py`, one class per route, and a
   concurrency test using two threads and a barrier inside
   `reconciliation.apply_filled_resolution`. Confirm they fail. Commit.
- [ ] 4. Implement `api/idempotency.py`. Move commits out of the five service
   functions and into the routes, and apply `@idempotent`. Confirm the new tests
   and all of `tests/api` and `tests/execution` pass. Commit.
- [ ] 5. Add `prune_idempotency` to `q-outbox prune` and to the relay's periodic
   prune, with a test. Commit.
- [ ] 6. Document the header, the replay marker, the in-progress conflict and the
   enforcement flag in `README.md`. Record the research-job remainder in
   `FINDINGS.md`. Commit.
- [ ] 7. In `q_contracts`, on the branch `Q-044-idempotent-execution-commands`:
   recapture the OpenAPI, add the header check to `tools/validate.py` with a
   test, close Finding 3, and update `COMPAT.md`. If another batch-07 recapture
   merged first, rebase and capture again. Run `make check`. Commit in
   `q_contracts`.
- [ ] 8. Run `scripts/ci.sh`. Fix, re-run, commit.
- [ ] 9. **Human:** human-verifiable criterion 1, with enforcement on
   (`Q_EXECUTION_IDEMPOTENCY_ENFORCED=true`) and then off.

## Validation

- **Unit:** body hashing; claim, replay and reuse decisions.
- **Integration:** every covered route; concurrent resolve; rollback and `503`;
  business refusal replay; prune.
- **Regression:** `tests/api`, `tests/execution`; `make contracts-check`;
  `q_contracts` `make check`.
- **Manual:** a flatten sent twice with one key.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/api/test_execution_idempotency.py tests/api tests/execution -q
cd ../q_contracts && make check
```

## Handoff

List the five routes and their test outcomes. Confirm the concurrent resolve
applied one fill. Give the shipped enforcement default and the log line emitted
for a missing key. Report the `q_contracts` commit with the recapture and the
new check. From the human step, give both responses' headers and the audit-log
entry.
