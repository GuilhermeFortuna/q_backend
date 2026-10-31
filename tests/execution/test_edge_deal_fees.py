"""Fee mapping from MT5 signed deal cash flows: fee = -(commission + fee)."""

from decimal import Decimal

import pytest

from q_backend.execution.brokers.edge import _deal_fee


@pytest.mark.parametrize(
    ("deal", "expected"),
    [
        ({"commission": -1.5}, Decimal("1.5")),  # a charge is a positive cost
        ({"commission": 2.0}, Decimal("-2.0")),  # a rebate is kept negative, never clamped
        ({"commission": -1.0, "fee": -0.25}, Decimal("1.25")),  # both fields, counted once each
        ({}, Decimal("0")),  # missing fields default to zero
        ({"commission": None, "fee": None}, Decimal("0")),
    ],
)
def test_fee_is_the_negated_signed_cash_flow(deal, expected) -> None:
    assert _deal_fee(deal) == expected
