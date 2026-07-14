from __future__ import annotations

import ast
import fcntl
import inspect
import os
import select
import subprocess
import sys
import traceback
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    SidecarChildBootstrap,
    decode_sidecar_child_bootstrap,
    encode_sidecar_child_bootstrap,
    prepare_sidecar_runtime_config,
)
from flowsight.sidecar import parent_handoff as handoff_module
from flowsight.sidecar import runtime_config as config_module

HANDOFF_ERROR = "sidecar child parent handoff failed"
_ISOLATED_CHILD_TIMEOUT = 30.0

_CHILD_SOURCE = r"""
import os
import sys
from pathlib import Path

from flowsight.sidecar import decode_sidecar_child_bootstrap
from flowsight.sidecar import runtime_config as config_module
from flowsight.sidecar.parent_handoff import await_parent_handoff

runtime_root = Path(sys.argv[1])
ready_fd = int(sys.argv[2])
arguments = tuple(sys.argv[3:])
config_module._USER_RUNTIME_PATH = lambda *_args, **_kwargs: runtime_root
bootstrap = decode_sidecar_child_bootstrap(arguments)
os.write(ready_fd, b"E")
await_parent_handoff(bootstrap)
os.write(ready_fd, b"R")
"""


class _Control(BaseException):
    pass


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    return project


def _bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handoff_fd: int,
    *,
    startup_timeout: float = 5.0,
) -> SidecarChildBootstrap:
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )
    config = prepare_sidecar_runtime_config(
        _project(tmp_path),
        startup_timeout=startup_timeout,
    )
    return decode_sidecar_child_bootstrap(
        encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=101,
            startup_writer_fd=102,
            parent_handoff_fd=handoff_fd,
        )
    )


def _assert_fixed(error: BaseException) -> None:
    assert type(error) is RuntimeError
    assert str(error) == HANDOFF_ERROR
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", []) == []


def _is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return True
    return False


def test_private_surface_and_signature_are_exact() -> None:
    assert not hasattr(sidecar_package, "await_parent_handoff")
    assert inspect.signature(handoff_module.await_parent_handoff).parameters.keys() == {"bootstrap"}
    assert get_type_hints(handoff_module.await_parent_handoff) == {
        "bootstrap": SidecarChildBootstrap,
        "return": type(None),
    }


def test_eof_release_consumes_the_exact_read_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)
    os.close(writer)

    handoff_module.await_parent_handoff(bootstrap)

    assert _is_closed(reader)


def test_forged_bootstrap_fails_before_property_or_descriptor_access() -> None:
    reader, writer = os.pipe()
    accesses: list[str] = []

    class ForgedBootstrap:
        @property
        def config(self) -> NoReturn:
            accesses.append("config")
            raise _Control("forged config")

        @property
        def parent_handoff_fd(self) -> NoReturn:
            accesses.append("parent_handoff_fd")
            raise _Control("forged descriptor")

    try:
        with pytest.raises(RuntimeError) as captured:
            handoff_module.await_parent_handoff(ForgedBootstrap())  # type: ignore[arg-type]
        _assert_fixed(captured.value)
        assert accesses == []
        assert not _is_closed(reader)
    finally:
        os.close(writer)
        os.close(reader)


def test_incomplete_or_duplicate_exact_bootstrap_cannot_consume_an_untrusted_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_reader, source_writer = os.pipe()
    source = _bootstrap(monkeypatch, tmp_path, source_reader)
    os.close(source_writer)

    incomplete_reader, incomplete_writer = os.pipe()
    incomplete = object.__new__(SidecarChildBootstrap)
    object.__setattr__(incomplete, "config", source.config)
    object.__setattr__(incomplete, "parent_handoff_fd", incomplete_reader)
    try:
        with pytest.raises(RuntimeError) as incomplete_error:
            handoff_module.await_parent_handoff(incomplete)
        _assert_fixed(incomplete_error.value)
        assert not _is_closed(incomplete_reader)
    finally:
        os.close(incomplete_writer)
        os.close(incomplete_reader)

    duplicate_reader, duplicate_writer = os.pipe()
    duplicate = object.__new__(SidecarChildBootstrap)
    object.__setattr__(duplicate, "config", source.config)
    object.__setattr__(duplicate, "owner_lock_fd", 101)
    object.__setattr__(duplicate, "startup_writer_fd", 101)
    object.__setattr__(duplicate, "parent_handoff_fd", duplicate_reader)
    os.close(duplicate_writer)
    with pytest.raises(RuntimeError) as duplicate_error:
        handoff_module.await_parent_handoff(duplicate)
    _assert_fixed(duplicate_error.value)
    assert not _is_closed(duplicate_reader)
    os.close(duplicate_reader)
    handoff_module.await_parent_handoff(source)


