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


class _MalformedValueInvoked(BaseException):
    pass


class _UnexpectedCollaboratorCall(BaseException):
    pass


class _OpaqueMalformed:
    @staticmethod
    def _invoked(protocol: str) -> NoReturn:
        raise _MalformedValueInvoked(f"malformed value protocol invoked: {protocol}")

    def __bool__(self) -> NoReturn:
        self._invoked("bool")

    def __eq__(self, other: object) -> NoReturn:
        del other
        self._invoked("equality")

    def __repr__(self) -> NoReturn:
        self._invoked("repr")

    def __getitem__(self, key: object) -> NoReturn:
        del key
        self._invoked("item access")

    def __iter__(self) -> NoReturn:
        self._invoked("iteration")

    def __hash__(self) -> NoReturn:
        self._invoked("hash")

    def __len__(self) -> NoReturn:
        self._invoked("length")

    def __contains__(self, item: object) -> NoReturn:
        del item
        self._invoked("containment")

    def __lt__(self, other: object) -> NoReturn:
        del other
        self._invoked("ordering")

    def __le__(self, other: object) -> NoReturn:
        del other
        self._invoked("ordering")

    def __gt__(self, other: object) -> NoReturn:
        del other
        self._invoked("ordering")

    def __ge__(self, other: object) -> NoReturn:
        del other
        self._invoked("ordering")

    def __str__(self) -> NoReturn:
        self._invoked("string conversion")

    def __format__(self, format_spec: str) -> NoReturn:
        del format_spec
        self._invoked("formatting")

    def __index__(self) -> NoReturn:
        self._invoked("index conversion")

    def __int__(self) -> NoReturn:
        self._invoked("integer conversion")

    def __float__(self) -> NoReturn:
        self._invoked("float conversion")


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
            raise _UnexpectedCollaboratorCall("unexpected extra monotonic read")
        return _resolve(self.clock_values.pop(0))

    def discover(self, store: StateStore, timeout: float) -> object:
        self.events.append(("discover", store, timeout))
        if not self.discoveries:
            raise _UnexpectedCollaboratorCall("unexpected extra discovery")
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
        raise _UnexpectedCollaboratorCall("unexpected collaborator call")
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


@pytest.mark.parametrize(
    "invalid",
    [None, object(), "code", True, pytest.param(_OpaqueMalformed(), id="opaque")],
)
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
        pytest.param(
            _OpaqueMalformed(),
            TypeError,
            "store must be an exact StateStore",
            id="opaque",
        ),
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
        pytest.param(
            _OpaqueMalformed(),
            TypeError,
            "timeout must be a built-in int or float",
            id="opaque",
        ),
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


