# Q-023: Indicators served by q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.3, §7.1, §9 invariant 1, §10 phase 2](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#71-keeping-multi-repo-overhead-low)  
**Depends on:** Q-022  
**Implementation plan:** [`../plans/Q-023-indicators-served-by-q-core-plan.md`](../plans/Q-023-indicators-served-by-q-core-plan.md)

## Purpose

Every indicator and transform that research, backtests, and forward
execution use is computed by pandas code in `q_backend`: seven indicators,
five moving-average types, and four series transforms. Two of them, the
weighted and Hull moving averages and the rolling rank, run a Python function
per bar. Q-022 implements the same sixteen functions once in `q_core`, where
the parity gate checks them. Until `q_backend` calls them, the Rust copy is a
second implementation that nothing uses, and invariant 1 is broken the other
way round. This task makes `q_backend` depend on a tagged `q_core` release and
routes the three indicator modules through it, without changing any number a
backtest, a feature, or a live decision produces. It also removes the second
copy of the exit-rule indicator step inside the candle engine, so the
backtest and the live evaluator have one path into these functions. Q-024 and
the batch 05 kernels build on that single path.

## Requirements

### Dependency on q_core

- `q_backend` depends on `q_core` pinned to one release tag. The lockfile
  records the exact commit that tag resolved to, so two installs from the same
  lockfile get the same `q_core`.
- A fresh checkout installs and runs the pinned `q_core` in local development,
  in the GitHub CI workflow, and in the container image, with no path that
  points at a sibling checkout.
- `q_core` is required. No code path falls back to the pandas implementation
  when `q_core` is missing, old, or fails to import, because a silent fallback
  is a second implementation that nobody checks.
- An installed `q_core` that lacks any of the sixteen functions fails when the
  indicator modules are imported, with a message that names the missing
  functions and the installed `q_core` version. It does not fail in the middle
  of a backtest.

### Delegation

- The realized-volatility, Yang–Zhang, RSI, Bollinger, MACD, Donchian, and ATR
  indicators, the five moving-average types, and the rolling z-score, rolling
  rank, percent-change, and clip transforms all get their values from
  `q_core`. None of the sixteen keeps a pandas or NumPy computation of its
  values in `q_backend`.
- Every public function in the three modules keeps its name, parameter names,
  parameter order, defaults, and return shape. A function that returns a tuple
  still returns a tuple of the same length and order. No caller changes.
- Each returned series has the input's index object, and the name and dtype
  that today's implementation gives for the same input, including the cases
  where that name is empty today.
- Values for valid parameters equal today's values under the Q-021
  indicator policy (absolute-or-relative tolerance). A value `a` matches the
  expected value `b` if |a − b| ≤ 1e-10 or |a − b| ≤ 1e-12 · |expected|. Missing values sit at exactly
  the same positions, and so do positive and negative infinities.
- Inputs are handed to `q_core` without per-element Python objects. An input
  that is already contiguous float64 is not copied, and each result is wrapped
  without a copy.
- Series passed together to one function must share an index. Mismatched
  indexes raise a `ValueError` instead of being silently aligned or misaligned.
- Parameter validation stays in `q_backend`, and the existing error messages
  are kept word for word: the moving-average period and type checks, the
  percent-change lag check, and the clip bound check.

### Allowed behavior change: out-of-range windows

- This is the one intended behavior change in this task, and it was accepted
  explicitly. A window, period, or span below a function's minimum raises
  `ValueError`, and the message names the parameter and the rejected value.
  The moving-average period and percent-change lag already raise
  `ValueError` today, and they keep their existing messages.
  The check runs in `q_backend` before `q_core` is called. Today some of these
  cases raise `ZeroDivisionError` or `IndexError`, and some return a result.
  The minimum is 1 for every window, period, and span, except the Yang–Zhang
  window, which is at least 2.
- Every other input keeps today's outcome, including the existing validation
  messages above. No other exception type or message changes.

### One indicator path

- The candle backtest engine and the forward evaluator add a strategy's
  indicators and the ATR and Donchian columns its exit rules need through the
  same function. The engine keeps no copy of that step.
- That function lives in the backtesting layer, so the engine does not import
  the execution package. Existing imports of it from the execution package
  keep working.
- A strategy whose exit strategy is absent or `None` gets its own indicators
  and nothing else, in both the backtest and the evaluator.
- The existing tests that check the engine computes ATR or Donchian columns
  only when an exit rule needs them keep passing unchanged.

### Preserved guarantees

- Every golden file under `tests/backtesting/goldens/` stays byte-identical
  without regeneration. If a golden differs, the task stops and reports the
  difference. It does not regenerate the golden.
- The double-run determinism test, the backtest-to-live parity test, the
  genome registry parity test, the strategy, genome-node, and feature-spec
  causality tests, and the leakage exemption hygiene tests pass, and none of
  their files change.
- No existing test file changes. No existing test asserts one of the replaced
  exception types, so the parameter-domain change needs no edit.
- A reference of today's outputs for all sixteen functions is committed before
  any function is switched. It covers representative and edge parameters,
  missing values inside the series, constant and monotonic series, and integer
  input. It records the `q_backend` commit it came from, and the switched
  functions are checked against it.

### Cost

- The evaluator benchmark is measured before and after the switch, and its
  median indicator and evaluate phase times are reported for each fixture,
  whether they improve or not. Its existing thresholds (median indicator phase
  under 250 ms, median evaluate phase under 50 ms) still hold.
- The time to build and install `q_core` from a cold cache is measured and
  reported for local development and CI, because every consumer now compiles
  it.

## Constraints and non-goals