def test_real_isolated_child_cannot_pass_the_gate_before_parent_eof_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    handoff_reader, handoff_writer = os.pipe()
    ready_reader, ready_writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, handoff_reader)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-c",
                _CHILD_SOURCE,
                bootstrap.config.runtime_root,
                str(ready_writer),
                *encode_sidecar_child_bootstrap(
                    bootstrap.config,
                    owner_lock_fd=bootstrap.owner_lock_fd,
                    startup_writer_fd=bootstrap.startup_writer_fd,
                    parent_handoff_fd=bootstrap.parent_handoff_fd,
                ),
            ),
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(handoff_reader, ready_writer),
            start_new_session=True,
        )
        os.close(handoff_reader)
        handoff_reader = -1
        os.close(ready_writer)
        ready_writer = -1

        assert select.select((ready_reader,), (), (), _ISOLATED_CHILD_TIMEOUT) == (
            [ready_reader],
            [],
            [],
        )
        assert os.read(ready_reader, 1) == b"E"
        assert select.select((ready_reader,), (), (), 0.2) == ([], [], [])

        os.close(handoff_writer)
        handoff_writer = -1
        assert select.select((ready_reader,), (), (), _ISOLATED_CHILD_TIMEOUT) == (
            [ready_reader],
            [],
            [],
        )
        assert os.read(ready_reader, 1) == b"R"
        assert process.wait(timeout=_ISOLATED_CHILD_TIMEOUT) == 0
    finally:
        for descriptor in (ready_writer, ready_reader, handoff_writer, handoff_reader):
            if descriptor >= 0:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=2.0)


def test_data_or_timeout_fails_closed_and_retires_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)
    os.write(writer, b"x")
    os.close(writer)

    with pytest.raises(RuntimeError) as captured:
        handoff_module.await_parent_handoff(bootstrap)

    _assert_fixed(captured.value)
    assert _is_closed(reader)

    timeout_reader, timeout_writer = os.pipe()
    timeout_bootstrap = _bootstrap(monkeypatch, tmp_path / "timeout", timeout_reader)

    class EmptyPoll:
        def register(self, *_args: object) -> None:
            pass

        def poll(self, *_args: object) -> list[tuple[int, int]]:
            return []

    monkeypatch.setattr(handoff_module, "_POLL", EmptyPoll)
    try:
        with pytest.raises(RuntimeError) as timeout:
            handoff_module.await_parent_handoff(timeout_bootstrap)
        _assert_fixed(timeout.value)
        assert _is_closed(timeout_reader)
    finally:
        os.close(timeout_writer)


@pytest.mark.parametrize("outcome", ["eof", "data", "timeout"])
def test_high_numbered_pipe_descriptor_is_supported_and_fails_closed_when_needed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
) -> None:
    initial_reader, writer = os.pipe()
    try:
        reader = fcntl.fcntl(initial_reader, fcntl.F_DUPFD, 1025)
    except OSError:
        os.close(initial_reader)
        os.close(writer)
        pytest.skip("the host cannot allocate an inherited descriptor above FD_SETSIZE")
    os.close(initial_reader)
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)

    if outcome == "eof":
        os.close(writer)
        writer = -1
        handoff_module.await_parent_handoff(bootstrap)
    elif outcome == "data":
        os.write(writer, b"x")
        os.close(writer)
        writer = -1
        with pytest.raises(RuntimeError) as captured:
            handoff_module.await_parent_handoff(bootstrap)
        _assert_fixed(captured.value)
    else:

        class EmptyPoll:
            def register(self, *_args: object) -> None:
                pass

            def poll(self, *_args: object) -> list[tuple[int, int]]:
                return []

        monkeypatch.setattr(handoff_module, "_POLL", EmptyPoll)
        with pytest.raises(RuntimeError) as captured:
            handoff_module.await_parent_handoff(bootstrap)
        _assert_fixed(captured.value)

    try:
        assert _is_closed(reader)
    finally:
        if writer >= 0:
            os.close(writer)


