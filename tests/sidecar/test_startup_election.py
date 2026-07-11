from __future__ import annotations

import ast
import errno
import inspect
import math
import os
import sys
import traceback
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    OwnerElectionError,
    OwnerElectionErrorCode,
    OwnerLock,
    OwnerLockError,
    OwnerLockErrorCode,
    SidecarState,
    StateStore,
    resolve_owner_election,
)
from flowsight.sidecar import owner_lock as owner_lock_module
from flowsight.sidecar import startup_discovery as discovery_module
from flowsight.sidecar import startup_election as election_module

PROJECT_ID = "election-project"
STARTUP_ID = "0123456789abcdef0123456789abcdef"
TOKEN = "A" * 43
PRIVATE = "private-token-path-pid-fd-errno-detail"
_UNEXPECTED = object()


class _DerivedInt(int):
    pass


class _DerivedFloat(float):
    pass


class _DerivedStore(StateStore):
    pass


class _DerivedState(SidecarState):
    pass


class _DerivedOwner(OwnerLock):
    pass


class _DerivedOwnerError(OwnerLockError):
    pass


class _ProcessControl(BaseException):
    pass


class _Flow:
    def __init__(
        self,
        *,
        clock_values: list[object],
        discoveries: list[object] | None = None,
        acquire: object = _UNEXPECTED,
        release: object = _UNEXPECTED,
    ) -> None:
        self.clock_values = list(clock_values)
        self.discoveries = list(discoveries or [])
        self.acquire_value = acquire
        self.release_value = release
        self.events: list[tuple[object, ...]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(election_module, "_read_monotonic", self.read_clock)
        monkeypatch.setattr(election_module, "_discover_startup", self.discover)
        monkeypatch.setattr(election_module, "_acquire_owner", self.acquire)
        monkeypatch.setattr(election_module, "_exit_owner", self.release)

    def read_clock(self) -> object:
        self.events.append(("clock",))
        if not self.clock_values:
            raise AssertionError("unexpected extra monotonic read")
        return _resolve(self.clock_values.pop(0))

    def discover(self, store: StateStore, timeout: float) -> object:
        self.events.append(("discover", store, timeout))
        if not self.discoveries:
            raise AssertionError("unexpected extra discovery")
        return _resolve(self.discoveries.pop(0))

    def acquire(self, store: StateStore) -> object:
        self.events.append(("acquire", store))
        return _resolve(self.acquire_value)

    def release(
        self,
        owner: OwnerLock,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
    ) -> object:
        self.events.append(("release", owner, exception_type, exception))
        return _resolve(self.release_value)


def _resolve(value: object) -> object:
    if value is _UNEXPECTED:
        raise AssertionError("unexpected collaborator call")
    if isinstance(value, BaseException):
        raise value
    return value


def _raise(error: BaseException) -> NoReturn:
    raise error


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "runtime", project_id=PROJECT_ID)


def _state(store: StateStore) -> SidecarState:
    return SidecarState(
        project_id=PROJECT_ID,
        startup_id=STARTUP_ID,
        pid=4321,
        port=4040,
        token=TOKEN,
        database_path=str(store.database_path),
        started_at_ns=1_700_000_000_000_000_000,
    )


def _fake_owner() -> OwnerLock:
    return object.__new__(OwnerLock)


def _assert_raw_descriptor_closed(file_descriptor: int) -> None:
    with pytest.raises(OSError) as captured:
        os.fstat(file_descriptor)
    assert captured.value.errno == errno.EBADF


def _assert_election_error(
    error: BaseException,
    code: OwnerElectionErrorCode,
    *,
    notes: list[str] | None = None,
) -> OwnerElectionError:
    assert type(error) is OwnerElectionError
    election_error = error
    assert election_error.code is code
    assert str(election_error) == f"sidecar owner election failed ({code})"
    assert election_error.args == (f"sidecar owner election failed ({code})",)
    assert election_error.__cause__ is None
    assert getattr(election_error, "__notes__", None) == notes
    exposed = "\n".join(
        (
            str(election_error),
            repr(election_error),
            repr(election_error.args),
            repr(election_error.__cause__),
            repr(getattr(election_error, "__notes__", None)),
        )
    )
    assert PRIVATE not in exposed
    assert TOKEN not in exposed
    return election_error


def _release_events(flow: _Flow) -> list[tuple[object, ...]]:
    return [event for event in flow.events if event[0] == "release"]


def _builtins_retain_identity(value: object, target: object, seen: set[int]) -> bool:
    if value is target:
        return True
    marker = id(value)
    if marker in seen:
        return False
    seen.add(marker)
    if type(value) is dict:
        mapping: dict[object, object] = value
        return any(
            _builtins_retain_identity(item, target, seen)
            for pair in mapping.items()
            for item in pair
        )
    if type(value) in {list, tuple, set, frozenset}:
        values: list[object] | tuple[object, ...] | set[object] | frozenset[object] = value
        return any(_builtins_retain_identity(item, target, seen) for item in values)
    return False


def _assert_module_does_not_retain_identity(target: object) -> None:
    for name, value in vars(election_module).items():
        if name != "__builtins__":
            assert not _builtins_retain_identity(value, target, set())


