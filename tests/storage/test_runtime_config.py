"""runtime_config: `remote` data source + gateway URL/token resolution (WO184)."""

from __future__ import annotations

import pytest

from q_backend.storage import runtime_config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point the runtime config at a temp file and clear gateway env vars."""
    monkeypatch.setenv(
        "Q_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json")
    )
    monkeypatch.delenv("Q_MT5_GATEWAY_URL", raising=False)
    monkeypatch.delenv("Q_MT5_GATEWAY_TOKEN", raising=False)
    yield


def test_remote_is_a_valid_data_source():
    runtime_config.set_data_source("remote")
    assert runtime_config.get_data_source() == "remote"


def test_invalid_data_source_still_rejected():
    with pytest.raises(ValueError):
        runtime_config.set_data_source("bogus")


def test_gateway_url_getter_defaults_none():
    assert runtime_config.get_remote_gateway_url() is None


def test_gateway_url_from_config():
    runtime_config.set_remote_gateway_url("http://box:18812")
    assert runtime_config.get_remote_gateway_url() == "http://box:18812"


def test_gateway_url_env_takes_precedence(monkeypatch):
    runtime_config.set_remote_gateway_url("http://config-host:18812")
    monkeypatch.setenv("Q_MT5_GATEWAY_URL", "http://env-host:1234")
    assert runtime_config.get_remote_gateway_url() == "http://env-host:1234"


def test_gateway_url_blank_env_ignored(monkeypatch):
    runtime_config.set_remote_gateway_url("http://config-host:18812")
    monkeypatch.setenv("Q_MT5_GATEWAY_URL", "   ")
    assert runtime_config.get_remote_gateway_url() == "http://config-host:18812"


def test_gateway_url_clear():
    runtime_config.set_remote_gateway_url("http://box:18812")
    runtime_config.set_remote_gateway_url(None)
    assert runtime_config.get_remote_gateway_url() is None


def test_gateway_token_env_precedence(monkeypatch):
    runtime_config.set_remote_gateway_token("config-token")
    assert runtime_config.get_remote_gateway_token() == "config-token"
    monkeypatch.setenv("Q_MT5_GATEWAY_TOKEN", "env-token")
    assert runtime_config.get_remote_gateway_token() == "env-token"
