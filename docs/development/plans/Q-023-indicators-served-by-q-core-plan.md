# Q-023 implementation plan: Indicators served by q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-023-indicators-served-by-q-core-spec.md`](../specs/Q-023-indicators-served-by-q-core-spec.md)  
**Depends on:** Q-022

## Current-system context

`q_backend` does not depend on `q_core` today. `pyproject.toml` lists no
`q-core`, and `[tool.uv.sources]` holds only the `metatrader5` stub. The
indicator math is pandas in three modules under `src/q_backend/backtesting/`.
`technical_indicators.py` (83 lines) holds `compute_realized_vol`,
`compute_yang_zhang`, `compute_rsi`, `compute_bollinger_bands`, `compute_macd`,
`compute_donchian_channels`, and `compute_atr`. `moving_averages.py` (65
lines) holds `compute_ma` over `sma`/`ema`/`wma`/`smma`/`hma`, with `_wma` as a
per-window `rolling.apply` Python callback and `_hma` built from three `_wma`
passes. It also holds `normalize_ma_type`, `VALID_MA_TYPES`, and
`MA_TYPE_LABELS`, which are not math. `transforms.py` (47 lines) holds
`compute_rolling_zscore` (its docstring says population std, but it uses
`ddof=1`), `compute_rolling_rank` (a Python loop over every index),
`compute_pct_change`, and `compute_clip`. Outside the engine they are called
from 17 source modules, 59 call sites in all: nine strategy modules,
`strategy.py`, `genome/composite_strategy.py`, `features/compute.py`,
`features/targets.py`, `features/evaluation.py`,
`session_context/compute.py`, `market_data/exogenous_context.py`, and
`execution/indicator_frame.py`. Every call is positional, with pandas `Series`
taken from a single frame. Running today's functions on
pandas 3.0.2 gives these output names: the input's name for most functions,
`high`/`low` for Donchian, and `None` for `compute_atr`, `compute_yang_zhang`
(its operands have different names), and `compute_rolling_rank` (it builds a
new `Series`). Every result is `float64`, except `compute_clip` on `int64`
input. That stays `int64` when both bounds are whole numbers and becomes
`float64` otherwise. Out-of-domain windows are inconsistent:
`compute_rsi`/`compute_atr` with 0 raise `ZeroDivisionError`,
`compute_rolling_rank` with 0 or -1 raises `IndexError`, `compute_yang_zhang`
with 1 raises `ZeroDivisionError`, and several functions accept 0.

`backtesting/engine.py` `_run_single_chunk` calls
`self.strategy.compute_indicators(chunk)` at line 123 without a copy. Lines
125–155 are then a verbatim copy of `execution/indicator_frame.py`
`augment_indicator_frame`, which differs in three ways. It passes
`frame.copy()`. It returns early when `exit_strategy` is `None` instead of
testing `hasattr`. And it binds `compute_atr`/`compute_donchian_channels` at
module import, where the engine imports them lazily inside the method. That
lazy import is load-bearing: `tests/backtesting/test_exit_rules.py` lines 183,
194, and 464 patch `q_backend.backtesting.technical_indicators.compute_atr`
and `.compute_donchian_channels` and expect the engine to see the patch.
`augment_indicator_frame` is used by `execution/evaluator.py:220` and
`api/services/execution_chart.py:186`, and `tests/api/test_execution_chart_api.py`
imports it and patches `chart_service.augment_indicator_frame`. The engine
cannot simply import `q_backend.execution.indicator_frame`, because
`q_backend/execution/__init__.py` imports `warmup.py`, which imports
`optimization.walkforward`, which reaches back into the backtesting engine.
The regression locks already exist:
`tests/backtesting/test_goldens.py` (12 candle cases and 1 tick case on
`synthetic_ohlcv(n=400, seed=20240609)`, `FLOAT_DECIMALS = 10`,
`--regen-goldens`, `test_determinism_double_run`, `test_backtest_live_parity`),
`test_strategy_causality.py`, `genome/test_node_causality.py`,
`test_genome_parity.py`, `tests/features/test_leakage.py`, and
`tests/execution/test_evaluator_benchmark.py`. The benchmark covers three
`MACrossover` fixtures and asserts median `indicators_ms < 250` and
`evaluate_ms < 50`. The goldens exercise only SMA, MACD, RSI, ATR, and
Donchian, so they cannot prove the other eleven functions unchanged. On the
build side, `q_core` at `v2026.09.12` builds with maturin
(`pyproject.toml` `module-name = "q_core"`, distribution `q-core`,
`crates/q-py` pyo3 0.24 `abi3-py312`), pins Rust 1.98.0 in
`rust-toolchain.toml`, and publishes no wheels. Its `RELEASING.md` documents
the consumer pin as a uv git source by tag. The `q_backend` `Dockerfile` is a
single `uv:python3.12-bookworm-slim` stage that runs `uv sync --frozen
--no-dev` with no git and no compiler. `.github/workflows/ci.yml` runs
`uv sync --frozen` and then `scripts/ci.sh` on `ubuntu-latest`. This task
closes two gaps: the Q-022 kernels have no consumer, and the engine keeps a
second copy of the indicator step.