@pytest.mark.parametrize(
    "malformed",
    [False, 0, object(), pytest.param(_OpaqueMalformed(), id="opaque")],
)
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


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        object(),
        object.__new__(_DerivedOwner),
        pytest.param(_OpaqueMalformed(), id="opaque"),
    ],
)
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


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(PRIVATE),
        False,
        object(),
        pytest.param(_OpaqueMalformed(), id="opaque"),
    ],
)
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
    [
        OSError(PRIVATE),
        True,
        1,
        math.nan,
        math.inf,
        -math.inf,
        pytest.param(_OpaqueMalformed(), id="opaque"),
    ],
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

    top_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert {id(node) for node in top_level_imports} == {
        id(node) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    imported_names: list[tuple[str, int, str | None, str, str | None]] = []
    for node in top_level_imports:
        if isinstance(node, ast.Import):
            imported_names.extend(
                ("import", 0, None, alias.name, alias.asname) for alias in node.names
            )
        else:
            imported_names.extend(
                ("from", node.level, node.module, alias.name, alias.asname) for alias in node.names
            )
    assert len(imported_names) == len(set(imported_names))
    assert set(imported_names) <= {
        ("from", 0, "__future__", "annotations", None),
        ("import", 0, None, "math", None),
        ("import", 0, None, "time", None),
        ("from", 0, "enum", "StrEnum", None),
        ("from", 0, "typing", "Final", None),
        ("from", 0, "typing", "Literal", None),
        ("from", 0, "typing", "NoReturn", None),
        ("from", 0, "typing", "cast", None),
        ("from", 1, "health", "MAX_HEALTH_PROBE_TIMEOUT_SECONDS", None),
        ("from", 1, "owner_lock", "OwnerLock", None),
        ("from", 1, "owner_lock", "OwnerLockError", None),
        ("from", 1, "startup_discovery", "discover_existing_startup", None),
        ("from", 1, "state", "SidecarState", None),
        ("from", 1, "state", "StateStore", None),
    }

    structural_functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    structural_function_names = {node.name for node in structural_functions}
    assert len(structural_functions) == len(structural_function_names)
    assert structural_function_names <= {
        "_validate_timeout",
        "_discover_startup",
        "_acquire_owner",
        "_exit_owner",
        "_read_monotonic",
        "_observe_monotonic",
        "_remaining",
        "_new_error",
        "_raise_error",
        "_attempt_discovery",
        "_attempt_release",
        "_release_normal",
        "_release_pending",
        "_release_process_control",
        "resolve_owner_election",
    }
    assert {
        "_discover_startup",
        "_acquire_owner",
        "_exit_owner",
        "_read_monotonic",
        "resolve_owner_election",
    } <= structural_function_names
    structural_classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert len(structural_classes) == 2
    assert {node.name for node in structural_classes} == {
        "OwnerElectionErrorCode",
        "OwnerElectionError",
    }
    classes_by_name = {node.name: node for node in structural_classes}
    error_code_class = classes_by_name["OwnerElectionErrorCode"]
    error_class = classes_by_name["OwnerElectionError"]
    assert [_attribute_path(base) for base in error_code_class.bases] == ["StrEnum"]
    assert [_attribute_path(base) for base in error_class.bases] == ["RuntimeError"]
    assert error_code_class.keywords == []
    assert error_class.keywords == []
    code_bindings = [node for node in error_code_class.body if isinstance(node, ast.Assign)]
    assert len(code_bindings) == 2
    assert {
        node.targets[0].id
        for node in code_bindings
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    } == {
        "OWNER_ELECTION_DEADLINE_FAILED",
        "OWNER_ELECTION_FAILED",
    }
    for binding in code_bindings:
        assert len(binding.targets) == 1
        assert isinstance(binding.targets[0], ast.Name)
        assert isinstance(binding.value, ast.Constant)
        assert binding.value.value == binding.targets[0].id
    assert all(isinstance(node, (ast.Expr, ast.Assign)) for node in error_code_class.body)
    error_methods = [node for node in error_class.body if isinstance(node, ast.FunctionDef)]
    assert [node.name for node in error_methods] == ["__init__"]
    assert all(isinstance(node, (ast.Expr, ast.FunctionDef)) for node in error_class.body)
    assert {id(node) for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)} == {
        id(node) for node in [*structural_functions, *error_methods]
    }
    assert {id(node) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)} == {
        id(node) for node in structural_classes
    }
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree))
    for definition in [*structural_functions, *error_methods, *structural_classes]:
        assert definition.decorator_list == []

    structural_assignments: list[ast.Assign | ast.AnnAssign] = []
    assignment_names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            assert isinstance(node.target, ast.Name)
            assert node.value is not None
            structural_assignments.append(node)
            assignment_names.append(node.target.id)
        elif isinstance(node, ast.Assign):
            assert len(node.targets) == 1
            assert isinstance(node.targets[0], ast.Name)
            structural_assignments.append(node)
            assignment_names.append(node.targets[0].id)
    assert len(assignment_names) == len(set(assignment_names))
    assert set(assignment_names) <= {
        "_STATE_STORE_TYPE",
        "_SIDECAR_STATE_TYPE",
        "_OWNER_LOCK_TYPE",
        "_OWNER_LOCK_ERROR_TYPE",
        "_DISCOVER_EXISTING_STARTUP",
        "_OWNER_LOCK_ACQUIRE",
        "_OWNER_LOCK_EXIT",
        "_OWNER_CLEANUP_NOTE",
        "_ReleaseStatus",
    }
    assert {
        "_DISCOVER_EXISTING_STARTUP",
        "_OWNER_LOCK_ACQUIRE",
        "_OWNER_LOCK_EXIT",
    } <= set(assignment_names)
    assignment_by_name = dict(zip(assignment_names, structural_assignments, strict=True))
    expected_binding_paths = {
        "_STATE_STORE_TYPE": "StateStore",
        "_SIDECAR_STATE_TYPE": "SidecarState",
        "_OWNER_LOCK_TYPE": "OwnerLock",
        "_OWNER_LOCK_ERROR_TYPE": "OwnerLockError",
        "_DISCOVER_EXISTING_STARTUP": "discover_existing_startup",
        "_OWNER_LOCK_ACQUIRE": "OwnerLock.acquire",
        "_OWNER_LOCK_EXIT": "OwnerLock.__exit__",
    }
    for name, expected_path in expected_binding_paths.items():
        if name not in assignment_by_name:
            continue
        assignment = assignment_by_name[name]
        assert isinstance(assignment, ast.AnnAssign)
        assert isinstance(assignment.annotation, ast.Name)
        assert assignment.annotation.id == "Final"
        assert assignment.value is not None
        assert _attribute_path(assignment.value) == expected_path
    if "_OWNER_CLEANUP_NOTE" in assignment_by_name:
        note_assignment = assignment_by_name["_OWNER_CLEANUP_NOTE"]
        assert isinstance(note_assignment, ast.AnnAssign)
        assert isinstance(note_assignment.annotation, ast.Name)
        assert note_assignment.annotation.id == "Final"
        assert isinstance(note_assignment.value, ast.Constant)
        assert note_assignment.value.value == "sidecar owner lock cleanup failed"
    if "_ReleaseStatus" in assignment_by_name:
        release_status_assignment = assignment_by_name["_ReleaseStatus"]
        assert isinstance(release_status_assignment, ast.Assign)
        assert isinstance(release_status_assignment.value, ast.Subscript)
        assert isinstance(release_status_assignment.value.value, ast.Name)
        assert release_status_assignment.value.value.id == "Literal"
        assert isinstance(release_status_assignment.value.slice, ast.Tuple)
        release_status_values = release_status_assignment.value.slice.elts
        assert all(isinstance(node, ast.Constant) for node in release_status_values)
        assert [node.value for node in release_status_values] == [
            "released",
            "owner_error",
            "failed",
        ]

    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)
    assert type(tree.body[0].value.value) is str
    allowed_top_level = {
        id(tree.body[0]),
        *(id(node) for node in top_level_imports),
        *(id(node) for node in structural_assignments),
        *(id(node) for node in structural_classes),
        *(id(node) for node in structural_functions),
    }
    assert {id(node) for node in tree.body} == allowed_top_level

    mutable_or_dynamic_nodes = (
        ast.Call,
        ast.List,
        ast.Dict,
        ast.Set,
        ast.ListComp,
        ast.DictComp,
        ast.SetComp,
        ast.GeneratorExp,
    )
    forbidden_structural_nodes = (
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
        ast.NamedExpr,
        ast.Lambda,
        ast.With,
        ast.AsyncWith,
        ast.Yield,
        ast.YieldFrom,
        ast.Await,
    )
    assert not any(isinstance(node, forbidden_structural_nodes) for node in ast.walk(tree))

    parent_by_id = {
        id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    raw_collaborator_names = {
        "incumbent",
        "candidate",
        "post_lock_result",
        "result",
        "owner",
    }
    assignment_targets = {
        "candidate": "owner",
        "post_lock_result": "post_lock_state",
        "owner": "result",
    }
    owner_call_targets = {
        "_OWNER_LOCK_EXIT",
        "_attempt_release",
        "_exit_owner",
        "_release_process_control",
        "_release_pending",
        "_release_normal",
    }
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Name)
            or not isinstance(node.ctx, ast.Load)
            or node.id not in raw_collaborator_names
        ):
            continue
        parent = parent_by_id[id(node)]
        if isinstance(parent, ast.Call):
            call_target = _attribute_path(parent.func)
            if node.id == "owner":
                assert call_target in owner_call_targets
            else:
                assert call_target == "type"
                assert parent.args == [node]
                assert parent.keywords == []
            continue
        if isinstance(parent, ast.Compare):
            assert len(parent.ops) == 1
            assert isinstance(parent.ops[0], (ast.Is, ast.IsNot))
            assert len(parent.comparators) == 1
            other = parent.comparators[0] if parent.left is node else parent.left
            if node.id == "result":
                assert (isinstance(other, ast.Constant) and other.value is False) or (
                    isinstance(other, ast.Name) and other.id == "owner"
                )
            elif node.id == "owner":
                assert isinstance(other, ast.Name)
                assert other.id == "result"
            else:
                assert isinstance(other, ast.Constant)
                assert other.value is None
            continue
        if isinstance(parent, ast.Assign):
            assert parent.value is node
            assert len(parent.targets) == 1
            assert isinstance(parent.targets[0], ast.Name)
            assert parent.targets[0].id == assignment_targets[node.id]
            continue
        if isinstance(parent, ast.Return):
            assert parent.value is node
            assert node.id in {"incumbent", "owner"}
            continue
        raise AssertionError(f"unsafe raw collaborator use: {node.id}")

    for assignment in structural_assignments:
        assigned_value = assignment.value
        assert assigned_value is not None
        assert not any(
            isinstance(descendant, mutable_or_dynamic_nodes)
            for descendant in ast.walk(assigned_value)
        )
    for function in [*structural_functions, *error_methods]:
        defaults = [*function.args.defaults, *function.args.kw_defaults]
        assert not any(
            isinstance(descendant, mutable_or_dynamic_nodes)
            for default in defaults
            if default is not None
            for descendant in ast.walk(default)
        )

    def normalized_call_path(call: ast.Call) -> str:
        receiver = _attribute_path(call.func)
        if receiver is not None:
            return receiver
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Call)
        assert _attribute_path(call.func.value.func) == "super"
        return f"super().{call.func.attr}"

    scopes: dict[str, ast.FunctionDef] = {
        **{node.name: node for node in structural_functions},
        "OwnerElectionError.__init__": error_methods[0],
    }
    permitted_calls = {
        "OwnerElectionError.__init__": {
            "type",
            "TypeError",
            "super",
            "super().__init__",
        },
        "_validate_timeout": {
            "type",
            "TypeError",
            "ValueError",
            "float",
            "cast",
            "math.isfinite",
        },
        "_discover_startup": {"_DISCOVER_EXISTING_STARTUP"},
        "_acquire_owner": {"_OWNER_LOCK_ACQUIRE"},
        "_exit_owner": {"_OWNER_LOCK_EXIT"},
        "_read_monotonic": {"time.monotonic"},
        "_observe_monotonic": {"_read_monotonic", "type", "math.isfinite"},
        "_remaining": {"_observe_monotonic", "math.isfinite"},
        "_new_error": {"OwnerElectionError"},
        "_raise_error": {"_new_error"},
        "_attempt_discovery": {"_discover_startup"},
        "_attempt_release": {"type", "_exit_owner"},
        "_release_normal": {
            "_attempt_release",
            "_exit_owner",
            "_raise_error",
            "OwnerElectionError",
        },
        "_release_pending": {
            "_attempt_release",
            "_exit_owner",
            "_raise_error",
            "OwnerElectionError",
        },
        "_release_process_control": {
            "_attempt_release",
            "_exit_owner",
            "BaseException.add_note",
            "BaseException.__setattr__",
        },
        "resolve_owner_election": {
            "type",
            "TypeError",
            "_validate_timeout",
            "_observe_monotonic",
            "_raise_error",
            "math.isfinite",
            "_remaining",
            "_attempt_discovery",
            "_discover_startup",
            "_acquire_owner",
            "_new_error",
            "OwnerElectionError",
            "_attempt_release",
            "_exit_owner",
            "BaseException.add_note",
            "BaseException.__setattr__",
            "_release_process_control",
            "_release_pending",
            "_release_normal",
        },
    }
    calls_by_scope: dict[str, list[str]] = {}
    scoped_calls: set[int] = set()
    for scope_name, scope in scopes.items():
        calls = [node for node in ast.walk(scope) if isinstance(node, ast.Call)]
        scoped_calls.update(id(node) for node in calls)
        calls_by_scope[scope_name] = [normalized_call_path(node) for node in calls]
        assert set(calls_by_scope[scope_name]) <= permitted_calls[scope_name]
    assert scoped_calls == {id(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    all_call_paths = [path for paths in calls_by_scope.values() for path in paths]
    assert all_call_paths.count("_DISCOVER_EXISTING_STARTUP") == 1
    assert all_call_paths.count("_OWNER_LOCK_ACQUIRE") == 1
    assert all_call_paths.count("_OWNER_LOCK_EXIT") == 1
    assert all_call_paths.count("_discover_startup") <= 1
    assert all_call_paths.count("_acquire_owner") == 1
    assert all_call_paths.count("_exit_owner") <= 1
    assert all_call_paths.count("_attempt_discovery") <= 2
    assert all_call_paths.count("_attempt_release") <= 3
    assert all_call_paths.count("_release_normal") <= 1
    assert all_call_paths.count("_release_pending") <= 2
    assert all_call_paths.count("_release_process_control") <= 1

    discovery_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and normalized_call_path(node) == "_DISCOVER_EXISTING_STARTUP"
    ]
    assert len(discovery_calls) == 1
    discovery_call = discovery_calls[0]
    assert len(discovery_call.args) == 2
    assert [argument.id for argument in discovery_call.args if isinstance(argument, ast.Name)] == [
        "store",
        "timeout",
    ]
    assert discovery_call.keywords == []

    acquire_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and normalized_call_path(node) == "_OWNER_LOCK_ACQUIRE"
    ]
    assert len(acquire_calls) == 1
    acquire_call = acquire_calls[0]
    assert len(acquire_call.args) == 1
    assert isinstance(acquire_call.args[0], ast.Name)
    assert acquire_call.args[0].id == "store"
    assert acquire_call.keywords == []

    clock_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and normalized_call_path(node) == "time.monotonic"
    ]
    assert len(clock_calls) == 1
    assert clock_calls[0].args == []
    assert clock_calls[0].keywords == []

    exit_calls = [
        node
        for node in ast.walk(scopes["_exit_owner"])
        if isinstance(node, ast.Call) and normalized_call_path(node) == "_OWNER_LOCK_EXIT"
    ]
    assert len(exit_calls) == 1
    exit_call = exit_calls[0]
    assert len(exit_call.args) == 4
    assert [argument.id for argument in exit_call.args[:3] if isinstance(argument, ast.Name)] == [
        "owner",
        "exception_type",
        "exception",
    ]
    assert isinstance(exit_call.args[3], ast.Constant)
    assert exit_call.args[3].value is None
    assert exit_call.keywords == []

    add_note_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and normalized_call_path(node) == "BaseException.add_note"
    ]
    assert len(add_note_calls) == 1
    add_note_call = add_note_calls[0]
    assert len(add_note_call.args) == 2
    assert [argument.id for argument in add_note_call.args if isinstance(argument, ast.Name)] == [
        "active_error",
        "_OWNER_CLEANUP_NOTE",
    ]
    assert add_note_call.keywords == []

    set_notes_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and normalized_call_path(node) == "BaseException.__setattr__"
    ]
    assert len(set_notes_calls) == 1
    set_notes_call = set_notes_calls[0]
    assert len(set_notes_call.args) == 3
    assert isinstance(set_notes_call.args[0], ast.Name)
    assert set_notes_call.args[0].id == "active_error"
    assert isinstance(set_notes_call.args[1], ast.Constant)
    assert set_notes_call.args[1].value == "__notes__"
    assert isinstance(set_notes_call.args[2], ast.List)
    assert len(set_notes_call.args[2].elts) == 1
    assert isinstance(set_notes_call.args[2].elts[0], ast.Name)
    assert set_notes_call.args[2].elts[0].id == "_OWNER_CLEANUP_NOTE"
    assert set_notes_call.keywords == []

    def normalized_attribute_path(attribute: ast.Attribute) -> str:
        receiver = _attribute_path(attribute)
        if receiver is not None:
            return receiver
        assert isinstance(attribute.value, ast.Call)
        assert _attribute_path(attribute.value.func) == "super"
        return f"super().{attribute.attr}"

    load_attributes = {
        normalized_attribute_path(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }
    assert load_attributes <= {
        "OwnerLock.acquire",
        "OwnerLock.__exit__",
        "math.isfinite",
        "time.monotonic",
        "OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED",
        "OwnerElectionErrorCode.OWNER_ELECTION_FAILED",
        "BaseException.add_note",
        "BaseException.__setattr__",
        "super().__init__",
    }
    stored_attributes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    ]
    assert len(stored_attributes) == 1
    assert _attribute_path(stored_attributes[0]) == "self.code"
    assert not any(
        isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(tree)
    )
