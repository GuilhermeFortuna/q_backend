"""Tests for q_core integration and indicator kernel bridge."""

from __future__ import annotations

import importlib.metadata
import re


def test_q_core_installed():
    version = importlib.metadata.version("q-core")
    assert version
    import q_core

    rev = q_core.contracts_rev()
    assert isinstance(rev, str)
    assert len(rev) == 40
    assert bool(re.fullmatch(r"[0-9a-f]{40}", rev))
