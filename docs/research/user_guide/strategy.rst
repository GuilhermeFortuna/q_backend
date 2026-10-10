====================
Strategy development
====================

The :class:`~q_backend.research.ResearchStrategy` abstract base class lets you express custom entry and exit rules as Python methods.
Only ``entry_strategy`` is required; the other hooks have default implementations.

Lifecycle hooks
---------------

A research strategy implements up to four hooks:

1. ``compute_indicators(self, frame: DataFrame) -> DataFrame``
   Called once per backtest on the full historical dataset. Adds indicator columns. Must not alter existing market columns or index.
2. ``entry_strategy(self, frame: DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None``
   Evaluated per closed bar. Returns :meth:`~q_backend.research.TradeOrder.buy`, :meth:`~q_backend.research.TradeOrder.sell`, or ``None``.
3. ``exit_strategy(self, frame: DataFrame, positions: tuple[ResearchPosition, ...] = (), *, phase: str = "bar") -> TradeOrder | None``
   Evaluated per closed bar before ``entry_strategy``. Returns :meth:`~q_backend.research.TradeOrder.close` or ``None``.
   The optional ``phase`` parameter enables intrabar exits; see :doc:`execution`.
4. ``chart_indicators(self) -> Sequence[ChartIndicator]``
   Declares which indicator columns to render on the Trade Chart in the research desktop terminal.

Position context
----------------

Both decision hooks optionally receive the ``positions`` parameter, containing an immutable tuple of open :class:`~q_backend.research.ResearchPosition` objects:

.. list-table::
   :header-rows: 1
   :widths: 25 25 50

   * - Field
     - Type
     - Description
   * - ``symbol``
     - ``str``
     - Traded instrument symbol.
   * - ``side``
     - ``Literal["long", "short"]``
     - Direction of the position.
   * - ``entry_time``
     - :class:`pandas.Timestamp`
     - Timezone-aware fill timestamp.
   * - ``entry_price``
     - ``float``
     - Filled entry price.
   * - ``quantity``
     - ``float``
     - Filled quantity from position sizing.

Example: close below the entry
------------------------------

This long-only example requests an exit when a completed candle closes below
the entry price. The exit fills at the next open; it is not an intrabar stop.

.. code-block:: python

   from q_backend.research import ResearchStrategy, TradeOrder

   class CloseBelowEntry(ResearchStrategy):
       def entry_strategy(self, frame, positions=()):
           if positions or len(frame) < 2:
               return None
           if frame["close"].iloc[-1] > frame["open"].iloc[-1]:
               return TradeOrder.buy()
           return None

       def exit_strategy(self, frame, positions=()):
           close = frame["close"].iloc[-1]
           for pos in positions:
               # Close long if price dips below entry
               if pos.side == "long" and close < pos.entry_price:
                   return TradeOrder.close()
           return None

Keep indicator calculations causal
----------------------------------

Although ``compute_indicators`` receives the full dataset, each row's indicator
must use only that row and earlier rows. Avoid negative shifts, centered rolling
windows, or fitting a model to the entire test period. Do not change the index
or existing market-data columns.

Show indicators on the chart
----------------------------

Declare computed columns with :class:`~q_backend.research.ChartIndicator`.
For the RSI strategy in the quickstart, add this method to ``RSIReversion``:

.. code-block:: python

   def chart_indicators(self):
       from q_backend.research import ChartIndicator

       return (ChartIndicator("rsi", pane="oscillator", label="RSI"),)

Use ``pane="price"`` for overlays such as moving averages and
``pane="oscillator"`` for separate indicator plots. Column names must match
those returned by ``compute_indicators``. See :doc:`publishing` to review them
in the desktop.
