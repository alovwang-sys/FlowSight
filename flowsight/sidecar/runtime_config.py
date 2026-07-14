"""Exact scalar configuration prepared before sidecar child handoff."""

from __future__ import annotations

import hashlib
import math
import os
import stat
from pathlib import Path
from typing import Final, NoReturn, SupportsIndex, cast

from platformdirs import user_runtime_path

from .state import StateStore

_MAX_PATH_CHARACTERS: Final = 4096
_MAX_PATH_BYTES: Final = 4096
_MAX_STARTUP_TIMEOUT_SECONDS: Final = 30.0
_PROJECT_ID_PREFIX: Final = b"flowsight-project-v1\x00"
_PROJECT_ID_HEX_CHARACTERS: Final = frozenset("0123456789abcdef")

_PLATFORM_PATH_TYPE: Final = type(Path())
_STAT_RESULT_TYPE: Final = os.stat_result
_STATE_STORE_TYPE: Final = StateStore
_PATH_SEPARATOR: Final = os.sep

# Capture reviewed dependencies at import. Tests may replace only these private
# seams for deterministic fault injection; public attribute replacement later
# cannot redirect production dispatch.
_USER_RUNTIME_PATH: Final = user_runtime_path
_STATE_STORE_CONSTRUCTOR: Final = StateStore
_DIRNAME: Final = os.path.dirname
_FSPATH: Final = os.fspath
_FSENCODE: Final = os.fsencode
_ISABS: Final = os.path.isabs
_REALPATH: Final = os.path.realpath
_STAT: Final = os.stat
_IS_DIRECTORY: Final = stat.S_ISDIR
_SHA256: Final = hashlib.sha256
_ISFINITE: Final = math.isfinite


class _RuntimeConfigurationFailure(Exception):
    pass


def _fspath(value: str | Path) -> object:
    return _FSPATH(value)


def _fsencode(value: str) -> object:
    return _FSENCODE(value)


def _is_absolute(value: str) -> object:
    return _ISABS(value)


def _dirname(value: str) -> object:
    return _DIRNAME(value)


def _resolve_project_root(value: str) -> object:
    return _REALPATH(value, strict=True)


def _read_project_root_stat(value: str) -> object:
    return _STAT(value)


def _is_directory(mode: int) -> object:
    return _IS_DIRECTORY(mode)


def _derive_project_digest(material: bytes) -> object:
    return _SHA256(material).hexdigest()


def _query_user_runtime_path() -> object:
    return _USER_RUNTIME_PATH(
        "flowsight",
        appauthor=False,
        ensure_exists=False,
    )


def _construct_state_store(runtime_path: Path, project_id: str) -> object:
    return _STATE_STORE_CONSTRUCTOR(runtime_path, project_id=project_id)


def _read_state_store_fields(store: StateStore) -> object:
    return object.__getattribute__(store, "__dict__")


def _read_config_slots(config: SidecarRuntimeConfig) -> object:
    return (
        object.__getattribute__(config, "project_root"),
        object.__getattribute__(config, "runtime_root"),
        object.__getattribute__(config, "project_id"),
        object.__getattribute__(config, "requested_port"),
        object.__getattribute__(config, "startup_timeout"),
    )


