"""Import boundaries for Linux-side market-data clients."""

from __future__ import annotations

import subprocess
import sys


def test_remote_client_import_does_not_load_metatrader5() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import q_backend.market_data.clients.remote, sys; "
            "print('MetaTrader5' in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"