def test_public_shape_exports_and_error_contract_are_exact() -> None:
    assert sidecar_package.resolve_owner_election is resolve_owner_election
    assert sidecar_package.OwnerElectionError is OwnerElectionError
    assert sidecar_package.OwnerElectionErrorCode is OwnerElectionErrorCode
    assert sidecar_package.__all__.count("resolve_owner_election") == 1
    assert sidecar_package.__all__.count("OwnerElectionError") == 1
    assert sidecar_package.__all__.count("OwnerElectionErrorCode") == 1

    signature = inspect.signature(resolve_owner_election)
    assert tuple(signature.parameters) == ("store", "timeout")
    assert signature.parameters["store"].default is inspect.Parameter.empty
    assert signature.parameters["timeout"].default == 0.5
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in signature.parameters.values()
    )
    assert get_type_hints(resolve_owner_election) == {
        "store": StateStore,
        "timeout": float,
        "return": SidecarState | OwnerLock,
    }
    assert tuple(OwnerElectionErrorCode) == (
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )

    for code in OwnerElectionErrorCode:
        error = OwnerElectionError(code)
        _assert_election_error(error, code)
        assert vars(error) == {"code": code}

    error_signature = inspect.signature(OwnerElectionError)
    assert tuple(error_signature.parameters) == ("code",)
    assert error_signature.parameters["code"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


@pytest.mark.parametrize("invalid", [None, object(), "code", True])
def test_error_rejects_inexact_code(invalid: object) -> None:
    with pytest.raises(TypeError) as captured:
        OwnerElectionError(invalid)  # type: ignore[arg-type]
    assert str(captured.value) == "code must be an exact OwnerElectionErrorCode"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("invalid", "error_type", "message"),
    [
        (None, TypeError, "store must be an exact StateStore"),
        (object(), TypeError, "store must be an exact StateStore"),
    ],
)
def test_store_preflight_precedes_all_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
    error_type: type[Exception],
    message: str,
) -> None:
    flow = _Flow(clock_values=[])
    flow.install(monkeypatch)
    with pytest.raises(error_type) as captured:
        resolve_owner_election(invalid)  # type: ignore[arg-type]
    assert str(captured.value) == message
    assert flow.events == []

    derived = _DerivedStore(tmp_path / "derived", project_id=PROJECT_ID)
    with pytest.raises(TypeError, match="^store must be an exact StateStore$"):
        resolve_owner_election(derived)
    assert flow.events == []


