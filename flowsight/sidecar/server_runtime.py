"""Blocking Uvicorn runtime for one exact prebound sidecar listener."""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import sys
import threading
from collections.abc import Callable
from types import FunctionType
from typing import Any, Final, NoReturn, cast

from uvicorn import Config, Server

from .app import create_sidecar_app
from .state import LOOPBACK_HOST, SidecarState

type _BoundSlotGetter = Callable[[object, type[object]], object]

_STATE_TYPE: Final = SidecarState
_SOCKET_TYPE: Final = socket.socket
_SOCKET_BASE_TYPE: Final = socket.socket.__mro__[1]
_FUNCTION_TYPE: Final = FunctionType
_CONFIG_TYPE: Final = Config
_SERVER_TYPE: Final = Server
_LOOPBACK_HOST: Final = LOOPBACK_HOST
_AF_INET: Final = int(socket.AF_INET)
_SOCK_STREAM: Final = int(socket.SOCK_STREAM)
_IPPROTO_TCP: Final = socket.IPPROTO_TCP
_SOL_SOCKET: Final = socket.SOL_SOCKET
_SO_ACCEPTCONN: Final = socket.SO_ACCEPTCONN

_PLATFORM: Final = sys.platform
_TCP_CONNECTION_INFO: Final[object] = getattr(socket, "TCP_CONNECTION_INFO", None)
_TCP_LISTEN_STATE: Final = b"\x01"
_LISTEN_BACKLOG: Final = 128
_CONCURRENCY_LIMIT: Final = 128
_GRACEFUL_SHUTDOWN_SECONDS: Final = 2
_NOTIFY_SECONDS: Final = 30
_H11_INCOMPLETE_EVENT_BYTES: Final = 16_384
_CLEANUP_NOTE: Final = "prebound sidecar server cleanup failed"

_STATE_PID_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _STATE_TYPE.__dict__["pid"].__get__,
)
_STATE_HOST_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _STATE_TYPE.__dict__["host"].__get__,
)
_STATE_PORT_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _STATE_TYPE.__dict__["port"].__get__,
)
_SOCKET_FAMILY_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _SOCKET_BASE_TYPE.__dict__["family"].__get__,
)
_SOCKET_KIND_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _SOCKET_BASE_TYPE.__dict__["type"].__get__,
)
_SOCKET_PROTOCOL_GETTER: Final[_BoundSlotGetter] = cast(
    _BoundSlotGetter,
    _SOCKET_BASE_TYPE.__dict__["proto"].__get__,
)

_SOCKET_FILENO: Final = socket.socket.fileno
_SOCKET_GETSOCKNAME: Final = socket.socket.getsockname
_SOCKET_GETSOCKOPT: Final = socket.socket.getsockopt
_SOCKET_CLOSE: Final = _SOCKET_BASE_TYPE.__dict__["close"]
_INT_EQUAL: Final = int.__eq__
_GETPID: Final = os.getpid
_GET_INHERITABLE: Final = os.get_inheritable
_GET_IDENT: Final = threading.get_ident
_MAIN_THREAD_IDENT: Final = threading.main_thread().ident
_GET_RUNNING_LOOP: Final = asyncio.get_running_loop
_IS_COROUTINE_FUNCTION: Final = inspect.iscoroutinefunction
_IS_GENERATOR_FUNCTION: Final = inspect.isgeneratorfunction
_IS_ASYNC_GENERATOR_FUNCTION: Final = inspect.isasyncgenfunction
_CREATE_SIDECAR_APP: Final = create_sidecar_app
_CONFIG_CONSTRUCTOR: Final = Config
_SERVER_CONSTRUCTOR: Final = Server
_SERVER_RUN: Final = Server.run
_ADD_NOTE: Final = BaseException.add_note


class _CompatibilityFailure(Exception):
    pass


class _ServerFailure(Exception):
    pass


class _OneShotNotifier:
    __slots__ = ("_callback",)

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback: Callable[[], None] | None = callback

    async def __call__(self) -> None:
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


def _raise_state_type() -> NoReturn:
    raise TypeError("state must be an exact SidecarState") from None


def _raise_listener_type() -> NoReturn:
    raise TypeError("listener must be an exact built-in socket.socket") from None


def _raise_hook_type() -> NoReturn:
    raise TypeError("on_started must be an exact Python function") from None


def _raise_incompatible() -> NoReturn:
    raise ValueError("sidecar state and listener are incompatible") from None


def _raise_server_failure() -> NoReturn:
    raise RuntimeError("prebound sidecar server failed") from None


def _is_sync_function(callback: FunctionType) -> bool:
    admitted = False
    try:
        try:
            coroutine_function = _IS_COROUTINE_FUNCTION(callback)
            generator_function = _IS_GENERATOR_FUNCTION(callback)
            async_generator_function = _IS_ASYNC_GENERATOR_FUNCTION(callback)
        except Exception:
            pass
        else:
            admitted = (
                coroutine_function is False
                and generator_function is False
                and async_generator_function is False
            )
    except BaseException:
        del callback, admitted
        raise
    del callback
    return admitted


