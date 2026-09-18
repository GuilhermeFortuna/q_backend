from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import subprocess
import time
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "scripts/install-user-units.sh"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_install_script_idempotent(tmp_path: Path):
    dest = tmp_path / "units"
    quadlet_dest = tmp_path / "quadlet"

    pg_port = _find_free_port()
    redis_port = _find_free_port()
    env = os.environ.copy()
    env["Q_UNITS_PG_PORT"] = str(pg_port)
    env["Q_UNITS_REDIS_PORT"] = str(redis_port)

    # First installation run
    res = subprocess.run(
        [
            str(INSTALL_SCRIPT),
            "--dest",
            str(dest),
            "--quadlet-dest",
            str(quadlet_dest),
            "--no-sync",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Install failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    expected_units = [
        dest / "q-migrate.service",
        dest / "q-api.service",
        dest / "q-outbox-relay.service",
        dest / "q-market-publisher.service",
        dest / "q-research-worker.service",
        dest / "q-execution-worker.service",
        dest / "q-backend.target",
    ]
    expected_quadlets = [
        quadlet_dest / "q-postgres.container",
        quadlet_dest / "q-postgres-data.volume",
        quadlet_dest / "q-redis.container",
    ]
    all_files = expected_units + expected_quadlets
    for f in all_files:
        assert f.is_file(), f"Missing installed file: {f}"
        content = f.read_text()
        assert not re.findall(r"@[A-Z0-9_]+@", content), f"Unexpanded placeholders in {f}"

    # Record modification times
    mtimes_run1 = {f: f.stat().st_mtime_ns for f in all_files}

    # Small delay to ensure any timestamp change would be detectable
    time.sleep(0.1)

    # Second installation run
    res2 = subprocess.run(
        [
            str(INSTALL_SCRIPT),
            "--dest",
            str(dest),
            "--quadlet-dest",
            str(quadlet_dest),
            "--no-sync",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"Second install failed:\nSTDOUT:\n{res2.stdout}\nSTDERR:\n{res2.stderr}"

    mtimes_run2 = {f: f.stat().st_mtime_ns for f in all_files}
    assert mtimes_run1 == mtimes_run2, "Files were modified on second installation run"


def test_install_script_uninstall(tmp_path: Path):
    dest = tmp_path / "units"
    quadlet_dest = tmp_path / "quadlet"

    pg_port = _find_free_port()
    redis_port = _find_free_port()
    env = os.environ.copy()
    env["Q_UNITS_PG_PORT"] = str(pg_port)
    env["Q_UNITS_REDIS_PORT"] = str(redis_port)

    # Install
    subprocess.run(
        [
            str(INSTALL_SCRIPT),
            "--dest",
            str(dest),
            "--quadlet-dest",
            str(quadlet_dest),
            "--no-sync",
        ],
        env=env,
        check=True,
    )

    # Uninstall
    res = subprocess.run(
        [
            str(INSTALL_SCRIPT),
            "--dest",
            str(dest),
            "--quadlet-dest",
            str(quadlet_dest),
            "--no-sync",
            "--uninstall",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Uninstall failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    for f in [
        dest / "q-migrate.service",
        dest / "q-api.service",
        dest / "q-outbox-relay.service",
        dest / "q-market-publisher.service",
        dest / "q-research-worker.service",
        dest / "q-execution-worker.service",
        dest / "q-backend.target",
        quadlet_dest / "q-postgres.container",
        quadlet_dest / "q-postgres-data.volume",
        quadlet_dest / "q-redis.container",
    ]:
        assert not f.exists(), f"File was not removed by uninstall: {f}"


def test_install_script_refuses_when_port_held(tmp_path: Path):
    dest = tmp_path / "units"
    quadlet_dest = tmp_path / "quadlet"

    port = _find_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))
        s.listen(1)

        env = os.environ.copy()
        env["Q_UNITS_PG_PORT"] = str(port)

        res = subprocess.run(
            [
                str(INSTALL_SCRIPT),
                "--dest",
                str(dest),
                "--quadlet-dest",
                str(quadlet_dest),
                "--no-sync",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode != 0
        assert "docker compose" in res.stderr
