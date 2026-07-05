from __future__ import annotations

from unittest.mock import Mock

import pytest
import sentry_sdk
from dramatiq import Message

from q_backend.storage.settings import Settings


def _message(actor_name: str, *args: object, **kwargs: object) -> Message:
    return Message(
        queue_name="default",
        actor_name=actor_name,
        args=args,
        kwargs=kwargs,
        options={},
    )


def test_init_accepts_extra_integrations(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability import sentry

    sentry._initialized = False
    sdk_init = Mock()
    integration = Mock()
    monkeypatch.setattr(sentry_sdk, "init", sdk_init)
    monkeypatch.setattr(sentry_sdk, "set_tag", Mock())

    assert sentry.init_sentry(
        Settings(sentry_dsn="https://public@example.invalid/1"),
        component="worker",
        extra_integrations=[integration],
    )
    assert integration in sdk_init.call_args.kwargs["integrations"]


def test_disabled_middleware_does_not_push_scope(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability.worker import SentryTradingContextMiddleware

    new_scope = Mock()
    monkeypatch.setattr(sentry_sdk, "new_scope", new_scope)
    middleware = SentryTradingContextMiddleware(enabled=False, worker_id="worker-1")

    middleware.before_process_message(Mock(), _message("run_backtest", "bt-1", "{}"))

    new_scope.assert_not_called()


def test_worker_middleware_tags_actor_ids_and_attempt(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability.worker import SentryTradingContextMiddleware

    scope = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=scope)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(sentry_sdk, "new_scope", Mock(return_value=context))
    middleware = SentryTradingContextMiddleware(enabled=True, worker_id="worker-1")
    message = _message("run_optimization_trials", "study-1", "db-id", "{}", 4)
    message.options["retries"] = 0

    middleware.before_process_message(Mock(), message)
    middleware.after_process_message(Mock(), message, exception=RuntimeError("boom"))

    scope.set_tag.assert_any_call("actor", "run_optimization_trials")
    scope.set_tag.assert_any_call("study_id", "study-1")
    scope.set_tag.assert_any_call("worker_id", "worker-1")
    scope.set_tag.assert_any_call("attempt", 1)
    context.__exit__.assert_called_once()


def test_worker_middleware_handles_message_without_ids(monkeypatch: pytest.MonkeyPatch):
    from q_backend.observability.worker import SentryTradingContextMiddleware

    scope = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=scope)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(sentry_sdk, "new_scope", Mock(return_value=context))
    middleware = SentryTradingContextMiddleware(enabled=True, worker_id="worker-1")
    message = _message("ping", "hello")

    middleware.before_process_message(Mock(), message)
    middleware.after_skip_message(Mock(), message)

    scope.set_tag.assert_any_call("actor", "ping")
    context.__exit__.assert_called_once()


def test_final_actor_failure_event_has_worker_tags(monkeypatch: pytest.MonkeyPatch):
    from dramatiq.broker import MessageProxy
    from sentry_sdk.integrations.dramatiq import DramatiqIntegration, SentryMiddleware

    from q_backend.observability import sentry
    from q_backend.observability.worker import SentryTradingContextMiddleware

    events: list[dict[str, object]] = []
    original_init = sentry_sdk.init

    def init_with_transport(*args: object, **kwargs: object):
        kwargs["transport"] = events.append
        return original_init(*args, **kwargs)

    sentry._initialized = False
    monkeypatch.setattr(sentry_sdk, "init", init_with_transport)
    assert sentry.init_sentry(
        Settings(sentry_dsn="https://public@example.invalid/1"),
        component="worker",
        extra_integrations=[DramatiqIntegration()],
    )
    broker = Mock()
    broker.get_actor.return_value.options = {}
    message = MessageProxy(
        _message("run_optimization_trials", "study-1", "db-id", "{}", 4)
    )
    trading = SentryTradingContextMiddleware(enabled=True, worker_id="worker-1")
    integration = SentryMiddleware()

    trading.before_process_message(broker, message)
    integration.before_process_message(broker, message)
    integration.after_process_message(
        broker, message, exception=RuntimeError("actor failed")
    )
    trading.after_process_message(broker, message)

    assert len(events) == 1
    tags = events[0]["tags"]
    assert isinstance(tags, dict)
    assert tags["component"] == "worker"
    assert tags["actor"] == "run_optimization_trials"
    assert tags["study_id"] == "study-1"
    assert tags["worker_id"] == "worker-1"
    assert tags["attempt"] == 1


def test_worker_shutdown_flushes_sentry(monkeypatch: pytest.MonkeyPatch):
    from q_backend.tasks.broker import MarketDataMiddleware

    flush = Mock()
    monkeypatch.setattr(sentry_sdk, "flush", flush)

    MarketDataMiddleware().before_worker_shutdown(Mock(), Mock())

    flush.assert_called_once_with(timeout=2)


@pytest.mark.parametrize(
    ("module_name", "command"),
    [
        ("q_optimize", "q_optimize"),
        ("q_execution", "q_execution"),
        ("q_train_encoder", "q_train_encoder"),
        ("q_promote_encoder", "q_promote_encoder"),
    ],
)
def test_cli_initializes_and_tags_before_parsing(
    monkeypatch: pytest.MonkeyPatch, module_name: str, command: str
):
    module = __import__(f"q_backend.cli.{module_name}", fromlist=["main"])
    init = Mock(return_value=False)
    set_tag = Mock()
    monkeypatch.setattr(module, "init_sentry", init)
    monkeypatch.setattr(module.sentry_sdk, "set_tag", set_tag)

    with pytest.raises(SystemExit) as exc_info:
        module.main(["--help"])

    assert exc_info.value.code == 0
    init.assert_called_once()
    assert init.call_args.kwargs["component"] == "cli"
    set_tag.assert_called_once_with("cli_command", command)


def test_worker_launcher_marks_replacement_process(monkeypatch: pytest.MonkeyPatch):
    from q_backend.cli import worker

    settings = Settings(worker_processes=3)
    init = Mock(return_value=True)
    execv = Mock(side_effect=RuntimeError("stop before process replacement"))
    monkeypatch.setattr(worker, "get_settings", Mock(return_value=settings))
    monkeypatch.setattr(worker, "init_sentry", init)
    monkeypatch.setattr(worker.os, "execv", execv)

    with pytest.raises(RuntimeError, match="stop before process replacement"):
        worker.main()

    assert worker.os.environ["Q_DRAMATIQ_WORKER"] == "1"
    assert init.call_args.kwargs["component"] == "worker"
    assert "--processes" in execv.call_args.args[1]
    assert "3" in execv.call_args.args[1]
