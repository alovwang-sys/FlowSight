from __future__ import annotations

import ast
import gc
import inspect
import math
import threading
import time
import traceback
import weakref
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
    wait_for_owner_election,
)
from flowsight.sidecar import startup_wait as wait_module

PROJECT_ID = "wait-project"
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


class _DerivedOwnerLockError(OwnerLockError):
    pass


class _DerivedOwnerElectionError(OwnerElectionError):
    pass


class _OpaqueDerivedOwnerLockError(OwnerLockError):
    def __getattribute__(self, name: str) -> object:
        if name == "code":
            raise _MalformedValueInvoked("derived owner error code accessed")
        return super().__getattribute__(name)


class _OpaqueDerivedOwnerElectionError(OwnerElectionError):
    def __getattribute__(self, name: str) -> object:
        if name == "code":
            raise _MalformedValueInvoked("derived election error code accessed")
        return super().__getattribute__(name)


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

    def __add__(self, other: object) -> NoReturn:
        del other
        self._invoked("addition")

    def __radd__(self, other: object) -> NoReturn:
        del other
        self._invoked("reflected addition")

    def __sub__(self, other: object) -> NoReturn:
        del other
        self._invoked("subtraction")

    def __rsub__(self, other: object) -> NoReturn:
        del other
        self._invoked("reflected subtraction")

    def __mul__(self, other: object) -> NoReturn:
        del other
        self._invoked("multiplication")

    def __rmul__(self, other: object) -> NoReturn:
        del other
        self._invoked("reflected multiplication")

    def __truediv__(self, other: object) -> NoReturn:
        del other
        self._invoked("division")

    def __rtruediv__(self, other: object) -> NoReturn:
        del other
        self._invoked("reflected division")

    def __call__(self, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        self._invoked("call")

    def __getattr__(self, name: str) -> NoReturn:
        del name
        self._invoked("attribute access")

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
        election_values: list[object],
        wait_values: list[object] | None = None,
    ) -> None:
        self.clock_values = list(clock_values)
        self.election_values = list(election_values)
        self.wait_values = list(wait_values or [])
        self.events: list[tuple[object, ...]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wait_module, "_read_monotonic", self.read_clock)
        monkeypatch.setattr(wait_module, "_resolve_owner_election", self.elect)
        monkeypatch.setattr(wait_module, "_wait_interval", self.wait)

    def read_clock(self) -> object:
        self.events.append(("clock",))
        if not self.clock_values:
            raise _UnexpectedCollaboratorCall("unexpected extra monotonic read")
        return _resolve(self.clock_values.pop(0))

    def elect(self, store: StateStore, timeout: float) -> object:
        if type(timeout) is not float or not math.isfinite(timeout) or timeout <= 0.0:
            raise _UnexpectedCollaboratorCall("election received an invalid retry budget")
        self.events.append(("elect", store, timeout))
        if not self.election_values:
            raise _UnexpectedCollaboratorCall("unexpected extra election")
        return _resolve(self.election_values.pop(0))

    def wait(self, interval: float) -> object:
        if type(interval) is not float or not math.isfinite(interval) or interval <= 0.0:
            raise _UnexpectedCollaboratorCall("wait received an invalid interval")
        self.events.append(("wait", interval))
        if not self.wait_values:
            raise _UnexpectedCollaboratorCall("unexpected extra wait")
        return _resolve(self.wait_values.pop(0))


def _resolve(value: object) -> object:
    if value is _UNEXPECTED:
        raise _UnexpectedCollaboratorCall("unexpected collaborator call")
    if isinstance(value, BaseException):
        raise value
    return value


def _store(tmp_path: Path, suffix: str = "runtime") -> StateStore:
    return StateStore(tmp_path / suffix, project_id=PROJECT_ID)


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


def _held() -> OwnerLockError:
    return OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_HELD)


def _event_names(flow: _Flow) -> list[object]:
    return [event[0] for event in flow.events]


def _assert_election_error(
    error: BaseException,
    code: OwnerElectionErrorCode,
    *,
    expected_context: BaseException | None = None,
) -> OwnerElectionError:
    assert type(error) is OwnerElectionError
    election_error = error
    assert election_error.code is code
    message = f"sidecar owner election failed ({code})"
    assert str(election_error) == message
    assert election_error.args == (message,)
    assert election_error.__cause__ is None
    assert election_error.__context__ is expected_context
    assert election_error.__suppress_context__ is True
    assert getattr(election_error, "__notes__", None) is None
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
    for name, value in vars(wait_module).items():
        if name != "__builtins__":
            assert not _builtins_retain_identity(value, target, set()), name


def test_public_shape_and_export_are_exact() -> None:
    assert sidecar_package.wait_for_owner_election is wait_for_owner_election
    assert sidecar_package.__all__.count("wait_for_owner_election") == 1

    signature = inspect.signature(wait_for_owner_election)
    assert tuple(signature.parameters) == ("store", "timeout")
    assert signature.parameters["store"].default is inspect.Parameter.empty
    assert signature.parameters["timeout"].default == 0.5
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in signature.parameters.values()
    )
    assert get_type_hints(wait_for_owner_election) == {
        "store": StateStore,
        "timeout": float,
        "return": SidecarState | OwnerLock,
    }

    assert tuple(OwnerElectionErrorCode) == (
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    assert tuple(OwnerLockErrorCode) == (
        OwnerLockErrorCode.OWNER_LOCK_HELD,
        OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED,
        OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID,
        OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED,
        OwnerLockErrorCode.OWNER_LOCK_CLOSED,
    )
    public_wait_names = [name for name in sidecar_package.__all__ if "wait" in name.lower()]
    assert public_wait_names == ["wait_for_owner_election"]


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        object(),
        pytest.param(_OpaqueMalformed(), id="opaque"),
    ],
)
def test_store_preflight_precedes_clock_election_and_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    flow = _Flow(clock_values=[], election_values=[])
    flow.install(monkeypatch)

    with pytest.raises(TypeError, match="^store must be an exact StateStore$"):
        wait_for_owner_election(invalid)  # type: ignore[arg-type]
    assert flow.events == []

    derived = _DerivedStore(tmp_path / "derived", project_id=PROJECT_ID)
    with pytest.raises(TypeError, match="^store must be an exact StateStore$"):
        wait_for_owner_election(derived)
    assert flow.events == []


