"""Atomic child-side ownership transfer for inherited bootstrap descriptors."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, NoReturn

from .child_bootstrap import SidecarChildBootstrap, encode_sidecar_child_bootstrap
from .owner_lock import OwnerLock
from .runtime_config import SidecarRuntimeConfig
from .startup_channel import StartupWriter
from .state import StateStore

_MAX_DESCRIPTOR: Final = 2_147_483_647
_CLEANUP_NOTE: Final = "sidecar child descriptor adoption cleanup failed"

_BOOTSTRAP_TYPE: Final = SidecarChildBootstrap
_CONFIG_TYPE: Final = SidecarRuntimeConfig
_STORE_TYPE: Final = StateStore
_OWNER_TYPE: Final = OwnerLock
_WRITER_TYPE: Final = StartupWriter
_PATH_TYPE: Final = type(Path())

_ENCODE_BOOTSTRAP: Final = encode_sidecar_child_bootstrap
_STATE_STORE_CONSTRUCTOR: Final = StateStore
_ADOPT_OWNER: Final = OwnerLock.adopt_inherited
_ADOPT_WRITER: Final = StartupWriter.adopt_inherited
_RAW_CLOSE: Final = os.close
_OWNER_CLOSE: Final = OwnerLock.close
_WRITER_CLOSE: Final = StartupWriter.close
_ADD_NOTE: Final = BaseException.add_note
_FSPATH: Final = os.fspath

type _AdoptionResult = tuple[OwnerLock, StartupWriter]
type _ShallowAdmission = tuple[SidecarRuntimeConfig, int, int]


class _AdoptionFailure(Exception):
    pass


def _encode_bootstrap(
    config: SidecarRuntimeConfig,
    owner_lock_fd: int,
    startup_writer_fd: int,
) -> object:
    return _ENCODE_BOOTSTRAP(
        config,
        owner_lock_fd=owner_lock_fd,
        startup_writer_fd=startup_writer_fd,
    )


def _construct_state_store(runtime_root: str, project_id: str) -> object:
    return _STATE_STORE_CONSTRUCTOR(runtime_root, project_id=project_id)


def _adopt_owner(store: StateStore, file_descriptor: int) -> OwnerLock:
    return _ADOPT_OWNER(store, file_descriptor)


def _adopt_writer(file_descriptor: int) -> StartupWriter:
    return _ADOPT_WRITER(file_descriptor)


def _close_raw(file_descriptor: int) -> None:
    _RAW_CLOSE(file_descriptor)


def _close_owner(owner: OwnerLock) -> None:
    _OWNER_CLOSE(owner)


def _close_writer(writer: StartupWriter) -> None:
    _WRITER_CLOSE(writer)


def _add_note(error: BaseException, note: str) -> None:
    _ADD_NOTE(error, note)


def _path_text(path: Path) -> object:
    return _FSPATH(path)


def _make_result(owner: OwnerLock, writer: StartupWriter) -> object:
    return (owner, writer)


def _read_bootstrap_slots(bootstrap: SidecarChildBootstrap) -> object:
    return (
        object.__getattribute__(bootstrap, "config"),
        object.__getattribute__(bootstrap, "owner_lock_fd"),
        object.__getattribute__(bootstrap, "startup_writer_fd"),
    )


def _shallow_admission(value: object) -> _ShallowAdmission:
    if type(value) is not _BOOTSTRAP_TYPE:
        raise _AdoptionFailure
    slots = _read_bootstrap_slots(value)
    if type(slots) is not tuple or len(slots) != 3:
        raise _AdoptionFailure
    config, owner_lock_fd, startup_writer_fd = slots
    if (
        type(config) is not _CONFIG_TYPE
        or type(owner_lock_fd) is not int
        or type(startup_writer_fd) is not int
        or not 3 <= owner_lock_fd <= _MAX_DESCRIPTOR
        or not 3 <= startup_writer_fd <= _MAX_DESCRIPTOR
        or owner_lock_fd == startup_writer_fd
    ):
        raise _AdoptionFailure
    return config, owner_lock_fd, startup_writer_fd


def _canonical_encoding(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) == 8
        and all(type(argument) is str for argument in value)
    )


def _read_config_identity(config: SidecarRuntimeConfig) -> object:
    return (
        object.__getattribute__(config, "runtime_root"),
        object.__getattribute__(config, "project_id"),
    )


def _admit_store(value: object, runtime_root: str, project_id: str) -> StateStore:
    if type(value) is not _STORE_TYPE:
        raise _AdoptionFailure
    store = value
    stored_project_id = object.__getattribute__(store, "project_id")
    stored_runtime_root = object.__getattribute__(store, "runtime_root")
    if (
        type(stored_project_id) is not str
        or stored_project_id != project_id
        or type(stored_runtime_root) is not _PATH_TYPE
    ):
        raise _AdoptionFailure
    stored_runtime_text = _path_text(stored_runtime_root)
    if type(stored_runtime_text) is not str or stored_runtime_text != runtime_root:
        raise _AdoptionFailure
    return store


def _record_cleanup_failure(
    current_control: BaseException | None,
    cleanup_error: BaseException,
    prior_failure: bool,
) -> tuple[BaseException | None, bool]:
    if isinstance(cleanup_error, Exception):
        return current_control, True
    if current_control is None:
        return cleanup_error, prior_failure
    return current_control, True


def _cleanup_resources(
    writer: StartupWriter | None,
    raw_writer_fd: int,
    owner: OwnerLock | None,
    raw_owner_fd: int,
) -> tuple[BaseException | None, bool]:
    cleanup_control: BaseException | None = None
    cleanup_failed = False

    if writer is not None:
        try:
            _close_writer(writer)
        except BaseException as cleanup_error:
            cleanup_control, cleanup_failed = _record_cleanup_failure(
                cleanup_control,
                cleanup_error,
                cleanup_failed,
            )
    elif raw_writer_fd >= 0:
        try:
            _close_raw(raw_writer_fd)
        except BaseException as cleanup_error:
            cleanup_control, cleanup_failed = _record_cleanup_failure(
                cleanup_control,
                cleanup_error,
                cleanup_failed,
            )

    if owner is not None:
        try:
            _close_owner(owner)
        except BaseException as cleanup_error:
            cleanup_control, cleanup_failed = _record_cleanup_failure(
                cleanup_control,
                cleanup_error,
                cleanup_failed,
            )
    elif raw_owner_fd >= 0:
        try:
            _close_raw(raw_owner_fd)
        except BaseException as cleanup_error:
            cleanup_control, cleanup_failed = _record_cleanup_failure(
                cleanup_control,
                cleanup_error,
                cleanup_failed,
            )

    return cleanup_control, cleanup_failed


def _best_effort_cleanup_note(error: BaseException) -> None:
    try:
        _add_note(error, _CLEANUP_NOTE)
    except BaseException:
        pass


def _adopt_pair(value: object) -> _AdoptionResult:
    raw_owner_fd = -1
    raw_writer_fd = -1
    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    active_error: BaseException | None = None

    try:
        config, admitted_owner_fd, admitted_writer_fd = _shallow_admission(value)
        raw_owner_fd = admitted_owner_fd
        raw_writer_fd = admitted_writer_fd

        encoding = _encode_bootstrap(config, raw_owner_fd, raw_writer_fd)
        if not _canonical_encoding(encoding):
            raise _AdoptionFailure
        del encoding

        config_identity = _read_config_identity(config)
        if type(config_identity) is not tuple or len(config_identity) != 2:
            raise _AdoptionFailure
        runtime_root, project_id = config_identity
        if type(runtime_root) is not str or type(project_id) is not str:
            raise _AdoptionFailure
        store = _admit_store(
            _construct_state_store(runtime_root, project_id),
            runtime_root,
            project_id,
        )

        owner_locator = raw_owner_fd
        raw_owner_fd = -1
        owner = _adopt_owner(store, owner_locator)

        writer_locator = raw_writer_fd
        raw_writer_fd = -1
        writer = _adopt_writer(writer_locator)

        candidate = _make_result(owner, writer)
        if (
            type(candidate) is not tuple
            or len(candidate) != 2
            or type(candidate[0]) is not _OWNER_TYPE
            or candidate[0] is not owner
            or type(candidate[1]) is not _WRITER_TYPE
            or candidate[1] is not writer
        ):
            raise _AdoptionFailure
        return candidate
    except BaseException as failure:
        active_error = failure

    if active_error is None:
        active_error = _AdoptionFailure()

    cleanup_control, cleanup_failed = _cleanup_resources(
        writer,
        raw_writer_fd,
        owner,
        raw_owner_fd,
    )
    if not isinstance(active_error, Exception):
        if cleanup_control is not None or cleanup_failed:
            _best_effort_cleanup_note(active_error)
        raise active_error
    if cleanup_control is not None:
        if cleanup_failed:
            _best_effort_cleanup_note(cleanup_control)
        raise cleanup_control
    raise _AdoptionFailure


def _raise_adoption_failure() -> NoReturn:
    raise RuntimeError("sidecar child descriptor adoption failed") from None


def adopt_sidecar_child_descriptors(
    bootstrap: SidecarChildBootstrap,
) -> tuple[OwnerLock, StartupWriter]:
    """Consume one admitted child descriptor pair and return its exact handles."""

    result: _AdoptionResult | None = None
    failed = False
    try:
        result = _adopt_pair(bootstrap)
    except Exception:
        failed = True
    del bootstrap
    if failed or type(result) is not tuple:
        del result, failed
        _raise_adoption_failure()
    del failed
    return result
