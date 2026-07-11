"""Long-lived project owner-lock descriptor for the local sidecar."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
from enum import StrEnum
from types import TracebackType
from typing import Final, Literal

from .state import StateStorageError, StateStore, _OwnerLockFileCleanupError

_LOCK_OPERATION: Final = fcntl.LOCK_EX | fcntl.LOCK_NB
_CONTINUITY_PROBE: Final = fcntl.LOCK_SH | fcntl.LOCK_NB
_MIN_INHERITED_DESCRIPTOR: Final = 3
_CONTENTION_ERRNOS: Final = frozenset(
    {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}
)


class OwnerLockErrorCode(StrEnum):
    """Stable, non-sensitive owner-lock failure codes."""

    OWNER_LOCK_HELD = "OWNER_LOCK_HELD"
    OWNER_LOCK_STORAGE_FAILED = "OWNER_LOCK_STORAGE_FAILED"
    INHERITED_OWNER_LOCK_INVALID = "INHERITED_OWNER_LOCK_INVALID"
    OWNER_LOCK_CLEANUP_FAILED = "OWNER_LOCK_CLEANUP_FAILED"
    OWNER_LOCK_CLOSED = "OWNER_LOCK_CLOSED"


class OwnerLockError(RuntimeError):
    """The owner-lock descriptor could not satisfy its fixed contract."""

    def __init__(self, code: OwnerLockErrorCode) -> None:
        self.code = code
        super().__init__(f"sidecar owner lock failed ({code})")


class _OwnerLockFailure(Exception):
    def __init__(self, code: OwnerLockErrorCode) -> None:
        self.code = code
        super().__init__()


def _public_failure(code: OwnerLockErrorCode) -> OwnerLockError:
    return OwnerLockError(code)


def _is_private_owner_file(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
        and metadata.st_size == 0
    )


def _descriptor_is_exact_owner_file(file_descriptor: int) -> bool:
    try:
        metadata = os.fstat(file_descriptor)
        flags = fcntl.fcntl(file_descriptor, fcntl.F_GETFL)
    except (OSError, NotImplementedError, ValueError):
        return False
    return _is_private_owner_file(metadata) and flags & os.O_ACCMODE == os.O_RDWR


def _same_inode(first_descriptor: int, second_descriptor: int) -> bool:
    try:
        first = os.fstat(first_descriptor)
        second = os.fstat(second_descriptor)
    except (OSError, NotImplementedError):
        return False
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _flock_status(file_descriptor: int, operation: int) -> str:
    try:
        fcntl.flock(file_descriptor, operation)
    except OSError as error:
        error_number = error.errno
        if error_number in _CONTENTION_ERRNOS:
            return "blocked"
        return "error"
    return "ok"


def _try_close(file_descriptor: int) -> bool:
    if file_descriptor < 0:
        return True
    try:
        os.close(file_descriptor)
    except (OSError, NotImplementedError):
        return False
    return True


def _finish_descriptors(
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
            active_error.add_note("sidecar owner lock cleanup failed")
        return
    if cleanup_control is not None:
        raise cleanup_control
    if cleanup_failed:
        raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED) from None


def _require_store(store: object) -> StateStore:
    if type(store) is not StateStore:
        raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED)
    return store


def _open_store_descriptor(store: StateStore, *, create: bool) -> int:
    return store._open_owner_lock_file(create=create)


def _acquire(store: object) -> OwnerLock:
    trusted_store = _require_store(store)
    owner_descriptor = -1
    verification_descriptor = -1
    try:
        owner_descriptor = _open_store_descriptor(trusted_store, create=True)
        if owner_descriptor < _MIN_INHERITED_DESCRIPTOR:
            promoted_descriptor = fcntl.fcntl(
                owner_descriptor,
                fcntl.F_DUPFD_CLOEXEC,
                _MIN_INHERITED_DESCRIPTOR,
            )
            descriptor_to_close = owner_descriptor
            owner_descriptor = promoted_descriptor
            if not _try_close(descriptor_to_close):
                raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)
        if not _descriptor_is_exact_owner_file(owner_descriptor):
            raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED)

        lock_status = _flock_status(owner_descriptor, _LOCK_OPERATION)
        if lock_status == "blocked":
            raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_HELD)
        if lock_status != "ok":
            raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED)

        verification_descriptor = _open_store_descriptor(trusted_store, create=False)
        if not _same_inode(owner_descriptor, verification_descriptor):
            raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED)
        os.set_inheritable(owner_descriptor, False)

        descriptor_to_close = verification_descriptor
        verification_descriptor = -1
        if not _try_close(descriptor_to_close):
            raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)

        result = OwnerLock._from_descriptor(owner_descriptor)
        owner_descriptor = -1
        return result
    except (_OwnerLockFailure, StateStorageError):
        raise
    except (OSError, NotImplementedError, ValueError):
        raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED) from None
    finally:
        _finish_descriptors(
            sys.exception(),
            verification_descriptor,
            owner_descriptor,
        )


def _adopt(store: object, inherited_descriptor: object) -> OwnerLock:
    if type(inherited_descriptor) is not int or inherited_descriptor < _MIN_INHERITED_DESCRIPTOR:
        raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID)

    candidate_descriptor = inherited_descriptor
    probe_descriptor = -1
    final_descriptor = -1
    try:
        trusted_store = _require_store(store)
        try:
            os.fstat(candidate_descriptor)
        except OSError as error:
            if error.errno == errno.EBADF:
                candidate_descriptor = -1
            raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID) from None

        if not _descriptor_is_exact_owner_file(candidate_descriptor):
            raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID)

        probe_descriptor = _open_store_descriptor(trusted_store, create=False)
        if not _same_inode(candidate_descriptor, probe_descriptor):
            raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID)

        # A shared probe must be blocked to prove that the inherited open-file
        # description already owns an exclusive lock. An exclusive reassertion
        # on the candidate then proves that the blocker is that same description.
        if _flock_status(probe_descriptor, _CONTINUITY_PROBE) != "blocked":
            raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID)
        candidate_status = _flock_status(candidate_descriptor, _LOCK_OPERATION)
        if candidate_status == "blocked":
            raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID)
        if candidate_status != "ok":
            raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED)

        final_descriptor = _open_store_descriptor(trusted_store, create=False)
        if not _descriptor_is_exact_owner_file(candidate_descriptor) or not _same_inode(
            candidate_descriptor, final_descriptor
        ):
            raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID)
        os.set_inheritable(candidate_descriptor, False)

        for descriptor_name in ("final", "probe"):
            descriptor_to_close = (
                final_descriptor if descriptor_name == "final" else probe_descriptor
            )
            if descriptor_name == "final":
                final_descriptor = -1
            else:
                probe_descriptor = -1
            if not _try_close(descriptor_to_close):
                raise _OwnerLockFailure(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)

        result = OwnerLock._from_descriptor(candidate_descriptor)
        candidate_descriptor = -1
        return result
    except (_OwnerLockFailure, StateStorageError):
        raise
    except (OSError, NotImplementedError, ValueError):
        raise _OwnerLockFailure(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID) from None
    finally:
        _finish_descriptors(
            sys.exception(),
            final_descriptor,
            probe_descriptor,
            candidate_descriptor,
        )


class OwnerLock:
    """One long-lived exclusive lock held solely by an open descriptor."""

    __slots__ = ("_descriptor",)
    _descriptor: int

    def __init__(self) -> None:
        raise TypeError("OwnerLock must be acquired or adopted")

    @classmethod
    def _from_descriptor(cls, file_descriptor: int) -> OwnerLock:
        result = object.__new__(cls)
        result._descriptor = file_descriptor
        return result

    @classmethod
    def acquire(cls, store: StateStore) -> OwnerLock:
        """Acquire the exact project owner lock once without waiting."""

        failure_code: OwnerLockErrorCode | None = None
        try:
            return _acquire(store)
        except _OwnerLockFileCleanupError:
            failure_code = OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED
        except StateStorageError:
            failure_code = OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED
        except _OwnerLockFailure as failure:
            failure_code = failure.code
        raise _public_failure(failure_code) from None

    @classmethod
    def adopt_inherited(cls, store: StateStore, file_descriptor: int) -> OwnerLock:
        """Consume and strictly claim one already-locked inherited descriptor."""

        failure_code: OwnerLockErrorCode | None = None
        try:
            return _adopt(store, file_descriptor)
        except _OwnerLockFileCleanupError:
            failure_code = OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED
        except StateStorageError:
            failure_code = OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID
        except _OwnerLockFailure as failure:
            failure_code = failure.code
        raise _public_failure(failure_code) from None

    def __repr__(self) -> str:
        state = "active" if self._descriptor >= 0 else "closed"
        return f"<OwnerLock {state}>"

    def fileno(self) -> int:
        if self._descriptor < 0:
            raise _public_failure(OwnerLockErrorCode.OWNER_LOCK_CLOSED) from None
        return self._descriptor

    def close(self) -> None:
        file_descriptor = self._descriptor
        if file_descriptor < 0:
            return
        self._descriptor = -1
        cleanup_failed = not _try_close(file_descriptor)
        if cleanup_failed:
            raise _public_failure(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED) from None

    def __enter__(self) -> OwnerLock:
        if self._descriptor < 0:
            raise _public_failure(OwnerLockErrorCode.OWNER_LOCK_CLOSED) from None
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exception_type, traceback
        file_descriptor = self._descriptor
        if file_descriptor < 0:
            return False
        self._descriptor = -1
        cleanup_failed = not _try_close(file_descriptor)
        if cleanup_failed:
            if exception is not None:
                exception.add_note("sidecar owner lock cleanup failed")
                return False
            raise _public_failure(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED) from None
        return False
