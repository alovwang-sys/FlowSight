"""Private parent-side ownership primitives for one future sidecar launch."""

from __future__ import annotations

import fcntl
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, NoReturn, Protocol, cast

from .child_bootstrap import (
    SidecarChildBootstrap,
    decode_sidecar_child_bootstrap,
    encode_sidecar_child_bootstrap,
)
from .incumbent_port import admit_configured_incumbent_port
from .owner_lock import OwnerLock
from .runtime_config import SidecarRuntimeConfig, prepare_sidecar_runtime_config
from .startup_admission import receive_startup_outcome
from .startup_channel import StartupReader, StartupWriter, open_startup_channel
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
_WRITER_CLOSE: Final = StartupWriter.close
_CHILD_EXECUTABLE: Final = sys.executable
_CHILD_ENTRY_MODULE: Final = "flowsight.sidecar.child_entry"
_POPEN: Final = subprocess.Popen
_DEVNULL: Final = subprocess.DEVNULL
_PIPE: Final = os.pipe
_FCNTL: Final = fcntl.fcntl
_F_DUPFD_CLOEXEC: Final = fcntl.F_DUPFD_CLOEXEC
_ISFINITE: Final = math.isfinite
_FSPATH: Final = os.fspath
_PREPARE_RUNTIME_CONFIG: Final = prepare_sidecar_runtime_config
_OWNER_CLOSE: Final = OwnerLock.close
_PLATFORM_PATH_TYPE: Final = type(Path())
_ADD_NOTE: Final = BaseException.add_note
_PARENT_CLEANUP_NOTE: Final = "sidecar parent startup cleanup failed"
_PARENT_STARTUP_ERROR: Final = "sidecar parent startup failed"
_OPEN_STARTUP_CHANNEL: Final = open_startup_channel
_RECEIVE_STARTUP_OUTCOME: Final = receive_startup_outcome
_READER_TYPE: Final = StartupReader
_WRITER_TYPE: Final = StartupWriter

