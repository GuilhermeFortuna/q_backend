# Q-134: Strategy files in the Research stack

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 37 — Strategy files
**Depends on:** none
**Repositories:** q_backend (owner), q_contracts, q_frontend
**Implementation plan:** [Plan](../plans/Q-134-strategy-files-in-the-research-stack-plan.md)

## Purpose

A `ResearchStrategy` written with the research library runs only from a script. The Research stack can show its
published result (Batch 17) but cannot run it again, because the stack never holds the strategy. This task makes
the stack read strategy files from a directory the author edits in their own editor. It lists them, backtests
them from the Backtests workspace, and freezes the exact code of every run into an immutable strategy version.
The version is the seam that a later task deploys to MT5 (`q_contracts/docs/system-architecture.md` §5.1).

No translation is needed. `ResearchStrategyAdapter` (`src/q_backend/research/adapter.py`) already turns a
`ResearchStrategy` into the `TradingStrategy` that the candle engine and the forward evaluator consume. This task
adds discovery, parameter metadata, validation and freezing around that adapter.

## Current system

- `q_backend.research.backtest()` (`src/q_backend/research/engine.py`) runs a `ResearchStrategy` instance on a
  frame through `ResearchStrategyAdapter` and the `q_core` candle kernel, with an optional `TickStore` for
  intrabar fills and `check_causality=` for indicator look-ahead.
- `result.publish()` (`src/q_backend/research/publishing.py`) posts a finished run to
  `POST /api/v1/backtests/import`. The run is stored with `origin="script"` and provenance (script, class,
  source, git revision), and cannot be re-run.
- Backtest jobs (`src/q_backend/api/backtest_jobs.py`) build only registry strategies (`strategy_registry.py`).
  `StrategyParamSpec.type` is `int`, `float` or `categorical`.
- The author's strategies live on the `research/experiments` branch in `experiments/strategies/*.py`, several
  classes per file, next to module-level helpers, constants and Optuna drivers. Parameters are `__init__`
  keyword arguments with defaults, of type `int`, `float`, `bool` and `datetime.time`.

## Behaviour

### Strategy directory

- The setting `Q_STRATEGY_DIR` names the directory. It defaults to `~/q-strategies`. The directory sits outside
  every Q repository, because strategies are private work and the repositories are public. A missing directory
  is an empty catalog, not an error.
- The directory is imported as the Python package `q_strategies`, whatever its name on disk.
  - Each top-level `*.py` file whose name does not start with `_` is one **strategy file**. Its stem is the
    strategy id (`late_session_trend.py` → `late_session_trend`).
  - Modules and packages whose names start with `_` (for example `_lib/`) hold shared helpers. Strategy files
    import them relatively: `from ._lib.ticks import ceil_tick`.
- A strategy file defines exactly one `ResearchStrategy` subclass (classes it imports do not count). It may
  also define functions and constants.
- Strategy files may import the standard library, `numpy`, `pandas`, `q_backend.research` and helper modules
  in the directory. Nothing enforces this list; it is the documented contract, and the validation below catches
  missing modules.
- `q_backend.research.load_strategy(strategy_id)` returns the class from the current file, so experiment
  scripts and Optuna drivers use the same code the stack runs.

### Parameter declaration

- A strategy class declares its parameters in a class attribute `parameters`, a mapping from `__init__`
  argument name to one of `IntParam`, `FloatParam`, `BoolParam`, `ChoiceParam` or `TimeParam`, exported from
  `q_backend.research`.
  - Each takes an optional `label` and `hint`.
  - `IntParam` and `FloatParam` take `min`, `max` and `step`, `ChoiceParam` takes `choices`.
  - The default comes from the `__init__` signature and is not repeated.
- Every `__init__` argument after `self` has a default and a declaration, and every declaration names an
  `__init__` argument. The default must match the declared type and bounds. `TimeParam` defaults are
  `datetime.time` values and travel on the wire as `"HH:MM"`.
- The class attribute `label` names the strategy in the UI (default: the class name). The first paragraph of the
  class docstring is its description.

### Validation

- The stack validates a file each time its **bundle hash** changes. The hash covers the strategy file and every
  file under the directory's `_`-prefixed helper modules (sorted relative paths and contents). Validation runs in
  a child process with a 10-second timeout, so a file that hangs or crashes on import cannot hurt the API. It
  checks:
  - the file imports;
  - it defines exactly one `ResearchStrategy` subclass;
  - its parameter declaration is complete and consistent, as above;
  - the class builds with its defaults;
  - it pickles, because session-parallel runs need that.
- A file that fails stays listed with `valid: false` and the first error message. It cannot be run.
- Every stack run of a strategy file passes `check_causality=True` to `backtest()`. Indicator look-ahead fails
  the run with the error that names the column and bar.

### Strategy versions

- A **strategy version** is immutable. It holds:
  - the strategy id and class name;
  - the bundle hash and the bundle source (each path and its content);
  - the parameter specs;
  - the creation time.

  Versions are stored in PostgreSQL. They are content-addressed: freezing a bundle whose hash already has a
  version returns that version.
- Every backtest of a strategy file freezes the current bundle before it runs, and the run references the
  version. There is no separate "save" step. Editing or deleting a file never changes a version or a past run.
- A run executes the version's frozen source, never the live file. Re-running from Backtests history reruns the
  same version with the same parameters, even after the file changed.

### Control API (`q_contracts`)

