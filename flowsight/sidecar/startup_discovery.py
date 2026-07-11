"""Read-only discovery of one already-published healthy sidecar startup."""

from __future__ import annotations

import math
import time
from typing import Final, cast

from .health import MAX_HEALTH_PROBE_TIMEOUT_SECONDS, probe_sidecar_health
from .state import SidecarState, StateStore

_STATE_STORE_TYPE: Final = StateStore
_SIDECAR_STATE_TYPE: Final = SidecarState
_STATE_STORE_LOAD: Final = StateStore.load
_PROBE_SIDECAR_HEALTH: Final = probe_sidecar_health


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


def _load_state(store: StateStore) -> object:
    return _STATE_STORE_LOAD(store)


def _probe_state(state: SidecarState, timeout: float) -> object:
    return _PROBE_SIDECAR_HEALTH(state, timeout=timeout)


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


def _rebuild_state(value: object) -> tuple[SidecarState, SidecarState] | None:
    if type(value) is not _SIDECAR_STATE_TYPE:
        return None
    state = value
    try:
        rebuilt = _SIDECAR_STATE_TYPE.from_wire(state.to_wire())
        if type(rebuilt) is not _SIDECAR_STATE_TYPE or rebuilt is state or rebuilt != state:
            return None
    except Exception:
        return None
    return state, rebuilt


def discover_existing_startup(
    store: StateStore,
    timeout: float = 0.5,
) -> SidecarState | None:
    """Return the second state only when one incumbent is freshly verified."""

    if type(store) is not _STATE_STORE_TYPE:
        raise TypeError("store must be an exact StateStore")
    normalized_timeout = _validate_timeout(timeout)

    try:
        started = _observe_monotonic(None)
        if started is None:
            return None
        deadline = started + normalized_timeout
        if not math.isfinite(deadline) or deadline <= started:
            return None

        first_pair = _rebuild_state(_load_state(store))
        if first_pair is None:
            return None
        first, first_copy = first_pair

        probe_window = _remaining(deadline, started)
        if probe_window is None:
            return None
        observed, probe_timeout = probe_window
        if _probe_state(first, probe_timeout) is not True:
            return None

        second_load_window = _remaining(deadline, observed)
        if second_load_window is None:
            return None
        observed, _unused = second_load_window
        second_pair = _rebuild_state(_load_state(store))
        if second_pair is None:
            return None
        second, second_copy = second_pair
        if second is first or second != first or second_copy != first_copy:
            return None

        if _remaining(deadline, observed) is None:
            return None
        return second
    except Exception:
        return None