## Interfaces produced

```toml
# pyproject.toml  (additions)
[project]
dependencies = [ ..., "q-core" ]

[tool.uv.sources]
q-core = { git = "https://github.com/GuilhermeFortuna/q_core.git", tag = "<Q-022 tag vYYYY.MM.DD[.n]>" }
```

```python
# src/q_backend/backtesting/indicator_kernels.py   (new; the only module that imports q_core)
REQUIRED_KERNELS: tuple[str, ...]            # the 16 q_core function names as published by Q-022

def check_kernels(module: ModuleType) -> None: ...
    """Raise ImportError naming every missing REQUIRED_KERNELS entry and module.version(). Runs at import."""
def as_float64(series: pd.Series) -> np.ndarray: ...
    """Contiguous float64 values. No copy when already contiguous float64; pd.NA -> NaN."""
def shared_index(*series: pd.Series) -> pd.Index: ...
    """The common index object; ValueError if lengths or labels differ (identity short-circuits)."""
def to_series(values: np.ndarray, index: pd.Index, name: Hashable | None) -> pd.Series: ...
    """Wrap a q_core result without copying."""
def require_window(param: str, value: int, minimum: int) -> int: ...
    """ValueError f"{param} must be >= {minimum} (got {value})." for out-of-domain windows."""
```

```python
# src/q_backend/backtesting/technical_indicators.py   (bodies delegate; signatures unchanged)
def compute_realized_vol(close: pd.Series, window: int, periods_per_year: int = 252) -> pd.Series: ...        # name=close.name; window >= 1
def compute_yang_zhang(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series,
                       window: int, periods_per_year: int = 252) -> pd.Series: ...                          # name=None; window >= 2
def compute_rsi(close: pd.Series, period: int) -> pd.Series: ...                                             # name=close.name; period >= 1
def compute_bollinger_bands(close: pd.Series, period: int, num_std: float) -> tuple[pd.Series, pd.Series, pd.Series]: ...  # (upper, middle, lower)
def compute_macd(close: pd.Series, fast_period: int, slow_period: int, signal_period: int) -> tuple[pd.Series, pd.Series, pd.Series]: ...
def compute_donchian_channels(high: pd.Series, low: pd.Series, period: int) -> tuple[pd.Series, pd.Series]: ...  # names high.name, low.name
def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series: ...            # name=None

# src/q_backend/backtesting/moving_averages.py   (compute_ma delegates; _wma and _hma removed)
def compute_ma(series: pd.Series, period: int, ma_type: str) -> pd.Series: ...
# unchanged: MaType, VALID_MA_TYPES, MA_TYPE_LABELS, normalize_ma_type

# src/q_backend/backtesting/transforms.py   (bodies delegate; docstring of zscore corrected to sample std, ddof=1)
def compute_rolling_zscore(series: pd.Series, window: int) -> pd.Series: ...
def compute_rolling_rank(series: pd.Series, window: int) -> pd.Series: ...                                    # name=None
def compute_pct_change(series: pd.Series, change_bars: int) -> pd.Series: ...
def compute_clip(series: pd.Series, clip_low: float, clip_high: float) -> pd.Series: ...                     # dtype per baseline
```