def _no_running_loop() -> bool:
    try:
        loop = _GET_RUNNING_LOOP()
    except RuntimeError as error:
        return type(error) is RuntimeError
    except Exception:
        return False
    del loop
    return False


def _listener_is_accepting(listener: socket.socket) -> bool:
    result: object = None
    option = _TCP_CONNECTION_INFO
    admitted = False
    try:
        if _PLATFORM == "linux":
            result = _SOCKET_GETSOCKOPT(listener, _SOL_SOCKET, _SO_ACCEPTCONN)
            admitted = type(result) is int and result == 1
        elif _PLATFORM == "darwin" and type(option) is int:
            result = _SOCKET_GETSOCKOPT(
                listener,
                _IPPROTO_TCP,
                option,
                1,
            )
            admitted = type(result) is bytes and result == _TCP_LISTEN_STATE
    except BaseException:
        del listener, result, option, admitted
        raise
    del listener, result, option
    return admitted


def _preflight(state: SidecarState, listener: socket.socket) -> int:
    state_pid: object = None
    state_host: object = None
    state_port: object = None
    current_pid: object = None
    current_thread_ident: object = None
    no_running_loop: object = None
    descriptor: object = None
    family: object = None
    kind: object = None
    protocol: object = None
    address: object = None
    address_host: object = None
    address_port: object = None
    inheritable: object = None
    accepting: object = None
    failed = False
    compatible = False
    try:
        try:
            state_pid = _STATE_PID_GETTER(state, _STATE_TYPE)
            state_host = _STATE_HOST_GETTER(state, _STATE_TYPE)
            state_port = _STATE_PORT_GETTER(state, _STATE_TYPE)
            current_pid = _GETPID()
            current_thread_ident = _GET_IDENT()
            no_running_loop = _no_running_loop()
            descriptor = _SOCKET_FILENO(listener)
            family = _SOCKET_FAMILY_GETTER(listener, _SOCKET_TYPE)
            kind = _SOCKET_KIND_GETTER(listener, _SOCKET_TYPE)
            protocol = _SOCKET_PROTOCOL_GETTER(listener, _SOCKET_TYPE)
            address = _SOCKET_GETSOCKNAME(listener)
            inheritable = _GET_INHERITABLE(descriptor)
            accepting = _listener_is_accepting(listener)
            if type(address) is tuple and len(address) == 2:
                address_host, address_port = address
                compatible = (
                    type(state_pid) is int
                    and type(current_pid) is int
                    and state_pid == current_pid
                    and type(state_host) is str
                    and state_host == _LOOPBACK_HOST
                    and type(state_port) is int
                    and 1 <= state_port <= 65_535
                    and type(current_thread_ident) is int
                    and type(_MAIN_THREAD_IDENT) is int
                    and current_thread_ident == _MAIN_THREAD_IDENT
                    and no_running_loop is True
                    and type(descriptor) is int
                    and descriptor >= 0
                    and type(family) is int
                    and _INT_EQUAL(family, _AF_INET) is True
                    and type(kind) is int
                    and _INT_EQUAL(kind, _SOCK_STREAM) is True
                    and type(protocol) is int
                    and protocol in {0, _IPPROTO_TCP}
                    and type(address_host) is str
                    and address_host == _LOOPBACK_HOST
                    and type(address_port) is int
                    and address_port == state_port
                    and type(inheritable) is bool
                    and inheritable is False
                    and accepting is True
                )
        except Exception:
            failed = True
    except BaseException:
        del (
            state,
            listener,
            state_pid,
            state_host,
            state_port,
            current_pid,
            current_thread_ident,
            no_running_loop,
            descriptor,
            family,
            kind,
            protocol,
            address,
            address_host,
            address_port,
            inheritable,
            accepting,
            failed,
            compatible,
        )
        raise

    if not failed and compatible and type(state_port) is int:
        admitted_port = state_port
        del (
            state,
            listener,
            state_pid,
            state_host,
            state_port,
            current_pid,
            current_thread_ident,
            no_running_loop,
            descriptor,
            family,
            kind,
            protocol,
            address,
            address_host,
            address_port,
            inheritable,
            accepting,
            failed,
            compatible,
        )
        return admitted_port

    del (
        state,
        listener,
        state_pid,
        state_host,
        state_port,
        current_pid,
        current_thread_ident,
        no_running_loop,
        descriptor,
        family,
        kind,
        protocol,
        address,
        address_host,
        address_port,
        inheritable,
        accepting,
        failed,
        compatible,
    )
    raise _CompatibilityFailure from None


