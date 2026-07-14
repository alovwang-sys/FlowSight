"""One-shot composition of sidecar discovery and owner election."""

from __future__ import annotations

import math
import time
from enum import StrEnum
from typing import Final, Literal, NoReturn, cast

from .health import MAX_HEALTH_PROBE_TIMEOUT_SECONDS
from .owner_lock import OwnerLock, OwnerLockError
from .startup_discovery import discover_existing_startup
from .state import SidecarState, StateStore

_STATE_STORE_TYPE: Final = StateStore
_SIDECAR_STATE_TYPE: Final = SidecarState
_OWNER_LOCK_TYPE: Final = OwnerLock
_OWNER_LOCK_ERROR_TYPE: Final = OwnerLockError

_DISCOVER_EXISTING_STARTUP: Final = discover_existing_startup
_OWNER_LOCK_ACQUIRE: Final = OwnerLock.acquire
_OWNER_LOCK_EXIT: Final = OwnerLock.__exit__

_OWNER_CLEANUP_NOTE: Final = "sidecar owner lock cleanup failed"


class OwnerElectionErrorCode(StrEnum):
    """Stable, non-sensitive owner-election failure codes."""

    OWNER_ELECTION_DEADLINE_FAILED = "OWNER_ELECTION_DEADLINE_FAILED"
    OWNER_ELECTION_FAILED = "OWNER_ELECTION_FAILED"


class OwnerElectionError(RuntimeError):
    """One owner-election decision could not produce a trusted result."""

    def __init__(self, code: OwnerElectionErrorCode) -> None:
        if type(code) is not OwnerElectionErrorCode:
            raise TypeError("code must be an exact OwnerElectionErrorCode")
        self.code = code
        super().__init__(f"sidecar owner election failed ({code})")


def _validate_timeout(value: object) -> float:
    if type(value) not in {int, float}:
        raise TypeError("timeout must be a built-in int or float")
    if type(value) is int:
        integer_timeout = value
        if integer_timeout <= 0 or integer_timeout > MAX_HEALTH_PROBE_TIMEOUT_SECONDS:
            raise ValueError("timeout must be finite, positive, and at most 30 seconds")
        return float(integer_timeout)

    timeout = cast(float, value)
    if not math.isfinite(timeout) or timeout <= 0.0 or timeout > MAX_HEALTH_PROBE_TIMEOUT_SECONDS:
        raise ValueError("timeout must be finite, positive, and at most 30 seconds")
    return timeout


def _discover_startup(store: StateStore, timeout: float) -> object:
    return _DISCOVER_EXISTING_STARTUP(store, timeout)


def _acquire_owner(store: StateStore) -> object:
    return _OWNER_LOCK_ACQUIRE(store)


def _exit_owner(
    owner: OwnerLock,
    exception_type: type[BaseException] | None,
    exception: BaseException | None,
) -> object:
    return _OWNER_LOCK_EXIT(owner, exception_type, exception, None)


def _read_monotonic() -> object:
    return time.monotonic()


def _observe_monotonic(previous: float | None) -> float | None:
    try:
        observed = _read_monotonic()
    except Exception:
        return None
    if type(observed) is not float or not math.isfinite(observed):
        return None
    if previous is not None and observed < previous:
        return None
    return observed


def _remaining(deadline: float, previous: float) -> tuple[float, float] | None:
    observed = _observe_monotonic(previous)
    if observed is None:
        return None
    remaining = deadline - observed
    if not math.isfinite(remaining) or remaining <= 0.0:
        return None
    return observed, remaining


def _new_error(code: OwnerElectionErrorCode) -> OwnerElectionError:
    return OwnerElectionError(code)


def _raise_error(code: OwnerElectionErrorCode) -> NoReturn:
    raise _new_error(code) from None


def _attempt_discovery(store: StateStore, timeout: float) -> tuple[bool, object]:
    try:
        return True, _discover_startup(store, timeout)
    except Exception:
        return False, None


_ReleaseStatus = Literal["released", "owner_error", "failed"]


def _attempt_release(
    owner: OwnerLock,
    active_error: BaseException | None,
) -> tuple[_ReleaseStatus, OwnerLockError | None]:
    exception_type = None if active_error is None else type(active_error)
    try:
        result = _exit_owner(owner, exception_type, active_error)
    except OwnerLockError as error:
        if type(error) is _OWNER_LOCK_ERROR_TYPE:
            return "owner_error", error
        return "failed", None
    except Exception:
        return "failed", None
    if result is not False:
        return "failed", None
    return "released", None


