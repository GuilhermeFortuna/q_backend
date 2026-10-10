=====================
Review in the desktop
=====================

Publishing saves a completed local backtest in the Research stack. Open it in
Backtests history to inspect the Trade Chart, Performance, Monthly breakdown,
and Trade List views.

Start the Research stack
------------------------

From the **Q workspace root**:

.. code-block:: bash

   ./dev research

Local backtesting needs no API, database, or worker. Publishing uses the
Research API and its storage, so the stack must be running for this step.

Publish a finished result
-------------------------

After running the :doc:`../getting_started/quickstart`:

.. code-block:: python

   run_id = result.publish(name="RSI reversion")
   print(f"Published run: {run_id}")

The API address defaults to ``http://127.0.0.1:8001``. Set ``Q_API_URL`` before
starting your script, or pass ``api_url=`` to
:meth:`~q_backend.research.BacktestResult.publish`.

The timeframe comes from ``bars.attrs["q_research"]["timeframe"]``. For a custom
DataFrame without that metadata, provide it explicitly:

.. code-block:: python

   run_id = result.publish(name="RSI reversion", timeframe="M5")

To display custom indicators, declare them in
:meth:`~q_backend.research.ResearchStrategy.chart_indicators`; see
:doc:`strategy` for an example.

What publishing does
--------------------

* Stores a run marked as originating from a script, without changing ``result``.
* Creates a new run on each call; it does not retry automatically.
* Saves results for review. Custom script strategies cannot be rerun or optimized
  from the desktop.
