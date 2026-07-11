from __future__ import annotations

import ast
import http.client
import inspect
import json
import logging
import math
import os
import socket
import threading
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    LOOPBACK_HOST,
    SidecarState,
    StateStore,
    create_startup_state,
)
from flowsight.sidecar import startup_discovery as discovery_module
from flowsight.sidecar.startup_discovery import discover_existing_startup

STARTUP_ID = "0123456789abcdef0123456789abcdef"
OTHER_STARTUP_ID = "fedcba9876543210fedcba9876543210"
TOKEN = "A" * 43
OTHER_TOKEN = "B" * 43
PROJECT_ID = "project-alpha"
PID = 4321
PORT = 4040
STARTED_AT_NS = 1_700_000_000_000_000_000
PRIVATE = "private-token-path-pid-port-errno-detail"


class _DerivedInt(int):
    pass


class _DerivedFloat(float):
    pass


class _DerivedStore(StateStore):
    pass


class _DerivedState(SidecarState):
    pass


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "runtime", project_id=PROJECT_ID)


def _state(
    store: StateStore,
    *,
    startup_id: str = STARTUP_ID,
    pid: int = PID,
    port: int = PORT,
    token: str = TOKEN,
    project_id: str = PROJECT_ID,
    database_path: str | None = None,
    started_at_ns: int = STARTED_AT_NS,
) -> SidecarState:
    return SidecarState(
        project_id=project_id,
        startup_id=startup_id,
        pid=pid,
        port=port,
        token=token,
        database_path=str(store.database_path) if database_path is None else database_path,
        started_at_ns=started_at_ns,
    )


def _copy_state(state: SidecarState) -> SidecarState:
    copied = SidecarState.from_wire(state.to_wire())
    assert type(copied) is SidecarState
    assert copied == state
    assert copied is not state
    return copied


def _raise(error: BaseException) -> NoReturn:
    raise error


def _assert_fixed_private(error: BaseException, *values: object) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    for value in (PRIVATE, TOKEN, STARTUP_ID, str(PID), str(PORT), *values):
        try:
            encoded = str(value)
        except Exception:
            continue
        if len(encoded) == 1:
            continue
        assert encoded not in str(error)
        assert encoded not in repr(error)


class _Clock:
    def __init__(
        self,
        values: list[object],
        events: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.values = list(values)
        self.events = events
        self.calls = 0

    def read(self) -> object:
        self.calls += 1
        if self.events is not None:
            self.events.append(("clock", self.calls))
        if not self.values:
            raise AssertionError("unexpected extra monotonic read")
        return self.values.pop(0)


def _install_clock(
    monkeypatch: pytest.MonkeyPatch,
    values: list[object],
    events: list[tuple[object, ...]] | None = None,
) -> _Clock:
    clock = _Clock(values, events)
    monkeypatch.setattr(discovery_module.time, "monotonic", clock.read)
    return clock


def _install_flow(
    monkeypatch: pytest.MonkeyPatch,
    loaded: list[object],
    *,
    probe_result: object = True,
    clock_values: list[object] | None = None,
) -> tuple[list[tuple[object, ...]], _Clock]:
    events: list[tuple[object, ...]] = []
    remaining_loads = list(loaded)

    def load(store: StateStore) -> object:
        events.append(("load", store))
        if not remaining_loads:
            raise AssertionError("unexpected extra state load")
        return remaining_loads.pop(0)

    def probe(state: SidecarState, timeout: float) -> object:
        events.append(("probe", state, timeout))
        return probe_result

    monkeypatch.setattr(discovery_module, "_load_state", load)
    monkeypatch.setattr(discovery_module, "_probe_state", probe)
    clock = _install_clock(
        monkeypatch,
        [0.0, 0.1, 0.2, 0.3] if clock_values is None else clock_values,
        events,
    )
    return events, clock


def _install_zero_work_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        discovery_module.time,
        "monotonic",
        lambda: calls.append("clock"),
    )
    monkeypatch.setattr(
        discovery_module,
        "_load_state",
        lambda *_args: calls.append("load"),
    )
    monkeypatch.setattr(
        discovery_module,
        "_probe_state",
        lambda *_args: calls.append("probe"),
    )
    return calls


