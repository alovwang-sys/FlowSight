from __future__ import annotations

import ast
import copy
import errno
import fcntl
import gc
import json
import os
import pickle
import selectors
import socket
import stat
import subprocess
import sys
import time
import traceback
import weakref
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, NoReturn

import pytest

from flowsight.sidecar import (
    OwnerLock,
    OwnerLockError,
    OwnerLockErrorCode,
    StateStorageError,
    StateStore,
)
from flowsight.sidecar import owner_lock as owner_lock_module
from flowsight.sidecar import state as state_module

PROJECT_ID = "owner-lock-project"


class _IntDescriptor(IntEnum):
    VALUE = 9


class _DescriptorSubclass(int):
    pass


class _StateStoreSubclass(StateStore):
    pass


class _BodyError(Exception):
    pass


class _OwnerFileFaultHarness:
    """Isolate and account for the three descriptors owned by the lock-file helper."""

    def __init__(
        self,
        store: StateStore,
        *,
        stage: str | None = None,
        control_error: BaseException | None = None,
        cleanup_role: str | None = None,
        reuse_cleanup_descriptor: bool = False,
    ) -> None:
        self.store = store
        self.stage = stage
        self.control_error = control_error
        self.cleanup_role = cleanup_role
        self.reuse_cleanup_descriptor = reuse_cleanup_descriptor
        self.raw_detail = f"raw owner-file {stage} /private/owner"
        self.roles: dict[str, int] = {}
        self.close_counts: dict[str, int] = {}
        self.owner_open_flags: list[int] = []
        self.canonical_calls = 0
        self.replacement_descriptor = -1
        self._real_open = os.open
        self._real_close = os.close
        self._real_dup2 = os.dup2
        self._real_fchmod = os.fchmod
        self._real_fstat = os.fstat
        self._real_fsync = os.fsync
        self._real_open_directory = store._open_owner_lock_directory

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            self.store,
            "_open_owner_lock_directory",
            self._open_owner_lock_directory,
        )
        monkeypatch.setattr(
            self.store,
            "_require_owner_lock_canonical_directory",
            self._require_owner_lock_canonical_directory,
        )
        monkeypatch.setattr(state_module.os, "open", self._open)
        monkeypatch.setattr(state_module.os, "close", self._close)
        monkeypatch.setattr(state_module.os, "fchmod", self._fchmod)
        monkeypatch.setattr(state_module.os, "fstat", self._fstat)
        monkeypatch.setattr(state_module.os, "fsync", self._fsync)

    def close_unresolved_descriptor(self) -> None:
        if self.cleanup_role is None:
            return
        file_descriptor = self.roles[self.cleanup_role]
        if self.replacement_descriptor >= 0:
            file_descriptor = self.replacement_descriptor
        self._real_close(file_descriptor)

    def assert_closed(self, file_descriptor: int) -> None:
        with pytest.raises(OSError) as captured:
            self._real_fstat(file_descriptor)
        assert captured.value.errno == errno.EBADF

    def _raise_stage_failure(self) -> NoReturn:
        if self.control_error is not None:
            raise self.control_error
        raise OSError(errno.EIO, self.raw_detail)

    def _open_owner_lock_directory(self, *, create: bool, repair: bool) -> int:
        file_descriptor = self._real_open_directory(create=create, repair=repair)
        assert "directory" not in self.roles
        self.roles["directory"] = file_descriptor
        return file_descriptor

    def _open(
        self,
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        is_owner_file = os.fsdecode(path) == "sidecar-owner.lock" and dir_fd is not None
        if is_owner_file:
            self.owner_open_flags.append(flags)
            if self.stage == "exclusive_create_open" and flags & os.O_EXCL:
                self._raise_stage_failure()
            if (
                self.stage == "existing_fallback_open"
                and len(self.owner_open_flags) == 2
                and "first" not in self.roles
            ):
                self._raise_stage_failure()
            if (
                self.stage == "verification_open"
                and "first" in self.roles
                and "verified" not in self.roles
            ):
                self._raise_stage_failure()

        file_descriptor = self._real_open(path, flags, mode, dir_fd=dir_fd)
        if is_owner_file:
            role = "first" if "first" not in self.roles else "verified"
            self.roles[role] = file_descriptor
        return file_descriptor

    def _fchmod(self, file_descriptor: int, mode: int) -> None:
        if self.stage == "fchmod" and file_descriptor == self.roles.get("first"):
            self._raise_stage_failure()
        self._real_fchmod(file_descriptor, mode)

    def _fstat(self, file_descriptor: int) -> os.stat_result:
        role = self._role(file_descriptor)
        if self.stage == "first_fstat" and role == "first":
            self._raise_stage_failure()
        if self.stage == "verified_fstat" and role == "verified":
            self._raise_stage_failure()
        return self._real_fstat(file_descriptor)

    def _fsync(self, file_descriptor: int) -> None:
        role = self._role(file_descriptor)
        if self.stage == "file_fsync" and role == "first":
            self._raise_stage_failure()
        if self.stage == "directory_fsync" and role == "directory":
            self._raise_stage_failure()
        self._real_fsync(file_descriptor)

    def _require_owner_lock_canonical_directory(self, file_descriptor: int) -> None:
        assert file_descriptor == self.roles["directory"]
        self.canonical_calls += 1
        if self.stage == "canonical_first" and self.canonical_calls == 1:
            self._raise_stage_failure()
        if self.stage == "canonical_second" and self.canonical_calls == 2:
            self._raise_stage_failure()

    def _close(self, file_descriptor: int) -> None:
        role = self._role(file_descriptor)
        if role is not None:
            self.close_counts[role] = self.close_counts.get(role, 0) + 1
        if role is not None and role == self.cleanup_role:
            if self.reuse_cleanup_descriptor:
                self._real_close(file_descriptor)
                temporary_descriptor = self._real_open(os.devnull, os.O_RDONLY)
                if temporary_descriptor == file_descriptor:
                    self.replacement_descriptor = temporary_descriptor
                else:
                    self.replacement_descriptor = self._real_dup2(
                        temporary_descriptor,
                        file_descriptor,
                    )
                    self._real_close(temporary_descriptor)
            raise OSError(errno.EIO, "raw owner-file cleanup /private/owner")
        self._real_close(file_descriptor)

    def _role(self, file_descriptor: int) -> str | None:
        for role, owned_descriptor in self.roles.items():
            if owned_descriptor == file_descriptor:
                return role
        return None


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "runtime", project_id=PROJECT_ID)


def _assert_private_error(
    error: OwnerLockError,
    code: OwnerLockErrorCode,
    *forbidden: str,
) -> None:
    assert error.code is code
    assert str(error) == f"sidecar owner lock failed ({code})"
    exposed = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            repr(getattr(error, "__notes__", None)),
            repr(error.__cause__),
            repr(error.__context__),
        )
    )
    for value in forbidden:
        assert value not in exposed
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", None) is None


def _assert_closed(file_descriptor: int) -> None:
    with pytest.raises(OSError) as captured:
        os.fstat(file_descriptor)
    assert captured.value.errno == errno.EBADF


def _open_owner_path(store: StateStore) -> int:
    return os.open(store.lock_path, os.O_RDWR | os.O_NONBLOCK)


def _readline_bounded(stream: BinaryIO, *, timeout: float = 10.0) -> bytes:
    selector = selectors.DefaultSelector()
    encoded = bytearray()
    deadline = time.monotonic() + timeout
    try:
        selector.register(stream, selectors.EVENT_READ)
        while b"\n" not in encoded:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "child output timed out"
            assert selector.select(remaining), "child output timed out"
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            encoded.extend(chunk)
            assert len(encoded) <= 64 * 1024, "child output exceeded the test limit"
        assert b"\n" in encoded, "child output ended before a complete line"
        return bytes(encoded).split(b"\n", 1)[0]
    finally:
        selector.close()


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def test_acquire_creates_one_persistent_private_noninheritable_lock(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    owner = OwnerLock.acquire(store)
    descriptor = owner.fileno()
    first_metadata = os.fstat(descriptor)
    try:
        assert repr(owner) == "<OwnerLock active>"
        assert os.get_inheritable(descriptor) is False
        assert stat.S_ISREG(first_metadata.st_mode)
        assert first_metadata.st_uid == os.getuid()
        assert stat.S_IMODE(first_metadata.st_mode) == 0o600
        assert first_metadata.st_nlink == 1
        assert first_metadata.st_size == 0
        assert os.lstat(store.runtime_root).st_mode & 0o777 == 0o700
        assert os.lstat(store.runtime_dir).st_mode & 0o777 == 0o700
    finally:
        owner.close()

    assert repr(owner) == "<OwnerLock closed>"
    assert store.lock_path.is_file()
    second = OwnerLock.acquire(store)
    try:
        second_metadata = os.fstat(second.fileno())
        assert (second_metadata.st_dev, second_metadata.st_ino) == (
            first_metadata.st_dev,
            first_metadata.st_ino,
        )
    finally:
        second.close()


def test_contention_is_fixed_nonblocking_and_close_allows_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = OwnerLock.acquire(store)
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.acquire(store)
        _assert_private_error(
            captured.value,
            OwnerLockErrorCode.OWNER_LOCK_HELD,
            str(store.lock_path),
            str(first.fileno()),
        )
    finally:
        first.close()

    retry = OwnerLock.acquire(store)
    retry.close()


def test_acquire_requires_exact_state_store_before_filesystem_access(tmp_path: Path) -> None:
    store = _StateStoreSubclass(tmp_path / "runtime", project_id=PROJECT_ID)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.acquire(store)

    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED,
        str(store.runtime_root),
    )
    assert not store.runtime_root.exists()


