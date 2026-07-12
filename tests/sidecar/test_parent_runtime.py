from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import NoReturn

import pytest

from flowsight.sidecar import (
    StartupChannelError,
    open_startup_channel,
    prepare_sidecar_runtime_config,
)
from flowsight.sidecar import parent_runtime as runtime_module
from flowsight.sidecar import runtime_config as config_module
from flowsight.sidecar.owner_lock import OwnerLockError
from flowsight.sidecar.state import SidecarState, StateStore


class _Control(BaseException):
    pass


class _Process:
    def __init__(
        self, *, terminate: BaseException | None = None, wait: BaseException | None = None
    ) -> None:
        self.terminate_error = terminate
        self.wait_error = wait
        self.terminate_calls = 0
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def wait(self, *, timeout: float | None = None) -> None:
        self.wait_calls += 1
        if timeout is not None:
            assert timeout == 0.25
        if self.wait_error is not None:
            raise self.wait_error


class _Reaper:
    def __init__(
        self,
        *,
        start: BaseException | None = None,
        alive_after_start: bool = True,
        alive_after_join: bool = False,
        child_reaped_after_join: bool = True,
        start_result: object = None,
    ) -> None:
        self.start_error = start
        self.alive_after_start = alive_after_start
        self.alive_after_join = alive_after_join
        self.child_reaped_after_join = child_reaped_after_join
        self.start_result = start_result
        self.started = False
        self.start_calls = 0
        self.join_calls = 0

    def start(self) -> object:
        self.start_calls += 1
        self.started = self.alive_after_start
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def join(self, timeout: float) -> None:
        self.join_calls += 1
        assert timeout == 0.25

    def is_alive(self) -> bool:
        if not self.started:
            return False
        return self.alive_after_join if self.join_calls else self.alive_after_start

    def begin_wait(self) -> bool:
        return True

    @property
    def wait_ownership(self) -> bool:
        return self.started

    @property
    def child_reaped(self) -> bool:
        return self.join_calls > 0 and self.child_reaped_after_join


def _writer() -> tuple[int, int]:
    return os.pipe()


def _config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
    project = tmp_path / "project"
    project.mkdir()
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )
    return prepare_sidecar_runtime_config(project)


def test_start_failure_keeps_gate_held_and_parent_waits_once() -> None:
    reader, writer = _writer()
    process = _Process()
    handoff = runtime_module._ChildHandoff(process, writer)
    reaper = _Reaper(start=RuntimeError("start"), alive_after_start=False)
    try:
        with pytest.raises(RuntimeError):
            handoff.transfer_wait_ownership(reaper)
        assert handoff.transferred is False
        assert handoff.committed is False
        assert handoff.cleanup_before_commit() is True
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
        assert reaper.join_calls == 0
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is True


def test_transferred_failure_joins_reaper_without_second_process_wait() -> None:
    reader, writer = _writer()
    process = _Process()
    handoff = runtime_module._ChildHandoff(process, writer)
    reaper = _Reaper(alive_after_join=False)
    try:
        handoff.transfer_wait_ownership(reaper)
        assert handoff.cleanup_before_commit() is True
        assert process.terminate_calls == 1
        assert process.wait_calls == 0
        assert reaper.join_calls == 1
        assert handoff.committed is False
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is True


def test_real_reaper_cleanup_begins_its_wait_without_releasing_gate() -> None:
    reader, writer = _writer()
    process = _Process()
    reaper = runtime_module._WaitOnlyReaper(process)
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        handoff.transfer_wait_ownership(reaper)
        assert process.wait_calls == 0
        assert handoff.cleanup_before_commit() is True
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
        assert reaper.child_reaped is True
        assert handoff.committed is False
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is True


