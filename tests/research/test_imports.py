"""Import boundaries for the research library."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_fresh_import_does_not_start_services() -> None:
    script = textwrap.dedent("""
        import importlib
        importlib.import_module("q_backend.research")
        from q_backend.research import load_bars, load_ticks, resample_ticks
        print(load_bars.__name__, load_ticks.__name__, resample_ticks.__name__)
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "load_bars load_ticks resample_ticks"


def test_adjusted_series_warning_lazy_export() -> None:
    script = textwrap.dedent("""
        from q_backend.research import AdjustedSeriesWarning
        import warnings
        assert issubclass(AdjustedSeriesWarning, UserWarning)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            import q_backend.research as research
            assert research.AdjustedSeriesWarning is AdjustedSeriesWarning
        assert not caught
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_tick_store_import_is_inert() -> None:
    script = textwrap.dedent("""
        from pathlib import Path
        import q_backend.research as research
        assert "TickStore" in research.__all__
        root = Path("/tmp/q103-tick-store-inert-root")
        store = research.TickStore("WDO$N", root=root)
        assert store.symbol == "WDO$N"
        assert not root.exists()
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_chart_indicator_is_public_export() -> None:
    script = textwrap.dedent("""
        import q_backend.research as research
        assert "ChartIndicator" in research.__all__
        assert research.ChartIndicator.__name__ == "ChartIndicator"
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        check=False,
    )
    assert result.returncode == 0, result.stderr