def test_regular_descriptor_fails_before_wait_and_leaks_no_private_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    descriptor = os.open(tmp_path / "regular", os.O_CREAT | os.O_RDWR, 0o600)
    bootstrap = _bootstrap(monkeypatch, tmp_path, descriptor)

    with pytest.raises(RuntimeError) as captured:
        handoff_module.await_parent_handoff(bootstrap)

    _assert_fixed(captured.value)
    assert _is_closed(descriptor)
    assert str(tmp_path) not in "".join(traceback.format_exception(captured.value))


def test_inexact_endpoint_collaborator_output_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)
    monkeypatch.setattr(handoff_module, "_FCNTL", lambda *_args: False)
    try:
        with pytest.raises(RuntimeError) as captured:
            handoff_module.await_parent_handoff(bootstrap)
        _assert_fixed(captured.value)
        assert _is_closed(reader)
    finally:
        os.close(writer)


@pytest.mark.parametrize("set_result", [1, 0])
def test_nonblocking_transition_must_succeed_and_be_observable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    set_result: int,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)
    canonical_fcntl = handoff_module._FCNTL

    def fake_fcntl(descriptor: int, operation: int, *arguments: int) -> object:
        if operation == handoff_module._F_SETFL:
            return set_result
        if set_result == 0 and operation == handoff_module._F_GETFL and arguments == ():
            return canonical_fcntl(descriptor, operation)
        return canonical_fcntl(descriptor, operation, *arguments)

    monkeypatch.setattr(handoff_module, "_FCNTL", fake_fcntl)
    try:
        with pytest.raises(RuntimeError) as captured:
            handoff_module.await_parent_handoff(bootstrap)
        _assert_fixed(captured.value)
        assert _is_closed(reader)
    finally:
        os.close(writer)


def test_public_math_replacement_cannot_redirect_captured_handoff_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)
    os.close(writer)
    monkeypatch.setattr(handoff_module.math, "isfinite", lambda _value: False)
    monkeypatch.setattr(handoff_module.math, "ceil", lambda _value: 0)
    monkeypatch.setattr(handoff_module.fcntl, "F_GETFL", -1)
    monkeypatch.setattr(handoff_module.fcntl, "F_SETFL", -1)
    monkeypatch.setattr(handoff_module.os, "O_ACCMODE", -1)
    monkeypatch.setattr(handoff_module.os, "O_RDONLY", -1)
    monkeypatch.setattr(handoff_module.os, "O_NONBLOCK", -1)
    monkeypatch.setattr(handoff_module.select, "POLLIN", -1)
    monkeypatch.setattr(handoff_module.select, "POLLHUP", -1)
    monkeypatch.setattr(handoff_module.select, "POLLERR", -1)
    monkeypatch.setattr(handoff_module.select, "POLLNVAL", -1)

    handoff_module.await_parent_handoff(bootstrap)

    assert _is_closed(reader)


def test_process_control_preserves_identity_after_endpoint_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)
    control = _Control("private handoff control")

    def interrupt(*_args: object, **_kwargs: object) -> NoReturn:
        raise control

    monkeypatch.setattr(handoff_module, "_POLL", interrupt)
    try:
        with pytest.raises(_Control) as captured:
            handoff_module.await_parent_handoff(bootstrap)
        assert captured.value is control
        assert _is_closed(reader)
    finally:
        os.close(writer)


@pytest.mark.parametrize("final_clock", [5.0, 1.0])
def test_eof_after_deadline_or_clock_rollback_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    final_clock: float,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader, startup_timeout=4.0)
    os.close(writer)
    clocks = iter((0.0, 1.0, 2.0, final_clock))
    monkeypatch.setattr(handoff_module, "_READ_MONOTONIC", lambda: next(clocks))

    with pytest.raises(RuntimeError) as captured:
        handoff_module.await_parent_handoff(bootstrap)

    _assert_fixed(captured.value)
    assert _is_closed(reader)


