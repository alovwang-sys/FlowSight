from __future__ import annotations

import ast
import http.client
import inspect
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    OwnerLock,
    OwnerLockError,
    OwnerLockErrorCode,
    StartupChannelError,
    StartupFailure,
    StartupFailureCode,
    StartupReader,
    StartupReady,
    StateStore,
    encode_sidecar_child_bootstrap,
    open_startup_channel,
    prepare_sidecar_runtime_config,
    run_sidecar_child,
)
from flowsight.sidecar import child_runtime as runtime_module
from flowsight.sidecar import runtime_config as config_module
from flowsight.sidecar.state import SidecarState

TRANSACTION_ERROR = "sidecar child transaction failed"


class _Control(BaseException):
    pass


class _TupleSubclass(tuple[str, ...]):
    pass


CHILD_SOURCE = r"""
import signal
import sys
from pathlib import Path

from flowsight.sidecar import run_sidecar_child
from flowsight.sidecar import runtime_config as config_module

mode = sys.argv[1]
runtime_root = Path(sys.argv[2])
config_module._USER_RUNTIME_PATH = lambda *_args, **_kwargs: runtime_root
if mode == "custom":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
elif mode == "default":
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
else:
    raise SystemExit(31)
run_sidecar_child(tuple(sys.argv[3:]))
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def _resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    requested_port: int = 0,
) -> tuple[StateStore, tuple[str, ...], StartupReader, int, int]:
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )
    config = prepare_sidecar_runtime_config(
        _project(tmp_path),
        requested_port=requested_port,
    )
    store = StateStore(config.runtime_root, project_id=config.project_id)
    store.ensure_private_directory()
    parent_owner = OwnerLock.acquire(store)
    reader, parent_writer = open_startup_channel()
    owner_fd = -1
    writer_fd = -1
    try:
        owner_fd = os.dup(parent_owner.fileno())
        writer_fd = os.dup(parent_writer.fileno())
        parent_owner.close()
        parent_writer.close()
        arguments = encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=owner_fd,
            startup_writer_fd=writer_fd,
        )
        return store, arguments, reader, owner_fd, writer_fd
    except BaseException:
        parent_owner.close()
        parent_writer.close()
        reader.close()
        for descriptor in (writer_fd, owner_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _assert_owner_released(store: StateStore) -> None:
    successor = OwnerLock.acquire(store)
    successor.close()


def _assert_reader_closed(reader: StartupReader) -> None:
    with pytest.raises(StartupChannelError):
        reader.receive(timeout=2.0)


def _reap_child(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5.0)


def test_public_export_and_exact_signature() -> None:
    assert sidecar_package.run_sidecar_child is run_sidecar_child
    assert sidecar_package.__all__.count("run_sidecar_child") == 1
    signature = inspect.signature(run_sidecar_child)
    assert tuple(signature.parameters) == ("arguments",)
    assert signature.parameters["arguments"].annotation == "tuple[str, ...]"
    assert signature.return_annotation == "None"
    assert get_type_hints(run_sidecar_child) == {
        "arguments": tuple[str, ...],
        "return": type(None),
    }


def test_child_transaction_ast_keeps_the_child_only_composition_boundary() -> None:
    source = Path(runtime_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    public_functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    assert public_functions == ["run_sidecar_child"]
    imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert imports == {"socket"}
    imported_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported_modules == {
        "__future__",
        "typing",
        "child_preparation",
        "listener",
        "owner_lock",
        "runtime_config",
        "server_runtime",
        "startup_channel",
        "startup_state",
        "state",
    }
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert (
        sum(isinstance(call.func, ast.Name) and call.func.id == "_PREPARE_CHILD" for call in calls)
        == 1
    )
    assert (
        sum(
            isinstance(call.func, ast.Name) and call.func.id == "_BIND_LOOPBACK_LISTENER"
            for call in calls
        )
        == 1
    )
    assert (
        sum(
            isinstance(call.func, ast.Name) and call.func.id == "_CREATE_STARTUP_STATE"
            for call in calls
        )
        == 1
    )
    assert (
        sum(isinstance(call.func, ast.Name) and call.func.id == "_STATE_PUBLISH" for call in calls)
        == 1
    )
    assert (
        sum(
            isinstance(call.func, ast.Name) and call.func.id == "_SERVE_OWNED_PREBOUND"
            for call in calls
        )
        == 1
    )
    forbidden_attributes = {"bind", "listen", "fromfd", "dup", "unlink", "run"}
    assert not any(
        isinstance(call.func, ast.Attribute) and call.func.attr in forbidden_attributes
        for call in calls
    )
    nested_functions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "on_started_hook"
    ]
    assert nested_functions == ["on_started_hook"]


@pytest.mark.parametrize(
    "arguments",
    [[], (value for value in ()), _TupleSubclass(("value",))],
)
def test_wrong_top_level_arguments_fail_before_preparation(
    monkeypatch: pytest.MonkeyPatch,
    arguments: object,
) -> None:
    calls: list[object] = []

    def prepare(unexpected: tuple[str, ...]) -> object:
        calls.append(unexpected)
        raise AssertionError

    monkeypatch.setattr(runtime_module, "_PREPARE_CHILD", prepare)

    with pytest.raises(TypeError) as captured:
        run_sidecar_child(arguments)  # type: ignore[arg-type]

    assert str(captured.value) == "arguments must be an exact built-in tuple"
    assert captured.value.__cause__ is None
    assert calls == []


def test_preparation_failure_sends_nothing_or_attempts_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def prepare(arguments: tuple[str, ...]) -> object:
        calls.append(arguments)
        raise RuntimeError("raw dependency detail")

    monkeypatch.setattr(runtime_module, "_PREPARE_CHILD", prepare)

    with pytest.raises(RuntimeError) as captured:
        run_sidecar_child(("canonical",))

    assert str(captured.value) == TRANSACTION_ERROR
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert getattr(captured.value, "__notes__", []) == []
    assert calls == [("canonical",)]


def test_ready_publish_bridge_and_cleanup_use_one_exact_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, owner_fd, writer_fd = _resources(monkeypatch, tmp_path)
    calls: list[str] = []

    def bridge(
        state: SidecarState,
        listener: socket.socket,
        *,
        on_started: Callable[[], None],
    ) -> None:
        calls.append("bridge")
        on_started()
        listener.close()
        assert type(state) is SidecarState

    monkeypatch.setattr(runtime_module, "_SERVE_OWNED_PREBOUND", bridge)
    try:
        assert run_sidecar_child(arguments) is None
        outcome = reader.receive(timeout=2.0)
        assert type(outcome) is StartupReady
        assert outcome.port > 0
        assert outcome.sidecar_pid == os.getpid()
        assert store.load() is None
        assert calls == ["bridge"]
        _assert_owner_released(store)
        with pytest.raises(OSError):
            os.fstat(owner_fd)
        with pytest.raises(OSError):
            os.fstat(writer_fd)
    finally:
        reader.close()


def test_normal_bridge_return_without_ready_emits_one_failure_then_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)

    def bridge(
        _state: SidecarState,
        listener: socket.socket,
        *,
        on_started: Callable[[], None],
    ) -> None:
        del on_started
        listener.close()

    monkeypatch.setattr(runtime_module, "_SERVE_OWNED_PREBOUND", bridge)
    try:
        with pytest.raises(RuntimeError) as captured:
            run_sidecar_child(arguments)
        assert str(captured.value) == TRANSACTION_ERROR
        assert captured.value.__cause__ is None
        assert captured.value.__suppress_context__ is True
        assert reader.receive(timeout=2.0) == StartupFailure(
            code=StartupFailureCode.SIDECAR_STARTUP_FAILED
        )
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()


def test_publish_failure_removes_attempted_state_then_emits_failure_in_cleanup_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)
    calls: list[str] = []
    canonical_publish = runtime_module._STATE_PUBLISH
    canonical_remove = runtime_module._STATE_REMOVE_IF_OWNED
    canonical_close = runtime_module._SOCKET_CLOSE
    canonical_send = runtime_module._WRITER_SEND
    canonical_writer_close = runtime_module._WRITER_CLOSE
    canonical_owner_close = runtime_module._OWNER_CLOSE

    def publish(actual_store: StateStore, state: SidecarState) -> None:
        calls.append("publish")
        canonical_publish(actual_store, state)
        raise RuntimeError("dependency detail")

    def close_listener(listener: socket.socket) -> None:
        calls.append("listener")
        canonical_close(listener)

    def remove(actual_store: StateStore, startup_id: str) -> bool:
        calls.append("remove")
        return canonical_remove(actual_store, startup_id)

    def send(writer: object, message: object) -> None:
        calls.append("failure-send")
        canonical_send(writer, message)

    def close_writer(writer: object) -> None:
        calls.append("writer")
        canonical_writer_close(writer)

    def close_owner(owner: object) -> None:
        calls.append("owner")
        canonical_owner_close(owner)

    monkeypatch.setattr(runtime_module, "_STATE_PUBLISH", publish)
    monkeypatch.setattr(runtime_module, "_SOCKET_CLOSE", close_listener)
    monkeypatch.setattr(runtime_module, "_STATE_REMOVE_IF_OWNED", remove)
    monkeypatch.setattr(runtime_module, "_WRITER_SEND", send)
    monkeypatch.setattr(runtime_module, "_WRITER_CLOSE", close_writer)
    monkeypatch.setattr(runtime_module, "_OWNER_CLOSE", close_owner)
    try:
        with pytest.raises(RuntimeError, match=f"^{TRANSACTION_ERROR}$"):
            run_sidecar_child(arguments)
        assert reader.receive(timeout=2.0) == StartupFailure(
            code=StartupFailureCode.SIDECAR_STARTUP_FAILED
        )
        assert calls == ["publish", "listener", "remove", "failure-send", "writer", "owner"]
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()


def test_non_none_publication_result_is_not_a_success_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)
    canonical_publish = runtime_module._STATE_PUBLISH

    def publish(actual_store: StateStore, state: SidecarState) -> int:
        canonical_publish(actual_store, state)
        return 0

    def bridge(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("publish failure must not reach the bridge")

    monkeypatch.setattr(runtime_module, "_STATE_PUBLISH", publish)
    monkeypatch.setattr(runtime_module, "_SERVE_OWNED_PREBOUND", bridge)
    try:
        with pytest.raises(RuntimeError, match=f"^{TRANSACTION_ERROR}$"):
            run_sidecar_child(arguments)
        assert reader.receive(timeout=2.0) == StartupFailure(
            code=StartupFailureCode.SIDECAR_STARTUP_FAILED
        )
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()


def test_ready_attempt_never_falls_back_to_failure_after_bridge_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)

    def bridge(
        _state: SidecarState,
        listener: socket.socket,
        *,
        on_started: Callable[[], None],
    ) -> None:
        on_started()
        listener.close()
        raise RuntimeError("bridge detail")

    monkeypatch.setattr(runtime_module, "_SERVE_OWNED_PREBOUND", bridge)
    try:
        with pytest.raises(RuntimeError, match=f"^{TRANSACTION_ERROR}$"):
            run_sidecar_child(arguments)
        assert type(reader.receive(timeout=2.0)) is StartupReady
        _assert_reader_closed(reader)
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()


@pytest.mark.parametrize("reported_result", [False, 0], ids=["false", "non-bool"])
def test_successful_publication_requires_exact_true_compare_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reported_result: object,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)
    canonical_remove = runtime_module._STATE_REMOVE_IF_OWNED

    def remove(actual_store: StateStore, startup_id: str) -> object:
        assert canonical_remove(actual_store, startup_id) is True
        return reported_result

    def bridge(
        _state: SidecarState,
        listener: socket.socket,
        *,
        on_started: Callable[[], None],
    ) -> None:
        on_started()
        listener.close()

    monkeypatch.setattr(runtime_module, "_STATE_REMOVE_IF_OWNED", remove)
    monkeypatch.setattr(runtime_module, "_SERVE_OWNED_PREBOUND", bridge)
    try:
        with pytest.raises(RuntimeError, match=f"^{TRANSACTION_ERROR}$"):
            run_sidecar_child(arguments)
        assert type(reader.receive(timeout=2.0)) is StartupReady
        _assert_reader_closed(reader)
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()


@pytest.mark.parametrize("stage", ["state", "bridge"])
def test_process_control_preserves_identity_and_never_emits_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)
    injected = _Control("control")

    if stage == "state":

        def create_state(_store: StateStore, _listener: socket.socket) -> SidecarState:
            raise injected

        monkeypatch.setattr(runtime_module, "_CREATE_STARTUP_STATE", create_state)
    else:

        def bridge(
            _state: SidecarState,
            listener: socket.socket,
            *,
            on_started: Callable[[], None],
        ) -> None:
            del on_started
            listener.close()
            raise injected

        monkeypatch.setattr(runtime_module, "_SERVE_OWNED_PREBOUND", bridge)

    try:
        with pytest.raises(_Control) as captured:
            run_sidecar_child(arguments)
        assert captured.value is injected
        assert getattr(injected, "__notes__", []) == []
        _assert_reader_closed(reader)
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()


def test_active_control_survives_listener_cleanup_failure_with_one_safe_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)
    injected = _Control("control")
    listeners: list[socket.socket] = []

    def create_state(_store: StateStore, _listener: socket.socket) -> SidecarState:
        raise injected

    def close_listener(listener: socket.socket) -> None:
        listeners.append(listener)
        raise RuntimeError("raw cleanup detail")

    monkeypatch.setattr(runtime_module, "_CREATE_STARTUP_STATE", create_state)
    monkeypatch.setattr(runtime_module, "_SOCKET_CLOSE", close_listener)
    try:
        with pytest.raises(_Control) as captured:
            run_sidecar_child(arguments)
        assert captured.value is injected
        assert injected.__notes__ == ["sidecar child transaction cleanup failed"]
        _assert_reader_closed(reader)
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        for listener in listeners:
            listener.close()
        reader.close()


def test_public_replacement_cannot_redirect_captured_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, _owner_fd, _writer_fd = _resources(monkeypatch, tmp_path)

    def replacement(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("public replacement must not run")

    def bridge(
        _state: SidecarState,
        listener: socket.socket,
        *,
        on_started: Callable[[], None],
    ) -> None:
        on_started()
        listener.close()

    monkeypatch.setattr(sidecar_package, "prepare_sidecar_child", replacement)
    monkeypatch.setattr(runtime_module, "_SERVE_OWNED_PREBOUND", bridge)
    try:
        assert run_sidecar_child(arguments) is None
        assert type(reader.receive(timeout=2.0)) is StartupReady
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()


def test_real_child_publishes_ready_serves_health_and_cleans_up_after_sigterm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, owner_fd, writer_fd = _resources(monkeypatch, tmp_path)
    process: subprocess.Popen[bytes] | None = None
    try:
        child_cwd = tmp_path / "child"
        child_cwd.mkdir()
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-u",
                "-c",
                CHILD_SOURCE,
                "custom",
                str(tmp_path / "runtime-root"),
                *arguments,
            ),
            cwd=child_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(owner_fd, writer_fd),
            start_new_session=True,
        )
        os.close(owner_fd)
        os.close(writer_fd)
        owner_fd = -1
        writer_fd = -1

        ready = reader.receive(timeout=10.0)
        assert type(ready) is StartupReady
        state = store.load()
        assert type(state) is SidecarState
        assert ready.startup_id == state.startup_id
        assert ready.sidecar_pid == process.pid == state.pid
        assert ready.port == state.port
        with pytest.raises(OwnerLockError) as held:
            OwnerLock.acquire(store)
        assert held.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD

        connection = http.client.HTTPConnection(state.host, state.port, timeout=2.0)
        try:
            connection.request(
                "GET",
                "/internal/v1/health",
                headers={
                    "Host": state.authority,
                    "Authorization": f"Bearer {state.token}",
                },
            )
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {
                "status": "ok",
                "protocol_version": state.protocol_version,
                "state_schema_version": state.state_schema_version,
                "project_id": state.project_id,
                "startup_id": state.startup_id,
                "sidecar_pid": state.pid,
                "host": state.host,
                "port": state.port,
            }
        finally:
            connection.close()

        for host, token, expected_status, expected_code in (
            (state.authority, "wrong-token", 401, "AUTH_REJECTED"),
            (f"localhost:{state.port}", state.token, 403, "HOST_REJECTED"),
        ):
            rejected = http.client.HTTPConnection(state.host, state.port, timeout=2.0)
            try:
                rejected.request(
                    "GET",
                    "/internal/v1/health",
                    headers={"Host": host, "Authorization": f"Bearer {token}"},
                )
                rejected_response = rejected.getresponse()
                rejected_headers = tuple(rejected_response.getheaders())
                rejected_body = rejected_response.read()
                assert rejected_response.status == expected_status
                assert json.loads(rejected_body) == {"detail": {"code": expected_code}}
                names = {name.lower() for name, _value in rejected_headers}
                assert "server" not in names
                assert "date" not in names
                assert not any(name.startswith("access-control-") for name in names)
                assert state.token.encode() not in rejected_body
            finally:
                rejected.close()

        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=10.0)
        stdout, stderr = process.communicate(timeout=5.0)
        assert process.returncode == 0
        assert stdout == b""
        assert stderr == b""
        assert state.token.encode() not in stdout + stderr
        assert state.database_path.encode() not in stdout + stderr
        assert store.load() is None
        _assert_owner_released(store)
        replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            replacement.bind((state.host, state.port))
        finally:
            replacement.close()
    finally:
        reader.close()
        if owner_fd >= 0:
            os.close(owner_fd)
        if writer_fd >= 0:
            os.close(writer_fd)
        if process is not None:
            _reap_child(process)


def test_real_child_default_sigterm_releases_os_resources_without_claiming_outer_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, arguments, reader, owner_fd, writer_fd = _resources(monkeypatch, tmp_path)
    process: subprocess.Popen[bytes] | None = None
    state: SidecarState | None = None
    try:
        child_cwd = tmp_path / "default-child"
        child_cwd.mkdir()
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-u",
                "-c",
                CHILD_SOURCE,
                "default",
                str(tmp_path / "runtime-root"),
                *arguments,
            ),
            cwd=child_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(owner_fd, writer_fd),
            start_new_session=True,
        )
        os.close(owner_fd)
        os.close(writer_fd)
        owner_fd = -1
        writer_fd = -1

        ready = reader.receive(timeout=10.0)
        assert type(ready) is StartupReady
        state = store.load()
        assert type(state) is SidecarState
        assert ready.startup_id == state.startup_id
        assert ready.sidecar_pid == process.pid == state.pid

        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=10.0)
        stdout, stderr = process.communicate(timeout=5.0)
        assert process.returncode == -signal.SIGTERM
        assert stdout == b""
        assert stderr == b""
        replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            replacement.bind((state.host, state.port))
        finally:
            replacement.close()
        _assert_owner_released(store)
        assert store.load() == state
    finally:
        reader.close()
        if state is not None and store.load() is not None:
            assert store.remove_if_owned(state.startup_id) is True
        if owner_fd >= 0:
            os.close(owner_fd)
        if writer_fd >= 0:
            os.close(writer_fd)
        if process is not None:
            _reap_child(process)


def test_real_child_pre_ready_bind_failure_emits_only_fixed_failure_and_releases_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    requested_port = occupied.getsockname()[1]
    store, arguments, reader, owner_fd, writer_fd = _resources(
        monkeypatch,
        tmp_path,
        requested_port=requested_port,
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        child_cwd = tmp_path / "failed-child"
        child_cwd.mkdir()
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-u",
                "-c",
                CHILD_SOURCE,
                "custom",
                str(tmp_path / "runtime-root"),
                *arguments,
            ),
            cwd=child_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(owner_fd, writer_fd),
            start_new_session=True,
        )
        os.close(owner_fd)
        os.close(writer_fd)
        owner_fd = -1
        writer_fd = -1

        assert reader.receive(timeout=10.0) == StartupFailure(
            code=StartupFailureCode.SIDECAR_STARTUP_FAILED
        )
        process.wait(timeout=10.0)
        stdout, stderr = process.communicate(timeout=5.0)
        assert process.returncode != 0
        assert stdout == b""
        assert b"sidecar child transaction failed" in stderr
        assert str(tmp_path).encode() not in stderr
        assert b"requested-port" not in stderr
        assert store.load() is None
        _assert_owner_released(store)
    finally:
        reader.close()
        occupied.close()
        if owner_fd >= 0:
            os.close(owner_fd)
        if writer_fd >= 0:
            os.close(writer_fd)
        if process is not None:
            _reap_child(process)
