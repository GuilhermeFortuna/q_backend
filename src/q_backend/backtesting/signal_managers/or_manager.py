from __future__ import annotations

from q_backend.backtesting.signal_managers.base import SignalManager, Stance


class OrManager(SignalManager):
    id = "or"
    label = "Any (OR)"
    description = (
        "Net LONG if any instance is LONG and none is SHORT; "
        "net SHORT if any is SHORT and none is LONG; otherwise FLAT."
    )

    def combine(self, stances: list[Stance]) -> Stance:
        if not stances:
            return Stance.FLAT

        has_long = any(s == Stance.LONG for s in stances)
        has_short = any(s == Stance.SHORT for s in stances)

        if has_long and not has_short:
            return Stance.LONG
        if has_short and not has_long:
            return Stance.SHORT
        return Stance.FLAT
