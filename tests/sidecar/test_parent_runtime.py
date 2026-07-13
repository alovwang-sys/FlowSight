from __future__ import annotations

import ast
import gc
import inspect
import os
import pickle
import signal
import socket
import threading
import time
import warnings
from copy import copy, deepcopy
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    StartupChannelError,
    StartupFailure,
    StartupFailureCode,
    open_startup_channel,
    prepare_sidecar_runtime_config,
    start_or_attach_sidecar,
)
from flowsight.sidecar import parent_runtime as runtime_module
from flowsight.sidecar import runtime_config as config_module
from flowsight.sidecar.health import probe_sidecar_health
from flowsight.sidecar.owner_lock import OwnerLock, OwnerLockError
from flowsight.sidecar.startup_channel import StartupReader
from flowsight.sidecar.state import SidecarState, StateStore

PARENT_ERROR = "sidecar parent startup failed"
CLEANUP_NOTE = "sidecar parent startup cleanup failed"


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
        with pytest.raises(TypeError):
            runtime_module._ChildLaunchPlan(first._argv, first._pass_fds, handoff)
        monkeypatch.setattr(
            runtime_module,
            "_POPEN",
            lambda *_args, **_kwargs: calls.append(1) or _SpawnedProcess(),
        )
        assert runtime_module._spawn_isolated_child(first) is not None
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
    owner_fd = owner.fileno()
    writer_fd = writer.fileno()
    handoff_reader = handoff._reader_fd
    handoff_writer = handoff._writer_fd
    try:
        assert runtime_module._retire_parent_child_handles(owner, writer, handoff) is True
        with pytest.raises(OwnerLockError):
            owner.fileno()
        with pytest.raises(StartupChannelError):
            writer.fileno()
        for descriptor in (owner_fd, writer_fd, handoff_reader):
            with pytest.raises(OSError):
                os.fstat(descriptor)
        assert os.fstat(handoff_writer)
    finally:
        reader.close()
        assert handoff.close_uncommitted() is True


def test_parent_handoff_pipe_cannot_be_copied_to_duplicate_launch_authority(
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
        with pytest.raises(TypeError):
            copy(handoff)
        with pytest.raises(TypeError):
            deepcopy(handoff)
        with pytest.raises(TypeError):
            pickle.dumps(handoff)
        plan = runtime_module._owner_child_command(config, owner, writer, handoff)
        assert plan is not None
        with pytest.raises(TypeError):
            copy(plan)
        with pytest.raises(TypeError):
            deepcopy(plan)
        with pytest.raises(TypeError):
            pickle.dumps(plan)
    finally:
        owner.close()
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_parent_handle_retirement_attempts_later_handles_after_ordinary_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    calls: list[str] = []
    real_writer_close = runtime_module._WRITER_CLOSE
    real_retire_reader = runtime_module._ParentHandoffPipe.retire_reader

    def fail_owner(_owner: object) -> NoReturn:
        calls.append("owner")
        raise RuntimeError("close")

    def close_writer(actual_writer: object) -> None:
        calls.append("writer")
        real_writer_close(actual_writer)  # type: ignore[arg-type]

    def retire_reader(actual_handoff: object) -> bool:
        calls.append("reader")
        return real_retire_reader(actual_handoff)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module, "_OWNER_CLOSE", fail_owner)
    monkeypatch.setattr(runtime_module, "_WRITER_CLOSE", close_writer)
    monkeypatch.setattr(runtime_module._ParentHandoffPipe, "retire_reader", retire_reader)
    try:
        assert runtime_module._retire_parent_child_handles(owner, writer, handoff) is False
        assert calls == ["owner", "writer", "reader"]
    finally:
        runtime_module.OwnerLock.close(owner)
        reader.close()
        assert handoff.close_uncommitted() is True


def test_parent_handle_retirement_preserves_first_control_after_later_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    control = _Control()
    calls: list[str] = []
    real_writer_close = runtime_module._WRITER_CLOSE
    real_retire_reader = runtime_module._ParentHandoffPipe.retire_reader

    def control_owner(_owner: object) -> NoReturn:
        calls.append("owner")
        raise control

    def close_writer(actual_writer: object) -> None:
        calls.append("writer")
        real_writer_close(actual_writer)  # type: ignore[arg-type]

    def retire_reader(actual_handoff: object) -> bool:
        calls.append("reader")
        return real_retire_reader(actual_handoff)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module, "_OWNER_CLOSE", control_owner)
    monkeypatch.setattr(runtime_module, "_WRITER_CLOSE", close_writer)
    monkeypatch.setattr(runtime_module._ParentHandoffPipe, "retire_reader", retire_reader)
    try:
        with pytest.raises(_Control) as captured:
            runtime_module._retire_parent_child_handles(owner, writer, handoff)
        assert captured.value is control
        assert calls == ["owner", "writer", "reader"]
    finally:
        runtime_module.OwnerLock.close(owner)
        reader.close()
        assert handoff.close_uncommitted() is True


