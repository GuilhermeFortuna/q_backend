===========
Market Data
===========

Overview
--------

The market data module (:mod:`q_backend.research.data`) provides functional helpers to ingest historical OHLCV bars and tick sequences from MetaTrader 5 via the Q gateway.

Loading Bars
------------

Use :func:`~q_backend.research.load_bars` to request completed candle series:

.. code-block:: python

   from q_backend.research import load_bars

   bars = load_bars(
       "WDO$N",
       timeframe="M10",
       start="2026-09-01",
       end="2026-09-30T18:00:00-03:00",
   )

Characteristics of returned bars:
- **Index**: Timezone-aware timestamp (``time``) in Brasília time (``America/Sao_Paulo``). Sorted and unique.
- **Columns**: ``open``, ``high``, ``low``, ``close`` (``float64``), ``tick_volume`` (``int64``), ``spread``, ``real_volume`` (``float64``).
- **Forming Candle Exclusion**: The current incomplete candle is excluded.
- **Paging**: Transparently paginates queries larger than the gateway's 50,000-candle limit.
- **Metadata**: Query parameters, data source, and tick-grid statistics are stored in ``bars.attrs["q_research"]``.

Loading & Resampling Ticks
--------------------------

Fetch granular trade and quote ticks with :func:`~q_backend.research.load_ticks`:

.. code-block:: python

   from q_backend.research import load_ticks, resample_ticks

   ticks = load_ticks("WIN$N", start="2026-10-01")
   minute_bars = resample_ticks(ticks, timeframe="M1")

Tick Flag Formatting
--------------------

Rather than raw numeric bitmasks, the ``flags`` column returns descriptive labels joined by pipe delimiters (``" | "``):

- ``bid update``
- ``ask update``
- ``last-price update``
- ``volume update``
- ``buy trade``
- ``sell trade``
- ``undocumented bits (N)``

To filter trades by direction unambiguously:

.. code-block:: python

   buy_mask = ticks["flags"].str.contains("buy trade", regex=False)
   sell_mask = ticks["flags"].str.contains("sell trade", regex=False)
   buy_trades = ticks.loc[buy_mask & ~sell_mask]
