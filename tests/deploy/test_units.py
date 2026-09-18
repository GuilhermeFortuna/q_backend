from __future__ import annotations

import configparser
import os
from pathlib import Path
import re
import shutil
import subprocess
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "deploy/systemd/user"
QUADLET_DIR = REPO_ROOT / "deploy/systemd/quadlet"

ARCHITECTURE_TABLE = {
    "q-migrate.service": {
        "Requires": {"q-postgres.service"},
        "Wants": set(),
        "After": {"q-postgres.service"},
    },
    "q-api.service": {
        "Requires": {"q-postgres.service", "q-migrate.service"},
        "Wants": {"q-redis.service"},
        "After": {"q-postgres.service", "q-migrate.service", "q-redis.service"},
    },
    "q-outbox-relay.service": {
        "Requires": {"q-postgres.service", "q-redis.service"},
        "Wants": set(),
        "After": {"q-postgres.service", "q-redis.service"},
    },
    "q-market-publisher.service": {
        "Requires": {"q-redis.service"},
        "Wants": {"mt5-gateway.service"},
        "After": {"q-redis.service", "mt5-gateway.service"},
    },
    "q-research-worker.service": {
        "Requires": {"q-postgres.service", "q-redis.service"},
        "Wants": set(),
        "After": {"q-postgres.service", "q-redis.service"},
    },
}

LONG_RUNNING_SERVICES = {
    "q-api.service",
    "q-outbox-relay.service",
    "q-market-publisher.service",
    "q-research-worker.service",
}


def _render_templates(backend_dir: str, dest: Path) -> dict[str, Path]:
    rendered = {}
    assert TEMPLATES_DIR.is_dir(), f"Missing templates directory: {TEMPLATES_DIR}"
    for template_file in TEMPLATES_DIR.glob("*.service.in"):
        unit_name = template_file.name.removesuffix(".in")
        content = template_file.read_text()
        content = content.replace("@Q_BACKEND_DIR@", backend_dir)
        target_path = dest / unit_name
        target_path.write_text(content)
        rendered[unit_name] = target_path
    return rendered


def test_rendered_templates_placeholders_and_execstart(tmp_path: Path):
    backend_dir = "/opt/q_backend"
    rendered = _render_templates(backend_dir, tmp_path)
    assert len(rendered) >= 5

    for unit_name, file_path in rendered.items():
        content = file_path.read_text()
        # No @...@ placeholders should remain
        unexpanded = re.findall(r"@[A-Z0-9_]+@", content)
        assert not unexpanded, f"Unexpanded placeholders in {unit_name}: {unexpanded}"

        config = configparser.ConfigParser(strict=False, interpolation=None)
        config.read_string(content)

        exec_start = config.get("Service", "ExecStart")
        assert exec_start.startswith(
            f"{backend_dir}/.venv/bin/"
        ), f"{unit_name}: ExecStart must start with {backend_dir}/.venv/bin/, got {exec_start}"
        assert "uv " not in exec_start, f"{unit_name}: ExecStart must not contain uv"
        assert "bash" not in exec_start, f"{unit_name}: ExecStart must not contain bash"


def test_long_running_units_supervision_attributes(tmp_path: Path):
    rendered = _render_templates("/opt/q_backend", tmp_path)

    for unit_name in LONG_RUNNING_SERVICES:
        assert unit_name in rendered
        config = configparser.ConfigParser(strict=False, interpolation=None)
        config.read_string(rendered[unit_name].read_text())

        assert config.get("Service", "Type") == "notify"
        assert config.get("Service", "Restart") == "on-failure"
        assert config.get("Service", "RestartMaxDelaySec") == "30"
        assert config.get("Service", "RestartPreventExitStatus") == "78"

    # Worker specifically requires NotifyAccess=all
    worker_cfg = configparser.ConfigParser(strict=False, interpolation=None)
    worker_cfg.read_string(rendered["q-research-worker.service"].read_text())
    assert worker_cfg.get("Service", "NotifyAccess") == "all"
    assert worker_cfg.get("Service", "KillMode") == "mixed"
    assert worker_cfg.get("Service", "TimeoutStopSec") == "60"


