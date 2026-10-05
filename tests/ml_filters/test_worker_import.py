"""Check worker import order in a fresh interpreter, before API startup."""

import subprocess
import sys


def test_ml_filter_service_import_does_not_start_api():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import q_backend.ml_filters.service; " "assert 'q_backend.api.main' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