def test_parent_handle_retirement_attempts_reader_after_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    calls: list[str] = []
    real_owner_close = runtime_module._OWNER_CLOSE
    real_retire_reader = runtime_module._ParentHandoffPipe.retire_reader

    def close_owner(actual_owner: object) -> None:
        calls.append("owner")
        real_owner_close(actual_owner)  # type: ignore[arg-type]

    def fail_writer(_writer: object) -> NoReturn:
        calls.append("writer")
        raise RuntimeError("close")

    def retire_reader(actual_handoff: object) -> bool:
        calls.append("reader")
        return real_retire_reader(actual_handoff)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module, "_OWNER_CLOSE", close_owner)
    monkeypatch.setattr(runtime_module, "_WRITER_CLOSE", fail_writer)
    monkeypatch.setattr(runtime_module._ParentHandoffPipe, "retire_reader", retire_reader)
    try:
        assert runtime_module._retire_parent_child_handles(owner, writer, handoff) is False
        assert calls == ["owner", "writer", "reader"]
    finally:
        writer.close()
        reader.close()
        assert handoff.close_uncommitted() is True


def test_parent_handle_retirement_never_replaces_first_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    owner = runtime_module.OwnerLock.acquire(store)
    reader, writer = open_startup_channel()
    handoff = runtime_module._open_parent_handoff()
    assert handoff is not None
    first = _Control()
    later = _Control()
    calls: list[str] = []
    real_retire_reader = runtime_module._ParentHandoffPipe.retire_reader

    def control_owner(_owner: object) -> NoReturn:
        calls.append("owner")
        raise first

    def control_writer(_writer: object) -> NoReturn:
        calls.append("writer")
        raise later

    def retire_reader(actual_handoff: object) -> bool:
        calls.append("reader")
        return real_retire_reader(actual_handoff)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module, "_OWNER_CLOSE", control_owner)
    monkeypatch.setattr(runtime_module, "_WRITER_CLOSE", control_writer)
    monkeypatch.setattr(runtime_module._ParentHandoffPipe, "retire_reader", retire_reader)
    try:
        with pytest.raises(_Control) as captured:
            runtime_module._retire_parent_child_handles(owner, writer, handoff)
        assert captured.value is first
        assert calls == ["owner", "writer", "reader"]
    finally:
        runtime_module.OwnerLock.close(owner)
        writer.close()
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


class _Clock:
    def __init__(self, start: float, step: float = 0.1) -> None:
        self._next = start
        self._step = step

    def __call__(self) -> float:
        value = self._next
        self._next += self._step
        return value


def _incumbent_state(config, tmp_path: Path) -> SidecarState:
    return SidecarState(
        project_id=config.project_id,
        startup_id="a" * 32,
        pid=4321,
        port=8123,
        token="x" * 32,
        database_path=str(tmp_path / "events.sqlite3"),
        started_at_ns=1,
    )


def _assert_fixed_parent_error(error, *, notes=()) -> None:
    assert type(error) is RuntimeError
    assert error.args == (PARENT_ERROR,)
    assert str(error) == PARENT_ERROR
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert tuple(getattr(error, "__notes__", ())) == tuple(notes)


def _owner_election_harness(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    election_calls: list[float] = []

    def election(actual_store, timeout):
        assert actual_store is store
        election_calls.append(timeout)
        return OwnerLock.acquire(store)

    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", _Clock(100.0))
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_a, **_k: store)
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", election)
    return config, store, election_calls