def test_dependencies_match_architecture_table(tmp_path: Path):
    rendered = _render_templates("/opt/q_backend", tmp_path)

    for unit_name, expected in ARCHITECTURE_TABLE.items():
        assert unit_name in rendered, f"Missing unit: {unit_name}"
        config = configparser.ConfigParser(strict=False, interpolation=None)
        config.read_string(rendered[unit_name].read_text())

        requires = set(config.get("Unit", "Requires", fallback="").split())
        wants = set(config.get("Unit", "Wants", fallback="").split())
        after = set(config.get("Unit", "After", fallback="").split())

        assert (
            requires == expected["Requires"]
        ), f"{unit_name} Requires mismatch: expected {expected['Requires']}, got {requires}"
        assert wants == expected["Wants"], f"{unit_name} Wants mismatch: expected {expected['Wants']}, got {wants}"
        assert after == expected["After"], f"{unit_name} After mismatch: expected {expected['After']}, got {after}"


GATEWAY_SYSTEMD_DIR = REPO_ROOT / "gateway/systemd"


def test_mt5_edge_unit_structure():
    unit_path = GATEWAY_SYSTEMD_DIR / "mt5-edge.service"
    env_path = GATEWAY_SYSTEMD_DIR / "mt5-edge.env.example"
    assert unit_path.is_file()
    assert env_path.is_file()

    config = configparser.ConfigParser(strict=False, interpolation=None)
    config.read_string(unit_path.read_text())

    after = set(config.get("Unit", "After", fallback="").split())
    binds_to = set(config.get("Unit", "BindsTo", fallback="").split())
    assert "mt5-terminal.service" in after
    assert binds_to == {"mt5-terminal.service"}

    env_text = env_path.read_text()
    assert "MT5_EDGE_HOST=127.0.0.1" in env_text
    assert "MT5_EDGE_PORT=18813" in env_text


def test_quadlet_and_env_files_exist():
    assert (QUADLET_DIR / "q-postgres.container").is_file()
    assert (QUADLET_DIR / "q-postgres-data.volume").is_file()
    assert (QUADLET_DIR / "q-redis.container").is_file()
    assert (REPO_ROOT / "deploy/systemd/user/q-backend.target").is_file()
    assert (REPO_ROOT / "deploy/systemd/backend.env.example").is_file()
    assert (REPO_ROOT / "deploy/systemd/postgres.env.example").is_file()


def test_systemd_analyze_verify(tmp_path: Path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not installed")

    # Render with the actual repo directory so executables exist in .venv/bin/
    rendered = _render_templates(str(REPO_ROOT), tmp_path)
    target_file = REPO_ROOT / "deploy/systemd/user/q-backend.target"
    shutil.copy(target_file, tmp_path / "q-backend.target")

    # Stubs for quadlet-generated units and gateway unit so systemd-analyze can resolve dependencies
    (tmp_path / "q-postgres.service").write_text("[Unit]\nDescription=Postgres\n[Service]\nExecStart=/bin/true\n")
    (tmp_path / "q-redis.service").write_text("[Unit]\nDescription=Redis\n[Service]\nExecStart=/bin/true\n")
    (tmp_path / "mt5-gateway.service").write_text("[Unit]\nDescription=Gateway\n[Service]\nExecStart=/bin/true\n")

    # Ensure %h/.config/q/backend.env exists for verification of EnvironmentFile
    user_config_dir = Path.home() / ".config/q"
    user_config_dir.mkdir(parents=True, exist_ok=True)
    env_file = user_config_dir / "backend.env"
    created_env = False
    if not env_file.exists():
        shutil.copy(REPO_ROOT / "deploy/systemd/backend.env.example", env_file)
        created_env = True

    try:
        service_files = [str(p) for p in rendered.values()]
        env = os.environ.copy()
        env["SYSTEMD_UNIT_PATH"] = f"{tmp_path}:"
        res = subprocess.run(
            ["systemd-analyze", "--user", "verify"] + service_files,
            env=env,
            capture_output=True,
            text=True,
        )
        assert (
            res.returncode == 0
        ), f"systemd-analyze --user verify failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    finally:
        if created_env and env_file.exists():
            env_file.unlink()