def test_reaper_retries_one_failed_wait_permission_after_gate_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FirstSetFails:
        def __init__(self) -> None:
            self.event = threading.Event()
            self.set_calls = 0

        def set(self) -> None:
            self.set_calls += 1
            if self.set_calls == 1:
                raise RuntimeError("set")
            self.event.set()

        def wait(self) -> bool:
            return self.event.wait(1.0)

    events: list[_FirstSetFails] = []

    def new_event() -> _FirstSetFails:
        event = _FirstSetFails()
        events.append(event)
        return event

    reader, writer = _writer()
    process = _Process()
    monkeypatch.setattr(runtime_module, "_EVENT", new_event)
    monkeypatch.setattr(runtime_module, "_EVENT_SET", lambda event: event.set())
    reaper = runtime_module._WaitOnlyReaper(process)
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        handoff.transfer_wait_ownership(reaper)
        assert handoff.release_gate() is True
        reaper.join(1.0)
        assert events[0].set_calls == 2
        assert process.wait_calls == 1
        assert reaper.child_reaped is True
    finally:
        os.close(reader)


def test_non_none_close_result_still_releases_the_reaper_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = _writer()
    process = _Process()
    reaper = runtime_module._WaitOnlyReaper(process)
    handoff = runtime_module._ChildHandoff(process, writer)

    def close_then_report_non_none(descriptor: int) -> object:
        os.close(descriptor)
        return object()

    monkeypatch.setattr(runtime_module, "_CLOSE", close_then_report_non_none)
    try:
        handoff.transfer_wait_ownership(reaper)
        assert handoff.release_gate() is False
        assert handoff.committed is True
        reaper.join(1.0)
        assert process.wait_calls == 1
        assert reaper.child_reaped is True
    finally:
        os.close(reader)


def test_unconfirmed_cleanup_never_releases_gate() -> None:
    reader, writer = _writer()
    process = _Process(wait=RuntimeError("still alive"))
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        assert handoff.cleanup_before_commit() is False
        assert handoff.committed is False
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is False
        os.close(writer)


def test_cleanup_attempt_permanently_rejects_later_gate_release() -> None:
    reader, writer = _writer()
    process = _Process(wait=RuntimeError("still alive"))
    handoff = runtime_module._ChildHandoff(process, writer)
    reaper = _Reaper(alive_after_join=True, child_reaped_after_join=False)
    try:
        handoff.transfer_wait_ownership(reaper)
        assert handoff.cleanup_before_commit() is False
        assert handoff.release_gate() is False
        assert handoff.committed is False
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is False
        os.close(writer)


def test_malformed_started_reaper_can_only_join_and_never_release_gate() -> None:
    reader, writer = _writer()
    process = _Process()
    handoff = runtime_module._ChildHandoff(process, writer)
    reaper = _Reaper(start_result=object())
    try:
        with pytest.raises(RuntimeError):
            handoff.transfer_wait_ownership(reaper)
        assert handoff.transferred is True
        assert handoff.release_gate() is False
        assert handoff.cleanup_before_commit() is True
        assert process.wait_calls == 0
        assert reaper.join_calls == 1
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is True


def test_completed_reaper_without_a_successful_wait_cannot_retire_gate() -> None:
    reader, writer = _writer()
    process = _Process()
    handoff = runtime_module._ChildHandoff(process, writer)
    reaper = _Reaper(alive_after_join=False, child_reaped_after_join=False)
    try:
        handoff.transfer_wait_ownership(reaper)
        assert handoff.cleanup_before_commit() is False
        assert reaper.join_calls == 1
        assert handoff.retire_writer_after_reaped_cleanup() is False
        assert os.fstat(writer)
    finally:
        os.close(reader)
        os.close(writer)


def test_close_attempt_is_irreversible_even_when_close_reports_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = _writer()
    process = _Process()
    handoff = runtime_module._ChildHandoff(process, writer)
    reaper = _Reaper()
    control = _Control()

    def close_once(descriptor: int) -> NoReturn:
        assert descriptor == writer
        os.close(descriptor)
        raise control

    monkeypatch.setattr(runtime_module, "_CLOSE", close_once)
    try:
        handoff.transfer_wait_ownership(reaper)
        with pytest.raises(_Control) as captured:
            handoff.release_gate()
        assert captured.value is control
        assert handoff.committed is True
        assert handoff.retire_writer_after_reaped_cleanup() is False
        with pytest.raises(OSError):
            os.fstat(writer)
    finally:
        os.close(reader)


