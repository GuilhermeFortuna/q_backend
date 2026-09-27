# Q-067: Catalog-based paper deployments

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §5.1, §6.1, §9](https://github.com/GuilhermeFortuna/q_contracts/blob/main/docs/system-architecture.md)  
**Depends on:** Q-066  
**Implementation plan:** [`../plans/Q-067-catalog-based-paper-deployments-plan.md`](../plans/Q-067-catalog-based-paper-deployments-plan.md)

## Purpose

Today the terminal creates a deployment from a saved backtest run with no
parameter controls. The API also accepts a client-built `identity`, but that
path checks a hash and registry name without validating parameter bounds or
proving that the worker can build the strategy. This task gives the terminal a
server-validated catalog path and preserves every accepted configuration edit
as a revision. A research backtest is useful evidence, but is not a prerequisite
for an internal paper run.

## Requirements

### Deployable catalog

- `GET /api/v1/execution/strategy-catalog` lists registered built-in candle
  strategies and saved custom wrappers. It supplies display name, source kind,
  base strategy, description and typed parameter specs/defaults, including
  supported exit-rule parameters. Tick strategies and `CompositeStrategy`
  genomes are absent. The existing research catalog remains unchanged.
- A custom wrapper is deployable only when its base resolves to a supported
  built-in candle strategy. At creation, the backend expands the wrapper's
  defaults and the submitted overrides into a compiled base-strategy config.
  Editing or deleting the custom wrapper later cannot change an existing
  deployment. The original wrapper name remains visible as provenance.

### Create and validate

- `POST /api/v1/execution/deployments` gains a mutually exclusive `catalog`
  input: `strategy_name`, `strategy_params`, `exit_params`, `symbol`,
  `timeframe`, `sizing_config`, `risk_config`, `paper_cost_config`. It creates
  a draft paper deployment at `config_revision=1`. The backend computes the
  compiled config and hash; clients cannot supply or override them on this
  path. A saved-run create path remains available.
- Resolve defaults from registry metadata, then validate every supplied
  parameter name, type, choice, min/max and step against that metadata.
  Validate exit parameters through the exit-rule registry. Reject unknown
  fields and values that cannot build a strategy. Validate exact symbol
  availability through the market-data provider, a supported timeframe of
  M15 or slower, a finite warm-up bound, positive sizing and point value,
  nonnegative paper costs, and existing risk bounds. Quote freshness stays a
  system safety setting, not an editable cost. Closed market or a
  temporarily missing quote does not invalidate an otherwise known symbol;
  the runtime still refuses stale or unavailable quotes.
- Apply the same buildability and identity checks to existing saved-run and
  explicit-identity creation paths so neither bypasses forward-execution
  eligibility. Each path preserves its source provenance.

### Edit and resume

- `PATCH /api/v1/execution/deployments/{id}/configuration` accepts
  `expected_revision`, `actor`, and a complete replacement of parameters,
  sizing, risk and paper costs. It requires an `Idempotency-Key`. The strategy
  name, symbol, timeframe, account and broker mode cannot change. A different
  symbol or timeframe requires a new deployment.
- Edits are allowed in `draft`, or in `paused` when the net position is flat,
  no order awaits reconciliation, and no flatten action is pending. The API
  locks the deployment row, validates the entire replacement, stores a new
  immutable revision and updates the current projection in one transaction.
  A stale `expected_revision` returns `409`; a disallowed lifecycle, position
  or pending order also returns a clear conflict without a partial update.
- A revision records the source strategy, expanded compiled config, hash,
  sizing, risk, paper costs, actor and timestamp. Every subsequent decision
  stores its revision and cost snapshot. Prior decisions and revisions remain
  unchanged. The deployment stream publishes the new current revision.
- After start or resume, the worker warms from historical bars without
  emitting historical orders. Its first tradable decision is for a bar whose
  close is strictly after the persisted activation time. A revision change
  rebuilds the evaluator before that decision. Duplicate bars and worker
  restarts do not replay an old signal.

## Constraints and non-goals

- This task configures paper deployments; Q-068 owns the dev-stack live lock,
  paper dispatch audit, cost application and performance reporting.
- One strategy, one symbol and one timeframe per deployment. No optimizer,
  backtest runner, genome editor, tick strategy or terminal-side strategy math.
- Preserve current intent-before-submission, lease, kill-switch and unknown
  order invariants. No in-place edit of a running strategy or an open position.

## Acceptance criteria

1. Catalog lists eligible built-in/custom candle strategies with usable typed
   parameter specs, but excludes tick and genome entries.
2. A catalog create with changed parameters and symbol produces a draft whose
   worker-buildable compiled config, hash, revision 1 and custom provenance
   match the API response and deployment event. A saved backtest is not needed.
3. Unknown, mistyped, out-of-range or non-buildable parameters, unsupported
   timeframe, unknown symbol, invalid costs and tick/genome strategies fail
   before a deployment or event is committed.
4. A custom wrapper changed or deleted after deployment does not change the
   deployment's base strategy, defaults, decisions or recovery build.
5. A draft or paused-flat edit with the current revision creates exactly one
   new revision and event; an idempotent retry creates none. Concurrent stale
   revision, running, open-position and unresolved-order edits leave state
   unchanged.
6. After pause/edit/resume, the first decision uses the new revision and a
   newly closed bar. Paused bars never produce catch-up orders, including
   after restart. Existing `tests/execution` and `tests/api` pass.
7. `make contracts-check` and the full `./scripts/ci.sh` pass.