def _construct_config(
    app: Callable[..., Any],
    port: int,
    notifier: _OneShotNotifier,
) -> object:
    result: object = None
    try:
        result = _CONFIG_CONSTRUCTOR(
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
    except BaseException:
        del app, port, notifier, result
        raise
    del app, port, notifier
    return result


def _run_server(
    state: SidecarState,
    listener: socket.socket,
    on_started: Callable[[], None],
    port: int,
) -> object:
    app: Callable[..., Any] | None = None
    notifier: _OneShotNotifier | None = None
    config: object = None
    server: object = None
    sockets: list[socket.socket] | None = None
    result: object = None
    try:
        app = _CREATE_SIDECAR_APP(state)
        notifier = _OneShotNotifier(on_started)
        config = _construct_config(app, port, notifier)
        if type(config) is not _CONFIG_TYPE:
            raise _ServerFailure from None
        server = _SERVER_CONSTRUCTOR(config)
        if type(server) is not _SERVER_TYPE:
            raise _ServerFailure from None
        sockets = [listener]
        result = _SERVER_RUN(server, sockets=sockets)
    except BaseException:
        del state, listener, on_started, port, app, notifier, config, server, sockets, result
        raise
    del state, listener, on_started, port, app, notifier, config, server, sockets
    return result


def _close_listener(listener: socket.socket) -> bool:
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


def _close_during_control(listener: socket.socket, active_error: BaseException) -> None:
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


def _serve_owned(
    state: SidecarState,
    listener: socket.socket,
    on_started: Callable[[], None],
    port: int,
) -> bool:
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


def serve_prebound_sidecar_app(
    state: SidecarState,
    listener: socket.socket,
    *,
    on_started: Callable[[], None],
) -> None:
    """Serve one exact private app on the supplied listener until shutdown."""

    if type(state) is not _STATE_TYPE:
        del state, listener, on_started
        _raise_state_type()
    if type(listener) is not _SOCKET_TYPE:
        del state, listener, on_started
        _raise_listener_type()
    if type(on_started) is not _FUNCTION_TYPE:
        del state, listener, on_started
        _raise_hook_type()
    try:
        synchronous_hook = _is_sync_function(on_started)
    except BaseException:
        del state, listener, on_started
        raise
    if not synchronous_hook:
        del state, listener, on_started, synchronous_hook
        _raise_hook_type()
    del synchronous_hook

    compatibility_failed = False
    port: int | None = None
    try:
        port = _preflight(state, listener)
    except _CompatibilityFailure:
        compatibility_failed = True
    except BaseException:
        del state, listener, on_started, compatibility_failed, port
        raise
    if compatibility_failed or type(port) is not int:
        del state, listener, on_started, compatibility_failed, port
        _raise_incompatible()
    del compatibility_failed

    try:
        succeeded = _serve_owned(state, listener, on_started, port)
    except BaseException:
        del state, listener, on_started, port
        raise
    del state, listener, on_started, port
    if not succeeded:
        del succeeded
        _raise_server_failure()
    del succeeded


def _serve_after_owned_admission(
    state: SidecarState,
    listener: socket.socket,
    on_started: FunctionType,
) -> bool:
    synchronous_hook: bool | None = None
    candidate_port: object = None
    admitted_port: int | None = None
    close_ok: bool | None = None
    try:
        try:
            synchronous_hook = _is_sync_function(on_started)
        except Exception:
            pass
        if synchronous_hook is True:
            try:
                candidate_port = _preflight(state, listener)
            except Exception:
                pass
            if type(candidate_port) is int:
                admitted_port = candidate_port
    except BaseException as active_error:
        _close_during_control(listener, active_error)
        del (
            state,
            listener,
            on_started,
            synchronous_hook,
            candidate_port,
            admitted_port,
            close_ok,
            active_error,
        )
        raise

    if synchronous_hook is not True or admitted_port is None:
        try:
            try:
                close_ok = _close_listener(listener)
            except Exception:
                pass
        except BaseException:
            del (
                state,
                listener,
                on_started,
                synchronous_hook,
                candidate_port,
                admitted_port,
                close_ok,
            )
            raise
        del state, listener, on_started
        if close_ok is not True:
            del synchronous_hook, candidate_port, admitted_port, close_ok
            _raise_server_failure()
        del close_ok
        if synchronous_hook is not True:
            del synchronous_hook, candidate_port, admitted_port
            _raise_hook_type()
        del synchronous_hook, candidate_port, admitted_port
        _raise_incompatible()

    try:
        return _serve_owned(state, listener, on_started, admitted_port)
    except BaseException:
        del (
            state,
            listener,
            on_started,
            synchronous_hook,
            candidate_port,
            admitted_port,
            close_ok,
        )
        raise


def serve_owned_prebound_sidecar_app(
    state: SidecarState,
    listener: socket.socket,
    *,
    on_started: Callable[[], None],
) -> None:
    """Consume and serve one exact private app on the supplied listener."""

    if type(state) is not _STATE_TYPE:
        del state, listener, on_started
        _raise_state_type()
    if type(listener) is not _SOCKET_TYPE:
        del state, listener, on_started
        _raise_listener_type()
    if type(on_started) is not _FUNCTION_TYPE:
        del state, listener, on_started
        _raise_hook_type()
    try:
        succeeded = _serve_after_owned_admission(state, listener, on_started)
    except BaseException:
        del state, listener, on_started
        raise
    del state, listener, on_started
    if succeeded is not True:
        del succeeded
        _raise_server_failure()
    del succeeded