@pytest.mark.parametrize(
    ("invalid", "error_type", "message"),
    [
        (None, TypeError, "timeout must be a built-in int or float"),
        (True, TypeError, "timeout must be a built-in int or float"),
        (_DerivedInt(1), TypeError, "timeout must be a built-in int or float"),
        (_DerivedFloat(1.0), TypeError, "timeout must be a built-in int or float"),
        (0, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (31, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (10**1000, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (-1.0, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (30.0001, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (math.nan, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (math.inf, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (-math.inf, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        pytest.param(
            _OpaqueMalformed(),
            TypeError,
            "timeout must be a built-in int or float",
            id="opaque",
        ),
    ],
)
def test_timeout_preflight_precedes_clock_election_and_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
    error_type: type[Exception],
    message: str,
) -> None:
    flow = _Flow(clock_values=[], election_values=[])
    flow.install(monkeypatch)

    with pytest.raises(error_type) as captured:
        wait_for_owner_election(_store(tmp_path), invalid)  # type: ignore[arg-type]
    assert str(captured.value) == message
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert flow.events == []


@pytest.mark.parametrize("timeout", [1, 1.0, 30, 30.0, pytest.param(5e-324, id="min-subnormal")])
@pytest.mark.parametrize("result_kind", ["state", "owner"])
def test_first_election_runs_immediately_and_returns_exact_result_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: int | float,
    result_kind: str,
) -> None:
    store = _store(tmp_path)
    result: SidecarState | OwnerLock
    if result_kind == "state":
        result = object.__new__(SidecarState)
    else:
        result = _fake_owner()
    started = 0.0 if float(timeout) == 5e-324 else 10.0
    flow = _Flow(clock_values=[started], election_values=[result])
    flow.install(monkeypatch)

    assert wait_for_owner_election(store, timeout) is result
    assert flow.events == [
        ("clock",),
        ("elect", store, float(timeout)),
    ]
    assert flow.clock_values == []
    assert flow.election_values == []


def test_successful_attempt_admitted_before_outer_deadline_has_no_outer_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    winner = _fake_owner()
    completed_after_deadline = False
    simulated_now = 100.0
    events: list[tuple[object, ...]] = []

    def read_clock() -> object:
        events.append(("clock",))
        return simulated_now

    def elect(target: StateStore, timeout: float) -> object:
        nonlocal completed_after_deadline, simulated_now
        events.append(("elect", target, timeout))
        simulated_now = 101.001
        completed_after_deadline = True
        return winner

    monkeypatch.setattr(wait_module, "_read_monotonic", read_clock)
    monkeypatch.setattr(wait_module, "_resolve_owner_election", elect)
    monkeypatch.setattr(
        wait_module,
        "_wait_interval",
        lambda interval: (_ for _ in ()).throw(
            _UnexpectedCollaboratorCall(f"unexpected wait: {interval}")
        ),
    )

    assert wait_for_owner_election(store, 1.0) is winner
    assert completed_after_deadline is True
    assert simulated_now > 101.0
    assert events == [
        ("clock",),
        ("elect", store, pytest.approx(1.0)),
    ]


@pytest.mark.parametrize("result_kind", ["state", "owner"])
def test_one_contention_waits_then_returns_exact_retry_result_with_reused_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_kind: str,
) -> None:
    store = _store(tmp_path)
    result: SidecarState | OwnerLock
    if result_kind == "state":
        result = object.__new__(SidecarState)
    else:
        result = _fake_owner()
    flow = _Flow(
        clock_values=[10.0, 10.2, 10.225],
        election_values=[_held(), result],
        wait_values=[None],
    )
    flow.install(monkeypatch)

    assert wait_for_owner_election(store, 1.0) is result
    assert flow.events == [
        ("clock",),
        ("elect", store, pytest.approx(1.0)),
        ("clock",),
        ("wait", pytest.approx(0.025)),
        ("clock",),
        ("elect", store, pytest.approx(0.775)),
    ]
    assert flow.clock_values == []


def test_state_published_during_contention_is_returned_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent = _state(store)
    flow = _Flow(
        clock_values=[0.0, 0.005, 0.031],
        election_values=[_held(), incumbent],
        wait_values=[None],
    )
    flow.install(monkeypatch)

    assert wait_for_owner_election(store, 0.5) is incumbent
    assert flow.events == [
        ("clock",),
        ("elect", store, pytest.approx(0.5)),
        ("clock",),
        ("wait", pytest.approx(0.025)),
        ("clock",),
        ("elect", store, pytest.approx(0.469)),
    ]


def test_each_iteration_recomputes_budget_and_caps_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    winner = _fake_owner()
    flow = _Flow(
        clock_values=[20.0, 20.2, 20.226, 20.3, 20.326],
        election_values=[_held(), _held(), winner],
        wait_values=[None, None],
    )
    flow.install(monkeypatch)

    assert wait_for_owner_election(store, 1.0) is winner
    assert flow.events == [
        ("clock",),
        ("elect", store, pytest.approx(1.0)),
        ("clock",),
        ("wait", pytest.approx(0.025)),
        ("clock",),
        ("elect", store, pytest.approx(0.774)),
        ("clock",),
        ("wait", pytest.approx(0.025)),
        ("clock",),
        ("elect", store, pytest.approx(0.674)),
    ]


def test_final_poll_is_capped_to_current_positive_remaining_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    flow = _Flow(
        clock_values=[0.0, 0.09, 0.1],
        election_values=[_held()],
        wait_values=[None],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(store, 0.1)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert flow.events == [
        ("clock",),
        ("elect", store, pytest.approx(0.1)),
        ("clock",),
        ("wait", pytest.approx(0.01)),
        ("clock",),
    ]


@pytest.mark.parametrize(
    ("post_wait", "expected_names"),
    [
        (0.01, ["clock", "elect", "clock", "wait", "clock"]),
        (0.02, ["clock", "elect", "clock", "wait", "clock"]),
        (0.029999, ["clock", "elect", "clock", "wait", "clock"]),
    ],
    ids=["no-progress", "partial-progress", "almost-full-interval"],
)
def test_wait_must_prove_the_full_requested_interval_without_busy_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_wait: float,
    expected_names: list[str],
) -> None:
    store = _store(tmp_path)
    flow = _Flow(
        clock_values=[0.0, 0.005, post_wait],
        election_values=[_held()],
        wait_values=[None],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(store, 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert _event_names(flow) == expected_names


@pytest.mark.parametrize(
    "post_second_wait",
    [0.2, 0.21, 0.224999],
    ids=["no-progress", "partial-progress", "almost-full-interval"],
)
def test_every_repeated_wait_must_prove_its_full_requested_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_second_wait: float,
) -> None:
    store = _store(tmp_path)
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.126, 0.2, post_second_wait],
        election_values=[_held(), _held()],
        wait_values=[None, None],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(store, 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert _event_names(flow) == [
        "clock",
        "elect",
        "clock",
        "wait",
        "clock",
        "elect",
        "clock",
        "wait",
        "clock",
    ]


def test_repeated_contention_expires_instead_of_returning_last_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _held()
    second = _held()
    flow = _Flow(
        clock_values=[0.0, 0.01, 0.035, 0.09, 0.1],
        election_values=[first, second],
        wait_values=[None, None],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(store, 0.1)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert flow.events == [
        ("clock",),
        ("elect", store, pytest.approx(0.1)),
        ("clock",),
        ("wait", pytest.approx(0.025)),
        ("clock",),
        ("elect", store, pytest.approx(0.065)),
        ("clock",),
        ("wait", pytest.approx(0.01)),
        ("clock",),
    ]
    assert captured.value is not first
    assert captured.value is not second


@pytest.mark.parametrize(
    "error",
    [
        OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED),
        OwnerLockError(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID),
        OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED),
        OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLOSED),
        OwnerElectionError(OwnerElectionErrorCode.OWNER_ELECTION_FAILED),
        OwnerElectionError(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED),
    ],
)
def test_exact_noncontention_domain_errors_preserve_full_identity_and_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OwnerLockError | OwnerElectionError,
) -> None:
    store = _store(tmp_path)
    cause = RuntimeError("safe exact cause")
    context = ValueError("safe exact context")
    error.__cause__ = cause
    error.__context__ = context
    error.__suppress_context__ = True
    flow = _Flow(clock_values=[0.0], election_values=[error])
    flow.install(monkeypatch)

    with pytest.raises(type(error)) as captured:
        wait_for_owner_election(store, 1.0)
    assert captured.value is error
    assert captured.value.code is error.code
    assert captured.value.args == error.args
    assert captured.value.__cause__ is cause
    assert captured.value.__context__ is context
    assert captured.value.__suppress_context__ is True
    assert _event_names(flow) == ["clock", "elect"]


@pytest.mark.parametrize(
    "error",
    [
        OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED),
        OwnerLockError(OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID),
        OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED),
        OwnerLockError(OwnerLockErrorCode.OWNER_LOCK_CLOSED),
        OwnerElectionError(OwnerElectionErrorCode.OWNER_ELECTION_FAILED),
        OwnerElectionError(OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED),
    ],
)
def test_exact_noncontention_error_after_wait_preserves_identity_without_first_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OwnerLockError | OwnerElectionError,
) -> None:
    store = _store(tmp_path)
    cause = RuntimeError("retry exact cause")
    context = ValueError("retry exact context")
    error.__cause__ = cause
    error.__context__ = context
    error.__suppress_context__ = True
    first_contention = _held()
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.126],
        election_values=[first_contention, error],
        wait_values=[None],
    )
    flow.install(monkeypatch)

    with pytest.raises(type(error)) as captured:
        wait_for_owner_election(store, 1.0)
    assert captured.value is error
    assert captured.value.__cause__ is cause
    assert captured.value.__context__ is context
    assert captured.value.__context__ is not first_contention
    assert captured.value.__suppress_context__ is True
    assert flow.events == [
        ("clock",),
        ("elect", store, pytest.approx(1.0)),
        ("clock",),
        ("wait", pytest.approx(0.025)),
        ("clock",),
        ("elect", store, pytest.approx(0.874)),
    ]