def test_public_shape_signature_annotations_and_export_are_exact() -> None:
    assert sidecar_package.discover_existing_startup is discover_existing_startup
    assert sidecar_package.__all__.count("discover_existing_startup") == 1

    signature = inspect.signature(discover_existing_startup)
    assert tuple(signature.parameters) == ("store", "timeout")
    assert signature.parameters["store"].default is inspect.Parameter.empty
    assert signature.parameters["store"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["timeout"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    timeout_default = signature.parameters["timeout"].default
    assert type(timeout_default) is float
    assert timeout_default == 0.5
    hints = get_type_hints(discover_existing_startup)
    assert hints == {
        "store": StateStore,
        "timeout": float,
        "return": SidecarState | None,
    }


@pytest.mark.parametrize("invalid", [None, object(), "caller-value", True, 1])
def test_wrong_store_type_is_fixed_and_fails_before_all_work(
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    calls = _install_zero_work_guards(monkeypatch)
    with pytest.raises(TypeError) as captured:
        discover_existing_startup(invalid)  # type: ignore[arg-type]
    assert type(captured.value) is TypeError
    assert str(captured.value) == "store must be an exact StateStore"
    _assert_fixed_private(captured.value, invalid)
    assert calls == []


def test_store_subclass_is_rejected_before_all_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_zero_work_guards(monkeypatch)
    store = _DerivedStore(tmp_path / "derived", project_id=PROJECT_ID)
    with pytest.raises(TypeError) as captured:
        discover_existing_startup(store)
    assert type(captured.value) is TypeError
    assert str(captured.value) == "store must be an exact StateStore"
    assert calls == []


@pytest.mark.parametrize(
    ("invalid", "expected_type", "expected_message"),
    [
        (True, TypeError, "timeout must be a built-in int or float"),
        (False, TypeError, "timeout must be a built-in int or float"),
        ("0.5", TypeError, "timeout must be a built-in int or float"),
        (object(), TypeError, "timeout must be a built-in int or float"),
        (_DerivedInt(1), TypeError, "timeout must be a built-in int or float"),
        (_DerivedFloat(0.5), TypeError, "timeout must be a built-in int or float"),
        (0, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (0.0, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (-0.1, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (math.nan, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (math.inf, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (-math.inf, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        (30.1, ValueError, "timeout must be finite, positive, and at most 30 seconds"),
        pytest.param(
            10**10_000,
            ValueError,
            "timeout must be finite, positive, and at most 30 seconds",
            id="huge-int",
        ),
    ],
)
def test_invalid_timeout_is_fixed_and_fails_before_all_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
    expected_type: type[Exception],
    expected_message: str,
) -> None:
    store = _store(tmp_path)
    calls = _install_zero_work_guards(monkeypatch)
    with pytest.raises(expected_type) as captured:
        discover_existing_startup(store, invalid)  # type: ignore[arg-type]
    assert type(captured.value) is expected_type
    assert str(captured.value) == expected_message
    _assert_fixed_private(captured.value, invalid)
    assert calls == []


def test_exact_order_current_budget_and_second_object_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    events, clock = _install_flow(monkeypatch, [first, second])

    result = discover_existing_startup(store, 1.0)

    assert result is second
    assert result is not first
    assert clock.calls == 4
    assert events == [
        ("clock", 1),
        ("load", store),
        ("clock", 2),
        ("probe", first, pytest.approx(0.9)),
        ("clock", 3),
        ("load", store),
        ("clock", 4),
    ]


@pytest.mark.parametrize("timeout", [1, 1.0, 30, 30.0])
def test_valid_timeout_is_normalized_and_probe_gets_only_remaining_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: int | float,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    events, _clock = _install_flow(
        monkeypatch,
        [first, second],
        clock_values=[10.0, 10.25, 10.5, 10.75],
    )
    assert discover_existing_startup(store, timeout) is second
    probes = [event for event in events if event[0] == "probe"]
    assert probes == [("probe", first, pytest.approx(float(timeout) - 0.25))]


@pytest.mark.parametrize(
    ("clock_values", "expected_names"),
    [
        ([0.0, 1.0], ["clock", "load", "clock"]),
        ([0.0, 0.1, 1.0], ["clock", "load", "clock", "probe", "clock"]),
        (
            [0.0, 0.1, 0.2, 1.0],
            ["clock", "load", "clock", "probe", "clock", "load", "clock"],
        ),
    ],
)
def test_expiry_at_every_admission_boundary_stops_later_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_values: list[object],
    expected_names: list[str],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    events, _clock = _install_flow(
        monkeypatch,
        [first, second],
        clock_values=clock_values,
    )
    assert discover_existing_startup(store, 1.0) is None
    assert [event[0] for event in events] == expected_names


@pytest.mark.parametrize(
    ("clock_values", "expected_names"),
    [
        ([1.0, 0.9], ["clock", "load", "clock"]),
        ([1.0, 1.1, 1.0], ["clock", "load", "clock", "probe", "clock"]),
        (
            [1.0, 1.1, 1.2, 1.1],
            ["clock", "load", "clock", "probe", "clock", "load", "clock"],
        ),
    ],
)
def test_clock_rollback_at_every_later_boundary_stops_later_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_values: list[object],
    expected_names: list[str],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    events, _clock = _install_flow(
        monkeypatch,
        [first, second],
        clock_values=clock_values,
    )
    assert discover_existing_startup(store, 1.0) is None
    assert [event[0] for event in events] == expected_names


@pytest.mark.parametrize(
    "invalid_clock",
    [True, 0, _DerivedFloat(0.1), math.nan, math.inf, -math.inf],
)
def test_invalid_initial_clock_result_stops_before_first_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_clock: object,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    events, _clock = _install_flow(
        monkeypatch,
        [state],
        clock_values=[invalid_clock],
    )
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events] == ["clock"]


@pytest.mark.parametrize(
    ("clock_values", "expected_names"),
    [
        ([0.0, True], ["clock", "load", "clock"]),
        ([0.0, _DerivedFloat(0.1)], ["clock", "load", "clock"]),
        ([0.0, 0.1, 0], ["clock", "load", "clock", "probe", "clock"]),
        (
            [0.0, 0.1, _DerivedFloat(0.2)],
            ["clock", "load", "clock", "probe", "clock"],
        ),
        (
            [0.0, 0.1, 0.2, math.nan],
            ["clock", "load", "clock", "probe", "clock", "load", "clock"],
        ),
        (
            [0.0, 0.1, 0.2, _DerivedFloat(0.3)],
            ["clock", "load", "clock", "probe", "clock", "load", "clock"],
        ),
    ],
)
def test_invalid_later_clock_result_stops_later_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_values: list[object],
    expected_names: list[str],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    events, _clock = _install_flow(
        monkeypatch,
        [first, second],
        clock_values=clock_values,
    )
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events] == expected_names


def test_deadline_overflow_stops_before_first_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    events, _clock = _install_flow(
        monkeypatch,
        [state],
        clock_values=[float.fromhex("0x1.fffffffffffffp+1023")],
    )
    assert discover_existing_startup(store, 30.0) is None
    assert [event[0] for event in events] == ["clock"]


@pytest.mark.parametrize("invalid", [None, object()])
def test_invalid_first_load_stops_before_network_and_second_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    events, _clock = _install_flow(monkeypatch, [invalid])
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events] == ["clock", "load"]


def test_derived_first_load_stops_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    derived = _DerivedState(**_state(store).to_wire())  # type: ignore[arg-type]
    events, _clock = _install_flow(monkeypatch, [derived])
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events] == ["clock", "load"]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("project_id", object()),
        ("startup_id", "\x00"),
        ("pid", True),
        ("port", 0),
        ("token", object()),
        ("database_path", object()),
        ("started_at_ns", 0),
        ("host", "127.0.0.2"),
        ("protocol_version", 2),
        ("state_schema_version", 2),
    ],
)
def test_forged_exact_first_state_fails_reconstruction_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    forged = _state(store)
    object.__setattr__(forged, field, invalid)
    events, _clock = _install_flow(monkeypatch, [forged])
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events] == ["clock", "load"]


@pytest.mark.parametrize("probe_result", [False, None, 1, "true", object()])
def test_only_exact_true_probe_result_reaches_second_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_result: object,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    events, _clock = _install_flow(
        monkeypatch,
        [first, second],
        probe_result=probe_result,
    )
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events] == ["clock", "load", "clock", "probe"]


