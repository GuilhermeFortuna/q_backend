"""Focused checks for scripts/ci.sh service isolation (mocked docker/systemctl/uv/make)."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_SCRIPT = REPO_ROOT / "scripts" / "ci.sh"

DEV_DATABASE_URL = "postgresql+psycopg://q:q@localhost:5434/q"
DEV_REDIS_URL = "redis://localhost:6380/0"
HOSTED_DATABASE_URL = "postgresql+psycopg://postgres:password@localhost:5434/q_storage"
HOSTED_REDIS_URL = "redis://localhost:6380/0"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip())
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def mock_bin(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    docker_log = log_dir / "docker.log"
    systemctl_log = log_dir / "systemctl.log"
    uv_log = log_dir / "uv.log"

    _write_executable(
        bin_dir / "docker",
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        log="{docker_log}"
        state="{state_dir}"
        printf '%s\\n' "$*" >> "$log"
        if [[ "${{Q_CI_MOCK_DOCKER_UNAVAILABLE:-0}}" == "1" ]]; then
          echo "docker unavailable" >&2
          exit 1
        fi
        if [[ "$1" == "info" ]]; then
          exit 0
        fi
        if [[ "$1" != "compose" ]]; then
          echo "unexpected docker invocation: $*" >&2
          exit 1
        fi
        shift
        args=("$@")
        sub=""
        for ((i=0; i<${{#args[@]}}; i++)); do
          if [[ "${{args[i]}}" == "up" || "${{args[i]}}" == "down" || "${{args[i]}}" == "port" ]]; then
            sub="${{args[i]}}"
            break
          fi
        done
        case "$sub" in
          up)
            if [[ "${{Q_CI_MOCK_COMPOSE_UP_FAIL:-0}}" == "1" ]]; then
              echo "compose up failed" >&2
              exit 1
            fi
            echo "${{COMPOSE_PROJECT_NAME:-}}" >> "$state/projects"
            echo "$$" > "$state/up_pid"
            pg="${{Q_CI_MOCK_PG_PORT:-54321}}"
            redis="${{Q_CI_MOCK_REDIS_PORT:-54322}}"
            echo "$pg" > "$state/pg_port"
            echo "$redis" > "$state/redis_port"
            if [[ "${{Q_CI_MOCK_COMPOSE_UP_BLOCK:-0}}" == "1" ]]; then
              while true; do sleep 1; done
            fi
            exit 0
            ;;
          port)
            if [[ "${{Q_CI_MOCK_COMPOSE_PORT_FAIL:-0}}" == "1" ]]; then
              echo "compose port failed" >&2
              exit 1
            fi
            service=""
            for ((i=0; i<${{#args[@]}}; i++)); do
              if [[ "${{args[i]}}" == "postgres" ]]; then
                service="postgres"
              elif [[ "${{args[i]}}" == "redis" ]]; then
                service="redis"
              fi
            done
            if [[ "$service" == "postgres" ]]; then
              port="$(cat "$state/pg_port" 2>/dev/null || echo 54321)"
              echo "127.0.0.1:$port"
            elif [[ "$service" == "redis" ]]; then
              port="$(cat "$state/redis_port" 2>/dev/null || echo 54322)"
              echo "127.0.0.1:$port"
            else
              echo "unknown port query: $*" >&2
              exit 1
            fi
            ;;
          down)
            echo "${{COMPOSE_PROJECT_NAME:-}}" >> "$state/down"
            exit 0
            ;;
          *)
            echo "unexpected compose subcommand: $*" >&2
            exit 1
            ;;
        esac
        """,
    )

    _write_executable(
        bin_dir / "systemctl",
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        log="{systemctl_log}"
        printf '%s\\n' "$*" >> "$log"
        if [[ "$1" == "--user" && "$2" == "start" ]]; then
          echo "refusing user unit start: $*" >&2
          exit 1
        fi
        if [[ "$1" == "--user" && "$2" == "stop" ]]; then
          echo "refusing user unit stop: $*" >&2
          exit 1
        fi
        if [[ "$1" == "status" && "$2" == "ci-docker.slice" ]]; then
          if [[ "${{Q_CI_MOCK_SLICE:-0}}" == "1" ]]; then
            exit 0
          fi
          exit 1
        fi
        exit 0
        """,
    )

    _write_executable(
        bin_dir / "uv",
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        log="{uv_log}"
        printf '%s\\n' "$*" >> "$log"
        exit 0
        """,
    )

    _write_executable(
        bin_dir / "make",
        """\
        #!/usr/bin/env bash
        exit 0
        """,
    )

    class MockBin:
        pass

    fixture = MockBin()
    fixture.path = bin_dir
    fixture.docker_log = docker_log
    fixture.systemctl_log = systemctl_log
    fixture.uv_log = uv_log
    fixture.state_dir = state_dir
    return fixture


