"""Indicator catalog coverage tests (acceptance criterion 3)."""

from __future__ import annotations

from q_backend.market_data.indicator_catalog import (
    CATALOG_EXCLUDED_KERNELS,
    charted_kernels,
    published_q_core_kernels,
)


def test_every_kernel_is_charted_or_excluded():
    published = published_q_core_kernels()
    charted = charted_kernels()
    assert published == charted | CATALOG_EXCLUDED_KERNELS
    assert CATALOG_EXCLUDED_KERNELS == frozenset({"clip"})
