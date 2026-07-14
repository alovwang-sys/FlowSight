"""SDK-side process election and authenticated sidecar discovery spike."""

from __future__ import annotations

import fcntl
import http.client
import json
import math
import os
import selectors
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .state import PROTOCOL_VERSION, SidecarState, StateStore

_ATTACH_POLL: Final = 0.025
_MAX_RESPONSE_BYTES: Final = 256 * 1024


class SidecarError(RuntimeError):
    """Base class for safe, structured lifecycle errors."""


class SidecarUnavailableError(SidecarError):
    """An owner exists or is starting but did not become healthy in time."""


class SidecarStartupError(SidecarError):
    """A newly elected sidecar could not start."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sidecar startup failed ({code})")


class AuthenticationError(SidecarError):
    """The sidecar rejected an authenticated private request."""


class LeaseRejectedError(SidecarError):
    """The project already has another live producer."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"producer lease rejected ({code})")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Configuration for one isolated project-scoped spike runtime."""

    runtime_dir: Path
    project_id: str
    requested_port: int | None = None
    default_port: int = 4040
    startup_timeout: float = 5.0
    request_timeout: float = 0.5
    idle_timeout: float = 5.0
    lease_ttl: float = 5.0
    writer_capacity: int = 64

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or not self.project_id:
            raise ValueError("project_id must be a non-empty built-in str")
        for name, port in (
            ("requested_port", self.requested_port),
            ("default_port", self.default_port),
        ):
            if port is not None and (type(port) is not int or not 0 <= port <= 65535):
                raise ValueError(f"{name} must be a built-in int in 0..65535")
        for name, value in (
            ("startup_timeout", self.startup_timeout),
            ("request_timeout", self.request_timeout),
            ("idle_timeout", self.idle_timeout),
            ("lease_ttl", self.lease_ttl),
        ):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive built-in number")
        if self.lease_ttl < 1.0:
            raise ValueError("lease_ttl must be at least 1 second")
        if type(self.writer_capacity) is not int or self.writer_capacity <= 0:
            raise ValueError("writer_capacity must be a positive built-in int")


@dataclass(frozen=True, slots=True)
class SidecarHandle:
    """Authenticated state returned by ``ensure_sidecar``."""

    state: SidecarState
    started_by_caller: bool


def _deadline(timeout: float) -> float:
    return time.monotonic() + timeout