def test_post_commit_cleanup_cannot_terminate_or_wait() -> None:
    reader, writer = _writer()
    process = _Process()
    handoff = runtime_module._ChildHandoff(process, writer)
    reaper = _Reaper()
    try:
        handoff.transfer_wait_ownership(reaper)
        assert handoff.release_gate() is True
        assert handoff.cleanup_before_commit() is False
        assert process.terminate_calls == 0
        assert process.wait_calls == 0
        assert reaper.join_calls == 0
    finally:
        os.close(reader)


def test_cleanup_is_one_shot_after_reaped_child() -> None:
    reader, writer = _writer()
    process = _Process()
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        assert handoff.cleanup_before_commit() is True
        assert handoff.cleanup_before_commit() is False
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is True


def test_pre_commit_cleanup_preserves_terminate_control_after_reap() -> None:
    reader, writer = _writer()
    control = _Control()
    process = _Process(terminate=control)
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        with pytest.raises(_Control) as captured:
            handoff.cleanup_before_commit()
        assert captured.value is control
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is True


def test_pre_commit_cleanup_preserves_wait_control_without_releasing_gate() -> None:
    reader, writer = _writer()
    control = _Control()
    process = _Process(wait=control)
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        with pytest.raises(_Control) as captured:
            handoff.cleanup_before_commit()
        assert captured.value is control
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is False
        os.close(writer)


def test_wait_control_outranks_an_ordinary_terminate_failure() -> None:
    reader, writer = _writer()
    control = _Control()
    process = _Process(terminate=RuntimeError("ordinary"), wait=control)
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        with pytest.raises(_Control) as captured:
            handoff.cleanup_before_commit()
        assert captured.value is control
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is False
        os.close(writer)


def test_prepare_and_election_use_one_fresh_bounded_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    clocks = iter((10.0, 10.1, 10.2, 10.3))
    calls: list[tuple[StateStore, float]] = []
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", lambda: next(clocks))
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_args, **_kwargs: store)

    def election(actual_store: StateStore, timeout: float) -> object:
        calls.append((actual_store, timeout))
        return object()

    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", election)
    prepared = runtime_module._prepare_election(config)
    assert prepared is not None
    _config_value, actual_store, deadline, observed = prepared
    assert deadline == 15.0
    assert observed == 10.1
    assert runtime_module._elect_once(actual_store, deadline, observed) is None
    assert calls[0][0] is store
    assert calls[0][1] == pytest.approx(4.8)


def test_incumbent_requires_fresh_deadline_before_port_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    incumbent = SidecarState(
        project_id=config.project_id,
        startup_id="a" * 32,
        pid=123,
        port=8123,
        token="x" * 32,
        database_path=str(tmp_path / "events.sqlite3"),
        started_at_ns=1,
    )
    calls: list[object] = []
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", lambda: 5.0)
    monkeypatch.setattr(runtime_module, "_ADMIT_INCUMBENT_PORT", lambda *_args: calls.append(1))

    assert runtime_module._admit_incumbent(config, incumbent, 5.0, 5.0) is None
    assert calls == []