- **No new indicators, no changed parameters, no changed warm-up.** The
  sixteen functions keep today's semantics, including the Bollinger standard
  deviation with `ddof=1` that the z-score docstring wrongly calls population
  standard deviation. The docstring is corrected. The math is not.
- **No change to the inline math elsewhere.** The momentum, trend-blend, and
  time-series-momentum formulas written inline in the genome composite
  strategy and in feature computation, the numba kernels in the
  time-series-momentum and Gatev pairs strategies, the session-context and
  exogenous-context computations, and the tick breakout strategy's rolling SMA
  are left as they are. They move when their own kernels exist. Porting them
  here would widen the parity surface past what Q-021 fixtures cover.
- **No columnar signals and no change to the per-bar loop.** Signal columns
  are Q-024, and the candle loop moving to `q_core` is Q-028. The engine's
  per-row exit checks stay as they are.
- **No wheel publishing, release workflow, or package index for `q_core`.**
  Decided: `q_backend` consumes the Q-022 tag as a git source, and every
  consumer compiles `q_core`. Prebuilt wheels on a release or a local index
  would remove that build, but they are deferred to later `q_core` release
  work.
- **No feature-matrix cache invalidation.** Decided: `ENGINE_VERSION` in
  feature matrices stays 1. Matrix ids change only when semantics change, and
  this switch preserves values within the parity tolerance. Bumping the
  version would orphan every cached matrix over differences below that
  tolerance. It would also weaken the existing test that sets the version to 2
  to prove the id changes.
- **No `q_core` change.** If a kernel disagrees with the reference or a golden,
  the fix belongs to Q-022 and a new `q_core` tag. `q_backend` does not work
  around it.
- **No new benchmark suite.** The evaluator benchmark and the golden suite's
  wall time are the measurements. Engine, indicator, and tick benchmarks are
  not added.
- **No generated Python bindings or contract change.** `CONTRACTS_REV` is
  unchanged.

## Acceptance criteria

### Agent-verifiable

1. The project dependencies declare `q-core` from the `q_core` git repository
   at the Q-022 tag. The lockfile records that tag and its commit, and
   `uv sync --frozen` on a clean clone installs it.
2. The GitHub CI workflow installs the Rust toolchain that `q_core` pins
   before syncing dependencies, and the workflow passes on the task branch.
3. With a stub `q_core` module that lacks one required function, importing the
   indicator modules raises `ImportError`, and the message names that function
   and the installed version.
4. None of the three indicator modules computes a value with pandas rolling,
   exponential weighting, or NumPy arithmetic, and no module other than the
   single bridge module imports `q_core`.
5. The committed pandas reference, generated at the recorded pre-change
   commit, matches the delegated output of all sixteen functions for every
   case. Every value passes |a − b| ≤ 1e-10 or |a − b| ≤ 1e-12 · |expected|.
   Missing and infinite positions match exactly, and so do the index, name, and
   dtype. The maximum absolute difference for each function is printed. A negative control that perturbs
   one reference value by 1e-6 makes the comparison fail.
6. A float64 contiguous input shares memory with the array passed to `q_core`,
   and the returned series shares memory with the array `q_core` returned.
7. Mismatched indexes on a multi-input function raise `ValueError`. The
   existing moving-average, percent-change, and clip error messages are
   unchanged.
8. The allowed behavior change holds. For every function with a window,
   period, or span parameter, a value one below its minimum raises
   `ValueError`, and the message contains the parameter name and the value.
   The moving-average and percent-change functions keep their existing
   messages. The cases include Yang–Zhang with
   a window of 1, RSI and ATR with a period of 0, rolling rank with a window of
   0 or -1, and MACD with a fast span of 0. Every one of these cases, with its
   previous outcome, is listed in the handoff, and no other exception changes.
9. The candle engine has no inline ATR or Donchian completion. In sequential
   mode, a spy on the shared indicator function sees exactly one call per run,
   and in day-trade mode one call per trading day. The execution-package name
   is the same object as the backtesting-layer function.
10. `tests/backtesting/test_goldens.py` passes with no golden file changed, and
   `git diff --exit-code` over `tests/backtesting/goldens/` is clean.
11. The determinism, backtest-to-live parity, genome parity, causality, and
    leakage suites pass, and no existing test file changed.
12. The evaluator benchmark is run five times before and five times after the
    switch. The individual and median indicator and evaluate phase times for
    each fixture are reported, and the thresholds hold.
13. The golden suite's wall time before and after (five runs each) and the
    cold-cache `q_core` build time are reported.
14. The `q_backend` row in `q_contracts/COMPAT.md` records the `q_core` tag
    that `q_backend` pins.
15. The full validation suite passes in `q_backend`.

### Human-verifiable

1. The container image builds from a clean state with the pinned `q_core`,
   and `q_core` imports inside it with the expected version.  
   Command: `docker build --no-cache -t q-backend:q023 . && docker run --rm --entrypoint python q-backend:q023 -c "import q_core; print(q_core.version(), q_core.contracts_rev())"`
2. A backtest over a real catalogued series is run on `development` and on the
   task branch with the same configuration. Its headline metrics and trade
   count are confirmed identical.  
   Command: `pnpm tauri:dev` (Backtests workspace, same config both runs, API from each branch in turn)
3. For one paper deployment, the chart endpoint served by `development` and by
   the task branch returns the same indicator values over the same closed bars,
   within |a − b| ≤ 1e-10 or |a − b| ≤ 1e-12 · |expected|, with missing values in the same places.  
   Command: `curl -s localhost:8000/api/v1/execution/deployments/<id>/chart | jq '[.last_bar_close_time, (.indicators | map({key, last: .values[-5:]}))]'` (run against each branch's API in turn, then diff)
