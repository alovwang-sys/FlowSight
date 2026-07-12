from __future__ import annotations

import os
from typing import NoReturn

import pytest

from flowsight.sidecar import parent_runtime as runtime_module


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
    ) -> None:
        self.start_error = start
        self.alive_after_start = alive_after_start
        self.alive_after_join = alive_after_join
        self.started = False
        self.start_calls = 0
        self.join_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.started = self.alive_after_start
        if self.start_error is not None:
            raise self.start_error

    def join(self, timeout: float) -> None:
        self.join_calls += 1
        assert timeout == 0.25

    def is_alive(self) -> bool:
        if not self.started:
            return False
        return self.alive_after_join if self.join_calls else self.alive_after_start


def _writer() -> tuple[int, int]:
    return os.pipe()


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
    reaper = _Reaper(alive_after_join=True)
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