@pytest.mark.parametrize(
    ("invalid", "error_type", "message"),
    [
        (None, TypeError, "timeout must be a built-in int or float"),
        (True, TypeError, "timeout must be a built-in int or float"),
        (_DerivedInt(1), TypeError, "timeout must be a built-in int or float"),
        (_DerivedFloat(1.0), TypeError, "timeout must be a built-in int or float"),
        (0, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (-1.0, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (30.0001, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (math.nan, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (math.inf, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
    ],
)
def test_timeout_preflight_precedes_all_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
    error_type: type[Exception],
    message: str,
) -> None:
    flow = _Flow(clock_values=[])
    flow.install(monkeypatch)
    with pytest.raises(error_type) as captured:
        resolve_owner_election(_store(tmp_path), invalid)  # type: ignore[arg-type]
    assert str(captured.value) == message
    assert flow.events == []


def test_prelock_incumbent_is_returned_unchanged_without_owner_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent = object.__new__(SidecarState)
    flow = _Flow(clock_values=[10.0, 10.1, 10.2], discoveries=[incumbent])
    flow.install(monkeypatch)

    assert resolve_owner_election(store, 1) is incumbent
    assert flow.events == [
        ("clock",),
        ("clock",),
        ("discover", store, pytest.approx(0.9)),
        ("clock",),
    ]


def test_exact_none_advances_once_and_postlock_none_transfers_exact_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    flow = _Flow(
        clock_values=[10.0, 10.1, 10.2, 10.3, 10.4],
        discoveries=[None, None],
        acquire=owner,
    )
    flow.install(monkeypatch)

    assert resolve_owner_election(store, 1.0) is owner
    assert flow.events == [
        ("clock",),
        ("clock",),
        ("discover", store, pytest.approx(0.9)),
        ("clock",),
        ("acquire", store),
        ("clock",),
        ("discover", store, pytest.approx(0.7)),
        ("clock",),
    ]
    assert flow.discoveries == []


def test_postlock_incumbent_is_returned_only_after_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent = object.__new__(SidecarState)
    owner = _fake_owner()
    flow = _Flow(
        clock_values=[10.0, 10.1, 10.2, 10.3, 10.4, 10.5],
        discoveries=[None, incumbent],
        acquire=owner,
        release=False,
    )
    flow.install(monkeypatch)

    assert resolve_owner_election(store, 1.0) is incumbent
    assert flow.events == [
        ("clock",),
        ("clock",),
        ("discover", store, pytest.approx(0.9)),
        ("clock",),
        ("acquire", store),
        ("clock",),
        ("discover", store, pytest.approx(0.7)),
        ("clock",),
        ("release", owner, None, None),
        ("clock",),
    ]


@pytest.mark.parametrize("malformed", [False, 0, object()])
def test_malformed_prelock_discovery_never_grants_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed: object,
) -> None:
    store = _store(tmp_path)
    flow = _Flow(clock_values=[0.0, 0.1], discoveries=[malformed])
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(store, 1.0)
    _assert_election_error(captured.value, OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    assert [event[0] for event in flow.events] == ["clock", "clock", "discover"]


def test_derived_state_is_rejected_before_and_after_owner_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    derived = object.__new__(_DerivedState)
    pre = _Flow(clock_values=[0.0, 0.1], discoveries=[derived])
    pre.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured_pre:
        resolve_owner_election(store, 1.0)
    _assert_election_error(captured_pre.value, OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    assert _release_events(pre) == []

    owner = _fake_owner()
    post = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3],
        discoveries=[None, derived],
        acquire=owner,
        release=False,
    )
    post.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured_post:
        resolve_owner_election(store, 1.0)
    pending = _assert_election_error(
        captured_post.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    assert _release_events(post) == [("release", owner, OwnerElectionError, pending)]


def test_exact_direct_owner_lock_error_preserves_identity_and_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    cause = RuntimeError("safe cause")
    context = ValueError("safe inner context")
    caller_context = LookupError("safe caller context")
    held = OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_HELD)
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2],
        discoveries=[None],
    )
    flow.install(monkeypatch)

    def raise_held(target: StateStore) -> NoReturn:
        flow.events.append(("acquire", target))
        try:
            raise context
        except ValueError:
            raise held from cause

    monkeypatch.setattr(election_module, "_acquire_owner", raise_held)
    try:
        raise caller_context
    except LookupError:
        with pytest.raises(OwnerLockError) as captured:
            resolve_owner_election(store, 1.0)
    assert captured.value is held
    assert captured.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD
    assert captured.value.__cause__ is cause
    assert captured.value.__context__ is context
    assert [event[0] for event in flow.events] == [
        "clock",
        "clock",
        "discover",
        "clock",
        "acquire",
    ]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError(PRIVATE), _DerivedOwnerError(OwnerLockErrorCode.OWNER_LOCK_HELD)],
)
def test_inexact_or_ordinary_acquire_failure_is_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    store = _store(tmp_path)
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2],
        discoveries=[None],
        acquire=failure,
    )
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(store, 1.0)
    _assert_election_error(captured.value, OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    assert _release_events(flow) == []


@pytest.mark.parametrize("candidate", [None, object(), object.__new__(_DerivedOwner)])
def test_malformed_owner_result_is_not_invoked_or_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: object,
) -> None:
    store = _store(tmp_path)
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2],
        discoveries=[None],
        acquire=candidate,
    )
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(store, 1.0)
    _assert_election_error(captured.value, OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    assert _release_events(flow) == []


@pytest.mark.parametrize("failure", [RuntimeError(PRIVATE), False, object()])
def test_postlock_failure_cancels_owner_with_one_active_error_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: object,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3],
        discoveries=[None, failure],
        acquire=owner,
        release=False,
    )
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(store, 1.0)
    pending = _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    assert _release_events(flow) == [("release", owner, OwnerElectionError, pending)]
    assert len([event for event in flow.events if event[0] == "discover"]) == 2
    assert len([event for event in flow.events if event[0] == "acquire"]) == 1


