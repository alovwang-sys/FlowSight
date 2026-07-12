from __future__ import annotations

import ast
import asyncio
import gc
import http.client
import inspect
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import warnings
import weakref
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    SidecarState,
    StateStore,
    bind_loopback_listener,
    create_startup_state,
    serve_prebound_sidecar_app,
)
from flowsight.sidecar import server_runtime as server_module

STATE_TYPE_ERROR = "state must be an exact SidecarState"
LISTENER_TYPE_ERROR = "listener must be an exact built-in socket.socket"
HOOK_TYPE_ERROR = "on_started must be an exact Python function"
COMPATIBILITY_ERROR = "sidecar state and listener are incompatible"
SERVER_ERROR = "prebound sidecar server failed"

CHILD_TIMEOUT_SECONDS = 10.0
CHILD_CLEANUP_GRACE_SECONDS = 1.0
CHILD_REAP_SECONDS = 5.0
PIPE_MESSAGE_LIMIT = 8192
HTTP_TIMEOUT_SECONDS = 3.0
HTTP_BODY_LIMIT = 64 * 1024
PROJECT_ID = "project-v1-" + "a" * 64

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _Control(BaseException):
    pass


class _TrackedFailure(Exception):
    references: list[weakref.ReferenceType[_TrackedFailure]] = []

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.references.append(weakref.ref(self))


class _DerivedState(SidecarState):
    pass


class _DerivedSocket(socket.socket):
    pass


class _IntSubclass(int):
    def __index__(self) -> NoReturn:
        raise AssertionError("integer protocol ran")

    def __eq__(self, other: object) -> NoReturn:
        del other
        raise AssertionError("integer equality ran")


class _TextSubclass(str):
    def __eq__(self, other: object) -> NoReturn:
        del other
        raise AssertionError("text equality ran")


class _ExplodingProxy:
    def __getattribute__(self, name: str) -> NoReturn:
        raise AssertionError(f"proxy dispatch ran: {name}")


class _CallableObject:
    def __call__(self) -> None:
        raise AssertionError("callable object ran")


class _BoundHook:
    def run(self) -> None:
        raise AssertionError("bound method ran")


def _started() -> None:
    return None


async def _async_started() -> None:
    return None


def _generator_started() -> object:
    yield None


async def _async_generator_started() -> object:
    yield None


def _live_pair(tmp_path: Path) -> tuple[StateStore, socket.socket, SidecarState]:
    store = StateStore(tmp_path / "runtime-root", project_id=PROJECT_ID)
    listener = bind_loopback_listener(0)
    try:
        state = create_startup_state(store, listener)
    except BaseException:
        listener.close()
        raise
    return store, listener, state


def _state_for_port(tmp_path: Path, port: int, *, pid: int | None = None) -> SidecarState:
    return SidecarState(
        project_id=PROJECT_ID,
        startup_id="b" * 32,
        pid=os.getpid() if pid is None else pid,
        port=port,
        token="T" * 43,
        database_path=str((tmp_path / "runtime-root" / "events.sqlite3").absolute()),
        started_at_ns=1,
    )


def _replace_state(state: SidecarState, **changes: object) -> SidecarState:
    values = state.to_wire()
    values.update(changes)
    return SidecarState.from_wire(values)


def _forge_state(
    state: SidecarState,
    *,
    missing: str | None = None,
    replacements: dict[str, object] | None = None,
) -> SidecarState:
    values = state.to_wire()
    if replacements is not None:
        values.update(replacements)
    forged = object.__new__(SidecarState)
    for name, value in values.items():
        if name != missing:
            object.__setattr__(forged, name, value)
    return forged


def _listener_snapshot(listener: socket.socket) -> tuple[object, ...]:
    descriptor = listener.fileno()
    metadata = os.fstat(descriptor)
    try:
        if sys.platform == "linux":
            accepting: object = listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        else:
            option = socket.TCP_CONNECTION_INFO
            accepting = listener.getsockopt(socket.IPPROTO_TCP, option, 1)
    except OSError as error:
        accepting = (OSError, error.errno)
    return (
        descriptor,
        metadata.st_dev,
        metadata.st_ino,
        listener.family,
        listener.type,
        listener.proto,
        listener.getsockname(),
        listener.get_inheritable(),
        listener.getblocking(),
        accepting,
    )


def _assert_fixed_error(
    error: BaseException,
    expected_type: type[TypeError] | type[ValueError] | type[RuntimeError],
    expected_text: str,
    *,
    context: BaseException | None = None,
) -> None:
    assert type(error) is expected_type
    assert str(error) == expected_text
    assert error.args == (expected_text,)
    assert error.__cause__ is None
    assert error.__context__ is context
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", []) == []


def _production_frames(error: BaseException) -> list[tuple[str, dict[str, object]]]:
    production_path = Path(server_module.__file__).resolve()
    result: list[tuple[str, dict[str, object]]] = []
    current = error.__traceback__
    while current is not None:
        if Path(current.tb_frame.f_code.co_filename).resolve() == production_path:
            result.append((current.tb_frame.f_code.co_name, dict(current.tb_frame.f_locals)))
        current = current.tb_next
    return result


def _contains_identity(value: object, targets: tuple[object, ...]) -> bool:
    if any(value is target for target in targets):
        return True
    if type(value) in {tuple, list}:
        return any(_contains_identity(item, targets) for item in value)
    if type(value) is dict:
        mapping = value
        return any(
            _contains_identity(key, targets) or _contains_identity(item, targets)
            for key, item in mapping.items()  # type: ignore[union-attr]
        )
    return False


def _assert_frames_hide(error: BaseException, *targets: object) -> None:
    frames = _production_frames(error)
    assert frames
    exact_targets = tuple(targets)
    assert all(
        not _contains_identity(value, exact_targets)
        for _name, local_values in frames
        for value in local_values.values()
    )


def _assert_no_sensitive_text(error: BaseException, state: SidecarState) -> None:
    formatted = "".join(traceback.format_exception(error))
    assert state.token not in formatted
    assert state.database_path not in formatted
    assert str(state.port) not in str(error)


def _assert_usable_unchanged(
    listener: socket.socket,
    before: tuple[object, ...],
) -> None:
    assert listener.fileno() >= 3
    assert _listener_snapshot(listener) == before


def _wrong_port(port: int) -> int:
    return 1 if port != 1 else 2


def _snapshot_uvicorn_loggers() -> tuple[
    tuple[logging.Logger, int, list[logging.Handler], bool], ...
]:
    return tuple(
        (logger, logger.level, list(logger.handlers), logger.propagate)
        for logger in (
            logging.getLogger("uvicorn.error"),
            logging.getLogger("uvicorn.access"),
            logging.getLogger("uvicorn.asgi"),
        )
    )


def _restore_uvicorn_loggers(
    snapshots: tuple[tuple[logging.Logger, int, list[logging.Handler], bool], ...],
) -> None:
    for logger, level, handlers, propagate in snapshots:
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate


