"""Bounded retry policy for sidecar owner election contention."""

from __future__ import annotations

import math
import time
from typing import Final, NoReturn, cast

from .health import MAX_HEALTH_PROBE_TIMEOUT_SECONDS
from .owner_lock import OwnerLock, OwnerLockError, OwnerLockErrorCode
from .startup_election import (
    OwnerElectionError,
    OwnerElectionErrorCode,
    resolve_owner_election,
)
from .state import SidecarState, StateStore

_ATTACH_POLL_SECONDS: Final = 0.025

_STATE_STORE_TYPE: Final = StateStore
_SIDECAR_STATE_TYPE: Final = SidecarState
_OWNER_LOCK_TYPE: Final = OwnerLock
_OWNER_LOCK_ERROR_TYPE: Final = OwnerLockError
_OWNER_LOCK_ERROR_CODE_TYPE: Final = OwnerLockErrorCode
_OWNER_ELECTION_ERROR_TYPE: Final = OwnerElectionError
_OWNER_ELECTION_ERROR_CODE_TYPE: Final = OwnerElectionErrorCode

_RESOLVE_OWNER_ELECTION: Final = resolve_owner_election
_READ_MONOTONIC: Final = time.monotonic
_WAIT: Final = time.sleep


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


def _read_monotonic() -> object:
    return _READ_MONOTONIC()


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


def _resolve_owner_election(store: StateStore, timeout: float) -> object:
    return _RESOLVE_OWNER_ELECTION(store, timeout)


def _wait_interval(interval: float) -> object:
    return _WAIT(interval)


def _new_error(code: OwnerElectionErrorCode) -> OwnerElectionError:
    return OwnerElectionError(code)


def _raise_error(code: OwnerElectionErrorCode) -> NoReturn:
    raise _new_error(code) from None


def wait_for_owner_election(
    store: StateStore,
    timeout: float = 0.5,
) -> SidecarState | OwnerLock:
    """Return one incumbent or owner after bounded contention retries."""

    if type(store) is not _STATE_STORE_TYPE:
        raise TypeError("store must be an exact StateStore")
    normalized_timeout = _validate_timeout(timeout)

    started = _observe_monotonic(None)
    if started is None:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
    deadline = started + normalized_timeout
    if not math.isfinite(deadline) or deadline <= started:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)

    election_timeout = deadline - started
    if not math.isfinite(election_timeout) or election_timeout <= 0.0:
        _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
    admitted_at = started

    while True:
        outcome: object = None
        contention = False
        malformed = False
        try:
            outcome = _resolve_owner_election(store, election_timeout)
        except OwnerLockError as error:
            if type(error) is not _OWNER_LOCK_ERROR_TYPE:
                malformed = True
            else:
                try:
                    owner_error_code = error.code
                except Exception:
                    malformed = True
                else:
                    if type(owner_error_code) is not _OWNER_LOCK_ERROR_CODE_TYPE:
                        malformed = True
                    elif owner_error_code is OwnerLockErrorCode.OWNER_LOCK_HELD:
                        contention = True
                    else:
                        raise
        except OwnerElectionError as error:
            if type(error) is not _OWNER_ELECTION_ERROR_TYPE:
                malformed = True
            else:
                try:
                    election_error_code = error.code
                except Exception:
                    malformed = True
                else:
                    if type(election_error_code) is _OWNER_ELECTION_ERROR_CODE_TYPE:
                        raise
                    malformed = True
        except Exception:
            malformed = True

        if malformed:
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
        if not contention:
            if type(outcome) is _SIDECAR_STATE_TYPE or type(outcome) is _OWNER_LOCK_TYPE:
                return outcome
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)

        wait_window = _remaining(deadline, admitted_at)
        if wait_window is None:
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
        wait_started, wait_remaining = wait_window
        interval = min(_ATTACH_POLL_SECONDS, wait_remaining)

        wait_failed = False
        wait_result: object = None
        try:
            wait_result = _wait_interval(interval)
        except Exception:
            wait_failed = True
        if wait_failed or wait_result is not None:
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_FAILED)

        next_observed = _observe_monotonic(wait_started)
        if next_observed is None:
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
        elapsed = next_observed - wait_started
        if not math.isfinite(elapsed) or elapsed < interval:
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
        next_remaining = deadline - next_observed
        if not math.isfinite(next_remaining) or next_remaining <= 0.0:
            _raise_error(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED)
        admitted_at = next_observed
        election_timeout = next_remaining
