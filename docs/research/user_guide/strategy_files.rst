=====================
Strategy files
=====================

A strategy file is a ``ResearchStrategy`` written in your own editor and kept outside the Q
repositories, because strategies are private work and the repositories are public. The Research
stack lists strategy files from ``Q_STRATEGY_DIR``, validates them, backtests them from the
Backtests workspace, and freezes the exact code of every run into an immutable strategy version.

The directory
-------------

``Q_STRATEGY_DIR`` names the directory. It defaults to ``~/q-strategies``. A missing directory is an
empty catalog, not an error.

The directory is imported as the Python package ``q_strategies``, whatever its name on disk.

* Each top-level ``*.py`` file whose name does not start with ``_`` is one **strategy file**. Its stem
  is the strategy id: ``late_session_trend.py`` is ``late_session_trend``.
* Modules and packages whose names start with ``_`` (for example ``_lib/``) are shared helpers.
  Strategy files import them relatively: ``from ._lib.ticks import round_up_to_tick``.
* A strategy file defines exactly one ``ResearchStrategy`` subclass. Classes it imports do not count.
  It may also define functions and constants.
* A strategy file may import the standard library, ``numpy``, ``pandas``, ``q_backend.research`` and
  the helpers in its directory. The list is the documented contract; validation catches missing modules.

Declaring parameters
--------------------

A strategy class declares its ``__init__`` parameters in a class attribute ``parameters``. Each
argument maps to one of the declarations exported from ``q_backend.research``:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Declaration
     - Meaning
   * - ``IntParam(min, max, step=1)``
     - Integer with inclusive bounds.
   * - ``FloatParam(min, max, step=None)``
     - Float with inclusive bounds.
   * - ``BoolParam()``
     - Boolean, shown as a checkbox.
   * - ``ChoiceParam(choices=(...))``
     - String restricted to ``choices``.
   * - ``TimeParam()``
     - Time of day, shown as ``HH:MM``. Its default is a whole-minute ``datetime.time``.

Every declaration takes an optional ``label`` (default: the argument name) and ``hint``. The default
comes from the ``__init__`` signature and is not repeated. The class attribute ``label`` names the
strategy in the UI (default: the class name), and the first paragraph of the class docstring is its
description.

Every ``__init__`` argument needs a default and a declaration, and every declaration must name an
``__init__`` argument. The default must match the declared type and bounds.

Validation
----------

The stack validates a file each time its **bundle hash** changes. The bundle is the strategy file plus
every file under the directory's ``_``-prefixed helpers, so editing a helper revalidates every strategy
file that can import it. Validation runs in a separate process with a 10-second timeout, so a file that
hangs or crashes on import cannot hurt the API. It checks that:

* the file imports;
* it defines exactly one ``ResearchStrategy`` subclass;
* its parameter declaration is complete and consistent;
* the class builds with its defaults;
* it pickles, because session-parallel runs need that;
* each hook's signature is one the engine accepts.

A file that fails stays listed as invalid, with the first error. It cannot be run.

Versions
--------

A **strategy version** is immutable. It holds the strategy id, class name, bundle hash, every source
file of the bundle, the parameter specs and the creation time. Versions are content-addressed: a bundle
whose hash already has a version resolves to that version.

Every backtest of a strategy file freezes the current bundle before it runs, and the run records the
version. There is no separate "save" step. Editing or deleting a file never changes a version or a past
run. A run executes the version's frozen source, never the live file, so **Re-run** from Backtests
history reruns the same version with the same parameters, even after the file has changed.

Loading from scripts
--------------------

``load_strategy()`` returns the class from the file as it is on disk now, with its helper imports
resolved. Experiment scripts and Optuna drivers use it, so they run the code the stack runs:

.. code-block:: python

   from q_backend.research import backtest, load_strategy

   SessionBreakout = load_strategy("session_breakout")
   result = backtest(bars, strategy=SessionBreakout(range_bars=6), symbol="WIN$N", day_trade=True)

Running from the Backtests workspace
------------------------------------

The entry strategy picker gains a **Strategy files** group. Valid files are selectable; invalid files
are shown disabled, with their error. The list refreshes when the window regains focus, so an edit in
your editor appears without a restart. ``bool`` parameters render as checkboxes and ``time`` parameters
as ``HH:MM`` inputs.

Strategy-file runs use the candle engine, fixed-quantity sizing and the same costs, exit parameters and
day-trade settings as registry runs. They run through the research library's ``backtest()`` with
``check_causality=True``, so an indicator that reads a later bar fails the run with an error naming the
column and the bar. Optimize, Validate (walk-forward) and Discover do not list strategy files; keep
running optimizations from scripts through ``load_strategy()``.

Backtests history shows the strategy id and the first eight characters of the bundle hash for each
strategy-file run.

With ``./dev research --container`` the strategy directory is mounted read-only into the API and worker
containers.

Deploying to execution
----------------------

A saved backtest run of a strategy file can be deployed to the execution engine (either in paper mode
or to an MT5 demo account):

* **Deployable order kinds**: A deployment runs market entries and closes, protective levels,
  resting stop and limit entries, ``amend``, ``cancel``, OCO groups, timed exits and expiring entries.
  Only an MT5 demo deployment can hold resting or protective orders: a paper deployment of a run that
  uses them is refused with HTTP 422 at creation. Priced entries, priced closes and tick hooks are
  refused with HTTP 422 (see :doc:`live_placeable_orders`). A run with an enabled exit rule
  (``trailing`` or ``breakeven``) is also refused with HTTP 422 until its stops are managed live.
  If an order of a non-deployable kind or an exception is raised at runtime, the deployment pauses
  fail-closed with a risk event.
* **Timeframe**: Forward timeframes must be at least **M5** (e.g. M5, M10, M15, H1). M1 runs are
  refused.
* **Day-trading session**: Deployments require day trading (``day_trade=True``). Entries are accepted
  only within the session window (``start_time`` to ``end_time``). At or after ``close_time``, any open
  position is flattened with a market close. Always choose a close time inside active exchange trading
  hours (for example 17:00 or 18:15) so the closing market order is executed before market close.
* **Warmup bars**: The evaluator loads historical bars before the first decision so rolling indicators
  (moving averages, ATR, etc.) are warm. The strategy class may declare ``warmup_bars = N`` (defaulting
  to ``Q_EXECUTION_RESEARCH_WARMUP_BARS``, standard 100).
* **Parity**: Forward decisions bar-by-bar match backtest decisions exactly on identical bars,
  including session entry gates and day-trade exit.

Example
-------

The example below ships in ``examples/research/strategy_files``. It is validated by the test suite, and
the causality check passes on synthetic intraday bars. The helper ``_lib/ticks.py`` is imported
relatively by the strategy file.

.. literalinclude:: ../../../examples/research/strategy_files/_lib/ticks.py
   :caption: _lib/ticks.py
   :language: python

.. literalinclude:: ../../../examples/research/strategy_files/session_breakout.py
   :caption: session_breakout.py
   :language: python
