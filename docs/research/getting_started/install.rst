============
Installation
============

Q Research is part of ``q_backend``, imported as ``q_backend.research``.
Use the backend's managed Python environment rather than a separate package.

Set up Python
-------------

From the Q workspace root:

.. code-block:: bash

   cd q_backend
   uv sync
   uv run python

Run the Python examples in this interpreter, or save them in a script and run
``uv run python your_script.py`` from ``q_backend``.

Choose what you need to run
---------------------------

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Task
     - Required services
   * - Compute indicators or backtest an existing DataFrame
     - None
   * - Read sessions already saved in a TickStore
     - None
   * - Fetch bars or ticks, or sync a TickStore
     - MT5 gateway with a connected terminal
   * - Publish a result to the desktop
     - Research stack; see :doc:`../user_guide/publishing`

Connect the MT5 gateway
-----------------------

From the **Q workspace root**, launch the gateway and wait for connectivity:

.. code-block:: bash

   ./dev gateway

The default gateway address is ``http://127.0.0.1:18812``. To use another gateway,
set its address in the shell where you run your research script:

.. code-block:: bash

   export Q_MT5_GATEWAY_URL="http://127.0.0.1:18812"

Set ``Q_MT5_GATEWAY_TOKEN`` only if your gateway requires authentication. The
terminal must be connected to your broker and have the requested symbol's history.

Continue with the :doc:`quickstart`. For B3 futures, also read
:doc:`price_series` before interpreting point-based profit and loss.
