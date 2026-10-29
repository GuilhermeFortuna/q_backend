# Batch 38 — Research strategies on MT5

**Status:** issues published on 2026-10-29; written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend. Tasks also change q_contracts and q_terminal.

## Outcome

A strategy file version that Q-134 froze is deployed from `q_terminal` to an MT5 demo account. It trades there
with market orders, protective stops and targets, and resting stop and limit orders. The forward evaluator runs
the same hooks as the research backtest, and the broker fills the orders against real ticks. Real-money trading
stays locked until the controlled-account validation is recorded.

## Findings this batch answers

Each was read from the code on 2026-10-29.

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | Only the allowlist tells a demo account from a real one. | `evaluate_live_gates` (`execution/brokers/live_gates.py`) checks the enable flag, the login allowlist, deployment activation and `Q_LIVE_EXECUTION_VALIDATED`. The edge's `/v1/account` reports no trade mode. `live_execution_dry_run` is read nowhere. | Q-135 |
| 2 | Strategy versions cannot be deployed. | `build_strategy_from_compiled` (`execution/strategy_build.py`) builds registry strategies only, and the deploy catalog (`execution/catalog.py`) lists registry entries only. | Q-136 |
| 3 | The forward evaluator never calls research hooks. | `StrategyEvaluator` (`execution/evaluator.py`) decides through `signal_arrays` and `candle_kernel.evaluate_bar`. Research hooks run only through the kernel's `runtime_callback` in backtests. | Q-136 |
| 4 | Forward execution refuses M5 and has no day-trade session. | `execution/validation.py` rejects timeframes below M15. Nothing in `execution/` reads `day_trade_*` settings, while the IND and WIN strategies exit at the session close. | Q-136 |
| 5 | All bundles import as one package name. | `research/strategy_files.py` imports every bundle as `q_strategies` and evicts the previous one from `sys.modules`. A worker holding several deployments needs them side by side. | Q-136 |
| 6 | The edge sends market deals only. | `gateway/mt5_execution_edge.py` builds `TRADE_ACTION_DEAL` requests and refuses order types other than buy and sell. `ExecutionBroker` has `submit_market_order` only. | Q-137 |

## Tasks and delivery order

| Task | Repositories | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| [Q-135](https://github.com/GuilhermeFortuna/q_backend/issues/59) — MT5 demo-account gate | q_backend, q_contracts | none | [Spec](../specs/Q-135-mt5-demo-account-gate-spec.md) | [Plan](../plans/Q-135-mt5-demo-account-gate-plan.md) |
| [Q-136](https://github.com/GuilhermeFortuna/q_backend/issues/60) — Deploy research strategy versions | q_backend, q_contracts, q_terminal | Q-135 | [Spec](../specs/Q-136-deploy-research-strategy-versions-spec.md) | [Plan](../plans/Q-136-deploy-research-strategy-versions-plan.md) |
| [Q-137](https://github.com/GuilhermeFortuna/q_backend/issues/61) — Stop, limit and protective orders on MT5 | q_backend, q_contracts | Q-136 | [Spec](../specs/Q-137-stop-limit-and-protective-orders-on-mt5-spec.md) | [Plan](../plans/Q-137-stop-limit-and-protective-orders-on-mt5-plan.md) |

Implement them in order. Q-135 and Q-137 both change the execution-edge contract and `gateway/mt5_execution_edge.py`.
Q-136 and Q-137 both change the forward evaluator and the deployment validation.

## Shared decisions

- **MT5 demo replaces Q's paper broker for forward tests.** The paper broker stays for tests and existing
  deployments, but no new feature depends on it. A deployment of a strategy version defaults to `mt5_live` when
  the gate allows MT5 orders.
- **The account decides, not a flag.** The edge reports MT5's trade mode. A demo account needs no
  controlled-account validation; a real or contest account still does. A missing trade mode counts as real.
- **One set of hooks.** The forward evaluator calls the version's `compute_indicators`, `exit_strategy` and
  `entry_strategy` through the research adapter's hook invocation. There is no second implementation of a strategy.
- **Only orders the broker can hold.** Live, a strategy decides on completed bars, and MT5 fills against real
  ticks. Deployable orders:
  - market entries and closes;
  - `stop_loss` and `take_profit` on entries;
  - `amend`;
  - resting `buy_stop`, `buy_limit`, `sell_stop` and `sell_limit` orders, with `expire_bars`;
  - `cancel`.

  Priced (`price=`) entries and closes are filled within the candle that decided them. Live trading cannot
  reproduce that, and the research guide already says so. Tick hooks need a live tick feed. Neither is deployable.
- **Known before deploy.** A stack run records the order kinds its strategy used. A deployment is created only
  from a run whose kinds the current release supports, so an unsupported order is refused at creation, not
  discovered on the account.
- **Fail closed at runtime too.** An order kind outside the supported set, returned live, pauses the deployment
  with a risk event. Nothing is sent.
- **Focused verification.** Fakes for the edge (`tests/execution/fake_edge.py`), for `MetaTrader5`
  (`tests/gateway/fake_metatrader5.py`) and for the stack's fixtures. No MT5, Wine, Docker, GPU or desktop run is
  required of an agent. The human inspects each task on the demo account.

## Recorded, not in this batch

- **Real-money activation.** It stays behind `Q_LIVE_EXECUTION_VALIDATED`. The demo runs of this batch are the
  evidence for that decision, which the human takes outside a task.
- **Tick hooks and sub-bar decisions live** (tier B in `q_contracts/docs/system-architecture.md` §3.2).
- **Retiring the paper broker** and its cost configuration.
- **Automatic rewriting of priced orders.** Strategies such as `RSIStrategy` and `AlphaExperimentStrategy` in
  `experiments/strategies/wdo_strategies.py` use priced closes for their stops. They become deployable once the
  author expresses those stops as `stop_loss` and `amend`.

## Review and integration

Documentation is committed on the local `docs/batch-38-mt5-strategy-deployment` branch of `q_backend` in
`.worktrees/q_backend/docs-batch-38-mt5-strategy-deployment`. The complete specification and plan text is
embedded in each issue.
