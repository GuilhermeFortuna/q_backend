from __future__ import annotations

from q_backend.backtesting.signal_managers.base import SignalManager, Stance
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class MajorityManager(SignalManager):
    id = "majority"
    label = "Majority vote"
    description = (
        "Net LONG when LONG count meets vote_threshold and exceeds SHORT count; "
        "symmetric for SHORT; ties or insufficient votes yield FLAT."
    )

    def __init__(self, vote_threshold: int = 2) -> None:
        self._vote_threshold = vote_threshold

    @property
    def vote_threshold(self) -> int:
        return self._vote_threshold

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="vote_threshold",
                label="Vote Threshold",
                type="int",
                default=2,
                min=1,
                step=1,
                hint="Minimum LONG or SHORT votes required for a net stance.",
            ),
        ]

    def combine(self, stances: list[Stance]) -> Stance:
        if not stances:
            return Stance.FLAT

        long_count = sum(1 for s in stances if s == Stance.LONG)
        short_count = sum(1 for s in stances if s == Stance.SHORT)

        if long_count >= self._vote_threshold and long_count > short_count:
            return Stance.LONG
        if short_count >= self._vote_threshold and short_count > long_count:
            return Stance.SHORT
        return Stance.FLAT