type _ConfigSnapshot = tuple[str, str, str, int | None, float]


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

    __slots__ = ("_issued_plan", "_reader_fd", "_spawn_claimed", "_writer_fd")

    _issued_plan: _ChildLaunchPlan | None
    _reader_fd: int
    _spawn_claimed: bool
    _writer_fd: int

    def __new__(cls, /, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("_ParentHandoffPipe must be opened internally") from None

    def __init__(self, reader_fd: int, writer_fd: int) -> None:
        del self, reader_fd, writer_fd
        raise TypeError("_ParentHandoffPipe must be opened internally") from None

    def __copy__(self) -> NoReturn:
        raise TypeError("_ParentHandoffPipe is move-only") from None

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("_ParentHandoffPipe is move-only") from None

    def __reduce__(self) -> NoReturn:
        raise TypeError("_ParentHandoffPipe cannot be serialized") from None

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        del protocol
        raise TypeError("_ParentHandoffPipe cannot be serialized") from None

    def __getstate__(self) -> NoReturn:
        raise TypeError("_ParentHandoffPipe cannot be serialized") from None

    def bind_plan(self, plan: _ChildLaunchPlan) -> bool:
        if (
            self._issued_plan is not None
            or self._reader_fd < _MIN_DESCRIPTOR
            or self._writer_fd < _MIN_DESCRIPTOR
        ):
            return False
        self._issued_plan = plan
        return True

    def take_plan_reader_fd(self, plan: _ChildLaunchPlan) -> int | None:
        if (
            self._issued_plan is not plan
            or self._spawn_claimed
            or self._reader_fd < _MIN_DESCRIPTOR
            or self._writer_fd < _MIN_DESCRIPTOR
        ):
            return None
        self._spawn_claimed = True
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


class _ChildLaunchPlan:
    """One private command that remains bound to its live parent gate."""

    __slots__ = ("_argv", "_bound", "_handoff", "_pass_fds", "_spawn_attempted")

    _argv: tuple[str, ...]
    _bound: bool
    _handoff: _ParentHandoffPipe
    _pass_fds: tuple[int, int, int]
    _spawn_attempted: bool

    def __new__(cls, /, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("_ChildLaunchPlan must be issued internally") from None

    def __init__(
        self,
        argv: tuple[str, ...],
        pass_fds: tuple[int, int, int],
        handoff: _ParentHandoffPipe,
    ) -> None:
        del self, argv, pass_fds, handoff
        raise TypeError("_ChildLaunchPlan must be issued internally") from None

    def __copy__(self) -> NoReturn:
        raise TypeError("_ChildLaunchPlan is move-only") from None

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("_ChildLaunchPlan is move-only") from None

    def __reduce__(self) -> NoReturn:
        raise TypeError("_ChildLaunchPlan cannot be serialized") from None

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        del protocol
        raise TypeError("_ChildLaunchPlan cannot be serialized") from None

    def __getstate__(self) -> NoReturn:
        raise TypeError("_ChildLaunchPlan cannot be serialized") from None

    def take_command(self) -> tuple[tuple[str, ...], tuple[int, int, int]] | None:
        if self._spawn_attempted:
            return None
        self._spawn_attempted = True
        if not self._bound:
            return None
        handoff_reader_fd = self._handoff.take_plan_reader_fd(self)
        if handoff_reader_fd is None or handoff_reader_fd != self._pass_fds[2]:
            return None
        return self._argv, self._pass_fds


def _open_parent_handoff() -> _ParentHandoffPipe | None:
    try:
        endpoints = _PIPE()
    except Exception:
        return None
    if type(endpoints) is not tuple or len(endpoints) != 2:
        return None
    if (
        type(endpoints[0]) is not int
        or type(endpoints[1]) is not int
        or endpoints[0] < 0
        or endpoints[1] < 0
        or endpoints[0] == endpoints[1]
    ):
        seen: set[int] = set()
        for descriptor in endpoints:
            if type(descriptor) is not int or descriptor < 0 or descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                _CLOSE(descriptor)
            except Exception:
                pass
        return None
    promoted = [endpoints[0], endpoints[1]]
    for index, descriptor in enumerate(endpoints):
        if descriptor >= _MIN_DESCRIPTOR:
            continue
        seen = set()
        try:
            replacement = _FCNTL(descriptor, _F_DUPFD_CLOEXEC, _MIN_DESCRIPTOR)
        except Exception:
            replacement = -1
        if type(replacement) is not int or replacement < _MIN_DESCRIPTOR:
            for active_descriptor in promoted:
                if active_descriptor < 0 or active_descriptor in seen:
                    continue
                seen.add(active_descriptor)
                try:
                    _CLOSE(active_descriptor)
                except Exception:
                    pass
            return None
        promoted[index] = replacement
        try:
            result = _CLOSE(descriptor)
        except Exception:
            result = False
        if result is not None:
            for active_descriptor in promoted:
                if active_descriptor < 0 or active_descriptor in seen:
                    continue
                seen.add(active_descriptor)
                try:
                    _CLOSE(active_descriptor)
                except Exception:
                    pass
            return None
    if promoted[0] == promoted[1]:
        seen = set()
        for descriptor in promoted:
            if descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                _CLOSE(descriptor)
            except Exception:
                pass
        return None
    handoff = object.__new__(_ParentHandoffPipe)
    handoff._reader_fd = promoted[0]
    handoff._writer_fd = promoted[1]
    handoff._issued_plan = None
    handoff._spawn_claimed = False
    return handoff


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
) -> _ChildLaunchPlan | None:
    if (
        type(config) is not _CONFIG_TYPE
        or type(owner) is not _OWNER_TYPE
        or type(writer) is not StartupWriter
        or type(handoff) is not _ParentHandoffPipe
    ):
        return None
    handoff_reader_fd = handoff._reader_fd
    if handoff_reader_fd < _MIN_DESCRIPTOR or handoff._writer_fd < _MIN_DESCRIPTOR:
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
    plan = object.__new__(_ChildLaunchPlan)
    plan._argv = (_CHILD_EXECUTABLE, "-I", "-m", _CHILD_ENTRY_MODULE, *suffix)
    plan._pass_fds = (owner_fd, writer_fd, handoff_reader_fd)
    plan._handoff = handoff
    plan._spawn_attempted = False
    plan._bound = handoff.bind_plan(plan)
    if not plan._bound:
        return None
    return plan


def _spawn_isolated_child(plan: _ChildLaunchPlan) -> _Process | None:
    if type(plan) is not _ChildLaunchPlan:
        return None
    command = plan.take_command()
    if command is None:
        return None
    argv, pass_fds = command
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


def _retire_parent_child_handles(
    owner: OwnerLock,
    writer: StartupWriter,
    handoff: _ParentHandoffPipe,
) -> bool:
    if (
        type(owner) is not _OWNER_TYPE
        or type(writer) is not StartupWriter
        or type(handoff) is not _ParentHandoffPipe
    ):
        return False
    active_control: BaseException | None = None
    ordinary_failure = False
    try:
        result = _OWNER_CLOSE(owner)
        if result is not None:
            ordinary_failure = True
    except Exception:
        ordinary_failure = True
    except BaseException as error:
        active_control = error
    try:
        result = _WRITER_CLOSE(writer)
        if result is not None:
            ordinary_failure = True
    except Exception:
        ordinary_failure = True
    except BaseException as error:
        if active_control is None:
            active_control = error
    try:
        if handoff.retire_reader() is not True:
            ordinary_failure = True
    except Exception:
        ordinary_failure = True
    except BaseException as error:
        if active_control is None:
            active_control = error
    if active_control is not None:
        raise active_control
    return not ordinary_failure


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


_NEW_WAIT_ONLY_REAPER: Final = _WaitOnlyReaper
_NEW_CHILD_HANDOFF: Final = _ChildHandoff


def _new_parent_failure(*, cleanup_failed: bool) -> RuntimeError:
    failure = RuntimeError(_PARENT_STARTUP_ERROR)
    if cleanup_failed:
        _note_cleanup_failure(failure)
    return failure


def _raise_parent_failure(*, cleanup_failed: bool = False) -> NoReturn:
    raise _new_parent_failure(cleanup_failed=cleanup_failed) from None


def _consume_startup_outcome(
    store: StateStore,
    reader: StartupReader,
    timeout: float | None,
) -> tuple[SidecarState | None, bool]:
    """Finish the one reader ownership boundary, admitting one verified state."""

    outcome: object = None
    try:
        with reader:
            if timeout is not None:
                outcome = _RECEIVE_STARTUP_OUTCOME(store, reader, timeout)
    except Exception:
        return None, False
    if timeout is None or type(outcome) is not _STATE_TYPE:
        return None, True
    return outcome, True


def _cleanup_unpublished_child(child: _ChildHandoff) -> bool:
    cleaned = False
    control: BaseException | None = None
    try:
        cleaned = child.cleanup_before_commit() is True
    except Exception:
        cleaned = False
    except BaseException as error:
        control = error
    retired = False
    if cleaned:
        try:
            retired = child.retire_writer_after_reaped_cleanup() is True
        except Exception:
            retired = False
        except BaseException as error:
            if control is None:
                control = error
    if control is not None:
        _note_cleanup_failure(control)
        raise control
    return cleaned and retired


def _fail_unpublished(
    store: StateStore,
    reader: StartupReader,
    child: _ChildHandoff,
    active: BaseException | None,
) -> NoReturn:
    cleaned = False
    cleanup_control: BaseException | None = None
    try:
        cleaned = _cleanup_unpublished_child(child)
    except BaseException as error:
        cleanup_control = error
    _unused_state, reader_ok = _consume_startup_outcome(store, reader, None)
    cleanup_failed = not cleaned or reader_ok is not True
    if active is not None:
        if cleanup_failed or cleanup_control is not None:
            _note_cleanup_failure(active)
        raise active
    if cleanup_control is not None:
        raise cleanup_control
    _raise_parent_failure(cleanup_failed=cleanup_failed)


def _cleanup_unwrapped_child(
    process: _Process,
    writer_fd: int,
    handoff: _ParentHandoffPipe,
) -> bool:
    """Contain a pre-commit child when its handoff wrapper could not be built."""

    active_control: BaseException | None = None
    ordinary_failure = False
    try:
        process.terminate()
    except Exception:
        terminated = False
        ordinary_failure = True
    except BaseException as error:
        terminated = False
        active_control = error
    else:
        terminated = True
    try:
        process.wait(timeout=_CLEANUP_GRACE_SECONDS)
    except Exception:
        reaped = False
        ordinary_failure = True
    except BaseException as error:
        reaped = False
        if active_control is None:
            active_control = error
    else:
        reaped = True
    writer_retired = False
    if reaped and writer_fd >= _MIN_DESCRIPTOR:
        try:
            writer_retired = _CLOSE(writer_fd) is None
        except Exception:
            ordinary_failure = True
        except BaseException as error:
            if active_control is None:
                active_control = error
    elif reaped:
        try:
            writer_retired = handoff.close_uncommitted() is True
        except Exception:
            ordinary_failure = True
        except BaseException as error:
            if active_control is None:
                active_control = error
    if active_control is not None:
        _note_cleanup_failure(active_control)
        raise active_control
    return terminated and reaped and writer_retired and not ordinary_failure


def _fail_unwrapped_child(
    store: StateStore,
    reader: StartupReader,
    process: _Process,
    writer_fd: int,
    handoff: _ParentHandoffPipe,
    active: BaseException | None,
) -> NoReturn:
    cleaned = False
    cleanup_control: BaseException | None = None
    try:
        cleaned = _cleanup_unwrapped_child(process, writer_fd, handoff)
    except BaseException as error:
        cleanup_control = error
    _unused_state, reader_ok = _consume_startup_outcome(store, reader, None)
    cleanup_failed = not cleaned or reader_ok is not True
    if active is not None:
        if cleanup_failed or cleanup_control is not None:
            _note_cleanup_failure(active)
        raise active
    if cleanup_control is not None:
        raise cleanup_control
    _raise_parent_failure(cleanup_failed=cleanup_failed)


def _close_rejected_startup_channel(
    store: StateStore,
    endpoints: tuple[object, ...] | list[object],
) -> bool:
    """Retire exact known endpoints returned inside one malformed channel result."""

    closed = True
    active_control: BaseException | None = None
    seen: list[object] = []
    for endpoint in endpoints:
        if type(endpoint) not in (_READER_TYPE, _WRITER_TYPE) or any(
            endpoint is previous for previous in seen
        ):
            continue
        seen.append(endpoint)
        if type(endpoint) is _READER_TYPE:
            try:
                _unused_state, endpoint_ok = _consume_startup_outcome(
                    store,
                    endpoint,
                    None,
                )
                if endpoint_ok is not True:
                    closed = False
            except BaseException as error:
                if active_control is None:
                    active_control = error
                closed = False
        else:
            try:
                if _WRITER_CLOSE(cast(StartupWriter, endpoint)) is not None:
                    closed = False
            except Exception:
                closed = False
            except BaseException as error:
                if active_control is None:
                    active_control = error
                closed = False
    if active_control is not None:
        _note_cleanup_failure(active_control)
        raise active_control
    return closed


def _close_unlaunched_owner_resources(
    store: StateStore,
    owner: OwnerLock,
    reader: StartupReader | None,
    writer: StartupWriter | None,
    handoff: _ParentHandoffPipe | None,
) -> bool:
    closed = True
    control: BaseException | None = None
    try:
        _retire_owner(owner)
    except Exception:
        closed = False
    except BaseException as error:
        control = error
        closed = False
    if writer is not None:
        try:
            if _WRITER_CLOSE(writer) is not None:
                closed = False
        except Exception:
            closed = False
        except BaseException as error:
            if control is None:
                control = error
            closed = False
    if reader is not None:
        _unused_state, reader_ok = _consume_startup_outcome(store, reader, None)
        if reader_ok is not True:
            closed = False
    if handoff is not None:
        try:
            if handoff.close_uncommitted() is not True:
                closed = False
        except Exception:
            closed = False
        except BaseException as error:
            if control is None:
                control = error
            closed = False
    if control is not None:
        if not closed:
            _note_cleanup_failure(control)
        raise control
    return closed


def _admit_launched_child(
    store: StateStore,
    owner: OwnerLock,
    writer: StartupWriter,
    reader: StartupReader,
    handoff: _ParentHandoffPipe,
    process: _Process,
    deadline: float,
    observed: float,
) -> SidecarState:
    reserved_deadline = deadline - _CLEANUP_GRACE_SECONDS
    active: BaseException | None = None
    retired = False
    try:
        retired = _retire_parent_child_handles(owner, writer, handoff) is True
    except Exception:
        retired = False
    except BaseException as error:
        active = error
    writer_fd: int | None = None
    taken: object = None
    try:
        taken = handoff.take_writer()
    except Exception:
        taken = None
    except BaseException as error:
        if active is None:
            active = error
    if type(taken) is int and taken >= _MIN_DESCRIPTOR:
        writer_fd = taken
    child: _ChildHandoff | None = None
    child_factory_failed = writer_fd is None
    if writer_fd is not None:
        try:
            child = _NEW_CHILD_HANDOFF(process, writer_fd)
        except Exception:
            child_factory_failed = True
        except BaseException as error:
            child_factory_failed = True
            if active is None:
                active = error
    if child_factory_failed or child is None:
        _fail_unwrapped_child(
            store,
            reader,
            process,
            writer_fd if writer_fd is not None else -1,
            handoff,
            active,
        )
    transferred = False
    if active is None and retired and writer_fd is not None:
        reaper: _Reaper | None = None
        reaper_control: BaseException | None = None
        try:
            reaper = _NEW_WAIT_ONLY_REAPER(process)
        except Exception:
            reaper = None
        except BaseException as error:
            reaper_control = error
        if reaper is None:
            _fail_unpublished(store, reader, child, reaper_control)
        try:
            child.transfer_wait_ownership(reaper)
            transferred = True
        except Exception:
            transferred = False
        except BaseException as error:
            active = error
    fresh: tuple[float, float] | None = None
    if active is None and transferred:
        try:
            fresh = _remaining(reserved_deadline, observed)
        except Exception:
            fresh = None
        except BaseException as error:
            active = error
    if active is not None or fresh is None:
        _fail_unpublished(store, reader, child, active)
    observed = fresh[0]
    released = False
    release_control: BaseException | None = None
    try:
        released = child.release_gate() is True
    except Exception:
        released = False
    except BaseException as error:
        release_control = error
    if release_control is not None:
        _consume_startup_outcome(store, reader, None)
        raise release_control
    if not released:
        if child.committed is not True:
            _fail_unpublished(store, reader, child, None)
        _consume_startup_outcome(store, reader, None)
        _raise_parent_failure()
    admission: tuple[float, float] | None = None
    try:
        admission = _remaining(reserved_deadline, observed)
    except BaseException:
        _consume_startup_outcome(store, reader, None)
        raise
    if admission is None:
        _consume_startup_outcome(store, reader, None)
        _raise_parent_failure()
    state, reader_ok = _consume_startup_outcome(store, reader, admission[1])
    if state is None or reader_ok is not True:
        _raise_parent_failure()
    return state


def _launch_owned_child(
    config: SidecarRuntimeConfig,
    store: StateStore,
    owner: OwnerLock,
    deadline: float,
    observed: float,
) -> SidecarState:
    reserved_deadline = deadline - _CLEANUP_GRACE_SECONDS
    reader: StartupReader | None = None
    writer: StartupWriter | None = None
    handoff: _ParentHandoffPipe | None = None
    rejected_channel: tuple[object, ...] | list[object] | None = None
    process: _Process | None = None
    active: BaseException | None = None
    try:
        window = _remaining(reserved_deadline, observed)
        if window is not None:
            observed = window[0]
            channel = _OPEN_STARTUP_CHANNEL()
            if (
                type(channel) is tuple
                and len(channel) == 2
                and type(channel[0]) is _READER_TYPE
                and type(channel[1]) is _WRITER_TYPE
            ):
                reader, writer = channel
                handoff = _open_parent_handoff()
            elif type(channel) in (tuple, list):
                rejected_channel = channel
        if reader is not None and writer is not None and handoff is not None:
            window = _remaining(reserved_deadline, observed)
            if window is not None:
                observed = window[0]
                plan = _owner_child_command(config, owner, writer, handoff)
                if plan is not None:
                    window = _remaining(reserved_deadline, observed)
                    if window is not None:
                        observed = window[0]
                        process = _spawn_isolated_child(plan)
    except Exception:
        process = None
    except BaseException as error:
        active = error
        process = None
    if process is None or reader is None or writer is None or handoff is None:
        closed = False
        close_control: BaseException | None = None
        try:
            closed = _close_unlaunched_owner_resources(store, owner, reader, writer, handoff)
        except BaseException as error:
            close_control = error
        if rejected_channel is not None:
            try:
                if _close_rejected_startup_channel(store, rejected_channel) is not True:
                    closed = False
            except BaseException as error:
                if close_control is None:
                    close_control = error
                closed = False
        if active is not None:
            if not closed:
                _note_cleanup_failure(active)
            raise active
        if close_control is not None:
            raise close_control
        _raise_parent_failure(cleanup_failed=not closed)
    return _admit_launched_child(
        store,
        owner,
        writer,
        reader,
        handoff,
        process,
        deadline,
        observed,
    )


def start_or_attach_sidecar(config: SidecarRuntimeConfig) -> SidecarState:
    """Attach one healthy compatible sidecar or start and admit exactly one child."""

    if type(config) is not _CONFIG_TYPE:
        raise TypeError("config must be an exact SidecarRuntimeConfig")
    prepared = _prepare_election(config)
    if prepared is None:
        _raise_parent_failure()
    exact_config, store, deadline, observed = prepared
    elected: tuple[SidecarState | OwnerLock, float] | None = None
    try:
        elected = _elect_once(store, deadline, observed)
    except Exception:
        elected = None
    if elected is None:
        _raise_parent_failure()
    outcome, observed = elected
    if type(outcome) is _STATE_TYPE:
        admitted = _admit_incumbent(exact_config, outcome, deadline, observed)
        if admitted is None:
            _raise_parent_failure()
        return admitted
    return _launch_owned_child(exact_config, store, cast(OwnerLock, outcome), deadline, observed)
