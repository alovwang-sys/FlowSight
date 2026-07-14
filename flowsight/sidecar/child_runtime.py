"""One exact child-only sidecar startup transaction."""

from __future__ import annotations

import socket
from typing import Final, NoReturn

from .child_preparation import prepare_sidecar_child
from .listener import bind_loopback_listener
from .owner_lock import OwnerLock
from .runtime_config import SidecarRuntimeConfig
from .server_runtime import serve_owned_prebound_sidecar_app
from .startup_channel import (
    StartupFailure,
    StartupFailureCode,
    StartupReady,
    StartupWriter,
)
from .startup_state import create_startup_state
from .state import SidecarState, StateStore

_ARGUMENTS_TYPE: Final = tuple
_CONFIG_TYPE: Final = SidecarRuntimeConfig
_OWNER_TYPE: Final = OwnerLock
_WRITER_TYPE: Final = StartupWriter
_STORE_TYPE: Final = StateStore
_STATE_TYPE: Final = SidecarState
_SOCKET_TYPE: Final = socket.socket
_READY_TYPE: Final = StartupReady
_FAILURE_TYPE: Final = StartupFailure

_OBJECT_GETATTRIBUTE: Final = object.__getattribute__
_PREPARE_CHILD: Final = prepare_sidecar_child
_STATE_STORE_CONSTRUCTOR: Final = StateStore
_BIND_LOOPBACK_LISTENER: Final = bind_loopback_listener
_CREATE_STARTUP_STATE: Final = create_startup_state
_STATE_PUBLISH: Final = StateStore.publish
_STATE_REMOVE_IF_OWNED: Final = StateStore.remove_if_owned
_SERVE_OWNED_PREBOUND: Final = serve_owned_prebound_sidecar_app
_STARTUP_READY_CONSTRUCTOR: Final = StartupReady
_STARTUP_FAILURE_CONSTRUCTOR: Final = StartupFailure
_STARTUP_FAILURE_CODE: Final = StartupFailureCode.SIDECAR_STARTUP_FAILED
_WRITER_SEND: Final = StartupWriter.send
_WRITER_CLOSE: Final = StartupWriter.close
_OWNER_CLOSE: Final = OwnerLock.close
_SOCKET_CLOSE: Final = socket.socket.close
_ADD_NOTE: Final = BaseException.add_note

_CLEANUP_NOTE: Final = "sidecar child transaction cleanup failed"


class _TransactionFailure(Exception):
    pass


def _config_runtime_root(config: SidecarRuntimeConfig) -> object:
    return _OBJECT_GETATTRIBUTE(config, "runtime_root")


def _config_project_id(config: SidecarRuntimeConfig) -> object:
    return _OBJECT_GETATTRIBUTE(config, "project_id")


def _config_requested_port(config: SidecarRuntimeConfig) -> object:
    return _OBJECT_GETATTRIBUTE(config, "requested_port")


def _state_startup_id(state: SidecarState) -> object:
    return _OBJECT_GETATTRIBUTE(state, "startup_id")


def _state_pid(state: SidecarState) -> object:
    return _OBJECT_GETATTRIBUTE(state, "pid")


def _state_port(state: SidecarState) -> object:
    return _OBJECT_GETATTRIBUTE(state, "port")


_CONFIG_RUNTIME_ROOT_GETTER: Final = _config_runtime_root
_CONFIG_PROJECT_ID_GETTER: Final = _config_project_id
_CONFIG_REQUESTED_PORT_GETTER: Final = _config_requested_port
_STATE_STARTUP_ID_GETTER: Final = _state_startup_id
_STATE_PID_GETTER: Final = _state_pid
_STATE_PORT_GETTER: Final = _state_port


def _raise_arguments_type() -> NoReturn:
    raise TypeError("arguments must be an exact built-in tuple") from None


def _raise_transaction_failure() -> NoReturn:
    raise RuntimeError("sidecar child transaction failed") from None


def _admit_prepared(
    candidate: object,
) -> tuple[SidecarRuntimeConfig, OwnerLock, StartupWriter]:
    if type(candidate) is not _ARGUMENTS_TYPE or len(candidate) != 3:
        raise _TransactionFailure
    config = candidate[0]
    owner = candidate[1]
    writer = candidate[2]
    if (
        type(config) is not _CONFIG_TYPE
        or type(owner) is not _OWNER_TYPE
        or type(writer) is not _WRITER_TYPE
    ):
        del config, owner, writer
        raise _TransactionFailure
    return config, owner, writer


