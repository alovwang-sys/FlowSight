"""Canonical inert arguments for a future sidecar child process."""

from __future__ import annotations

import os
from typing import Final, NoReturn, cast

from .runtime_config import SidecarRuntimeConfig, prepare_sidecar_runtime_config

CHILD_BOOTSTRAP_SCHEMA_VERSION: Final = 1

_BOOTSTRAP_MARKER: Final = "flowsight-sidecar-bootstrap-v1"
_PROJECT_ROOT_PREFIX: Final = "project-root="
_RUNTIME_ROOT_PREFIX: Final = "runtime-root="
_PROJECT_ID_PREFIX: Final = "project-id="
_REQUESTED_PORT_PREFIX: Final = "requested-port="
_STARTUP_TIMEOUT_PREFIX: Final = "startup-timeout="
_OWNER_LOCK_FD_PREFIX: Final = "owner-lock-fd="
_STARTUP_WRITER_FD_PREFIX: Final = "startup-writer-fd="
_ARGUMENT_COUNT: Final = 8
_MAX_ARGUMENT_BYTES: Final = 9216
_MAX_PATH_CHARACTERS: Final = 4096
_MAX_PATH_BYTES: Final = 4096
_MAX_DESCRIPTOR: Final = 2_147_483_647
_PROJECT_ID_VALUE_PREFIX: Final = "project-v1-"
_PROJECT_ID_HEX_CHARACTERS: Final = frozenset("0123456789abcdef")

_CONFIG_TYPE: Final = SidecarRuntimeConfig
_FSENCODE: Final = os.fsencode
_PREPARE_SIDECAR_RUNTIME_CONFIG: Final = prepare_sidecar_runtime_config

type _ConfigSnapshot = tuple[str, str, str, int | None, float]


class _BootstrapFailure(Exception):
    pass


def _fsencode(value: str) -> object:
    return _FSENCODE(value)


def _prepare_runtime_config(
    project_root: str,
    requested_port: int | None,
    startup_timeout: float,
) -> object:
    return _PREPARE_SIDECAR_RUNTIME_CONFIG(
        project_root,
        requested_port=requested_port,
        startup_timeout=startup_timeout,
    )


def _read_config_slots(config: SidecarRuntimeConfig) -> object:
    return (
        object.__getattribute__(config, "project_root"),
        object.__getattribute__(config, "runtime_root"),
        object.__getattribute__(config, "project_id"),
        object.__getattribute__(config, "requested_port"),
        object.__getattribute__(config, "startup_timeout"),
    )


def _make_bootstrap(
    config: SidecarRuntimeConfig,
    owner_lock_fd: int,
    startup_writer_fd: int,
) -> object:
    result: SidecarChildBootstrap = object.__new__(_BOOTSTRAP_TYPE)
    object.__setattr__(result, "config", config)
    object.__setattr__(result, "owner_lock_fd", owner_lock_fd)
    object.__setattr__(result, "startup_writer_fd", startup_writer_fd)
    return result


def _read_bootstrap_slots(bootstrap: SidecarChildBootstrap) -> object:
    return (
        object.__getattribute__(bootstrap, "config"),
        object.__getattribute__(bootstrap, "owner_lock_fd"),
        object.__getattribute__(bootstrap, "startup_writer_fd"),
    )


