from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

import pytest

from flowsight.sidecar import parent_runtime as runtime_module
from flowsight.sidecar import prepare_sidecar_runtime_config
from flowsight.sidecar import runtime_config as config_module
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

    def wait(self, *, timeout: float) -> None:
        self.wait_calls += 1
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
    assert observed == 10.0
    assert runtime_module._elect_once(actual_store, deadline, observed) is None
    assert calls == [(store, 4.9)]


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
