====================
Strategy Development
====================

The :class:`~q_backend.research.ResearchStrategy` abstract base class provides the execution lifecycle for custom strategies.

Lifecycle Hooks
---------------

A research strategy implements up to four hooks:

1. ``compute_indicators(self, frame: DataFrame) -> DataFrame``
   Called once per backtest on the full historical dataset. Adds indicator columns. Must not alter existing market columns or index.
2. ``entry_strategy(self, frame: DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None``
   Evaluated per closed bar. Returns :meth:`~q_backend.research.TradeOrder.buy`, :meth:`~q_backend.research.TradeOrder.sell`, or ``None``.
3. ``exit_strategy(self, frame: DataFrame, positions: tuple[ResearchPosition, ...] = (), *, phase: str = "bar") -> TradeOrder | None``
   Evaluated per closed bar before ``entry_strategy``. Returns :meth:`~q_backend.research.TradeOrder.close` or ``None``.
4. ``chart_indicators(self) -> Sequence[ChartIndicator]``
   Declares which indicator columns to render on the Trade Chart in the research desktop terminal.

Position Context
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

Example: Directional Stop Strategy
----------------------------------

.. code-block:: python

   from q_backend.research import ResearchStrategy, TradeOrder, ResearchPosition

   class DirectionalStop(ResearchStrategy):
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