def test_adopt_with_nonexact_store_consumes_the_transferred_descriptor(
    tmp_path: Path,
) -> None:
    store = _StateStoreSubclass(tmp_path / "runtime", project_id=PROJECT_ID)
    transferred_descriptor = os.open(os.devnull, os.O_RDWR)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.adopt_inherited(store, transferred_descriptor)

    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED,
        str(transferred_descriptor),
        str(store.runtime_root),
    )
    _assert_closed(transferred_descriptor)


@pytest.mark.parametrize("stage", ["duplicate", "low_close", "post_promotion"])
def test_low_owner_descriptor_promotion_has_fixed_failure_and_close_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    low_descriptor = 2
    promoted_descriptor = 9
    try_close_attempts: list[int] = []
    final_close_attempts: list[int] = []
    raw_detail = f"raw descriptor promotion {stage} /private/owner"

    def return_low_descriptor(_store: StateStore, *, create: bool) -> int:
        assert create is True
        return low_descriptor

    def duplicate_at_least(
        file_descriptor: int,
        command: int,
        minimum: int,
    ) -> int:
        assert (file_descriptor, command, minimum) == (
            low_descriptor,
            fcntl.F_DUPFD_CLOEXEC,
            3,
        )
        if stage == "duplicate":
            raise OSError(errno.EIO, raw_detail)
        return promoted_descriptor

    def report_try_close(file_descriptor: int) -> bool:
        try_close_attempts.append(file_descriptor)
        return stage != "low_close"

    def report_final_close(file_descriptor: int) -> None:
        final_close_attempts.append(file_descriptor)

    def fail_after_promotion(_file_descriptor: int) -> bool:
        raise OSError(errno.EIO, raw_detail)

    monkeypatch.setattr(owner_lock_module, "_open_store_descriptor", return_low_descriptor)
    monkeypatch.setattr(owner_lock_module.fcntl, "fcntl", duplicate_at_least)
    monkeypatch.setattr(owner_lock_module, "_try_close", report_try_close)
    monkeypatch.setattr(owner_lock_module.os, "close", report_final_close)
    monkeypatch.setattr(
        owner_lock_module,
        "_descriptor_is_exact_owner_file",
        fail_after_promotion,
    )

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.acquire(_store(tmp_path))

    expected_code = (
        OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED
        if stage == "low_close"
        else OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED
    )
    _assert_private_error(captured.value, expected_code, raw_detail)
    if stage == "duplicate":
        assert try_close_attempts == []
        assert final_close_attempts == [low_descriptor]
    else:
        assert try_close_attempts == [low_descriptor]
        assert final_close_attempts == [promoted_descriptor]


def test_owner_lock_file_uses_exclusive_create_then_existing_file_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    real_open = os.open
    owner_open_flags: list[int] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fsdecode(path) == "sidecar-owner.lock" and dir_fd is not None:
            owner_open_flags.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(state_module.os, "open", recording_open)

    created = store._open_owner_lock_file(create=True)
    os.close(created)
    create_attempts = owner_open_flags.copy()
    owner_open_flags.clear()

    reopened = store._open_owner_lock_file(create=True)
    os.close(reopened)
    fallback_attempts = owner_open_flags.copy()

    assert len(create_attempts) == 2
    assert create_attempts[0] & (os.O_CREAT | os.O_EXCL) == os.O_CREAT | os.O_EXCL
    assert create_attempts[1] & (os.O_CREAT | os.O_EXCL) == 0
    assert len(fallback_attempts) == 3
    assert fallback_attempts[0] & (os.O_CREAT | os.O_EXCL) == os.O_CREAT | os.O_EXCL
    assert all(flags & (os.O_CREAT | os.O_EXCL) == 0 for flags in fallback_attempts[1:])
    for flags in [*create_attempts, *fallback_attempts]:
        assert flags & os.O_ACCMODE == os.O_RDWR
        assert flags & os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            assert flags & os.O_NOFOLLOW


def test_owner_lock_canonical_probe_close_failure_is_cleanup_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    real_open_directory = store._open_owner_lock_directory
    real_open = os.open
    real_close = os.close
    real_dup2 = os.dup2
    returned_directories: list[int] = []
    canonical_close_attempts = 0
    replacement_descriptor = -1

    def recording_open_directory(*, create: bool, repair: bool) -> int:
        file_descriptor = real_open_directory(create=create, repair=repair)
        returned_directories.append(file_descriptor)
        return file_descriptor

    def close_then_reuse_canonical(file_descriptor: int) -> None:
        nonlocal canonical_close_attempts, replacement_descriptor
        if len(returned_directories) >= 2 and file_descriptor == returned_directories[1]:
            canonical_close_attempts += 1
            real_close(file_descriptor)
            temporary_descriptor = real_open(os.devnull, os.O_RDONLY)
            if temporary_descriptor == file_descriptor:
                replacement_descriptor = temporary_descriptor
            else:
                replacement_descriptor = real_dup2(temporary_descriptor, file_descriptor)
                real_close(temporary_descriptor)
            raise OSError(errno.EIO, "raw canonical cleanup /private/owner")
        real_close(file_descriptor)

    monkeypatch.setattr(store, "_open_owner_lock_directory", recording_open_directory)
    monkeypatch.setattr(state_module.os, "close", close_then_reuse_canonical)

    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.acquire(store)

        _assert_private_error(
            captured.value,
            OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
            "raw canonical cleanup",
            str(store.lock_path),
        )
        assert len(returned_directories) == 2
        assert canonical_close_attempts == 1
        assert replacement_descriptor == returned_directories[1]
        assert os.fstat(replacement_descriptor)
        _assert_closed(returned_directories[0])
    finally:
        if replacement_descriptor >= 0:
            real_close(replacement_descriptor)


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_owner_lock_canonical_probe_cleanup_notes_active_process_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    real_open_directory = store._open_owner_lock_directory
    real_close = os.close
    real_fstat = os.fstat
    returned_directories: list[int] = []
    canonical_close_attempts = 0
    error = error_factory()

    def recording_open_directory(*, create: bool, repair: bool) -> int:
        file_descriptor = real_open_directory(create=create, repair=repair)
        returned_directories.append(file_descriptor)
        return file_descriptor

    def interrupt_canonical_fstat(file_descriptor: int) -> os.stat_result:
        if len(returned_directories) >= 2 and file_descriptor == returned_directories[1]:
            raise error
        return real_fstat(file_descriptor)

    def fail_canonical_close(file_descriptor: int) -> None:
        nonlocal canonical_close_attempts
        if len(returned_directories) >= 2 and file_descriptor == returned_directories[1]:
            canonical_close_attempts += 1
            raise OSError(errno.EIO, "raw canonical cleanup /private/owner")
        real_close(file_descriptor)

    monkeypatch.setattr(store, "_open_owner_lock_directory", recording_open_directory)
    monkeypatch.setattr(state_module.os, "fstat", interrupt_canonical_fstat)
    monkeypatch.setattr(state_module.os, "close", fail_canonical_close)

    try:
        with pytest.raises(error_factory) as captured:
            OwnerLock.acquire(store)

        assert captured.value is error
        assert getattr(captured.value, "__notes__", []) == [
            "sidecar owner lock file cleanup failed"
        ]
        assert "raw canonical cleanup" not in repr(captured.value.__notes__)
        assert len(returned_directories) == 2
        assert canonical_close_attempts == 1
        assert real_fstat(returned_directories[1])
        with pytest.raises(OSError) as closed:
            real_fstat(returned_directories[0])
        assert closed.value.errno == errno.EBADF
    finally:
        if len(returned_directories) >= 2:
            real_close(returned_directories[1])


