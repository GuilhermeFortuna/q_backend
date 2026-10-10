============
Installation
============

Environment Setup
-----------------

The Q research library lives in ``q_backend.research``. It uses the Python environment of the ``q_backend`` workspace.

From the workspace root, sync the backend dependencies:

.. code-block:: bash

   cd q_backend
   uv sync

Gateway Configuration
---------------------

Q Research accesses live and historical market data from MetaTrader 5 via Q's lightweight MT5 Gateway.

1. Ensure MetaTrader 5 is connected to your broker.
2. Launch the gateway service (or run ``./dev gateway`` from the workspace root).
3. Set the gateway URL and optional token in your environment:

.. code-block:: bash

   export Q_MT5_GATEWAY_URL="http://127.0.0.1:18812"
   export Q_MT5_GATEWAY_TOKEN="your-optional-token"

The gateway can run locally under Wine or on a dedicated remote machine.

.. note::
   Pure quantitative analysis, indicator calculations, tick-store reads, and local backtests require neither PostgreSQL, Redis, nor a running API/worker. The MT5 gateway is only queried when requesting fresh data via :func:`~q_backend.research.load_bars` or synchronizing ticks via :class:`~q_backend.research.TickStore`.
