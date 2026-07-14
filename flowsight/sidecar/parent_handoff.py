"""Private EOF-only child gate for parent process-ownership handoff."""

from __future__ import annotations

import fcntl
import math
import os
import select
import stat
import time
from typing import Final, NoReturn

from .child_bootstrap import SidecarChildBootstrap
from .runtime_config import SidecarRuntimeConfig

_BOOTSTRAP_TYPE: Final = SidecarChildBootstrap
_CONFIG_TYPE: Final = SidecarRuntimeConfig
_MIN_DESCRIPTOR: Final = 3
_MAX_DESCRIPTOR: Final = 2_147_483_647
_MAX_TIMEOUT_SECONDS: Final = 30.0
_CLEANUP_NOTE: Final = "sidecar parent handoff cleanup failed"

_OBJECT_GETATTRIBUTE: Final = object.__getattribute__
_STAT_RESULT_TYPE: Final = os.stat_result
_FSTAT: Final = os.fstat
_FCNTL: Final = fcntl.fcntl
_F_GETFL: Final = fcntl.F_GETFL
_F_SETFL: Final = fcntl.F_SETFL
_FPATHCONF: Final = os.fpathconf
_POLL: Final = select.poll
_READ: Final = os.read
_CLOSE: Final = os.close
_READ_MONOTONIC: Final = time.monotonic
_IS_FIFO: Final = stat.S_ISFIFO
_ISFINITE: Final = math.isfinite
_CEIL: Final = math.ceil
_ADD_NOTE: Final = BaseException.add_note
_O_ACCMODE: Final = os.O_ACCMODE
_O_RDONLY: Final = os.O_RDONLY
_O_NONBLOCK: Final = os.O_NONBLOCK
_POLLIN: Final = select.POLLIN
_POLLHUP: Final = select.POLLHUP
_POLLERR: Final = select.POLLERR
_POLLNVAL: Final = select.POLLNVAL


class _HandoffFailure(Exception):
    pass


def _read_bootstrap_slots(
    bootstrap: SidecarChildBootstrap,
) -> tuple[SidecarRuntimeConfig, int]:
    config: object = None
    owner_descriptor: object = None
    writer_descriptor: object = None
    handoff_descriptor: object = None
    try:
        if type(bootstrap) is not _BOOTSTRAP_TYPE:
            raise _HandoffFailure
        config = _OBJECT_GETATTRIBUTE(bootstrap, "config")
        owner_descriptor = _OBJECT_GETATTRIBUTE(bootstrap, "owner_lock_fd")
        writer_descriptor = _OBJECT_GETATTRIBUTE(bootstrap, "startup_writer_fd")
        handoff_descriptor = _OBJECT_GETATTRIBUTE(bootstrap, "parent_handoff_fd")
        if (
            type(config) is not _CONFIG_TYPE
            or type(owner_descriptor) is not int
            or type(writer_descriptor) is not int
            or type(handoff_descriptor) is not int
            or not _MIN_DESCRIPTOR <= owner_descriptor <= _MAX_DESCRIPTOR
            or not _MIN_DESCRIPTOR <= writer_descriptor <= _MAX_DESCRIPTOR
            or not _MIN_DESCRIPTOR <= handoff_descriptor <= _MAX_DESCRIPTOR
            or owner_descriptor == writer_descriptor
            or owner_descriptor == handoff_descriptor
            or writer_descriptor == handoff_descriptor
        ):
            raise _HandoffFailure
        return config, handoff_descriptor
    except BaseException:
        del bootstrap, config, owner_descriptor, writer_descriptor, handoff_descriptor
        raise


def _read_timeout(config: SidecarRuntimeConfig) -> float:
    timeout: object = None
    try:
        timeout = _OBJECT_GETATTRIBUTE(config, "startup_timeout")
        if (
            type(timeout) is not float
            or not _ISFINITE(timeout)
            or not 0.0 < timeout <= _MAX_TIMEOUT_SECONDS
        ):
            raise _HandoffFailure
        return timeout
    except BaseException:
        del config, timeout
        raise


def _remaining(deadline: float, previous: float) -> tuple[float, float] | None:
    observed = _READ_MONOTONIC()
    if type(observed) is not float or not _ISFINITE(observed) or observed < previous:
        return None
    remaining = deadline - observed
    if not _ISFINITE(remaining) or remaining <= 0.0:
        return None
    return observed, remaining


def _is_read_only_pipe(descriptor: int) -> bool:
    metadata: object = None
    mode: object = None
    flags: object = None
    pipe_buffer: object = None
    set_result: object = None
    verified_flags: object = None
    try:
        metadata = _FSTAT(descriptor)
        if type(metadata) is not _STAT_RESULT_TYPE:
            return False
        mode = metadata.st_mode
        flags = _FCNTL(descriptor, _F_GETFL)
        pipe_buffer = _FPATHCONF(descriptor, "PC_PIPE_BUF")
        if (
            type(mode) is not int
            or type(flags) is not int
            or type(pipe_buffer) is not int
            or pipe_buffer <= 0
            or _IS_FIFO(mode) is not True
            or flags & _O_ACCMODE != _O_RDONLY
        ):
            return False
        if flags & _O_NONBLOCK == 0:
            set_result = _FCNTL(descriptor, _F_SETFL, flags | _O_NONBLOCK)
            if type(set_result) is not int or set_result != 0:
                return False
            verified_flags = _FCNTL(descriptor, _F_GETFL)
            if type(verified_flags) is not int or verified_flags & _O_NONBLOCK == 0:
                return False
        return True
    except BaseException:
        del descriptor, metadata, mode, flags, pipe_buffer, set_result, verified_flags
        raise