@pytest.mark.parametrize("operation", ["acquire", "adopt"])
@pytest.mark.parametrize("nested_stage", ["runtime_root", "project_directory"])
def test_nested_owner_directory_close_failure_is_public_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    nested_stage: str,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    parent: OwnerLock | None = None
    candidate = -1
    if operation == "adopt":
        parent = OwnerLock.acquire(store)
        candidate = os.dup(parent.fileno())
    real_try_close = store._try_close
    real_open_runtime_root = store._open_runtime_root
    injected_descriptor = -1
    close_attempts = 0
    runtime_root_returned = False

    def recording_open_runtime_root(
        flags: int,
        *,
        create: bool,
        repair: bool,
        _owner_lock_cleanup: bool = False,
    ) -> int:
        nonlocal runtime_root_returned
        file_descriptor = real_open_runtime_root(
            flags,
            create=create,
            repair=repair,
            _owner_lock_cleanup=_owner_lock_cleanup,
        )
        runtime_root_returned = True
        return file_descriptor

    def close_then_report_failure(file_descriptor: int) -> bool:
        nonlocal injected_descriptor, close_attempts
        closed = real_try_close(file_descriptor)
        should_inject = nested_stage == "runtime_root" or runtime_root_returned
        if injected_descriptor < 0 and should_inject:
            assert closed is True
            injected_descriptor = file_descriptor
            close_attempts += 1
            return False
        return closed

    monkeypatch.setattr(store, "_open_runtime_root", recording_open_runtime_root)
    monkeypatch.setattr(store, "_try_close", close_then_report_failure)
    try:
        with pytest.raises(OwnerLockError) as captured:
            if operation == "acquire":
                OwnerLock.acquire(store)
            else:
                OwnerLock.adopt_inherited(store, candidate)

        _assert_private_error(
            captured.value,
            OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
            str(store.runtime_root),
            str(injected_descriptor),
        )
        assert close_attempts == 1
        _assert_closed(injected_descriptor)
        if operation == "adopt":
            _assert_closed(candidate)
    finally:
        if parent is not None:
            parent.close()


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_nested_owner_directory_cleanup_notes_active_process_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    opened: list[int] = []
    cleanup_attempts = 0
    error = error_factory()

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        file_descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(file_descriptor)
        return file_descriptor

    def interrupt_first_directory_fstat(file_descriptor: int) -> os.stat_result:
        if len(opened) >= 2 and file_descriptor == opened[1]:
            raise error
        return real_fstat(file_descriptor)

    def fail_first_directory_cleanup(file_descriptor: int) -> None:
        nonlocal cleanup_attempts
        if opened and file_descriptor == opened[0]:
            cleanup_attempts += 1
            raise OSError(errno.EIO, "raw nested cleanup /private/owner")
        real_close(file_descriptor)

    monkeypatch.setattr(state_module.os, "open", recording_open)
    monkeypatch.setattr(state_module.os, "fstat", interrupt_first_directory_fstat)
    monkeypatch.setattr(state_module.os, "close", fail_first_directory_cleanup)

    try:
        with pytest.raises(error_factory) as captured:
            OwnerLock.acquire(store)

        assert captured.value is error
        assert getattr(captured.value, "__notes__", []) == [
            "sidecar owner lock file cleanup failed"
        ]
        assert "raw nested cleanup" not in repr(captured.value.__notes__)
        assert len(opened) == 2
        assert cleanup_attempts == 1
        assert real_fstat(opened[0])
        with pytest.raises(OSError) as closed:
            real_fstat(opened[1])
        assert closed.value.errno == errno.EBADF
    finally:
        if opened:
            real_close(opened[0])


@pytest.mark.parametrize(
    "stage",
    [
        "exclusive_create_open",
        "existing_fallback_open",
        "fchmod",
        "first_fstat",
        "file_fsync",
        "directory_fsync",
        "canonical_first",
        "verification_open",
        "verified_fstat",
        "canonical_second",
    ],
)
def test_owner_lock_file_internal_failure_closes_every_allocated_descriptor_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    if stage == "existing_fallback_open":
        initialized = store._open_owner_lock_file(create=True)
        os.close(initialized)
    harness = _OwnerFileFaultHarness(store, stage=stage)
    harness.install(monkeypatch)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.acquire(store)

    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED,
        harness.raw_detail,
        str(store.lock_path),
    )
    assert harness.roles
    for role, file_descriptor in harness.roles.items():
        assert harness.close_counts[role] == 1
        harness.assert_closed(file_descriptor)


@pytest.mark.parametrize("cleanup_role", ["first", "directory"])
def test_owner_lock_file_success_path_close_failure_is_visible_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_role: str,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    harness = _OwnerFileFaultHarness(store, cleanup_role=cleanup_role)
    harness.install(monkeypatch)

    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.acquire(store)

        _assert_private_error(
            captured.value,
            OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
            "raw owner-file cleanup",
            str(store.lock_path),
        )
        assert set(harness.roles) == {"directory", "first", "verified"}
        for role, file_descriptor in harness.roles.items():
            assert harness.close_counts[role] == 1
            if role == cleanup_role:
                assert os.fstat(file_descriptor)
            else:
                harness.assert_closed(file_descriptor)
    finally:
        harness.close_unresolved_descriptor()


@pytest.mark.parametrize("cleanup_role", ["verified", "first", "directory"])
def test_owner_lock_file_finally_close_error_attempts_all_descriptors_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_role: str,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    harness = _OwnerFileFaultHarness(
        store,
        stage="canonical_second",
        cleanup_role=cleanup_role,
    )
    harness.install(monkeypatch)

    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.acquire(store)

        _assert_private_error(
            captured.value,
            OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
            harness.raw_detail,
            "raw owner-file cleanup",
            str(store.lock_path),
        )
        assert set(harness.roles) == {"directory", "first", "verified"}
        for role, file_descriptor in harness.roles.items():
            assert harness.close_counts[role] == 1
            if role == cleanup_role:
                assert os.fstat(file_descriptor)
            else:
                harness.assert_closed(file_descriptor)
    finally:
        harness.close_unresolved_descriptor()


def test_owner_lock_file_finally_never_retries_a_reused_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    harness = _OwnerFileFaultHarness(
        store,
        stage="canonical_second",
        cleanup_role="verified",
        reuse_cleanup_descriptor=True,
    )
    harness.install(monkeypatch)

    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.acquire(store)

        _assert_private_error(
            captured.value,
            OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
            harness.raw_detail,
            "raw owner-file cleanup",
        )
        assert harness.replacement_descriptor == harness.roles["verified"]
        assert harness.close_counts == {"verified": 1, "first": 1, "directory": 1}
        assert os.fstat(harness.replacement_descriptor)
        harness.assert_closed(harness.roles["first"])
        harness.assert_closed(harness.roles["directory"])
    finally:
        harness.close_unresolved_descriptor()


@pytest.mark.parametrize(
    "stage",
    ["exclusive_create_open", "first_fstat", "canonical_second"],
)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_owner_lock_file_process_control_propagates_after_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    error = error_factory()
    harness = _OwnerFileFaultHarness(
        store,
        stage=stage,
        control_error=error,
    )
    harness.install(monkeypatch)

    with pytest.raises(error_factory) as captured:
        OwnerLock.acquire(store)

    assert captured.value is error
    assert getattr(captured.value, "__notes__", None) is None
    assert harness.roles
    for role, file_descriptor in harness.roles.items():
        assert harness.close_counts[role] == 1
        harness.assert_closed(file_descriptor)


@pytest.mark.parametrize("cleanup_role", ["verified", "first", "directory"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_owner_lock_file_process_control_keeps_identity_and_fixed_cleanup_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_role: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    error = error_factory()
    harness = _OwnerFileFaultHarness(
        store,
        stage="canonical_second",
        control_error=error,
        cleanup_role=cleanup_role,
    )
    harness.install(monkeypatch)

    try:
        with pytest.raises(error_factory) as captured:
            OwnerLock.acquire(store)

        assert captured.value is error
        assert getattr(captured.value, "__notes__", []) == [
            "sidecar owner lock file cleanup failed"
        ]
        assert set(harness.roles) == {"directory", "first", "verified"}
        for role, file_descriptor in harness.roles.items():
            assert harness.close_counts[role] == 1
            if role == cleanup_role:
                assert os.fstat(file_descriptor)
            else:
                harness.assert_closed(file_descriptor)
    finally:
        harness.close_unresolved_descriptor()


@pytest.mark.parametrize(
    "shape",
    ["mode", "nonempty", "hardlink", "symlink", "directory", "fifo"],
)
def test_acquire_rejects_unsafe_existing_owner_file_without_repair(
    tmp_path: Path,
    shape: str,
) -> None:
    store = _store(tmp_path)
    store.ensure_private_directory()
    target = store.runtime_dir / "target"
    if shape == "directory":
        store.lock_path.mkdir(mode=0o700)
    elif shape == "fifo":
        os.mkfifo(store.lock_path, mode=0o600)
    elif shape == "symlink":
        target.write_bytes(b"")
        target.chmod(0o600)
        store.lock_path.symlink_to(target)
    else:
        store.lock_path.write_bytes(b"x" if shape == "nonempty" else b"")
        store.lock_path.chmod(0o644 if shape == "mode" else 0o600)
        if shape == "hardlink":
            os.link(store.lock_path, target)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.acquire(store)

    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED,
        str(store.lock_path),
    )
    if shape == "mode":
        assert stat.S_IMODE(os.lstat(store.lock_path).st_mode) == 0o644


def test_acquire_distinguishes_noncontention_flock_failure_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    raw_detail = "raw flock failure /private/owner"
    attempted_descriptor = -1
    closed: list[int] = []
    real_close = os.close

    def fail_flock(file_descriptor: int, operation: int) -> NoReturn:
        nonlocal attempted_descriptor
        attempted_descriptor = file_descriptor
        assert operation == fcntl.LOCK_EX | fcntl.LOCK_NB
        raise OSError(errno.EIO, raw_detail)

    def recording_close(file_descriptor: int) -> None:
        if file_descriptor == attempted_descriptor:
            closed.append(file_descriptor)
        real_close(file_descriptor)

    monkeypatch.setattr("flowsight.sidecar.owner_lock.fcntl.flock", fail_flock)
    monkeypatch.setattr("flowsight.sidecar.owner_lock.os.close", recording_close)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.acquire(store)

    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED,
        raw_detail,
        str(store.lock_path),
    )
    assert closed == [attempted_descriptor]


