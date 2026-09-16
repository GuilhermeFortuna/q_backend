# Phase 2 acceptance record (Q-031)

**Written against commit:** `5da9eeb` (branch tip at handoff)  
**Branch:** `Q-031-execution-evaluator-on-q-core`  
**Scope:** Q-026 through Q-031 — q_core bootstrap part 2

## Environment

- Host: local development (Linux)
- Python: 3.12+ via `uv`
- q_core: pinned wheel from Q-028 (`DecisionStep`, `enabled_rules`, `max_position`, `size_entry`)

## Automated validation

| Group | Command | Result |
|---|---|---|
| Exit rules | `uv run pytest tests/backtesting/test_exit_rules.py -q` | pass |
| Candle bridge | `uv run pytest tests/backtesting/test_indicator_kernels.py -q` | pass |
| Decision-step parity | `uv run pytest tests/backtesting/test_goldens.py::test_backtest_live_parity -q` | pass |
| Queued-signal baselines | `uv run pytest tests/backtesting/test_signal_baseline.py -q` | pass |
| Registry baselines | `uv run pytest tests/backtesting/test_engine_registry_baseline.py -q` | pass |
| Execution evaluator | `uv run pytest tests/execution -q` | pass |
| Sizing / warmup | `uv run pytest tests/backtesting/test_position_sizing.py tests/backtesting/test_position_sizing_factory.py tests/execution/test_warmup.py -q` | pass |
| Full CI | `./scripts/ci.sh` | pass (2014 tests) |

## Derived figures (Q-031)

Evaluator benchmark medians (3 runs after change, p50 ms):

| Fixture | indicator p50 | evaluate p50 |
|---|---:|---:|
| CCM$ H1 | 2.09 | 0.21 |
| WIN$ H1 | 2.09 | 0.20 |
| WDO$ M15 | 2.09 | 0.21 |

Worker full-path p95 remains under the 50 ms budget (test asserts `<500` ms with headroom; observed well below 50 ms on fixtures).

Hygiene: `grep -rn "def on_bar\|def should_exit\|def evaluate_queued_signals" src` returns no matches.

## Manual acceptance (pending)

| Task | Criterion | Command | Compare against |
|---|---|---|---|
| Q-026 | Exit-rule fixtures regenerate with no diff; regeneration time reported | `cd q_core && time make fixtures-backend-check` | empty diff |
| Q-026 | Rule catalog matches backend registry at pin | `cd q_core && cargo doc -p q-engine --no-deps --open` | every registered rule, param name and default |
| Q-027 | Candle-engine and queued-signal fixtures regenerate with no diff | `cd q_core && time make fixtures-backend-check` | empty diff |
| Q-027 | Kernel throughput vs Python engine on 50k-bar series | `cd q_core && make bench-candle-kernel` | median wall time and bars/s for both |
| Q-028 | Real-lake backtests identical on development and task branch | `pnpm tauri:dev` (Backtests workspace, each backend branch) | trade counts and headline metrics |
| Q-028 | 50-trial optimization wall time on both branches | `pnpm tauri:dev` (Optimization workspace, same seed) | wall times and best trial metrics |
| Q-029 | Tick fixtures regenerate with no diff | `cd q_core && time make fixtures-backend-check` | empty diff |
| Q-029 | Tick simulation throughput vs Python kernel | `cd q_core && make bench-tick-kernel` | median wall time on 5M-tick stream |
| Q-030 | Real tick backtest identical on development and task branch | `pnpm tauri:dev` (Backtests workspace, tick engine) | trade count, metrics, M1 chart bars |
| Q-031 | Paper MACrossover + trailing stop, restart while open, trailing exit | `uv run q-execution --log-level INFO run` + decisions API | exit reason `trailing`, timings within bound |

**Phase 2 status:** pending manual entries above.

## Deferred follow-ups

- Evaluator open-trade update after fills (FINDINGS #18) — justify: live activation safety
- Evaluator pandas window swap (FINDINGS #21) — justify: contracted-column consumer or benchmark bottleneck
- Candle/tick day-boundary mismatch (FINDINGS #9)
- Duplicate momentum math (FINDINGS #10)
- Tick inverse-vol sizing rejection (FINDINGS #19)