def _track_owner_resources(monkeypatch):
    tracked: dict[str, int] = {}
    real_open_channel = runtime_module._OPEN_STARTUP_CHANNEL
    real_open_handoff = runtime_module._open_parent_handoff

    def open_channel():
        reader, writer = real_open_channel()
        tracked["reader_fd"] = reader.fileno()
        tracked["writer_fd"] = writer.fileno()
        return reader, writer

    def open_handoff():
        handoff = real_open_handoff()
        if handoff is not None:
            tracked["handoff_reader_fd"] = handoff._reader_fd
            tracked["handoff_writer_fd"] = handoff._writer_fd
        return handoff

    monkeypatch.setattr(runtime_module, "_OPEN_STARTUP_CHANNEL", open_channel)
    monkeypatch.setattr(runtime_module, "_open_parent_handoff", open_handoff)
    return tracked


def _assert_fd_closed(descriptor: int) -> None:
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_start_or_attach_public_shape_is_exact() -> None:
    assert sidecar_package.start_or_attach_sidecar is runtime_module.start_or_attach_sidecar
    assert start_or_attach_sidecar is runtime_module.start_or_attach_sidecar
    assert sidecar_package.__all__.count("start_or_attach_sidecar") == 1
    signature = inspect.signature(start_or_attach_sidecar)
    assert tuple(signature.parameters) == ("config",)
    parameter = signature.parameters["config"]
    assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameter.default is inspect.Parameter.empty
    hints = get_type_hints(start_or_attach_sidecar)
    assert hints == {
        "config": config_module.SidecarRuntimeConfig,
        "return": SidecarState,
    }


def test_parent_runtime_composition_stays_inside_reviewed_imports() -> None:
    source_file = inspect.getsourcefile(runtime_module)
    assert source_file is not None
    tree = ast.parse(Path(source_file).read_text())
    relative: set[str] = set()
    absolute: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is not None
            if node.level:
                relative.add(node.module)
            else:
                absolute.add(node.module)
        elif isinstance(node, ast.Import):
            absolute.update(alias.name for alias in node.names)
    assert relative == {
        "child_bootstrap",
        "incumbent_port",
        "owner_lock",
        "runtime_config",
        "startup_admission",
        "startup_channel",
        "startup_wait",
        "state",
    }
    assert absolute == {
        "__future__",
        "collections.abc",
        "fcntl",
        "math",
        "os",
        "pathlib",
        "subprocess",
        "sys",
        "threading",
        "time",
        "typing",
    }


def test_wrong_config_type_is_rejected_before_any_work(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_a, **_k: calls.append(1))
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_a: calls.append(1))
    for wrong in (None, object(), "project", 4040):
        with pytest.raises(TypeError):
            start_or_attach_sidecar(wrong)
    assert calls == []


def test_malformed_exact_config_fails_closed_before_election(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = object.__new__(config_module.SidecarRuntimeConfig)
    object.__setattr__(forged, "project_root", "")
    object.__setattr__(forged, "runtime_root", "relative-runtime")
    object.__setattr__(forged, "project_id", "wrong-project")
    object.__setattr__(forged, "requested_port", None)
    object.__setattr__(forged, "startup_timeout", 5.0)
    calls: list[object] = []
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_a, **_k: calls.append(1))
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_a: calls.append(1))
    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(forged)
    _assert_fixed_parent_error(captured.value)
    assert calls == []


def test_incumbent_branch_admits_exactly_once_and_returns_exact_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    incumbent = _incumbent_state(config, tmp_path)
    election_calls: list[float] = []
    admit_calls: list[tuple[object, object]] = []
    launch_guards: list[object] = []
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", _Clock(100.0))
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_a, **_k: store)

    def election(actual_store, timeout):
        assert actual_store is store
        election_calls.append(timeout)
        return incumbent

    def admit(actual_config, actual_incumbent):
        admit_calls.append((actual_config, actual_incumbent))
        return actual_incumbent

    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", election)
    monkeypatch.setattr(runtime_module, "_ADMIT_INCUMBENT_PORT", admit)
    monkeypatch.setattr(runtime_module, "_OPEN_STARTUP_CHANNEL", lambda: launch_guards.append(1))
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: launch_guards.append(1))

    result = start_or_attach_sidecar(config)
    assert result is incumbent
    assert admit_calls == [(config, incumbent)]
    assert election_calls == [pytest.approx(4.8)]
    assert launch_guards == []