```python
# src/q_backend/backtesting/indicator_frame.py   (new home; body moved from execution/indicator_frame.py)
def augment_indicator_frame(strategy: TradingStrategy, frame: pd.DataFrame) -> pd.DataFrame: ...
    """compute_indicators(frame.copy()) then exit-rule ATR/Donchian completion, calling
    technical_indicators.compute_atr / .compute_donchian_channels through the module attribute."""

# src/q_backend/execution/indicator_frame.py   (re-export only)
from q_backend.backtesting.indicator_frame import augment_indicator_frame   # same object

# src/q_backend/backtesting/engine.py   (_run_single_chunk: lines 122-155 become one call)
chunk = augment_indicator_frame(self.strategy, chunk)
```

```
tests/fixtures/indicators/export_pandas_baseline.py   generator, run once at the pre-change commit
tests/fixtures/indicators/pandas_baseline.json        cases, full-precision values, name, dtype, provenance
tests/backtesting/test_indicator_baseline.py          delegated output vs baseline + negative control
tests/backtesting/test_indicator_kernels.py           bridge helpers, import check, parameter domain
tests/backtesting/test_indicator_frame_path.py        one indicator path for engine and evaluator
Dockerfile                                            builder stage with git, gcc, rustup 1.98.0; runtime stage copies /app/.venv
.github/workflows/ci.yml                              Rust 1.98.0 step before `uv sync --frozen`
README.md                                             Prerequisites: Rust toolchain for the q_core build
q_contracts: COMPAT.md                                q_backend row records the q_core tag
```

## Implementation decisions

- **`q_core` is pinned as a uv git source by tag, the form `q_core/RELEASING.md`
  documents, and `uv.lock` records the resolved commit.** Architecture §7.1
  allows a git source or a local index. A local index would need a place to
  host wheels, and `q_core` publishes none. Release assets would also need a
  `q_core` release workflow, plus manylinux tagging old enough for the
  bookworm image's glibc 2.36, and both are `q_core` release work outside this
  task. The cost of the git source is that every consumer compiles `q_core`.
  That cost is measured (step 5) and reported, and it is the evidence for a
  later wheel-publishing task.

- **The container build becomes two stages. A builder stage from the same uv
  base image adds `git`, `gcc`, `libc6-dev`, `curl`, and a minimal rustup
  profile with toolchain 1.98.0, then runs `uv sync --frozen --no-dev`. The
  runtime stage copies `/app/.venv`, `src`, `alembic`, and the entrypoint.**
  uv needs `git` to fetch a git source, and the maturin build needs `cargo`
  and a C linker. Installing them in the one existing stage would ship a
  compiler and about 1 GB of toolchain in the API and worker image. Both stages
  use the same base image, so the venv's interpreter paths stay valid. The
  toolchain version comes from `q_core`'s `rust-toolchain.toml`, so the
  builder installs exactly that version instead of `stable`.

- **CI installs Rust 1.98.0 with `dtolnay/rust-toolchain`, the step `q_core`'s
  own CI uses, before `uv sync --frozen`. It relies on `setup-uv`'s cache for
  the built wheel.** `ubuntu-latest` ships rustup, and `q_core`'s
  `rust-toolchain.toml` would make rustup fetch 1.98.0 on the fly. But that
  only works while the runner image keeps rustup, and it hides the toolchain
  choice inside a build log. The uv cache keeps wheels built from source, so a
  warm CI run does not recompile `q_core`.

- **One bridge module, `backtesting/indicator_kernels.py`, is the only
  importer of `q_core`, and it checks at import that all sixteen Q-022
  functions exist.** Q-022 fixes the exact Python names, and keeping them in
  one `REQUIRED_KERNELS` tuple means a rename touches one line. An import-time
  check turns a stale `q_core` (an old venv, or a forgotten `uv sync`) into an
  error that names what is missing. Without it, the first `AttributeError`
  would come in the middle of an optimization run. There is no `try/except
  ImportError` fallback, because a pandas fallback is exactly the second
  implementation that invariant 1 forbids.

- **The conversions do one bounded copy at most on the way in and none on the
  way out.** `as_float64` uses `Series.to_numpy(dtype=np.float64,
  na_value=np.nan)`, which returns the underlying buffer for contiguous float64
  data and makes exactly one copy otherwise (the golden `volume` is already
  float; integer and nullable inputs copy once). `to_series` constructs with
  `copy=False` over the array `q_core` returned, because pandas 3 otherwise
  copies NumPy input in the constructor. Invariant 5 allows bounded copies, and
  a test with `np.shares_memory` pins both paths.

