"""Import and construction boundaries for the research library."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_fresh_import_does_not_start_services() -> None:
    script = textwrap.dedent("""
        import importlib
        importlib.import_module("q_backend.research")
        from q_backend.research import Research
        Research(source="local", database_url="sqlite:///:memory:", market_data_root="/tmp/q-research-empty-root")
        print("ok")
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