def test_incumbent_port_incompatibility_is_terminal_without_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    incumbent = _incumbent_state(config, tmp_path)
    admit_calls: list[object] = []
    launch_guards: list[object] = []
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", _Clock(100.0))
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_a, **_k: store)
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_a: incumbent)

    def admit(*_args: object) -> NoReturn:
        admit_calls.append(1)
        raise RuntimeError("configured incumbent port is incompatible")

    monkeypatch.setattr(runtime_module, "_ADMIT_INCUMBENT_PORT", admit)
    monkeypatch.setattr(runtime_module, "_OPEN_STARTUP_CHANNEL", lambda: launch_guards.append(1))
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: launch_guards.append(1))

    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert admit_calls == [1]
    assert launch_guards == []


def test_exhausted_budget_after_election_prevents_incumbent_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    incumbent = _incumbent_state(config, tmp_path)
    admit_calls: list[object] = []
    clocks = iter((100.0, 100.1, 100.2, 100.3, 106.0))
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", lambda: next(clocks))
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_a, **_k: store)
    monkeypatch.setattr(runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_a: incumbent)
    monkeypatch.setattr(runtime_module, "_ADMIT_INCUMBENT_PORT", lambda *_a: admit_calls.append(1))

    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert admit_calls == []


def test_owner_branch_launches_one_gated_child_and_returns_verified_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, election_calls = _owner_election_harness(monkeypatch, tmp_path)
    tracked = _track_owner_resources(monkeypatch)
    verified = _incumbent_state(config, tmp_path)
    events: list[str] = []
    process = _Process()
    popen_calls: list[tuple[object, dict[str, object]]] = []
    receive_calls: list[tuple[object, object, float]] = []
    reapers: list[_Reaper] = []

    def popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return process

    real_retire = runtime_module._retire_parent_child_handles

    def retire(owner, writer, handoff):
        events.append("retire")
        return real_retire(owner, writer, handoff)

    class _RecordingReaper(_Reaper):
        def start(self):
            events.append("reaper_start")
            return super().start()

        def begin_wait(self):
            events.append("begin_wait")
            return super().begin_wait()

    def new_reaper(actual_process):
        assert actual_process is process
        reaper = _RecordingReaper()
        reapers.append(reaper)
        return reaper

    def receive(actual_store, reader, timeout):
        events.append("receive")
        assert type(reader) is StartupReader
        receive_calls.append((actual_store, reader, timeout))
        return verified

    monkeypatch.setattr(runtime_module, "_POPEN", popen)
    monkeypatch.setattr(runtime_module, "_retire_parent_child_handles", retire)
    monkeypatch.setattr(runtime_module, "_NEW_WAIT_ONLY_REAPER", new_reaper)
    monkeypatch.setattr(runtime_module, "_RECEIVE_STARTUP_OUTCOME", receive)

    result = start_or_attach_sidecar(config)
    assert result is verified
    assert events == ["retire", "reaper_start", "begin_wait", "receive"]
    assert election_calls == [pytest.approx(4.8)]
    assert len(popen_calls) == 1
    argv, kwargs = popen_calls[0]
    assert argv[:4] == (
        runtime_module._CHILD_EXECUTABLE,
        "-I",
        "-m",
        "flowsight.sidecar.child_entry",
    )
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is runtime_module._DEVNULL
    assert kwargs["stdout"] is runtime_module._DEVNULL
    assert kwargs["stderr"] is runtime_module._DEVNULL
    assert len(kwargs["pass_fds"]) == 3
    assert receive_calls[0][0] is store
    assert receive_calls[0][2] == pytest.approx(3.95)
    assert process.terminate_calls == 0
    assert process.wait_calls == 0
    assert len(reapers) == 1
    assert reapers[0].wait_ownership is True
    assert reapers[0].join_calls == 0
    for key in ("reader_fd", "writer_fd", "handoff_reader_fd", "handoff_writer_fd"):
        _assert_fd_closed(tracked[key])
    successor = OwnerLock.acquire(store)
    successor.close()


def test_expired_reserved_budget_after_owner_election_prevents_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    launch_guards: list[object] = []
    clocks = iter((100.0, 100.1, 100.2, 100.3, 104.8))
    monkeypatch.setattr(runtime_module, "_READ_MONOTONIC", lambda: next(clocks))
    monkeypatch.setattr(runtime_module, "_CONSTRUCT_STORE", lambda *_a, **_k: store)
    monkeypatch.setattr(
        runtime_module, "_WAIT_FOR_OWNER_ELECTION", lambda *_a: OwnerLock.acquire(store)
    )
    monkeypatch.setattr(runtime_module, "_OPEN_STARTUP_CHANNEL", lambda: launch_guards.append(1))
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: launch_guards.append(1))

    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert launch_guards == []
    successor = OwnerLock.acquire(store)
    successor.close()