def _owner_error_with_malformed_code(code: object) -> OwnerLockError:
    error = OwnerLockError.__new__(OwnerLockError)
    RuntimeError.__init__(error, "malformed owner error")
    error.code = code  # type: ignore[assignment]
    return error


def _owner_error_without_code() -> OwnerLockError:
    error = OwnerLockError.__new__(OwnerLockError)
    RuntimeError.__init__(error, "malformed owner error")
    return error


def _election_error_with_malformed_code(code: object) -> OwnerElectionError:
    error = OwnerElectionError.__new__(OwnerElectionError)
    RuntimeError.__init__(error, "malformed election error")
    error.code = code  # type: ignore[assignment]
    return error


def _election_error_without_code() -> OwnerElectionError:
    error = OwnerElectionError.__new__(OwnerElectionError)
    RuntimeError.__init__(error, "malformed election error")
    return error


def _phantom_owner_error_code(value: str) -> OwnerLockErrorCode:
    code = str.__new__(OwnerLockErrorCode, value)
    code._name_ = "PHANTOM"
    code._value_ = value
    return code


def _phantom_election_error_code(value: str) -> OwnerElectionErrorCode:
    code = str.__new__(OwnerElectionErrorCode, value)
    code._name_ = "PHANTOM"
    code._value_ = value
    return code


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(PRIVATE),
        _DerivedOwnerLockError(OwnerLockErrorCode.OWNER_LOCK_HELD),
        _DerivedOwnerElectionError(OwnerElectionErrorCode.OWNER_ELECTION_FAILED),
        _OpaqueDerivedOwnerLockError(OwnerLockErrorCode.OWNER_LOCK_HELD),
        _OpaqueDerivedOwnerElectionError(OwnerElectionErrorCode.OWNER_ELECTION_FAILED),
        _owner_error_without_code(),
        _owner_error_with_malformed_code(None),
        _owner_error_with_malformed_code("OWNER_LOCK_HELD"),
        pytest.param(_owner_error_with_malformed_code(_OpaqueMalformed()), id="opaque-owner-code"),
        _election_error_with_malformed_code(None),
        _election_error_without_code(),
        pytest.param(
            _election_error_with_malformed_code(_OpaqueMalformed()),
            id="opaque-election-code",
        ),
    ],
)
@pytest.mark.parametrize("after_contention", [False, True], ids=["first", "retry"])
def test_ordinary_derived_and_malformed_election_failures_become_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    after_contention: bool,
) -> None:
    store = _store(tmp_path)
    first_contention = _held() if after_contention else None
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.126] if after_contention else [0.0],
        election_values=[first_contention, failure] if first_contention is not None else [failure],
        wait_values=[None] if after_contention else [],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(store, 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    assert captured.value is not failure
    if first_contention is not None:
        assert captured.value.__context__ is not first_contention
        assert captured.value.__cause__ is not first_contention
    assert _event_names(flow) == (
        ["clock", "elect", "clock", "wait", "clock", "elect"]
        if after_contention
        else ["clock", "elect"]
    )


@pytest.mark.parametrize(
    "failure",
    [
        OwnerLockError(_phantom_owner_error_code("OWNER_LOCK_HELD")),
        OwnerLockError(_phantom_owner_error_code(PRIVATE)),
        OwnerElectionError(_phantom_election_error_code("OWNER_ELECTION_FAILED")),
        OwnerElectionError(_phantom_election_error_code(PRIVATE)),
    ],
    ids=[
        "owner-canonical-value",
        "owner-private-value",
        "election-canonical-value",
        "election-private-value",
    ],
)
@pytest.mark.parametrize("after_contention", [False, True], ids=["first", "retry"])
def test_exact_type_phantom_error_codes_are_fixed_without_wait_or_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OwnerLockError | OwnerElectionError,
    after_contention: bool,
) -> None:
    assert type(failure.code) in {OwnerLockErrorCode, OwnerElectionErrorCode}
    first_contention = _held() if after_contention else None
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.126] if after_contention else [0.0],
        election_values=[first_contention, failure] if first_contention is not None else [failure],
        wait_values=[None] if after_contention else [],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(_store(tmp_path), 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    assert captured.value is not failure
    if first_contention is not None:
        assert captured.value.__context__ is not first_contention
        assert captured.value.__cause__ is not first_contention
    assert _event_names(flow) == (
        ["clock", "elect", "clock", "wait", "clock", "elect"]
        if after_contention
        else ["clock", "elect"]
    )


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        False,
        0,
        object(),
        object.__new__(_DerivedState),
        object.__new__(_DerivedOwner),
        pytest.param(_OpaqueMalformed(), id="opaque"),
    ],
)
@pytest.mark.parametrize("after_contention", [False, True], ids=["first", "retry"])
def test_malformed_election_result_is_never_inspected_or_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed: object,
    after_contention: bool,
) -> None:
    store = _store(tmp_path)
    first_contention = _held() if after_contention else None
    flow = _Flow(
        clock_values=[0.0, 0.1, 0.126] if after_contention else [0.0],
        election_values=[first_contention, malformed]
        if first_contention is not None
        else [malformed],
        wait_values=[None] if after_contention else [],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(store, 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    if first_contention is not None:
        assert captured.value.__context__ is not first_contention
        assert captured.value.__cause__ is not first_contention
    assert _event_names(flow) == (
        ["clock", "elect", "clock", "wait", "clock", "elect"]
        if after_contention
        else ["clock", "elect"]
    )


@pytest.mark.parametrize(
    "wait_failure",
    [
        RuntimeError(PRIVATE),
        False,
        0,
        object(),
        pytest.param(_OpaqueMalformed(), id="opaque"),
    ],
)
def test_ordinary_or_malformed_wait_failure_becomes_fixed_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_failure: object,
) -> None:
    store = _store(tmp_path)
    flow = _Flow(
        clock_values=[0.0, 0.2],
        election_values=[_held()],
        wait_values=[wait_failure],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(store, 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_FAILED,
    )
    assert _event_names(flow) == ["clock", "elect", "clock", "wait"]


@pytest.mark.parametrize(
    ("clock_values", "election_values", "wait_values", "expected_names"),
    [
        ([RuntimeError(PRIVATE)], [], [], ["clock"]),
        ([_OpaqueMalformed()], [], [], ["clock"]),
        ([0], [], [], ["clock"]),
        ([True], [], [], ["clock"]),
        ([_DerivedFloat(0.0)], [], [], ["clock"]),
        ([math.nan], [], [], ["clock"]),
        ([math.inf], [], [], ["clock"]),
        ([1.7976931348623157e308], [], [], ["clock"]),
        (
            [0.0, RuntimeError(PRIVATE)],
            [_held()],
            [],
            ["clock", "elect", "clock"],
        ),
        (
            [0.0, _OpaqueMalformed()],
            [_held()],
            [],
            ["clock", "elect", "clock"],
        ),
        (
            [0.0, math.nan],
            [_held()],
            [],
            ["clock", "elect", "clock"],
        ),
        (
            [0.0, math.inf],
            [_held()],
            [],
            ["clock", "elect", "clock"],
        ),
        (
            [1.0, 0.9],
            [_held()],
            [],
            ["clock", "elect", "clock"],
        ),
        (
            [0.0, 1.0],
            [_held()],
            [],
            ["clock", "elect", "clock"],
        ),
        (
            [0.0, 0.2, RuntimeError(PRIVATE)],
            [_held()],
            [None],
            ["clock", "elect", "clock", "wait", "clock"],
        ),
        (
            [0.0, 0.2, _OpaqueMalformed()],
            [_held()],
            [None],
            ["clock", "elect", "clock", "wait", "clock"],
        ),
        (
            [0.0, 0.2, 0.19],
            [_held()],
            [None],
            ["clock", "elect", "clock", "wait", "clock"],
        ),
        (
            [0.0, 0.2, math.nan],
            [_held()],
            [None],
            ["clock", "elect", "clock", "wait", "clock"],
        ),
    ],
    ids=[
        "initial-exception",
        "initial-opaque",
        "initial-int",
        "initial-bool",
        "initial-derived-float",
        "initial-nan",
        "initial-infinity",
        "deadline-overflow",
        "pre-wait-exception",
        "pre-wait-opaque",
        "pre-wait-nan",
        "pre-wait-infinity",
        "pre-wait-rollback",
        "pre-wait-expiry",
        "post-wait-exception",
        "post-wait-opaque",
        "post-wait-rollback",
        "post-wait-nan",
    ],
)
def test_clock_failure_at_every_boundary_is_fixed_deadline_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_values: list[object],
    election_values: list[object],
    wait_values: list[object],
    expected_names: list[str],
) -> None:
    flow = _Flow(
        clock_values=clock_values,
        election_values=election_values,
        wait_values=wait_values,
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(_store(tmp_path), 1.0)
    _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert _event_names(flow) == expected_names


@pytest.mark.parametrize(
    ("clock_values", "election_values", "wait_values", "expected_names"),
    [
        ([_ProcessControl("initial")], [], [], ["clock"]),
        (
            [0.0],
            [_ProcessControl("election")],
            [],
            ["clock", "elect"],
        ),
        (
            [0.0, 0.1, 0.126],
            [_held(), _ProcessControl("retry-election")],
            [None],
            ["clock", "elect", "clock", "wait", "clock", "elect"],
        ),
        (
            [0.0, _ProcessControl("before-wait")],
            [_held()],
            [],
            ["clock", "elect", "clock"],
        ),
        (
            [0.0, 0.2],
            [_held()],
            [_ProcessControl("wait")],
            ["clock", "elect", "clock", "wait"],
        ),
        (
            [0.0, 0.2, _ProcessControl("after-wait")],
            [_held()],
            [None],
            ["clock", "elect", "clock", "wait", "clock"],
        ),
    ],
    ids=[
        "initial-clock",
        "election",
        "retry-election",
        "wait-admission-clock",
        "wait",
        "post-wait-clock",
    ],
)
def test_process_control_at_every_seam_preserves_identity_and_stops_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_values: list[object],
    election_values: list[object],
    wait_values: list[object],
    expected_names: list[str],
) -> None:
    process_control = next(
        value
        for value in [*clock_values, *election_values, *wait_values]
        if type(value) is _ProcessControl
    )
    flow = _Flow(
        clock_values=clock_values,
        election_values=election_values,
        wait_values=wait_values,
    )
    flow.install(monkeypatch)

    with pytest.raises(_ProcessControl) as captured:
        wait_for_owner_election(_store(tmp_path), 1.0)
    assert captured.value is process_control
    assert _event_names(flow) == expected_names


def test_consumed_contention_is_not_an_exception_chain_or_module_retention_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contention = _held()
    flow = _Flow(
        clock_values=[0.0, 0.2, 0.21],
        election_values=[contention],
        wait_values=[None],
    )
    flow.install(monkeypatch)

    with pytest.raises(OwnerElectionError) as captured:
        wait_for_owner_election(_store(tmp_path), 1.0)
    deadline_error = _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
    )
    assert deadline_error.__context__ is not contention
    assert deadline_error.__cause__ is not contention
    assert getattr(deadline_error, "__notes__", None) is None

    monkeypatch.undo()
    _assert_module_does_not_retain_identity(contention)