def request_json(
    state: SidecarState,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    timeout: float = 0.5,
    origin: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Issue one exact-Host, bearer-authenticated private JSON request."""

    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    connection = http.client.HTTPConnection(state.host, state.port, timeout=timeout)
    # ``HTTPConnection.debuglevel`` is a mutable class attribute.  A host
    # application may have enabled it globally; force it off before adding the
    # capability header so the token cannot be printed to stdout/stderr.
    connection.set_debuglevel(0)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", state.authority)
        connection.putheader("Authorization", f"Bearer {state.token}")
        if origin is not None:
            connection.putheader("Origin", origin)
        if body:
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(body)))
        else:
            connection.putheader("Content-Length", "0")
        connection.endheaders(body)
        response = connection.getresponse()
        encoded = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(encoded) > _MAX_RESPONSE_BYTES:
            raise SidecarUnavailableError("sidecar response exceeded the spike limit")
        try:
            decoded = json.loads(encoded or b"{}")
        except json.JSONDecodeError as error:
            raise SidecarUnavailableError("sidecar returned invalid JSON") from error
        if type(decoded) is not dict:
            raise SidecarUnavailableError("sidecar returned an invalid JSON object")
        return response.status, decoded
    finally:
        connection.close()


def probe_health(
    state: SidecarState,
    *,
    expected_project_id: str | None = None,
    timeout: float = 0.5,
) -> dict[str, Any] | None:
    """Return authenticated health only when it proves this exact startup."""

    try:
        status, body = request_json(
            state,
            "GET",
            "/internal/v1/health",
            timeout=timeout,
        )
    except (OSError, TimeoutError, http.client.HTTPException, SidecarError):
        return None
    project_id = state.project_id if expected_project_id is None else expected_project_id
    if status != 200:
        return None
    expected = {
        "status": "ok",
        "protocol_version": PROTOCOL_VERSION,
        "project_id": project_id,
        "startup_id": state.startup_id,
        "sidecar_pid": state.pid,
        "port": state.port,
    }
    if any(body.get(key) != value for key, value in expected.items()):
        return None
    return body


def _matching_healthy_state(config: RuntimeConfig, store: StateStore) -> SidecarState | None:
    state = store.load()
    if state is None or state.project_id != config.project_id:
        return None
    if (
        probe_health(
            state,
            expected_project_id=config.project_id,
            timeout=config.request_timeout,
        )
        is None
    ):
        return None
    if config.requested_port not in {None, 0, state.port}:
        raise SidecarStartupError("EXPLICIT_PORT_MISMATCH")
    return state


def _wait_for_owner_or_election(
    config: RuntimeConfig, store: StateStore, deadline: float
) -> tuple[SidecarState | None, int]:
    """Wait for a healthy winner, or re-elect if the current lock owner vanishes."""

    waiter = threading.Event()
    while True:
        state = _matching_healthy_state(config, store)
        if state is not None:
            return state, -1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SidecarUnavailableError("sidecar owner did not become healthy before timeout")

        lock_fd = store.open_lock()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
        else:
            return None, lock_fd
        waiter.wait(min(_ATTACH_POLL, remaining))


def _read_ready(fd: int, process: subprocess.Popen[bytes], deadline: float) -> dict[str, Any]:
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    data = bytearray()
    try:
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SidecarStartupError("STARTUP_TIMEOUT")
            if not selector.select(remaining):
                raise SidecarStartupError("STARTUP_TIMEOUT")
            chunk = os.read(fd, 4096)
            if not chunk:
                code = process.poll()
                error_code = "CHILD_EXITED" if code is not None else "READY_PIPE_CLOSED"
                raise SidecarStartupError(error_code)
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise SidecarStartupError("READY_MESSAGE_TOO_LARGE")
        try:
            result = json.loads(bytes(data).split(b"\n", 1)[0])
        except json.JSONDecodeError as error:
            raise SidecarStartupError("INVALID_READY_MESSAGE") from error
        if type(result) is not dict:
            raise SidecarStartupError("INVALID_READY_MESSAGE")
        return result
    finally:
        selector.close()


def _terminate_failed_start(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)


def _reap_sidecar(process: subprocess.Popen[bytes]) -> None:
    """Reap a directly spawned sidecar without blocking the SDK caller."""

    def wait_for_exit() -> None:
        process.wait()

    threading.Thread(
        target=wait_for_exit,
        name="flowsight-sidecar-reaper-spike",
        daemon=True,
    ).start()


def _spawn_sidecar(config: RuntimeConfig, store: StateStore, lock_fd: int) -> SidecarState:
    ready_read_fd, ready_write_fd = os.pipe()
    command = [
        sys.executable,
        "-m",
        "spikes.sidecar_otel.sidecar",
        "--runtime-dir",
        str(store.runtime_dir),
        "--project-id",
        config.project_id,
        "--lock-fd",
        str(lock_fd),
        "--ready-fd",
        str(ready_write_fd),
        "--default-port",
        str(config.default_port),
        "--idle-timeout",
        str(config.idle_timeout),
        "--lease-ttl",
        str(config.lease_ttl),
        "--writer-capacity",
        str(config.writer_capacity),
    ]
    if config.requested_port is not None:
        command.extend(("--requested-port", str(config.requested_port)))

    try:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(lock_fd, ready_write_fd),
            start_new_session=True,
        )
    except BaseException:
        os.close(ready_read_fd)
        os.close(ready_write_fd)
        raise
    os.close(ready_write_fd)
    try:
        ready = _read_ready(ready_read_fd, process, _deadline(config.startup_timeout))
    except BaseException:
        _terminate_failed_start(process)
        raise
    finally:
        os.close(ready_read_fd)

    if ready.get("status") != "ready":
        _terminate_failed_start(process)
        code = ready.get("code")
        raise SidecarStartupError(code if type(code) is str else "UNKNOWN_STARTUP_FAILURE")

    state = store.load()
    if state is None or state.project_id != config.project_id:
        _terminate_failed_start(process)
        raise SidecarStartupError("STATE_NOT_PUBLISHED")
    health_deadline = _deadline(config.startup_timeout)
    waiter = threading.Event()
    while (
        probe_health(
            state,
            expected_project_id=config.project_id,
            timeout=config.request_timeout,
        )
        is None
    ):
        if process.poll() is not None:
            store.remove_if_owned(state.startup_id)
            raise SidecarStartupError("CHILD_EXITED_AFTER_READY")
        remaining = health_deadline - time.monotonic()
        if remaining <= 0:
            _terminate_failed_start(process)
            store.remove_if_owned(state.startup_id)
            raise SidecarStartupError("HEALTH_NOT_READY")
        waiter.wait(min(_ATTACH_POLL, remaining))
    _reap_sidecar(process)
    return state


def ensure_sidecar(config: RuntimeConfig) -> SidecarHandle:
    """Attach to one healthy owner or atomically elect and start one."""

    store = StateStore(config.runtime_dir)
    store.ensure_private_directory()
    state = _matching_healthy_state(config, store)
    if state is not None:
        return SidecarHandle(state=state, started_by_caller=False)

    lock_fd = store.open_lock()
    transferred = False
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            lock_fd = -1
            state, lock_fd = _wait_for_owner_or_election(
                config, store, _deadline(config.startup_timeout)
            )
            if state is not None:
                return SidecarHandle(state=state, started_by_caller=False)

        # Required second check: another process may have published a healthy
        # sidecar immediately before this caller acquired the election lock.
        state = _matching_healthy_state(config, store)
        if state is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            return SidecarHandle(state=state, started_by_caller=False)

        state = _spawn_sidecar(config, store, lock_fd)
        transferred = True
        # Do not call LOCK_UN here.  The child inherited this locked open-file
        # description and remains the sole owner after the parent closes its copy.
        os.close(lock_fd)
        lock_fd = -1
        return SidecarHandle(state=state, started_by_caller=True)
    finally:
        if lock_fd >= 0:
            if not transferred:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)


def _lease_payload(state: SidecarState, producer_id: str, lease_id: str) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "project_id": state.project_id,
        "producer_id": producer_id,
        "lease_id": lease_id,
    }


def _error_code(body: dict[str, Any], fallback: str) -> str:
    detail = body.get("detail")
    if type(detail) is dict:
        code = detail.get("code")
        if type(code) is str:
            return code
    return fallback


def hello(
    state: SidecarState,
    producer_id: str,
    *,
    wait_timeout_ms: int = 0,
    timeout: float = 2.0,
) -> str:
    payload: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "project_id": state.project_id,
        "producer_id": producer_id,
        "wait_timeout_ms": wait_timeout_ms,
    }
    status, body = request_json(state, "POST", "/internal/v1/hello", payload, timeout=timeout)
    if status == 409:
        raise LeaseRejectedError(_error_code(body, "MULTI_WORKER_UNSUPPORTED"))
    lease_id = body.get("lease_id")
    if status != 200 or type(lease_id) is not str:
        raise AuthenticationError("sidecar rejected producer hello")
    return lease_id


def flush(state: SidecarState, producer_id: str, lease_id: str, *, timeout: float = 2.0) -> None:
    status, body = request_json(
        state,
        "POST",
        "/internal/v1/flush",
        _lease_payload(state, producer_id, lease_id),
        timeout=timeout,
    )
    if status != 200 or body.get("status") != "flushed":
        raise SidecarUnavailableError("sidecar flush failed")


def renew(state: SidecarState, producer_id: str, lease_id: str, *, timeout: float = 2.0) -> None:
    status, body = request_json(
        state,
        "POST",
        "/internal/v1/renew",
        _lease_payload(state, producer_id, lease_id),
        timeout=timeout,
    )
    if status == 409:
        raise LeaseRejectedError(_error_code(body, "INVALID_LEASE"))
    if status != 200 or body.get("status") != "renewed":
        raise SidecarUnavailableError("sidecar lease renewal failed")


def goodbye(
    state: SidecarState,
    producer_id: str,
    lease_id: str,
    *,
    timeout: float = 2.0,
) -> None:
    status, body = request_json(
        state,
        "POST",
        "/internal/v1/goodbye",
        _lease_payload(state, producer_id, lease_id),
        timeout=timeout,
    )
    if status != 200 or body.get("status") not in {"released", "already_released"}:
        raise SidecarUnavailableError("sidecar goodbye failed")


def stop_sidecar(state: SidecarState, *, timeout: float = 2.0) -> None:
    status, body = request_json(
        state,
        "POST",
        "/internal/v1/stop",
        {"protocol_version": PROTOCOL_VERSION, "project_id": state.project_id},
        timeout=timeout,
    )
    if status != 200 or body.get("status") != "stopping":
        raise SidecarUnavailableError("sidecar explicit stop failed")