def test_failed_spawn_closes_owner_channel_and_handoff_without_a_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, election_calls = _owner_election_harness(monkeypatch, tmp_path)
    tracked = _track_owner_resources(monkeypatch)
    popen_calls: list[object] = []

    def popen(*_args: object, **_kwargs: object) -> NoReturn:
        popen_calls.append(1)
        raise RuntimeError("spawn")

    monkeypatch.setattr(runtime_module, "_POPEN", popen)
    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert popen_calls == [1]
    assert election_calls == [pytest.approx(4.8)]
    for key in ("reader_fd", "writer_fd", "handoff_reader_fd", "handoff_writer_fd"):
        _assert_fd_closed(tracked[key])
    successor = OwnerLock.acquire(store)
    successor.close()


def test_pre_commit_reaper_failure_terminates_the_unpublished_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, _election_calls = _owner_election_harness(monkeypatch, tmp_path)
    tracked = _track_owner_resources(monkeypatch)
    process = _Process()
    receive_guards: list[object] = []
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: process)
    monkeypatch.setattr(
        runtime_module,
        "_NEW_WAIT_ONLY_REAPER",
        lambda _process: _Reaper(start=RuntimeError("start"), alive_after_start=False),
    )
    monkeypatch.setattr(
        runtime_module,
        "_RECEIVE_STARTUP_OUTCOME",
        lambda *_a: receive_guards.append(1),
    )

    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert receive_guards == []
    for key in ("reader_fd", "writer_fd", "handoff_reader_fd", "handoff_writer_fd"):
        _assert_fd_closed(tracked[key])
    successor = OwnerLock.acquire(store)
    successor.close()


def test_pre_commit_cleanup_failure_is_visible_as_the_fixed_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _store, _election_calls = _owner_election_harness(monkeypatch, tmp_path)
    tracked = _track_owner_resources(monkeypatch)
    process = _Process(wait=RuntimeError("still alive"))
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: process)
    monkeypatch.setattr(
        runtime_module,
        "_NEW_WAIT_ONLY_REAPER",
        lambda _process: _Reaper(start=RuntimeError("start"), alive_after_start=False),
    )
    try:
        with pytest.raises(RuntimeError) as captured:
            start_or_attach_sidecar(config)
        _assert_fixed_parent_error(captured.value, notes=(CLEANUP_NOTE,))
        assert process.terminate_calls == 1
        assert process.wait_calls == 1
        assert os.fstat(tracked["handoff_writer_fd"])
    finally:
        os.close(tracked["handoff_writer_fd"])


def test_child_failure_message_is_fixed_error_without_post_commit_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _store, _election_calls = _owner_election_harness(monkeypatch, tmp_path)
    tracked = _track_owner_resources(monkeypatch)
    process = _Process()
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: process)
    monkeypatch.setattr(runtime_module, "_NEW_WAIT_ONLY_REAPER", lambda _process: _Reaper())
    monkeypatch.setattr(
        runtime_module,
        "_RECEIVE_STARTUP_OUTCOME",
        lambda *_a: StartupFailure(code=StartupFailureCode.SIDECAR_STARTUP_FAILED),
    )

    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert process.terminate_calls == 0
    assert process.wait_calls == 0
    for key in ("reader_fd", "writer_fd", "handoff_reader_fd", "handoff_writer_fd"):
        _assert_fd_closed(tracked[key])


def test_post_commit_admission_fault_never_terminates_the_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _store, _election_calls = _owner_election_harness(monkeypatch, tmp_path)
    tracked = _track_owner_resources(monkeypatch)
    process = _Process()
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: process)
    monkeypatch.setattr(runtime_module, "_NEW_WAIT_ONLY_REAPER", lambda _process: _Reaper())

    def receive(*_args: object) -> NoReturn:
        raise StartupChannelError.__new__(StartupChannelError)

    monkeypatch.setattr(runtime_module, "_RECEIVE_STARTUP_OUTCOME", receive)
    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert process.terminate_calls == 0
    assert process.wait_calls == 0
    for key in ("reader_fd", "writer_fd", "handoff_reader_fd", "handoff_writer_fd"):
        _assert_fd_closed(tracked[key])


