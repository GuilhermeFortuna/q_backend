"""Run lifecycle, command idempotency, revision checks, pause/step/speed and crash recovery."""

from __future__ import annotations

import uuid

import pytest

from q_backend.replay.config import to_market_us
from q_backend.replay.controller import ReplayController
from q_backend.replay.journal import ReplayRunStore

from .conftest import GENERATION, RUN_ID, START, FakeMono, make_config

BASE = to_market_us(START)
SEC = 1_000_000


def _ctrl(store: ReplayRunStore, mono: FakeMono, handler=None) -> ReplayController:
    if handler is None:
        return ReplayController(store, mono_ns=mono)
    return ReplayController(store, handler=handler, mono_ns=mono)


def _create(controller: ReplayController, **config_kwargs):
    config = make_config(**config_kwargs)
    status = controller.create(config, run_id=RUN_ID, generation=GENERATION, dataset_checksums=())
    return config, status


def _cmd(controller: ReplayController, action: str, *, revision: int, **params):
    return controller.command(command_id=str(uuid.uuid4()), expected_revision=revision, action=action, **params)


def test_create_starts_prepared_at_interval_start(store, mono) -> None:
    controller = _ctrl(store, mono)
    _, status = _create(controller)
    assert status.state == "prepared"
    assert status.revision == 0
    assert status.canonical_cursor_us == BASE
    assert status.queue_depth == 0


def test_second_run_on_same_simulator_is_refused(store, mono) -> None:
    from q_backend.replay.journal import RunAlreadyExistsError

    controller = _ctrl(store, mono)
    _create(controller)
    with pytest.raises(RunAlreadyExistsError):
        _create(controller)


def test_resume_then_tick_moves_market_with_real_time(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller, rate=1.0)
    result = _cmd(controller, "resume", revision=0)
    assert result.accepted and result.state == "running" and result.revision == 1
    mono.advance_ms(2_500)
    controller.tick()
    assert controller.status().canonical_cursor_us == BASE + 2_500_000


