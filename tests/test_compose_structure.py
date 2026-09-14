"""Compose structure tests for the containerized Research profile (Q-032)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"


def _render_config() -> dict:
    env = {
        **os.environ,
        "Q_BACKEND_IMAGE": "q-backend:dev",
        "Q_TORCH_DEVICE": "cuda",
        "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        "Q_WORKER_PROCESSES": "14",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "-f",
            str(COMPOSE_FILE),
            "--profile",
            "containerized",
            "config",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    return yaml.safe_load(result.stdout)


@pytest.mark.skipif(
    subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose unavailable",
)
def test_containerized_services_share_image_without_build() -> None:
    config = _render_config()
    services = config["services"]
    assert "backend" in services
    assert "worker" in services

    backend = services["backend"]
    worker = services["worker"]

    assert backend.get("image") == "q-backend:dev"
    assert worker.get("image") == "q-backend:dev"
    assert "build" not in backend
    assert "build" not in worker


@pytest.mark.skipif(
    subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose unavailable",
)
def test_worker_only_has_gpu_and_cuda_env() -> None:
    config = _render_config()
    backend = config["services"]["backend"]
    worker = config["services"]["worker"]

    backend_env = backend.get("environment") or {}
    worker_env = worker.get("environment") or {}
    if isinstance(backend_env, list):
        backend_env = dict(item.split("=", 1) for item in backend_env)
    if isinstance(worker_env, list):
        worker_env = dict(item.split("=", 1) for item in worker_env)

    assert "Q_TORCH_DEVICE" not in backend_env or backend_env.get("Q_TORCH_DEVICE") in (
        None,
        "",
        "cpu",
    )
    assert worker_env.get("Q_TORCH_DEVICE") == "cuda"
    assert worker_env.get("CUBLAS_WORKSPACE_CONFIG") == ":4096:8"

    # Compose may render deploy.resources.reservations.devices or gpus
    worker_gpus = worker.get("gpus")
    deploy = worker.get("deploy") or {}
    reservations = (deploy.get("resources") or {}).get("reservations") or {}
    devices = reservations.get("devices") or []
    has_gpu = False
    if worker_gpus in ("all", ["all"]):
        has_gpu = True
    elif isinstance(worker_gpus, list) and worker_gpus:
        # compose config normalizes `gpus: all` to [{'count': -1}]
        has_gpu = any((isinstance(item, dict) and item.get("count") == -1) or item == "all" for item in worker_gpus)
    elif any(d.get("capabilities") == ["gpu"] or "gpu" in (d.get("capabilities") or []) for d in devices):
        has_gpu = True
    assert has_gpu, f"worker missing GPU request: gpus={worker_gpus} deploy={deploy}"

    backend_gpus = backend.get("gpus")
    backend_deploy = backend.get("deploy") or {}
    assert not backend_gpus
    assert not (backend_deploy.get("resources") or {}).get("reservations", {}).get("devices")


@pytest.mark.skipif(
    subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose unavailable",
)
def test_q020_data_and_live_source_mounts_present() -> None:
    config = _render_config()
    for name in ("backend", "worker"):
        volumes = config["services"][name].get("volumes") or []
        rendered = []
        for vol in volumes:
            if isinstance(vol, str):
                rendered.append(vol)
            else:
                rendered.append(f"{vol.get('source')}:{vol.get('target')}:{vol.get('mode', 'rw')}")
        joined = "\n".join(rendered)
        assert "/data" in joined
        assert "/app/src" in joined
        assert "/app/contracts" in joined
        assert "/app/alembic" in joined
        assert "alembic.ini" in joined