def test_post_commit_control_preserves_identity_without_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _store, _election_calls = _owner_election_harness(monkeypatch, tmp_path)
    process = _Process()
    control = _Control()
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: process)
    monkeypatch.setattr(runtime_module, "_NEW_WAIT_ONLY_REAPER", lambda _process: _Reaper())

    def receive(*_args: object) -> NoReturn:
        raise control

    monkeypatch.setattr(runtime_module, "_RECEIVE_STARTUP_OUTCOME", receive)
    with pytest.raises(_Control) as captured:
        start_or_attach_sidecar(config)
    assert captured.value is control
    assert process.terminate_calls == 0
    assert process.wait_calls == 0


def test_pre_commit_control_preserves_identity_after_contained_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _store, _election_calls = _owner_election_harness(monkeypatch, tmp_path)
    process = _Process()
    control = _Control()
    monkeypatch.setattr(runtime_module, "_POPEN", lambda *_a, **_k: process)
    monkeypatch.setattr(
        runtime_module,
        "_NEW_WAIT_ONLY_REAPER",
        lambda _process: _Reaper(start=control, alive_after_start=False),
    )
    with pytest.raises(_Control) as captured:
        start_or_attach_sidecar(config)
    assert captured.value is control
    assert tuple(getattr(captured.value, "__notes__", ())) == ()
    assert process.terminate_calls == 1
    assert process.wait_calls == 1


def test_ordinary_failures_never_leak_configuration_scalars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _store, _election_calls = _owner_election_harness(monkeypatch, tmp_path)

    def popen(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError(f"raw failure {tmp_path} token=super-secret port=8123")

    monkeypatch.setattr(runtime_module, "_POPEN", popen)
    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    text = repr(captured.value) + repr(captured.value.args) + str(captured.value)
    assert str(tmp_path) not in text
    assert "token" not in text
    assert "8123" not in text
    assert "super-secret" not in text


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


def _isolated_real_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    requested_port: int | None = None,
    startup_timeout: float = 20.0,
):
    (tmp_path / "xdg-runtime").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    if not project.exists():
        project.mkdir()
    return prepare_sidecar_runtime_config(
        project,
        requested_port=requested_port,
        startup_timeout=startup_timeout,
    )