def _release_normal(owner: OwnerLock) -> None:
    status, owner_error = _attempt_release(owner, None)
    if status == "released":
        return
    if status == "owner_error" and owner_error is not None:
        raise owner_error from None
    _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)


def _release_pending(owner: OwnerLock, pending_error: OwnerElectionError) -> None:
    status, owner_error = _attempt_release(owner, pending_error)
    if status == "released":
        return
    if status == "owner_error" and owner_error is not None:
        raise owner_error from None
    _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)


def _release_process_control(owner: OwnerLock, active_error: BaseException) -> None:
    status, _owner_error = _attempt_release(owner, active_error)
    if status == "released":
        return
    try:
        BaseException.add_note(active_error, _OWNER_CLEANUP_NOTE)
    except Exception:
        try:
            BaseException.__setattr__(
                active_error,
                "__notes__",
                [_OWNER_CLEANUP_NOTE],
            )
        except Exception:
            return


def resolve_owner_election(
    store: StateStore,
    timeout: float = 0.5,
) -> SidecarState | OwnerLock:
    """Return one incumbent or transfer one nonblocking owner-lock winner."""

    if type(store) is not _STATE_STORE_TYPE:
        raise TypeError("store must be an exact StateStore")
    normalized_timeout = _validate_timeout(timeout)

    started = _observe_monotonic(None)
    if started is None:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
    deadline = started + normalized_timeout
    if not math.isfinite(deadline) or deadline <= started:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)

    discovery_window = _remaining(deadline, started)
    if discovery_window is None:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
    observed, discovery_timeout = discovery_window
    discovery_ok, incumbent = _attempt_discovery(store, discovery_timeout)
    if not discovery_ok:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    if type(incumbent) is _SIDECAR_STATE_TYPE:
        if _remaining(deadline, observed) is None:
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
        return incumbent
    if incumbent is not None:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)

    acquire_window = _remaining(deadline, observed)
    if acquire_window is None:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
    observed, _unused = acquire_window

    candidate: object = None
    acquire_failed = False
    pending_error: OwnerElectionError | None = None
    active_error: BaseException | None = None
    post_lock_state: SidecarState | None = None
    result: OwnerLock | None = None
    try:
        try:
            candidate = _acquire_owner(store)
        except OwnerLockError as error:
            if type(error) is _OWNER_LOCK_ERROR_TYPE:
                raise
            acquire_failed = True
        except Exception:
            acquire_failed = True

        if acquire_failed or type(candidate) is not _OWNER_LOCK_TYPE:
            pending_error = _new_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
        else:
            owner = candidate
            post_lock_window = _remaining(deadline, observed)
            if post_lock_window is None:
                pending_error = _new_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
            else:
                observed, post_lock_timeout = post_lock_window
                discovery_ok, post_lock_result = _attempt_discovery(store, post_lock_timeout)
                if not discovery_ok:
                    pending_error = _new_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
                elif post_lock_result is None:
                    if _remaining(deadline, observed) is None:
                        pending_error = _new_error(
                            OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED
                        )
                    else:
                        result = owner
                        return owner
                elif type(post_lock_result) is _SIDECAR_STATE_TYPE:
                    release_window = _remaining(deadline, observed)
                    if release_window is None:
                        pending_error = _new_error(
                            OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED
                        )
                    else:
                        observed, _unused = release_window
                        post_lock_state = post_lock_result
                else:
                    pending_error = _new_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    except OwnerElectionError as error:
        pending_error = error
    except OwnerLockError:
        if candidate is None:
            raise
        pending_error = _new_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    except Exception:
        pending_error = _new_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    except BaseException as error:
        active_error = error
        raise
    finally:
        if type(candidate) is _OWNER_LOCK_TYPE:
            owner = candidate
            if active_error is not None:
                _release_process_control(owner, active_error)
            elif result is owner:
                pass
            elif pending_error is not None:
                _release_pending(owner, pending_error)
            elif type(post_lock_state) is _SIDECAR_STATE_TYPE:
                _release_normal(owner)
            else:
                pending_error = _new_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
                _release_pending(owner, pending_error)

    if pending_error is not None:
        raise pending_error from None
    if type(post_lock_state) is not _SIDECAR_STATE_TYPE:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    if _remaining(deadline, observed) is None:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
    return post_lock_state
