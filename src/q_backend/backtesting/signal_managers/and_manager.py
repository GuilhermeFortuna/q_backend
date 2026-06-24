from __future__ import annotations

from q_backend.backtesting.signal_managers.base import SignalManager, Stance


class AndManager(SignalManager):
    id = "and"
    label = "All (AND)"
    description = (
        "Net LONG only when every non-flat instance is LONG (and at least one is non-flat); "
        "symmetric for SHORT; otherwise FLAT."
    )

    def combine(self, stances: list[Stance]) -> Stance:
        if not stances:
            return Stance.FLAT

        non_flat = [s for s in stances if s != Stance.FLAT]
        if not non_flat:
            return Stance.FLAT
        if all(s == Stance.LONG for s in non_flat):
            return Stance.LONG
        if all(s == Stance.SHORT for s in non_flat):
            return Stance.SHORT
        return Stance.FLAT
