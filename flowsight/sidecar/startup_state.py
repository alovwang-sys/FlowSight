"""Exact in-memory startup identity for one already-bound sidecar listener."""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn, cast

from .state import LOOPBACK_HOST, SidecarState, StateStore

_DATABASE_NAME: Final = "events.sqlite3"
_STARTUP_ID_CHARACTERS: Final = frozenset("0123456789abcdef")
_TOKEN_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_PLATFORM_PATH_TYPE: Final = type(Path())
_SOCKET_TYPE: Final = socket.socket
_STATE_STORE_TYPE: Final = StateStore
_SIDECAR_STATE_TYPE: Final = SidecarState
_ADDRESS_FAMILY_TYPE: Final = type(socket.AF_INET)
_SOCKET_KIND_TYPE: Final = type(socket.SOCK_STREAM)
_TCP_CONNECTION_INFO: Final[object] = getattr(socket, "TCP_CONNECTION_INFO", None)
_TCP_LISTEN_STATE: Final = b"\x01"


class StartupStateErrorCode(StrEnum):
    """Stable, non-sensitive startup-state generation failure codes."""

    STARTUP_STATE_GENERATION_FAILED = "STARTUP_STATE_GENERATION_FAILED"


class StartupStateError(RuntimeError):
    """An exact sidecar startup state could not be created safely."""

    def __init__(self, code: StartupStateErrorCode) -> None:
        if type(code) is not StartupStateErrorCode:
            raise TypeError("code must be an exact StartupStateErrorCode")
        self.code = code
        super().__init__(f"sidecar startup state failed ({code})")


class _StartupStateGenerationFailure(Exception):
    pass


def _reject() -> NoReturn:
    raise _StartupStateGenerationFailure


def _valid_text(value: object, *, maximum: int) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _read_store_fields(store: StateStore) -> object:
    """Read only the exact instance dictionary, bypassing caller hooks."""

    return object.__getattribute__(store, "__dict__")


def _snapshot_store(store: StateStore) -> tuple[str, str]:
    raw_fields = _read_store_fields(store)
    if type(raw_fields) is not dict:
        _reject()
    fields = cast(dict[object, object], raw_fields)
    if any(type(name) is not str for name in fields):
        _reject()

    try:
        raw_project_id = fields["project_id"]
        raw_runtime_root = fields["runtime_root"]
        raw_runtime_dir = fields["runtime_dir"]
        raw_database_path = fields["database_path"]
    except KeyError:
        _reject()

    if not _valid_text(raw_project_id, maximum=256):
        _reject()
    project_id = cast(str, raw_project_id)
    if (
        type(raw_runtime_root) is not _PLATFORM_PATH_TYPE
        or type(raw_runtime_dir) is not _PLATFORM_PATH_TYPE
        or type(raw_database_path) is not _PLATFORM_PATH_TYPE
    ):
        _reject()
    runtime_root = raw_runtime_root
    runtime_dir = raw_runtime_dir
    database_path = raw_database_path
    if (
        not runtime_root.is_absolute()
        or not runtime_dir.is_absolute()
        or not database_path.is_absolute()
        or ".." in runtime_root.parts
        or ".." in runtime_dir.parts
        or ".." in database_path.parts
    ):
        _reject()

    project_digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:32]
    expected_runtime_dir = runtime_root / f"project-{project_digest}"
    expected_database_path = expected_runtime_dir / _DATABASE_NAME
    if runtime_dir != expected_runtime_dir or database_path != expected_database_path:
        _reject()

    encoded_database_path = str(database_path)
    if not _valid_text(encoded_database_path, maximum=4096):
        _reject()
    return project_id, encoded_database_path


def _read_listener_fileno(listener: socket.socket) -> object:
    return listener.fileno()


def _read_listener_family(listener: socket.socket) -> object:
    return listener.family


def _read_listener_type(listener: socket.socket) -> object:
    return listener.type


def _read_listener_protocol(listener: socket.socket) -> object:
    return listener.proto


def _read_listener_address(listener: socket.socket) -> object:
    return listener.getsockname()


