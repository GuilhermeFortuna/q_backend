==========
Tick store
==========

Save sessions for offline use
-----------------------------

Broker tick history is short, and old sessions are eventually purged by MetaTrader 5. To enable reproducible offline research and fast intrabar replay, :class:`~q_backend.research.TickStore` keeps saved tick sessions on disk under ``data/tick_store/`` (configurable via ``Q_RESEARCH_TICK_STORE``).

Storage layout
--------------

Data is organized as partitioned zstandard-compressed Apache Parquet files:

.. code-block:: text

   data/tick_store/
   └── WDO_N/
       ├── 2026-09-01.parquet
       ├── 2026-09-02.parquet
       └── bars_M1/
           ├── 2026-09-01.parquet
           └── 2026-09-02.parquet

Single-pass streaming
---------------------

When syncing sessions with :func:`~q_backend.research.sync_ticks` or
:meth:`~q_backend.research.TickStore.sync`:

1. Existing sessions are checked locally. A fully stored interval needs no gateway.
2. Missing intervals stream forward through bounded tick pages. The first page
   triggers terminal synchronization; subsequent cursors follow actual ticks,
   skipping empty stretches without daily requests. Each response is used once.
3. A producer thread downloads while the caller writes incremental Parquet row
   groups. The queue holds at most two pages; producer and consumer can each hold
   one additional page. Native requests are serialized.
4. A daily file appears atomically only after a successful cursor passes its end.
   Repeated ticks, including equal timestamps, retain their original order.
5. Scanned days with no ticks are **empty** and are retried next time. Failed or
   interrupted downloads leave completed days intact and remove unfinished files.
   Remaining dates in the failed interval are **failed**, with reasons in
   ``report.errors``; independent missing intervals continue.

Sync skips sessions already on disk and never stores the current exchange day.
Pass ``overwrite=True`` (or ``q-sync-ticks --overwrite``) to re-download stored
sessions, for example after switching data sources. Replaced sessions drop their
cached M1 bars; a session the source reports as empty is removed.
``unsettled`` remains a compatibility field and is always empty. ``first_msc``,
``last_msc`` and ``ticks_received`` describe received data, including unfinished
days; they are not a global broker-retention guarantee. Empty native responses
with an error never certify missing history. Cold synchronization may still wait
inside MT5, but progress shows the active interval and received pages.

The gateway must support ``/v1/ticks/page``. After updating it, run
``./dev restart gateway`` from the workspace root. An older gateway raises an
update/restart error rather than reverting to the slow daily loop.

Python usage
------------

.. code-block:: python

   from q_backend.research import TickStore

   store = TickStore("WDO$N")

   # Sync history explicitly
   report = store.sync(start="2025-10-01")
   print(report)

   # Load offline bars with transparent M1 caching
   bars = store.bars("M10", start="2025-10-01")

   # Load offline ticks across multiple days
   ticks = store.ticks(start="2026-09-01", end="2026-09-05")

CLI sync
--------

Sync history directly from the terminal:

.. code-block:: bash

   uv run q-sync-ticks --symbol 'WDO$N' --start 2025-10-01

Use single quotes around symbols containing ``$`` in shell commands so the shell
does not interpret the suffix as an environment variable.

Check what is stored
--------------------

.. code-block:: python

   print(store.sessions())

Reads use stored sessions only. ``store.bars(..., sync=True)`` fetches missing
sessions first and therefore needs the gateway. The standalone
:func:`~q_backend.research.sync_ticks` helper performs the same sync operation.
The store is separate from the API tick cache and the stack's market-data catalog.