@pytest.mark.parametrize("invalid", [None, object()])
def test_invalid_second_load_fails_after_one_probe_and_two_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    events, _clock = _install_flow(monkeypatch, [first, invalid])
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events] == [
        "clock",
        "load",
        "clock",
        "probe",
        "clock",
        "load",
    ]


def test_derived_second_load_is_rejected_after_one_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    derived = _DerivedState(**first.to_wire())  # type: ignore[arg-type]
    events, _clock = _install_flow(monkeypatch, [first, derived])
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events].count("load") == 2
    assert [event[0] for event in events].count("probe") == 1


def test_same_raw_object_reused_by_second_load_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    events, _clock = _install_flow(monkeypatch, [first, first])
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events].count("load") == 2
    assert [event[0] for event in events].count("probe") == 1


def _invalid_reconstruction(kind: str, raw: SidecarState) -> SidecarState:
    if kind == "raw-same-object":
        return raw
    if kind == "derived":
        return _DerivedState(**raw.to_wire())  # type: ignore[arg-type]
    if kind == "unequal-exact":
        values = raw.to_wire()
        values["token"] = OTHER_TOKEN
        return SidecarState.from_wire(values)
    raise AssertionError(f"unknown reconstruction result: {kind}")


@pytest.mark.parametrize("position", ["first", "second"])
@pytest.mark.parametrize("kind", ["raw-same-object", "derived", "unequal-exact"])
def test_bad_schema_reconstruction_result_fails_at_exact_flow_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: str,
    kind: str,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    target_call = 1 if position == "first" else 2
    target_raw = first if position == "first" else second
    invalid = _invalid_reconstruction(kind, target_raw)
    events, _clock = _install_flow(monkeypatch, [first, second])
    real_from_wire = SidecarState.from_wire
    reconstruction_calls = 0

    def from_wire(_cls: type[SidecarState], value: object) -> SidecarState:
        nonlocal reconstruction_calls
        reconstruction_calls += 1
        if reconstruction_calls == target_call:
            return invalid
        return real_from_wire(value)

    monkeypatch.setattr(SidecarState, "from_wire", classmethod(from_wire))
    assert discover_existing_startup(store) is None
    if position == "first":
        assert [event[0] for event in events] == ["clock", "load"]
        assert reconstruction_calls == 1
    else:
        assert [event[0] for event in events] == [
            "clock",
            "load",
            "clock",
            "probe",
            "clock",
            "load",
        ]
        assert reconstruction_calls == 2