def _read_one_pipe_message(file_descriptor: int, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    result = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise AssertionError("child control pipe timed out")
        readable, _, _ = select.select([file_descriptor], [], [], remaining)
        if not readable:
            raise AssertionError("child control pipe timed out")
        chunk = os.read(file_descriptor, PIPE_MESSAGE_LIMIT + 1 - len(result))
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > PIPE_MESSAGE_LIMIT:
            raise AssertionError("child control message was too large")
    if not result:
        raise AssertionError("child control pipe closed without a message")
    return bytes(result)


def _read_pipe_to_eof(file_descriptor: int) -> bytes:
    result = bytearray()
    while True:
        remaining = PIPE_MESSAGE_LIMIT + 1 - len(result)
        if remaining <= 0:
            raise AssertionError("child signal message was too large")
        chunk = os.read(file_descriptor, remaining)
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > PIPE_MESSAGE_LIMIT:
            raise AssertionError("child signal message was too large")


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group_exit(process_group_id: int) -> None:
    deadline = time.monotonic() + CHILD_REAP_SECONDS
    waiter = threading.Event()
    while _process_group_exists(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise AssertionError("child process group survived cleanup")
        waiter.wait(min(0.01, remaining))


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes, bool]:
    escalated = False
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        stdout, stderr = process.communicate(timeout=CHILD_CLEANUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        escalated = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate(timeout=CHILD_REAP_SECONDS)
    _wait_for_group_exit(process.pid)
    return stdout, stderr, escalated


def _isolated_environment(tmp_path: Path) -> dict[str, str]:
    selected = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("PYTHON")
        and name not in {"HOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
        and not name.startswith("XDG_")
    }
    directories = {
        "HOME": tmp_path / "home",
        "XDG_CACHE_HOME": tmp_path / "xdg-cache",
        "XDG_CONFIG_HOME": tmp_path / "xdg-config",
        "XDG_DATA_HOME": tmp_path / "xdg-data",
        "XDG_RUNTIME_DIR": tmp_path / "xdg-runtime",
    }
    for directory in directories.values():
        directory.mkdir(exist_ok=True)
    directories["XDG_RUNTIME_DIR"].chmod(0o700)
    selected.update({name: str(path) for name, path in directories.items()})
    selected.update({"PYTHONPATH": "", "PYTHONNOUSERSITE": "1"})
    return selected


CHILD_SOURCE = r"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import sys

from flowsight.sidecar import StateStore, bind_loopback_listener, create_startup_state
from flowsight.sidecar import server_runtime as runtime_module
from flowsight.sidecar.server_runtime import serve_prebound_sidecar_app

mode = sys.argv[1]
fixture_fd = int(sys.argv[2])
signal_fd = int(sys.argv[3])
runtime_root = Path(sys.argv[4])
expected_origin = Path(sys.argv[5]).resolve()

if Path(runtime_module.__file__).resolve() != expected_origin:
    raise SystemExit(70)

store = StateStore(runtime_root, project_id="project-v1-" + "a" * 64)
listener = bind_loopback_listener(0)
state = create_startup_state(store, listener)
fixture = json.dumps(
    {"kind": "SERVER_STARTED", "state": state.to_wire()},
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8") + b"\n"
if len(fixture) > 8192:
    raise SystemExit(71)

def audit(event, args):
    del args
    if event == "socket.bind":
        raise AssertionError("a second network listener bind was attempted")

sys.addaudithook(audit)

def on_started():
    loop = asyncio.get_running_loop()
    if not loop.is_running() or listener.getblocking() is not False:
        raise AssertionError("startup hook ran before Uvicorn started")
    if os.write(fixture_fd, fixture) != len(fixture):
        raise AssertionError("fixture write was incomplete")
    os.close(fixture_fd)
    if mode == "hook-ordinary":
        raise OSError("private-hook-ordinary-payload")
    if mode == "hook-non-none":
        return False
    if mode == "hook-control":
        hook_control = HookControl("private-hook-control-payload")
        hook_control.add_note("hook-control-note")
        raise hook_control
    return None

class HookControl(BaseException):
    pass

close_calls = 0
run_calls = 0
run_completed_with_none = False
if mode == "close-raise-after-close":
    canonical_run = runtime_module._SERVER_RUN
    canonical_close = runtime_module._SOCKET_CLOSE

    def exact_run(server, *, sockets):
        global run_calls, run_completed_with_none
        if type(sockets) is not list or sockets != [listener]:
            raise AssertionError("canonical run received a substituted listener")
        run_calls += 1
        run_result = canonical_run(server, sockets=sockets)
        if run_result is not None:
            raise AssertionError("canonical Server.run returned non-None")
        run_completed_with_none = True
        return None

    def close_fault(observed_listener):
        global close_calls
        if observed_listener is not listener:
            raise AssertionError("close received a substituted listener")
        close_calls += 1
        canonical_close(observed_listener)
        raise OSError("private-close-after-close-payload")

    runtime_module._SERVER_RUN = exact_run
    runtime_module._SOCKET_CLOSE = close_fault

if mode in {"custom", "close-raise-after-close"}:
    def replayed(signum, frame):
        del frame
        if signum != signal.SIGTERM or listener.fileno() != -1:
            raise AssertionError("SIGTERM replay preceded listener shutdown")
        os.write(signal_fd, b"SIGTERM_REPLAYED\n")

    signal.signal(signal.SIGTERM, replayed)
elif mode == "default":
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
elif mode in {"hook-ordinary", "hook-non-none", "hook-control"}:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
else:
    raise SystemExit(72)

if mode == "custom":
    outer_context = KeyboardInterrupt("outer-caller-context")
    outer_context.add_note("outer-caller-note")
    inner_context = ValueError("inner-caller-context")
    inner_context.add_note("inner-caller-note")
    try:
        raise outer_context
    except KeyboardInterrupt as observed_outer:
        if observed_outer is not outer_context:
            raise SystemExit(75)
        try:
            raise inner_context
        except ValueError as observed_inner:
            if observed_inner is not inner_context:
                raise SystemExit(76)
            result = serve_prebound_sidecar_app(state, listener, on_started=on_started)
            if (
                sys.exception() is not inner_context
                or inner_context.__notes__ != ["inner-caller-note"]
            ):
                raise SystemExit(77)
        if (
            sys.exception() is not outer_context
            or outer_context.__notes__ != ["outer-caller-note"]
        ):
            raise SystemExit(78)
else:
    try:
        result = serve_prebound_sidecar_app(state, listener, on_started=on_started)
    except BaseException as error:
        if mode in {"hook-ordinary", "hook-non-none"}:
            if (
                type(error) is not RuntimeError
                or str(error) != "prebound sidecar server failed"
                or error.args != ("prebound sidecar server failed",)
                or error.__cause__ is not None
                or error.__context__ is not None
                or error.__suppress_context__ is not True
                or getattr(error, "__notes__", []) != []
            ):
                raise SystemExit(79)
            marker = b"HOOK_FIXED_FAILURE\n"
        elif mode == "hook-control":
            if (
                type(error) is not HookControl
                or str(error) != "private-hook-control-payload"
                or error.__notes__ != ["hook-control-note"]
            ):
                raise SystemExit(80)
            marker = b"HOOK_CONTROL_PRESERVED\n"
        elif mode == "close-raise-after-close":
            if (
                type(error) is not RuntimeError
                or str(error) != "prebound sidecar server failed"
                or error.args != ("prebound sidecar server failed",)
                or error.__cause__ is not None
                or error.__context__ is not None
                or error.__suppress_context__ is not True
                or getattr(error, "__notes__", []) != []
                or run_calls != 1
                or run_completed_with_none is not True
                or close_calls != 1
            ):
                raise SystemExit(83)
            marker = b"CLOSE_FIXED_FAILURE\n"
        else:
            raise
        if listener.fileno() != -1:
            raise SystemExit(81)
        if os.write(signal_fd, marker) != len(marker):
            raise SystemExit(82)
        os.close(signal_fd)
        raise SystemExit(0)
if mode == "default":
    os.write(signal_fd, b"DEFAULT_RETURNED\n")
    raise SystemExit(73)
if result is not None or listener.fileno() != -1:
    raise SystemExit(74)
os.write(signal_fd, b"SERVER_RETURNED\n")
os.close(signal_fd)
raise SystemExit(0)
"""


def _spawn_real_child(
    tmp_path: Path,
    *,
    mode: str,
) -> tuple[subprocess.Popen[bytes], int, int]:
    fixture_reader, fixture_writer = os.pipe()
    signal_reader, signal_writer = os.pipe()
    os.set_inheritable(fixture_reader, False)
    os.set_inheritable(fixture_writer, False)
    os.set_inheritable(signal_reader, False)
    os.set_inheritable(signal_writer, False)
    child_cwd = tmp_path / f"child-{mode}"
    child_cwd.mkdir()
    runtime_root = tmp_path / f"runtime-{mode}"
    command = (
        sys.executable,
        "-I",
        "-u",
        "-c",
        CHILD_SOURCE,
        mode,
        str(fixture_writer),
        str(signal_writer),
        str(runtime_root),
        str(Path(server_module.__file__).resolve()),
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=child_cwd,
            env=_isolated_environment(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(fixture_writer, signal_writer),
            start_new_session=True,
        )
    except BaseException:
        os.close(fixture_reader)
        os.close(fixture_writer)
        os.close(signal_reader)
        os.close(signal_writer)
        raise
    os.close(fixture_writer)
    os.close(signal_writer)
    return process, fixture_reader, signal_reader


def _receive_started_state(file_descriptor: int) -> SidecarState:
    encoded = _read_one_pipe_message(file_descriptor, timeout=CHILD_TIMEOUT_SECONDS)
    decoded = json.loads(encoded)
    assert type(decoded) is dict
    assert set(decoded) == {"kind", "state"}
    assert decoded["kind"] == "SERVER_STARTED"
    state = SidecarState.from_wire(decoded["state"])
    assert type(state) is SidecarState
    return state


def _http_exchange(
    state: SidecarState,
    *,
    path: str = "/internal/v1/health",
    host_header: str | None = None,
    token: str | None = None,
    method: str = "GET",
) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    connection = http.client.HTTPConnection(
        state.host,
        state.port,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    try:
        connection.connect()
        connection.putrequest(
            method,
            path,
            skip_host=True,
            skip_accept_encoding=True,
        )
        connection.putheader("Host", state.authority if host_header is None else host_header)
        connection.putheader(
            "Authorization",
            f"Bearer {state.token if token is None else token}",
        )
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read(HTTP_BODY_LIMIT + 1)
        assert len(body) <= HTTP_BODY_LIMIT
        return response.status, tuple(response.getheaders()), body
    finally:
        connection.close()


def _assert_private_headers(headers: tuple[tuple[str, str], ...]) -> None:
    names = {name.lower() for name, _value in headers}
    assert "server" not in names
    assert "date" not in names
    assert not any(name.startswith("access-control-") for name in names)


def _assert_health(state: SidecarState) -> tuple[tuple[str, str], ...]:
    status, headers, body = _http_exchange(state)
    assert status == 200
    assert json.loads(body) == {
        "status": "ok",
        "protocol_version": state.protocol_version,
        "state_schema_version": state.state_schema_version,
        "project_id": state.project_id,
        "startup_id": state.startup_id,
        "sidecar_pid": state.pid,
        "host": state.host,
        "port": state.port,
    }
    _assert_private_headers(headers)
    response_text = repr(headers) + body.decode("utf-8")
    assert state.token not in response_text
    assert state.database_path not in response_text
    return headers


def _assert_rejected(
    state: SidecarState,
    *,
    host_header: str | None = None,
    token: str | None = None,
    status: int,
    code: str,
) -> None:
    observed_status, headers, body = _http_exchange(
        state,
        host_header=host_header,
        token=token,
    )
    assert observed_status == status
    assert json.loads(body) == {"detail": {"code": code}}
    _assert_private_headers(headers)
    response_text = repr(headers) + body.decode("utf-8")
    assert state.token not in response_text
    assert state.database_path not in response_text


def _assert_child_streams_empty(stdout: bytes, stderr: bytes, state: SidecarState) -> None:
    assert stdout == b""
    assert stderr == b""
    combined = stdout + stderr
    assert state.token.encode() not in combined
    assert state.database_path.encode() not in combined


def _assert_port_rebinds(port: int) -> None:
    replacement = bind_loopback_listener(port)
    try:
        assert replacement.getsockname() == ("127.0.0.1", port)
    finally:
        replacement.close()


def test_public_surface_signature_and_exact_type_order(tmp_path: Path) -> None:
    assert sidecar_package.serve_prebound_sidecar_app is serve_prebound_sidecar_app
    assert sidecar_package.__all__.count("serve_prebound_sidecar_app") == 1
    assert server_module.serve_prebound_sidecar_app is serve_prebound_sidecar_app

    signature = inspect.signature(serve_prebound_sidecar_app)
    assert tuple(signature.parameters) == ("state", "listener", "on_started")
    assert signature.parameters["state"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["listener"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["on_started"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["on_started"].default is inspect.Parameter.empty
    assert signature.return_annotation == "None"
    assert get_type_hints(serve_prebound_sidecar_app) == {
        "state": SidecarState,
        "listener": socket.socket,
        "on_started": Callable[[], None],
        "return": type(None),
    }

    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    derived_state = object.__new__(_DerivedState)
    wrong_states = (object(), derived_state, _ExplodingProxy())
    try:
        for wrong_state in wrong_states:
            incumbent = _ExplodingProxy()
            hook = _ExplodingProxy()
            with pytest.raises(TypeError) as captured:
                serve_prebound_sidecar_app(
                    wrong_state,  # type: ignore[arg-type]
                    incumbent,  # type: ignore[arg-type]
                    on_started=hook,  # type: ignore[arg-type]
                )
            _assert_fixed_error(captured.value, TypeError, STATE_TYPE_ERROR)
            _assert_frames_hide(captured.value, wrong_state, incumbent, hook)

        wrong_listener = _ExplodingProxy()
        hook = _ExplodingProxy()
        with pytest.raises(TypeError) as captured:
            serve_prebound_sidecar_app(
                state,
                wrong_listener,  # type: ignore[arg-type]
                on_started=hook,  # type: ignore[arg-type]
            )
        _assert_fixed_error(captured.value, TypeError, LISTENER_TYPE_ERROR)
        _assert_frames_hide(captured.value, state, wrong_listener, hook)
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()

    derived_listener = _DerivedSocket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(TypeError) as captured:
            serve_prebound_sidecar_app(state, derived_listener, on_started=_started)
        _assert_fixed_error(captured.value, TypeError, LISTENER_TYPE_ERROR)
        _assert_frames_hide(captured.value, state, derived_listener)
        assert derived_listener.fileno() >= 3
    finally:
        derived_listener.close()


def test_listener_and_hook_type_checks_do_not_read_later_inputs(tmp_path: Path) -> None:
    _store, listener, state = _live_pair(tmp_path)
    malformed_state = _forge_state(state, missing="pid")
    wrong_listener = _ExplodingProxy()
    wrong_hook = _ExplodingProxy()
    try:
        with pytest.raises(TypeError) as captured_listener:
            serve_prebound_sidecar_app(
                malformed_state,
                wrong_listener,  # type: ignore[arg-type]
                on_started=wrong_hook,  # type: ignore[arg-type]
            )
        _assert_fixed_error(captured_listener.value, TypeError, LISTENER_TYPE_ERROR)

        with pytest.raises(TypeError) as captured_hook:
            serve_prebound_sidecar_app(
                malformed_state,
                listener,
                on_started=wrong_hook,  # type: ignore[arg-type]
            )
        _assert_fixed_error(captured_hook.value, TypeError, HOOK_TYPE_ERROR)
        assert listener.fileno() >= 3
    finally:
        listener.close()


@pytest.mark.parametrize(
    "kind",
    ["object", "callable-object", "bound-method", "builtin", "proxy"],
    ids=["object", "callable-object", "bound-method", "builtin", "proxy"],
)
def test_invalid_startup_hook_type_fails_before_transfer(
    tmp_path: Path,
    kind: str,
) -> None:
    invalid_hook: object
    if kind == "object":
        invalid_hook = object()
    elif kind == "callable-object":
        invalid_hook = _CallableObject()
    elif kind == "bound-method":
        invalid_hook = _BoundHook().run
    elif kind == "builtin":
        invalid_hook = len
    else:
        invalid_hook = _ExplodingProxy()
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    try:
        with pytest.raises(TypeError) as captured:
            serve_prebound_sidecar_app(
                state,
                listener,
                on_started=invalid_hook,  # type: ignore[arg-type]
            )
        _assert_fixed_error(captured.value, TypeError, HOOK_TYPE_ERROR)
        _assert_frames_hide(captured.value, state, listener, invalid_hook)
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


@pytest.mark.parametrize(
    "invalid_hook",
    [_async_started, _generator_started, _async_generator_started],
    ids=["coroutine-function", "generator-function", "async-generator-function"],
)
def test_async_and_generator_hooks_fail_without_creating_unawaited_objects(
    tmp_path: Path,
    invalid_hook: Callable[[], object],
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    try:
        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter("always")
            with pytest.raises(TypeError) as captured:
                serve_prebound_sidecar_app(
                    state,
                    listener,
                    on_started=invalid_hook,  # type: ignore[arg-type]
                )
            gc.collect()
        _assert_fixed_error(captured.value, TypeError, HOOK_TYPE_ERROR)
        _assert_frames_hide(captured.value, state, listener, invalid_hook)
        assert captured_warnings == []
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


@pytest.mark.parametrize("field", ["pid", "host", "port"])
def test_malformed_state_identity_slots_fail_without_transfer(
    tmp_path: Path,
    field: str,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    malformed = _forge_state(state, missing=field)
    try:
        with pytest.raises(ValueError) as captured:
            serve_prebound_sidecar_app(malformed, listener, on_started=_started)
        _assert_fixed_error(captured.value, ValueError, COMPATIBILITY_ERROR)
        _assert_frames_hide(captured.value, malformed, listener)
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


@pytest.mark.parametrize(
    "replacement",
    [
        {"pid": True},
        {"pid": 0},
        {"host": "localhost"},
        {"host": object()},
        {"host": _TextSubclass("127.0.0.1")},
        {"port": True},
        {"port": 0},
        {"port": 1.0},
        {"port": _IntSubclass(4040)},
    ],
    ids=[
        "pid-bool",
        "pid-zero",
        "host-wrong",
        "host-object",
        "host-derived",
        "port-bool",
        "port-zero",
        "port-float",
        "port-derived",
    ],
)
def test_malformed_state_identity_values_fail_without_protocol_dispatch(
    tmp_path: Path,
    replacement: dict[str, object],
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    malformed = _forge_state(state, replacements=replacement)
    try:
        with pytest.raises(ValueError) as captured:
            serve_prebound_sidecar_app(malformed, listener, on_started=_started)
        _assert_fixed_error(captured.value, ValueError, COMPATIBILITY_ERROR)
        _assert_frames_hide(captured.value, malformed, listener, *replacement.values())
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


@pytest.mark.parametrize("mismatch", ["pid", "port", "inheritable"])
def test_valid_listener_compatibility_mismatch_remains_usable(
    tmp_path: Path,
    mismatch: str,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    if mismatch == "pid":
        state = _replace_state(state, pid=os.getpid() + 1)
    elif mismatch == "port":
        state = _replace_state(state, port=_wrong_port(state.port))
    else:
        listener.set_inheritable(True)
    before = _listener_snapshot(listener)
    try:
        with pytest.raises(ValueError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        _assert_fixed_error(captured.value, ValueError, COMPATIBILITY_ERROR)
        _assert_frames_hide(captured.value, state, listener)
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


def test_off_main_thread_fails_before_transfer(tmp_path: Path) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=invoke, name="p0-020-off-main", daemon=False)
    try:
        worker.start()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert len(errors) == 1
        _assert_fixed_error(errors[0], ValueError, COMPATIBILITY_ERROR)
        _assert_frames_hide(errors[0], state, listener)
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()
        worker.join(timeout=2.0)


@pytest.mark.parametrize("replacement", [None, True, object(), -1])
def test_malformed_frozen_main_thread_identity_fails_before_transfer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement: object,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    monkeypatch.setattr(server_module, "_MAIN_THREAD_IDENT", replacement)
    try:
        with pytest.raises(ValueError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        _assert_fixed_error(captured.value, ValueError, COMPATIBILITY_ERROR)
        _assert_frames_hide(captured.value, state, listener, replacement)
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


def test_running_loop_fails_without_unawaited_coroutine_warning(tmp_path: Path) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)

    async def invoke() -> BaseException:
        with pytest.raises(ValueError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        return captured.value

    try:
        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter("always")
            error = asyncio.run(invoke())
            gc.collect()
        _assert_fixed_error(error, ValueError, COMPATIBILITY_ERROR)
        _assert_frames_hide(error, state, listener)
        assert captured_warnings == []
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


def test_private_one_shot_notifier_calls_once_and_clears_reference() -> None:
    calls: list[tuple[object, ...]] = []

    def started() -> None:
        calls.append(())

    notifier = server_module._OneShotNotifier(started)
    assert type(notifier) is server_module._OneShotNotifier
    assert not hasattr(notifier, "__dict__")
    assert server_module._OneShotNotifier.__slots__ == ("_callback",)
    assert object.__getattribute__(notifier, "_callback") is started

    assert asyncio.run(notifier()) is None
    assert calls == [()]
    assert object.__getattribute__(notifier, "_callback") is None
    assert asyncio.run(notifier()) is None
    assert calls == [()]


@pytest.mark.parametrize(
    "result",
    [False, 0, 0.0, "", b"", (), [], {}, set()],
    ids=["false", "zero", "float", "text", "bytes", "tuple", "list", "dict", "set"],
)
def test_private_notifier_rejects_exact_builtin_non_none_and_stays_spent(
    result: object,
) -> None:
    calls = 0

    def started() -> object:
        nonlocal calls
        calls += 1
        return result

    notifier = server_module._OneShotNotifier(started)
    with pytest.raises(server_module._ServerFailure):
        asyncio.run(notifier())
    assert object.__getattribute__(notifier, "_callback") is None
    assert asyncio.run(notifier()) is None
    assert calls == 1


@pytest.mark.parametrize("control_type", [RuntimeError, KeyboardInterrupt, SystemExit, _Control])
def test_private_notifier_clears_reference_when_hook_raises(
    control_type: type[BaseException],
) -> None:
    control = control_type("private-hook-payload")
    control.add_note("caller-note")

    def started() -> NoReturn:
        raise control

    notifier = server_module._OneShotNotifier(started)
    with pytest.raises(control_type) as captured:
        asyncio.run(notifier())
    assert captured.value is control
    assert captured.value.__notes__ == ["caller-note"]
    assert object.__getattribute__(notifier, "_callback") is None
    assert asyncio.run(notifier()) is None


def test_config_constructor_receives_complete_frozen_keyword_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = object()
    notifier = server_module._OneShotNotifier(_started)
    canonical_config = server_module._CONFIG_CONSTRUCTOR
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    logger_snapshots = _snapshot_uvicorn_loggers()

    def construct(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return canonical_config(*args, **kwargs)

    monkeypatch.setattr(server_module, "_CONFIG_CONSTRUCTOR", construct)
    try:
        config = server_module._construct_config(app, 43210, notifier)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)
    assert type(config) is canonical_config
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (app,)
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 43210,
        "uds": None,
        "fd": None,
        "loop": "asyncio",
        "http": "h11",
        "ws": "none",
        "lifespan": "off",
        "interface": "asgi3",
        "env_file": None,
        "reload": False,
        "workers": 1,
        "proxy_headers": False,
        "forwarded_allow_ips": [],
        "server_header": False,
        "date_header": False,
        "access_log": False,
        "log_config": None,
        "log_level": "critical",
        "use_colors": False,
        "factory": False,
        "root_path": "",
        "backlog": 128,
        "limit_concurrency": 128,
        "limit_max_requests": None,
        "limit_max_requests_jitter": 0,
        "timeout_keep_alive": 5,
        "timeout_graceful_shutdown": 2,
        "timeout_notify": 30,
        "callback_notify": notifier,
        "headers": [],
        "h11_max_incomplete_event_size": 16384,
        "reset_contextvars": True,
    }
    assert type(kwargs["forwarded_allow_ips"]) is list
    assert type(kwargs["headers"]) is list
    assert kwargs["forwarded_allow_ips"] is not kwargs["headers"]


def test_real_config_attributes_ignore_malicious_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "47")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("UVICORN_RELOAD", "true")
    monkeypatch.setenv("UVICORN_LOG_LEVEL", "trace")
    app = object()
    notifier = server_module._OneShotNotifier(_started)
    logger_snapshots = _snapshot_uvicorn_loggers()

    try:
        config = server_module._construct_config(app, 43210, notifier)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)

    assert type(config) is server_module._CONFIG_TYPE
    assert config.app is app
    assert config.host == "127.0.0.1"
    assert config.port == 43210
    assert config.uds is None
    assert config.fd is None
    assert config.loop == "asyncio"
    assert config.http == "h11"
    assert config.ws == "none"
    assert config.lifespan == "off"
    assert config.interface == "asgi3"
    assert config.reload is False
    assert config.workers == 1
    assert config.proxy_headers is False
    assert config.forwarded_allow_ips == []
    assert type(config.forwarded_allow_ips) is list
    assert config.server_header is False
    assert config.date_header is False
    assert config.access_log is False
    assert config.log_config is None
    assert config.log_level == "critical"
    assert config.use_colors is False
    assert config.factory is False
    assert config.root_path == ""
    assert config.backlog == 128
    assert config.limit_concurrency == 128
    assert config.limit_max_requests is None
    assert config.limit_max_requests_jitter == 0
    assert config.timeout_keep_alive == 5
    assert config.timeout_graceful_shutdown == 2
    assert config.timeout_notify == 30
    assert config.callback_notify is notifier
    assert config.headers == []
    assert type(config.headers) is list
    assert config.headers is not config.forwarded_allow_ips
    assert config.h11_max_incomplete_event_size == 16384
    assert config.reset_contextvars is True


@pytest.mark.parametrize(
    "kind",
    ["closed", "unbound", "bound-not-listening", "udp", "ipv6", "wildcard"],
)
def test_malformed_listener_shapes_fail_before_transfer(
    tmp_path: Path,
    kind: str,
) -> None:
    candidate: socket.socket | None = None
    before: tuple[object, ...] | None = None
    try:
        if kind == "closed":
            _store, candidate, state = _live_pair(tmp_path)
            candidate.close()
        elif kind == "unbound":
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            state = _state_for_port(tmp_path, 4040)
        elif kind == "bound-not-listening":
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.bind(("127.0.0.1", 0))
            state = _state_for_port(tmp_path, int(candidate.getsockname()[1]))
        elif kind == "udp":
            candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            candidate.bind(("127.0.0.1", 0))
            state = _state_for_port(tmp_path, int(candidate.getsockname()[1]))
        elif kind == "ipv6":
            try:
                candidate = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            except OSError:
                pytest.skip("IPv6 socket allocation is unavailable")
            state = _state_for_port(tmp_path, 4040)
        else:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(("0.0.0.0", 0))
            candidate.listen(1)
            state = _state_for_port(tmp_path, int(candidate.getsockname()[1]))

        if candidate.fileno() >= 0:
            before = _listener_snapshot(candidate)
        with pytest.raises(ValueError) as captured:
            serve_prebound_sidecar_app(state, candidate, on_started=_started)
        _assert_fixed_error(captured.value, ValueError, COMPATIBILITY_ERROR)
        _assert_frames_hide(captured.value, state, candidate)
        if before is None:
            assert candidate.fileno() == -1
        else:
            _assert_usable_unchanged(candidate, before)
    finally:
        if candidate is not None:
            candidate.close()


@pytest.mark.parametrize(
    "dependency",
    [
        "_IS_COROUTINE_FUNCTION",
        "_IS_GENERATOR_FUNCTION",
        "_IS_ASYNC_GENERATOR_FUNCTION",
    ],
)
def test_function_kind_fault_is_fixed_and_does_not_transfer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dependency: str,
) -> None:
    _TrackedFailure.references.clear()
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    secret = f"private-function-kind-{dependency}"

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise _TrackedFailure(secret)

    monkeypatch.setattr(server_module, dependency, fail)
    try:
        with pytest.raises(TypeError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        _assert_fixed_error(captured.value, TypeError, HOOK_TYPE_ERROR)
        assert secret not in "".join(traceback.format_exception(captured.value))
        _assert_frames_hide(captured.value, state, listener)
        _assert_usable_unchanged(listener, before)
        gc.collect()
        assert _TrackedFailure.references
        assert all(reference() is None for reference in _TrackedFailure.references)
    finally:
        listener.close()


@pytest.mark.parametrize(
    "dependency",
    [
        "_IS_COROUTINE_FUNCTION",
        "_IS_GENERATOR_FUNCTION",
        "_IS_ASYNC_GENERATOR_FUNCTION",
    ],
)
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, _Control])
def test_function_kind_process_control_preserves_identity_without_transfer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dependency: str,
    control_type: type[BaseException],
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    control = control_type(f"function-kind-control-{dependency}")
    control.add_note("caller-note")
    close_events: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise control

    def unexpected_close(_listener: socket.socket) -> NoReturn:
        close_events.append("close")
        raise AssertionError("pre-transfer listener was closed")

    monkeypatch.setattr(server_module, dependency, fail)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", unexpected_close)
    try:
        with pytest.raises(control_type) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        assert captured.value is control
        assert captured.value.__notes__ == ["caller-note"]
        assert traceback.extract_tb(captured.value.__traceback__)[-1].name == "fail"
        _assert_frames_hide(captured.value, state, listener, _started)
        assert close_events == []
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


_PREFLIGHT_DEPENDENCIES = (
    "_STATE_PID_GETTER",
    "_STATE_HOST_GETTER",
    "_STATE_PORT_GETTER",
    "_GETPID",
    "_GET_IDENT",
    "_GET_RUNNING_LOOP",
    "_SOCKET_FILENO",
    "_SOCKET_FAMILY_GETTER",
    "_SOCKET_KIND_GETTER",
    "_SOCKET_PROTOCOL_GETTER",
    "_SOCKET_GETSOCKNAME",
    "_GET_INHERITABLE",
    "_SOCKET_GETSOCKOPT",
    "_INT_EQUAL",
)


@pytest.mark.parametrize("dependency", _PREFLIGHT_DEPENDENCIES)
def test_each_preflight_ordinary_fault_becomes_fixed_incompatibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dependency: str,
) -> None:
    _TrackedFailure.references.clear()
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    secret = f"private-preflight-{dependency}"
    later_events: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise _TrackedFailure(secret)

    def unexpected_app(_state: SidecarState) -> NoReturn:
        later_events.append("app")
        raise AssertionError("serving began after failed preflight")

    monkeypatch.setattr(server_module, dependency, fail)
    monkeypatch.setattr(server_module, "_CREATE_SIDECAR_APP", unexpected_app)
    try:
        with pytest.raises(ValueError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        _assert_fixed_error(captured.value, ValueError, COMPATIBILITY_ERROR)
        assert secret not in "".join(traceback.format_exception(captured.value))
        _assert_frames_hide(captured.value, state, listener)
        assert later_events == []
        _assert_usable_unchanged(listener, before)
        gc.collect()
        assert _TrackedFailure.references
        assert all(reference() is None for reference in _TrackedFailure.references)
    finally:
        listener.close()


@pytest.mark.parametrize("dependency", _PREFLIGHT_DEPENDENCIES)
def test_each_preflight_process_control_preserves_identity_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dependency: str,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    before = _listener_snapshot(listener)
    control = _Control(f"preflight-control-{dependency}")
    control.add_note("caller-note")
    later_events: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise control

    def unexpected_close(_listener: socket.socket) -> NoReturn:
        later_events.append("close")
        raise AssertionError("pre-transfer listener was closed")

    monkeypatch.setattr(server_module, dependency, fail)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", unexpected_close)
    try:
        with pytest.raises(_Control) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
        assert captured.value is control
        assert captured.value.__notes__ == ["caller-note"]
        assert traceback.extract_tb(captured.value.__traceback__)[-1].name == "fail"
        _assert_frames_hide(captured.value, state, listener)
        assert later_events == []
        _assert_usable_unchanged(listener, before)
    finally:
        listener.close()


def test_canonical_pipeline_order_same_socket_and_captured_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _TrackedFailure.references.clear()
    _store, listener, state = _live_pair(tmp_path)
    canonical_app = server_module._CREATE_SIDECAR_APP
    canonical_config = server_module._CONFIG_CONSTRUCTOR
    canonical_server = server_module._SERVER_CONSTRUCTOR
    canonical_close = server_module._SOCKET_CLOSE
    logger_snapshots = _snapshot_uvicorn_loggers()
    events: list[str] = []
    observed_sockets: list[list[socket.socket]] = []
    secret = "private-canonical-pipeline-failure"

    def create_app(observed_state: SidecarState) -> object:
        assert observed_state is state
        events.append("app")
        return canonical_app(observed_state)

    def construct_config(*args: object, **kwargs: object) -> object:
        events.append("config")
        return canonical_config(*args, **kwargs)

    def construct_server(config: object) -> object:
        assert type(config) is server_module._CONFIG_TYPE
        events.append("server")
        return canonical_server(config)  # type: ignore[arg-type]

    def fail_run(server: object, *, sockets: list[socket.socket]) -> NoReturn:
        assert type(server) is server_module._SERVER_TYPE
        assert type(sockets) is list
        assert len(sockets) == 1
        assert sockets[0] is listener
        observed_sockets.append(sockets)
        events.append("run")
        raise _TrackedFailure(secret)

    def close(observed_listener: socket.socket) -> object:
        assert observed_listener is listener
        events.append("close")
        return canonical_close(observed_listener)

    def public_replacement(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("public dependency replacement was used")

    monkeypatch.setattr(server_module, "_CREATE_SIDECAR_APP", create_app)
    monkeypatch.setattr(server_module, "_CONFIG_CONSTRUCTOR", construct_config)
    monkeypatch.setattr(server_module, "_SERVER_CONSTRUCTOR", construct_server)
    monkeypatch.setattr(server_module, "_SERVER_RUN", fail_run)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", close)
    monkeypatch.setattr(server_module, "create_sidecar_app", public_replacement)
    monkeypatch.setattr(server_module, "Config", public_replacement)
    monkeypatch.setattr(server_module, "Server", public_replacement)
    caplog.clear()
    try:
        with pytest.raises(RuntimeError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)

    _assert_fixed_error(captured.value, RuntimeError, SERVER_ERROR)
    _assert_frames_hide(captured.value, state, listener)
    _assert_no_sensitive_text(captured.value, state)
    assert events == ["app", "config", "server", "run", "close"]
    assert len(observed_sockets) == 1
    assert observed_sockets[0] == [listener]
    assert listener.fileno() == -1
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []
    assert secret not in "".join(traceback.format_exception(captured.value))
    observed_sockets.clear()
    gc.collect()
    assert _TrackedFailure.references
    assert all(reference() is None for reference in _TrackedFailure.references)
    _assert_port_rebinds(state.port)


@pytest.mark.parametrize(
    "dependency",
    [
        "_CREATE_SIDECAR_APP",
        "_CONFIG_CONSTRUCTOR",
        "_SERVER_CONSTRUCTOR",
        "_SERVER_RUN",
        "_SOCKET_CLOSE",
    ],
)
def test_each_post_transfer_ordinary_fault_is_fixed_closed_and_released(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dependency: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _TrackedFailure.references.clear()
    _store, listener, state = _live_pair(tmp_path)
    canonical_close = server_module._SOCKET_CLOSE
    logger_snapshots = _snapshot_uvicorn_loggers()
    secret = f"private-post-transfer-{dependency}"
    calls: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append(dependency)
        raise _TrackedFailure(secret)

    def close(observed_listener: socket.socket) -> object:
        assert observed_listener is listener
        calls.append("close")
        return canonical_close(observed_listener)

    monkeypatch.setattr(server_module, dependency, fail)
    if dependency == "_SOCKET_CLOSE":

        def malformed_run(_server: object, *, sockets: list[socket.socket]) -> object:
            assert type(sockets) is list and sockets == [listener]
            return object()

        monkeypatch.setattr(server_module, "_SERVER_RUN", malformed_run)
    else:
        monkeypatch.setattr(server_module, "_SOCKET_CLOSE", close)
    caplog.clear()
    try:
        with pytest.raises(RuntimeError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)

    _assert_fixed_error(captured.value, RuntimeError, SERVER_ERROR)
    _assert_frames_hide(captured.value, state, listener)
    _assert_no_sensitive_text(captured.value, state)
    assert calls.count(dependency) == 1
    if dependency == "_SOCKET_CLOSE":
        assert listener.fileno() >= 3
        listener.close()
    else:
        assert calls.count("close") == 1
        assert listener.fileno() == -1
        _assert_port_rebinds(state.port)
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []
    assert secret not in "".join(traceback.format_exception(captured.value))
    gc.collect()
    assert _TrackedFailure.references
    assert all(reference() is None for reference in _TrackedFailure.references)


@pytest.mark.parametrize(
    "dependency",
    [
        "_CREATE_SIDECAR_APP",
        "_CONFIG_CONSTRUCTOR",
        "_SERVER_CONSTRUCTOR",
        "_SERVER_RUN",
        "_SOCKET_CLOSE",
    ],
)
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, _Control])
def test_each_post_transfer_process_control_preserves_identity_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dependency: str,
    control_type: type[BaseException],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    canonical_close = server_module._SOCKET_CLOSE
    logger_snapshots = _snapshot_uvicorn_loggers()
    control = control_type(f"post-transfer-control-{dependency}")
    control.add_note("caller-note")
    calls: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append(dependency)
        raise control

    def close(observed_listener: socket.socket) -> object:
        assert observed_listener is listener
        calls.append("close")
        return canonical_close(observed_listener)

    monkeypatch.setattr(server_module, dependency, fail)
    if dependency == "_SOCKET_CLOSE":

        def malformed_run(_server: object, *, sockets: list[socket.socket]) -> object:
            assert type(sockets) is list and sockets == [listener]
            return object()

        monkeypatch.setattr(server_module, "_SERVER_RUN", malformed_run)
    else:
        monkeypatch.setattr(server_module, "_SOCKET_CLOSE", close)
    try:
        with pytest.raises(control_type) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)

    assert captured.value is control
    assert captured.value.__notes__ == ["caller-note"]
    assert traceback.extract_tb(captured.value.__traceback__)[-1].name == "fail"
    _assert_frames_hide(captured.value, state, listener)
    assert calls.count(dependency) == 1
    if dependency == "_SOCKET_CLOSE":
        assert listener.fileno() >= 3
        listener.close()
    else:
        assert calls.count("close") == 1
        assert listener.fileno() == -1
        _assert_port_rebinds(state.port)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "cleanup_outcome",
    ["ordinary", "control", "close-then-error"],
)
def test_cleanup_failure_cannot_replace_active_process_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_outcome: str,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    canonical_close = server_module._SOCKET_CLOSE
    logger_snapshots = _snapshot_uvicorn_loggers()
    active = _Control("active-process-control")
    active.add_note("caller-note")
    cleanup_control = KeyboardInterrupt("cleanup-process-control")
    calls: list[str] = []

    def fail_run(_server: object, *, sockets: list[socket.socket]) -> NoReturn:
        assert type(sockets) is list and sockets == [listener]
        calls.append("run")
        raise active

    def fail_close(observed_listener: socket.socket) -> object:
        assert observed_listener is listener
        calls.append("close")
        if cleanup_outcome == "ordinary":
            raise OSError("private-cleanup-error")
        if cleanup_outcome == "control":
            raise cleanup_control
        canonical_close(observed_listener)
        raise OSError("private-ambiguous-cleanup-error")

    canonical_add_note = server_module._ADD_NOTE

    def add_note(error: BaseException, note: str) -> None:
        assert error is active
        assert note == "prebound sidecar server cleanup failed"
        calls.append("note")
        canonical_add_note(error, note)

    monkeypatch.setattr(server_module, "_SERVER_RUN", fail_run)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", fail_close)
    monkeypatch.setattr(server_module, "_ADD_NOTE", add_note)
    try:
        with pytest.raises(_Control) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)

    assert captured.value is active
    assert captured.value.__notes__ == [
        "caller-note",
        "prebound sidecar server cleanup failed",
    ]
    assert calls == ["run", "close", "note"]
    _assert_frames_hide(captured.value, state, listener)
    if cleanup_outcome == "close-then-error":
        assert listener.fileno() == -1
        _assert_port_rebinds(state.port)
    else:
        assert listener.fileno() >= 3
        listener.close()


@pytest.mark.parametrize("note_control_type", [KeyboardInterrupt, SystemExit, _Control])
def test_add_note_process_control_cannot_replace_active_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    note_control_type: type[BaseException],
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    logger_snapshots = _snapshot_uvicorn_loggers()
    active = _Control("active-process-control")
    active.add_note("caller-note")
    note_control = note_control_type("add-note-process-control")
    note_control.add_note("add-note-caller-note")
    calls: list[str] = []

    def fail_run(_server: object, *, sockets: list[socket.socket]) -> NoReturn:
        assert type(sockets) is list and sockets == [listener]
        calls.append("run")
        raise active

    def fail_close(observed_listener: socket.socket) -> NoReturn:
        assert observed_listener is listener
        calls.append("close")
        raise OSError("private-cleanup-error")

    def fail_add_note(error: BaseException, note: str) -> NoReturn:
        assert error is active
        assert note == "prebound sidecar server cleanup failed"
        calls.append("note")
        raise note_control

    monkeypatch.setattr(server_module, "_SERVER_RUN", fail_run)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", fail_close)
    monkeypatch.setattr(server_module, "_ADD_NOTE", fail_add_note)
    try:
        with pytest.raises(_Control) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)

    assert captured.value is active
    assert captured.value.__notes__ == ["caller-note"]
    assert note_control.__notes__ == ["add-note-caller-note"]
    assert calls == ["run", "close", "note"]
    _assert_frames_hide(captured.value, state, listener, note_control)
    assert listener.fileno() >= 3
    listener.close()


@pytest.mark.parametrize("run_result", [False, 0, "", [], {}, object()])
def test_non_none_server_run_result_fails_closed_after_one_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_result: object,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    canonical_close = server_module._SOCKET_CLOSE
    logger_snapshots = _snapshot_uvicorn_loggers()
    calls: list[str] = []

    def malformed_run(_server: object, *, sockets: list[socket.socket]) -> object:
        assert type(sockets) is list and sockets == [listener]
        calls.append("run")
        return run_result

    def close(observed_listener: socket.socket) -> object:
        assert observed_listener is listener
        calls.append("close")
        return canonical_close(observed_listener)

    monkeypatch.setattr(server_module, "_SERVER_RUN", malformed_run)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", close)
    try:
        with pytest.raises(RuntimeError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)

    _assert_fixed_error(captured.value, RuntimeError, SERVER_ERROR)
    _assert_frames_hide(captured.value, state, listener, run_result)
    assert calls == ["run", "close"]
    assert listener.fileno() == -1
    _assert_port_rebinds(state.port)


def test_owned_guard_covers_control_before_first_runtime_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    canonical_close = server_module._SOCKET_CLOSE
    control = _Control("owned-entry-control")
    control.add_note("caller-note")
    calls: list[str] = []

    def fail_before_runtime(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append("run-helper")
        raise control

    def close(observed_listener: socket.socket) -> object:
        assert observed_listener is listener
        calls.append("close")
        return canonical_close(observed_listener)

    monkeypatch.setattr(server_module, "_run_server", fail_before_runtime)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", close)
    with pytest.raises(_Control) as captured:
        serve_prebound_sidecar_app(state, listener, on_started=_started)

    assert captured.value is control
    assert captured.value.__notes__ == ["caller-note"]
    assert calls == ["run-helper", "close"]
    assert listener.fileno() == -1
    _assert_frames_hide(captured.value, state, listener)
    _assert_port_rebinds(state.port)


@pytest.mark.parametrize("caller_type", [ValueError, KeyboardInterrupt])
def test_caller_active_context_is_preserved_on_fixed_type_failure(
    tmp_path: Path,
    caller_type: type[BaseException],
) -> None:
    _store, listener, _state = _live_pair(tmp_path)
    caller_error = caller_type("caller-private-context")
    caller_error.add_note("caller-note")
    try:
        try:
            raise caller_error
        except caller_type:
            with pytest.raises(TypeError) as captured:
                serve_prebound_sidecar_app(
                    object(),  # type: ignore[arg-type]
                    listener,
                    on_started=_started,
                )
        _assert_fixed_error(
            captured.value,
            TypeError,
            STATE_TYPE_ERROR,
            context=caller_error,
        )
        assert caller_error.__notes__ == ["caller-note"]
        assert "caller-private-context" not in "".join(traceback.format_exception(captured.value))
        assert listener.fileno() >= 3
    finally:
        listener.close()


@pytest.mark.parametrize("caller_type", [ValueError, KeyboardInterrupt])
def test_caller_active_context_is_preserved_on_fixed_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caller_type: type[BaseException],
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    canonical_close = server_module._SOCKET_CLOSE
    caller_error = caller_type("caller-private-context")
    caller_error.add_note("caller-note")

    def fail_app(_state: SidecarState) -> NoReturn:
        raise OSError("raw-private-dependency-error")

    def close(observed_listener: socket.socket) -> object:
        return canonical_close(observed_listener)

    monkeypatch.setattr(server_module, "_CREATE_SIDECAR_APP", fail_app)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", close)
    try:
        raise caller_error
    except caller_type:
        with pytest.raises(RuntimeError) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)

    _assert_fixed_error(
        captured.value,
        RuntimeError,
        SERVER_ERROR,
        context=caller_error,
    )
    assert caller_error.__notes__ == ["caller-note"]
    formatted = "".join(traceback.format_exception(captured.value))
    assert "caller-private-context" not in formatted
    assert "raw-private-dependency-error" not in formatted
    assert listener.fileno() == -1
    _assert_port_rebinds(state.port)


@pytest.mark.parametrize("caller_type", [ValueError, KeyboardInterrupt])
def test_caller_context_and_dependency_control_both_preserve_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caller_type: type[BaseException],
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    canonical_close = server_module._SOCKET_CLOSE
    caller_error = caller_type("caller-private-context")
    caller_error.add_note("caller-note")
    control = _Control("dependency-control")
    control.add_note("control-note")

    def fail_app(_state: SidecarState) -> NoReturn:
        raise control

    def close(observed_listener: socket.socket) -> object:
        return canonical_close(observed_listener)

    monkeypatch.setattr(server_module, "_CREATE_SIDECAR_APP", fail_app)
    monkeypatch.setattr(server_module, "_SOCKET_CLOSE", close)
    try:
        raise caller_error
    except caller_type:
        with pytest.raises(_Control) as captured:
            serve_prebound_sidecar_app(state, listener, on_started=_started)

    assert captured.value is control
    assert captured.value.__context__ is caller_error
    assert captured.value.__notes__ == ["control-note"]
    assert caller_error.__notes__ == ["caller-note"]
    _assert_frames_hide(captured.value, state, listener)
    assert listener.fileno() == -1
    _assert_port_rebinds(state.port)


def test_live_public_alias_constant_cast_and_enum_replacements_cannot_redirect_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    logger_snapshots = _snapshot_uvicorn_loggers()
    events: list[str] = []
    assert type(server_module._AF_INET) is int
    assert type(server_module._SOCK_STREAM) is int
    assert type(server_module._MAIN_THREAD_IDENT) is int
    assert server_module._GET_IDENT() == server_module._MAIN_THREAD_IDENT
    assert type(server_module._SOCKET_FAMILY_GETTER(listener, server_module._SOCKET_TYPE)) is int
    assert type(server_module._SOCKET_KIND_GETTER(listener, server_module._SOCKET_TYPE)) is int

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        events.append("replacement")
        raise AssertionError("live public replacement was used")

    def fail_run(server: object, *, sockets: list[socket.socket]) -> NoReturn:
        assert type(server) is server_module._SERVER_TYPE
        assert server.config.host == "127.0.0.1"  # type: ignore[union-attr]
        assert server.config.port == state.port  # type: ignore[union-attr]
        assert type(sockets) is list and sockets == [listener]
        events.append("run")
        raise OSError("expected terminating fault")

    monkeypatch.setattr(server_module, "_SERVER_RUN", fail_run)
    try:
        with monkeypatch.context() as replacements:
            replacements.setattr(server_module, "create_sidecar_app", unexpected)
            replacements.setattr(server_module, "Config", unexpected)
            replacements.setattr(server_module, "Server", unexpected)
            replacements.setattr(server_module, "LOOPBACK_HOST", "203.0.113.77")
            replacements.setattr(server_module, "cast", unexpected, raising=False)
            replacements.setattr(sidecar_package, "create_sidecar_app", unexpected)
            replacements.setattr(server_module.socket, "socket", unexpected)
            replacements.setattr(server_module.socket, "AF_INET", object())
            replacements.setattr(server_module.socket, "SOCK_STREAM", object())
            replacements.setattr(server_module.socket, "IPPROTO_TCP", object())
            replacements.setattr(server_module.socket, "SOL_SOCKET", object())
            replacements.setattr(server_module.socket, "SO_ACCEPTCONN", object())
            replacements.setattr(server_module.socket, "_intenum_converter", unexpected)
            replacements.setattr(server_module.socket, "AddressFamily", unexpected)
            replacements.setattr(server_module.socket, "SocketKind", unexpected)
            replacements.setattr(server_module.threading, "get_ident", unexpected)
            replacements.setattr(server_module.threading, "current_thread", unexpected)
            replacements.setattr(server_module.threading, "main_thread", unexpected)
            with pytest.raises(RuntimeError) as captured:
                serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)
        if listener.fileno() >= 0:
            listener.close()

    _assert_fixed_error(captured.value, RuntimeError, SERVER_ERROR)
    assert events == ["run"]
    assert listener.fileno() == -1
    _assert_port_rebinds(state.port)


def test_physical_close_capture_ignores_later_socket_real_close_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _store, listener, state = _live_pair(tmp_path)
    logger_snapshots = _snapshot_uvicorn_loggers()
    replacement_calls: list[socket.socket] = []
    original_real_close = socket.socket.__dict__["_real_close"]

    def fail_run(_server: object, *, sockets: list[socket.socket]) -> NoReturn:
        assert type(sockets) is list and sockets == [listener]
        raise OSError("expected terminating fault")

    def replacement_close(observed_listener: socket.socket) -> NoReturn:
        replacement_calls.append(observed_listener)
        raise AssertionError("late socket._real_close replacement was used")

    class ReplacementSocketBase:
        @staticmethod
        def close(observed_listener: socket.socket) -> NoReturn:
            replacement_calls.append(observed_listener)
            raise AssertionError("mutated _real_close default was used")

    monkeypatch.setattr(server_module, "_SERVER_RUN", fail_run)
    try:
        with monkeypatch.context() as replacement:
            replacement.setattr(socket.socket, "_real_close", replacement_close)
            replacement.setattr(original_real_close, "__defaults__", (ReplacementSocketBase,))
            with pytest.raises(RuntimeError) as captured:
                serve_prebound_sidecar_app(state, listener, on_started=_started)
    finally:
        _restore_uvicorn_loggers(logger_snapshots)
        if listener.fileno() >= 0:
            listener.close()

    _assert_fixed_error(captured.value, RuntimeError, SERVER_ERROR)
    assert server_module._SOCKET_CLOSE is server_module._SOCKET_BASE_TYPE.__dict__["close"]
    assert type(server_module._SOCKET_CLOSE).__name__ == "method_descriptor"
    assert replacement_calls == []
    assert listener.fileno() == -1
    _assert_port_rebinds(state.port)


def test_real_prebound_server_auth_socket_identity_and_custom_signal(tmp_path: Path) -> None:
    process, fixture_reader, signal_reader = _spawn_real_child(tmp_path, mode="custom")
    state: SidecarState | None = None
    try:
        try:
            state = _receive_started_state(fixture_reader)
        finally:
            os.close(fixture_reader)
        assert state.pid == process.pid
        _assert_health(state)
        _assert_rejected(
            state,
            token="wrong-token",
            status=401,
            code="AUTH_REJECTED",
        )
        _assert_health(state)
        _assert_rejected(
            state,
            host_header=f"localhost:{state.port}",
            status=403,
            code="HOST_REJECTED",
        )
        _assert_health(state)
        status, headers, body = _http_exchange(state, path="/docs")
        assert status == 404
        assert json.loads(body) == {"detail": "Not Found"}
        _assert_private_headers(headers)
        assert state.token not in (repr(headers) + body.decode("utf-8"))
        assert state.database_path not in (repr(headers) + body.decode("utf-8"))

        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        stdout, stderr = process.communicate(timeout=CHILD_REAP_SECONDS)
        signal_message = _read_pipe_to_eof(signal_reader)
        os.close(signal_reader)

        assert process.returncode == 0
        assert signal_message == b"SIGTERM_REPLAYED\nSERVER_RETURNED\n"
        assert not _process_group_exists(process.pid)
        _assert_child_streams_empty(stdout, stderr, state)
        _assert_port_rebinds(state.port)
    except BaseException:
        try:
            os.close(signal_reader)
        except OSError:
            pass
        stdout, stderr, _escalated = _terminate_and_reap(process)
        if state is not None:
            _assert_child_streams_empty(stdout, stderr, state)
        raise


@pytest.mark.parametrize(
    ("mode", "expected_marker"),
    [
        ("hook-ordinary", b"HOOK_FIXED_FAILURE\n"),
        ("hook-non-none", b"HOOK_FIXED_FAILURE\n"),
        ("hook-control", b"HOOK_CONTROL_PRESERVED\n"),
    ],
)
def test_real_startup_hook_failure_paths_close_and_preserve_contract(
    tmp_path: Path,
    mode: str,
    expected_marker: bytes,
) -> None:
    process, fixture_reader, signal_reader = _spawn_real_child(tmp_path, mode=mode)
    state: SidecarState | None = None
    try:
        try:
            state = _receive_started_state(fixture_reader)
        finally:
            os.close(fixture_reader)
        assert state.pid == process.pid
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        stdout, stderr = process.communicate(timeout=CHILD_REAP_SECONDS)
        signal_message = _read_pipe_to_eof(signal_reader)
        os.close(signal_reader)

        assert process.returncode == 0
        assert signal_message == expected_marker
        assert not _process_group_exists(process.pid)
        _assert_child_streams_empty(stdout, stderr, state)
        _assert_port_rebinds(state.port)
    except BaseException:
        try:
            os.close(signal_reader)
        except OSError:
            pass
        stdout, stderr, _escalated = _terminate_and_reap(process)
        if state is not None:
            _assert_child_streams_empty(stdout, stderr, state)
        raise


def test_real_canonical_none_run_makes_close_fault_the_only_fixed_failure(
    tmp_path: Path,
) -> None:
    process, fixture_reader, signal_reader = _spawn_real_child(
        tmp_path,
        mode="close-raise-after-close",
    )
    state: SidecarState | None = None
    try:
        try:
            state = _receive_started_state(fixture_reader)
        finally:
            os.close(fixture_reader)
        assert state.pid == process.pid
        _assert_health(state)

        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        stdout, stderr = process.communicate(timeout=CHILD_REAP_SECONDS)
        signal_message = _read_pipe_to_eof(signal_reader)
        os.close(signal_reader)

        assert process.returncode == 0
        assert signal_message == b"SIGTERM_REPLAYED\nCLOSE_FIXED_FAILURE\n"
        assert not _process_group_exists(process.pid)
        _assert_child_streams_empty(stdout, stderr, state)
        _assert_port_rebinds(state.port)
    except BaseException:
        try:
            os.close(signal_reader)
        except OSError:
            pass
        stdout, stderr, _escalated = _terminate_and_reap(process)
        if state is not None:
            _assert_child_streams_empty(stdout, stderr, state)
        raise


def test_real_default_sigterm_with_stalled_request_is_bounded_and_reaped(
    tmp_path: Path,
) -> None:
    process, fixture_reader, signal_reader = _spawn_real_child(tmp_path, mode="default")
    state: SidecarState | None = None
    stalled: socket.socket | None = None
    try:
        try:
            state = _receive_started_state(fixture_reader)
        finally:
            os.close(fixture_reader)
        assert state.pid == process.pid
        _assert_health(state)

        stalled = socket.create_connection(
            (state.host, state.port),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        stalled.settimeout(HTTP_TIMEOUT_SECONDS)
        request = (
            "POST /internal/v1/health HTTP/1.1\r\n"
            f"Host: {state.authority}\r\n"
            f"Authorization: Bearer {state.token}\r\n"
            "Content-Length: 64\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
            "x"
        ).encode("ascii")
        stalled.sendall(request)

        started = time.monotonic()
        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        elapsed = time.monotonic() - started
        stdout, stderr = process.communicate(timeout=CHILD_REAP_SECONDS)
        signal_message = _read_pipe_to_eof(signal_reader)
        os.close(signal_reader)

        assert elapsed < CHILD_TIMEOUT_SECONDS
        assert process.returncode == -signal.SIGTERM
        assert signal_message == b""
        assert not _process_group_exists(process.pid)
        _assert_child_streams_empty(stdout, stderr, state)
        stalled.close()
        stalled = None
        _assert_port_rebinds(state.port)
    except BaseException:
        if stalled is not None:
            stalled.close()
        try:
            os.close(signal_reader)
        except OSError:
            pass
        stdout, stderr, _escalated = _terminate_and_reap(process)
        if state is not None:
            _assert_child_streams_empty(stdout, stderr, state)
        raise


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return "<dynamic>"


def test_server_runtime_source_has_exact_positive_ast_allowlist() -> None:
    production_path = Path(server_module.__file__).resolve()
    assert production_path == REPOSITORY_ROOT / "flowsight" / "sidecar" / "server_runtime.py"
    source = production_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    def import_signature(
        node: ast.Import | ast.ImportFrom,
    ) -> tuple[str, int, str | None, tuple[tuple[str, str | None], ...]]:
        return (
            "import" if isinstance(node, ast.Import) else "from",
            0 if isinstance(node, ast.Import) else node.level,
            None if isinstance(node, ast.Import) else node.module,
            tuple((alias.name, alias.asname) for alias in node.names),
        )

    top_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    ] == top_level_imports
    assert [import_signature(node) for node in top_level_imports] == [
        ("from", 0, "__future__", (("annotations", None),)),
        ("import", 0, None, (("asyncio", None),)),
        ("import", 0, None, (("inspect", None),)),
        ("import", 0, None, (("os", None),)),
        ("import", 0, None, (("socket", None),)),
        ("import", 0, None, (("sys", None),)),
        ("import", 0, None, (("threading", None),)),
        ("from", 0, "collections.abc", (("Callable", None),)),
        ("from", 0, "types", (("FunctionType", None),)),
        (
            "from",
            0,
            "typing",
            (("Any", None), ("Final", None), ("NoReturn", None), ("cast", None)),
        ),
        ("from", 0, "uvicorn", (("Config", None), ("Server", None))),
        ("from", 1, "app", (("create_sidecar_app", None),)),
        ("from", 1, "state", (("LOOPBACK_HOST", None), ("SidecarState", None))),
    ]

    type_aliases = [node for node in tree.body if isinstance(node, ast.TypeAlias)]
    assert len(type_aliases) == 1
    assert ast.dump(type_aliases[0], include_attributes=False) == ast.dump(
        ast.parse("type _BoundSlotGetter = Callable[[object, type[object]], object]").body[0],
        include_attributes=False,
    )
    assert getattr(type_aliases[0], "type_params", []) == []

    top_level_definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert {node.name for node in top_level_definitions} == {
        "_CompatibilityFailure",
        "_ServerFailure",
        "_OneShotNotifier",
        "_raise_state_type",
        "_raise_listener_type",
        "_raise_hook_type",
        "_raise_incompatible",
        "_raise_server_failure",
        "_is_sync_function",
        "_no_running_loop",
        "_listener_is_accepting",
        "_preflight",
        "_construct_config",
        "_run_server",
        "_close_listener",
        "_close_during_control",
        "_serve_owned",
        "serve_prebound_sidecar_app",
    }
    assert tuple(node.name for node in top_level_definitions) == (
        "_CompatibilityFailure",
        "_ServerFailure",
        "_OneShotNotifier",
        "_raise_state_type",
        "_raise_listener_type",
        "_raise_hook_type",
        "_raise_incompatible",
        "_raise_server_failure",
        "_is_sync_function",
        "_no_running_loop",
        "_listener_is_accepting",
        "_preflight",
        "_construct_config",
        "_run_server",
        "_close_listener",
        "_close_during_control",
        "_serve_owned",
        "serve_prebound_sidecar_app",
    )
    public_definitions = [
        node.name for node in top_level_definitions if not node.name.startswith("_")
    ]
    assert public_definitions == ["serve_prebound_sidecar_app"]

    classes = {node.name: node for node in top_level_definitions if isinstance(node, ast.ClassDef)}
    assert all(
        node.decorator_list == [] and node.keywords == [] and getattr(node, "type_params", []) == []
        for node in classes.values()
    )
    assert [ast.unparse(base) for base in classes["_CompatibilityFailure"].bases] == ["Exception"]
    assert [ast.unparse(base) for base in classes["_ServerFailure"].bases] == ["Exception"]
    assert all(
        len(classes[name].body) == 1 and isinstance(classes[name].body[0], ast.Pass)
        for name in ("_CompatibilityFailure", "_ServerFailure")
    )
    notifier_class = classes["_OneShotNotifier"]
    notifier_methods = {
        node.name: node
        for node in notifier_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert set(notifier_methods) == {"__init__", "__call__"}
    assert isinstance(notifier_methods["__init__"], ast.FunctionDef)
    assert isinstance(notifier_methods["__call__"], ast.AsyncFunctionDef)
    slots_assignment = next(
        node
        for node in notifier_class.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__slots__" for target in node.targets
        )
    )
    assert ast.literal_eval(slots_assignment.value) == ("_callback",)

    top_level_assignments = [node for node in tree.body if isinstance(node, ast.AnnAssign)]
    assignment_values = {
        node.target.id: node.value
        for node in top_level_assignments
        if isinstance(node.target, ast.Name) and node.value is not None
    }
    assert tuple(assignment_values) == (
        "_STATE_TYPE",
        "_SOCKET_TYPE",
        "_SOCKET_BASE_TYPE",
        "_FUNCTION_TYPE",
        "_CONFIG_TYPE",
        "_SERVER_TYPE",
        "_LOOPBACK_HOST",
        "_AF_INET",
        "_SOCK_STREAM",
        "_IPPROTO_TCP",
        "_SOL_SOCKET",
        "_SO_ACCEPTCONN",
        "_PLATFORM",
        "_TCP_CONNECTION_INFO",
        "_TCP_LISTEN_STATE",
        "_LISTEN_BACKLOG",
        "_CONCURRENCY_LIMIT",
        "_GRACEFUL_SHUTDOWN_SECONDS",
        "_NOTIFY_SECONDS",
        "_H11_INCOMPLETE_EVENT_BYTES",
        "_CLEANUP_NOTE",
        "_STATE_PID_GETTER",
        "_STATE_HOST_GETTER",
        "_STATE_PORT_GETTER",
        "_SOCKET_FAMILY_GETTER",
        "_SOCKET_KIND_GETTER",
        "_SOCKET_PROTOCOL_GETTER",
        "_SOCKET_FILENO",
        "_SOCKET_GETSOCKNAME",
        "_SOCKET_GETSOCKOPT",
        "_SOCKET_CLOSE",
        "_INT_EQUAL",
        "_GETPID",
        "_GET_INHERITABLE",
        "_GET_IDENT",
        "_MAIN_THREAD_IDENT",
        "_GET_RUNNING_LOOP",
        "_IS_COROUTINE_FUNCTION",
        "_IS_GENERATOR_FUNCTION",
        "_IS_ASYNC_GENERATOR_FUNCTION",
        "_CREATE_SIDECAR_APP",
        "_CONFIG_CONSTRUCTOR",
        "_SERVER_CONSTRUCTOR",
        "_SERVER_RUN",
        "_ADD_NOTE",
    )
    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)
    assert tree.body[0].value.value == (
        "Blocking Uvicorn runtime for one exact prebound sidecar listener."
    )
    cursor = 1
    assert tree.body[cursor : cursor + len(top_level_imports)] == top_level_imports
    cursor += len(top_level_imports)
    assert tree.body[cursor : cursor + 1] == type_aliases
    cursor += 1
    assert tree.body[cursor : cursor + len(top_level_assignments)] == top_level_assignments
    cursor += len(top_level_assignments)
    assert tree.body[cursor:] == top_level_definitions
    assert set(assignment_values) == {
        "_STATE_TYPE",
        "_SOCKET_TYPE",
        "_SOCKET_BASE_TYPE",
        "_FUNCTION_TYPE",
        "_CONFIG_TYPE",
        "_SERVER_TYPE",
        "_LOOPBACK_HOST",
        "_AF_INET",
        "_SOCK_STREAM",
        "_IPPROTO_TCP",
        "_SOL_SOCKET",
        "_SO_ACCEPTCONN",
        "_PLATFORM",
        "_TCP_CONNECTION_INFO",
        "_TCP_LISTEN_STATE",
        "_LISTEN_BACKLOG",
        "_CONCURRENCY_LIMIT",
        "_GRACEFUL_SHUTDOWN_SECONDS",
        "_NOTIFY_SECONDS",
        "_H11_INCOMPLETE_EVENT_BYTES",
        "_CLEANUP_NOTE",
        "_STATE_PID_GETTER",
        "_STATE_HOST_GETTER",
        "_STATE_PORT_GETTER",
        "_SOCKET_FAMILY_GETTER",
        "_SOCKET_KIND_GETTER",
        "_SOCKET_PROTOCOL_GETTER",
        "_SOCKET_FILENO",
        "_SOCKET_GETSOCKNAME",
        "_SOCKET_GETSOCKOPT",
        "_SOCKET_CLOSE",
        "_INT_EQUAL",
        "_GETPID",
        "_GET_INHERITABLE",
        "_GET_IDENT",
        "_MAIN_THREAD_IDENT",
        "_GET_RUNNING_LOOP",
        "_IS_COROUTINE_FUNCTION",
        "_IS_GENERATOR_FUNCTION",
        "_IS_ASYNC_GENERATOR_FUNCTION",
        "_CREATE_SIDECAR_APP",
        "_CONFIG_CONSTRUCTOR",
        "_SERVER_CONSTRUCTOR",
        "_SERVER_RUN",
        "_ADD_NOTE",
    }
    assert len(top_level_assignments) == len(assignment_values)
    assert all(
        (
            isinstance(node.annotation, ast.Name)
            and node.annotation.id == "Final"
            or isinstance(node.annotation, ast.Subscript)
            and isinstance(node.annotation.value, ast.Name)
            and node.annotation.value.id == "Final"
        )
        for node in top_level_assignments
    )
    assert not any(
        isinstance(node, (ast.Assign, ast.NamedExpr))
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is str
        )
    )
    assert all(
        not any(
            isinstance(
                descendant,
                (
                    ast.Dict,
                    ast.DictComp,
                    ast.GeneratorExp,
                    ast.List,
                    ast.ListComp,
                    ast.Set,
                    ast.SetComp,
                ),
            )
            for descendant in ast.walk(value)
        )
        for value in assignment_values.values()
    )

    def expression_dump(source_text: str) -> str:
        return ast.dump(ast.parse(source_text, mode="eval").body, include_attributes=False)

    exact_captures = {
        "_STATE_TYPE": "SidecarState",
        "_SOCKET_TYPE": "socket.socket",
        "_SOCKET_BASE_TYPE": "socket.socket.__mro__[1]",
        "_FUNCTION_TYPE": "FunctionType",
        "_CONFIG_TYPE": "Config",
        "_SERVER_TYPE": "Server",
        "_LOOPBACK_HOST": "LOOPBACK_HOST",
        "_AF_INET": "int(socket.AF_INET)",
        "_SOCK_STREAM": "int(socket.SOCK_STREAM)",
        "_IPPROTO_TCP": "socket.IPPROTO_TCP",
        "_SOL_SOCKET": "socket.SOL_SOCKET",
        "_SO_ACCEPTCONN": "socket.SO_ACCEPTCONN",
        "_PLATFORM": "sys.platform",
        "_TCP_CONNECTION_INFO": "getattr(socket, 'TCP_CONNECTION_INFO', None)",
        "_STATE_PID_GETTER": "cast(_BoundSlotGetter, _STATE_TYPE.__dict__['pid'].__get__)",
        "_STATE_HOST_GETTER": "cast(_BoundSlotGetter, _STATE_TYPE.__dict__['host'].__get__)",
        "_STATE_PORT_GETTER": "cast(_BoundSlotGetter, _STATE_TYPE.__dict__['port'].__get__)",
        "_SOCKET_FAMILY_GETTER": (
            "cast(_BoundSlotGetter, _SOCKET_BASE_TYPE.__dict__['family'].__get__)"
        ),
        "_SOCKET_KIND_GETTER": (
            "cast(_BoundSlotGetter, _SOCKET_BASE_TYPE.__dict__['type'].__get__)"
        ),
        "_SOCKET_PROTOCOL_GETTER": (
            "cast(_BoundSlotGetter, _SOCKET_BASE_TYPE.__dict__['proto'].__get__)"
        ),
        "_SOCKET_FILENO": "socket.socket.fileno",
        "_SOCKET_GETSOCKNAME": "socket.socket.getsockname",
        "_SOCKET_GETSOCKOPT": "socket.socket.getsockopt",
        "_SOCKET_CLOSE": "_SOCKET_BASE_TYPE.__dict__['close']",
        "_INT_EQUAL": "int.__eq__",
        "_GETPID": "os.getpid",
        "_GET_INHERITABLE": "os.get_inheritable",
        "_GET_IDENT": "threading.get_ident",
        "_MAIN_THREAD_IDENT": "threading.main_thread().ident",
        "_GET_RUNNING_LOOP": "asyncio.get_running_loop",
        "_IS_COROUTINE_FUNCTION": "inspect.iscoroutinefunction",
        "_IS_GENERATOR_FUNCTION": "inspect.isgeneratorfunction",
        "_IS_ASYNC_GENERATOR_FUNCTION": "inspect.isasyncgenfunction",
        "_CREATE_SIDECAR_APP": "create_sidecar_app",
        "_CONFIG_CONSTRUCTOR": "Config",
        "_SERVER_CONSTRUCTOR": "Server",
        "_SERVER_RUN": "Server.run",
        "_ADD_NOTE": "BaseException.add_note",
    }
    assert {
        name: ast.dump(assignment_values[name], include_attributes=False) for name in exact_captures
    } == {name: expression_dump(value) for name, value in exact_captures.items()}
    for name, expected in {
        "_TCP_LISTEN_STATE": b"\x01",
        "_LISTEN_BACKLOG": 128,
        "_CONCURRENCY_LIMIT": 128,
        "_GRACEFUL_SHUTDOWN_SECONDS": 2,
        "_NOTIFY_SECONDS": 30,
        "_H11_INCOMPLETE_EVENT_BYTES": 16_384,
        "_CLEANUP_NOTE": "prebound sidecar server cleanup failed",
    }.items():
        assert isinstance(assignment_values[name], ast.Constant)
        assert type(assignment_values[name].value) is type(expected)
        assert assignment_values[name].value == expected

    functions = {
        node.name: node for node in top_level_definitions if isinstance(node, ast.FunctionDef)
    }
    signature_sources = {
        "_raise_state_type": "def expected() -> NoReturn: pass",
        "_raise_listener_type": "def expected() -> NoReturn: pass",
        "_raise_hook_type": "def expected() -> NoReturn: pass",
        "_raise_incompatible": "def expected() -> NoReturn: pass",
        "_raise_server_failure": "def expected() -> NoReturn: pass",
        "_is_sync_function": "def expected(callback: FunctionType) -> bool: pass",
        "_no_running_loop": "def expected() -> bool: pass",
        "_listener_is_accepting": ("def expected(listener: socket.socket) -> bool: pass"),
        "_preflight": ("def expected(state: SidecarState, listener: socket.socket) -> int: pass"),
        "_construct_config": (
            "def expected(app: Callable[..., Any], port: int, "
            "notifier: _OneShotNotifier) -> object: pass"
        ),
        "_run_server": (
            "def expected(state: SidecarState, listener: socket.socket, "
            "on_started: Callable[[], None], port: int) -> object: pass"
        ),
        "_close_listener": "def expected(listener: socket.socket) -> bool: pass",
        "_close_during_control": (
            "def expected(listener: socket.socket, active_error: BaseException) -> None: pass"
        ),
        "_serve_owned": (
            "def expected(state: SidecarState, listener: socket.socket, "
            "on_started: Callable[[], None], port: int) -> bool: pass"
        ),
        "serve_prebound_sidecar_app": (
            "def expected(state: SidecarState, listener: socket.socket, *, "
            "on_started: Callable[[], None]) -> None: pass"
        ),
        "_OneShotNotifier.__init__": (
            "def expected(self, callback: Callable[[], None]) -> None: pass"
        ),
        "_OneShotNotifier.__call__": "async def expected(self) -> None: pass",
    }
    signature_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {
        **functions,
        "_OneShotNotifier.__init__": notifier_methods["__init__"],
        "_OneShotNotifier.__call__": notifier_methods["__call__"],
    }
    assert set(signature_nodes) == set(signature_sources)
    for name, signature_source in signature_sources.items():
        expected_signature = ast.parse(signature_source).body[0]
        assert isinstance(expected_signature, (ast.FunctionDef, ast.AsyncFunctionDef))
        observed_signature = signature_nodes[name]
        assert type(observed_signature) is type(expected_signature)
        assert ast.dump(observed_signature.args, include_attributes=False) == ast.dump(
            expected_signature.args,
            include_attributes=False,
        )
        assert ast.dump(observed_signature.returns, include_attributes=False) == ast.dump(
            expected_signature.returns,
            include_attributes=False,
        )
        assert observed_signature.decorator_list == []
        assert getattr(observed_signature, "type_params", []) == []
        assert observed_signature.type_comment is None
    allowed_function_nodes = {
        id(node)
        for node in (*functions.values(), *notifier_methods.values())
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert all(
        id(node) in allowed_function_nodes
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert not any(
        isinstance(
            node,
            (
                ast.Lambda,
                ast.Global,
                ast.Nonlocal,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        )
        for node in ast.walk(tree)
    )
    stored_attributes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    ]
    assert len(stored_attributes) == 2
    assert all(
        isinstance(node.value, ast.Name) and node.value.id == "self" and node.attr == "_callback"
        for node in stored_attributes
    )
    assert not any(
        isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
        for node in ast.walk(tree)
    )

    all_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert Counter(_call_name(node) for node in all_calls) == Counter(
        {
            "RuntimeError": 1,
            "TypeError": 3,
            "ValueError": 1,
            "_ADD_NOTE": 1,
            "_CONFIG_CONSTRUCTOR": 1,
            "_CREATE_SIDECAR_APP": 1,
            "_GETPID": 1,
            "_GET_IDENT": 1,
            "_GET_INHERITABLE": 1,
            "_GET_RUNNING_LOOP": 1,
            "_INT_EQUAL": 2,
            "_IS_ASYNC_GENERATOR_FUNCTION": 1,
            "_IS_COROUTINE_FUNCTION": 1,
            "_IS_GENERATOR_FUNCTION": 1,
            "_OneShotNotifier": 1,
            "_SERVER_CONSTRUCTOR": 1,
            "_SERVER_RUN": 1,
            "_SOCKET_CLOSE": 2,
            "_SOCKET_FAMILY_GETTER": 1,
            "_SOCKET_FILENO": 1,
            "_SOCKET_GETSOCKNAME": 1,
            "_SOCKET_GETSOCKOPT": 2,
            "_SOCKET_KIND_GETTER": 1,
            "_SOCKET_PROTOCOL_GETTER": 1,
            "_STATE_HOST_GETTER": 1,
            "_STATE_PID_GETTER": 1,
            "_STATE_PORT_GETTER": 1,
            "_close_during_control": 1,
            "_close_listener": 1,
            "_construct_config": 1,
            "_is_sync_function": 1,
            "_listener_is_accepting": 1,
            "_no_running_loop": 1,
            "_preflight": 1,
            "_raise_hook_type": 2,
            "_raise_incompatible": 1,
            "_raise_listener_type": 1,
            "_raise_server_failure": 1,
            "_raise_state_type": 1,
            "_run_server": 1,
            "_serve_owned": 1,
            "callback": 1,
            "cast": 6,
            "getattr": 1,
            "int": 2,
            "len": 1,
            "main_thread": 1,
            "type": 25,
        }
    )
    for function in (*functions.values(), *notifier_methods.values()):
        assert not any(
            isinstance(node, ast.Call) and _call_name(node) in {"cast", "getattr"}
            for node in ast.walk(function)
        )
    assert not any(
        isinstance(node, ast.Call)
        and _call_name(node)
        in {
            "bind",
            "bind_socket",
            "compile",
            "create_task",
            "dup",
            "eval",
            "exec",
            "fromfd",
            "input",
            "listen",
            "open",
            "print",
            "run",
            "socket",
        }
        for function in (*functions.values(), *notifier_methods.values())
        for node in ast.walk(function)
    )

    construct_config = functions["_construct_config"]
    config_calls = [
        node
        for node in ast.walk(construct_config)
        if isinstance(node, ast.Call) and _call_name(node) == "_CONFIG_CONSTRUCTOR"
    ]
    assert len(config_calls) == 1
    expected_config_call = ast.parse(
        """
_CONFIG_CONSTRUCTOR(
    app,
    host=_LOOPBACK_HOST,
    port=port,
    uds=None,
    fd=None,
    loop="asyncio",
    http="h11",
    ws="none",
    lifespan="off",
    env_file=None,
    log_config=None,
    log_level="critical",
    access_log=False,
    use_colors=False,
    interface="asgi3",
    reload=False,
    workers=1,
    proxy_headers=False,
    forwarded_allow_ips=[],
    server_header=False,
    date_header=False,
    root_path="",
    limit_concurrency=_CONCURRENCY_LIMIT,
    limit_max_requests=None,
    limit_max_requests_jitter=0,
    backlog=_LISTEN_BACKLOG,
    timeout_keep_alive=5,
    timeout_notify=_NOTIFY_SECONDS,
    timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
    callback_notify=notifier,
    headers=[],
    factory=False,
    h11_max_incomplete_event_size=_H11_INCOMPLETE_EVENT_BYTES,
    reset_contextvars=True,
)
""",
        mode="eval",
    ).body
    assert ast.dump(config_calls[0], include_attributes=False) == ast.dump(
        expected_config_call,
        include_attributes=False,
    )
    config_empty_lists = [node for node in ast.walk(config_calls[0]) if isinstance(node, ast.List)]
    assert len(config_empty_lists) == 2
    assert all(node.elts == [] and isinstance(node.ctx, ast.Load) for node in config_empty_lists)
    assert config_empty_lists[0] is not config_empty_lists[1]

    run_server = functions["_run_server"]
    server_run_calls = [
        node
        for node in ast.walk(run_server)
        if isinstance(node, ast.Call) and _call_name(node) == "_SERVER_RUN"
    ]
    assert len(server_run_calls) == 1
    assert ast.dump(server_run_calls[0], include_attributes=False) == expression_dump(
        "_SERVER_RUN(server, sockets=sockets)"
    )
    socket_lists = [
        node
        for node in ast.walk(run_server)
        if isinstance(node, ast.List)
        and len(node.elts) == 1
        and isinstance(node.elts[0], ast.Name)
        and node.elts[0].id == "listener"
    ]
    assert len(socket_lists) == 1
    assert ast.dump(socket_lists[0], include_attributes=False) == expression_dump("[listener]")

    def body_dump(nodes: list[ast.stmt]) -> list[str]:
        return [ast.dump(node, include_attributes=False) for node in nodes]

    def expected_body(source_text: str) -> list[str]:
        parsed = ast.parse(source_text)
        function = parsed.body[0]
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        return body_dump(function.body)

    assert body_dump(notifier_methods["__init__"].body) == expected_body(
        """
def expected(self, callback):
    self._callback: Callable[[], None] | None = callback
"""
    )
    assert body_dump(notifier_methods["__call__"].body) == expected_body(
        """
async def expected(self):
    callback: Callable[[], None] | None = self._callback
    if callback is None:
        del callback, self
        return
    self._callback = None
    result: object = None
    try:
        result = callback()
    except BaseException:
        del callback, result, self
        raise
    if result is not None:
        del callback, result, self
        raise _ServerFailure from None
    del callback, result, self
"""
    )
    for name, error_type, message in (
        ("_raise_state_type", "TypeError", STATE_TYPE_ERROR),
        ("_raise_listener_type", "TypeError", LISTENER_TYPE_ERROR),
        ("_raise_hook_type", "TypeError", HOOK_TYPE_ERROR),
        ("_raise_incompatible", "ValueError", COMPATIBILITY_ERROR),
        ("_raise_server_failure", "RuntimeError", SERVER_ERROR),
    ):
        assert body_dump(functions[name].body) == expected_body(
            f"""\
def expected():
    raise {error_type}({message!r}) from None
"""
        )

    assert body_dump(functions["_close_listener"].body) == expected_body(
        """
def expected(listener):
    result: object = None
    failed = False
    try:
        try:
            result = _SOCKET_CLOSE(listener)
        except Exception:
            failed = True
    except BaseException:
        del listener, result, failed
        raise
    del listener
    return not failed and result is None
"""
    )
    assert body_dump(functions["_close_during_control"].body) == expected_body(
        """
def expected(listener, active_error):
    cleanup_failed = False
    result: object = None
    try:
        try:
            result = _SOCKET_CLOSE(listener)
        except BaseException:
            cleanup_failed = True
        if result is not None:
            cleanup_failed = True
    except BaseException:
        cleanup_failed = True
    if cleanup_failed:
        try:
            _ADD_NOTE(active_error, _CLEANUP_NOTE)
        except BaseException:
            pass
    del listener, active_error, cleanup_failed, result
"""
    )
    assert body_dump(functions["_serve_owned"].body) == expected_body(
        """
def expected(state, listener, on_started, port):
    result: object = None
    failed = False
    active_control: BaseException | None = None
    close_ok = False
    try:
        try:
            result = _run_server(state, listener, on_started, port)
        except Exception:
            failed = True
        if result is not None:
            failed = True
    except BaseException as error:
        active_control = error
        raise
    finally:
        if active_control is not None:
            _close_during_control(listener, active_control)
            del (
                state,
                listener,
                on_started,
                port,
                result,
                failed,
                active_control,
                close_ok,
            )
        else:
            del state, on_started, port, result
            try:
                close_ok = _close_listener(listener)
            except BaseException:
                del listener, failed, active_control, close_ok
                raise
            del listener
            if not close_ok:
                failed = True
            del active_control, close_ok
    return not failed
"""
    )

    serve_owned = functions["_serve_owned"]
    top_level_finally = [
        node for node in serve_owned.body if isinstance(node, ast.Try) and bool(node.finalbody)
    ]
    assert len(top_level_finally) == 1
    ownership_guard = top_level_finally[0]
    guard_calls = [
        _call_name(node) for node in ast.walk(ownership_guard) if isinstance(node, ast.Call)
    ]
    assert guard_calls.count("_run_server") == 1
    assert guard_calls.count("_close_during_control") == 1
    assert guard_calls.count("_close_listener") == 1
    guard_index = serve_owned.body.index(ownership_guard)
    assert all(
        isinstance(node, (ast.Assign, ast.AnnAssign)) for node in serve_owned.body[:guard_index]
    )
    assert len(serve_owned.body[guard_index + 1 :]) == 1
    assert isinstance(serve_owned.body[-1], ast.Return)

    public_function = functions["serve_prebound_sidecar_app"]
    assert not any(isinstance(node, ast.Return) for node in ast.walk(public_function))
    owned_calls = [
        node
        for node in ast.walk(public_function)
        if isinstance(node, ast.Call) and _call_name(node) == "_serve_owned"
    ]
    assert len(owned_calls) == 1
    parent_by_id = {
        id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    owned_parent = parent_by_id[id(owned_calls[0])]
    assert isinstance(owned_parent, ast.Assign)
    owned_try = parent_by_id[id(owned_parent)]
    assert isinstance(owned_try, ast.Try)
    assert len(owned_try.handlers) == 1
    assert isinstance(owned_try.handlers[0].type, ast.Name)
    assert owned_try.handlers[0].type.id == "BaseException"

    assert Counter(
        ast.unparse(node.type) if node.type is not None else None
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
    ) == Counter(
        {
            "BaseException": 15,
            "Exception": 5,
            "RuntimeError": 1,
            "_CompatibilityFailure": 1,
        }
    )
    assert [
        ast.dump(node, include_attributes=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Set)
    ] == [
        expression_dump("{0, _IPPROTO_TCP}"),
    ]
    assert all(
        node.attr
        not in {
            "database_path",
            "token",
            "runtime_root",
            "state_path",
            "lock_path",
            "startup_writer",
        }
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    )
    assert Counter(type(node).__name__ for node in ast.walk(tree)) == Counter(
        {
            "And": 8,
            "AnnAssign": 76,
            "Assign": 62,
            "AsyncFunctionDef": 1,
            "Attribute": 51,
            "BinOp": 7,
            "BitOr": 7,
            "BoolOp": 9,
            "Call": 84,
            "ClassDef": 3,
            "Compare": 52,
            "Constant": 149,
            "Del": 174,
            "Delete": 37,
            "Eq": 10,
            "ExceptHandler": 22,
            "Expr": 10,
            "FunctionDef": 16,
            "GtE": 1,
            "If": 19,
            "Import": 6,
            "ImportFrom": 7,
            "In": 1,
            "Is": 29,
            "IsNot": 10,
            "List": 10,
            "Load": 545,
            "LtE": 2,
            "Module": 1,
            "Name": 758,
            "Not": 6,
            "Or": 1,
            "Pass": 4,
            "Raise": 21,
            "Return": 11,
            "Set": 1,
            "Store": 141,
            "Subscript": 26,
            "Try": 20,
            "Tuple": 15,
            "TypeAlias": 1,
            "UnaryOp": 6,
            "alias": 18,
            "arg": 24,
            "arguments": 17,
            "keyword": 34,
        }
    )