def _read_listener_accepting(listener: socket.socket) -> object:
    if sys.platform == "darwin":
        if type(_TCP_CONNECTION_INFO) is not int:
            _reject()
        return listener.getsockopt(socket.IPPROTO_TCP, _TCP_CONNECTION_INFO, 1)
    if sys.platform == "linux":
        return listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    _reject()


def _read_listener_inheritable(listener: socket.socket) -> object:
    return listener.get_inheritable()


def _inspect_listener(listener: socket.socket) -> int:
    descriptor = _read_listener_fileno(listener)
    if type(descriptor) is not int or descriptor < 0:
        _reject()

    family = _read_listener_family(listener)
    if type(family) is not _ADDRESS_FAMILY_TYPE or family != socket.AF_INET:
        _reject()

    kind = _read_listener_type(listener)
    if type(kind) is not _SOCKET_KIND_TYPE or kind != socket.SOCK_STREAM:
        _reject()

    protocol = _read_listener_protocol(listener)
    if type(protocol) is not int or protocol not in {0, socket.IPPROTO_TCP}:
        _reject()

    address = _read_listener_address(listener)
    if type(address) is not tuple or len(address) != 2:
        _reject()
    host, port = address
    if type(host) is not str or host != LOOPBACK_HOST:
        _reject()
    if type(port) is not int or not 1 <= port <= 65_535:
        _reject()

    accepting = _read_listener_accepting(listener)
    if sys.platform == "darwin":
        if type(accepting) is not bytes or accepting != _TCP_LISTEN_STATE:
            _reject()
    elif sys.platform == "linux":
        if type(accepting) is not int or accepting != 1:
            _reject()
    else:
        _reject()

    inheritable = _read_listener_inheritable(listener)
    if type(inheritable) is not bool or inheritable:
        _reject()
    return port


def _valid_startup_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 32
        and all(character in _STARTUP_ID_CHARACTERS for character in value)
    )


def _valid_token(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 43
        and all(character in _TOKEN_CHARACTERS for character in value)
    )


def _revalidate_state(candidate: SidecarState) -> SidecarState:
    if type(candidate) is not _SIDECAR_STATE_TYPE:
        _reject()
    revalidated = _SIDECAR_STATE_TYPE.from_wire(candidate.to_wire())
    if (
        type(revalidated) is not _SIDECAR_STATE_TYPE
        or revalidated is candidate
        or revalidated != candidate
    ):
        _reject()
    return revalidated


def _generate_state(store: StateStore, listener: socket.socket) -> SidecarState:
    project_id, database_path = _snapshot_store(store)
    port = _inspect_listener(listener)

    startup_id = secrets.token_hex(16)
    if not _valid_startup_id(startup_id):
        _reject()
    token = secrets.token_urlsafe(32)
    if not _valid_token(token):
        _reject()
    pid = os.getpid()
    if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
        _reject()
    started_at_ns = time.time_ns()
    if type(started_at_ns) is not int or not 1 <= started_at_ns <= 2**63 - 1:
        _reject()

    candidate = _SIDECAR_STATE_TYPE(
        project_id=project_id,
        startup_id=startup_id,
        pid=pid,
        port=port,
        token=token,
        database_path=database_path,
        started_at_ns=started_at_ns,
    )
    return _revalidate_state(candidate)


def _public_failure() -> StartupStateError:
    return StartupStateError(StartupStateErrorCode.STARTUP_STATE_GENERATION_FAILED)


def create_startup_state(store: StateStore, listener: socket.socket) -> SidecarState:
    """Create one exact private state for an already-listening loopback socket."""

    if type(store) is not _STATE_STORE_TYPE:
        raise TypeError("store must be an exact StateStore")
    if type(listener) is not _SOCKET_TYPE:
        raise TypeError("listener must be an exact built-in socket.socket")

    generated: SidecarState | None = None
    try:
        candidate = _generate_state(store, listener)
        if type(candidate) is not _SIDECAR_STATE_TYPE:
            _reject()
        generated = candidate
    except Exception:
        pass
    if generated is None:
        # Raising after leaving the internal handler keeps the public exception
        # free of sensitive causes and contexts.
        raise _public_failure() from None
    return generated
