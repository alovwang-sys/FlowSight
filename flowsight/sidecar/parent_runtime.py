"""Private parent-side ownership primitives for one future sidecar launch."""

from __future__ import annotations

import math
import os
import time
from typing import Final, Protocol, cast

from .incumbent_port import admit_configured_incumbent_port
from .owner_lock import OwnerLock
from .runtime_config import SidecarRuntimeConfig
from .startup_wait import wait_for_owner_election
from .state import SidecarState, StateStore

_CLOSE: Final = os.close
_CLEANUP_GRACE_SECONDS: Final = 0.25
_MIN_DESCRIPTOR: Final = 3
_MAX_TIMEOUT_SECONDS: Final = 30.0

_CONFIG_TYPE: Final = SidecarRuntimeConfig
_STORE_TYPE: Final = StateStore
_STATE_TYPE: Final = SidecarState
_OWNER_TYPE: Final = OwnerLock
_CONSTRUCT_STORE: Final = StateStore
_WAIT_FOR_OWNER_ELECTION: Final = wait_for_owner_election
_ADMIT_INCUMBENT_PORT: Final = admit_configured_incumbent_port
_READ_MONOTONIC: Final = time.monotonic

type _ConfigSnapshot = tuple[str, str, str, int | None, float]


class _Process(Protocol):
    def terminate(self) -> object: ...

    def wait(self, *, timeout: float) -> object: ...


class _Reaper(Protocol):
    def start(self) -> object: ...

    def join(self, timeout: float) -> object: ...

    def is_alive(self) -> bool: ...

    @property
    def wait_ownership(self) -> bool: ...

    @property
    def child_reaped(self) -> bool: ...


def _read_config(config: object) -> _ConfigSnapshot | None:
    if type(config) is not _CONFIG_TYPE:
        return None
    try:
        project_root = object.__getattribute__(config, "project_root")
        runtime_root = object.__getattribute__(config, "runtime_root")
        project_id = object.__getattribute__(config, "project_id")
        requested_port = object.__getattribute__(config, "requested_port")
        startup_timeout = object.__getattribute__(config, "startup_timeout")
    except Exception:
        return None
    if (
        type(project_root) is not str
        or type(runtime_root) is not str
        or type(project_id) is not str
        or (requested_port is not None and type(requested_port) is not int)
        or (type(requested_port) is int and not 0 <= requested_port <= 65_535)
        or type(startup_timeout) is not float
        or not math.isfinite(startup_timeout)
        or not 0.0 < startup_timeout <= _MAX_TIMEOUT_SECONDS
    ):
        return None
    return project_root, runtime_root, project_id, requested_port, startup_timeout


def _observe(previous: float | None) -> float | None:
    try:
        observed = _READ_MONOTONIC()
    except Exception:
        return None
    if type(observed) is not float or not math.isfinite(observed):
        return None
    if previous is not None and observed < previous:
        return None
    return observed


def _remaining(deadline: float, previous: float) -> tuple[float, float] | None:
    observed = _observe(previous)
    if observed is None:
        return None
    remaining = deadline - observed
    if not math.isfinite(remaining) or remaining <= 0.0:
        return None
    return observed, remaining


def _prepare_election(
    config: object,
) -> tuple[SidecarRuntimeConfig, StateStore, float, float] | None:
    snapshot = _read_config(config)
    if snapshot is None:
        return None
    started = _observe(None)
    if started is None:
        return None
    deadline = started + snapshot[4]
    if not math.isfinite(deadline) or deadline <= started:
        return None
    try:
        store = _CONSTRUCT_STORE(snapshot[1], project_id=snapshot[2])
    except Exception:
        return None
    if type(store) is not _STORE_TYPE:
        return None
    return cast(SidecarRuntimeConfig, config), store, deadline, started


