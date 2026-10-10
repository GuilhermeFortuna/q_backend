==========
Q Research
==========

Load market data, write a strategy, and inspect a backtest using ordinary
pandas DataFrames. Q Research is the Python research library of the Q platform;
its indicators and execution kernel use the shared Rust engine, ``q_core``.

The usual workflow is **data → indicators → strategy → backtest → results**.
Local analysis runs in your Python process. A gateway is needed for fresh MT5
data; publishing results to the desktop is optional.

.. grid:: 1 1 3 3
   :gutter: 3

   .. grid-item-card:: Getting started
      :link: getting_started/index
      :link-type: doc
      :shadow: none

      Set up your environment and work through a first backtest, step by step.

   .. grid-item-card:: User guide
      :link: user_guide/index
      :link-type: doc
      :shadow: none

      Understand data, strategy decisions, execution timing, and results.

   .. grid-item-card:: API reference
      :link: reference/index
      :link-type: doc
      :shadow: none

      Look up parameters, return values, and methods for the public API.

Start with a DataFrame
----------------------

.. code-block:: python

   from q_backend.research import indicators, load_bars

   bars = load_bars("WIN$N", timeframe="M5", start="2026-09-01")
   bars["ema_21"] = indicators.ma(bars["close"], period=21, kind="ema")
   print(bars[["close", "ema_21"]].tail())

This example needs the MT5 gateway. Already have a price frame? Use it directly
with :mod:`~q_backend.research.indicators` or :func:`~q_backend.research.backtest`.
The :doc:`getting_started/quickstart` explains the required columns and index.

Find the right guide
--------------------

* **Choose a futures series:** :doc:`getting_started/price_series`.
* **Save ticks for offline research:** :doc:`user_guide/tick_store`.
* **Write entry and exit rules:** :doc:`user_guide/strategy`.
* **Understand when orders fill:** :doc:`user_guide/execution`.
* **Review a run in the desktop:** :doc:`user_guide/publishing`.

.. toctree::
   :maxdepth: 2
   :hidden:

   getting_started/index
   user_guide/index
   reference/index
