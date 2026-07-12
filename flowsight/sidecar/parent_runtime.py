"""Private parent-side ownership primitives for one future sidecar launch."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol, cast

from .child_bootstrap import (
    SidecarChildBootstrap,
    decode_sidecar_child_bootstrap,
    encode_sidecar_child_bootstrap,
)
from .incumbent_port import admit_configured_incumbent_port
from .owner_lock import OwnerLock
from .runtime_config import SidecarRuntimeConfig, prepare_sidecar_runtime_config
from .startup_channel import StartupWriter
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
_THREAD: Final = threading.Thread
_THREAD_START: Final = cast(Callable[[threading.Thread], object], threading.Thread.start)
_EVENT: Final = threading.Event
_EVENT_SET: Final = cast(Callable[[threading.Event], object], threading.Event.set)
_ENCODE_CHILD_BOOTSTRAP: Final = encode_sidecar_child_bootstrap
_DECODE_CHILD_BOOTSTRAP: Final = decode_sidecar_child_bootstrap
_BOOTSTRAP_TYPE: Final = SidecarChildBootstrap
_OWNER_FILENO: Final = OwnerLock.fileno
_WRITER_FILENO: Final = StartupWriter.fileno
_CHILD_EXECUTABLE: Final = sys.executable
_CHILD_ENTRY_MODULE: Final = "flowsight.sidecar.child_entry"
_POPEN: Final = subprocess.Popen
_DEVNULL: Final = subprocess.DEVNULL
_PIPE: Final = os.pipe
_ISFINITE: Final = math.isfinite
_FSPATH: Final = os.fspath
_PREPARE_RUNTIME_CONFIG: Final = prepare_sidecar_runtime_config
_OWNER_CLOSE: Final = OwnerLock.close
_PLATFORM_PATH_TYPE: Final = type(Path())
_ADD_NOTE: Final = BaseException.add_note
_PARENT_CLEANUP_NOTE: Final = "sidecar parent startup cleanup failed"

type _ConfigSnapshot = tuple[str, str, str, int | None, float]
type _ChildCommand = tuple[tuple[str, ...], tuple[int, int, int]]


class _Process(Protocol):
    def terminate(self) -> object: ...

    def wait(self, timeout: float | None = None) -> object: ...


class _Reaper(Protocol):
    def start(self) -> object: ...

    def join(self, timeout: float) -> object: ...

    def is_alive(self) -> bool: ...

    def begin_wait(self) -> object: ...

    @property
    def wait_ownership(self) -> bool: ...

    @property
    def child_reaped(self) -> bool: ...


class _OwnerCleanupFailure(Exception):
    pass


class _ParentHandoffPipe:
    """Private provenance for one child gate and its retained parent writer."""

    __slots__ = ("_reader_fd", "_writer_fd")

    def __init__(self, reader_fd: int, writer_fd: int) -> None:
        self._reader_fd = reader_fd
        self._writer_fd = writer_fd

    def child_reader_fd(self) -> int | None:
        if self._reader_fd < _MIN_DESCRIPTOR or self._writer_fd < _MIN_DESCRIPTOR:
            return None
        return self._reader_fd

    def take_writer(self) -> int | None:
        if self._writer_fd < _MIN_DESCRIPTOR:
            return None
        writer_fd = self._writer_fd
        self._writer_fd = -1
        return writer_fd

    def retire_reader(self) -> bool:
        reader_fd = self._reader_fd
        if reader_fd < _MIN_DESCRIPTOR:
            return False
        self._reader_fd = -1
        try:
            result = _CLOSE(reader_fd)
        except Exception:
            return False
        return result is None

    def close_uncommitted(self) -> bool:
        descriptors = (self._reader_fd, self._writer_fd)
        self._reader_fd = -1
        self._writer_fd = -1
        closed = True
        for descriptor in descriptors:
            if descriptor < _MIN_DESCRIPTOR:
                continue
            try:
                if _CLOSE(descriptor) is not None:
                    closed = False
            except Exception:
                closed = False
        return closed


def _open_parent_handoff() -> _ParentHandoffPipe | None:
    try:
        endpoints = _PIPE()
    except Exception:
        return None
    if (
        type(endpoints) is not tuple
        or len(endpoints) != 2
        or type(endpoints[0]) is not int
        or type(endpoints[1]) is not int
        or endpoints[0] < _MIN_DESCRIPTOR
        or endpoints[1] < _MIN_DESCRIPTOR
        or endpoints[0] == endpoints[1]
    ):
        return None
    return _ParentHandoffPipe(endpoints[0], endpoints[1])


class _WaitOnlyReaper:
    """Private daemon thread that performs one and only one child wait."""

    __slots__ = (
        "_child_reaped",
        "_handoff_release",
        "_process",
        "_started",
        "_thread",
        "_wait_permission_sent",
        "_wait_ownership",
    )

    def __init__(self, process: _Process) -> None:
        self._process = process
        self._thread: threading.Thread | None = None
        self._handoff_release = _EVENT()
        self._started = False
        self._wait_ownership = False
        self._wait_permission_sent = False
        self._child_reaped = False

    def _wait_once(self) -> None:
        if self._handoff_release.wait() is not True:
            return
        try:
            self._process.wait()
        except BaseException:
            return
        self._child_reaped = True

    def start(self) -> None:
        if self._started:
            raise RuntimeError
        self._started = True
        thread = _THREAD(target=self._wait_once, daemon=True)
        self._thread = thread
        self._wait_ownership = True
        try:
            result = _THREAD_START(thread)
        except Exception:
            self._wait_ownership = False
            raise
        if result is not None:
            raise RuntimeError

    def join(self, timeout: float) -> None:
        thread = self._thread
        if thread is None:
            raise RuntimeError
        thread.join(timeout)

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def begin_wait(self) -> bool:
        if self._wait_permission_sent:
            return True
        active_control: BaseException | None = None
        try:
            first = _EVENT_SET(self._handoff_release)
        except Exception:
            first = False
        except BaseException as error:
            first = False
            active_control = error
        if first is None:
            self._wait_permission_sent = True
            if active_control is not None:
                raise active_control
            return True
        try:
            second = _EVENT_SET(self._handoff_release)
        except Exception:
            if active_control is not None:
                raise active_control from None
            return False
        except BaseException:
            if active_control is not None:
                raise active_control from None
            raise
        if second is not None:
            if active_control is not None:
                raise active_control from None
            return False
        self._wait_permission_sent = True
        if active_control is not None:
            raise active_control from None
        return True

    @property
    def wait_ownership(self) -> bool:
        return self._wait_ownership

    @property
    def child_reaped(self) -> bool:
        return self._child_reaped


def _config_snapshot(config: object) -> _ConfigSnapshot | None:
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
        or not _ISFINITE(startup_timeout)
        or not 0.0 < startup_timeout <= _MAX_TIMEOUT_SECONDS
    ):
        return None
    return project_root, runtime_root, project_id, requested_port, startup_timeout


def _matches_prepared_config(snapshot: _ConfigSnapshot) -> bool:
    try:
        prepared = _PREPARE_RUNTIME_CONFIG(
            snapshot[0],
            requested_port=snapshot[3],
            startup_timeout=snapshot[4],
        )
    except Exception:
        return False
    prepared_snapshot = _config_snapshot(prepared)
    return prepared_snapshot == snapshot


def _read_config(config: object) -> _ConfigSnapshot | None:
    snapshot = _config_snapshot(config)
    if snapshot is None or not _matches_prepared_config(snapshot):
        return None
    return snapshot


def _store_matches_snapshot(store: StateStore, snapshot: _ConfigSnapshot) -> bool:
    try:
        fields = object.__getattribute__(store, "__dict__")
        if type(fields) is not dict:
            return False
        stored_project_id: object = None
        stored_runtime_root: object = None
        for field_name, field_value in fields.items():
            if type(field_name) is not str:
                return False
            if field_name == "project_id":
                stored_project_id = field_value
            elif field_name == "runtime_root":
                stored_runtime_root = field_value
        if type(stored_project_id) is not str or stored_project_id != snapshot[2]:
            return False
        if type(stored_runtime_root) is not _PLATFORM_PATH_TYPE:
            return False
        stored_runtime_text = _FSPATH(stored_runtime_root)
    except Exception:
        return False
    return type(stored_runtime_text) is str and stored_runtime_text == snapshot[1]


def _retire_owner(owner: OwnerLock) -> None:
    try:
        result = _OWNER_CLOSE(owner)
    except Exception:
        raise _OwnerCleanupFailure from None
    if result is not None:
        raise _OwnerCleanupFailure


def _note_cleanup_failure(active_control: BaseException) -> None:
    try:
        _ADD_NOTE(active_control, _PARENT_CLEANUP_NOTE)
    except BaseException:
        pass


def _observe(previous: float | None) -> float | None:
    try:
        observed = _READ_MONOTONIC()
    except Exception:
        return None
    if type(observed) is not float or not _ISFINITE(observed):
        return None
    if previous is not None and observed < previous:
        return None
    return observed


def _remaining(deadline: float, previous: float) -> tuple[float, float] | None:
    observed = _observe(previous)
    if observed is None:
        return None
    remaining = deadline - observed
    if not _ISFINITE(remaining) or remaining <= 0.0:
        return None
    return observed, remaining


def _prepare_election(
    config: object,
) -> tuple[SidecarRuntimeConfig, StateStore, float, float] | None:
    snapshot = _config_snapshot(config)
    if snapshot is None:
        return None
    started = _observe(None)
    if started is None:
        return None
    deadline = started + snapshot[4]
    if not _ISFINITE(deadline) or deadline <= started:
        return None
    if not _matches_prepared_config(snapshot):
        return None
    window = _remaining(deadline, started)
    if window is None:
        return None
    observed, _timeout = window
    try:
        store = _CONSTRUCT_STORE(snapshot[1], project_id=snapshot[2])
    except Exception:
        return None
    if type(store) is not _STORE_TYPE or not _store_matches_snapshot(store, snapshot):
        return None
    return cast(SidecarRuntimeConfig, config), store, deadline, observed


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
    try:
        final_window = _remaining(deadline, observed)
    except BaseException as active_control:
        if type(outcome) is _OWNER_TYPE:
            try:
                _retire_owner(outcome)
            except BaseException:
                _note_cleanup_failure(active_control)
        raise active_control
    if final_window is None:
        if type(outcome) is _OWNER_TYPE:
            _retire_owner(outcome)
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


def _owner_child_command(
    config: SidecarRuntimeConfig,
    owner: OwnerLock,
    writer: StartupWriter,
    handoff: _ParentHandoffPipe,
) -> _ChildCommand | None:
    if (
        type(config) is not _CONFIG_TYPE
        or type(owner) is not _OWNER_TYPE
        or type(writer) is not StartupWriter
        or type(handoff) is not _ParentHandoffPipe
    ):
        return None
    handoff_reader_fd = handoff.child_reader_fd()
    if handoff_reader_fd is None:
        return None
    try:
        owner_fd = _OWNER_FILENO(owner)
        writer_fd = _WRITER_FILENO(writer)
    except Exception:
        return None
    if (
        type(owner_fd) is not int
        or type(writer_fd) is not int
        or owner_fd < _MIN_DESCRIPTOR
        or writer_fd < _MIN_DESCRIPTOR
        or owner_fd == writer_fd
        or owner_fd == handoff_reader_fd
        or writer_fd == handoff_reader_fd
    ):
        return None
    try:
        suffix = _ENCODE_CHILD_BOOTSTRAP(
            config,
            owner_lock_fd=owner_fd,
            startup_writer_fd=writer_fd,
            parent_handoff_fd=handoff_reader_fd,
        )
    except Exception:
        return None
    if (
        type(suffix) is not tuple
        or not suffix
        or any(type(argument) is not str for argument in suffix)
        or type(_CHILD_EXECUTABLE) is not str
        or not _CHILD_EXECUTABLE
    ):
        return None
    return (
        (_CHILD_EXECUTABLE, "-I", "-m", _CHILD_ENTRY_MODULE, *suffix),
        (owner_fd, writer_fd, handoff_reader_fd),
    )


def _spawn_isolated_child(command: _ChildCommand) -> _Process | None:
    if (
        type(command) is not tuple
        or len(command) != 2
        or type(command[0]) is not tuple
        or type(command[1]) is not tuple
    ):
        return None
    argv = command[0]
    pass_fds = command[1]
    if (
        type(argv) is not tuple
        or len(argv) != 13
        or any(type(argument) is not str for argument in argv)
        or argv[0] != _CHILD_EXECUTABLE
        or argv[1:4] != ("-I", "-m", _CHILD_ENTRY_MODULE)
        or type(pass_fds) is not tuple
        or len(pass_fds) != 3
        or any(
            type(descriptor) is not int or descriptor < _MIN_DESCRIPTOR for descriptor in pass_fds
        )
        or len(set(pass_fds)) != 3
    ):
        return None
    try:
        bootstrap = _DECODE_CHILD_BOOTSTRAP(argv[4:])
        owner_fd = object.__getattribute__(bootstrap, "owner_lock_fd")
        writer_fd = object.__getattribute__(bootstrap, "startup_writer_fd")
        handoff_reader_fd = object.__getattribute__(bootstrap, "parent_handoff_fd")
    except Exception:
        return None
    if (
        type(bootstrap) is not _BOOTSTRAP_TYPE
        or (owner_fd, writer_fd, handoff_reader_fd) != pass_fds
    ):
        return None
    try:
        process = _POPEN(
            argv,
            stdin=_DEVNULL,
            stdout=_DEVNULL,
            stderr=_DEVNULL,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
            shell=False,
        )
    except Exception:
        return None
    return cast(_Process, process)


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
                if reaper.begin_wait() is not True:
                    ordinary_failure = True
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
        reaper = self._reaper
        if reaper is None or reaper.wait_ownership is not True or reaper.is_alive() is not True:
            return False
        writer_fd = self._writer_fd
        if writer_fd < _MIN_DESCRIPTOR:
            return False
        self._writer_fd = -1
        self._committed = True
        try:
            result = _CLOSE(writer_fd)
        except BaseException as active_control:
            try:
                reaper.begin_wait()
            except BaseException:
                _note_cleanup_failure(active_control)
            raise active_control
        permission_ok = reaper.begin_wait() is True
        return result is None and permission_ok

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