def _track_spawned(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    spawned: list[object] = []
    real_popen = runtime_module._POPEN

    def popen(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(process)
        return process

    monkeypatch.setattr(runtime_module, "_POPEN", popen)
    return spawned


def _wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _terminate_and_reap(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    def exited() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    if _wait_for(exited, 10.0):
        return
    os.kill(pid, signal.SIGKILL)
    assert _wait_for(exited, 10.0)


def test_real_start_or_attach_launches_gated_child_to_authenticated_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _isolated_real_config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    spawned = _track_spawned(monkeypatch)
    gate_observations: list[tuple[str, bool]] = []

    class _ObservedHandoff(runtime_module._ChildHandoff):
        def transfer_wait_ownership(self, reaper) -> None:
            gate_observations.append(("transfer", store.load() is None))
            super().transfer_wait_ownership(reaper)

        def release_gate(self) -> bool:
            gate_observations.append(("release", store.load() is None))
            return super().release_gate()

    monkeypatch.setattr(runtime_module, "_NEW_CHILD_HANDOFF", _ObservedHandoff)
    threads_before = set(threading.enumerate())
    state: SidecarState | None = None
    try:
        state = start_or_attach_sidecar(config)
        assert type(state) is SidecarState
        assert gate_observations == [("transfer", True), ("release", True)]
        assert len(spawned) == 1
        assert spawned[0].pid == state.pid
        assert probe_sidecar_health(state, 5.0) is True
        assert store.load() == state
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            gc.collect()
        reaper_threads = [
            thread for thread in set(threading.enumerate()) - threads_before if thread.daemon
        ]
        assert len(reaper_threads) == 1
        again = start_or_attach_sidecar(config)
        assert again == state
        assert len(spawned) == 1
    finally:
        if state is not None:
            _terminate_and_reap(state.pid)
    assert _wait_for(lambda: spawned[0].returncode is not None, 10.0)
    reaper_threads[0].join(10.0)
    assert reaper_threads[0].is_alive() is False


def test_real_concurrent_callers_share_one_incumbent_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _isolated_real_config(monkeypatch, tmp_path, startup_timeout=25.0)
    spawned = _track_spawned(monkeypatch)
    results: list[object] = [None, None]

    def call(index: int) -> None:
        try:
            results[index] = start_or_attach_sidecar(config)
        except BaseException as error:  # noqa: BLE001 - recorded for assertions
            results[index] = error

    callers = [threading.Thread(target=call, args=(index,)) for index in range(2)]
    try:
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(30.0)
        assert all(not caller.is_alive() for caller in callers)
        assert all(type(result) is SidecarState for result in results)
        first, second = results
        assert first == second
        assert len(spawned) == 1
        assert spawned[0].pid == first.pid
    finally:
        launched = spawned[0].pid if spawned else None
        if launched is not None:
            _terminate_and_reap(launched)


def test_real_explicit_port_mismatch_is_terminal_without_second_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _isolated_real_config(monkeypatch, tmp_path)
    spawned = _track_spawned(monkeypatch)
    state: SidecarState | None = None
    try:
        state = start_or_attach_sidecar(config)
        mismatched_port = state.port - 1 if state.port > 1 else state.port + 1
        mismatched = prepare_sidecar_runtime_config(
            tmp_path / "project",
            requested_port=mismatched_port,
            startup_timeout=20.0,
        )
        with pytest.raises(RuntimeError) as captured:
            start_or_attach_sidecar(mismatched)
        _assert_fixed_parent_error(captured.value)
        assert len(spawned) == 1
        assert probe_sidecar_health(state, 5.0) is True
    finally:
        if state is not None:
            _terminate_and_reap(state.pid)


def test_real_pre_ready_child_failure_is_synchronously_contained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    occupied_port = blocker.getsockname()[1]
    try:
        config = _isolated_real_config(
            monkeypatch,
            tmp_path,
            requested_port=occupied_port,
        )
        store = StateStore(config.runtime_root, project_id=config.project_id)
        spawned = _track_spawned(monkeypatch)
        with pytest.raises(RuntimeError) as captured:
            start_or_attach_sidecar(config)
        _assert_fixed_parent_error(captured.value)
        assert len(spawned) == 1
        assert _wait_for(lambda: spawned[0].returncode is not None, 10.0)
        assert store.load() is None
        successor = OwnerLock.acquire(store)
        successor.close()
    finally:
        blocker.close()


def test_real_post_commit_admission_failure_leaves_attachable_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _isolated_real_config(monkeypatch, tmp_path)
    spawned = _track_spawned(monkeypatch)
    real_receive = runtime_module._RECEIVE_STARTUP_OUTCOME

    def discarding_receive(actual_store, reader, timeout):
        real_receive(actual_store, reader, timeout)
        return None

    monkeypatch.setattr(runtime_module, "_RECEIVE_STARTUP_OUTCOME", discarding_receive)
    try:
        with pytest.raises(RuntimeError) as captured:
            start_or_attach_sidecar(config)
        _assert_fixed_parent_error(captured.value)
        assert len(spawned) == 1
        assert spawned[0].returncode is None
        monkeypatch.setattr(runtime_module, "_RECEIVE_STARTUP_OUTCOME", real_receive)
        state = start_or_attach_sidecar(config)
        assert type(state) is SidecarState
        assert state.pid == spawned[0].pid
        assert len(spawned) == 1
        assert probe_sidecar_health(state, 5.0) is True
    finally:
        launched = spawned[0].pid if spawned else None
        if launched is not None:
            _terminate_and_reap(launched)


def test_real_pre_commit_cleanup_leaves_no_child_or_descriptor_behind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _isolated_real_config(monkeypatch, tmp_path)
    store = StateStore(config.runtime_root, project_id=config.project_id)
    tracked = _track_owner_resources(monkeypatch)
    spawned = _track_spawned(monkeypatch)
    monkeypatch.setattr(
        runtime_module,
        "_NEW_WAIT_ONLY_REAPER",
        lambda _process: _Reaper(start=RuntimeError("start"), alive_after_start=False),
    )
    with pytest.raises(RuntimeError) as captured:
        start_or_attach_sidecar(config)
    _assert_fixed_parent_error(captured.value)
    assert len(spawned) == 1
    assert spawned[0].returncode is not None
    for key in ("reader_fd", "writer_fd", "handoff_reader_fd", "handoff_writer_fd"):
        _assert_fd_closed(tracked[key])
    assert store.load() is None
    successor = OwnerLock.acquire(store)
    successor.close()