- `StrategyParamSpec.type` gains `bool` and `time`. A `bool` default is a boolean, and a `time` default is an
  `"HH:MM"` string. Existing types and payloads are unchanged.
- `GET /api/v1/strategy-files` lists every strategy file:
  - id, label, description, class name and bundle hash;
  - `valid` and `error`;
  - parameter specs, when valid;
  - `uses_tick_hooks`, true when any hook declares a `ticks` parameter.
- `GET /api/v1/strategy-versions/{version_id}` returns a version, including its source.
- `BacktestRequest` gains `strategy_file`, which holds the strategy id, or `strategy_version_id` for a re-run.
  Each is mutually exclusive with `strategy` and `entries`. Supplied parameters are validated against the specs.
  Unknown names, wrong types and out-of-bound values are rejected with `422`, as for registry strategies.
- Run list and detail payloads gain `strategy_version_id` (null for registry and script runs). Their `origin`
  is `stack`.
- Optimization, walk-forward, discovery, ML-filter and execution requests reject `strategy_file` and
  `strategy_version_id` with a clear `422`.

### Running

- The worker loads the bars through the market-data service, as other jobs do. It builds the version's class
  with the submitted parameters and calls `q_backend.research.backtest()`. Quantity, point value, costs, exit
  parameters and day-trade settings come from the request, as for registry runs. The worker passes a
  `TickStore` for the symbol when the configured tick store (`Q_RESEARCH_TICK_STORE`) holds that symbol, and
  `None` otherwise. The run records whether ticks were used.
- The completed run is stored in the same rows and lake files as other runs, so the Performance, Monthly,
  Trade Chart (with the strategy's `chart_indicators()`) and Trade List views serve it unchanged.
- Progress and cancellation work as for other candle jobs.
- In `./dev research --container` the strategy directory is mounted read-only into the API and worker
  containers.

### Research UI (`q_frontend`)

- The Backtests workspace's entry strategy picker gains a **Strategy files** group. Valid files are selectable.
  Invalid files are shown disabled, with their error.
- The list refreshes when the window regains focus, so an edit in the editor shows up without a restart.
- Parameter fields render `bool` as a checkbox and `time` as an `HH:MM` input.
- Backtests history shows the strategy id and the first eight characters of the bundle hash for a
  strategy-file run, and **Re-run** reruns its version.
- Optimize, Validate (walk-forward) and Discover do not list strategy files.

### Documentation

- The research library guide gains a "Strategy files" page, covering:
  - the directory and the package rules;
  - the parameter declaration;
  - validation;
  - versions;
  - `load_strategy()`;
  - running from the Backtests workspace;
  - an example file with a `_lib` helper.
- The README's configuration section lists `Q_STRATEGY_DIR`.

## Acceptance criteria

1. With a `tmp_path` strategy directory, `GET /api/v1/strategy-files` lists:
   - a valid file, with its parameter specs, including a `bool` and a `time` parameter;
   - a file with two strategy classes, a file with an undeclared `__init__` argument, a file that raises on
     import and a file that sleeps past the timeout, each with `valid: false` and its own error.
2. Changing a helper under `_lib/` changes the bundle hash of the files that share it, and they are validated
   again.
3. A backtest of a valid file creates one version and a completed run that references it. A second backtest of
   the unchanged file reuses that version. After the file is edited, a re-run from the first run reproduces the
   first run's trades and metrics, and a new run of the file creates a second version.
4. On the same bars and parameters, a stack run and a direct `backtest()` call of the same class produce the same
   trades and metrics.
5. A strategy whose `compute_indicators` reads a later bar fails its run with the causality error.
6. Parameters with an unknown name, a wrong type or an out-of-bound value are rejected with `422`, and
   optimization and walk-forward requests naming a strategy file are rejected with `422`.
7. `load_strategy("late_session_trend")` returns the class from the directory, and its relative `_lib` import
   resolves.
8. Frontend tests, against MSW:
   - the Strategy files group lists valid and disabled files;
   - `bool` and `time` fields submit the right values;
   - a strategy-file run in history shows its id and hash, and Re-run sends `strategy_version_id`.
9. `make contracts-check` passes in `q_backend` and `q_frontend` on the new contract revision, and existing
   registry and script-run tests pass unchanged.

## Out of scope

- Deployment of strategy versions, M5 forward execution, and MT5 order types (later tasks).
- Optimization, walk-forward and discovery over strategy files. The author keeps running Optuna in scripts
  through `load_strategy()`. Hook speed (Batch 16: 57.5 s on 70,000 bars) is a prerequisite for that.
- Editing strategy source in the UI. Files are edited in the author's editor only.
- Sandboxing strategy code. The stack is single-user and runs the author's own code; validation isolates import
  failures, not malicious code.
- Moving the existing classes out of `experiments/strategies/`. The author ports them; the implementing agent
  writes the documentation example only.

## Human inspection

Automated checks use fixtures, the API test client and MSW. No MT5, Docker, GPU or desktop run is required of the
implementing agent. During `./work inspect Q-134`, the reviewer points `Q_STRATEGY_DIR` at a directory holding a
port of `LateSessionTrendStrategy` (from `experiments/strategies/ind_strategies.py` on `research/experiments`).
They then:

1. run it on `IND$N` M5 from the Backtests workspace under `./dev research`;
2. compare its trades and PnL with a script `backtest()` run on the same range.