@pytest.mark.parametrize(
    ("discoveries", "clock_prefix", "cleanup", "expected_events"),
    [
        ([], [10.0], "none", ("clock", "clock")),
        (
            [None],
            [10.0, 10.1],
            "none",
            ("clock", "clock", "discover", "clock"),
        ),
        (
            [_UNEXPECTED],
            [10.0, 10.1],
            "none",
            ("clock", "clock", "discover", "clock"),
        ),
        (
            [None],
            [10.0, 10.1, 10.2],
            "pending",
            ("clock", "clock", "discover", "clock", "acquire", "clock", "release"),
        ),
        (
            [None, None],
            [10.0, 10.1, 10.2, 10.3],
            "pending",
            (
                "clock",
                "clock",
                "discover",
                "clock",
                "acquire",
                "clock",
                "discover",
                "clock",
                "release",
            ),
        ),
        (
            [None, _UNEXPECTED],
            [10.0, 10.1, 10.2, 10.3],
            "pending",
            (
                "clock",
                "clock",
                "discover",
                "clock",
                "acquire",
                "clock",
                "discover",
                "clock",
                "release",
            ),
        ),
        (
            [None, _UNEXPECTED],
            [10.0, 10.1, 10.2, 10.3, 10.4],
            "normal",
            (
                "clock",
                "clock",
                "discover",
                "clock",
                "acquire",
                "clock",
                "discover",
                "clock",
                "release",
                "clock",
            ),
        ),
    ],
    ids=[
        "before-prelock-discovery",
        "before-acquire",
        "before-prelock-state-return",
        "before-postlock-discovery",
        "before-winner-return",
        "before-state-cleanup",
        "after-state-cleanup",
    ],
)
@pytest.mark.parametrize("clock_failure", ["expiry", "rollback"])
def test_deadline_failure_at_every_behavior_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    discoveries: list[object],
    clock_prefix: list[float],
    cleanup: str,
    expected_events: tuple[str, ...],
    clock_failure: str,
) -> None:
    store = _store(tmp_path)
    incumbent = _state(store)
    resolved_discoveries = [incumbent if item is _UNEXPECTED else item for item in discoveries]
    terminal_clock = 11.0 if clock_failure == "expiry" else clock_prefix[-1] - 0.01
    owner = _fake_owner()
    flow = _Flow(
        clock_values=[*clock_prefix, terminal_clock],
        discoveries=resolved_discoveries,
        acquire=owner if "acquire" in expected_events else _UNEXPECTED,
        release=False if "release" in expected_events else _UNEXPECTED,
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(store, 1.0)
    deadline_error = _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    releases = _release_events(flow)
    if cleanup == "none":
        assert releases == []
    elif cleanup == "pending":
        assert releases == [("release", owner, OwnerElectionError, deadline_error)]
    else:
        assert cleanup == "normal"
        assert releases == [("release", owner, None, None)]
    assert tuple(event[0] for event in flow.events) == expected_events
    assert flow.clock_values == []
    assert flow.discoveries == []


@pytest.mark.parametrize(
    "clock_failure",
    [OSError(PRIVATE), True, 1, math.nan, math.inf, -math.inf],
)
def test_initial_clock_failure_shapes_are_fixed_deadline_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_failure: object,
) -> None:
    flow = _Flow(clock_values=[clock_failure])
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(_store(tmp_path), 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert flow.events == [("clock",)]


def test_deadline_overflow_is_rejected_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _Flow(clock_values=[sys.float_info.max])
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(_store(tmp_path), 30.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert flow.events == [("clock",)]


@pytest.mark.parametrize("stage", ["clock", "discover", "acquire"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_before_owner_binding_preserves_identity_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    error = error_factory()
    if stage == "clock":
        flow = _Flow(clock_values=[error])
    elif stage == "discover":
        flow = _Flow(clock_values=[0.0, 0.1], discoveries=[error])
    else:
        flow = _Flow(
            clock_values=[0.0, 0.1, 0.2],
            discoveries=[None],
            acquire=error,
        )
    flow.install(monkeypatch)
    with pytest.raises(error_factory) as captured:
        resolve_owner_election(_store(tmp_path), 1.0)
    assert captured.value is error
    assert getattr(captured.value, "__notes__", None) is None
    assert _release_events(flow) == []


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_after_owner_binding_gets_one_active_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    error = error_factory()
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3],
        discoveries=[None, error],
        acquire=owner,
        release=False,
    )
    flow.install(monkeypatch)
    with pytest.raises(error_factory) as captured:
        resolve_owner_election(store, 1.0)
    assert captured.value is error
    assert getattr(captured.value, "__notes__", None) is None
    assert _release_events(flow) == [("release", owner, error_factory, error)]


@pytest.mark.parametrize(
    ("discoveries", "clock_prefix", "cleanup"),
    [
        ([], [10.0], "none"),
        ([None], [10.0, 10.1], "none"),
        ([_UNEXPECTED], [10.0, 10.1], "none"),
        ([None], [10.0, 10.1, 10.2], "active"),
        ([None, None], [10.0, 10.1, 10.2, 10.3], "active"),
        ([None, _UNEXPECTED], [10.0, 10.1, 10.2, 10.3], "active"),
        ([None, _UNEXPECTED], [10.0, 10.1, 10.2, 10.3, 10.4], "normal"),
    ],
    ids=[
        "before-prelock-discovery",
        "before-acquire",
        "before-prelock-state-return",
        "before-postlock-discovery",
        "before-winner-return",
        "before-state-cleanup",
        "after-state-cleanup",
    ],
)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit, _ProcessControl])
def test_process_control_at_every_clock_boundary_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    discoveries: list[object],
    clock_prefix: list[float],
    cleanup: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    incumbent = object.__new__(SidecarState)
    resolved_discoveries = [incumbent if item is _UNEXPECTED else item for item in discoveries]
    owner = _fake_owner()
    error = error_factory()
    flow = _Flow(
        clock_values=[*clock_prefix, error],
        discoveries=resolved_discoveries,
        acquire=owner if cleanup != "none" else _UNEXPECTED,
        release=False if cleanup != "none" else _UNEXPECTED,
    )
    flow.install(monkeypatch)

    with pytest.raises(error_factory) as captured:
        resolve_owner_election(store, 1.0)
    assert captured.value is error
    assert getattr(captured.value, "__notes__", None) is None
    releases = _release_events(flow)
    if cleanup == "none":
        assert releases == []
    elif cleanup == "active":
        assert releases == [("release", owner, error_factory, error)]
    else:
        assert cleanup == "normal"
        assert releases == [("release", owner, None, None)]
    assert flow.clock_values == []
    assert flow.discoveries == []