@pytest.mark.parametrize(
    "stage",
    [
        "first_open",
        "descriptor_check",
        "verification_open",
        "inode_check",
        "set_inheritable",
        "verification_close",
    ],
)
def test_acquire_failure_stages_close_each_allocated_descriptor_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    store = _store(tmp_path)
    initialized = OwnerLock.acquire(store)
    initialized.close()
    raw_detail = f"raw acquire {stage} /private/owner"
    opened: list[int] = []
    close_counts: dict[int, int] = {}
    real_open = owner_lock_module._open_store_descriptor
    real_close = os.close
    real_try_close = owner_lock_module._try_close

    def recording_open(trusted_store: StateStore, *, create: bool) -> int:
        if stage == "first_open" and not opened:
            raise StateStorageError(raw_detail)
        if stage == "verification_open" and len(opened) == 1:
            raise StateStorageError(raw_detail)
        descriptor = real_open(trusted_store, create=create)
        opened.append(descriptor)
        return descriptor

    def recording_close(file_descriptor: int) -> None:
        if file_descriptor in opened:
            close_counts[file_descriptor] = close_counts.get(file_descriptor, 0) + 1
        real_close(file_descriptor)

    def fail_descriptor_check(_file_descriptor: int) -> bool:
        raise OSError(errno.EIO, raw_detail)

    def fail_inode_check(_first: int, _second: int) -> bool:
        raise OSError(errno.EIO, raw_detail)

    def fail_inheritable(_file_descriptor: int, _inheritable: bool) -> NoReturn:
        raise OSError(errno.EIO, raw_detail)

    def fail_verification_close(file_descriptor: int) -> bool:
        closed = real_try_close(file_descriptor)
        if stage == "verification_close" and len(opened) > 1 and file_descriptor == opened[1]:
            assert closed is True
            return False
        return closed

    monkeypatch.setattr(owner_lock_module, "_open_store_descriptor", recording_open)
    monkeypatch.setattr(owner_lock_module.os, "close", recording_close)
    monkeypatch.setattr(owner_lock_module, "_try_close", fail_verification_close)
    if stage == "descriptor_check":
        monkeypatch.setattr(
            owner_lock_module,
            "_descriptor_is_exact_owner_file",
            fail_descriptor_check,
        )
    elif stage == "inode_check":
        monkeypatch.setattr(owner_lock_module, "_same_inode", fail_inode_check)
    elif stage == "set_inheritable":
        monkeypatch.setattr(owner_lock_module.os, "set_inheritable", fail_inheritable)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.acquire(store)

    expected_code = (
        OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED
        if stage == "verification_close"
        else OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED
    )
    _assert_private_error(captured.value, expected_code, raw_detail, str(store.lock_path))
    for descriptor in opened:
        assert close_counts[descriptor] == 1
        _assert_closed(descriptor)


@pytest.mark.parametrize(
    "stage",
    [
        "candidate_check",
        "probe_open",
        "first_inode_check",
        "probe_flock",
        "candidate_flock",
        "final_open",
        "final_check",
        "final_inode_check",
        "set_inheritable",
        "final_close",
        "probe_close",
    ],
)
def test_adopt_failure_stages_consume_and_close_every_descriptor_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    store = _store(tmp_path)
    parent = OwnerLock.acquire(store)
    candidate = os.dup(parent.fileno())
    raw_detail = f"raw adopt {stage} /private/owner"
    opened: list[int] = []
    tracked = {candidate}
    close_counts: dict[int, int] = {}
    real_open = owner_lock_module._open_store_descriptor
    real_close = os.close
    real_try_close = owner_lock_module._try_close
    real_check = owner_lock_module._descriptor_is_exact_owner_file
    real_same_inode = owner_lock_module._same_inode
    real_flock_status = owner_lock_module._flock_status
    check_calls = 0
    inode_calls = 0
    flock_calls = 0

    def recording_open(trusted_store: StateStore, *, create: bool) -> int:
        if stage == "probe_open" and not opened:
            raise StateStorageError(raw_detail)
        if stage == "final_open" and len(opened) == 1:
            raise StateStorageError(raw_detail)
        descriptor = real_open(trusted_store, create=create)
        opened.append(descriptor)
        tracked.add(descriptor)
        return descriptor

    def recording_close(file_descriptor: int) -> None:
        if file_descriptor in tracked:
            close_counts[file_descriptor] = close_counts.get(file_descriptor, 0) + 1
        real_close(file_descriptor)

    def injected_check(file_descriptor: int) -> bool:
        nonlocal check_calls
        check_calls += 1
        if stage == "candidate_check" and check_calls == 1:
            raise OSError(errno.EIO, raw_detail)
        if stage == "final_check" and check_calls == 2:
            return False
        return real_check(file_descriptor)

    def injected_same_inode(first: int, second: int) -> bool:
        nonlocal inode_calls
        inode_calls += 1
        if stage == "first_inode_check" and inode_calls == 1:
            return False
        if stage == "final_inode_check" and inode_calls == 2:
            return False
        return real_same_inode(first, second)

    def injected_flock_status(file_descriptor: int, operation: int) -> str:
        nonlocal flock_calls
        flock_calls += 1
        if stage == "probe_flock" and flock_calls == 1:
            return "error"
        if stage == "candidate_flock" and flock_calls == 2:
            return "error"
        return real_flock_status(file_descriptor, operation)

    def fail_inheritable(_file_descriptor: int, _inheritable: bool) -> NoReturn:
        raise OSError(errno.EIO, raw_detail)

    def injected_try_close(file_descriptor: int) -> bool:
        closed = real_try_close(file_descriptor)
        if stage == "final_close" and len(opened) > 1 and file_descriptor == opened[1]:
            assert closed is True
            return False
        if stage == "probe_close" and opened and file_descriptor == opened[0]:
            assert closed is True
            return False
        return closed

    monkeypatch.setattr(owner_lock_module, "_open_store_descriptor", recording_open)
    monkeypatch.setattr(owner_lock_module.os, "close", recording_close)
    monkeypatch.setattr(owner_lock_module, "_descriptor_is_exact_owner_file", injected_check)
    monkeypatch.setattr(owner_lock_module, "_same_inode", injected_same_inode)
    monkeypatch.setattr(owner_lock_module, "_flock_status", injected_flock_status)
    monkeypatch.setattr(owner_lock_module, "_try_close", injected_try_close)
    if stage == "set_inheritable":
        monkeypatch.setattr(owner_lock_module.os, "set_inheritable", fail_inheritable)

    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.adopt_inherited(store, candidate)
        expected_code = (
            OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED
            if stage in {"final_close", "probe_close"}
            else (
                OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED
                if stage == "candidate_flock"
                else OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
            )
        )
        _assert_private_error(captured.value, expected_code, raw_detail, str(candidate))
        for descriptor in [candidate, *opened]:
            assert close_counts[descriptor] == 1
            _assert_closed(descriptor)
    finally:
        parent.close()