- **Names and dtypes follow the baseline, not a rule.** Today `compute_atr`,
  `compute_yang_zhang`, and `compute_rolling_rank` return unnamed series,
  Donchian returns `high`/`low`, and `compute_clip` keeps `int64` for integer
  input when both bounds are whole numbers (and `float64` otherwise). The
  wrappers reproduce exactly that. `compute_clip` casts the float64 kernel
  result back to the input's integer dtype only in that case, and the cast is
  exact because every clipped value is then a whole number. The baseline has a
  case for each branch. Bollinger and MACD outputs keep `close.name`, because that
  is what pandas arithmetic propagates today. A single "always name it after
  the input" rule would rename three columns that users of the feature store
  and charts already see.

- **Multi-input functions require the same index and raise `ValueError`
  otherwise.** Pandas aligns `high - low` by label, and NumPy arrays have no
  labels. Every current caller passes columns of one frame (all 60 call
  sites), so the check never fires in the codebase. Without it, a future
  caller with shifted indexes would get silently wrong values instead of
  today's union-aligned ones. `shared_index` short-circuits on identity, which
  is the common case, so the check costs nothing on the hot path.

- **Out-of-domain windows raise `ValueError` in Python before `q_core` is
  called. Domain minimums are 1 for every window and period, except
  `compute_yang_zhang`, whose window must be at least 2 because `k` divides by
  `window - 1`. MACD spans are at least 1.** Today's `ZeroDivisionError` and
  `IndexError` are accidents of the pandas code, and passing such values into a
  kernel would make `q_backend`'s behavior depend on how Q-022 reports domain
  errors across FFI. The existing messages ("MA period must be at least 1.",
  "change_bars must be >= 1 (got …).", "clip_low must be <= clip_high (got …).")
  and the `normalize_ma_type` check are kept word for word, because tests match
  on them. The genome and strategy parameter bounds never produce these
  values: `GENOME_PARAM_BOUNDS` periods start at 5.

- **The reference for all sixteen functions is exported once, from the
  pre-change `development` commit, into `tests/fixtures/indicators/pandas_baseline.json`,
  and compared with the Q-021 `AbsRelTol` rule (abs 1e-10 or rel 1e-12), not by rounding.** The goldens only
  reach five of the functions. The Q-021 fixtures live in `q_core` and check
  kernels, not the pandas wrapper contract (index, name, dtype, integer and
  nullable input, error types). Values are stored at full precision:
  JSON float `repr` round-trips exactly, and NaN and ±inf are stored as the
  strings `"nan"`, `"inf"`, and `"-inf"`. Rounding to 10 decimals, as the
  goldens do, can flip a digit on a difference of 1e-15 that straddles a
  rounding boundary. The rule is Q-021's indicator policy `AbsRelTol { abs: 1e-10, rel: 1e-12 }`:
  a value matches if |a − b| ≤ 1e-10 or |a − b| ≤ 1e-12 · |expected|, with NaN
  and ±inf positions exact. The baseline
  records the `q_backend` commit, pandas and NumPy versions, and the generator
  seed. The cases are: `synthetic_ohlcv()` from `test_goldens.py` (the golden
  frame itself) with two parameter sets per function, one short and one near
  the frame's default windows; a window larger than the series; a series with
  NaN runs inside it; a constant series (zero standard deviation, RSI 0/0); a
  strictly increasing series (RSI loss 0); a series containing `0.0` so that
  percent change produces ±inf; an `int64` series; and a `Float64` series with
  `pd.NA`. The generator is committed, so the reference can be reproduced at
  the recorded commit, but no test regenerates it, because regenerating after
  the switch would compare `q_core` to itself.

- **The comparison prints each function's maximum absolute difference, and a
  negative control perturbs one stored value by 1e-6 and asserts the comparison
  fails.** A comparison that only passes cannot show that the tolerance is not
  set so loose that everything passes. The printed maximum shows how much room
  is left under the tolerance.