@pytest.mark.parametrize(
    "release_result",
    [
        None,
        True,
        0,
        RuntimeError(PRIVATE),
        _DerivedOwnerError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED),
    ],
)
def test_normal_cleanup_malformed_or_ordinary_failure_becomes_fixed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_result: object,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3, 0.4],
        discoveries=[None, _state(store)],
        acquire=owner,
        release=release_result,
    )
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(store, 1.0)
    _assert_election_error(captured.value, OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    assert _release_events(flow) == [("release", owner, None, None)]


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit, _ProcessControl])
def test_process_control_from_normal_cleanup_propagates_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    error = error_factory()
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3, 0.4],
        discoveries=[None, object.__new__(SidecarState)],
        acquire=owner,
        release=error,
    )
    flow.install(monkeypatch)
    with pytest.raises(error_factory) as captured:
        resolve_owner_election(store, 1.0)
    assert captured.value is error
    assert _release_events(flow) == [("release", owner, None, None)]


def test_exact_owner_error_from_normal_cleanup_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    cleanup_error = OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3, 0.4],
        discoveries=[None, _state(store)],
        acquire=owner,
        release=cleanup_error,
    )
    flow.install(monkeypatch)
    with pytest.raises(OwnerLockError) as captured:
        resolve_owner_election(store, 1.0)
    assert captured.value is cleanup_error
    assert _release_events(flow) == [("release", owner, None, None)]


@pytest.mark.parametrize("release_result", [None, True, RuntimeError(PRIVATE)])
def test_pending_error_yields_to_fixed_cleanup_failure_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_result: object,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3],
        discoveries=[None, object()],
        acquire=owner,
        release=release_result,
    )
    flow.install(monkeypatch)
    with pytest.raises(OwnerElectionError) as captured:
        resolve_owner_election(store, 1.0)
    escaped = _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    releases = _release_events(flow)
    assert len(releases) == 1
    assert releases[0][:3] == ("release", owner, OwnerElectionError)
    assert releases[0][3] is not escaped


def test_exact_owner_error_from_pending_cleanup_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    cleanup_error = OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3],
        discoveries=[None, object()],
        acquire=owner,
        release=cleanup_error,
    )
    flow.install(monkeypatch)
    with pytest.raises(OwnerLockError) as captured:
        resolve_owner_election(store, 1.0)
    assert captured.value is cleanup_error
    releases = _release_events(flow)
    assert len(releases) == 1
    assert releases[0][:3] == ("release", owner, OwnerElectionError)
    pending = releases[0][3]
    assert type(pending) is OwnerElectionError
    assert pending.code is OwnerElectionErrorCode.OWNER_ELECTION_FAILED


@pytest.mark.parametrize(
    "cleanup_outcome",
    [
        "released",
        "none",
        "true",
        "ordinary",
        "derived-owner-error",
        "owner-error",
        "process-control",
    ],
)
def test_pending_deadline_cleanup_precedence_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_outcome: str,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    cleanup_owner_error = OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)
    cleanup_derived_owner_error = _DerivedOwnerError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)
    cleanup_control = SystemExit()
    release_result: object
    if cleanup_outcome == "released":
        release_result = False
    elif cleanup_outcome == "none":
        release_result = None
    elif cleanup_outcome == "true":
        release_result = True
    elif cleanup_outcome == "ordinary":
        release_result = RuntimeError(PRIVATE)
    elif cleanup_outcome == "derived-owner-error":
        release_result = cleanup_derived_owner_error
    elif cleanup_outcome == "owner-error":
        release_result = cleanup_owner_error
    else:
        assert cleanup_outcome == "process-control"
        release_result = cleanup_control
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3, 1.0],
        discoveries=[None, None],
        acquire=owner,
        release=release_result,
    )
    flow.install(monkeypatch)

    expected_type: type[BaseException]
    if cleanup_outcome == "owner-error":
        expected_type = OwnerLockError
    elif cleanup_outcome == "process-control":
        expected_type = SystemExit
    else:
        expected_type = OwnerElectionError
    with pytest.raises(expected_type) as captured:
        resolve_owner_election(store, 1.0)

    releases = _release_events(flow)
    assert len(releases) == 1
    assert releases[0][:3] == ("release", owner, OwnerElectionError)
    pending = releases[0][3]
    assert type(pending) is OwnerElectionError
    assert pending.code is OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED
    if cleanup_outcome == "released":
        assert captured.value is pending
    elif cleanup_outcome in {"none", "true", "ordinary", "derived-owner-error"}:
        escaped = _assert_election_error(
            captured.value,
            OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
        )
        assert escaped is not pending
    elif cleanup_outcome == "owner-error":
        assert captured.value is cleanup_owner_error
    else:
        assert captured.value is cleanup_control


@pytest.mark.parametrize("active_factory", [KeyboardInterrupt, SystemExit, _ProcessControl])
@pytest.mark.parametrize(
    "release_kind",
    ["none", "true", "ordinary", "owner-error", "derived-owner-error"],
)
def test_process_control_survives_ordinary_or_malformed_cleanup_with_fixed_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_factory: type[BaseException],
    release_kind: str,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    active = active_factory()
    release_result: object
    if release_kind == "none":
        release_result = None
    elif release_kind == "true":
        release_result = True
    elif release_kind == "ordinary":
        release_result = RuntimeError(PRIVATE)
    elif release_kind == "owner-error":
        release_result = OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)
    else:
        assert release_kind == "derived-owner-error"
        release_result = _DerivedOwnerError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED)
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3],
        discoveries=[None, active],
        acquire=owner,
        release=release_result,
    )
    flow.install(monkeypatch)
    with pytest.raises(active_factory) as captured:
        resolve_owner_election(store, 1.0)
    assert captured.value is active
    assert getattr(captured.value, "__notes__", None) == ["sidecar owner lock cleanup failed"]
    assert _release_events(flow) == [("release", owner, active_factory, active)]