class SidecarChildBootstrap:
    """Re-derived child configuration plus two inert descriptor locators."""

    __slots__ = ("config", "owner_lock_fd", "startup_writer_fd")

    config: SidecarRuntimeConfig
    owner_lock_fd: int
    startup_writer_fd: int

    def __new__(cls, /, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("SidecarChildBootstrap must be decoded") from None

    def __init__(self, /, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise TypeError("SidecarChildBootstrap must be decoded") from None

    def __repr__(self) -> str:
        return "<SidecarChildBootstrap>"

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del self, name, value
        raise AttributeError("SidecarChildBootstrap is immutable") from None

    def __delattr__(self, name: str) -> NoReturn:
        del self, name
        raise AttributeError("SidecarChildBootstrap is immutable") from None

    def __copy__(self) -> NoReturn:
        del self
        raise TypeError("SidecarChildBootstrap cannot be serialized") from None

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del self, memo
        raise TypeError("SidecarChildBootstrap cannot be serialized") from None

    def __reduce__(self) -> NoReturn:
        del self
        raise TypeError("SidecarChildBootstrap cannot be serialized") from None

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        del self, protocol
        raise TypeError("SidecarChildBootstrap cannot be serialized") from None

    def __getstate__(self) -> NoReturn:
        del self
        raise TypeError("SidecarChildBootstrap cannot be serialized") from None


_BOOTSTRAP_TYPE: Final = SidecarChildBootstrap


def _raise_invalid() -> NoReturn:
    raise ValueError("sidecar child bootstrap is invalid") from None


def _descriptor(value: object) -> int:
    if type(value) is not int or not 3 <= value <= _MAX_DESCRIPTOR:
        raise _BootstrapFailure
    return value


def _snapshot_config(value: object) -> _ConfigSnapshot:
    if type(value) is not _CONFIG_TYPE:
        raise _BootstrapFailure
    config = value
    slots = _read_config_slots(config)
    if type(slots) is not tuple or len(slots) != 5:
        raise _BootstrapFailure
    project_root, runtime_root, project_id, requested_port, startup_timeout = slots
    if (
        type(project_root) is not str
        or type(runtime_root) is not str
        or type(project_id) is not str
        or (requested_port is not None and type(requested_port) is not int)
        or type(startup_timeout) is not float
    ):
        raise _BootstrapFailure
    return (
        project_root,
        runtime_root,
        project_id,
        requested_port,
        startup_timeout,
    )


def _prepare_config_snapshot(
    project_root: str,
    requested_port: int | None,
    startup_timeout: float,
) -> tuple[SidecarRuntimeConfig, _ConfigSnapshot]:
    candidate = _prepare_runtime_config(project_root, requested_port, startup_timeout)
    snapshot = _snapshot_config(candidate)
    return cast(SidecarRuntimeConfig, candidate), snapshot


def _snapshots_equal(first: _ConfigSnapshot, second: _ConfigSnapshot) -> bool:
    return (
        first[0] == second[0]
        and first[1] == second[1]
        and first[2] == second[2]
        and first[3] == second[3]
        and first[4] == second[4]
    )


def _format_arguments(
    snapshot: _ConfigSnapshot,
    owner_lock_fd: int,
    startup_writer_fd: int,
) -> tuple[str, ...]:
    requested_port = "none" if snapshot[3] is None else str(snapshot[3])
    return (
        _BOOTSTRAP_MARKER,
        f"{_PROJECT_ROOT_PREFIX}{snapshot[0]}",
        f"{_RUNTIME_ROOT_PREFIX}{snapshot[1]}",
        f"{_PROJECT_ID_PREFIX}{snapshot[2]}",
        f"{_REQUESTED_PORT_PREFIX}{requested_port}",
        f"{_STARTUP_TIMEOUT_PREFIX}{float.hex(snapshot[4])}",
        f"{_OWNER_LOCK_FD_PREFIX}{owner_lock_fd}",
        f"{_STARTUP_WRITER_FD_PREFIX}{startup_writer_fd}",
    )


def _argument_byte_lengths(arguments: tuple[str, ...]) -> tuple[int, ...]:
    lengths: list[int] = []
    total = 0
    for argument in arguments:
        encoded = _fsencode(argument)
        if type(encoded) is not bytes or b"\x00" in encoded:
            raise _BootstrapFailure
        byte_length = len(encoded)
        total += byte_length + 1
        if total > _MAX_ARGUMENT_BYTES:
            raise _BootstrapFailure
        lengths.append(byte_length)
    return tuple(lengths)


def _encode_bootstrap(
    config: object,
    owner_lock_fd: object,
    startup_writer_fd: object,
) -> tuple[str, ...]:
    if type(config) is not _CONFIG_TYPE:
        raise _BootstrapFailure
    normalized_owner_fd = _descriptor(owner_lock_fd)
    normalized_writer_fd = _descriptor(startup_writer_fd)
    if normalized_owner_fd == normalized_writer_fd:
        raise _BootstrapFailure
    source_snapshot = _snapshot_config(config)
    _prepared, prepared_snapshot = _prepare_config_snapshot(
        source_snapshot[0],
        source_snapshot[3],
        source_snapshot[4],
    )
    if not _snapshots_equal(source_snapshot, prepared_snapshot):
        raise _BootstrapFailure
    arguments = _format_arguments(
        prepared_snapshot,
        normalized_owner_fd,
        normalized_writer_fd,
    )
    _argument_byte_lengths(arguments)
    return arguments


def _valid_wire_text(value: str) -> bool:
    return all(ord(character) >= 32 and ord(character) != 127 for character in value)


def _field(arguments: tuple[str, ...], index: int, prefix: str) -> str:
    argument = arguments[index]
    if argument[: len(prefix)] != prefix:
        raise _BootstrapFailure
    value = argument[len(prefix) :]
    if not value:
        raise _BootstrapFailure
    return value


def _parse_decimal(value: str, *, maximum_digits: int, minimum: int, maximum: int) -> int:
    if (
        not value
        or len(value) > maximum_digits
        or any(character < "0" or character > "9" for character in value)
        or (len(value) > 1 and value[0] == "0")
    ):
        raise _BootstrapFailure
    result = int(value, 10)
    if not minimum <= result <= maximum or str(result) != value:
        raise _BootstrapFailure
    return result


def _parse_port(value: str) -> int | None:
    if value == "none":
        return None
    return _parse_decimal(value, maximum_digits=5, minimum=0, maximum=65_535)


def _parse_timeout(value: str) -> float:
    result = float.fromhex(value)
    if not 0.0 < result <= 30.0 or result.hex() != value:
        raise _BootstrapFailure
    return result


def _valid_project_id(value: str) -> bool:
    digest = value[len(_PROJECT_ID_VALUE_PREFIX) :]
    return (
        value[: len(_PROJECT_ID_VALUE_PREFIX)] == _PROJECT_ID_VALUE_PREFIX
        and len(digest) == 64
        and all(character in _PROJECT_ID_HEX_CHARACTERS for character in digest)
    )


def _decode_bootstrap(arguments: object) -> SidecarChildBootstrap:
    if type(arguments) is not tuple or len(arguments) != _ARGUMENT_COUNT:
        raise _BootstrapFailure
    exact_arguments = cast(tuple[object, ...], arguments)
    if any(type(argument) is not str for argument in exact_arguments):
        raise _BootstrapFailure
    typed_arguments = cast(tuple[str, ...], exact_arguments)
    if any(not _valid_wire_text(argument) for argument in typed_arguments):
        raise _BootstrapFailure
    byte_lengths = _argument_byte_lengths(typed_arguments)
    if typed_arguments[0] != _BOOTSTRAP_MARKER:
        raise _BootstrapFailure

    project_root = _field(typed_arguments, 1, _PROJECT_ROOT_PREFIX)
    runtime_root = _field(typed_arguments, 2, _RUNTIME_ROOT_PREFIX)
    project_id = _field(typed_arguments, 3, _PROJECT_ID_PREFIX)
    requested_port_text = _field(typed_arguments, 4, _REQUESTED_PORT_PREFIX)
    startup_timeout_text = _field(typed_arguments, 5, _STARTUP_TIMEOUT_PREFIX)
    owner_lock_fd_text = _field(typed_arguments, 6, _OWNER_LOCK_FD_PREFIX)
    startup_writer_fd_text = _field(typed_arguments, 7, _STARTUP_WRITER_FD_PREFIX)

    if (
        len(project_root) > _MAX_PATH_CHARACTERS
        or len(runtime_root) > _MAX_PATH_CHARACTERS
        or byte_lengths[1] - len(_PROJECT_ROOT_PREFIX) > _MAX_PATH_BYTES
        or byte_lengths[2] - len(_RUNTIME_ROOT_PREFIX) > _MAX_PATH_BYTES
        or not _valid_project_id(project_id)
    ):
        raise _BootstrapFailure

    requested_port = _parse_port(requested_port_text)
    startup_timeout = _parse_timeout(startup_timeout_text)
    owner_lock_fd = _parse_decimal(
        owner_lock_fd_text,
        maximum_digits=10,
        minimum=3,
        maximum=_MAX_DESCRIPTOR,
    )
    startup_writer_fd = _parse_decimal(
        startup_writer_fd_text,
        maximum_digits=10,
        minimum=3,
        maximum=_MAX_DESCRIPTOR,
    )
    if owner_lock_fd == startup_writer_fd:
        raise _BootstrapFailure

    prepared_config, prepared_snapshot = _prepare_config_snapshot(
        project_root,
        requested_port,
        startup_timeout,
    )
    proof_snapshot: _ConfigSnapshot = (
        project_root,
        runtime_root,
        project_id,
        requested_port,
        startup_timeout,
    )
    if not _snapshots_equal(prepared_snapshot, proof_snapshot):
        raise _BootstrapFailure
    if _format_arguments(prepared_snapshot, owner_lock_fd, startup_writer_fd) != typed_arguments:
        raise _BootstrapFailure

    candidate = _make_bootstrap(prepared_config, owner_lock_fd, startup_writer_fd)
    if type(candidate) is not _BOOTSTRAP_TYPE:
        raise _BootstrapFailure
    bootstrap = candidate
    slots = _read_bootstrap_slots(bootstrap)
    if type(slots) is not tuple or len(slots) != 3:
        raise _BootstrapFailure
    stored_config, stored_owner_fd, stored_writer_fd = slots
    if (
        stored_config is not prepared_config
        or type(stored_owner_fd) is not int
        or stored_owner_fd != owner_lock_fd
        or type(stored_writer_fd) is not int
        or stored_writer_fd != startup_writer_fd
    ):
        raise _BootstrapFailure
    return bootstrap


def encode_sidecar_child_bootstrap(
    config: SidecarRuntimeConfig,
    *,
    owner_lock_fd: int,
    startup_writer_fd: int,
) -> tuple[str, ...]:
    """Return the one canonical inert argv suffix for a future launcher."""

    result: tuple[str, ...] | None = None
    failed = False
    try:
        result = _encode_bootstrap(config, owner_lock_fd, startup_writer_fd)
    except Exception:
        failed = True
    del config, owner_lock_fd, startup_writer_fd
    if failed or type(result) is not tuple:
        del result, failed
        _raise_invalid()
    del failed
    return result


def decode_sidecar_child_bootstrap(
    arguments: tuple[str, ...],
) -> SidecarChildBootstrap:
    """Re-derive one exact child bootstrap from canonical inert arguments."""

    result: SidecarChildBootstrap | None = None
    failed = False
    try:
        result = _decode_bootstrap(arguments)
    except Exception:
        failed = True
    del arguments
    if failed or type(result) is not _BOOTSTRAP_TYPE:
        del result, failed
        _raise_invalid()
    del failed
    return result