- **`augment_indicator_frame` moves to `backtesting/indicator_frame.py`, and
  `execution/indicator_frame.py` re-exports it.** The engine sits in the
  backtesting layer. Importing `q_backend.execution` from it would run
  `execution/__init__.py`, which pulls in the warm-up, API schemas, and
  walk-forward modules and closes an import cycle back to the engine. The
  re-export keeps `evaluator.py`, `execution_chart.py`, and
  `test_execution_chart_api.py` unchanged. Their patch of
  `chart_service.augment_indicator_frame` keeps working, because it patches the
  name in the chart service's own namespace.

- **The moved function calls `technical_indicators.compute_atr(...)` and
  `technical_indicators.compute_donchian_channels(...)` through the module
  attribute.** The three `test_exit_rules.py` tests patch those attributes and
  expect the engine to see the patch, as today's lazy import does. A
  `from … import` binding would make the "only required columns" test pass
  silently and the "ATR computed once" test fail. Keeping attribute lookup
  keeps those tests unchanged and still guarding the real path.

- **The unified path always passes `frame.copy()` to `compute_indicators` and
  treats a `None` exit strategy as absent.** This is the evaluator's current
  behavior, and the parity tests already lock it. Every registered strategy
  copies its input anyway (all 13 concrete `compute_indicators`
  implementations in `src` start with `data.copy()`), so the extra copy changes no output. It only stops
  test strategies that mutate their input from mutating the engine caller's
  frame. The copy cost is covered by the golden suite's wall-time measurement.
  The later `hasattr(self.strategy, "exit_strategy")` checks at engine lines
  257 and 279 are left alone, because they belong to the per-bar loop that
  Q-024 and Q-028 change.

- **`ENGINE_VERSION` in `features/matrix.py` stays 1.** Values are equal
  within the parity tolerance, and matrix ids are documented as changing for
  new semantics. `tests/features/test_matrix.py:213` also monkeypatches the
  version to 2 to prove that ids change, so bumping it to 2 would quietly
  weaken that test.

- **No existing test file is edited.** If a Q-022 kernel disagrees with the
  baseline or a golden, the task stops, sets the board to blocked with the
  first diverging function, case, and index, and waits for a new `q_core` tag.
  A workaround in the wrapper would put semantics back into Python.

## Ordered implementation

1. Work on the branch `Q-023-indicators-served-by-q-core` in `q_backend`,
   created from `development` by `./work start`. Confirm that the Q-022 tag
   exists (`git ls-remote --tags https://github.com/GuilhermeFortuna/q_core.git`)
   and read its Python surface and the Q-021 `AbsRelTol` rule (abs 1e-10 or rel 1e-12) policy at that tag. If
   the tag is missing, set the board to blocked and stop.
2. Before any source change, record the before-measurements. Run
   `uv run pytest tests/execution/test_evaluator_benchmark.py -s -q` five times
   and keep every `BENCHMARK` line. Run `uv run pytest
   tests/backtesting/test_goldens.py -q` five times and keep the wall times.
3. Write `tests/fixtures/indicators/export_pandas_baseline.py` with the cases
   from the decisions above, run it at the current commit, and commit the
   generator and `pandas_baseline.json`, with the commit hash from
   `git rev-parse HEAD` recorded inside the JSON. Then write
   `tests/backtesting/test_indicator_baseline.py`. For every case it asserts
   value agreement within the Q-021 `AbsRelTol` rule (abs 1e-10 or rel 1e-12), identical NaN and ±inf
   positions, `result.index is input.index`, and the recorded `name` and
   `dtype`. It also has the negative control that perturbs one value by 1e-6
   and expects `AssertionError`. Run it against today's pandas code and
   confirm that every case passes. Then temporarily remove the perturbation
   and confirm the negative-control test fails, which shows the control
   really checks something. Restore it and commit.
4. Write failing tests in `tests/backtesting/test_indicator_frame_path.py`.
   A `MagicMock(wraps=augment_indicator_frame)` patched at
   `q_backend.backtesting.engine.augment_indicator_frame` sees exactly 1 call
   for a sequential run over `synthetic_ohlcv()`, and one call per distinct
   date for a `ParallelMode.DAY_TRADE` run.
   `q_backend.execution.indicator_frame.augment_indicator_frame is
   q_backend.backtesting.indicator_frame.augment_indicator_frame`. A strategy
   whose `exit_strategy` is `None` runs through the engine without
   `AttributeError`, and its chunk has no `atr_*` column. Confirm they fail.
   Move the function, reduce `execution/indicator_frame.py` to the re-export,
   and replace engine lines 125–155 (and the call at 123) with the single call.
   Confirm the new tests, `tests/backtesting/test_exit_rules.py`,
   `tests/api/test_execution_chart_api.py`, and `test_goldens.py` pass with no
   golden change. Commit.