def _run_ci(
    mock_bin,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CI_RESOURCE_CONTROLLED"] = "1"
    env["Q_CI_PREFLIGHT_ONLY"] = "1"
    env["PATH"] = f"{mock_bin.path}{os.pathsep}{env.get('PATH', '')}"
    env.pop("GITHUB_ACTIONS", None)
    env.pop("Q_DATABASE_URL", None)
    env.pop("Q_REDIS_URL", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(CI_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _projects(state_dir: Path) -> list[str]:
    projects_file = state_dir / "projects"
    if not projects_file.exists():
        return []
    return [line for line in projects_file.read_text().splitlines() if line.strip()]


def _down_projects(state_dir: Path) -> list[str]:
    down_file = state_dir / "down"
    if not down_file.exists():
        return []
    return [line for line in down_file.read_text().splitlines() if line.strip()]


def test_local_run_uses_unique_compose_project_and_exports_urls(mock_bin) -> None:
    first = _run_ci(mock_bin, extra_env={"Q_CI_MOCK_PG_PORT": "55101", "Q_CI_MOCK_REDIS_PORT": "55102"})
    assert first.returncode == 0, first.stderr + first.stdout
    second = _run_ci(mock_bin, extra_env={"Q_CI_MOCK_PG_PORT": "55201", "Q_CI_MOCK_REDIS_PORT": "55202"})
    assert second.returncode == 0, second.stderr + second.stdout
    projects = _projects(mock_bin.state_dir)
    assert len(projects) >= 2
    assert projects[0] != projects[1]
    assert projects[0].startswith("q-backend-ci-")
    assert "postgresql+psycopg://postgres:password@127.0.0.1:55101/q_storage" in first.stdout
    assert "redis://127.0.0.1:55102/0" in first.stdout
    assert _down_projects(mock_bin.state_dir)
    systemctl_log = mock_bin.systemctl_log.read_text() if mock_bin.systemctl_log.exists() else ""
    assert "--user start" not in systemctl_log
    assert "q-dev" not in mock_bin.docker_log.read_text()


def test_ambient_ci_true_still_provisions_local_compose(mock_bin) -> None:
    result = _run_ci(mock_bin, extra_env={"CI": "true"})
    assert result.returncode == 0, result.stderr + result.stdout
    assert _projects(mock_bin.state_dir)
    assert "Hosted CI service endpoints verified" not in result.stdout


def test_github_actions_marker_without_run_metadata_still_uses_local_compose(mock_bin) -> None:
    result = _run_ci(mock_bin, extra_env={"CI": "true", "GITHUB_ACTIONS": "true"})
    assert result.returncode == 0, result.stderr + result.stdout
    assert _projects(mock_bin.state_dir)
    assert "Hosted CI service endpoints verified" not in result.stdout


def test_ambient_dev_urls_rejected(mock_bin) -> None:
    result = _run_ci(
        mock_bin,
        extra_env={
            "Q_DATABASE_URL": DEV_DATABASE_URL,
            "Q_REDIS_URL": DEV_REDIS_URL,
        },
    )
    assert result.returncode != 0
    assert not mock_bin.uv_log.exists() or "alembic" not in mock_bin.uv_log.read_text()


def test_compose_up_failure_skips_alembic(mock_bin) -> None:
    result = _run_ci(mock_bin, extra_env={"Q_CI_MOCK_COMPOSE_UP_FAIL": "1"})
    assert result.returncode != 0
    assert not mock_bin.uv_log.exists() or "alembic" not in mock_bin.uv_log.read_text()


def test_docker_unavailable_skips_alembic(mock_bin) -> None:
    result = _run_ci(mock_bin, extra_env={"Q_CI_MOCK_DOCKER_UNAVAILABLE": "1"})
    assert result.returncode != 0
    assert not mock_bin.uv_log.exists() or "alembic" not in mock_bin.uv_log.read_text()


def test_hosted_missing_services_fail_before_migration(mock_bin) -> None:
    env = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_WORKFLOW": "CI",
        "Q_DATABASE_URL": "postgresql+psycopg://postgres:password@127.0.0.1:9/q_storage",
        "Q_REDIS_URL": "redis://127.0.0.1:9/0",
    }
    result = _run_ci(mock_bin, extra_env=env)
    assert result.returncode != 0
    assert not mock_bin.uv_log.exists() or "alembic" not in mock_bin.uv_log.read_text()
    assert not _projects(mock_bin.state_dir)


def test_hosted_ci_accepts_only_job_service_urls(mock_bin) -> None:
    env = os.environ.copy()
    env.update(
        {
            "CI_RESOURCE_CONTROLLED": "1",
            "Q_CI_PREFLIGHT_ONLY": "1",
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_WORKFLOW": "CI",
            "Q_DATABASE_URL": HOSTED_DATABASE_URL,
            "Q_REDIS_URL": HOSTED_REDIS_URL,
            "PATH": f"{mock_bin.path}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source '{CI_SCRIPT}'; _ci_tcp_open() {{ return 0; }}; run_ci_pipeline",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30.0,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Hosted CI service endpoints verified" in result.stdout
    assert not _projects(mock_bin.state_dir)


def test_port_lookup_failure_cleans_up_started_compose_project(mock_bin) -> None:
    result = _run_ci(mock_bin, extra_env={"Q_CI_MOCK_COMPOSE_PORT_FAIL": "1"})
    assert result.returncode != 0
    projects = _projects(mock_bin.state_dir)
    assert projects
    assert _down_projects(mock_bin.state_dir) == projects


def test_compose_slice_override_selected_when_available(mock_bin) -> None:
    result = _run_ci(mock_bin, extra_env={"Q_CI_MOCK_SLICE": "1"})
    assert result.returncode == 0, result.stderr + result.stdout
    docker_log = mock_bin.docker_log.read_text()
    assert "docker-compose.ci-slice.yml" in docker_log


def test_signal_triggers_compose_down(mock_bin) -> None:
    env = os.environ.copy()
    env["CI_RESOURCE_CONTROLLED"] = "1"
    env["Q_CI_MOCK_COMPOSE_UP_BLOCK"] = "1"
    env["PATH"] = f"{mock_bin.path}{os.pathsep}{env.get('PATH', '')}"
    env.pop("GITHUB_ACTIONS", None)
    env.pop("Q_DATABASE_URL", None)
    env.pop("Q_REDIS_URL", None)
    proc = subprocess.Popen(
        ["bash", str(CI_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        start_new_session=True,
    )
    try:
        up_pid_file = mock_bin.state_dir / "up_pid"
        deadline = time.monotonic() + 10
        while not up_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert up_pid_file.exists(), "Compose startup did not begin"
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
    assert _down_projects(mock_bin.state_dir)


def test_ws_redis_url_uses_q_redis_db15(monkeypatch) -> None:
    monkeypatch.setenv("Q_REDIS_URL", "redis://127.0.0.1:60001/2")
    from tests.streaming.ws import conftest as ws_conftest

    assert ws_conftest.ws_test_redis_url() == "redis://127.0.0.1:60001/15"


def test_integration_guard_rejects_dev_defaults(monkeypatch) -> None:
    monkeypatch.delenv("Q_CI_ISOLATED", raising=False)
    monkeypatch.setenv("Q_DATABASE_URL", DEV_DATABASE_URL)
    monkeypatch.setenv("Q_REDIS_URL", DEV_REDIS_URL)
    from q_backend.storage.settings import get_settings

    get_settings.cache_clear()
    from tests.conftest import _require_isolated_integration_env

    with pytest.raises(pytest.fail.Exception, match="isolated"):
        _require_isolated_integration_env()
