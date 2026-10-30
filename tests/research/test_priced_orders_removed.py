"""Same-candle priced orders are no longer accepted."""

import pytest

from q_backend.research import TradeOrder


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_priced_market_entries_name_resting_replacement(action):
    with pytest.raises(ValueError, match="resting.*stop.*limit"):
        getattr(TradeOrder, action)(price=100)


def test_priced_close_names_replacements():
    with pytest.raises(ValueError, match="timed.*protective"):
        TradeOrder.close(price=100)


def test_resting_entries_still_accept_price():
    assert TradeOrder.buy_stop(price=100).price == 100
    assert TradeOrder.sell_limit(price=100).price == 100


@pytest.mark.parametrize("action", ["buy", "sell", "close"])
def test_priced_orders_in_lists_are_refused(action):
    with pytest.raises(ValueError, match="priced (entries|closes) are removed"):
        [getattr(TradeOrder, action)(price=100), getattr(TradeOrder, action)(price=101)]


@pytest.mark.parametrize("action", ["buy", "sell", "close"])
def test_direct_construction_cannot_bypass_refusal(action):
    with pytest.raises(ValueError, match="priced (entries|closes) are removed"):
        TradeOrder(action, price=100)