5. Write a failing test, `test_q_core_installed` in
   `tests/backtesting/test_indicator_kernels.py`. It asserts that
   `importlib.metadata.version("q-core")` resolves and that
   `q_core.contracts_rev()` is 40 lowercase hex characters. Confirm it fails
   with `PackageNotFoundError`. Add `q-core` to dependencies and the git source
   by tag, then run `uv lock` and `uv sync`. Time a cold build with
   `UV_CACHE_DIR=$(mktemp -d) uv sync --frozen` and record it. Confirm the test
   passes. Add the Rust 1.98.0 step to `.github/workflows/ci.yml`, convert the
   `Dockerfile` to the two-stage build, and add the Rust prerequisite to
   `README.md`. Commit.
6. Write failing tests in `test_indicator_kernels.py`. `check_kernels` on a
   `types.SimpleNamespace` stub with all but one required name and
   `version=lambda: "0.0.0"` raises `ImportError` whose message contains the
   missing name and `0.0.0`. `as_float64` on a contiguous float64 series
   returns an array for which `np.shares_memory` with `series.to_numpy()` is
   true. On an `int64` series it returns float64 values `[0.0, 1.0, 2.0]`, and
   on `pd.Series([1.0, pd.NA], dtype="Float64")` it returns `[1.0, nan]`.
   `shared_index` on two series with `RangeIndex(3)` and `RangeIndex(1, 4)`
   raises `ValueError`, and on the same frame's columns it returns that index
   object. `to_series` over an array returns a series that shares memory with
   it. `require_window("window", 0, 1)` raises `ValueError`. Confirm they fail,
   implement `indicator_kernels.py`, and confirm they pass. Commit.
7. Write failing parameter-domain tests in `test_indicator_kernels.py`.
   `compute_yang_zhang(o, h, l, c, 1)`, `compute_rsi(c, 0)`, `compute_atr(h, l,
   c, 0)`, and `compute_rolling_rank(c, 0)` raise `ValueError`, and so does
   `compute_atr` with `high` reindexed by one label. `compute_ma(c, 0, "sma")`
   still raises `ValueError("MA period must be at least 1.")`. Confirm the new
   cases fail (today they raise `ZeroDivisionError`, `IndexError`, or
   nothing). Delegate the seven functions in `technical_indicators.py`
   through the bridge. Confirm that the baseline test for those seven, the
   domain tests, `test_yang_zhang_volatility.py`, `test_exit_rules.py`, and
   `test_goldens.py` pass. Commit.
8. Delegate `compute_ma` for all five types and delete `_wma` and `_hma`, which
   nothing else imports. Confirm the baseline test for the five MA types,
   `test_moving_averages.py`, `test_strategy_library.py`, and the goldens pass.
   Commit.
9. Delegate the four transforms, delete the `compute_rolling_rank` loop, and
   correct the `compute_rolling_zscore` docstring to sample standard deviation
   (`ddof=1`). Confirm the full baseline test (all sixteen functions, every
   case, and the maximum difference per function printed with `-s`),
   `genome/test_normalization_nodes.py`, and
   `test_session_context_cache.py` pass. Commit.
10. Add a check to `test_indicator_kernels.py`: a scan of `src/q_backend` finds
    `import q_core` only in `backtesting/indicator_kernels.py`, and the three
    indicator modules contain no `.rolling(`, `.ewm(`, `.shift(`, `.clip(`,
    `.diff(`, `np.log`, or `np.sqrt`. Confirm
    it passes. Commit.
11. Regression. Run `uv run pytest tests/backtesting tests/features
    tests/execution tests/api/test_execution_chart_api.py -q`, then
    `git diff --exit-code development -- tests/backtesting/goldens`, then
    `git diff --name-status development -- tests`, and confirm that only the
    new files from steps 3–6 appear, with no `M` entries. If a golden differs,
    do not pass `--regen-goldens`. Set the board to blocked with the diff and
    the first function that the baseline test reports, then stop.