def test_consumed_contention_is_collectable_after_seams_are_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exercise() -> weakref.ReferenceType[OwnerLockError]:
        contention = _held()
        reference = weakref.ref(contention)
        flow = _Flow(
            clock_values=[0.0, 0.2, 0.21],
            election_values=[contention],
            wait_values=[None],
        )
        flow.install(monkeypatch)
        with pytest.raises(OwnerElectionError) as captured:
            wait_for_owner_election(_store(tmp_path), 1.0)
        _assert_election_error(
            captured.value,
            OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
        )
        return reference

    reference = exercise()
    monkeypatch.undo()
    gc.collect()
    assert reference() is None


def test_fixed_failure_suppresses_sensitive_caller_context_from_formatted_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_context = LookupError(PRIVATE)
    contention = _held()
    flow = _Flow(
        clock_values=[0.0, 0.2, 0.21],
        election_values=[contention],
        wait_values=[None],
    )
    flow.install(monkeypatch)

    try:
        raise caller_context
    except LookupError:
        with pytest.raises(OwnerElectionError) as captured:
            wait_for_owner_election(_store(tmp_path), 1.0)
    fixed = _assert_election_error(
        captured.value,
        OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED,
        expected_context=caller_context,
    )
    assert fixed.__context__ is not contention
    assert PRIVATE not in "".join(traceback.format_exception(fixed))