def test_malformed_existing_notes_cannot_replace_process_control_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    active = _ProcessControl()
    active.__notes__ = "malformed"  # type: ignore[assignment]
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3],
        discoveries=[None, active],
        acquire=owner,
        release=True,
    )
    flow.install(monkeypatch)
    with pytest.raises(_ProcessControl) as captured:
        resolve_owner_election(store, 1.0)
    assert captured.value is active
    assert captured.value.__notes__ == ["sidecar owner lock cleanup failed"]
    assert _release_events(flow) == [("release", owner, _ProcessControl, active)]


def test_cleanup_process_control_replaces_pending_and_active_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    for active in (object(), KeyboardInterrupt()):
        owner = _fake_owner()
        replacement = SystemExit()
        flow = _Flow(
            clock_values=[0.0, 0.1, 0.2, 0.3],
            discoveries=[None, active],
            acquire=owner,
            release=replacement,
        )
        flow.install(monkeypatch)
        with pytest.raises(SystemExit) as captured:
            resolve_owner_election(store, 1.0)
        assert captured.value is replacement
        releases = _release_events(flow)
        assert len(releases) == 1
        if isinstance(active, BaseException):
            assert releases == [("release", owner, type(active), active)]
        else:
            assert releases[0][:3] == ("release", owner, OwnerElectionError)
            pending = releases[0][3]
            assert type(pending) is OwnerElectionError
            assert pending.code is OwnerElectionErrorCode.OWNER_ELECTION_FAILED


def test_ordinary_failure_is_private_silent_and_not_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_error = RuntimeError(PRIVATE)
    flow = _Flow(clock_values=[0.0, 0.1], discoveries=[raw_error])
    with monkeypatch.context() as patcher:
        flow.install(patcher)
        with pytest.raises(OwnerElectionError) as captured:
            resolve_owner_election(_store(tmp_path), 1.0)
    _assert_election_error(captured.value, OwnerElectionErrorCode.OWNER_ELECTION_FAILED)
    assert captured.value.__context__ is None
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []
    _assert_module_does_not_retain_identity(raw_error)


def test_fixed_failure_suppresses_sensitive_caller_context_from_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _Flow(clock_values=[0.0, 0.1], discoveries=[object()])
    flow.install(monkeypatch)
    caller_error = RuntimeError(PRIVATE)
    try:
        raise caller_error
    except RuntimeError:
        with pytest.raises(OwnerElectionError) as captured:
            resolve_owner_election(_store(tmp_path), 1.0)
    escaped = _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    assert escaped.__context__ is caller_error
    assert escaped.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(escaped))
    assert PRIVATE not in formatted
    assert "OwnerElectionError" in formatted
    _assert_module_does_not_retain_identity(caller_error)


def test_caller_active_exception_does_not_cancel_normal_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    owner = _fake_owner()
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3, 0.4],
        discoveries=[None, None],
        acquire=owner,
    )
    flow.install(monkeypatch)
    try:
        raise LookupError("caller context")
    except LookupError:
        assert resolve_owner_election(store, 1.0) is owner
    assert _release_events(flow) == []


def test_success_results_and_caller_store_are_not_retained_after_seam_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent = _state(store)
    owner = _fake_owner()

    incumbent_flow = _Flow(clock_values=[0.0, 0.1, 0.2], discoveries=[incumbent])
    with monkeypatch.context() as patcher:
        incumbent_flow.install(patcher)
        assert resolve_owner_election(store, 1.0) is incumbent

    owner_flow = _Flow(
        clock_values=[0.0, 0.1, 0.2, 0.3, 0.4],
        discoveries=[None, None],
        acquire=owner,
    )
    with monkeypatch.context() as patcher:
        owner_flow.install(patcher)
        assert resolve_owner_election(store, 1.0) is owner

    for target in (store, incumbent, owner):
        _assert_module_does_not_retain_identity(target)