def test_owner_child_command_is_exact_isolated_child_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    try:
        plan = runtime_module._owner_child_command(config, owner, writer, handoff)
        assert plan is not None
        assert runtime_module._owner_child_command(config, owner, writer, handoff) is None
        command = (plan._argv, plan._pass_fds)
        argv, pass_fds = command
        assert argv[:4] == (
            runtime_module._CHILD_EXECUTABLE,
            "-I",
            "-m",
            "flowsight.sidecar.child_entry",
        )
        handoff_reader = handoff._reader_fd
        assert handoff_reader is not None
        assert pass_fds == (owner.fileno(), writer.fileno(), handoff_reader)
        assert argv[4:] == runtime_module._ENCODE_CHILD_BOOTSTRAP(
            config,
            owner_lock_fd=owner.fileno(),
            startup_writer_fd=writer.fileno(),
            parent_handoff_fd=handoff_reader,
        )
    finally:
        owner.close()
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_owner_child_command_rejects_an_invalid_handoff_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    try:
        handoff_writer = handoff.take_writer()
        assert handoff_writer is not None
        os.close(handoff_writer)
        assert runtime_module._owner_child_command(config, owner, writer, handoff) is None
    finally:
        owner.close()
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_spawn_isolated_child_uses_only_the_reviewed_subprocess_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _SpawnedProcess:
        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> None:
            del timeout

    process = _SpawnedProcess()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def popen(*args: object, **kwargs: object) -> _SpawnedProcess:
        calls.append((args, kwargs))
        return process

    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    try:
        plan = runtime_module._owner_child_command(config, owner, writer, handoff)
        assert plan is not None
        command = (plan._argv, plan._pass_fds)
        monkeypatch.setattr(runtime_module, "_POPEN", popen)
        assert runtime_module._spawn_isolated_child(plan) is process
        assert runtime_module._spawn_isolated_child(plan) is None
        assert calls == [
            (
                (command[0],),
                {
                    "stdin": runtime_module._DEVNULL,
                    "stdout": runtime_module._DEVNULL,
                    "stderr": runtime_module._DEVNULL,
                    "close_fds": True,
                    "pass_fds": command[1],
                    "start_new_session": True,
                    "shell": False,
                },
            )
        ]
    finally:
        owner.close()
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_failed_spawn_attempt_cannot_retry_the_same_launch_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    calls: list[object] = []
    try:
        plan = runtime_module._owner_child_command(config, owner, writer, handoff)
        assert plan is not None

        def fail_once(*_args: object, **_kwargs: object) -> NoReturn:
            calls.append(1)
            raise RuntimeError("spawn")

        monkeypatch.setattr(runtime_module, "_POPEN", fail_once)
        assert runtime_module._spawn_isolated_child(plan) is None
        monkeypatch.setattr(runtime_module, "_POPEN", lambda *_args, **_kwargs: calls.append(2))
        assert runtime_module._spawn_isolated_child(plan) is None
        assert calls == [1]
    finally:
        owner.close()
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_reconstructed_launch_plan_cannot_duplicate_handoff_spawn_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _SpawnedProcess:
        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> None:
            del timeout

    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    calls: list[object] = []
    try:
        first = runtime_module._owner_child_command(config, owner, writer, handoff)
        assert first is not None
        copied = runtime_module._ChildLaunchPlan(first._argv, first._pass_fds, handoff)
        monkeypatch.setattr(
            runtime_module,
            "_POPEN",
            lambda *_args, **_kwargs: calls.append(1) or _SpawnedProcess(),
        )
        assert runtime_module._spawn_isolated_child(first) is not None
        assert runtime_module._spawn_isolated_child(copied) is None
        assert calls == [1]
    finally:
        owner.close()
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_spawn_isolated_child_rejects_an_unreviewed_command_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def popen(*_args: object, **_kwargs: object) -> object:
        calls.append(1)
        return object()

    monkeypatch.setattr(runtime_module, "_POPEN", popen)
    assert runtime_module._spawn_isolated_child(object()) is None  # type: ignore[arg-type]
    assert calls == []


def test_spawn_rechecks_that_the_parent_still_holds_the_handoff_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    calls: list[object] = []
    try:
        plan = runtime_module._owner_child_command(config, owner, writer, handoff)
        assert plan is not None
        handoff_writer = handoff.take_writer()
        assert handoff_writer is not None
        os.close(handoff_writer)
        monkeypatch.setattr(runtime_module, "_POPEN", lambda *_args, **_kwargs: calls.append(1))
        assert runtime_module._spawn_isolated_child(plan) is None
        assert calls == []
    finally:
        owner.close()
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_malformed_parent_handoff_result_closes_the_known_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = os.pipe()
    monkeypatch.setattr(runtime_module, "_PIPE", lambda: (reader, object()))
    try:
        assert runtime_module._open_parent_handoff() is None
        with pytest.raises(OSError):
            os.fstat(reader)
    finally:
        os.close(writer)


