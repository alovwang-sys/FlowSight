"""Atomic IPv4 loopback listener reservation for the local sidecar."""

from __future__ import annotations

import errno
import socket
from enum import StrEnum
from typing import Final

from .state import LOOPBACK_HOST

DEFAULT_SIDECAR_PORT: Final = 4040
_LISTEN_BACKLOG: Final = 128


class ListenerErrorCode(StrEnum):
    """Stable, non-sensitive listener startup failure codes."""

    EXPLICIT_PORT_CONFLICT = "EXPLICIT_PORT_CONFLICT"
    EXPLICIT_PORT_UNAVAILABLE = "EXPLICIT_PORT_UNAVAILABLE"
    DEFAULT_PORT_UNAVAILABLE = "DEFAULT_PORT_UNAVAILABLE"
    LISTENER_SETUP_FAILED = "LISTENER_SETUP_FAILED"


class ListenerBindError(RuntimeError):
    """The loopback listener could not satisfy its atomic bind contract."""

    def __init__(self, code: ListenerErrorCode) -> None:
        self.code = code
        super().__init__(f"sidecar listener failed ({code})")


class _BindFailure(Exception):
    def __init__(self, error_number: int | None) -> None:
        self.error_number = error_number
        super().__init__()


class _SetupFailure(Exception):
    pass


def _validate_port(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 65_535:
        raise ValueError(f"{name} must be an exact built-in int in 0..65535")
    return value


def _close_after_failure(listener: socket.socket, active_error: BaseException) -> bool:
    """Attempt one close without replacing a process-control exception."""

    try:
        listener.close()
    except BaseException as cleanup_error:
        if not isinstance(active_error, Exception):
            active_error.add_note("loopback listener cleanup failed")
            return False
        if not isinstance(cleanup_error, Exception):
            raise
        return False
    return True


def _allocate_listener() -> socket.socket | None:
    try:
        return socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        return None


def _configure_listener(listener: socket.socket) -> bool:
    try:
        listener.set_inheritable(False)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except BaseException as error:
        _close_after_failure(listener, error)
        if not isinstance(error, Exception):
            raise
        return False
    return True


def _bind_listener(listener: socket.socket, port: int) -> tuple[str, int | None]:
    try:
        listener.bind((LOOPBACK_HOST, port))
    except OSError as error:
        error_number = error.errno
        if not _close_after_failure(listener, error):
            return "setup", None
        return "bind", error_number
    except BaseException as error:
        _close_after_failure(listener, error)
        if not isinstance(error, Exception):
            raise
        return "setup", None
    return "ok", None


def _start_listening(listener: socket.socket) -> bool:
    try:
        listener.listen(_LISTEN_BACKLOG)
    except BaseException as error:
        _close_after_failure(listener, error)
        if not isinstance(error, Exception):
            raise
        return False
    return True


def _open_listener(port: int) -> socket.socket:
    listener = _allocate_listener()
    if listener is None:
        raise _SetupFailure from None

    if not _configure_listener(listener):
        raise _SetupFailure from None

    bind_status, error_number = _bind_listener(listener, port)
    if bind_status == "setup":
        raise _SetupFailure from None
    if bind_status == "bind":
        raise _BindFailure(error_number) from None

    if not _start_listening(listener):
        raise _SetupFailure from None
    return listener


def _public_failure(code: ListenerErrorCode) -> ListenerBindError:
    return ListenerBindError(code)


def bind_loopback_listener(
    requested_port: int | None = None,
    *,
    default_port: int = DEFAULT_SIDECAR_PORT,
) -> socket.socket:
    """Return one already-listening socket bound atomically to ``127.0.0.1``.

    ``requested_port`` is explicit when not ``None``; an explicit conflict is
    therefore an error. With no explicit request, only a genuine conflict on
    ``default_port`` falls back to an OS-selected port. The returned socket is
    the exact socket a later Uvicorn runtime must retain and consume.
    """

    if requested_port is not None:
        requested_port = _validate_port(requested_port, "requested_port")
    default_port = _validate_port(default_port, "default_port")
    preferred_port = default_port if requested_port is None else requested_port

    failure_code: ListenerErrorCode | None = None
    try:
        listener = _open_listener(preferred_port)
    except _SetupFailure:
        failure_code = ListenerErrorCode.LISTENER_SETUP_FAILED
    except _BindFailure as failure:
        if requested_port is not None:
            failure_code = (
                ListenerErrorCode.EXPLICIT_PORT_CONFLICT
                if failure.error_number == errno.EADDRINUSE
                else ListenerErrorCode.EXPLICIT_PORT_UNAVAILABLE
            )
        elif failure.error_number != errno.EADDRINUSE:
            failure_code = ListenerErrorCode.DEFAULT_PORT_UNAVAILABLE
    else:
        return listener

    if failure_code is not None:
        raise _public_failure(failure_code) from None

    fallback_failure: ListenerErrorCode | None = None
    try:
        fallback = _open_listener(0)
    except _SetupFailure:
        fallback_failure = ListenerErrorCode.LISTENER_SETUP_FAILED
    except _BindFailure:
        fallback_failure = ListenerErrorCode.DEFAULT_PORT_UNAVAILABLE
    else:
        return fallback
    raise _public_failure(fallback_failure) from None