def _elect_once(
    store: StateStore,
    deadline: float,
    observed: float,
) -> tuple[SidecarState | OwnerLock, float] | None:
    window = _remaining(deadline, observed)
    if window is None:
        return None
    observed, timeout = window
    try:
        outcome = _WAIT_FOR_OWNER_ELECTION(store, timeout)
    except Exception:
        return None
    if type(outcome) not in {_STATE_TYPE, _OWNER_TYPE}:
        return None
    final_window = _remaining(deadline, observed)
    if final_window is None:
        return None
    return outcome, final_window[0]


def _admit_incumbent(
    config: SidecarRuntimeConfig,
    incumbent: SidecarState,
    deadline: float,
    observed: float,
) -> SidecarState | None:
    if _remaining(deadline, observed) is None:
        return None
    try:
        admitted = _ADMIT_INCUMBENT_PORT(config, incumbent)
    except Exception:
        return None
    if type(admitted) is not _STATE_TYPE or admitted is not incumbent:
        return None
    return admitted


class _ChildHandoff:
    """One-way ownership state for a child held behind the EOF gate."""

    __slots__ = (
        "_child_reaped",
        "_cleanup_attempted",
        "_committed",
        "_process",
        "_reaper",
        "_release_eligible",
        "_transfer_attempted",
        "_transferred",
        "_writer_retirement_attempted",
        "_writer_fd",
    )

    def __init__(self, process: _Process, handoff_writer_fd: int) -> None:
        self._process = process
        self._writer_fd = handoff_writer_fd
        self._reaper: _Reaper | None = None
        self._transferred = False
        self._release_eligible = False
        self._transfer_attempted = False
        self._cleanup_attempted = False
        self._child_reaped = False
        self._committed = False
        self._writer_retirement_attempted = False

    def transfer_wait_ownership(self, reaper: _Reaper) -> None:
        if self._transfer_attempted or self._cleanup_attempted or self._committed:
            raise RuntimeError
        self._transfer_attempted = True
        try:
            result = reaper.start()
        except Exception:
            self._reaper = reaper
            self._transferred = reaper.wait_ownership is True
            raise
        except BaseException:
            self._reaper = reaper
            self._transferred = reaper.wait_ownership is True
            raise
        self._reaper = reaper
        self._transferred = reaper.wait_ownership is True
        if result is not None or not self._transferred:
            raise RuntimeError
        self._release_eligible = True

    def cleanup_before_commit(self) -> bool:
        if self._committed or self._cleanup_attempted:
            return False
        self._cleanup_attempted = True
        active_control: BaseException | None = None
        ordinary_failure = False
        try:
            self._process.terminate()
        except Exception:
            terminated = False
            ordinary_failure = True
        except BaseException as error:
            terminated = False
            active_control = error
        else:
            terminated = True
        try:
            if self._transferred:
                reaper = self._reaper
                if reaper is None:
                    return False
                reaper.join(_CLEANUP_GRACE_SECONDS)
                exited = reaper.child_reaped is True
            else:
                self._process.wait(timeout=_CLEANUP_GRACE_SECONDS)
                exited = True
        except Exception:
            exited = False
            ordinary_failure = True
        except BaseException as error:
            exited = False
            if active_control is None:
                active_control = error
        self._child_reaped = exited
        if active_control is not None:
            raise active_control
        return terminated and exited and not ordinary_failure

    def release_gate(self) -> bool:
        if (
            not self._transferred
            or not self._release_eligible
            or self._cleanup_attempted
            or self._committed
        ):
            return False
        writer_fd = self._writer_fd
        if writer_fd < _MIN_DESCRIPTOR:
            return False
        self._writer_fd = -1
        self._committed = True
        result = _CLOSE(writer_fd)
        return result is None

    def retire_writer_after_reaped_cleanup(self) -> bool:
        if self._committed or self._writer_retirement_attempted or not self._child_reaped:
            return False
        writer_fd = self._writer_fd
        if writer_fd < _MIN_DESCRIPTOR:
            return True
        self._writer_fd = -1
        self._writer_retirement_attempted = True
        result = _CLOSE(writer_fd)
        return result is None

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def transferred(self) -> bool:
        return self._transferred