def _await_eof(descriptor: int, timeout: float) -> None:
    started: object = None
    deadline: float | None = None
    window: tuple[float, float] | None = None
    observed: float | None = None
    remaining: float | None = None
    poller = None
    event_mask: int | None = None
    timeout_milliseconds: int | None = None
    events: object = None
    event: object = None
    reported_descriptor: object = None
    reported_events: object = None
    payload: object = None
    try:
        started = _READ_MONOTONIC()
        if type(started) is not float or not _ISFINITE(started):
            raise _HandoffFailure
        deadline = started + timeout
        if not _ISFINITE(deadline) or deadline <= started:
            raise _HandoffFailure
        window = _remaining(deadline, started)
        if window is None:
            raise _HandoffFailure
        observed, remaining = window
        timeout_milliseconds = _CEIL(remaining * 1000.0)
        if type(timeout_milliseconds) is not int or timeout_milliseconds <= 0:
            raise _HandoffFailure
        event_mask = _POLLIN | _POLLHUP | _POLLERR | _POLLNVAL
        poller = _POLL()
        poller.register(descriptor, event_mask)
        events = poller.poll(timeout_milliseconds)
        if (
            type(events) is not list
            or len(events) != 1
            or type(events[0]) is not tuple
            or len(events[0]) != 2
        ):
            raise _HandoffFailure
        event = events[0]
        reported_descriptor, reported_events = event
        if (
            type(reported_descriptor) is not int
            or reported_descriptor != descriptor
            or type(reported_events) is not int
            or reported_events & (_POLLERR | _POLLNVAL) != 0
            or reported_events & (_POLLIN | _POLLHUP) == 0
        ):
            raise _HandoffFailure
        window = _remaining(deadline, observed)
        if window is None:
            raise _HandoffFailure
        observed, remaining = window
        payload = _READ(descriptor, 1)
        if type(payload) is not bytes or payload != b"":
            raise _HandoffFailure
        if _remaining(deadline, observed) is None:
            raise _HandoffFailure
    except BaseException:
        del (
            descriptor,
            timeout,
            started,
            deadline,
            window,
            observed,
            remaining,
            poller,
            event_mask,
            timeout_milliseconds,
            events,
            event,
            reported_descriptor,
            reported_events,
            payload,
        )
        raise
    del (
        descriptor,
        timeout,
        started,
        deadline,
        window,
        observed,
        remaining,
        poller,
        event_mask,
        timeout_milliseconds,
        events,
        event,
        reported_descriptor,
        reported_events,
        payload,
    )


def _await_handoff(bootstrap: SidecarChildBootstrap) -> None:
    descriptor = -1
    config: SidecarRuntimeConfig | None = None
    timeout: float | None = None
    active_control: BaseException | None = None
    ordinary_failure = False
    cleanup_failed = False
    cleanup_control: BaseException | None = None
    close_result: object = None
    try:
        config, descriptor = _read_bootstrap_slots(bootstrap)
        timeout = _read_timeout(config)
        if descriptor < _MIN_DESCRIPTOR or _is_read_only_pipe(descriptor) is not True:
            raise _HandoffFailure
        _await_eof(descriptor, timeout)
    except Exception:
        ordinary_failure = True
    except BaseException as error:
        active_control = error
        raise
    finally:
        if descriptor >= _MIN_DESCRIPTOR:
            try:
                close_result = _CLOSE(descriptor)
                if close_result is not None:
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            except BaseException as error:
                cleanup_failed = True
                cleanup_control = error
        if active_control is not None:
            if cleanup_failed:
                try:
                    _ADD_NOTE(active_control, _CLEANUP_NOTE)
                except BaseException:
                    pass
            del active_control
        elif cleanup_control is not None:
            control = cleanup_control
            try:
                _ADD_NOTE(control, _CLEANUP_NOTE)
            except BaseException:
                pass
            del (
                bootstrap,
                descriptor,
                config,
                timeout,
                ordinary_failure,
                cleanup_failed,
                cleanup_control,
                close_result,
            )
            raise control
        elif ordinary_failure or cleanup_failed:
            del (
                bootstrap,
                descriptor,
                config,
                timeout,
                ordinary_failure,
                cleanup_failed,
                cleanup_control,
                close_result,
            )
            raise _HandoffFailure
        del (
            bootstrap,
            descriptor,
            config,
            timeout,
            ordinary_failure,
            cleanup_failed,
            cleanup_control,
            close_result,
        )


def _raise_handoff_failure() -> NoReturn:
    raise RuntimeError("sidecar child parent handoff failed") from None


def await_parent_handoff(bootstrap: SidecarChildBootstrap) -> None:
    """Consume one inherited close-only gate before child resource adoption."""

    failed = False
    try:
        _await_handoff(bootstrap)
    except Exception:
        failed = True
    except BaseException:
        del bootstrap, failed
        raise
    del bootstrap
    if failed:
        del failed
        _raise_handoff_failure()
    del failed
