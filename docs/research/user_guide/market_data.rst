===========
Market data
===========

Use :func:`~q_backend.research.load_bars` for completed candles or
:func:`~q_backend.research.load_ticks` for individual tick rows. Both need a
connected MT5 gateway; see :doc:`../getting_started/install`.
For saved sessions that can be read offline, use :doc:`tick_store`.

Loading bars
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

Check dates and history coverage
--------------------------------

Date-only strings and naive timestamps are interpreted in Brasília time.
An ``end`` date such as ``"2026-09-30"`` means midnight **at the start** of that
day, not its end. Use an explicit timestamp when you want to include that day's
session.

The terminal may return less history than requested. Inspect the actual range
before treating the dataset as complete:

.. code-block:: python

   metadata = bars.attrs["q_research"]
   print(metadata["returned_start"], metadata["returned_end"])

An empty result raises :class:`~q_backend.research.NoMarketDataError`.
An initial tick request can be empty while MT5 downloads history; retry after
the terminal has loaded it and check the returned range.

Loading and resampling ticks
----------------------------

Fetch granular trade and quote ticks with :func:`~q_backend.research.load_ticks`:

.. code-block:: python

   from q_backend.research import load_ticks, resample_ticks

   ticks = load_ticks("WIN$N", start="2026-10-01")
   minute_bars = resample_ticks(ticks, timeframe="M1")

``resample_ticks`` uses positive ``last`` prices and fills empty intervals
between the first and last eligible row with the previous close and zero volume.
It can therefore create bars across overnight gaps in a multi-day frame.
``TickStore.bars`` aggregates each session separately and avoids that synthesis.

Reading tick flags
------------------

The ``flags`` column contains readable labels joined by ``" | "``:

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

A row carrying both buy and sell flags has an unknown aggressor direction.
Every row also carries the current bid and ask; flags identify which fields
changed on that row. Filter zero quotes before calculating spreads or midpoints.