@pytest.mark.parametrize("result_kind", ["state", "owner"])
def test_success_results_and_store_are_not_retained_after_seam_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_kind: str,
) -> None:
    store = _store(tmp_path)
    result: SidecarState | OwnerLock
    if result_kind == "state":
        result = object.__new__(SidecarState)
    else:
        result = _fake_owner()
    flow = _Flow(clock_values=[0.0], election_values=[result])
    flow.install(monkeypatch)

    assert wait_for_owner_election(store, 1.0) is result
    monkeypatch.undo()
    _assert_module_does_not_retain_identity(store)
    _assert_module_does_not_retain_identity(result)


def test_canonical_clock_wait_and_election_dispatch_are_frozen_at_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    def replaced_election(target: StateStore, timeout: float) -> NoReturn:
        del target, timeout
        raise _UnexpectedCollaboratorCall("later public election replacement was used")

    with monkeypatch.context() as patch:
        patch.setattr(wait_module, "resolve_owner_election", replaced_election)
        owner = wait_module._resolve_owner_election(store, 0.5)
    assert type(owner) is OwnerLock
    owner.close()

    original_monotonic = time.monotonic

    def replaced_monotonic() -> NoReturn:
        raise _UnexpectedCollaboratorCall("later public clock replacement was used")

    with monkeypatch.context() as patch:
        patch.setattr(wait_module.time, "monotonic", replaced_monotonic)
        observed = wait_module._read_monotonic()
    assert type(observed) is float
    assert observed <= original_monotonic()

    def replaced_wait(interval: float) -> NoReturn:
        del interval
        raise _UnexpectedCollaboratorCall("later public wait replacement was used")

    frozen_wait = wait_module._WAIT
    assert frozen_wait is time.sleep
    with monkeypatch.context() as patch:
        patch.setattr(wait_module.time, "sleep", replaced_wait)
        assert wait_module._WAIT is frozen_wait


