"""Strict project-scoped state used to discover one local FlowSight sidecar."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

PROTOCOL_VERSION: Final = 1
STATE_SCHEMA_VERSION: Final = 1
LOOPBACK_HOST: Final = "127.0.0.1"
MAX_STATE_BYTES: Final = 64 * 1024

_STATE_NAME: Final = "sidecar-state.json"
_OWNER_LOCK_NAME: Final = "sidecar-owner.lock"
_DATABASE_NAME: Final = "events.sqlite3"
_MUTATION_LOCK_NAME: Final = ".sidecar-state-mutation.lock"
_TEMP_PREFIX: Final = ".sidecar-state-"


class InvalidStateError(ValueError):
    """A state value does not satisfy the production discovery schema."""


class StateStorageError(RuntimeError):
    """The private state boundary could not complete a filesystem operation."""


class StateBusyError(StateStorageError):
    """Another process is atomically mutating the state record."""


class _OwnerLockFileCleanupError(StateStorageError):
    """An owner-lock helper descriptor reported ambiguous cleanup."""


def _exact_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidStateError(f"{name} is invalid")
    return value


def _exact_text(value: object, name: str, *, minimum: int = 1, maximum: int) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise InvalidStateError(f"{name} is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidStateError(f"{name} is invalid")
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidStateError("state fields are invalid")
        result[key] = value
    return result


def _private_directory(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _private_regular_file(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
    )


def _private_owner_lock_file(metadata: os.stat_result) -> bool:
    return _private_regular_file(metadata) and metadata.st_size == 0


def _file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class SidecarState:
    """Authenticated discovery record published atomically by one sidecar."""

    project_id: str
    startup_id: str
    pid: int
    port: int
    token: str = field(repr=False)
    database_path: str
    started_at_ns: int
    host: str = LOOPBACK_HOST
    protocol_version: int = PROTOCOL_VERSION
    state_schema_version: int = STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _exact_text(self.project_id, "project_id", maximum=256)
        _exact_text(self.startup_id, "startup_id", maximum=128)
        _exact_int(self.pid, "pid", minimum=1, maximum=2**31 - 1)
        _exact_int(self.port, "port", minimum=1, maximum=65_535)
        _exact_text(self.token, "token", minimum=32, maximum=512)
        database_path = _exact_text(self.database_path, "database_path", maximum=4096)
        if not Path(database_path).is_absolute():
            raise InvalidStateError("database_path is invalid")
        _exact_int(self.started_at_ns, "started_at_ns", minimum=1, maximum=2**63 - 1)
        host = _exact_text(self.host, "host", maximum=64)
        if host != LOOPBACK_HOST:
            raise InvalidStateError("host is not the v1 loopback address")
        _exact_int(
            self.protocol_version,
            "protocol_version",
            minimum=PROTOCOL_VERSION,
            maximum=PROTOCOL_VERSION,
        )
        _exact_int(
            self.state_schema_version,
            "state_schema_version",
            minimum=STATE_SCHEMA_VERSION,
            maximum=STATE_SCHEMA_VERSION,
        )

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"

    def to_wire(self) -> dict[str, object]:
        """Return the exact private JSON object; callers must not log it."""

        return {
            "state_schema_version": self.state_schema_version,
            "protocol_version": self.protocol_version,
            "project_id": self.project_id,
            "startup_id": self.startup_id,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "database_path": self.database_path,
            "started_at_ns": self.started_at_ns,
        }

    @classmethod
    def from_wire(cls, value: object) -> SidecarState:
        """Validate an exact decoded state object without coercion."""

        if type(value) is not dict:
            raise InvalidStateError("state root is invalid")
        expected = {
            "state_schema_version",
            "protocol_version",
            "project_id",
            "startup_id",
            "pid",
            "host",
            "port",
            "token",
            "database_path",
            "started_at_ns",
        }
        if set(value) != expected:
            raise InvalidStateError("state fields are invalid")
        raw: dict[str, Any] = value
        return cls(
            state_schema_version=raw["state_schema_version"],
            protocol_version=raw["protocol_version"],
            project_id=raw["project_id"],
            startup_id=raw["startup_id"],
            pid=raw["pid"],
            host=raw["host"],
            port=raw["port"],
            token=raw["token"],
            database_path=raw["database_path"],
            started_at_ns=raw["started_at_ns"],
        )


class StateStore:
    """Read and atomically publish one private project discovery record.

    Every cooperating publisher/remover must use this class so its short,
    non-blocking mutation lock serializes compare-and-remove. Readers do not
    take that lock: descriptor-anchored files plus atomic replace prevent a
    partial record; a reader fails closed if its opened inode changes.
    """

    def __init__(self, runtime_root: str | Path, *, project_id: str) -> None:
        self.project_id = _exact_text(project_id, "project_id", maximum=256)
        requested_root = Path(runtime_root).absolute()
        try:
            canonical_parent = requested_root.parent.resolve(strict=False)
        except (OSError, RuntimeError):
            raise StateStorageError("state root directory is invalid") from None
        # Resolve existing ancestor aliases once, then use descriptor-relative
        # O_NOFOLLOW traversal for every operation. The leaf remains unresolved
        # so a runtime-root symlink is still rejected. This supports standard
        # macOS aliases such as /var -> /private/var without following a path
        # that can later be swapped underneath the store.
        self.runtime_root = canonical_parent / requested_root.name
        project_digest = hashlib.sha256(self.project_id.encode("utf-8")).hexdigest()[:32]
        self.runtime_dir = self.runtime_root / f"project-{project_digest}"
        self.state_path = self.runtime_dir / _STATE_NAME
        self.lock_path = self.runtime_dir / _OWNER_LOCK_NAME
        self.mutation_lock_path = self.runtime_dir / _MUTATION_LOCK_NAME
        self.database_path = self.runtime_dir / _DATABASE_NAME

    def ensure_private_directory(self) -> None:
        """Create and durably anchor the project directory as current-user 0700."""

        directory_descriptor = self._open_directory(create=True, repair=True)
        try:
            self._require_canonical_directory(directory_descriptor)
        finally:
            self._finish_descriptor_cleanup(
                sys.exception(),
                "state directory cleanup failed",
                directory_descriptor,
            )

    def _open_owner_lock_file(self, *, create: bool) -> int:
        """Open the exact persistent owner-lock inode through the trusted directory."""

        directory_descriptor = -1
        first_descriptor = -1
        verified_descriptor = -1
        created = False
        flags = (
            os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            directory_descriptor = self._open_owner_lock_directory(
                create=create,
                repair=create,
            )
            if create:
                try:
                    first_descriptor = os.open(
                        _OWNER_LOCK_NAME,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                except FileExistsError:
                    first_descriptor = os.open(
                        _OWNER_LOCK_NAME,
                        flags,
                        dir_fd=directory_descriptor,
                    )
                else:
                    created = True
                    os.fchmod(first_descriptor, 0o600)
            else:
                first_descriptor = os.open(
                    _OWNER_LOCK_NAME,
                    flags,
                    dir_fd=directory_descriptor,
                )

            first_metadata = os.fstat(first_descriptor)
            if not _private_owner_lock_file(first_metadata):
                raise StateStorageError("sidecar owner lock file is invalid")
            if created:
                os.fsync(first_descriptor)
                os.fsync(directory_descriptor)

            self._require_owner_lock_canonical_directory(directory_descriptor)
            verified_descriptor = os.open(
                _OWNER_LOCK_NAME,
                flags,
                dir_fd=directory_descriptor,
            )
            verified_metadata = os.fstat(verified_descriptor)
            if not _private_owner_lock_file(verified_metadata) or (
                first_metadata.st_dev,
                first_metadata.st_ino,
            ) != (verified_metadata.st_dev, verified_metadata.st_ino):
                raise StateStorageError("sidecar owner lock file changed during operation")
            self._require_owner_lock_canonical_directory(directory_descriptor)

            descriptor_to_close = first_descriptor
            first_descriptor = -1
            if not self._try_close(descriptor_to_close):
                raise _OwnerLockFileCleanupError("sidecar owner lock file cleanup failed")
            descriptor_to_close = directory_descriptor
            directory_descriptor = -1
            if not self._try_close(descriptor_to_close):
                raise _OwnerLockFileCleanupError("sidecar owner lock directory cleanup failed")
            result = verified_descriptor
            verified_descriptor = -1
            return result
        except StateStorageError:
            raise
        except (OSError, NotImplementedError):
            raise StateStorageError("sidecar owner lock file setup failed") from None
        finally:
            self._finish_owner_lock_file_cleanup(
                sys.exception(),
                verified_descriptor,
                first_descriptor,
                directory_descriptor,
            )

    def _require_owner_lock_canonical_directory(self, directory_descriptor: int) -> None:
        """Verify the owner directory while preserving owner-lock cleanup semantics."""

        canonical_descriptor = -1
        try:
            canonical_descriptor = self._open_owner_lock_directory(
                create=False,
                repair=False,
            )
            anchored = os.fstat(directory_descriptor)
            canonical = os.fstat(canonical_descriptor)
            if (anchored.st_dev, anchored.st_ino) != (
                canonical.st_dev,
                canonical.st_ino,
            ):
                raise StateStorageError("state project directory changed during operation")
        except StateStorageError:
            raise
        except (OSError, NotImplementedError):
            raise StateStorageError("sidecar owner lock directory verification failed") from None
        finally:
            self._finish_owner_lock_file_cleanup(
                sys.exception(),
                canonical_descriptor,
            )

    def _open_owner_lock_directory(self, *, create: bool, repair: bool) -> int:
        return StateStore._open_directory(
            self,
            create=create,
            repair=repair,
            _owner_lock_cleanup=True,
        )

    def load(self) -> SidecarState | None:
        """Return a trusted state record or ``None`` for every invalid input."""

        directory_descriptor = -1
        result: SidecarState | None = None
        try:
            directory_descriptor = self._open_directory(create=False, repair=False)
            state = self._load_name(directory_descriptor, _STATE_NAME)
            if (
                state is not None
                and self._is_bound_state(state)
                and self._canonical_directory_matches(directory_descriptor)
            ):
                result = state
        except StateStorageError:
            result = None
        finally:
            if not self._try_close(directory_descriptor):
                result = None
        return result

    def publish(self, state: SidecarState) -> None:
        """Atomically publish one project-bound mode-0600 state record."""

        if type(state) is not SidecarState or not self._is_bound_state(state):
            raise InvalidStateError("state does not belong to this project store")
        encoded = json.dumps(
            state.to_wire(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not encoded or len(encoded) > MAX_STATE_BYTES:
            raise InvalidStateError("encoded state size is invalid")

        directory_descriptor = -1
        lock_descriptor = -1
        file_descriptor = -1
        temporary_name: str | None = None
        try:
            directory_descriptor = self._open_directory(create=True, repair=True)
            lock_descriptor = self._acquire_mutation_lock(
                directory_descriptor,
                exclusive=True,
            )
            temporary_name, file_descriptor = self._open_temporary_file(directory_descriptor)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(file_descriptor, remaining)
                if written <= 0:
                    raise StateStorageError("state publication write failed")
                remaining = remaining[written:]
            os.fsync(file_descriptor)
            descriptor_to_close = file_descriptor
            file_descriptor = -1
            if not self._try_close(descriptor_to_close):
                raise StateStorageError("state publication file close failed")
            os.replace(
                temporary_name,
                _STATE_NAME,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            temporary_name = None
            os.fsync(directory_descriptor)
            self._require_canonical_directory(directory_descriptor)
        except (InvalidStateError, StateStorageError):
            raise
        except (OSError, NotImplementedError):
            raise StateStorageError("state publication failed") from None
        finally:
            active_error = sys.exception()
            cleanup_failed = False
            if file_descriptor >= 0:
                cleanup_failed |= not self._try_close(file_descriptor)
            if temporary_name is not None and directory_descriptor >= 0:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except (OSError, NotImplementedError):
                    cleanup_failed = True
            cleanup_failed |= not self._try_close(lock_descriptor)
            if directory_descriptor >= 0:
                cleanup_failed |= not self._try_close(directory_descriptor)
            if cleanup_failed:
                self._surface_cleanup_failure(
                    active_error,
                    "state publication cleanup failed",
                )

    def remove_if_owned(self, startup_id: str) -> bool:
        """Remove exactly the owned record under a non-blocking mutation lock."""

        expected_startup_id = _exact_text(startup_id, "startup_id", maximum=128)
        directory_descriptor = -1
        lock_descriptor = -1
        try:
            directory_descriptor = self._open_directory(create=True, repair=True)
            lock_descriptor = self._acquire_mutation_lock(
                directory_descriptor,
                exclusive=True,
            )
            current = self._load_name(directory_descriptor, _STATE_NAME)
            if (
                current is None
                or not self._is_bound_state(current)
                or current.startup_id != expected_startup_id
            ):
                return False
            os.unlink(_STATE_NAME, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
            self._require_canonical_directory(directory_descriptor)
            return True
        except (InvalidStateError, StateStorageError):
            raise
        except (OSError, NotImplementedError):
            raise StateStorageError("state removal failed") from None
        finally:
            active_error = sys.exception()
            cleanup_failed = not self._try_close(lock_descriptor)
            if directory_descriptor >= 0:
                cleanup_failed |= not self._try_close(directory_descriptor)
            if cleanup_failed:
                self._surface_cleanup_failure(active_error, "state removal cleanup failed")

    def _open_directory(
        self,
        *,
        create: bool,
        repair: bool,
        _owner_lock_cleanup: bool = False,
    ) -> int:
        root_descriptor = -1
        project_descriptor = -1
        project_missing_observed = False
        try:
            flags = (
                os.O_RDONLY
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            root_descriptor = self._open_runtime_root(
                flags,
                create=create,
                repair=repair,
                _owner_lock_cleanup=_owner_lock_cleanup,
            )

            project_name = self.runtime_dir.name
            try:
                project_descriptor = os.open(project_name, flags, dir_fd=root_descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                project_missing_observed = True
                try:
                    os.mkdir(project_name, mode=0o700, dir_fd=root_descriptor)
                except FileExistsError:
                    pass
                project_descriptor = os.open(project_name, flags, dir_fd=root_descriptor)
            project_metadata = os.fstat(project_descriptor)
            project_repaired = repair and stat.S_IMODE(project_metadata.st_mode) != 0o700
            if project_repaired:
                os.fchmod(project_descriptor, 0o700)
            if not _private_directory(os.fstat(project_descriptor)):
                raise StateStorageError("state project directory is invalid")
            if project_missing_observed or project_repaired:
                os.fsync(project_descriptor)
            if project_missing_observed:
                os.fsync(root_descriptor)
            descriptor_to_close = root_descriptor
            root_descriptor = -1
            if not self._try_close(descriptor_to_close):
                if _owner_lock_cleanup:
                    raise _OwnerLockFileCleanupError("sidecar owner lock file cleanup failed")
                raise StateStorageError("state directory cleanup failed")
            result = project_descriptor
            project_descriptor = -1
            return result
        except StateStorageError:
            raise
        except (OSError, NotImplementedError):
            raise StateStorageError("state directory setup failed") from None
        finally:
            if _owner_lock_cleanup:
                self._finish_owner_lock_file_cleanup(
                    sys.exception(),
                    project_descriptor,
                    root_descriptor,
                )
            else:
                self._finish_descriptor_cleanup(
                    sys.exception(),
                    "state directory cleanup failed",
                    project_descriptor,
                    root_descriptor,
                )

    def _open_runtime_root(
        self,
        flags: int,
        *,
        create: bool,
        repair: bool,
        _owner_lock_cleanup: bool = False,
    ) -> int:
        parts = self.runtime_root.parts
        if not self.runtime_root.is_absolute() or len(parts) < 2:
            raise StateStorageError("state root directory is invalid")

        current_descriptor = -1
        next_descriptor = -1
        try:
            current_descriptor = os.open(os.sep, flags)
            for index, component in enumerate(parts[1:], start=1):
                if component in {"", ".", ".."}:
                    raise StateStorageError("state root directory is invalid")
                missing_observed = False
                try:
                    next_descriptor = os.open(component, flags, dir_fd=current_descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    missing_observed = True
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
                    except FileExistsError:
                        pass
                    next_descriptor = os.open(component, flags, dir_fd=current_descriptor)

                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise StateStorageError("state root directory is invalid")
                is_final = index == len(parts) - 1
                if is_final:
                    repaired = repair and stat.S_IMODE(metadata.st_mode) != 0o700
                    if repaired:
                        os.fchmod(next_descriptor, 0o700)
                    if not _private_directory(os.fstat(next_descriptor)):
                        raise StateStorageError("state root directory is invalid")
                    if missing_observed or repaired:
                        os.fsync(next_descriptor)
                elif missing_observed:
                    os.fchmod(next_descriptor, 0o700)
                    if not _private_directory(os.fstat(next_descriptor)):
                        raise StateStorageError("state root directory is invalid")
                    os.fsync(next_descriptor)
                if missing_observed:
                    os.fsync(current_descriptor)
                descriptor_to_close = current_descriptor
                current_descriptor = -1
                if not self._try_close(descriptor_to_close):
                    if _owner_lock_cleanup:
                        raise _OwnerLockFileCleanupError("sidecar owner lock file cleanup failed")
                    raise StateStorageError("state root directory cleanup failed")
                current_descriptor = next_descriptor
                next_descriptor = -1

            result = current_descriptor
            current_descriptor = -1
            return result
        except StateStorageError:
            raise
        except (OSError, NotImplementedError):
            raise StateStorageError("state root directory setup failed") from None
        finally:
            if _owner_lock_cleanup:
                self._finish_owner_lock_file_cleanup(
                    sys.exception(),
                    next_descriptor,
                    current_descriptor,
                )
            else:
                self._finish_descriptor_cleanup(
                    sys.exception(),
                    "state root directory cleanup failed",
                    next_descriptor,
                    current_descriptor,
                )

    def _canonical_directory_matches(self, directory_descriptor: int) -> bool:
        canonical_descriptor = -1
        matches = False
        try:
            canonical_descriptor = self._open_directory(create=False, repair=False)
            anchored = os.fstat(directory_descriptor)
            canonical = os.fstat(canonical_descriptor)
            matches = (anchored.st_dev, anchored.st_ino) == (
                canonical.st_dev,
                canonical.st_ino,
            )
        except (OSError, StateStorageError):
            matches = False
        finally:
            if not self._try_close(canonical_descriptor):
                matches = False
        return matches

    def _require_canonical_directory(self, directory_descriptor: int) -> None:
        if not self._canonical_directory_matches(directory_descriptor):
            raise StateStorageError("state project directory changed during operation")

    def _acquire_mutation_lock(self, directory_descriptor: int, *, exclusive: bool) -> int:
        lock_descriptor = -1
        created = False
        flags = (
            os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            try:
                lock_descriptor = os.open(
                    _MUTATION_LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                lock_descriptor = os.open(
                    _MUTATION_LOCK_NAME,
                    flags,
                    dir_fd=directory_descriptor,
                )
            else:
                created = True
                os.fchmod(lock_descriptor, 0o600)

            if not _private_regular_file(os.fstat(lock_descriptor)):
                raise StateStorageError("state mutation lock is invalid")
            if created:
                os.fsync(lock_descriptor)
                os.fsync(directory_descriptor)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(lock_descriptor, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateBusyError("state operation is busy") from None
            result = lock_descriptor
            lock_descriptor = -1
            return result
        except (StateStorageError, StateBusyError):
            raise
        except (OSError, NotImplementedError):
            raise StateStorageError("state mutation lock failed") from None
        finally:
            self._finish_descriptor_cleanup(
                sys.exception(),
                "state mutation lock cleanup failed",
                lock_descriptor,
            )

    def _open_temporary_file(self, directory_descriptor: int) -> tuple[str, int]:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _attempt in range(32):
            name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}"
            file_descriptor = -1
            try:
                file_descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
            except FileExistsError:
                continue
            try:
                setup_error: StateStorageError | None = None
                try:
                    os.fchmod(file_descriptor, 0o600)
                    if not _private_regular_file(os.fstat(file_descriptor)):
                        setup_error = StateStorageError("temporary state file is invalid")
                except (OSError, NotImplementedError):
                    setup_error = StateStorageError("temporary state file setup failed")
                if setup_error is not None:
                    raise setup_error from None
                result = file_descriptor
                file_descriptor = -1
                return name, result
            finally:
                if file_descriptor >= 0:
                    active_error = sys.exception()
                    cleanup_failed = not self._try_close(file_descriptor)
                    try:
                        os.unlink(name, dir_fd=directory_descriptor)
                    except (OSError, NotImplementedError):
                        cleanup_failed = True
                    if cleanup_failed:
                        self._surface_cleanup_failure(
                            active_error,
                            "temporary state file cleanup failed",
                        )
        raise StateStorageError("temporary state file allocation failed")

    def _load_name(self, directory_descriptor: int, name: str) -> SidecarState | None:
        file_descriptor = -1
        result: SidecarState | None = None
        try:
            flags = (
                os.O_RDONLY
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            file_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            before = os.fstat(file_descriptor)
            if _private_regular_file(before) and 0 < before.st_size <= MAX_STATE_BYTES:
                encoded = bytearray()
                while len(encoded) <= MAX_STATE_BYTES:
                    chunk = os.read(
                        file_descriptor,
                        min(8192, MAX_STATE_BYTES + 1 - len(encoded)),
                    )
                    if not chunk:
                        break
                    encoded.extend(chunk)
                after = os.fstat(file_descriptor)
                if (
                    encoded
                    and len(encoded) <= MAX_STATE_BYTES
                    and len(encoded) == after.st_size
                    and _private_regular_file(after)
                    and _file_signature(before) == _file_signature(after)
                ):
                    decoded = json.loads(
                        encoded.decode("utf-8"),
                        object_pairs_hook=_strict_object,
                    )
                    result = SidecarState.from_wire(decoded)
        except (OSError, UnicodeError, ValueError, RecursionError):
            result = None
        finally:
            if not self._try_close(file_descriptor):
                result = None
        return result

    def _is_bound_state(self, state: SidecarState) -> bool:
        return state.project_id == self.project_id and state.database_path == str(
            self.database_path
        )

    @staticmethod
    def _try_close(file_descriptor: int) -> bool:
        """Close one relinquished descriptor exactly once.

        POSIX leaves the descriptor state ambiguous after a reported close
        error, so callers must clear their owner variable before this call and
        must never retry the same integer.
        """

        if file_descriptor < 0:
            return True
        try:
            # Closing the descriptor releases flock atomically; a separate
            # LOCK_UN call creates an avoidable raw-error path during cleanup.
            os.close(file_descriptor)
        except (OSError, NotImplementedError):
            return False
        return True

    @staticmethod
    def _finish_owner_lock_file_cleanup(
        active_error: BaseException | None,
        *file_descriptors: int,
    ) -> None:
        cleanup_failed = False
        cleanup_control: BaseException | None = None
        for file_descriptor in file_descriptors:
            if file_descriptor < 0:
                continue
            try:
                os.close(file_descriptor)
            except BaseException as error:
                if isinstance(error, Exception):
                    cleanup_failed = True
                elif cleanup_control is None:
                    cleanup_control = error

        if active_error is not None and not isinstance(active_error, Exception):
            if cleanup_failed or cleanup_control is not None:
                active_error.add_note("sidecar owner lock file cleanup failed")
            return
        if cleanup_control is not None:
            raise cleanup_control
        if cleanup_failed:
            raise _OwnerLockFileCleanupError("sidecar owner lock file cleanup failed") from None

    @classmethod
    def _finish_descriptor_cleanup(
        cls,
        active_error: BaseException | None,
        message: str,
        *file_descriptors: int,
    ) -> None:
        cleanup_failed = False
        for file_descriptor in file_descriptors:
            cleanup_failed |= not cls._try_close(file_descriptor)
        if cleanup_failed:
            cls._surface_cleanup_failure(active_error, message)

    @staticmethod
    def _surface_cleanup_failure(
        active_error: BaseException | None,
        message: str,
    ) -> None:
        failure = StateStorageError(message)
        if active_error is None:
            raise failure
        if isinstance(active_error, Exception):
            raise failure from None
        active_error.add_note(message)