12. Measurement. Repeat both step 2 measurements five times each on the branch.
    Tabulate the individual values and medians per fixture against step 2, and
    confirm the benchmark thresholds hold.
13. In `q_contracts`, on the branch `Q-023-indicators-served-by-q-core`, update
    the `q_backend` row of `COMPAT.md` with the branch head and the pinned
    `q_core` tag, and add a line to "Verified by". Run `make check` and
    commit.
14. Human step, matching human-verifiable criterion 1. Build the image with
    `--no-cache`, run `q_core.version()` inside it, and record the build time
    and image size against the `development` image.
15. Human step, matching human-verifiable criterion 2. Run the same backtest on
    a real catalogued series with the API on `development`, then on the task
    branch, and compare the headline metrics and trade count.
16. Human step, matching human-verifiable criterion 3. For one paper
    deployment, fetch the chart endpoint from each branch's API at the same
    last closed bar, and diff the last five indicator values per key.
17. Run the full validation suite in `q_backend`. Commit.

## Validation

- **Unit:** bridge conversions without copies and with one bounded copy;
  import-time kernel check; index agreement; parameter domain errors and the
  kept messages; single indicator path for sequential and day-trade runs.
- **Integration:** the engine, evaluator, and chart endpoint share one
  indicator function; strategies, genome nodes, and features compute through
  `q_core` end to end.
- **Regression:** all sixteen functions match the pre-change pandas reference
  (values within the Q-021 `AbsRelTol` rule (abs 1e-10 or rel 1e-12); NaN and ±inf positions, index, name, and
  dtype exact); goldens byte-identical; determinism, backtest-to-live parity,
  genome parity, causality, and leakage suites unchanged and passing; no
  existing test file modified.
- **Manual:** container image build and import (step 14); real backtest
  metrics (step 15); chart indicator values (step 16).
- **Measurement:** evaluator benchmark medians per fixture and golden suite
  wall time, five runs before and five after, individual values and medians;
  cold-cache `q_core` build time locally, in the CI log, and in the container
  build.
- **Pins:** `CONTRACTS_REV` is unchanged. The `q_backend` row in
  `q_contracts/COMPAT.md` records the `q_core` tag (step 13).

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/backtesting/test_indicator_baseline.py tests/backtesting/test_indicator_kernels.py tests/backtesting/test_indicator_frame_path.py -v -s
uv run pytest tests/backtesting/test_goldens.py tests/backtesting/test_strategy_causality.py tests/backtesting/genome/test_node_causality.py tests/backtesting/test_genome_parity.py tests/features/test_leakage.py -q
git diff --exit-code development -- tests/backtesting/goldens
git diff --name-status development -- tests
for i in 1 2 3 4 5; do uv run pytest tests/execution/test_evaluator_benchmark.py -s -q | grep BENCHMARK; done
for i in 1 2 3 4 5; do /usr/bin/time -f '%e s' uv run pytest tests/backtesting/test_goldens.py -q > /dev/null; done
UV_CACHE_DIR="$(mktemp -d)" /usr/bin/time -f '%e s' uv sync --frozen

# human
docker build --no-cache -t q-backend:q023 .
docker run --rm --entrypoint python q-backend:q023 -c "import q_core; print(q_core.version(), q_core.contracts_rev())"
curl -s localhost:8000/api/v1/execution/deployments/<id>/chart | jq '[.last_bar_close_time, (.indicators | map({key, last: .values[-5:]}))]'

cd /home/gui/projects/q/q_contracts
make check
```

## Handoff

Report the pinned `q_core` tag and the commit `uv.lock` resolved it to. Report
the cold-cache `q_core` build time locally, in CI, and in the container
build, and the image size before and after. Report the baseline file's
provenance commit, its case count and size, the Q-021 `AbsRelTol` rule (abs 1e-10 or rel 1e-12) used, and the
maximum absolute difference for each of the sixteen functions. Confirm that
the negative control failed. Confirm that `git diff` over the goldens is empty
and list every file under `tests/` that changed (it should be new files only).
Give the evaluator benchmark's five individual median indicator and evaluate
phase times per fixture before and after, with the median of each, and the
golden suite's wall times before and after. List every case where an
out-of-domain parameter now raises `ValueError` instead of what it did before.
From the human steps, report the image import output, the real backtest's
headline metrics and trade count on both branches, and the chart value diff.
