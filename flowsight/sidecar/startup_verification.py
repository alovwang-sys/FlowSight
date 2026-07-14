"""One read-only verification window for an exact sidecar READY hint."""

from __future__ import annotations

import math
import time
from typing import Final, NoReturn, cast

from .health import MAX_HEALTH_PROBE_TIMEOUT_SECONDS, probe_sidecar_health
from .startup_channel import StartupReady
from .state import SidecarState, StateStore

_STATE_STORE_TYPE: Final = StateStore
_STARTUP_READY_TYPE: Final = StartupReady
_SIDECAR_STATE_TYPE: Final = SidecarState
_STATE_STORE_LOAD: Final = StateStore.load


class _ReadyVerificationFailure(Exception):
    pass


def _reject() -> NoReturn:
    raise _ReadyVerificationFailure


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


def _rebuild_ready(ready: StartupReady) -> StartupReady:
    rebuilt: StartupReady | None = None
    try:
        rebuilt = _STARTUP_READY_TYPE(
            startup_id=ready.startup_id,
            sidecar_pid=ready.sidecar_pid,
            port=ready.port,
        )
    except Exception:
        pass
    if type(rebuilt) is not _STARTUP_READY_TYPE or rebuilt is ready or rebuilt != ready:
        raise ValueError("ready is invalid") from None
    return rebuilt


def _load_state(store: StateStore) -> object:
    """Invoke the frozen canonical loader without instance method lookup."""

    return _STATE_STORE_LOAD(store)


def _probe_state(state: SidecarState, timeout: float) -> object:
    return probe_sidecar_health(state, timeout=timeout)


def _read_monotonic() -> float:
    observed = time.monotonic()
    if type(observed) is not float or not math.isfinite(observed):
        _reject()
    return observed


def _remaining(deadline: float, previous: float) -> tuple[float, float]:
    observed = _read_monotonic()
    if observed < previous:
        _reject()
    remaining = deadline - observed
    if not math.isfinite(remaining) or remaining <= 0.0:
        _reject()
    return observed, remaining


def _revalidate_loaded(state: SidecarState) -> SidecarState:
    revalidated = _SIDECAR_STATE_TYPE.from_wire(state.to_wire())
    if type(revalidated) is not _SIDECAR_STATE_TYPE or revalidated is state or revalidated != state:
        _reject()
    return revalidated


def verify_ready_startup(
    store: StateStore,
    ready: StartupReady,
    timeout: float = 0.5,
) -> SidecarState | None:
    """Return the second matching state admitted before ``timeout`` expires.

    ``timeout`` bounds successful admission and the delegated health request;
    it cannot interrupt either synchronous state load.
    """

    if type(store) is not _STATE_STORE_TYPE:
        raise TypeError("store must be an exact StateStore")
    if type(ready) is not _STARTUP_READY_TYPE:
        raise TypeError("ready must be an exact StartupReady")
    normalized_timeout = _validate_timeout(timeout)
    trusted_ready = _rebuild_ready(ready)

    verified: SidecarState | None = None
    try:
        started = _read_monotonic()
        deadline = started + normalized_timeout
        if not math.isfinite(deadline) or deadline <= started:
            _reject()

        raw_first = _load_state(store)
        if type(raw_first) is not _SIDECAR_STATE_TYPE:
            _reject()
        first_loaded = raw_first
        first_copy = _revalidate_loaded(first_loaded)
        if (
            first_loaded.startup_id != trusted_ready.startup_id
            or first_loaded.pid != trusted_ready.sidecar_pid
            or first_loaded.port != trusted_ready.port
        ):
            _reject()

        observed, probe_timeout = _remaining(deadline, started)
        healthy = _probe_state(first_loaded, probe_timeout)
        if healthy is not True:
            _reject()

        observed, _unused = _remaining(deadline, observed)
        raw_second = _load_state(store)
        if type(raw_second) is not _SIDECAR_STATE_TYPE:
            _reject()
        second_loaded = raw_second
        if second_loaded is first_loaded:
            _reject()
        second_copy = _revalidate_loaded(second_loaded)
        if second_loaded != first_loaded or second_copy != first_copy:
            _reject()

        _observed, _unused = _remaining(deadline, observed)
        verified = second_loaded
    except Exception:
        pass
    return verified
