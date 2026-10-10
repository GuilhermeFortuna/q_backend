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

Two-pass verification
---------------------

When syncing sessions with :func:`~q_backend.research.sync_ticks` or
:meth:`~q_backend.research.TickStore.sync`:

1. Every missing date is fetched twice sequentially from the gateway.
2. The session file is written only if both calls return identical tick counts.
3. If counts differ, the day is marked as **unsettled** and skipped until the next sync.
4. If both return zero, the report marks the day as **empty**; no file is written.
   Empty sessions are retried on the next sync.
5. Gateway errors mark only that day as **failed**.

Sync skips sessions already on disk and never stores the current exchange day.
Matching counts are a settlement check; they do not prove that tick contents or
broker history are complete.

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