def test_parent_handoff_promotes_low_descriptors_before_exposing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(runtime_module, "_PIPE", lambda: (0, 1))
    monkeypatch.setattr(runtime_module, "_FCNTL", lambda descriptor, *_args: descriptor + 3)
    monkeypatch.setattr(runtime_module, "_CLOSE", lambda descriptor: closed.append(descriptor))

    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    assert handoff._reader_fd == 3
    assert handoff.take_writer() == 4
    assert handoff.close_uncommitted() is True
    assert closed == [0, 1, 3]


def test_parent_retires_child_owned_handles_before_reaper_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    handoff_reader = handoff._reader_fd
    try:
        assert runtime_module._retire_parent_child_handles(owner, writer, handoff) is True
        with pytest.raises(OwnerLockError):
            owner.fileno()
        with pytest.raises(StartupChannelError):
            writer.fileno()
        with pytest.raises(OSError):
            os.fstat(handoff_reader)
    finally:
        reader.close()
        assert handoff.close_uncommitted() is True


def test_preflight_rejects_forged_exact_config_before_constructing_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = object.__new__(config_module.SidecarRuntimeConfig)
    object.__setattr__(forged, "project_root", "")
    object.__setattr__(forged, "runtime_root", "relative-runtime")
    object.__setattr__(forged, "project_id", "wrong-project")
    object.__setattr__(forged, "requested_port", None)
    object.__setattr__(forged, "startup_timeout", 5.0)
    calls: list[object] = []
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_args: calls.append(1))

    assert runtime_module._prepare_election(forged) is None
    assert calls == []


def test_preflight_rejects_exact_store_for_another_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    wrong_store = StateStore(config.runtime_root, project_id="project-v1-" + "0" * 64)
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_args, **_kwargs: wrong_store)

    assert runtime_module._prepare_election(config) is None


def test_expired_final_election_check_retires_owner_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    clocks = iter((1.0, 6.0))
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", lambda: next(clocks))
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_args: owner)

    assert runtime_module._elect_once(store, 5.0, 0.0) is None
    with pytest.raises(OwnerLockError):
        owner.fileno()


def test_final_election_control_preserves_identity_after_retiring_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    control = _Control()
    clocks = iter((1.0, control))

    def observe() -> float:
        value = next(clocks)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", observe)
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_args: owner)

    with pytest.raises(_Control) as captured:
        runtime_module._elect_once(store, 5.0, 0.0)
    assert captured.value is control
    with pytest.raises(OwnerLockError):
        owner.fileno()


def test_owner_close_failure_is_not_silently_lost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    clocks = iter((1.0, 6.0))
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", lambda: next(clocks))
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_args: owner)
    monkeypatch.setattr(
        runtime_module,
        "_OWNER_CLOSE",
        lambda _owner: (_ for _ in ()).throw(RuntimeError("close")),
    )

    with pytest.raises(runtime_module._OwnerCleanupFailure):
        runtime_module._elect_once(store, 5.0, 0.0)
    runtime_module.OwnerLock.close(owner)


def test_owner_cleanup_control_cannot_replace_primary_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    primary = _Control()
    cleanup = _Control()
    clocks = iter((1.0, primary))

    def observe() -> float:
        value = next(clocks)
        if isinstance(value, BaseException):
            raise value
        return value

    def fail_close(_owner: object) -> NoReturn:
        raise cleanup

    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", observe)
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_args: owner)
    monkeypatch.setattr(runtime_module, "_OWNER_CLOSE", fail_close)

    with pytest.raises(_Control) as captured:
        runtime_module._elect_once(store, 5.0, 0.0)
    assert captured.value is primary
    assert primary.__notes__ == ["sidecar parent startup cleanup failed"]
    runtime_module.OwnerLock.close(owner)