def test_frozen_direct_dispatch_ignores_later_public_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    assert election_module._DISCOVER_EXISTING_STARTUP is not None
    assert election_module._OWNER_LOCK_ACQUIRE.__self__ is OwnerLock
    assert election_module._OWNER_LOCK_ACQUIRE.__func__ is OwnerLock.acquire.__func__
    assert election_module._OWNER_LOCK_EXIT is OwnerLock.__exit__

    def replaced(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("later public replacement was called")

    monkeypatch.setattr(discovery_module, "discover_existing_startup", replaced)
    monkeypatch.setattr(OwnerLock, "acquire", classmethod(replaced))
    monkeypatch.setattr(OwnerLock, "__exit__", replaced)

    assert election_module._discover_startup(store, 0.1) is None
    owner = election_module._acquire_owner(store)
    assert type(owner) is OwnerLock
    assert election_module._exit_owner(owner, None, None) is False


def test_real_empty_store_returns_active_noninheritable_winner(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    winner = resolve_owner_election(store, 1.0)
    assert type(winner) is OwnerLock
    descriptor = winner.fileno()
    assert descriptor >= 3
    assert os.get_inheritable(descriptor) is False
    try:
        with pytest.raises(OwnerLockError) as captured:
            OwnerLock.acquire(store)
        assert captured.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD
    finally:
        winner.close()

    successor = OwnerLock.acquire(store)
    successor.close()


def test_real_preheld_owner_propagates_exact_contention_without_post_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent_owner = OwnerLock.acquire(store)
    calls: list[float] = []

    def no_incumbent(_store: StateStore, timeout: float) -> None:
        calls.append(timeout)
        return None

    try:
        monkeypatch.setattr(election_module, "_discover_startup", no_incumbent)
        with pytest.raises(OwnerLockError) as captured:
            resolve_owner_election(store, 1.0)
        assert type(captured.value) is OwnerLockError
        assert captured.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD
        assert len(calls) == 1
    finally:
        incumbent_owner.close()


def test_real_temporary_owner_is_closed_before_postlock_state_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent = _state(store)
    discoveries: list[SidecarState | None] = [None, incumbent]
    owner_box: list[OwnerLock] = []
    owner_descriptor = -1

    def discover(_store: StateStore, _timeout: float) -> SidecarState | None:
        return discoveries.pop(0)

    def acquire(target: StateStore) -> OwnerLock:
        nonlocal owner_descriptor
        owner = election_module._OWNER_LOCK_ACQUIRE(target)
        owner_descriptor = owner.fileno()
        owner_box.append(owner)
        return owner

    monkeypatch.setattr(election_module, "_discover_startup", discover)
    monkeypatch.setattr(election_module, "_acquire_owner", acquire)

    assert resolve_owner_election(store, 1.0) is incumbent
    assert len(owner_box) == 1
    assert owner_descriptor >= 3
    _assert_raw_descriptor_closed(owner_descriptor)
    successor = OwnerLock.acquire(store)
    successor.close()


def test_ambiguous_real_cleanup_never_retries_reused_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent = _state(store)
    discoveries: list[SidecarState | None] = [None, incumbent]
    owner_descriptor = -1
    replacement_descriptor = -1
    close_calls = 0
    real_try_close = owner_lock_module._try_close

    def discover(_store: StateStore, _timeout: float) -> SidecarState | None:
        return discoveries.pop(0)

    def acquire(target: StateStore) -> OwnerLock:
        nonlocal owner_descriptor
        owner = election_module._OWNER_LOCK_ACQUIRE(target)
        owner_descriptor = owner.fileno()
        return owner

    def ambiguous_close(file_descriptor: int) -> bool:
        nonlocal close_calls, replacement_descriptor
        if file_descriptor != owner_descriptor:
            return real_try_close(file_descriptor)
        close_calls += 1
        os.close(file_descriptor)
        replacement_descriptor = os.open(os.devnull, os.O_RDONLY)
        if replacement_descriptor != file_descriptor:
            promoted = os.dup2(replacement_descriptor, file_descriptor)
            os.close(replacement_descriptor)
            replacement_descriptor = promoted
        assert replacement_descriptor == file_descriptor
        return False

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(election_module, "_discover_startup", discover)
            patcher.setattr(election_module, "_acquire_owner", acquire)
            patcher.setattr(owner_lock_module, "_try_close", ambiguous_close)
            with pytest.raises(OwnerLockError) as captured:
                resolve_owner_election(store, 1.0)
        assert captured.value.code is OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED
        assert close_calls == 1
        assert replacement_descriptor == owner_descriptor
        assert os.fstat(replacement_descriptor).st_mode
        successor = OwnerLock.acquire(store)
        successor.close()
        assert os.fstat(replacement_descriptor).st_mode
    finally:
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)


def test_canonical_ambiguous_pending_cleanup_adds_only_fixed_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    discoveries: list[object] = [None, object()]
    calls = 0
    owner_descriptor = -1
    real_close = os.close
    real_try_close = owner_lock_module._try_close

    def discover(_store: StateStore, _timeout: float) -> object:
        return discoveries.pop(0)

    def acquire(target: StateStore) -> OwnerLock:
        nonlocal owner_descriptor
        owner = election_module._OWNER_LOCK_ACQUIRE(target)
        owner_descriptor = owner.fileno()
        return owner

    def fail_close(file_descriptor: int) -> bool:
        nonlocal calls
        if file_descriptor != owner_descriptor:
            return real_try_close(file_descriptor)
        calls += 1
        return False

    try:
        monkeypatch.setattr(election_module, "_discover_startup", discover)
        monkeypatch.setattr(election_module, "_acquire_owner", acquire)
        monkeypatch.setattr(owner_lock_module, "_try_close", fail_close)
        with pytest.raises(OwnerElectionError) as captured:
            resolve_owner_election(store, 1.0)
        _assert_election_error(
            captured.value,
            OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
            notes=["sidecar owner lock cleanup failed"],
        )
        assert calls == 1
    finally:
        if owner_descriptor >= 0:
            real_close(owner_descriptor)


def _attribute_path(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def test_production_ast_stays_inside_one_shot_election_boundary() -> None:
    source_path = Path(election_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert imports == {
        "__future__",
        "enum",
        "health",
        "math",
        "owner_lock",
        "startup_discovery",
        "state",
        "time",
        "typing",
    }

    forbidden_nodes = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
        ast.comprehension,
        ast.Global,
        ast.Nonlocal,
        ast.Delete,
        ast.AugAssign,
        ast.NamedExpr,
        ast.Lambda,
    )
    forbidden_calls = {
        "Popen",
        "adopt_inherited",
        "bind",
        "close",
        "connect",
        "eval",
        "exec",
        "fileno",
        "flock",
        "from_wire",
        "getattr",
        "globals",
        "kill",
        "listen",
        "load",
        "locals",
        "open",
        "open_startup_channel",
        "poll",
        "print",
        "publish",
        "remove",
        "remove_if_owned",
        "send",
        "sleep",
        "spawn",
        "start",
        "terminate",
        "to_wire",
        "vars",
        "wait",
    }
    observed_calls: list[tuple[str | None, ast.Call]] = []
    for node in ast.walk(tree):
        assert not isinstance(node, forbidden_nodes)
        if isinstance(node, ast.Call):
            receiver = _attribute_path(node.func)
            observed_calls.append((receiver, node))
            if receiver is None:
                assert isinstance(node.func, ast.Attribute)
                assert node.func.attr == "__init__"
                assert isinstance(node.func.value, ast.Call)
                assert isinstance(node.func.value.func, ast.Name)
                assert node.func.value.func.id == "super"
                continue
            assert receiver.rsplit(".", 1)[-1] not in forbidden_calls
            assert receiver not in {
                "discover_existing_startup",
                "OwnerLock.acquire",
                "OwnerLock.__exit__",
            }
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            assert isinstance(node.value, ast.Name)
            assert (node.value.id, node.attr) == ("self", "code")
        if isinstance(node, ast.Attribute):
            assert node.attr != "__traceback__"
        if isinstance(node, ast.Subscript):
            assert not isinstance(node.ctx, (ast.Store, ast.Del))

    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert [node.name for node in functions if not node.name.startswith("_")] == [
        "resolve_owner_election"
    ]
    assert [node.name for node in tree.body if isinstance(node, ast.ClassDef)] == [
        "OwnerElectionErrorCode",
        "OwnerElectionError",
    ]
    for function in functions:
        defaults = [*function.args.defaults, *function.args.kw_defaults]
        assert not any(
            isinstance(default, (ast.List, ast.Dict, ast.Set))
            for default in defaults
            if default is not None
        )

    module_assignments = [
        node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
    ]
    mutable_literals = (
        ast.List,
        ast.Dict,
        ast.Set,
        ast.ListComp,
        ast.DictComp,
        ast.SetComp,
        ast.GeneratorExp,
    )
    for assignment in module_assignments:
        assert not isinstance(assignment, ast.AugAssign)
        if isinstance(assignment, ast.Assign):
            assert len(assignment.targets) == 1
            assert isinstance(assignment.targets[0], ast.Name)
            assert assignment.targets[0].id == "_ReleaseStatus"
            assigned_value = assignment.value
        else:
            assert isinstance(assignment, ast.AnnAssign)
            assert isinstance(assignment.target, ast.Name)
            assert _attribute_path(assignment.annotation) == "Final"
            assert assignment.value is not None
            assigned_value = assignment.value
        assert not any(
            isinstance(descendant, mutable_literals) for descendant in ast.walk(assigned_value)
        )

    bindings = {
        statement.target.id: _attribute_path(statement.value)
        for statement in tree.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id
        in {
            "_DISCOVER_EXISTING_STARTUP",
            "_OWNER_LOCK_ACQUIRE",
            "_OWNER_LOCK_EXIT",
        }
    }
    assert bindings == {
        "_DISCOVER_EXISTING_STARTUP": "discover_existing_startup",
        "_OWNER_LOCK_ACQUIRE": "OwnerLock.acquire",
        "_OWNER_LOCK_EXIT": "OwnerLock.__exit__",
    }
    dispatches: list[tuple[str, str]] = []
    for function in functions:
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            receiver = _attribute_path(node.func)
            if receiver in bindings:
                dispatches.append((function.name, receiver))
    assert len(dispatches) == 3
    assert set(dispatches) == {
        ("_discover_startup", "_DISCOVER_EXISTING_STARTUP"),
        ("_acquire_owner", "_OWNER_LOCK_ACQUIRE"),
        ("_exit_owner", "_OWNER_LOCK_EXIT"),
    }
    exit_calls = [call for receiver, call in observed_calls if receiver == "_OWNER_LOCK_EXIT"]
    assert len(exit_calls) == 1
    exit_call = exit_calls[0]
    assert len(exit_call.args) == 4
    assert isinstance(exit_call.args[3], ast.Constant)
    assert exit_call.args[3].value is None
    assert exit_call.keywords == []