def test_real_held_lock_reaches_observable_wait_then_returns_active_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    incumbent = OwnerLock.acquire(store)
    wait_reached = threading.Event()
    release_complete = threading.Event()
    clock_values = iter([0.0, 0.0, 0.025])
    results: list[SidecarState | OwnerLock] = []
    failures: list[BaseException] = []

    def read_clock() -> object:
        try:
            return next(clock_values)
        except StopIteration:
            raise _UnexpectedCollaboratorCall("unexpected extra outer clock read") from None

    def observable_wait(interval: float) -> object:
        assert interval == pytest.approx(0.025)
        wait_reached.set()
        if not release_complete.wait(timeout=5.0):
            raise AssertionError("test did not release the incumbent lock")
        return None

    monkeypatch.setattr(wait_module, "_read_monotonic", read_clock)
    monkeypatch.setattr(wait_module, "_wait_interval", observable_wait)

    def run_waiter() -> None:
        try:
            results.append(wait_for_owner_election(store, 0.5))
        except BaseException as error:
            failures.append(error)

    waiter = threading.Thread(target=run_waiter, name="flowsight-owner-wait-test")
    waiter.start()
    try:
        assert wait_reached.wait(timeout=5.0), "waiter never reached bounded wait"
        incumbent.close()
        release_complete.set()
        waiter.join(timeout=5.0)
        assert not waiter.is_alive(), "waiter did not finish after observable release"
        assert failures == []
        assert len(results) == 1
        elected = results[0]
        assert type(elected) is OwnerLock

        with pytest.raises(OwnerLockError) as held:
            OwnerLock.acquire(store)
        assert held.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD

        elected.close()
        successor = OwnerLock.acquire(store)
        successor.close()
    finally:
        release_complete.set()
        incumbent.close()
        waiter.join(timeout=5.0)
        for result in results:
            if type(result) is OwnerLock:
                result.close()


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