def test_preflight_canonical_rederive_must_leave_a_fresh_outer_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    calls: list[object] = []
    clocks = iter((10.0, 15.0))
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", lambda: next(clocks))
    monkeypatch.setattr(
        runtime_module,
        "_PREPARE_RUNTIME_CONFIG",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(
        runtime_module,
        "_CONSTRUCT_STORE",
        lambda *_args, **_kwargs: calls.append(1),
    )

    assert runtime_module._prepare_election(config) is None
    assert calls == []


def test_wait_only_reaper_claims_one_wait_before_handoff_release() -> None:
    class _BlockingProcess:
        def __init__(self) -> None:
            self.wait_entered = threading.Event()
            self.allow_exit = threading.Event()
            self.wait_calls = 0

        def terminate(self) -> None:
            raise AssertionError("successful handoff must not terminate the child")

        def wait(self, *, timeout: float | None = None) -> None:
            assert timeout is None
            self.wait_calls += 1
            self.wait_entered.set()
            assert self.allow_exit.wait(1.0)

    reader, writer = _writer()
    process = _BlockingProcess()
    reaper = runtime_module._WaitOnlyReaper(process)
    handoff = runtime_module._ChildHandoff(process, writer)
    try:
        handoff.transfer_wait_ownership(reaper)
        assert reaper.wait_ownership is True
        assert process.wait_entered.is_set() is False
        assert handoff.release_gate() is True
        assert process.wait_entered.wait(1.0)
        process.allow_exit.set()
        reaper.join(1.0)
        assert process.wait_calls == 1
        assert reaper.child_reaped is True
        assert reaper.is_alive() is False
    finally:
        os.close(reader)


def test_wait_only_reaper_never_claims_a_failed_wait_as_reaped() -> None:
    class _FailingProcess:
        def terminate(self) -> None:
            raise AssertionError("not used")

        def wait(self, *, timeout: float | None = None) -> None:
            assert timeout is None
            raise _Control()

    reaper = runtime_module._WaitOnlyReaper(_FailingProcess())
    reaper.start()
    assert reaper.begin_wait() is True
    reaper.join(1.0)
    assert reaper.wait_ownership is True
    assert reaper.child_reaped is False


def test_finished_reaper_cannot_release_the_child_gate() -> None:
    class _FinishedReaper:
        def start(self) -> None:
            pass

        def join(self, _timeout: float) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def begin_wait(self) -> bool:
            raise AssertionError("finished reaper must never receive wait permission")

        @property
        def wait_ownership(self) -> bool:
            return True

        @property
        def child_reaped(self) -> bool:
            return False

    reader, writer = _writer()
    handoff = runtime_module._ChildHandoff(_Process(), writer)
    reaper = _FinishedReaper()
    try:
        handoff.transfer_wait_ownership(reaper)
        assert handoff.release_gate() is False
        assert os.fstat(writer)
    finally:
        os.close(reader)
        os.close(writer)


def test_control_during_reaper_start_conservatively_forbids_parent_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InterruptedThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> NoReturn:
            raise _Control()

        def join(self, _timeout: float) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    reader, writer = _writer()
    process = _Process()
    reaper = runtime_module._WaitOnlyReaper(process)
    handoff = runtime_module._ChildHandoff(process, writer)
    monkeypatch.setattr(runtime_module, "_THREAD", _InterruptedThread)
    monkeypatch.setattr(runtime_module, "_THREAD_START", lambda thread: thread.start())
    try:
        with pytest.raises(_Control):
            handoff.transfer_wait_ownership(reaper)
        assert handoff.transferred is True
        assert handoff.cleanup_before_commit() is False
        assert process.terminate_calls == 1
        assert process.wait_calls == 0
        assert os.fstat(writer)
    finally:
        os.close(reader)
        os.close(writer)


def test_ordinary_reaper_start_failure_leaves_parent_as_wait_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailedThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> NoReturn:
            raise RuntimeError("start")

    reader, writer = _writer()
    process = _Process()
    reaper = runtime_module._WaitOnlyReaper(process)
    handoff = runtime_module._ChildHandoff(process, writer)
    monkeypatch.setattr(runtime_module, "_THREAD", _FailedThread)
    monkeypatch.setattr(runtime_module, "_THREAD_START", lambda thread: thread.start())
    try:
        with pytest.raises(RuntimeError):
            handoff.transfer_wait_ownership(reaper)
        assert handoff.transferred is False
        assert handoff.cleanup_before_commit() is True
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
        assert os.fstat(writer)
    finally:
        os.close(reader)
        assert handoff.retire_writer_after_reaped_cleanup() is True