@pytest.mark.parametrize(
    ("position", "expected_names", "expected_calls"),
    [
        ("first", ["clock", "load"], 1),
        ("second", ["clock", "load", "clock", "probe", "clock", "load"], 2),
    ],
)
def test_ordinary_to_wire_failure_at_each_reconstruction_is_private_none(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    position: str,
    expected_names: list[str],
    expected_calls: int,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    error = OSError(f"{PRIVATE}:{TOKEN}:{STARTUP_ID}:{PID}:{PORT}:{store.database_path}")
    real_to_wire = SidecarState.to_wire
    to_wire_calls = 0
    caplog.set_level(logging.DEBUG)

    def to_wire(state: SidecarState) -> dict[str, object]:
        nonlocal to_wire_calls
        to_wire_calls += 1
        if (position == "first" and state is first) or (position == "second" and state is second):
            raise error
        return real_to_wire(state)

    with pytest.MonkeyPatch.context() as patcher:
        events, _clock = _install_flow(patcher, [first, second])
        patcher.setattr(SidecarState, "to_wire", to_wire)
        assert discover_existing_startup(store, 1.0) is None

    assert to_wire_calls == expected_calls
    assert [event[0] for event in events] == expected_names
    output = capsys.readouterr()
    sensitive = (PRIVATE, TOKEN, STARTUP_ID, str(PID), str(PORT), str(store.database_path))
    for value in sensitive:
        assert value not in output.out
        assert value not in output.err
        assert all(value not in record.getMessage() for record in caplog.records)
    assert not _contains_identity(vars(discovery_module), error)


@pytest.mark.parametrize(
    ("position", "expected_names", "expected_calls"),
    [
        ("first", ["clock", "load"], 1),
        ("second", ["clock", "load", "clock", "probe", "clock", "load"], 2),
    ],
)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_to_wire_failure_at_each_reconstruction_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: str,
    expected_names: list[str],
    expected_calls: int,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    error = error_factory()
    real_to_wire = SidecarState.to_wire
    to_wire_calls = 0

    def to_wire(state: SidecarState) -> dict[str, object]:
        nonlocal to_wire_calls
        to_wire_calls += 1
        if (position == "first" and state is first) or (position == "second" and state is second):
            raise error
        return real_to_wire(state)

    events, _clock = _install_flow(monkeypatch, [first, second])
    monkeypatch.setattr(SidecarState, "to_wire", to_wire)
    with pytest.raises(error_factory) as captured:
        discover_existing_startup(store, 1.0)

    assert captured.value is error
    assert getattr(captured.value, "__notes__", []) == []
    assert to_wire_calls == expected_calls
    assert [event[0] for event in events] == expected_names


def _rotated_state(store: StateStore, first: SidecarState, field: str) -> SidecarState:
    values = first.to_wire()
    replacements: dict[str, object] = {
        "project_id": "project-beta",
        "startup_id": OTHER_STARTUP_ID,
        "pid": PID + 1,
        "port": PORT + 1,
        "token": OTHER_TOKEN,
        "database_path": str(store.database_path.with_name("rotated.sqlite3")),
        "started_at_ns": STARTED_AT_NS + 1,
    }
    if field in replacements:
        values[field] = replacements[field]
        return SidecarState.from_wire(values)
    rotated = SidecarState.from_wire(values)
    if field == "host":
        object.__setattr__(rotated, "host", "127.0.0.2")
    elif field == "protocol_version":
        object.__setattr__(rotated, "protocol_version", 2)
    elif field == "state_schema_version":
        object.__setattr__(rotated, "state_schema_version", 2)
    else:
        raise AssertionError(f"unknown rotated field: {field}")
    return rotated


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "startup_id",
        "pid",
        "port",
        "token",
        "database_path",
        "started_at_ns",
        "host",
        "protocol_version",
        "state_schema_version",
    ],
)
def test_every_full_state_rotation_after_health_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _rotated_state(store, first, field)
    events, _clock = _install_flow(monkeypatch, [first, second])
    assert discover_existing_startup(store) is None
    assert [event[0] for event in events].count("load") == 2
    assert [event[0] for event in events].count("probe") == 1


def _install_equality_ordinal_failure(
    monkeypatch: pytest.MonkeyPatch,
    first: SidecarState,
    second: SidecarState,
    error: BaseException,
    ordinal: int,
) -> tuple[list[str], list[tuple[SidecarState, object]]]:
    events: list[str] = []
    equality_operands: list[tuple[SidecarState, object]] = []
    remaining = [first, second]
    real_equal = SidecarState.__eq__
    clock_values = [0.0, 0.1, 0.2, 0.3]

    def load(_store: StateStore) -> SidecarState:
        events.append(f"load-{3 - len(remaining)}")
        if not remaining:
            raise AssertionError("unexpected extra state load")
        return remaining.pop(0)

    def probe(_state: SidecarState, _timeout: float) -> bool:
        events.append("probe-1")
        return True

    def clock() -> float:
        call_number = 5 - len(clock_values)
        events.append(f"clock-{call_number}")
        if not clock_values:
            raise AssertionError("unexpected extra monotonic read")
        return clock_values.pop(0)

    def equal(left: SidecarState, right: object) -> object:
        equality_operands.append((left, right))
        call_number = len(equality_operands)
        events.append(f"equal-{call_number}")
        if call_number == ordinal:
            raise error
        return real_equal(left, right)

    monkeypatch.setattr(discovery_module, "_load_state", load)
    monkeypatch.setattr(discovery_module, "_probe_state", probe)
    monkeypatch.setattr(discovery_module.time, "monotonic", clock)
    monkeypatch.setattr(SidecarState, "__eq__", equal)
    return events, equality_operands


def _assert_equality_operand_prefix(
    operands: list[tuple[SidecarState, object]],
    first: SidecarState,
    second: SidecarState,
) -> None:
    assert 1 <= len(operands) <= 4
    first_copy = operands[0][0]
    assert type(first_copy) is SidecarState
    assert first_copy is not first
    assert operands[0][1] is first
    if len(operands) >= 2:
        second_copy = operands[1][0]
        assert type(second_copy) is SidecarState
        assert second_copy is not second
        assert operands[1][1] is second
    if len(operands) >= 3:
        assert operands[2][0] is second
        assert operands[2][1] is first
    if len(operands) >= 4:
        assert operands[3][0] is operands[1][0]
        assert operands[3][1] is first_copy