def test_control_cleanup_note_cannot_be_replaced_and_non_none_close_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    bootstrap = _bootstrap(monkeypatch, tmp_path, reader)
    primary = _Control("primary")
    cleanup = _Control("cleanup")
    canonical_poll = handoff_module._POLL

    def interrupt() -> NoReturn:
        raise primary

    def fail_close(_descriptor: int) -> NoReturn:
        raise cleanup

    monkeypatch.setattr(handoff_module, "_POLL", interrupt)
    monkeypatch.setattr(handoff_module, "_CLOSE", fail_close)
    try:
        with pytest.raises(_Control) as captured:
            handoff_module.await_parent_handoff(bootstrap)
        assert captured.value is primary
        assert primary.__notes__ == ["sidecar parent handoff cleanup failed"]
    finally:
        os.close(writer)
        os.close(reader)

    close_reader, close_writer = os.pipe()
    close_bootstrap = _bootstrap(monkeypatch, tmp_path / "close", close_reader)
    os.close(close_writer)
    monkeypatch.setattr(handoff_module, "_POLL", canonical_poll)
    monkeypatch.setattr(handoff_module, "_CLOSE", lambda _descriptor: object())
    with pytest.raises(RuntimeError) as close_error:
        handoff_module.await_parent_handoff(close_bootstrap)
    _assert_fixed(close_error.value)
    os.close(close_reader)


def test_source_freezes_one_child_only_eof_wait_boundary() -> None:
    tree = ast.parse(Path(handoff_module.__file__).read_text(encoding="utf-8"))
    imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert imports == {"fcntl", "math", "os", "select", "stat", "time"}
    public_functions = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    assert public_functions == ["await_parent_handoff"]
    forbidden = {
        "Popen",
        "subprocess",
        "threading",
        "asyncio",
        "socket",
        "logging",
        "print",
        "write",
        "kill",
        "terminate",
    }
    assert not any(isinstance(node, ast.Name) and node.id in forbidden for node in ast.walk(tree))
    factories = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_POLL"
    ]
    waits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "poll"
    ]
    assert len(factories) == 1
    assert len(waits) == 1
    module_binding_attributes = {
        id(attribute)
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and node.value is not None
        for attribute in ast.walk(node.value)
        if isinstance(attribute, ast.Attribute)
    }
    runtime_attributes = [
        attribute
        for attribute in ast.walk(tree)
        if isinstance(attribute, ast.Attribute) and id(attribute) not in module_binding_attributes
    ]
    assert [
        (attribute.value.id, attribute.attr)
        for attribute in runtime_attributes
        if isinstance(attribute.value, ast.Name)
    ] == [
        ("metadata", "st_mode"),
        ("poller", "register"),
        ("poller", "poll"),
    ]

    def call_name(node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
        return "<dynamic>"

    calls_by_function = {
        node.name: sorted(call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call))
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert calls_by_function == {
        "_read_bootstrap_slots": sorted(
            [
                "_OBJECT_GETATTRIBUTE",
                "_OBJECT_GETATTRIBUTE",
                "_OBJECT_GETATTRIBUTE",
                "_OBJECT_GETATTRIBUTE",
                "type",
                "type",
                "type",
                "type",
                "type",
            ]
        ),
        "_read_timeout": ["_ISFINITE", "_OBJECT_GETATTRIBUTE", "type"],
        "_remaining": ["_ISFINITE", "_ISFINITE", "_READ_MONOTONIC", "type"],
        "_is_read_only_pipe": sorted(
            [
                "_FCNTL",
                "_FCNTL",
                "_FCNTL",
                "_FPATHCONF",
                "_FSTAT",
                "_IS_FIFO",
                "type",
                "type",
                "type",
                "type",
                "type",
                "type",
            ]
        ),
        "_await_eof": sorted(
            [
                "_CEIL",
                "_ISFINITE",
                "_ISFINITE",
                "_POLL",
                "_READ",
                "_READ_MONOTONIC",
                "_remaining",
                "_remaining",
                "_remaining",
                "len",
                "len",
                "poller.poll",
                "poller.register",
                "type",
                "type",
                "type",
                "type",
                "type",
                "type",
                "type",
            ]
        ),
        "_await_handoff": sorted(
            [
                "_ADD_NOTE",
                "_ADD_NOTE",
                "_CLOSE",
                "_await_eof",
                "_is_read_only_pipe",
                "_read_bootstrap_slots",
                "_read_timeout",
            ]
        ),
        "_raise_handoff_failure": ["RuntimeError"],
        "await_parent_handoff": ["_await_handoff", "_raise_handoff_failure"],
    }
