==========
Tick Store
==========

Architecture
------------

Broker tick history is short, and old sessions are eventually purged by MetaTrader 5. To enable reproducible offline research and fast intrabar replay, :class:`~q_backend.research.TickStore` maintains an immutable on-disk tick store under ``data/tick_store/`` (configurable via ``Q_RESEARCH_TICK_STORE``).

Storage Layout
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

Two-Pass Verification
---------------------

When syncing sessions with :func:`~q_backend.research.sync_ticks` or ``TickStore.sync()``:
1. Every missing date is fetched twice sequentially from the gateway.
2. The session file is written only if both calls return identical tick counts.
3. If counts differ, the day is marked as **unsettled** and skipped until the next sync.
4. If both return zero, the day is recorded as **empty**.

Python Usage
------------

.. code-block:: python

   from q_backend.research import TickStore, sync_ticks

   store = TickStore("WDO$N")

   # Sync history explicitly
   report = sync_ticks("WDO$N", start="2025-10-01")
   print(report)

   # Load offline bars with transparent M1 caching
   bars = store.bars("M10", start="2025-10-01")

   # Load offline ticks across multiple days
   ticks = store.ticks(start="2026-09-01", end="2026-09-05")

CLI Sync
--------

Sync history directly from the terminal:

.. code-block:: bash

   uv run q-sync-ticks --symbol "WDO$N" --start 2025-10-01
