"""API startup must not initialize CUDA even when Q_TORCH_DEVICE=cuda (Q-032)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_api_startup_does_not_probe_cuda(monkeypatch) -> None:
    monkeypatch.setenv("Q_TORCH_DEVICE", "cuda")

    def _boom(*_args, **_kwargs):
        raise AssertionError("API must not call torch.cuda during startup")

    monkeypatch.setattr("torch.cuda.is_available", _boom)
    monkeypatch.setattr("torch.cuda.device_count", _boom)

    # Import after patching so any eager CUDA use would fail.
    from q_backend.api.main import app

    with TestClient(app) as client:
        response = client.get("/api/v1/system/health")
        assert response.status_code in (200, 503)