@pytest.mark.parametrize("stage", ["descriptor_check", "set_inheritable"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_during_acquire_closes_allocated_descriptors_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    initialized = OwnerLock.acquire(store)
    initialized.close()
    error = error_factory()
    opened: list[int] = []
    close_counts: dict[int, int] = {}
    real_open = owner_lock_module._open_store_descriptor
    real_close = os.close

    def recording_open(trusted_store: StateStore, *, create: bool) -> int:
        descriptor = real_open(trusted_store, create=create)
        opened.append(descriptor)
        return descriptor

    def recording_close(file_descriptor: int) -> None:
        if file_descriptor in opened:
            close_counts[file_descriptor] = close_counts.get(file_descriptor, 0) + 1
        real_close(file_descriptor)

    def interrupt_check(_file_descriptor: int) -> bool:
        raise error

    def interrupt_inheritable(_file_descriptor: int, _inheritable: bool) -> NoReturn:
        raise error

    monkeypatch.setattr(owner_lock_module, "_open_store_descriptor", recording_open)
    monkeypatch.setattr(owner_lock_module.os, "close", recording_close)
    if stage == "descriptor_check":
        monkeypatch.setattr(
            owner_lock_module,
            "_descriptor_is_exact_owner_file",
            interrupt_check,
        )
    else:
        monkeypatch.setattr(owner_lock_module.os, "set_inheritable", interrupt_inheritable)

    with pytest.raises(error_factory) as captured:
        OwnerLock.acquire(store)

    assert captured.value is error
    assert getattr(captured.value, "__notes__", None) is None
    for descriptor in opened:
        assert close_counts[descriptor] == 1
        _assert_closed(descriptor)


@pytest.mark.parametrize("stage", ["candidate_check", "candidate_flock", "set_inheritable"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_during_adopt_closes_consumed_descriptors_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    parent = OwnerLock.acquire(store)
    candidate = os.dup(parent.fileno())
    error = error_factory()
    opened: list[int] = []
    tracked = {candidate}
    close_counts: dict[int, int] = {}
    real_open = owner_lock_module._open_store_descriptor
    real_close = os.close
    real_check = owner_lock_module._descriptor_is_exact_owner_file
    real_status = owner_lock_module._flock_status
    check_calls = 0
    flock_calls = 0

    def recording_open(trusted_store: StateStore, *, create: bool) -> int:
        descriptor = real_open(trusted_store, create=create)
        opened.append(descriptor)
        tracked.add(descriptor)
        return descriptor

    def recording_close(file_descriptor: int) -> None:
        if file_descriptor in tracked:
            close_counts[file_descriptor] = close_counts.get(file_descriptor, 0) + 1
        real_close(file_descriptor)

    def interrupt_check(file_descriptor: int) -> bool:
        nonlocal check_calls
        check_calls += 1
        if stage == "candidate_check" and check_calls == 1:
            raise error
        return real_check(file_descriptor)

    def interrupt_flock(file_descriptor: int, operation: int) -> str:
        nonlocal flock_calls
        flock_calls += 1
        if stage == "candidate_flock" and flock_calls == 2:
            raise error
        return real_status(file_descriptor, operation)

    def interrupt_inheritable(_file_descriptor: int, _inheritable: bool) -> NoReturn:
        raise error

    monkeypatch.setattr(owner_lock_module, "_open_store_descriptor", recording_open)
    monkeypatch.setattr(owner_lock_module.os, "close", recording_close)
    monkeypatch.setattr(owner_lock_module, "_descriptor_is_exact_owner_file", interrupt_check)
    monkeypatch.setattr(owner_lock_module, "_flock_status", interrupt_flock)
    if stage == "set_inheritable":
        monkeypatch.setattr(owner_lock_module.os, "set_inheritable", interrupt_inheritable)

    try:
        with pytest.raises(error_factory) as captured:
            OwnerLock.adopt_inherited(store, candidate)
        assert captured.value is error
        assert getattr(captured.value, "__notes__", None) is None
        for descriptor in [candidate, *opened]:
            assert close_counts[descriptor] == 1
            _assert_closed(descriptor)
    finally:
        parent.close()


@pytest.mark.parametrize("operation", ["acquire", "adopt"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_cleanup_failure_adds_fixed_note_to_active_process_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    parent: OwnerLock | None = None
    candidate = -1
    error = error_factory()
    target_descriptor = -1
    close_attempts = 0
    real_open = owner_lock_module._open_store_descriptor
    real_close = os.close

    def recording_open(trusted_store: StateStore, *, create: bool) -> int:
        nonlocal target_descriptor
        descriptor = real_open(trusted_store, create=create)
        if operation == "acquire" and target_descriptor < 0:
            target_descriptor = descriptor
        return descriptor

    def interrupt_check(_file_descriptor: int) -> bool:
        raise error

    def fail_target_close(file_descriptor: int) -> None:
        nonlocal close_attempts
        if file_descriptor == target_descriptor:
            close_attempts += 1
            raise OSError(errno.EIO, "raw cleanup failure /private/owner")
        real_close(file_descriptor)

    if operation == "adopt":
        parent = OwnerLock.acquire(store)
        candidate = os.dup(parent.fileno())
        target_descriptor = candidate
    else:
        initialized = OwnerLock.acquire(store)
        initialized.close()

    monkeypatch.setattr(owner_lock_module, "_open_store_descriptor", recording_open)
    monkeypatch.setattr(
        owner_lock_module,
        "_descriptor_is_exact_owner_file",
        interrupt_check,
    )
    monkeypatch.setattr(owner_lock_module.os, "close", fail_target_close)
    try:
        with pytest.raises(error_factory) as captured:
            if operation == "acquire":
                OwnerLock.acquire(store)
            else:
                OwnerLock.adopt_inherited(store, candidate)
        assert captured.value is error
        assert getattr(captured.value, "__notes__", []) == ["sidecar owner lock cleanup failed"]
        assert close_attempts == 1
        assert os.fstat(target_descriptor)
    finally:
        real_close(target_descriptor)
        if parent is not None:
            parent.close()


def test_duplicate_of_exclusive_owner_can_be_adopted_gaplessly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = OwnerLock.acquire(store)
    inherited_descriptor = os.dup(parent.fileno())

    child = OwnerLock.adopt_inherited(store, inherited_descriptor)
    assert child.fileno() == inherited_descriptor
    assert os.get_inheritable(child.fileno()) is False
    parent.close()
    with pytest.raises(OwnerLockError) as held:
        OwnerLock.acquire(store)
    assert held.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD
    child.close()

    retry = OwnerLock.acquire(store)
    retry.close()


def test_unheld_and_shared_canonical_descriptors_are_rejected_and_consumed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    created = OwnerLock.acquire(store)
    created.close()

    unheld = _open_owner_path(store)
    with pytest.raises(OwnerLockError) as unheld_error:
        OwnerLock.adopt_inherited(store, unheld)
    assert unheld_error.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
    _assert_closed(unheld)

    shared = _open_owner_path(store)
    fcntl.flock(shared, fcntl.LOCK_SH | fcntl.LOCK_NB)
    with pytest.raises(OwnerLockError) as shared_error:
        OwnerLock.adopt_inherited(store, shared)
    assert shared_error.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
    _assert_closed(shared)


def test_read_only_canonical_descriptor_is_rejected_and_consumed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    initialized = OwnerLock.acquire(store)
    initialized.close()
    read_only = os.open(store.lock_path, os.O_RDONLY | os.O_NONBLOCK)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.adopt_inherited(store, read_only)

    assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
    _assert_closed(read_only)


def test_other_project_owner_descriptor_is_rejected_without_releasing_its_lock(
    tmp_path: Path,
) -> None:
    expected_store = _store(tmp_path)
    other_store = StateStore(tmp_path / "runtime", project_id="another-project")
    other_owner = OwnerLock.acquire(other_store)
    transferred = os.dup(other_owner.fileno())
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.adopt_inherited(expected_store, transferred)
        assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
        _assert_closed(transferred)
        with pytest.raises(OwnerLockError) as held:
            OwnerLock.acquire(other_store)
        assert held.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD
    finally:
        other_owner.close()


@pytest.mark.parametrize("mutation", ["mode", "hardlink"])
def test_adopt_rejects_inherited_descriptor_whose_metadata_became_unsafe(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = _store(tmp_path)
    parent = OwnerLock.acquire(store)
    inherited = os.dup(parent.fileno())
    hardlink = store.runtime_dir / "owner-lock-hardlink"
    if mutation == "mode":
        store.lock_path.chmod(0o644)
    else:
        os.link(store.lock_path, hardlink)
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.adopt_inherited(store, inherited)
        assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
        _assert_closed(inherited)
    finally:
        if mutation == "mode":
            store.lock_path.chmod(0o600)
        elif hardlink.exists():
            hardlink.unlink()
        parent.close()


def test_separate_open_description_cannot_claim_another_owner_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    owner = OwnerLock.acquire(store)
    separate = _open_owner_path(store)
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.adopt_inherited(store, separate)
        assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
        _assert_closed(separate)
        assert owner.fileno() >= 3
    finally:
        owner.close()


def test_wrong_regular_pipe_socket_and_directory_descriptors_are_consumed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    owner = OwnerLock.acquire(store)
    wrong_path = tmp_path / "wrong-lock"
    wrong_path.write_bytes(b"")
    wrong_path.chmod(0o600)
    wrong_regular = os.open(wrong_path, os.O_RDWR)
    pipe_read, pipe_write = os.pipe()
    network_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    socket_descriptor = network_socket.detach()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY)
    descriptors = [wrong_regular, pipe_read, socket_descriptor, directory_descriptor]
    try:
        for descriptor in descriptors:
            with pytest.raises(OwnerLockError) as captured:
                OwnerLock.adopt_inherited(store, descriptor)
            assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
            _assert_closed(descriptor)
    finally:
        os.close(pipe_write)
        owner.close()


@pytest.mark.parametrize(
    "invalid_descriptor",
    [True, False, -1, 0, 1, 2, 3.0, "3", _IntDescriptor.VALUE, _DescriptorSubclass(9)],
)
def test_malformed_inherited_descriptors_fail_before_consumption(
    tmp_path: Path,
    invalid_descriptor: object,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.adopt_inherited(store, invalid_descriptor)  # type: ignore[arg-type]
    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID,
        repr(invalid_descriptor),
    )


def test_closed_inherited_descriptor_is_invalid_without_closing_reused_fd(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    file_descriptor = os.open(os.devnull, os.O_RDONLY)
    os.close(file_descriptor)

    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.adopt_inherited(store, file_descriptor)

    assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID


def test_replaced_canonical_path_rejects_inherited_old_inode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    owner = OwnerLock.acquire(store)
    inherited = os.dup(owner.fileno())
    moved = store.runtime_dir / "owner-lock-moved"
    store.lock_path.rename(moved)
    store.lock_path.write_bytes(b"")
    store.lock_path.chmod(0o600)
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.adopt_inherited(store, inherited)
        assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
        _assert_closed(inherited)
    finally:
        owner.close()
        store.lock_path.unlink()
        moved.rename(store.lock_path)


def test_owner_lock_file_swap_during_acquire_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    real_open = os.open
    owner_open_count = 0
    moved_name = "sidecar-owner-lock-moved"

    def swap_before_verification(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal owner_open_count
        if os.fsdecode(path) == "sidecar-owner.lock" and dir_fd is not None:
            owner_open_count += 1
            if owner_open_count == 2:
                os.rename(
                    "sidecar-owner.lock",
                    moved_name,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
                replacement = real_open(
                    "sidecar-owner.lock",
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                    dir_fd=dir_fd,
                )
                real_close = os.close
                real_close(replacement)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_verification)
    with pytest.raises(OwnerLockError) as captured:
        OwnerLock.acquire(store)

    assert captured.value.code is OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED
    assert owner_open_count == 2


def test_acquire_rechecks_canonical_inode_after_flock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    initialized = OwnerLock.acquire(store)
    initialized.close()
    moved = store.runtime_dir / "owner-lock-after-flock"
    real_status = owner_lock_module._flock_status
    swapped = False

    def swap_after_successful_flock(file_descriptor: int, operation: int) -> str:
        nonlocal swapped
        status_value = real_status(file_descriptor, operation)
        if status_value == "ok" and operation == owner_lock_module._LOCK_OPERATION:
            store.lock_path.rename(moved)
            store.lock_path.write_bytes(b"")
            store.lock_path.chmod(0o600)
            swapped = True
        return status_value

    monkeypatch.setattr(owner_lock_module, "_flock_status", swap_after_successful_flock)
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.acquire(store)
        assert captured.value.code is OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED
        assert swapped is True
    finally:
        if store.lock_path.exists():
            store.lock_path.unlink()
        if moved.exists():
            moved.rename(store.lock_path)


def test_adopt_rechecks_canonical_inode_after_continuity_reassertion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    parent = OwnerLock.acquire(store)
    inherited = os.dup(parent.fileno())
    moved = store.runtime_dir / "owner-lock-after-adopt-reassertion"
    real_status = owner_lock_module._flock_status
    candidate_reassertions = 0

    def swap_after_candidate_reassertion(file_descriptor: int, operation: int) -> str:
        nonlocal candidate_reassertions
        status_value = real_status(file_descriptor, operation)
        if status_value == "ok" and operation == owner_lock_module._LOCK_OPERATION:
            candidate_reassertions += 1
            store.lock_path.rename(moved)
            store.lock_path.write_bytes(b"")
            store.lock_path.chmod(0o600)
        return status_value

    monkeypatch.setattr(
        owner_lock_module,
        "_flock_status",
        swap_after_candidate_reassertion,
    )
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.adopt_inherited(store, inherited)
        assert captured.value.code is OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
        assert candidate_reassertions == 1
        _assert_closed(inherited)
    finally:
        parent.close()
        if store.lock_path.exists():
            store.lock_path.unlink()
        if moved.exists():
            moved.rename(store.lock_path)


def test_close_is_idempotent_uses_no_unlock_and_closed_fileno_is_fixed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = OwnerLock.acquire(_store(tmp_path))
    calls: list[tuple[int, int]] = []

    def unexpected_flock(file_descriptor: int, operation: int) -> None:
        calls.append((file_descriptor, operation))

    monkeypatch.setattr("flowsight.sidecar.owner_lock.fcntl.flock", unexpected_flock)
    owner.close()
    owner.close()

    assert calls == []
    with pytest.raises(OwnerLockError) as captured:
        owner.fileno()
    _assert_private_error(captured.value, OwnerLockErrorCode.OWNER_LOCK_CLOSED)


def test_context_manager_closes_normally_and_preserves_body_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with OwnerLock.acquire(store) as owner:
        descriptor = owner.fileno()
    _assert_closed(descriptor)
    assert repr(owner) == "<OwnerLock closed>"

    body_error = _BodyError("private body detail")
    with pytest.raises(_BodyError) as captured:
        with OwnerLock.acquire(store) as failed_owner:
            failed_descriptor = failed_owner.fileno()
            raise body_error
    assert captured.value is body_error
    assert getattr(captured.value, "__notes__", None) is None
    _assert_closed(failed_descriptor)


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_context_manager_preserves_process_control_identity(
    tmp_path: Path,
    error_factory: type[BaseException],
) -> None:
    error = error_factory()
    with pytest.raises(error_factory) as captured:
        with OwnerLock.acquire(_store(tmp_path)) as owner:
            descriptor = owner.fileno()
            raise error
    assert captured.value is error
    assert getattr(captured.value, "__notes__", None) is None
    _assert_closed(descriptor)


def test_context_manager_close_failure_adds_fixed_note_and_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body_error = _BodyError("private body detail")
    owner = OwnerLock.acquire(_store(tmp_path))
    descriptor = owner.fileno()
    calls: list[int] = []

    def report_close_failure(file_descriptor: int) -> bool:
        calls.append(file_descriptor)
        return False

    monkeypatch.setattr(owner_lock_module, "_try_close", report_close_failure)
    with pytest.raises(_BodyError) as captured:
        with owner:
            raise body_error

    assert captured.value is body_error
    assert getattr(captured.value, "__notes__", []) == ["sidecar owner lock cleanup failed"]
    assert calls == [descriptor]
    assert repr(owner) == "<OwnerLock closed>"
    owner.close()
    os.close(descriptor)


def test_context_manager_close_failure_without_body_error_is_fixed_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = OwnerLock.acquire(_store(tmp_path))
    descriptor = owner.fileno()
    calls: list[int] = []

    def report_close_failure(file_descriptor: int) -> bool:
        calls.append(file_descriptor)
        return False

    monkeypatch.setattr(owner_lock_module, "_try_close", report_close_failure)
    with pytest.raises(OwnerLockError) as captured:
        with owner:
            pass

    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
        str(descriptor),
    )
    assert calls == [descriptor]
    assert repr(owner) == "<OwnerLock closed>"
    owner.close()
    os.close(descriptor)


def test_close_failure_is_visible_and_never_retries_reused_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = OwnerLock.acquire(_store(tmp_path))
    owner_descriptor = owner.fileno()
    real_close = os.close
    replacement_descriptor = -1
    injected = False

    def close_then_report_failure(file_descriptor: int) -> None:
        nonlocal injected, replacement_descriptor
        if file_descriptor == owner_descriptor and not injected:
            injected = True
            real_close(file_descriptor)
            temporary_descriptor = os.open(os.devnull, os.O_RDONLY)
            if temporary_descriptor != file_descriptor:
                replacement_descriptor = os.dup2(temporary_descriptor, file_descriptor)
                real_close(temporary_descriptor)
            else:
                replacement_descriptor = temporary_descriptor
            raise OSError(errno.EIO, "raw close failure /private/owner")
        real_close(file_descriptor)

    monkeypatch.setattr("flowsight.sidecar.owner_lock.os.close", close_then_report_failure)
    with pytest.raises(OwnerLockError) as captured:
        owner.close()

    _assert_private_error(
        captured.value,
        OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
        "raw close failure",
        str(owner_descriptor),
    )
    owner.close()
    assert os.fstat(replacement_descriptor)
    real_close(replacement_descriptor)


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_from_close_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_factory: type[BaseException],
) -> None:
    owner = OwnerLock.acquire(_store(tmp_path))
    descriptor = owner.fileno()
    error = error_factory()
    real_close = os.close
    calls = 0

    def interrupt_close(file_descriptor: int) -> None:
        nonlocal calls
        assert file_descriptor == descriptor
        calls += 1
        raise error

    monkeypatch.setattr("flowsight.sidecar.owner_lock.os.close", interrupt_close)
    with pytest.raises(error_factory) as captured:
        owner.close()
    assert captured.value is error
    assert calls == 1
    owner.close()
    real_close(descriptor)


def _run_contender(store: StateStore, expected: str) -> subprocess.CompletedProcess[str]:
    source = r"""
import sys
from pathlib import Path
from flowsight.sidecar import OwnerLock, OwnerLockError, OwnerLockErrorCode, StateStore

store = StateStore(Path(sys.argv[1]), project_id=sys.argv[2])
try:
    owner = OwnerLock.acquire(store)
except OwnerLockError as error:
    outcome = str(error.code)
else:
    outcome = "ACQUIRED"
    owner.close()
raise SystemExit(0 if outcome == sys.argv[3] else 9)
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(store.runtime_root),
            store.project_id,
            expected,
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_process_exit_releases_owner_lock_without_explicit_close(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = r"""
import os
import sys
from pathlib import Path
from flowsight.sidecar import OwnerLock, StateStore

store = StateStore(Path(sys.argv[1]), project_id=sys.argv[2])
OwnerLock.acquire(store)
os._exit(0)
"""
    child = subprocess.run(
        [sys.executable, "-c", source, str(store.runtime_root), store.project_id],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0, child.stderr.decode()

    successor = OwnerLock.acquire(store)
    successor.close()


def test_acquire_promotes_closed_stdio_descriptor_for_inherited_adoption(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source = r"""
import fcntl
import os
import sys
from pathlib import Path
from flowsight.sidecar import OwnerLock, OwnerLockError, OwnerLockErrorCode, StateStore

for file_descriptor in (0, 1, 2):
    try:
        os.close(file_descriptor)
    except OSError:
        pass

store = StateStore(Path(sys.argv[1]), project_id=sys.argv[2])
owner = OwnerLock.acquire(store)
if owner.fileno() < 3:
    raise SystemExit(11)
transferred = fcntl.fcntl(owner.fileno(), fcntl.F_DUPFD_CLOEXEC, 3)
if transferred < 3:
    raise SystemExit(12)
adopted = OwnerLock.adopt_inherited(store, transferred)
if adopted.fileno() != transferred or os.get_inheritable(adopted.fileno()):
    raise SystemExit(13)
owner.close()
try:
    OwnerLock.acquire(store)
except OwnerLockError as error:
    if error.code is not OwnerLockErrorCode.OWNER_LOCK_HELD:
        raise SystemExit(14)
else:
    raise SystemExit(15)
adopted.close()
retry = OwnerLock.acquire(store)
retry.close()
raise SystemExit(0)
"""

    child = subprocess.run(
        [sys.executable, "-c", source, str(store.runtime_root), store.project_id],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert child.returncode == 0, child.stderr.decode(errors="replace")


def test_owner_descriptor_is_closed_by_exec_without_pass_fds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    owner = OwnerLock.acquire(store)
    metadata = os.fstat(owner.fileno())
    source = r"""
import os
import sys

try:
    metadata = os.fstat(int(sys.argv[1]))
except OSError:
    raise SystemExit(0)
same_inode = (metadata.st_dev, metadata.st_ino) == (int(sys.argv[2]), int(sys.argv[3]))
raise SystemExit(9 if same_inode else 0)
"""
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                source,
                str(owner.fileno()),
                str(metadata.st_dev),
                str(metadata.st_ino),
            ],
            cwd=Path(__file__).parents[2],
            close_fds=False,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert child.returncode == 0, child.stderr.decode()
    finally:
        owner.close()


def test_real_exec_pass_fds_preserves_lock_until_child_last_close(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = OwnerLock.acquire(store)
    parent_metadata = os.fstat(parent.fileno())
    source = r"""
import json
import os
import sys
from pathlib import Path
from flowsight.sidecar import OwnerLock, StateStore

store = StateStore(Path(sys.argv[1]), project_id=sys.argv[2])
owner = OwnerLock.adopt_inherited(store, int(sys.argv[3]))
metadata = os.fstat(owner.fileno())
print(json.dumps({
    "status": "adopted",
    "inheritable": os.get_inheritable(owner.fileno()),
    "device": metadata.st_dev,
    "inode": metadata.st_ino,
}), flush=True)
sys.stdin.buffer.read(1)
owner.close()
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            source,
            str(store.runtime_root),
            store.project_id,
            str(parent.fileno()),
        ],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        close_fds=True,
        pass_fds=(parent.fileno(),),
    )
    assert child.stdin is not None
    assert child.stdout is not None
    assert child.stderr is not None
    try:
        ready = json.loads(_readline_bounded(child.stdout))
        assert ready == {
            "status": "adopted",
            "inheritable": False,
            "device": parent_metadata.st_dev,
            "inode": parent_metadata.st_ino,
        }

        parent.close()
        held = _run_contender(store, str(OwnerLockErrorCode.OWNER_LOCK_HELD))
        assert held.returncode == 0, held.stderr

        child.stdin.write(b"x")
        child.stdin.close()
        child.wait(timeout=10)
        errors = child.stderr.read().decode()
        assert child.returncode == 0, errors

        acquired = _run_contender(store, "ACQUIRED")
        assert acquired.returncode == 0, acquired.stderr
        final_metadata = os.lstat(store.lock_path)
        assert (final_metadata.st_dev, final_metadata.st_ino) == (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        )
    finally:
        parent.close()
        _reap_process(child)


def test_two_independent_exec_contenders_have_exactly_one_winner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = r"""
import sys
from pathlib import Path
from flowsight.sidecar import OwnerLock, OwnerLockError, StateStore

store = StateStore(Path(sys.argv[1]), project_id=sys.argv[2])
sys.stdin.buffer.read(1)
try:
    owner = OwnerLock.acquire(store)
except OwnerLockError as error:
    print(str(error.code), flush=True)
else:
    print("ACQUIRED", flush=True)
    sys.stdin.buffer.read(1)
    owner.close()
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", source, str(store.runtime_root), store.project_id],
            cwd=Path(__file__).parents[2],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            close_fds=True,
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(b"x")
            process.stdin.flush()
        outcomes = {
            process: _readline_bounded(process.stdout).decode().strip()
            for process in processes
            if process.stdout is not None
        }
        assert len(outcomes) == len(processes)
        assert sorted(outcomes.values()) == [
            "ACQUIRED",
            str(OwnerLockErrorCode.OWNER_LOCK_HELD),
        ]
        winner = next(process for process, outcome in outcomes.items() if outcome == "ACQUIRED")
        assert winner.stdin is not None
        winner.stdin.write(b"x")
        winner.stdin.close()
        loser = next(process for process in processes if process is not winner)
        assert loser.stdin is not None
        loser.stdin.close()
        for process in processes:
            process.wait(timeout=10)
            assert process.stderr is not None
            assert process.returncode == 0, process.stderr.read().decode()
    finally:
        for process in processes:
            _reap_process(process)


_MOVE_ONLY_MESSAGE = "OwnerLock is move-only"


class _OpaqueMoveOnlyInput:
    __slots__ = ("label", "__weakref__")

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> NoReturn:
        raise AssertionError(f"{self.label} was represented")

    def __bool__(self) -> NoReturn:
        raise AssertionError(f"{self.label} truthiness was inspected")

    def __index__(self) -> NoReturn:
        raise AssertionError(f"{self.label} index was inspected")


def _assert_move_only_error(error: BaseException, *forbidden: str) -> None:
    assert type(error) is TypeError
    assert error.args == (_MOVE_ONLY_MESSAGE,)
    assert str(error) == _MOVE_ONLY_MESSAGE
    assert repr(error) == f"TypeError({_MOVE_ONLY_MESSAGE!r})"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", None) is None
    exposed = "\n".join((str(error), repr(error), repr(error.args)))
    for value in forbidden:
        assert value not in exposed


def _expect_move_only(
    operation: Callable[[], object],
    *forbidden: str,
) -> TypeError:
    with pytest.raises(TypeError) as captured:
        operation()
    error = captured.value
    _assert_move_only_error(error, *forbidden)
    return error


def _assert_deleted_traceback_local(error: BaseException, name: str) -> None:
    traceback_cursor = error.__traceback__
    assert traceback_cursor is not None
    while traceback_cursor.tb_next is not None:
        traceback_cursor = traceback_cursor.tb_next
    assert name not in traceback_cursor.tb_frame.f_locals


def _pickle_operation(owner: OwnerLock, protocol: int) -> Callable[[], bytes]:
    def operation() -> bytes:
        return pickle.dumps(owner, protocol=protocol)

    return operation


def _exact_owner_ids() -> set[int]:
    gc.collect()
    return {id(value) for value in gc.get_objects() if type(value) is OwnerLock}


@pytest.mark.parametrize("closed", [False, True], ids=["active", "closed"])
def test_owner_lock_default_copy_and_pickle_matrix_is_move_only(
    tmp_path: Path,
    closed: bool,
) -> None:
    store = _store(tmp_path)
    owner = OwnerLock.acquire(store)
    descriptor = owner.fileno()
    metadata = os.fstat(descriptor)
    inheritable = os.get_inheritable(descriptor)
    expected_repr = "<OwnerLock active>"
    replacement_descriptor = -1
    if closed:
        owner.close()
        expected_repr = "<OwnerLock closed>"
        replacement_descriptor = os.open(os.devnull, os.O_RDONLY)
        if replacement_descriptor != descriptor:
            promoted = os.dup2(replacement_descriptor, descriptor)
            os.close(replacement_descriptor)
            replacement_descriptor = promoted
        assert replacement_descriptor == descriptor

    memo_value = object()
    memo: dict[int, object] = {sys.maxsize: memo_value}
    memo_snapshot = dict(memo)
    operations: list[tuple[str, Callable[[], object]]] = [
        ("copy.copy", lambda: copy.copy(owner)),
        ("copy.deepcopy", lambda: copy.deepcopy(owner)),
        ("copy.deepcopy with memo", lambda: copy.deepcopy(owner, memo)),
        ("direct __copy__", owner.__copy__),
        ("direct __deepcopy__", lambda: owner.__deepcopy__(memo)),
        ("direct __reduce__", owner.__reduce__),
        ("direct __reduce_ex__", lambda: owner.__reduce_ex__(-1)),
        ("pickle default", lambda: pickle.dumps(owner)),
    ]
    for protocol in [-1, *range(pickle.HIGHEST_PROTOCOL + 1)]:
        operations.append(
            (
                f"pickle protocol {protocol}",
                _pickle_operation(owner, protocol),
            )
        )

    owner_ids = _exact_owner_ids()
    try:
        for label, operation in operations:
            error = _expect_move_only(
                operation,
                str(store.lock_path),
                str(descriptor),
                label,
            )
            del error
            assert _exact_owner_ids() == owner_ids, label
            assert memo == memo_snapshot, label
            assert repr(owner) == expected_repr, label

        if closed:
            assert replacement_descriptor >= 0
            assert os.fstat(replacement_descriptor).st_mode
            with pytest.raises(OwnerLockError) as captured_closed:
                owner.fileno()
            _assert_private_error(
                captured_closed.value,
                OwnerLockErrorCode.OWNER_LOCK_CLOSED,
            )
        else:
            assert owner.fileno() == descriptor
            current_metadata = os.fstat(descriptor)
            assert (current_metadata.st_dev, current_metadata.st_ino) == (
                metadata.st_dev,
                metadata.st_ino,
            )
            assert os.get_inheritable(descriptor) is inheritable
            with pytest.raises(OwnerLockError) as captured_held:
                OwnerLock.acquire(store)
            _assert_private_error(
                captured_held.value,
                OwnerLockErrorCode.OWNER_LOCK_HELD,
            )
    finally:
        owner.close()
        if replacement_descriptor >= 0:
            try:
                os.close(replacement_descriptor)
            except OSError:
                pass

    successor = OwnerLock.acquire(store)
    successor.close()


def test_copy_replace_remains_unsupported_without_a_reconstruction_hook(
    tmp_path: Path,
) -> None:
    replace = getattr(copy, "replace", None)
    if replace is None:
        return

    store = _store(tmp_path)
    owner = OwnerLock.acquire(store)
    descriptor = owner.fileno()
    owner_ids = _exact_owner_ids()
    try:
        assert "__replace__" not in OwnerLock.__dict__
        with pytest.raises(TypeError) as captured:
            replace(owner)
        assert _MOVE_ONLY_MESSAGE not in str(captured.value)
        assert str(descriptor) not in str(captured.value)
        assert _exact_owner_ids() == owner_ids
        assert owner.fileno() == descriptor
    finally:
        owner.close()


def test_direct_guards_do_not_inspect_or_retain_opaque_inputs(tmp_path: Path) -> None:
    owner = OwnerLock.acquire(_store(tmp_path))
    owner_ids = _exact_owner_ids()

    def exercise_guards() -> tuple[
        weakref.ReferenceType[_OpaqueMoveOnlyInput],
        weakref.ReferenceType[_OpaqueMoveOnlyInput],
    ]:
        memo_input = _OpaqueMoveOnlyInput("memo-secret")
        protocol_input = _OpaqueMoveOnlyInput("protocol-secret")
        memo_reference = weakref.ref(memo_input)
        protocol_reference = weakref.ref(protocol_input)
        deepcopy_error = _expect_move_only(
            lambda: owner.__deepcopy__(memo_input),  # type: ignore[arg-type]
            "memo-secret",
        )
        reduce_error = _expect_move_only(
            lambda: owner.__reduce_ex__(protocol_input),
            "protocol-secret",
        )
        _assert_deleted_traceback_local(deepcopy_error, "memo")
        _assert_deleted_traceback_local(reduce_error, "protocol")
        assert all(
            value is not memo_input and value is not protocol_input
            for value in vars(owner_lock_module).values()
        )
        del deepcopy_error, reduce_error
        return memo_reference, protocol_reference

    try:
        memo_reference, protocol_reference = exercise_guards()
        gc.collect()
        assert memo_reference() is None
        assert protocol_reference() is None
        assert _exact_owner_ids() == owner_ids
    finally:
        owner.close()


def test_move_only_error_suppresses_but_does_not_rewrite_caller_context(
    tmp_path: Path,
) -> None:
    owner = OwnerLock.acquire(_store(tmp_path))
    raw_detail = "caller-context-secret /private/context"
    try:
        try:
            raise RuntimeError(raw_detail)
        except RuntimeError as active_error:
            with pytest.raises(TypeError) as captured:
                owner.__copy__()
            error = captured.value
            assert type(error) is TypeError
            assert error.args == (_MOVE_ONLY_MESSAGE,)
            assert error.__cause__ is None
            assert error.__context__ is active_error
            assert error.__suppress_context__ is True
            assert getattr(error, "__notes__", None) is None
            rendered = "".join(traceback.format_exception(error))
            assert _MOVE_ONLY_MESSAGE in rendered
            assert raw_detail not in rendered
    finally:
        owner.close()


def test_move_only_guards_make_no_owner_or_system_callouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner = OwnerLock.acquire(_store(tmp_path))
    unexpected_calls: list[str] = []
    caplog.clear()

    def unexpected(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        unexpected_calls.append("called")
        raise AssertionError("move-only guard performed a forbidden callout")

    try:
        with monkeypatch.context() as patcher:
            for attribute in ("fileno", "close", "__enter__", "__exit__", "__repr__"):
                patcher.setattr(OwnerLock, attribute, unexpected)
            for attribute in (
                "_descriptor_is_exact_owner_file",
                "_flock_status",
                "_open_store_descriptor",
                "_same_inode",
                "_try_close",
            ):
                patcher.setattr(owner_lock_module, attribute, unexpected)
            for attribute in (
                "close",
                "dup",
                "dup2",
                "fstat",
                "get_inheritable",
                "open",
                "set_inheritable",
                "unlink",
            ):
                patcher.setattr(owner_lock_module.os, attribute, unexpected)
            for attribute in ("fcntl", "flock"):
                patcher.setattr(owner_lock_module.fcntl, attribute, unexpected)

            operations: tuple[Callable[[], object], ...] = (
                lambda: copy.copy(owner),
                lambda: copy.deepcopy(owner),
                owner.__copy__,
                lambda: owner.__deepcopy__({}),
                owner.__reduce__,
                lambda: owner.__reduce_ex__(pickle.HIGHEST_PROTOCOL),
                lambda: pickle.dumps(owner, protocol=pickle.HIGHEST_PROTOCOL),
            )
            for operation in operations:
                error = _expect_move_only(operation)
                del error
        assert unexpected_calls == []
        output = capsys.readouterr()
        assert output.out == ""
        assert output.err == ""
        assert caplog.records == []
        assert owner.fileno() >= 3
    finally:
        owner.close()


def test_rejected_duplication_precedes_one_ambiguous_close_and_fd_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    owner = OwnerLock.acquire(store)
    descriptor = owner.fileno()
    metadata = os.fstat(descriptor)
    real_try_close = owner_lock_module._try_close
    close_calls = 0
    replacement_descriptor = -1

    def ambiguous_close(file_descriptor: int) -> bool:
        nonlocal close_calls, replacement_descriptor
        if file_descriptor != descriptor:
            return real_try_close(file_descriptor)
        close_calls += 1
        os.close(file_descriptor)
        replacement_descriptor = os.open(os.devnull, os.O_RDONLY)
        if replacement_descriptor != file_descriptor:
            promoted = os.dup2(replacement_descriptor, file_descriptor)
            os.close(replacement_descriptor)
            replacement_descriptor = promoted
        assert replacement_descriptor == descriptor
        return False

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(owner_lock_module, "_try_close", ambiguous_close)
            operations: tuple[Callable[[], object], ...] = (
                lambda: copy.copy(owner),
                lambda: copy.deepcopy(owner),
                lambda: pickle.dumps(owner, protocol=pickle.HIGHEST_PROTOCOL),
            )
            for operation in operations:
                error = _expect_move_only(operation)
                del error
            assert close_calls == 0
            assert replacement_descriptor == -1
            assert owner.fileno() == descriptor

            with pytest.raises(OwnerLockError) as captured:
                owner.close()
            _assert_private_error(
                captured.value,
                OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
            )
            owner.close()
            assert close_calls == 1
            assert replacement_descriptor == descriptor
            assert os.fstat(replacement_descriptor).st_mode

            successor = OwnerLock.acquire(store)
            try:
                successor_metadata = os.fstat(successor.fileno())
                assert (successor_metadata.st_dev, successor_metadata.st_ino) == (
                    metadata.st_dev,
                    metadata.st_ino,
                )
                assert os.fstat(replacement_descriptor).st_mode
            finally:
                successor.close()
        assert os.fstat(replacement_descriptor).st_mode
    finally:
        owner.close()
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)


def test_owner_lock_move_only_guards_have_exact_static_shape() -> None:
    source_path = Path(owner_lock_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    owner_classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OwnerLock"
    ]
    assert len(owner_classes) == 1
    method_nodes = [
        node
        for node in owner_classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert [node.name for node in method_nodes] == [
        "__init__",
        "_from_descriptor",
        "acquire",
        "adopt_inherited",
        "__repr__",
        "__copy__",
        "__deepcopy__",
        "__reduce__",
        "__reduce_ex__",
        "fileno",
        "close",
        "__enter__",
        "__exit__",
    ]
    methods = {node.name: node for node in method_nodes}

    expected_arguments: dict[str, list[tuple[str, str | None]]] = {
        "__copy__": [("self", None)],
        "__deepcopy__": [("self", None), ("memo", "dict[int, object]")],
        "__reduce__": [("self", None)],
        "__reduce_ex__": [("self", None), ("protocol", "object")],
    }
    deleted_argument = {
        "__copy__": None,
        "__deepcopy__": "memo",
        "__reduce__": None,
        "__reduce_ex__": "protocol",
    }
    for method_name, expected in expected_arguments.items():
        method = methods[method_name]
        assert isinstance(method, ast.FunctionDef)
        assert method.decorator_list == []
        assert method.args.posonlyargs == []
        assert method.args.vararg is None
        assert method.args.kwonlyargs == []
        assert method.args.kw_defaults == []
        assert method.args.kwarg is None
        assert method.args.defaults == []
        actual_arguments = [
            (
                argument.arg,
                ast.unparse(argument.annotation) if argument.annotation is not None else None,
            )
            for argument in method.args.args
        ]
        assert actual_arguments == expected
        assert method.returns is not None
        assert ast.unparse(method.returns) == "NoReturn"

        body = list(method.body)
        expected_deleted = deleted_argument[method_name]
        if expected_deleted is not None:
            delete = body.pop(0)
            assert isinstance(delete, ast.Delete)
            assert len(delete.targets) == 1
            target = delete.targets[0]
            assert isinstance(target, ast.Name)
            assert target.id == expected_deleted
            assert isinstance(target.ctx, ast.Del)
        assert len(body) == 1
        raise_statement = body[0]
        assert isinstance(raise_statement, ast.Raise)
        assert isinstance(raise_statement.exc, ast.Call)
        assert isinstance(raise_statement.exc.func, ast.Name)
        assert raise_statement.exc.func.id == "TypeError"
        assert raise_statement.exc.keywords == []
        assert len(raise_statement.exc.args) == 1
        message = raise_statement.exc.args[0]
        assert isinstance(message, ast.Constant)
        assert message.value == _MOVE_ONLY_MESSAGE
        assert isinstance(raise_statement.cause, ast.Constant)
        assert raise_statement.cause.value is None

    typing_imports = [
        node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "typing"
    ]
    assert len(typing_imports) == 1
    assert [(alias.name, alias.asname) for alias in typing_imports[0].names] == [
        ("Final", None),
        ("Literal", None),
        ("NoReturn", None),
    ]
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {
            "copy",
            "copyreg",
            "multiprocessing",
            "pickle",
            "subprocess",
            "threading",
        }
    )