def test_success_uses_exactly_four_state_equalities_before_final_clock(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    with pytest.MonkeyPatch.context() as patcher:
        events, equality_operands = _install_equality_ordinal_failure(
            patcher,
            first,
            second,
            AssertionError("unexpected fifth state equality"),
            5,
        )
        result = discover_existing_startup(store, 1.0)

    assert result is second
    assert events == [
        "clock-1",
        "load-1",
        "equal-1",
        "clock-2",
        "probe-1",
        "clock-3",
        "load-2",
        "equal-2",
        "equal-3",
        "equal-4",
        "clock-4",
    ]
    assert len(equality_operands) == 4
    _assert_equality_operand_prefix(equality_operands, first, second)


@pytest.mark.parametrize(
    ("ordinal", "expected_flow"),
    [
        (1, ["clock-1", "load-1", "equal-1"]),
        (
            2,
            [
                "clock-1",
                "load-1",
                "equal-1",
                "clock-2",
                "probe-1",
                "clock-3",
                "load-2",
                "equal-2",
            ],
        ),
        (
            3,
            [
                "clock-1",
                "load-1",
                "equal-1",
                "clock-2",
                "probe-1",
                "clock-3",
                "load-2",
                "equal-2",
                "equal-3",
            ],
        ),
        (
            4,
            [
                "clock-1",
                "load-1",
                "equal-1",
                "clock-2",
                "probe-1",
                "clock-3",
                "load-2",
                "equal-2",
                "equal-3",
                "equal-4",
            ],
        ),
    ],
)
def test_ordinary_failure_at_each_exact_state_equality_ordinal_is_private_none(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    ordinal: int,
    expected_flow: list[str],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    error = OSError(f"{PRIVATE}:{TOKEN}:{STARTUP_ID}:{PID}:{PORT}:{store.database_path}")
    caplog.set_level(logging.DEBUG)

    with pytest.MonkeyPatch.context() as patcher:
        events, equality_operands = _install_equality_ordinal_failure(
            patcher,
            first,
            second,
            error,
            ordinal,
        )
        result = discover_existing_startup(store, 1.0)

    assert result is None
    assert events == expected_flow
    assert len(equality_operands) == ordinal
    _assert_equality_operand_prefix(equality_operands, first, second)
    output = capsys.readouterr()
    sensitive = (PRIVATE, TOKEN, STARTUP_ID, str(PID), str(PORT), str(store.database_path))
    for value in sensitive:
        assert value not in output.out
        assert value not in output.err
        assert all(value not in record.getMessage() for record in caplog.records)
    assert not _contains_identity(vars(discovery_module), error)


@pytest.mark.parametrize(
    ("ordinal", "expected_flow"),
    [
        (1, ["clock-1", "load-1", "equal-1"]),
        (
            2,
            [
                "clock-1",
                "load-1",
                "equal-1",
                "clock-2",
                "probe-1",
                "clock-3",
                "load-2",
                "equal-2",
            ],
        ),
        (
            3,
            [
                "clock-1",
                "load-1",
                "equal-1",
                "clock-2",
                "probe-1",
                "clock-3",
                "load-2",
                "equal-2",
                "equal-3",
            ],
        ),
        (
            4,
            [
                "clock-1",
                "load-1",
                "equal-1",
                "clock-2",
                "probe-1",
                "clock-3",
                "load-2",
                "equal-2",
                "equal-3",
                "equal-4",
            ],
        ),
    ],
)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_at_each_exact_state_equality_ordinal_preserves_identity(
    tmp_path: Path,
    ordinal: int,
    expected_flow: list[str],
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    error = error_factory()

    with pytest.MonkeyPatch.context() as patcher:
        events, equality_operands = _install_equality_ordinal_failure(
            patcher,
            first,
            second,
            error,
            ordinal,
        )
        with pytest.raises(error_factory) as captured:
            discover_existing_startup(store, 1.0)

    assert captured.value is error
    assert getattr(captured.value, "__notes__", []) == []
    assert events == expected_flow
    assert len(equality_operands) == ordinal
    _assert_equality_operand_prefix(equality_operands, first, second)
    assert not _contains_identity(vars(discovery_module), error)


FAILURE_STAGES = [
    "clock-start",
    "load-first",
    "reconstruct-first",
    "clock-probe",
    "probe",
    "clock-second",
    "load-second",
    "reconstruct-second",
    "comparison",
    "clock-final",
]


def _install_stage_failure(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    first: SidecarState,
    second: SidecarState,
    error: BaseException,
) -> list[str]:
    events: list[str] = []
    loaded = [first, second]
    reconstructed: list[SidecarState] = []
    equality_calls = 0
    real_from_wire = SidecarState.from_wire
    real_equal = SidecarState.__eq__

    def load(_store: StateStore) -> SidecarState:
        index = sum(event.startswith("load-") for event in events)
        events.append(f"load-{index + 1}")
        if stage == "load-first" and index == 0:
            raise error
        if stage == "load-second" and index == 1:
            raise error
        return loaded[index]

    def probe(_state: SidecarState, _timeout: float) -> object:
        events.append("probe-1")
        if stage == "probe":
            raise error
        return True

    def from_wire(_cls: type[SidecarState], value: object) -> SidecarState:
        call_number = len(reconstructed) + 1
        events.append(f"reconstruct-{call_number}")
        if stage == "reconstruct-first" and call_number == 1:
            raise error
        if stage == "reconstruct-second" and call_number == 2:
            raise error
        result = real_from_wire(value)
        reconstructed.append(result)
        return result

    clock_stage = {
        "clock-start": 1,
        "clock-probe": 2,
        "clock-second": 3,
        "clock-final": 4,
    }.get(stage)

    def clock() -> float:
        call_number = sum(event.startswith("clock-") for event in events) + 1
        events.append(f"clock-{call_number}")
        if clock_stage == call_number:
            raise error
        return 0.1 * (call_number - 1)

    def equal(left: SidecarState, right: object) -> object:
        nonlocal equality_calls
        equality_calls += 1
        events.append(f"equal-{equality_calls}")
        if stage == "comparison" and equality_calls == 3:
            raise error
        return real_equal(left, right)

    monkeypatch.setattr(discovery_module, "_load_state", load)
    monkeypatch.setattr(discovery_module, "_probe_state", probe)
    monkeypatch.setattr(discovery_module.time, "monotonic", clock)
    monkeypatch.setattr(SidecarState, "from_wire", classmethod(from_wire))
    monkeypatch.setattr(SidecarState, "__eq__", equal)
    return events


def _expected_stage_events(stage: str) -> list[str]:
    complete = [
        "clock-1",
        "load-1",
        "reconstruct-1",
        "equal-1",
        "clock-2",
        "probe-1",
        "clock-3",
        "load-2",
        "reconstruct-2",
        "equal-2",
        "equal-3",
        "equal-4",
        "clock-4",
    ]
    prefix_lengths = {
        "clock-start": 1,
        "load-first": 2,
        "reconstruct-first": 3,
        "clock-probe": 5,
        "probe": 6,
        "clock-second": 7,
        "load-second": 8,
        "reconstruct-second": 9,
        "comparison": 11,
        "clock-final": 13,
    }
    return complete[: prefix_lengths[stage]]


def _contains_identity(
    value: object,
    target: object,
    seen: set[int] | None = None,
) -> bool:
    if value is target:
        return True
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if type(value) is dict:
        mapping: dict[object, object] = value
        return any(
            _contains_identity(key, target, seen) or _contains_identity(item, target, seen)
            for key, item in mapping.items()
        )
    if type(value) in {list, tuple, set, frozenset}:
        collection: list[object] | tuple[object, ...] | set[object] | frozenset[object] = value  # type: ignore[assignment]
        return any(_contains_identity(item, target, seen) for item in collection)
    return False


@pytest.mark.parametrize("stage", FAILURE_STAGES)
def test_ordinary_failure_at_every_stage_is_private_none_not_retried_or_retained(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    error = OSError(f"{PRIVATE}:{TOKEN}:{STARTUP_ID}:{PID}:{PORT}:{store.database_path}")
    caplog.set_level(logging.DEBUG)
    with pytest.MonkeyPatch.context() as patcher:
        events = _install_stage_failure(
            patcher,
            stage,
            first,
            second,
            error,
        )
        assert discover_existing_startup(store, 1.0) is None
        assert events == _expected_stage_events(stage)

    output = capsys.readouterr()
    sensitive = (PRIVATE, TOKEN, STARTUP_ID, str(PID), str(PORT), str(store.database_path))
    for value in sensitive:
        assert value not in output.out
        assert value not in output.err
        assert all(value not in record.getMessage() for record in caplog.records)
    assert not _contains_identity(vars(discovery_module), error)


@pytest.mark.parametrize("stage", FAILURE_STAGES)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_at_every_stage_preserves_identity_and_call_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    error = error_factory()
    events = _install_stage_failure(
        monkeypatch,
        stage,
        first,
        second,
        error,
    )
    with pytest.raises(error_factory) as captured:
        discover_existing_startup(store, 1.0)
    assert captured.value is error
    assert getattr(captured.value, "__notes__", []) == []
    assert events == _expected_stage_events(stage)


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_existing_health_cleanup_note_is_preserved_without_wrapper_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    error = error_factory()
    error.add_note("sidecar health probe cleanup failed")
    monkeypatch.setattr(discovery_module, "_load_state", lambda _store: first)
    monkeypatch.setattr(
        discovery_module,
        "_probe_state",
        lambda *_args: _raise(error),
    )
    _install_clock(monkeypatch, [0.0, 0.1])
    with pytest.raises(error_factory) as captured:
        discover_existing_startup(store, 1.0)
    assert captured.value is error
    assert captured.value.__notes__ == ["sidecar health probe cleanup failed"]


def test_frozen_unbound_loader_ignores_instance_and_later_class_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    store.publish(state)
    injected_calls: list[str] = []

    def injected(*_args: object, **_kwargs: object) -> NoReturn:
        injected_calls.append("load")
        raise AssertionError("injected load callback ran")

    object.__getattribute__(store, "__dict__")["load"] = injected
    monkeypatch.setattr(StateStore, "load", injected)
    monkeypatch.setattr(discovery_module, "_probe_state", lambda *_args: True)
    _install_clock(monkeypatch, [0.0, 0.1, 0.2, 0.3])

    result = discover_existing_startup(store, 1.0)

    assert type(result) is SidecarState
    assert result == state
    assert result is not state
    assert injected_calls == []


def _health_body(state: SidecarState) -> bytes:
    return json.dumps(
        {
            "status": "ok",
            "protocol_version": state.protocol_version,
            "state_schema_version": state.state_schema_version,
            "project_id": state.project_id,
            "startup_id": state.startup_id,
            "sidecar_pid": state.pid,
            "host": state.host,
            "port": state.port,
        },
        separators=(",", ":"),
    ).encode("ascii")


def test_real_published_state_and_one_bounded_loopback_health_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    listener: socket.socket | None = None
    thread: threading.Thread | None = None
    thread_started = False
    store: StateStore | None = None
    published: SidecarState | None = None
    result: SidecarState | None = None
    before_bytes: bytes | None = None
    before_signature: tuple[int, ...] | None = None
    captured_requests: list[bytes] = []
    accepted_clients: list[socket.socket] = []
    server_errors: list[BaseException] = []
    injected_probe_calls: list[str] = []
    started = threading.Event()
    finished = threading.Event()
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LOOPBACK_HOST, 0))
        listener.listen(1)
        listener.settimeout(2.0)
        server_listener = listener
        port = server_listener.getsockname()[1]
        assert type(port) is int
        store = _store(tmp_path)
        published = create_startup_state(store, server_listener)
        assert published.port == port
        store.publish(published)
        before_bytes = store.state_path.read_bytes()
        before_metadata = os.stat(store.state_path)
        before_signature = (
            before_metadata.st_dev,
            before_metadata.st_ino,
            before_metadata.st_mode,
            before_metadata.st_uid,
            before_metadata.st_gid,
            before_metadata.st_nlink,
            before_metadata.st_size,
            before_metadata.st_mtime_ns,
            before_metadata.st_ctime_ns,
        )
        body = _health_body(published)
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )

        def serve_once() -> None:
            client: socket.socket | None = None
            try:
                started.set()
                client, _address = server_listener.accept()
                accepted_clients.append(client)
                client.settimeout(2.0)
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = client.recv(1024)
                    if not chunk or len(request) + len(chunk) > 4096:
                        raise AssertionError("request framing was incomplete or oversized")
                    request.extend(chunk)
                captured_requests.append(bytes(request))
                client.sendall(response)
            except BaseException as error:
                server_errors.append(error)
            finally:
                if client is not None:
                    client.close()
                finished.set()

        thread = threading.Thread(
            target=serve_once,
            name="startup-discovery-test-server",
            daemon=True,
        )
        thread.start()
        thread_started = True
        assert started.wait(1.0)

        def injected_probe(*_args: object, **_kwargs: object) -> NoReturn:
            injected_probe_calls.append("probe")
            raise AssertionError("later module probe injection ran")

        monkeypatch.setattr(discovery_module, "probe_sidecar_health", injected_probe)
        monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
        result = discover_existing_startup(store, 1.0)
        assert finished.wait(3.0)
    finally:
        try:
            if listener is not None:
                listener.close()
        finally:
            if thread is not None and thread_started:
                thread.join(3.0)

    assert thread is not None
    assert not thread.is_alive()
    assert listener is not None
    assert listener.fileno() == -1
    assert len(accepted_clients) == 1
    assert accepted_clients[0].fileno() == -1
    assert server_errors == []
    assert injected_probe_calls == []
    assert store is not None
    assert published is not None
    assert before_bytes is not None
    assert before_signature is not None
    assert type(result) is SidecarState
    assert result == published
    assert result is not published
    assert store.state_path.read_bytes() == before_bytes
    after_metadata = os.stat(store.state_path)
    assert (
        after_metadata.st_dev,
        after_metadata.st_ino,
        after_metadata.st_mode,
        after_metadata.st_uid,
        after_metadata.st_gid,
        after_metadata.st_nlink,
        after_metadata.st_size,
        after_metadata.st_mtime_ns,
        after_metadata.st_ctime_ns,
    ) == before_signature
    assert captured_requests == [
        (
            f"GET /internal/v1/health HTTP/1.1\r\n"
            f"Host: {published.authority}\r\n"
            f"Authorization: Bearer {published.token}\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        ).encode("ascii")
    ]
    output = capsys.readouterr()
    assert published.token not in output.out
    assert published.token not in output.err
    assert str(store.database_path) not in output.out
    assert str(store.database_path) not in output.err


def test_real_no_state_path_performs_zero_network_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    attempts: list[str] = []

    def connect(*_args: object, **_kwargs: object) -> NoReturn:
        attempts.append("network")
        raise AssertionError("missing state reached network")

    monkeypatch.setattr(
        "flowsight.sidecar.health.http.client.HTTPConnection",
        connect,
    )
    assert discover_existing_startup(store, 1.0) is None
    assert attempts == []


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


def test_production_discovery_has_exact_alias_aware_read_only_ast_boundary() -> None:
    source_path = Path(discovery_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    assert len(tree.body) == 19
    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)
    assert (
        tree.body[0].value.value
        == "Read-only discovery of one already-published healthy sidecar startup."
    )
    module_imports = [
        (position, node)
        for position, node in enumerate(tree.body)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    all_imports = [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert all_imports == [node for _position, node in module_imports]
    observed_imports: list[
        tuple[int, str, int, str | None, tuple[tuple[str, str | None], ...]]
    ] = []
    for position, node in module_imports:
        if isinstance(node, ast.Import):
            observed_imports.append(
                (
                    position,
                    "import",
                    0,
                    None,
                    tuple((alias.name, alias.asname) for alias in node.names),
                )
            )
        else:
            assert isinstance(node, ast.ImportFrom)
            observed_imports.append(
                (
                    position,
                    "from",
                    node.level,
                    node.module,
                    tuple((alias.name, alias.asname) for alias in node.names),
                )
            )
    assert observed_imports == [
        (1, "from", 0, "__future__", (("annotations", None),)),
        (2, "import", 0, None, (("math", None),)),
        (3, "import", 0, None, (("time", None),)),
        (4, "from", 0, "typing", (("Final", None), ("cast", None))),
        (
            5,
            "from",
            1,
            "health",
            (("MAX_HEALTH_PROBE_TIMEOUT_SECONDS", None), ("probe_sidecar_health", None)),
        ),
        (6, "from", 1, "state", (("SidecarState", None), ("StateStore", None))),
    ]

    expected_function_positions = [
        (11, "_validate_timeout"),
        (12, "_load_state"),
        (13, "_probe_state"),
        (14, "_read_monotonic"),
        (15, "_observe_monotonic"),
        (16, "_remaining"),
        (17, "_rebuild_state"),
        (18, "discover_existing_startup"),
    ]
    module_function_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert [
        (position, node.name)
        for position, node in enumerate(tree.body)
        if isinstance(node, ast.FunctionDef)
    ] == expected_function_positions
    assert all(node.decorator_list == [] for node in module_function_nodes)
    all_function_nodes = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert all_function_nodes == module_function_nodes
    assert not any(
        isinstance(
            node,
            (
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
                ast.Delete,
                ast.NamedExpr,
            ),
        )
        for node in ast.walk(tree)
    )

    module_assignments = [
        (position, node)
        for position, node in enumerate(tree.body)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
    ]
    assert len(module_assignments) == 4
    assert all(isinstance(node, ast.AnnAssign) for _position, node in module_assignments)
    observed_finals: list[tuple[int, str, str | None, str | None, int]] = []
    for position, node in module_assignments:
        assert isinstance(node, ast.AnnAssign)
        assert isinstance(node.target, ast.Name)
        observed_finals.append(
            (
                position,
                node.target.id,
                _attribute_path(node.annotation),
                _attribute_path(node.value) if node.value is not None else None,
                node.simple,
            )
        )
    assert observed_finals == [
        (7, "_STATE_STORE_TYPE", "Final", "StateStore", 1),
        (8, "_SIDECAR_STATE_TYPE", "Final", "SidecarState", 1),
        (9, "_STATE_STORE_LOAD", "Final", "StateStore.load", 1),
        (10, "_PROBE_SIDECAR_HEALTH", "Final", "probe_sidecar_health", 1),
    ]

    expected_calls_by_function = {
        "_validate_timeout": {
            "type": 2,
            "TypeError": 1,
            "float": 1,
            "cast": 1,
            "math.isfinite": 1,
            "ValueError": 2,
        },
        "_load_state": {"_STATE_STORE_LOAD": 1},
        "_probe_state": {"_PROBE_SIDECAR_HEALTH": 1},
        "_read_monotonic": {"time.monotonic": 1},
        "_observe_monotonic": {
            "_read_monotonic": 1,
            "type": 1,
            "math.isfinite": 1,
        },
        "_remaining": {"_observe_monotonic": 1, "math.isfinite": 1},
        "_rebuild_state": {
            "type": 2,
            "_SIDECAR_STATE_TYPE.from_wire": 1,
            "state.to_wire": 1,
        },
        "discover_existing_startup": {
            "type": 1,
            "TypeError": 1,
            "_validate_timeout": 1,
            "_observe_monotonic": 1,
            "math.isfinite": 1,
            "_rebuild_state": 2,
            "_load_state": 2,
            "_remaining": 3,
            "_probe_state": 1,
        },
    }
    observed_calls_by_function: dict[str, dict[str, int]] = {}
    frozen_dispatches: list[tuple[str, str]] = []
    for function in module_function_nodes:
        observed_calls: dict[str, int] = {}
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            receiver = _attribute_path(node.func)
            assert receiver is not None
            observed_calls[receiver] = observed_calls.get(receiver, 0) + 1
            if receiver in {"_STATE_STORE_LOAD", "_PROBE_SIDECAR_HEALTH"}:
                frozen_dispatches.append((function.name, receiver))
        observed_calls_by_function[function.name] = observed_calls
    assert observed_calls_by_function == expected_calls_by_function
    assert frozen_dispatches == [
        ("_load_state", "_STATE_STORE_LOAD"),
        ("_probe_state", "_PROBE_SIDECAR_HEALTH"),
    ]
    public_calls = observed_calls_by_function["discover_existing_startup"]
    assert public_calls["_load_state"] == 2
    assert public_calls["_probe_state"] == 1
    assert (
        sum(
            calls.get("_load_state", 0)
            for name, calls in observed_calls_by_function.items()
            if name != "discover_existing_startup"
        )
        == 0
    )
    assert (
        sum(
            calls.get("_probe_state", 0)
            for name, calls in observed_calls_by_function.items()
            if name != "discover_existing_startup"
        )
        == 0
    )

    for node in ast.walk(tree):
        assert not isinstance(
            node,
            (
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.Global,
                ast.Nonlocal,
                ast.Delete,
                ast.NamedExpr,
                ast.Lambda,
                ast.AugAssign,
            ),
        )
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            assert not isinstance(node.ctx, (ast.Store, ast.Del))
        if isinstance(node, ast.FunctionDef):
            defaults = [*node.args.defaults, *node.args.kw_defaults]
            for default in defaults:
                if default is None:
                    continue
                assert not any(
                    isinstance(
                        descendant,
                        (
                            ast.List,
                            ast.Dict,
                            ast.Set,
                            ast.ListComp,
                            ast.SetComp,
                            ast.DictComp,
                            ast.GeneratorExp,
                        ),
                    )
                    for descendant in ast.walk(default)
                )
