"""Private, project-scoped state used by the sidecar lifecycle spike."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

PROTOCOL_VERSION: Final = 1
STATE_SCHEMA_VERSION: Final = 1
LOOPBACK_HOST: Final = "127.0.0.1"


class InvalidStateError(ValueError):
    """A state record does not satisfy the spike's strict schema."""


def _exact_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidStateError(f"{name} is invalid")
    return value


def _exact_text(value: object, name: str, *, minimum: int = 1, maximum: int = 512) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise InvalidStateError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class SidecarState:
    """Authenticated discovery record published atomically by one sidecar."""

    project_id: str
    startup_id: str
    pid: int
    port: int
    # ``repr=False`` is important: capability tokens must not accidentally land
    # in test failures or application logs.
    token: str = field(repr=False)
    database_path: str
    started_at_ns: int
    host: str = LOOPBACK_HOST
    protocol_version: int = PROTOCOL_VERSION
    state_schema_version: int = STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _exact_text(self.project_id, "project_id", maximum=256)
        _exact_text(self.startup_id, "startup_id", maximum=128)
        _exact_int(self.pid, "pid", minimum=1, maximum=2**31 - 1)
        _exact_int(self.port, "port", minimum=1, maximum=65535)
        _exact_text(self.token, "token", minimum=32, maximum=512)
        _exact_text(self.database_path, "database_path", maximum=4096)
        _exact_int(self.started_at_ns, "started_at_ns", minimum=1, maximum=2**63 - 1)
        if self.host != LOOPBACK_HOST:
            raise InvalidStateError("host is not the v1 loopback address")
        if self.protocol_version != PROTOCOL_VERSION:
            raise InvalidStateError("protocol_version is unsupported")
        if self.state_schema_version != STATE_SCHEMA_VERSION:
            raise InvalidStateError("state_schema_version is unsupported")

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"

    def to_wire(self) -> dict[str, object]:
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
    """Read and atomically publish one private state file."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir).absolute()
        self.state_path = self.runtime_dir / "sidecar-state.json"
        self.lock_path = self.runtime_dir / "sidecar-owner.lock"
        self.database_path = self.runtime_dir / "events.sqlite3"

    def ensure_private_directory(self) -> None:
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runtime_dir, 0o700)

    def open_lock(self) -> int:
        self.ensure_private_directory()
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        return fd

    def load(self) -> SidecarState | None:
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.state_path, flags)
        except OSError:
            return None

        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_mode & 0o077:
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(64 * 1024 + 1)
            if len(data) > 64 * 1024:
                return None
            return SidecarState.from_wire(json.loads(data))
        except (OSError, UnicodeError, json.JSONDecodeError, InvalidStateError):
            return None
        finally:
            os.close(fd)

    def publish(self, state: SidecarState) -> None:
        self.ensure_private_directory()
        encoded = json.dumps(
            state.to_wire(), separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".sidecar-state-", dir=self.runtime_dir)
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary_path, self.state_path)
            os.chmod(self.state_path, 0o600)
            self._fsync_directory()
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def remove_if_owned(self, startup_id: str) -> bool:
        current = self.load()
        if current is None or current.startup_id != startup_id:
            return False
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            return False
        self._fsync_directory()
        return True

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        directory_fd = os.open(self.runtime_dir, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