def test_pause_freezes_market_and_resume_continues_from_same_point(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    _cmd(controller, "resume", revision=0)
    mono.advance_ms(1_000)
    paused = _cmd(controller, "pause", revision=1)
    assert paused.state == "paused"
    frozen = controller.status().canonical_cursor_us
    assert frozen == BASE + SEC
    mono.advance_ms(30_000)
    assert controller.status().canonical_cursor_us == frozen
    _cmd(controller, "resume", revision=2)
    mono.advance_ms(500)
    controller.tick()
    assert controller.status().canonical_cursor_us == frozen + SEC // 2


def test_speed_change_preserves_current_market_time(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    _cmd(controller, "resume", revision=0)
    mono.advance_ms(1_000)
    speed = _cmd(controller, "speed", revision=1, rate=4.0)
    assert speed.accepted and speed.state == "running"
    assert controller.status().canonical_cursor_us == BASE + SEC  # no jump at the change
    mono.advance_ms(1_000)
    controller.tick()
    assert controller.status().canonical_cursor_us == BASE + SEC + 4 * SEC


def test_paused_step_advances_exact_market_time_and_processes_due_events(store, mono) -> None:
    seen: list[tuple[str, int]] = []
    controller = _ctrl(store, mono, handler=lambda e, c: seen.append((e.kind, e.at_us)))
    _create(controller)
    _cmd(controller, "pause", revision=0)  # prepared -> paused
    with store.transaction() as conn:
        controller._timeline.schedule(
            conn, at_us=BASE + 200_000, priority=1, kind="tick", payload={}, max_queued_events=100
        )
        controller._timeline.schedule(
            conn, at_us=BASE + 900_000, priority=1, kind="tick", payload={}, max_queued_events=100
        )
    step = _cmd(controller, "step", revision=1, market_ms=500)
    assert step.accepted and step.state == "paused"
    assert controller.status().canonical_cursor_us == BASE + 500_000
    assert seen == [("tick", BASE + 200_000)]


def test_step_while_running_is_refused_with_reason(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    _cmd(controller, "resume", revision=0)
    result = _cmd(controller, "step", revision=1, market_ms=100)
    assert not result.accepted and result.reason == "step_requires_pause"
    assert result.revision == 1 and result.state == "running"


def test_stale_revision_is_refused_and_leaves_state_unchanged(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    _cmd(controller, "resume", revision=0)
    before = controller.status()
    refused = _cmd(controller, "pause", revision=0)  # the client saw revision 0
    assert not refused.accepted and refused.reason == "stale_revision"
    after = controller.status()
    assert after.state == before.state == "running"
    assert after.revision == before.revision == 1


def test_repeated_command_id_returns_recorded_result_without_reapplying(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    command_id = str(uuid.uuid4())
    first = controller.command(command_id=command_id, expected_revision=0, action="resume")
    second = controller.command(command_id=command_id, expected_revision=0, action="resume")
    assert first == second
    assert controller.status().revision == 1


def test_invalid_speed_is_refused_as_invalid_rate(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    for rate in (0.0, 10.5, float("nan"), float("inf")):
        result = _cmd(controller, "speed", revision=0, rate=rate)
        assert not result.accepted and result.reason == "invalid_rate"


def test_parameters_must_match_the_action(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    with pytest.raises(ValueError):
        _cmd(controller, "speed", revision=0)
    with pytest.raises(ValueError):
        _cmd(controller, "step", revision=0)
    with pytest.raises(ValueError):
        _cmd(controller, "pause", revision=0, rate=1.0)


def test_run_completes_at_exclusive_end_and_refuses_further_commands(store, mono) -> None:
    seen: list[str] = []
    controller = _ctrl(store, mono, handler=lambda e, c: seen.append(e.kind))
    _create(controller, end_seconds=10)
    with store.transaction() as conn:
        controller._timeline.schedule(
            conn, at_us=BASE + 10 * SEC, priority=4, kind="at-end", payload={}, max_queued_events=100
        )
        controller._timeline.schedule(
            conn, at_us=BASE + 9 * SEC, priority=4, kind="inside", payload={}, max_queued_events=100
        )
    _cmd(controller, "resume", revision=0)
    mono.advance_ms(60_000)
    controller.tick()
    status = controller.status()
    assert status.state == "completed"
    assert status.canonical_cursor_us == BASE + 10 * SEC
    assert seen == ["inside"]
    late = _cmd(controller, "pause", revision=status.revision)
    assert not late.accepted and late.reason == "run_not_active"


def test_stop_cancels_unexecuted_work_and_journals_it(store, mono) -> None:
    controller = _ctrl(store, mono)
    _create(controller)
    with store.transaction() as conn:
        controller._timeline.schedule(
            conn, at_us=BASE + 30 * SEC, priority=4, kind="later", payload={}, max_queued_events=100
        )
    stopped = _cmd(controller, "stop", revision=0)
    assert stopped.accepted and stopped.state == "stopped"
    assert controller.status().queue_depth == 0
    payloads = [entry[4] for entry in controller.journal()]
    assert any('"cancelled_pending"' in payload for payload in payloads)


def test_handler_failure_rolls_back_the_advance_and_marks_run_failed(store, mono) -> None:
    calls: list[str] = []

    def handler(event, conn):
        calls.append(event.kind)
        if event.kind == "boom":
            raise RuntimeError("broker crashed mid-event")

    controller = _ctrl(store, mono, handler=handler)
    _create(controller)
    with store.transaction() as conn:
        controller._timeline.schedule(
            conn, at_us=BASE + 100_000, priority=1, kind="first", payload={}, max_queued_events=100
        )
        controller._timeline.schedule(
            conn, at_us=BASE + 200_000, priority=1, kind="boom", payload={}, max_queued_events=100
        )
    _cmd(controller, "resume", revision=0)
    mono.advance_ms(1_000)
    with pytest.raises(RuntimeError, match="broker crashed"):
        controller.tick()
    status = controller.status()
    assert status.state == "failed"
    assert status.last_error == "broker crashed mid-event"
    # Nothing from the failed advance is committed: both events stay pending, cursor unchanged.
    assert status.queue_depth == 2
    assert status.canonical_cursor_us == BASE


def test_restart_while_running_recovers_paused_without_jumping_forward(controller_factory, mono) -> None:
    controller = controller_factory()
    _create(controller)
    _cmd(controller, "resume", revision=0)
    mono.advance_ms(2_000)
    controller.tick()
    cursor = controller.status().canonical_cursor_us
    assert cursor == BASE + 2 * SEC

    mono.advance_ms(600_000)  # simulator down for ten minutes of real time
    restarted = controller_factory()
    status = restarted.status()
    assert status.state == "paused"
    assert status.canonical_cursor_us == cursor
    assert status.revision == 1
    payloads = [entry[4] for entry in restarted.journal()]
    assert any("recovered_paused" in payload for payload in payloads)


def test_committed_command_survives_restart(controller_factory, mono) -> None:
    controller = controller_factory()
    _create(controller)
    command_id = str(uuid.uuid4())
    controller.command(command_id=command_id, expected_revision=0, action="resume")
    restarted = controller_factory()
    replay = restarted.command(command_id=command_id, expected_revision=0, action="resume")
    assert replay.accepted and replay.revision == 1
    assert restarted.status().revision == 1
