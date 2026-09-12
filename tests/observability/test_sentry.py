from __future__ import annotations

from unittest.mock import Mock

import pytest
import sentry_sdk

from q_backend.storage.settings import Settings


@pytest.fixture(autouse=True)
def reset_sentry_state():
    from q_backend.observability import sentry

    sentry._initialized = False
    sentry_sdk.init(dsn=None)
    yield
    sentry._initialized = False
    sentry_sdk.init(dsn=None)


def test_init_without_dsn_is_a_noop(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability.sentry import init_sentry

    client_before = sentry_sdk.get_client()
    sdk_init = Mock()
    monkeypatch.setattr(sentry_sdk, "init", sdk_init)

    assert init_sentry(Settings(sentry_dsn=""), component="api") is False
    sdk_init.assert_not_called()
    assert sentry_sdk.get_client() is client_before


def test_init_configures_sdk_and_static_tags(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability.sentry import init_sentry

    sdk_init = Mock()
    set_tag = Mock()
    monkeypatch.setattr(sentry_sdk, "init", sdk_init)
    monkeypatch.setattr(sentry_sdk, "set_tag", set_tag)

    settings = Settings(
        sentry_dsn="https://public@example.invalid/1",
        sentry_environment="test",
        sentry_traces_sample_rate=0.25,
        sentry_profiles_sample_rate=0.05,
    )

    assert init_sentry(settings, component="api") is True
    kwargs = sdk_init.call_args.kwargs
    assert kwargs["environment"] == "test"
    assert kwargs["send_default_pii"] is False
    assert kwargs["max_request_body_size"] == "never"
    assert kwargs["traces_sample_rate"] == 0.25
    assert kwargs["profiles_sample_rate"] == 0.05
    assert {type(item).__name__ for item in kwargs["integrations"]} == {
        "FastApiIntegration",
        "StarletteIntegration",
        "SqlalchemyIntegration",
    }
    set_tag.assert_any_call("component", "api")


def test_strategy_builder_request_fields_are_redacted():
    from q_backend.observability.sentry import _scrub_event

    event = {
        "request": {
            "url": "http://localhost/api/v1/strategy-builder/interpret",
            "data": {
                "prompt": "secret prompt",
                "conversation": [{"content": "secret turn"}],
                "description": "secret description",
                "metadata": {"message": "nested secret", "safe": "kept"},
                "symbol": "WIN$",
            },
        }
    }

    scrubbed = _scrub_event(event, {})

    assert scrubbed["request"]["data"] == {
        "prompt": "[redacted]",
        "conversation": "[redacted]",
        "description": "[redacted]",
        "metadata": {"message": "[redacted]", "safe": "kept"},
        "symbol": "WIN$",
    }


def test_init_rejects_unknown_component():
    from q_backend.observability.sentry import init_sentry

    with pytest.raises(ValueError, match="Unsupported Sentry component"):
        init_sentry(
            Settings(sentry_dsn="https://public@example.invalid/1"),
            component="scheduler",
        )


def test_trading_context_applies_allowed_tags(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability.sentry import trading_context

    scope = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=scope)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(sentry_sdk, "new_scope", Mock(return_value=context))

    with trading_context(symbol="WIN$", study_id="abc"):
        pass

    scope.set_tag.assert_any_call("symbol", "WIN$")
    scope.set_tag.assert_any_call("study_id", "abc")


def test_trading_context_rejects_unknown_tags():
    from q_backend.observability.sentry import trading_context

    with pytest.raises(ValueError, match="Unsupported trading context tag"):
        with trading_context(prompt="must not leave this machine"):
            pass


def test_init_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability.sentry import init_sentry

    sdk_init = Mock()
    monkeypatch.setattr(sentry_sdk, "init", sdk_init)
    settings = Settings(sentry_dsn="https://public@example.invalid/1")

    assert init_sentry(settings, component="api") is True
    assert init_sentry(settings, component="api") is True
    sdk_init.assert_called_once()


def test_git_sha_stamping(monkeypatch: pytest.MonkeyPatch):
    import subprocess
    from q_backend.observability.sentry import get_git_sha, resolve_release

    # 1. helper returns q@<sha> in a git checkout
    mock_run = Mock()
    mock_run.return_value = Mock(returncode=0, stdout="abc1234\n")
    monkeypatch.setattr(subprocess, "run", mock_run)

    assert get_git_sha() == "abc1234"

    # And resolve_release formats it
    monkeypatch.delenv("Q_RELEASE", raising=False)
    assert resolve_release(use_cache=False) == "q@abc1234"

    # 2. "unknown" when git is absent (mock subprocess failure)
    mock_run.side_effect = Exception("git not found")
    assert get_git_sha() == "unknown"
    assert resolve_release(use_cache=False) == "q@unknown"

    # 3. With Q_RELEASE env var set
    monkeypatch.setenv("Q_RELEASE", "xyz789")
    assert resolve_release(use_cache=False) == "q@xyz789"

    monkeypatch.setenv("Q_RELEASE", "q@xyz789")
    assert resolve_release(use_cache=False) == "q@xyz789"


def test_release_present_on_mock_transport_event(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability import sentry
    from q_backend.storage.settings import Settings

    events: list[dict[str, object]] = []
    original_init = sentry_sdk.init

    def init_with_transport(*args: object, **kwargs: object):
        kwargs["transport"] = events.append
        return original_init(*args, **kwargs)

    sentry._initialized = False
    monkeypatch.setattr(sentry_sdk, "init", init_with_transport)

    assert sentry.init_sentry(
        Settings(sentry_dsn="https://public@example.invalid/1"),
        component="api",
    )

    client = sentry_sdk.get_client()
    assert client is not None
    assert client.options["release"] == sentry.RELEASE

    sentry_sdk.capture_message("test message")
    client.flush()
    assert len(events) > 0
    assert events[0].get("release") == sentry.RELEASE


def test_cross_repo_release_format_alignment():
    # Frontend release format: "q@" + __GIT_SHA__
    # Backend release format: q@ + git_sha
    fixture_sha = "abc1234"
    frontend_release = f"q@{fixture_sha}"
    backend_release = f"q@{fixture_sha}"
    assert frontend_release == backend_release