class SidecarRuntimeConfig:
    """Private parent/child scalar inputs with a non-revealing repr."""

    __slots__ = (
        "project_root",
        "runtime_root",
        "project_id",
        "requested_port",
        "startup_timeout",
    )

    project_root: str
    runtime_root: str
    project_id: str
    requested_port: int | None
    startup_timeout: float

    def __new__(cls, /, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("SidecarRuntimeConfig must be prepared") from None

    def __init__(self, /, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise TypeError("SidecarRuntimeConfig must be prepared") from None

    def __repr__(self) -> str:
        return "<SidecarRuntimeConfig>"

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("SidecarRuntimeConfig is immutable") from None

    def __delattr__(self, name: str) -> NoReturn:
        del name
        raise AttributeError("SidecarRuntimeConfig is immutable") from None

    def __eq__(self, other: object) -> bool:
        if type(other) is not _CONFIG_TYPE:
            return NotImplemented
        return (
            self.project_root == other.project_root
            and self.runtime_root == other.runtime_root
            and self.project_id == other.project_id
            and self.requested_port == other.requested_port
            and self.startup_timeout == other.startup_timeout
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.project_root,
                self.runtime_root,
                self.project_id,
                self.requested_port,
                self.startup_timeout,
            )
        )

    def __reduce__(self) -> NoReturn:
        raise TypeError("SidecarRuntimeConfig cannot be serialized") from None

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("SidecarRuntimeConfig cannot be serialized") from None

    def __getstate__(self) -> NoReturn:
        raise TypeError("SidecarRuntimeConfig cannot be serialized") from None


_CONFIG_TYPE: Final = SidecarRuntimeConfig


def _raise_project_root_type() -> NoReturn:
    raise TypeError("project_root must be an exact built-in str or platform Path") from None


def _raise_project_root_invalid() -> NoReturn:
    raise ValueError("project_root is invalid") from None


def _raise_port_invalid() -> NoReturn:
    raise ValueError("requested_port must be None or an exact built-in int in 0..65535") from None


def _raise_timeout_type() -> NoReturn:
    raise TypeError("startup_timeout must be a built-in int or float") from None


def _raise_timeout_invalid() -> NoReturn:
    raise ValueError("startup_timeout must be finite, positive, and at most 30 seconds") from None


def _raise_runtime_failure() -> NoReturn:
    raise RuntimeError("sidecar runtime configuration failed") from None


def _valid_path_text(value: object) -> tuple[str, bytes] | None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_PATH_CHARACTERS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    try:
        encoded = _fsencode(value)
    except Exception:
        return None
    if type(encoded) is not bytes or not 1 <= len(encoded) <= _MAX_PATH_BYTES:
        return None
    return value, encoded


def _path_has_parent_reference(value: str) -> bool:
    return ".." in value.split(_PATH_SEPARATOR)


def _is_filesystem_root(value: str) -> bool:
    parent = _dirname(value)
    return type(parent) is not str or parent == value


def _canonical_project_root(project_root: str | Path) -> tuple[str, bytes]:
    result: tuple[str, bytes] | None = None
    failed = False
    try:
        raw_root = project_root if type(project_root) is str else _fspath(project_root)
        raw_validated = _valid_path_text(raw_root)
        if raw_validated is None:
            raise _RuntimeConfigurationFailure

        canonical_root = _resolve_project_root(raw_validated[0])
        canonical_validated = _valid_path_text(canonical_root)
        if canonical_validated is None or _is_absolute(canonical_validated[0]) is not True:
            raise _RuntimeConfigurationFailure
        if _path_has_parent_reference(canonical_validated[0]) or _is_filesystem_root(
            canonical_validated[0]
        ):
            raise _RuntimeConfigurationFailure

        metadata = _read_project_root_stat(canonical_validated[0])
        if type(metadata) is not _STAT_RESULT_TYPE:
            raise _RuntimeConfigurationFailure
        mode = metadata.st_mode
        if type(mode) is not int or _is_directory(mode) is not True:
            raise _RuntimeConfigurationFailure
        result = canonical_validated
    except Exception:
        failed = True
    if failed or result is None:
        _raise_project_root_invalid()
    return result


def _exact_absolute_path_text(value: object) -> str:
    if type(value) is not _PLATFORM_PATH_TYPE:
        raise _RuntimeConfigurationFailure
    raw_path = _fspath(value)
    validated = _valid_path_text(raw_path)
    if validated is None or _is_absolute(validated[0]) is not True:
        raise _RuntimeConfigurationFailure
    if _path_has_parent_reference(validated[0]) or _is_filesystem_root(validated[0]):
        raise _RuntimeConfigurationFailure
    return validated[0]


def _derive_project_id(encoded_project_root: bytes) -> str:
    digest = _derive_project_digest(_PROJECT_ID_PREFIX + encoded_project_root)
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in _PROJECT_ID_HEX_CHARACTERS for character in digest)
    ):
        raise _RuntimeConfigurationFailure
    return f"project-v1-{digest}"


def _runtime_root(project_id: str) -> str:
    runtime_path = _query_user_runtime_path()
    _exact_absolute_path_text(runtime_path)

    store = _construct_state_store(cast(Path, runtime_path), project_id)
    if type(store) is not _STATE_STORE_TYPE:
        raise _RuntimeConfigurationFailure
    raw_fields = _read_state_store_fields(store)
    if type(raw_fields) is not dict:
        raise _RuntimeConfigurationFailure
    if any(type(field_name) is not str for field_name in raw_fields):
        raise _RuntimeConfigurationFailure
    try:
        stored_project_id = raw_fields["project_id"]
        stored_runtime_root = raw_fields["runtime_root"]
    except KeyError:
        raise _RuntimeConfigurationFailure from None
    if type(stored_project_id) is not str or stored_project_id != project_id:
        raise _RuntimeConfigurationFailure
    return _exact_absolute_path_text(stored_runtime_root)


