import pytest

from q_backend.backtesting.signal_managers.base import Stance
from q_backend.backtesting.signal_managers.registry import (
    get_manager,
    list_signal_managers,
)


@pytest.fixture
def or_manager():
    return get_manager("or", {})


@pytest.fixture
def and_manager():
    return get_manager("and", {})


@pytest.fixture
def majority_manager():
    return get_manager("majority", {"vote_threshold": 2})


class TestOrManager:
    def test_long_and_flat(self, or_manager):
        assert or_manager.combine([Stance.LONG, Stance.FLAT]) == Stance.LONG

    def test_long_and_short_disagreement(self, or_manager):
        assert or_manager.combine([Stance.LONG, Stance.SHORT]) == Stance.FLAT

    def test_all_flat(self, or_manager):
        assert or_manager.combine([Stance.FLAT, Stance.FLAT]) == Stance.FLAT

    def test_short_and_flat(self, or_manager):
        assert or_manager.combine([Stance.SHORT, Stance.FLAT]) == Stance.SHORT

    def test_empty_stances(self, or_manager):
        assert or_manager.combine([]) == Stance.FLAT


class TestAndManager:
    def test_all_long(self, and_manager):
        assert and_manager.combine([Stance.LONG, Stance.LONG]) == Stance.LONG

    def test_long_with_flat_ignored(self, and_manager):
        assert and_manager.combine([Stance.LONG, Stance.FLAT]) == Stance.LONG

    def test_long_and_short_disagreement(self, and_manager):
        assert and_manager.combine([Stance.LONG, Stance.SHORT]) == Stance.FLAT

    def test_all_flat(self, and_manager):
        assert and_manager.combine([Stance.FLAT, Stance.FLAT]) == Stance.FLAT

    def test_empty_stances(self, and_manager):
        assert and_manager.combine([]) == Stance.FLAT


class TestMajorityManager:
    def test_two_longs_one_short(self, majority_manager):
        assert majority_manager.combine([Stance.LONG, Stance.LONG, Stance.SHORT]) == Stance.LONG

    def test_tie_below_threshold(self, majority_manager):
        assert majority_manager.combine([Stance.LONG, Stance.SHORT]) == Stance.FLAT

    def test_single_long_below_threshold(self, majority_manager):
        assert majority_manager.combine([Stance.LONG, Stance.FLAT, Stance.FLAT]) == Stance.FLAT

    def test_threshold_one(self):
        manager = get_manager("majority", {"vote_threshold": 1})
        assert manager.combine([Stance.LONG, Stance.FLAT]) == Stance.LONG

    def test_empty_stances(self, majority_manager):
        assert majority_manager.combine([]) == Stance.FLAT


class TestRegistry:
    def test_unknown_manager_raises(self):
        with pytest.raises(ValueError, match="Unknown signal manager"):
            get_manager("nope", {})

    def test_list_signal_managers(self):
        managers = list_signal_managers()
        assert len(managers) == 3
        ids = {m.id for m in managers}
        assert ids == {"or", "and", "majority"}

        majority = next(m for m in managers if m.id == "majority")
        assert majority.param_names == ["vote_threshold"]

        or_manager = next(m for m in managers if m.id == "or")
        assert or_manager.param_names == []