def test_production_ast_stays_inside_bounded_retry_election_policy() -> None:
    source_path = Path(wait_module.__file__)
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
        ("from", 0, "typing", "Final", None),
        ("from", 0, "typing", "NoReturn", None),
        ("from", 0, "typing", "cast", None),
        ("from", 1, "health", "MAX_HEALTH_PROBE_TIMEOUT_SECONDS", None),
        ("from", 1, "owner_lock", "OwnerLock", None),
        ("from", 1, "owner_lock", "OwnerLockError", None),
        ("from", 1, "owner_lock", "OwnerLockErrorCode", None),
        ("from", 1, "startup_election", "OwnerElectionError", None),
        ("from", 1, "startup_election", "OwnerElectionErrorCode", None),
        ("from", 1, "startup_election", "resolve_owner_election", None),
        ("from", 1, "state", "SidecarState", None),
        ("from", 1, "state", "StateStore", None),
    }

    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    functions_by_name = {node.name: node for node in functions}
    assert len(functions) == len(functions_by_name)
    assert {id(node) for node in functions} == {
        id(node) for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert set(functions_by_name) == {
        "_validate_timeout",
        "_read_monotonic",
        "_observe_monotonic",
        "_remaining",
        "_resolve_owner_election",
        "_wait_interval",
        "_new_error",
        "_raise_error",
        "wait_for_owner_election",
    }
    assert not any(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree))
    assert all(function.decorator_list == [] for function in functions)

    top_assignments: list[ast.AnnAssign] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            assert isinstance(node.target, ast.Name)
            assert isinstance(node.annotation, ast.Name)
            assert node.annotation.id == "Final"
            assert node.value is not None
            top_assignments.append(node)
        else:
            assert not isinstance(node, ast.Assign)
    assignments_by_name = {node.target.id: node for node in top_assignments}
    assert set(assignments_by_name) == {
        "_ATTACH_POLL_SECONDS",
        "_STATE_STORE_TYPE",
        "_SIDECAR_STATE_TYPE",
        "_OWNER_LOCK_TYPE",
        "_OWNER_LOCK_ERROR_TYPE",
        "_OWNER_LOCK_ERROR_CODE_TYPE",
        "_OWNER_ELECTION_ERROR_TYPE",
        "_OWNER_ELECTION_ERROR_CODE_TYPE",
        "_RESOLVE_OWNER_ELECTION",
        "_READ_MONOTONIC",
        "_WAIT",
    }
    expected_bindings = {
        "_STATE_STORE_TYPE": "StateStore",
        "_SIDECAR_STATE_TYPE": "SidecarState",
        "_OWNER_LOCK_TYPE": "OwnerLock",
        "_OWNER_LOCK_ERROR_TYPE": "OwnerLockError",
        "_OWNER_LOCK_ERROR_CODE_TYPE": "OwnerLockErrorCode",
        "_OWNER_ELECTION_ERROR_TYPE": "OwnerElectionError",
        "_OWNER_ELECTION_ERROR_CODE_TYPE": "OwnerElectionErrorCode",
        "_RESOLVE_OWNER_ELECTION": "resolve_owner_election",
        "_READ_MONOTONIC": "time.monotonic",
        "_WAIT": "time.sleep",
    }
    for name, expected_path in expected_bindings.items():
        assert _attribute_path(assignments_by_name[name].value) == expected_path
    poll_assignment = assignments_by_name["_ATTACH_POLL_SECONDS"]
    assert isinstance(poll_assignment.value, ast.Constant)
    assert type(poll_assignment.value.value) is float
    assert poll_assignment.value.value == 0.025

    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)
    assert type(tree.body[0].value.value) is str
    allowed_top_level = {
        id(tree.body[0]),
        *(id(node) for node in top_level_imports),
        *(id(node) for node in top_assignments),
        *(id(node) for node in functions),
    }
    assert {id(node) for node in tree.body} == allowed_top_level

    forbidden_nodes = (
        ast.AsyncFor,
        ast.AsyncWith,
        ast.Await,
        ast.ClassDef,
        ast.DictComp,
        ast.For,
        ast.GeneratorExp,
        ast.Global,
        ast.Lambda,
        ast.ListComp,
        ast.Match,
        ast.NamedExpr,
        ast.Nonlocal,
        ast.Eq,
        ast.NotEq,
        ast.SetComp,
        ast.With,
        ast.Yield,
        ast.YieldFrom,
    )
    assert not any(isinstance(node, forbidden_nodes) for node in ast.walk(tree))
    while_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.While)]
    assert len(while_nodes) == 1
    assert while_nodes[0] in list(ast.walk(functions_by_name["wait_for_owner_election"]))
    assert isinstance(while_nodes[0].test, ast.Constant)
    assert while_nodes[0].test.value is True

    set_literals = [node for node in ast.walk(tree) if isinstance(node, ast.Set)]
    assert len(set_literals) == 1
    assert set_literals[0] in list(ast.walk(functions_by_name["_validate_timeout"]))
    assert [_attribute_path(node) for node in set_literals[0].elts] == ["int", "float"]
    assert not any(isinstance(node, (ast.List, ast.Dict)) for node in ast.walk(tree))
    assert not any(
        isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(tree)
    )

    string_constants = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and type(node.value) is str
    ]
    assert sorted(string_constants) == sorted(
        [
            "Bounded retry policy for sidecar owner election contention.",
            "Return one incumbent or owner after bounded contention retries.",
            "timeout must be a built-in int or float",
            "timeout must be finite, positive, and at most 30 seconds",
            "timeout must be finite, positive, and at most 30 seconds",
            "store must be an exact StateStore",
        ]
    )
    assert not any(
        isinstance(node, ast.Constant) and type(node.value) is bytes for node in ast.walk(tree)
    )

    tuple_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Tuple)]
    assert len(tuple_nodes) == 3
    stored_tuples = [node for node in tuple_nodes if isinstance(node.ctx, ast.Store)]
    assert len(stored_tuples) == 1
    assert [element.id for element in stored_tuples[0].elts if isinstance(element, ast.Name)] == [
        "wait_started",
        "wait_remaining",
    ]
    loaded_tuple_paths = [
        [_attribute_path(element) for element in node.elts]
        for node in tuple_nodes
        if isinstance(node.ctx, ast.Load)
    ]
    assert sorted(loaded_tuple_paths) == sorted([["float", "float"], ["observed", "remaining"]])

    def call_path(call: ast.Call) -> str:
        path = _attribute_path(call.func)
        assert path is not None
        return path

    permitted_calls = {
        "_validate_timeout": {
            "type",
            "TypeError",
            "ValueError",
            "float",
            "cast",
            "math.isfinite",
        },
        "_read_monotonic": {"_READ_MONOTONIC"},
        "_observe_monotonic": {"_read_monotonic", "type", "math.isfinite"},
        "_remaining": {"_observe_monotonic", "math.isfinite"},
        "_resolve_owner_election": {"_RESOLVE_OWNER_ELECTION"},
        "_wait_interval": {"_WAIT"},
        "_new_error": {"OwnerElectionError"},
        "_raise_error": {"_new_error"},
        "wait_for_owner_election": {
            "type",
            "TypeError",
            "_validate_timeout",
            "_observe_monotonic",
            "_remaining",
            "_resolve_owner_election",
            "_wait_interval",
            "_raise_error",
            "math.isfinite",
            "min",
        },
    }
    calls_by_scope: dict[str, list[str]] = {}
    scoped_calls: set[int] = set()
    for name, function in functions_by_name.items():
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        calls_by_scope[name] = [call_path(node) for node in calls]
        scoped_calls.update(id(node) for node in calls)
        assert set(calls_by_scope[name]) <= permitted_calls[name]
    assert scoped_calls == {id(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    all_calls = [path for paths in calls_by_scope.values() for path in paths]
    assert all_calls.count("_RESOLVE_OWNER_ELECTION") == 1
    assert all_calls.count("_READ_MONOTONIC") == 1
    assert all_calls.count("_WAIT") == 1
    assert all_calls.count("_resolve_owner_election") == 1
    assert all_calls.count("_wait_interval") == 1
    assert all_calls.count("min") == 1

    frozen_election_call = next(
        node
        for node in ast.walk(functions_by_name["_resolve_owner_election"])
        if isinstance(node, ast.Call) and call_path(node) == "_RESOLVE_OWNER_ELECTION"
    )
    assert [
        argument.id for argument in frozen_election_call.args if isinstance(argument, ast.Name)
    ] == [
        "store",
        "timeout",
    ]
    assert frozen_election_call.keywords == []
    frozen_clock_call = next(
        node
        for node in ast.walk(functions_by_name["_read_monotonic"])
        if isinstance(node, ast.Call) and call_path(node) == "_READ_MONOTONIC"
    )
    assert frozen_clock_call.args == []
    assert frozen_clock_call.keywords == []
    frozen_wait_call = next(
        node
        for node in ast.walk(functions_by_name["_wait_interval"])
        if isinstance(node, ast.Call) and call_path(node) == "_WAIT"
    )
    assert len(frozen_wait_call.args) == 1
    assert isinstance(frozen_wait_call.args[0], ast.Name)
    assert frozen_wait_call.args[0].id == "interval"
    assert frozen_wait_call.keywords == []

    public_scope = functions_by_name["wait_for_owner_election"]
    parent_by_id = {
        id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }

    stored_names = {
        node.id
        for node in ast.walk(public_scope)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    assert stored_names == {
        "admitted_at",
        "contention",
        "deadline",
        "elapsed",
        "election_error_code",
        "election_timeout",
        "interval",
        "malformed",
        "next_observed",
        "next_remaining",
        "normalized_timeout",
        "outcome",
        "owner_error_code",
        "started",
        "wait_failed",
        "wait_remaining",
        "wait_result",
        "wait_started",
        "wait_window",
    }

    public_try_nodes = [node for node in ast.walk(public_scope) if isinstance(node, ast.Try)]
    assert len(public_try_nodes) == 4
    assert all(node.finalbody == [] for node in public_try_nodes)
    handler_shapes = [
        (
            tuple((_attribute_path(handler.type), handler.name) for handler in node.handlers),
            len(node.orelse),
        )
        for node in public_try_nodes
    ]
    assert (
        handler_shapes.count(
            (
                (
                    ("OwnerLockError", "error"),
                    ("OwnerElectionError", "error"),
                    ("Exception", None),
                ),
                0,
            )
        )
        == 1
    )
    assert handler_shapes.count(((("Exception", None),), 0)) == 1
    assert handler_shapes.count(((("Exception", None),), 1)) == 2

    raw_store_loads = [
        node
        for node in ast.walk(public_scope)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "store"
    ]
    store_uses: list[str] = []
    for node in raw_store_loads:
        parent = parent_by_id[id(node)]
        assert isinstance(parent, ast.Call)
        if call_path(parent) == "type":
            assert parent.args == [node]
            assert parent.keywords == []
            store_uses.append("type")
            continue
        assert call_path(parent) == "_resolve_owner_election"
        assert len(parent.args) == 2
        assert parent.args[0] is node
        assert isinstance(parent.args[1], ast.Name)
        assert parent.args[1].id == "election_timeout"
        assert parent.keywords == []
        store_uses.append("election")
    assert store_uses.count("type") == 1
    assert store_uses.count("election") == 1

    raw_outcome_loads = [
        node
        for node in ast.walk(public_scope)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "outcome"
    ]
    outcome_uses: list[str] = []
    for node in raw_outcome_loads:
        parent = parent_by_id[id(node)]
        if isinstance(parent, ast.Call):
            assert call_path(parent) == "type"
            assert parent.args == [node]
            assert parent.keywords == []
            outcome_uses.append("type")
            continue
        if isinstance(parent, ast.Return):
            assert parent.value is node
            outcome_uses.append("return")
            continue
        raise AssertionError("unsafe raw election outcome use")
    assert outcome_uses.count("type") == 2
    assert outcome_uses.count("return") == 1

    raw_error_loads = [
        node
        for node in ast.walk(public_scope)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "error"
    ]
    error_uses: list[str] = []
    for node in raw_error_loads:
        parent = parent_by_id[id(node)]
        if isinstance(parent, ast.Call):
            assert call_path(parent) == "type"
            assert parent.args == [node]
            assert parent.keywords == []
            error_uses.append("type")
            continue
        if isinstance(parent, ast.Attribute):
            assert parent.value is node
            assert parent.attr == "code"
            assert isinstance(parent.ctx, ast.Load)
            error_uses.append("code")
            continue
        raise AssertionError("unsafe raw election error use")
    assert error_uses.count("type") == 2
    assert error_uses.count("code") == 2

    error_code_assignments: dict[str, ast.Assign] = {}
    for node in ast.walk(public_scope):
        if (
            not isinstance(node, ast.Attribute)
            or not isinstance(node.ctx, ast.Load)
            or not isinstance(node.value, ast.Name)
            or node.value.id != "error"
            or node.attr != "code"
        ):
            continue
        parent = parent_by_id[id(node)]
        assert isinstance(parent, ast.Assign)
        assert parent.value is node
        assert len(parent.targets) == 1
        target = parent.targets[0]
        assert isinstance(target, ast.Name)
        assert target.id in {"owner_error_code", "election_error_code"}
        assert target.id not in error_code_assignments
        error_code_assignments[target.id] = parent
    assert set(error_code_assignments) == {"owner_error_code", "election_error_code"}

    def assert_identity_code_uses(name: str, canonical_paths: set[str]) -> None:
        uses = [
            node
            for node in ast.walk(public_scope)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name
        ]
        observed_paths: set[str] = set()
        type_calls = 0
        for node in uses:
            parent = parent_by_id[id(node)]
            if isinstance(parent, ast.Call):
                assert call_path(parent) == "type"
                assert parent.args == [node]
                assert parent.keywords == []
                type_calls += 1
                continue
            if isinstance(parent, ast.Compare):
                assert parent.left is node
                assert len(parent.ops) == 1
                assert isinstance(parent.ops[0], ast.Is)
                assert len(parent.comparators) == 1
                path = _attribute_path(parent.comparators[0])
                assert path in canonical_paths
                observed_paths.add(path)
                continue
            raise AssertionError(f"unsafe raw {name} use")
        assert type_calls == 1
        assert observed_paths == canonical_paths

    assert_identity_code_uses(
        "owner_error_code",
        {
            "OwnerLockErrorCode.OWNER_LOCK_HELD",
            "OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED",
            "OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID",
            "OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED",
            "OwnerLockErrorCode.OWNER_LOCK_CLOSED",
        },
    )
    assert_identity_code_uses(
        "election_error_code",
        {
            "OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED",
            "OwnerElectionErrorCode.OWNER_ELECTION_FAILED",
        },
    )

    elapsed_assignments = [
        node
        for node in ast.walk(public_scope)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "elapsed"
    ]
    assert len(elapsed_assignments) == 1
    elapsed_value = elapsed_assignments[0].value
    assert isinstance(elapsed_value, ast.BinOp)
    assert isinstance(elapsed_value.op, ast.Sub)
    assert isinstance(elapsed_value.left, ast.Name)
    assert elapsed_value.left.id == "next_observed"
    assert isinstance(elapsed_value.right, ast.Name)
    assert elapsed_value.right.id == "wait_started"
    elapsed_finite_calls = [
        node
        for node in ast.walk(public_scope)
        if isinstance(node, ast.Call)
        and call_path(node) == "math.isfinite"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "elapsed"
    ]
    assert len(elapsed_finite_calls) == 1

    interval_assignments = [
        node
        for node in ast.walk(public_scope)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "interval"
    ]
    assert len(interval_assignments) == 1
    interval_value = interval_assignments[0].value
    assert isinstance(interval_value, ast.Call)
    assert call_path(interval_value) == "min"
    assert len(interval_value.args) == 2
    assert isinstance(interval_value.args[0], ast.Name)
    assert interval_value.args[0].id == "_ATTACH_POLL_SECONDS"
    assert isinstance(interval_value.args[1], ast.Name)
    assert interval_value.args[1].id == "wait_remaining"
    assert interval_value.keywords == []

    load_attributes = {
        _attribute_path(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }
    assert None not in load_attributes
    assert load_attributes <= {
        "time.monotonic",
        "time.sleep",
        "math.isfinite",
        "error.code",
        "OwnerLockErrorCode.OWNER_LOCK_HELD",
        "OwnerLockErrorCode.OWNER_LOCK_STORAGE_FAILED",
        "OwnerLockErrorCode.INHERITED_OWNER_LOCK_INVALID",
        "OwnerLockErrorCode.OWNER_LOCK_CLEANUP_FAILED",
        "OwnerLockErrorCode.OWNER_LOCK_CLOSED",
        "OwnerElectionErrorCode.OWNER_ELECTION_DEADLINE_FAILED",
        "OwnerElectionErrorCode.OWNER_ELECTION_FAILED",
    }
    assert not any(
        isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(tree)
    )

    forbidden_names = {
        "Popen",
        "asyncio",
        "bind",
        "callback",
        "close",
        "create_task",
        "eval",
        "exec",
        "fileno",
        "fork",
        "getattr",
        "kill",
        "listen",
        "logging",
        "open",
        "os",
        "print",
        "release",
        "signal",
        "socket",
        "spawn",
        "sqlite3",
        "subprocess",
        "terminate",
        "threading",
        "uvicorn",
        "waitpid",
    }
    loaded_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert loaded_names.isdisjoint(forbidden_names)