def _make_config(
    *,
    project_root: str,
    runtime_root: str,
    project_id: str,
    requested_port: int | None,
    startup_timeout: float,
) -> SidecarRuntimeConfig:
    result: SidecarRuntimeConfig = object.__new__(_CONFIG_TYPE)
    object.__setattr__(result, "project_root", project_root)
    object.__setattr__(result, "runtime_root", runtime_root)
    object.__setattr__(result, "project_id", project_id)
    object.__setattr__(result, "requested_port", requested_port)
    object.__setattr__(result, "startup_timeout", startup_timeout)
    return result


def _admit_config(
    candidate: object,
    *,
    project_root: str,
    runtime_root: str,
    project_id: str,
    requested_port: int | None,
    startup_timeout: float,
) -> SidecarRuntimeConfig:
    if type(candidate) is not _CONFIG_TYPE:
        raise _RuntimeConfigurationFailure
    raw_slots = _read_config_slots(candidate)
    if type(raw_slots) is not tuple or len(raw_slots) != 5:
        raise _RuntimeConfigurationFailure
    (
        raw_project_root,
        raw_runtime_root,
        raw_project_id,
        raw_requested_port,
        raw_startup_timeout,
    ) = raw_slots
    if (
        type(raw_project_root) is not str
        or type(raw_runtime_root) is not str
        or type(raw_project_id) is not str
        or type(raw_requested_port) is not type(requested_port)
        or type(raw_startup_timeout) is not float
    ):
        raise _RuntimeConfigurationFailure
    if (
        raw_project_root != project_root
        or raw_runtime_root != runtime_root
        or raw_project_id != project_id
        or raw_requested_port != requested_port
        or raw_startup_timeout != startup_timeout
    ):
        raise _RuntimeConfigurationFailure
    return candidate


def _prepare_runtime_config(
    project_root: str,
    encoded_project_root: bytes,
    requested_port: int | None,
    startup_timeout: float,
) -> SidecarRuntimeConfig:
    project_id = _derive_project_id(encoded_project_root)
    runtime_root = _runtime_root(project_id)
    candidate = _make_config(
        project_root=project_root,
        runtime_root=runtime_root,
        project_id=project_id,
        requested_port=requested_port,
        startup_timeout=startup_timeout,
    )
    return _admit_config(
        candidate,
        project_root=project_root,
        runtime_root=runtime_root,
        project_id=project_id,
        requested_port=requested_port,
        startup_timeout=startup_timeout,
    )


def _validate_requested_port(requested_port: object) -> int | None:
    if requested_port is None:
        return None
    if type(requested_port) is not int or not 0 <= requested_port <= 65_535:
        _raise_port_invalid()
    return requested_port


def _validate_startup_timeout(startup_timeout: object) -> float:
    if type(startup_timeout) not in {int, float}:
        _raise_timeout_type()
    if type(startup_timeout) is int:
        integer_timeout = startup_timeout
        if integer_timeout <= 0 or integer_timeout > _MAX_STARTUP_TIMEOUT_SECONDS:
            _raise_timeout_invalid()
        return float(integer_timeout)
    float_timeout = cast(float, startup_timeout)
    if (
        _ISFINITE(float_timeout) is not True
        or float_timeout <= 0.0
        or float_timeout > _MAX_STARTUP_TIMEOUT_SECONDS
    ):
        _raise_timeout_invalid()
    return float_timeout


def prepare_sidecar_runtime_config(
    project_root: str | Path,
    *,
    requested_port: int | None = None,
    startup_timeout: float = 5.0,
) -> SidecarRuntimeConfig:
    """Return exact private scalars without launching or touching runtime state."""

    if type(project_root) not in {str, _PLATFORM_PATH_TYPE}:
        _raise_project_root_type()
    normalized_port = _validate_requested_port(requested_port)
    normalized_timeout = _validate_startup_timeout(startup_timeout)
    canonical_root, encoded_root = _canonical_project_root(project_root)

    result: SidecarRuntimeConfig | None = None
    failed = False
    try:
        result = _prepare_runtime_config(
            canonical_root,
            encoded_root,
            normalized_port,
            normalized_timeout,
        )
    except Exception:
        failed = True
    if failed or type(result) is not _CONFIG_TYPE:
        _raise_runtime_failure()
    return result