def _run_adopted_child(
    config: SidecarRuntimeConfig,
    owner: OwnerLock,
    writer: StartupWriter,
) -> bool:
    runtime_root: object = None
    project_id: object = None
    requested_port: object = None
    store: StateStore | None = None
    listener: socket.socket | None = None
    state: SidecarState | None = None
    publication_result: object = None
    bridge_result: object = None
    state_startup_id: object = None
    listener_close_result: object = None
    removal_result: object = None
    failure_message: object = None
    failure_send_result: object = None
    writer_close_result: object = None
    owner_close_result: object = None
    publication_attempted = False
    publication_succeeded = False
    ready_attempted = False
    ready_succeeded = False
    failure_attempted = False
    listener_relinquished = False
    ordinary_failure = False
    cleanup_failed = False
    active_control: BaseException | None = None
    cleanup_control: BaseException | None = None
    on_started: object = None
    try:
        try:
            runtime_root = _CONFIG_RUNTIME_ROOT_GETTER(config)
            project_id = _CONFIG_PROJECT_ID_GETTER(config)
            requested_port = _CONFIG_REQUESTED_PORT_GETTER(config)
            if (
                type(runtime_root) is not str
                or type(project_id) is not str
                or (requested_port is not None and type(requested_port) is not int)
            ):
                raise _TransactionFailure

            store_candidate = _STATE_STORE_CONSTRUCTOR(runtime_root, project_id=project_id)
            if type(store_candidate) is not _STORE_TYPE:
                del store_candidate
                raise _TransactionFailure
            store = store_candidate

            listener_candidate = _BIND_LOOPBACK_LISTENER(requested_port)
            if type(listener_candidate) is not _SOCKET_TYPE:
                del listener_candidate
                raise _TransactionFailure
            listener = listener_candidate

            state_candidate = _CREATE_STARTUP_STATE(store, listener)
            if type(state_candidate) is not _STATE_TYPE:
                del state_candidate
                raise _TransactionFailure
            state = state_candidate

            publication_attempted = True
            publication_result = _STATE_PUBLISH(store, state)
            if publication_result is not None:
                raise _TransactionFailure
            publication_succeeded = True

            def on_started_hook() -> None:
                nonlocal ready_attempted, ready_succeeded, state, writer
                message: object = None
                send_result: object = None
                try:
                    if ready_attempted:
                        raise _TransactionFailure
                    startup_id = _STATE_STARTUP_ID_GETTER(state)
                    sidecar_pid = _STATE_PID_GETTER(state)
                    port = _STATE_PORT_GETTER(state)
                    if (
                        type(startup_id) is not str
                        or type(sidecar_pid) is not int
                        or type(port) is not int
                    ):
                        del startup_id, sidecar_pid, port
                        raise _TransactionFailure
                    message = _STARTUP_READY_CONSTRUCTOR(
                        startup_id=startup_id,
                        sidecar_pid=sidecar_pid,
                        port=port,
                    )
                    del startup_id, sidecar_pid, port
                    if type(message) is not _READY_TYPE:
                        raise _TransactionFailure
                    ready_attempted = True
                    send_result = _WRITER_SEND(writer, message)
                    if send_result is not None:
                        raise _TransactionFailure
                    ready_succeeded = True
                except BaseException:
                    del message, send_result
                    raise
                del message, send_result

            on_started = on_started_hook
            listener_relinquished = True
            bridge_result = _SERVE_OWNED_PREBOUND(
                state,
                listener,
                on_started=on_started_hook,
            )
            if bridge_result is not None or ready_succeeded is not True:
                raise _TransactionFailure
        except Exception:
            ordinary_failure = True
        except BaseException as error:
            active_control = error
            raise
    finally:
        if listener is not None and listener_relinquished is False:
            try:
                listener_close_result = _SOCKET_CLOSE(listener)
                if listener_close_result is not None:
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            except BaseException as error:
                if active_control is None and cleanup_control is None:
                    cleanup_control = error
                else:
                    cleanup_failed = True

        if publication_attempted and store is not None and state is not None:
            try:
                state_startup_id = _STATE_STARTUP_ID_GETTER(state)
                if type(state_startup_id) is not str:
                    raise _TransactionFailure
                removal_result = _STATE_REMOVE_IF_OWNED(store, state_startup_id)
                if publication_succeeded:
                    if removal_result is not True:
                        cleanup_failed = True
                elif type(removal_result) is not bool:
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            except BaseException as error:
                if active_control is None and cleanup_control is None:
                    cleanup_control = error
                else:
                    cleanup_failed = True

        if (
            active_control is None
            and cleanup_control is None
            and ready_attempted is False
            and failure_attempted is False
        ):
            try:
                failure_message = _STARTUP_FAILURE_CONSTRUCTOR(_STARTUP_FAILURE_CODE)
                if type(failure_message) is not _FAILURE_TYPE:
                    raise _TransactionFailure
                failure_attempted = True
                failure_send_result = _WRITER_SEND(writer, failure_message)
                if failure_send_result is not None:
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            except BaseException as error:
                if active_control is None and cleanup_control is None:
                    cleanup_control = error
                else:
                    cleanup_failed = True

        try:
            writer_close_result = _WRITER_CLOSE(writer)
            if writer_close_result is not None:
                cleanup_failed = True
        except Exception:
            cleanup_failed = True
        except BaseException as error:
            if active_control is None and cleanup_control is None:
                cleanup_control = error
            else:
                cleanup_failed = True

        try:
            owner_close_result = _OWNER_CLOSE(owner)
            if owner_close_result is not None:
                cleanup_failed = True
        except Exception:
            cleanup_failed = True
        except BaseException as error:
            if active_control is None and cleanup_control is None:
                cleanup_control = error
            else:
                cleanup_failed = True

        if active_control is not None and cleanup_failed:
            try:
                _ADD_NOTE(active_control, _CLEANUP_NOTE)
            except BaseException:
                pass

        del (
            config,
            owner,
            writer,
            runtime_root,
            project_id,
            requested_port,
            store,
            listener,
            state,
            publication_result,
            bridge_result,
            state_startup_id,
            listener_close_result,
            removal_result,
            failure_message,
            failure_send_result,
            writer_close_result,
            owner_close_result,
            on_started,
        )

    if cleanup_control is not None:
        if cleanup_failed:
            try:
                _ADD_NOTE(cleanup_control, _CLEANUP_NOTE)
            except BaseException:
                pass
        del (
            publication_attempted,
            publication_succeeded,
            ready_attempted,
            ready_succeeded,
            failure_attempted,
            listener_relinquished,
            ordinary_failure,
            cleanup_failed,
            active_control,
        )
        raise cleanup_control

    succeeded = (
        ordinary_failure is False
        and publication_succeeded is True
        and ready_succeeded is True
        and cleanup_failed is False
    )
    del (
        publication_attempted,
        publication_succeeded,
        ready_attempted,
        ready_succeeded,
        failure_attempted,
        listener_relinquished,
        ordinary_failure,
        cleanup_failed,
        active_control,
        cleanup_control,
    )
    return succeeded


def run_sidecar_child(arguments: tuple[str, ...]) -> None:
    """Run one adopted sidecar child transaction until server shutdown."""

    if type(arguments) is not _ARGUMENTS_TYPE:
        del arguments
        _raise_arguments_type()

    prepared: object = None
    config: SidecarRuntimeConfig | None = None
    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    preparation_failed = False
    try:
        prepared = _PREPARE_CHILD(arguments)
        config, owner, writer = _admit_prepared(prepared)
    except Exception:
        preparation_failed = True
    except BaseException:
        del arguments, prepared, config, owner, writer, preparation_failed
        raise
    del arguments, prepared
    if preparation_failed:
        del config, owner, writer, preparation_failed
        _raise_transaction_failure()
    del preparation_failed
    if config is None or owner is None or writer is None:
        del config, owner, writer
        _raise_transaction_failure()

    try:
        succeeded = _run_adopted_child(config, owner, writer)
    except BaseException:
        del config, owner, writer
        raise
    del config, owner, writer
    if succeeded is not True:
        del succeeded
        _raise_transaction_failure()
    del succeeded
