"""Admit one bounded startup-channel outcome without lifecycle policy."""

from __future__ import annotations

import math
import time
from enum import StrEnum
from typing import Final, NoReturn, cast

from .startup_channel import (
    MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS,
    StartupChannelError,
    StartupFailure,
    StartupReader,
    StartupReady,
)
from .startup_verification import verify_ready_startup
from .state import SidecarState, StateStore

_STATE_STORE_TYPE: Final = StateStore
_STARTUP_READER_TYPE: Final = StartupReader
_STARTUP_READY_TYPE: Final = StartupReady
_STARTUP_FAILURE_TYPE: Final = StartupFailure
_STARTUP_CHANNEL_ERROR_TYPE: Final = StartupChannelError
_SIDECAR_STATE_TYPE: Final = SidecarState

_STARTUP_READER_FILENO: Final = StartupReader.fileno
_STARTUP_READER_RECEIVE: Final = StartupReader.receive
_VERIFY_READY_STARTUP: Final = verify_ready_startup


class StartupAdmissionErrorCode(StrEnum):
    """Stable, non-sensitive startup-admission failure codes."""

    STARTUP_ADMISSION_DEADLINE_FAILED = "STARTUP_ADMISSION_DEADLINE_FAILED"


class StartupAdmissionError(RuntimeError):
    """The pre-transfer admission deadline could not be established."""

    def __init__(self, code: StartupAdmissionErrorCode) -> None:
        if type(code) is not StartupAdmissionErrorCode:
            raise TypeError("code must be an exact StartupAdmissionErrorCode")
        self.code = code
        super().__init__(f"sidecar startup admission failed ({code})")


def _validate_timeout(value: object) -> float:
    if type(value) not in {int, float}:
        raise TypeError("timeout must be a built-in int or float")
    if type(value) is int:
        integer_timeout = value
        if integer_timeout <= 0 or integer_timeout > MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS:
            raise ValueError("timeout must be finite, positive, and at most 30 seconds")
        return float(integer_timeout)

    timeout = cast(float, value)
    if (
        not math.isfinite(timeout)
        or timeout <= 0.0
        or timeout > MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS
    ):
        raise ValueError("timeout must be finite, positive, and at most 30 seconds")
    return timeout


def _reader_fileno(
    reader: StartupReader,
) -> tuple[object | None, StartupChannelError | None]:
    try:
        return _STARTUP_READER_FILENO(reader), None
    except StartupChannelError as error:
        if type(error) is _STARTUP_CHANNEL_ERROR_TYPE:
            return None, error
        raise


def _reader_receive(
    reader: StartupReader,
    timeout: float,
) -> tuple[object | None, StartupChannelError | None]:
    try:
        return _STARTUP_READER_RECEIVE(reader, timeout), None
    except StartupChannelError as error:
        if type(error) is _STARTUP_CHANNEL_ERROR_TYPE:
            return None, error
        raise


def _verify_ready(
    store: StateStore,
    ready: StartupReady,
    timeout: float,
) -> object:
    return _VERIFY_READY_STARTUP(store, ready, timeout)


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


def _prepare_receive_window(timeout: float) -> tuple[float, float, float] | None:
    try:
        started = _observe_monotonic(None)
        if started is None:
            return None
        deadline = started + timeout
        if not math.isfinite(deadline) or deadline <= started:
            return None
        receive_window = _remaining(deadline, started)
        if receive_window is None:
            return None
    except Exception:
        return None
    observed, receive_timeout = receive_window
    return deadline, observed, receive_timeout


def _raise_deadline_failure() -> NoReturn:
    raise StartupAdmissionError(
        StartupAdmissionErrorCode.STARTUP_ADMISSION_DEADLINE_FAILED
    ) from None


def _preflight_reader(reader: StartupReader) -> None:
    channel_error: StartupChannelError | None = None
    descriptor: object | None = None
    malformed = False
    try:
        descriptor, channel_error = _reader_fileno(reader)
    except Exception:
        malformed = True
    if channel_error is not None:
        if type(channel_error) is _STARTUP_CHANNEL_ERROR_TYPE:
            raise channel_error
        malformed = True
    if malformed or type(descriptor) is not int or descriptor < 3:
        raise ValueError("reader is invalid") from None


def _rebuild_failure(value: object) -> StartupFailure | None:
    if type(value) is not _STARTUP_FAILURE_TYPE:
        return None
    failure = value
    try:
        rebuilt = _STARTUP_FAILURE_TYPE(code=failure.code)
        if type(rebuilt) is not _STARTUP_FAILURE_TYPE or rebuilt is failure or rebuilt != failure:
            return None
    except Exception:
        return None
    return failure


def _rebuild_ready(value: object) -> StartupReady | None:
    if type(value) is not _STARTUP_READY_TYPE:
        return None
    ready = value
    try:
        rebuilt = _STARTUP_READY_TYPE(
            startup_id=ready.startup_id,
            sidecar_pid=ready.sidecar_pid,
            port=ready.port,
        )
        if type(rebuilt) is not _STARTUP_READY_TYPE or rebuilt is ready or rebuilt != ready:
            return None
    except Exception:
        return None
    return ready


def _rebuild_state(value: object) -> SidecarState | None:
    if type(value) is not _SIDECAR_STATE_TYPE:
        return None
    state = value
    try:
        rebuilt = _SIDECAR_STATE_TYPE.from_wire(state.to_wire())
        if type(rebuilt) is not _SIDECAR_STATE_TYPE or rebuilt is state or rebuilt != state:
            return None
    except Exception:
        return None
    return state


def receive_startup_outcome(
    store: StateStore,
    reader: StartupReader,
    timeout: float = 0.5,
) -> SidecarState | StartupFailure | None:
    """Consume one startup signal and admit only fresh READY evidence."""

    if type(store) is not _STATE_STORE_TYPE:
        raise TypeError("store must be an exact StateStore")
    if type(reader) is not _STARTUP_READER_TYPE:
        raise TypeError("reader must be an exact StartupReader")
    normalized_timeout = _validate_timeout(timeout)
    _preflight_reader(reader)

    receive_window = _prepare_receive_window(normalized_timeout)
    if receive_window is None:
        _raise_deadline_failure()
    deadline, observed, receive_timeout = receive_window

    message: object | None = None
    channel_error: StartupChannelError | None = None
    try:
        message, channel_error = _reader_receive(reader, receive_timeout)
    except Exception:
        return None
    if channel_error is not None:
        if type(channel_error) is _STARTUP_CHANNEL_ERROR_TYPE:
            raise channel_error
        return None

    try:
        failure = _rebuild_failure(message)
    except Exception:
        return None
    if failure is not None:
        return failure

    try:
        ready = _rebuild_ready(message)
    except Exception:
        return None
    if ready is None:
        return None

    try:
        verification_window = _remaining(deadline, observed)
    except Exception:
        return None
    if verification_window is None:
        return None
    observed, verification_timeout = verification_window
    try:
        candidate = _verify_ready(store, ready, verification_timeout)
    except Exception:
        return None
    try:
        state = _rebuild_state(candidate)
    except Exception:
        return None
    if state is None:
        return None

    try:
        final_window = _remaining(deadline, observed)
    except Exception:
        return None
    if final_window is None:
        return None
    return state
