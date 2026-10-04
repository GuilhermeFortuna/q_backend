"""MA Crossover · ML Filter: the research-only variant gated by a Q-086 model.

The variant shares every entry parameter and all crossover math with the original
``MACrossover``. The ML gate is not part of the per-slot stance calculation: the
backtest path wraps the finished ``CompositeEntryStrategy`` with
``EntryFilteredStrategy`` (see ``factory.wrap_with_ml_filter``).
"""

from q_backend.backtesting.strategies.ma_crossover import _build_ma_crossover, ma_crossover_param_specs
from q_backend.backtesting.strategy import MACrossoverStrategy
from q_backend.backtesting.strategy_registry import register_strategy

ML_FILTER_STRATEGY_NAME = "MACrossoverMLFilter"

register_strategy(
    name=ML_FILTER_STRATEGY_NAME,
    label="MA Crossover · ML Filter",
    description=(
        "Moving-average crossover whose entries are gated by a saved ML classifier. "
        "Exits and sizing follow the original crossover; research backtests only."
    ),
    params=ma_crossover_param_specs(),
    build=_build_ma_crossover,
    strategy_class=MACrossoverStrategy,
    category="trend",
    thesis=(
        "A crossover is only a candidate: a classifier trained on earlier crossover "
        "outcomes keeps the entries it scores as likely to be profitable and leaves "
        "the rest flat, while every original exit still fires."
    ),
    strong_in="Ranges where a trained model recognizes which crossovers whipsaw.",
    weak_in="Regime shifts away from the training window, where scores go stale.",
    capabilities=["ml_entry_filter", "research_only"],
)
