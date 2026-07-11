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
from typing import NoReturn

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    LOOPBACK_HOST,
    SidecarState,
    StartupReady,
    StateStore,
    create_startup_state,
)
from flowsight.sidecar import startup_verification as verification_module
from flowsight.sidecar.startup_verification import verify_ready_startup

STARTUP_ID = "0123456789abcdef0123456789abcdef"
OTHER_STARTUP_ID = "fedcba9876543210fedcba9876543210"
TOKEN = "A" * 43
OTHER_TOKEN = "B" * 43
PROJECT_ID = "project-alpha"
PID = 4321
PORT = 4040
STARTED_AT_NS = 1_700_000_000_000_000_000
PRIVATE = "private-token-path-pid-errno-detail"


class _DerivedInt(int):
    pass


class _DerivedFloat(float):
    pass


class _DerivedStr(str):
    pass


class _DerivedStore(StateStore):
    pass


class _DerivedReady(StartupReady):
    pass


class _DerivedState(SidecarState):
    pass


class _RaisingDescriptor:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __get__(self, _instance: object, _owner: object) -> NoReturn:
        raise self.error


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
    assert copied is not state
    return copied


def _ready(state: SidecarState) -> StartupReady:
    return StartupReady(state.startup_id, state.pid, state.port)


def _assert_no_private_exception(error: BaseException, *values: object) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    for value in (PRIVATE, TOKEN, STARTUP_ID, str(PID), *values):
        try:
            encoded = str(value)
        except Exception:
            continue
        if len(encoded) == 1:
            # Single digits can be part of a fixed public range message (for
            # example the supported 30-second cap), so exact-message checks
            # above are the non-echo evidence for those caller values.
            continue
        assert encoded not in str(error)
        assert encoded not in repr(error)


def _raise(error: BaseException) -> NoReturn:
    raise error


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
    monkeypatch.setattr(
        "flowsight.sidecar.startup_verification.time.monotonic",
        clock.read,
    )
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

    monkeypatch.setattr(verification_module, "_load_state", load)
    monkeypatch.setattr(verification_module, "_probe_state", probe)
    clock = _install_clock(
        monkeypatch,
        [0.0, 0.1, 0.2, 0.3] if clock_values is None else clock_values,
        events,
    )
    return events, clock


def test_public_shape_and_export_are_exact() -> None:
    assert sidecar_package.verify_ready_startup is verify_ready_startup
    assert "verify_ready_startup" in sidecar_package.__all__
    signature = inspect.signature(verify_ready_startup)
    assert tuple(signature.parameters) == ("store", "ready", "timeout")
    assert signature.parameters["store"].default is inspect.Parameter.empty
    assert signature.parameters["ready"].default is inspect.Parameter.empty
    assert signature.parameters["timeout"].default == 0.5


@pytest.mark.parametrize("invalid", [None, object(), "store", True])
def test_wrong_store_type_fails_before_ready_clock_load_or_probe(
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        verification_module,
        "_rebuild_ready",
        lambda *_args: calls.append("ready"),
    )
    monkeypatch.setattr(
        verification_module,
        "_read_monotonic",
        lambda: calls.append("clock"),
    )
    monkeypatch.setattr(
        verification_module,
        "_load_state",
        lambda *_args: calls.append("load"),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_state",
        lambda *_args: calls.append("probe"),
    )
    with pytest.raises(TypeError, match="exact StateStore") as captured:
        verify_ready_startup(invalid, StartupReady(STARTUP_ID, PID, PORT))  # type: ignore[arg-type]
    _assert_no_private_exception(captured.value)
    assert calls == []


def test_store_subclass_is_rejected_before_work(tmp_path: Path) -> None:
    store = _DerivedStore(tmp_path / "derived", project_id=PROJECT_ID)
    with pytest.raises(TypeError, match="exact StateStore"):
        verify_ready_startup(store, StartupReady(STARTUP_ID, PID, PORT))


@pytest.mark.parametrize("invalid", [None, object(), "ready", True])
def test_wrong_ready_type_fails_before_clock_load_or_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        verification_module,
        "_read_monotonic",
        lambda: calls.append("clock"),
    )
    monkeypatch.setattr(
        verification_module,
        "_load_state",
        lambda *_args: calls.append("load"),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_state",
        lambda *_args: calls.append("probe"),
    )
    with pytest.raises(TypeError, match="exact StartupReady") as captured:
        verify_ready_startup(store, invalid)  # type: ignore[arg-type]
    _assert_no_private_exception(captured.value)
    assert calls == []


def test_ready_subclass_is_rejected_before_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ready = _DerivedReady(STARTUP_ID, PID, PORT)
    with pytest.raises(TypeError, match="exact StartupReady"):
        verify_ready_startup(store, ready)


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
def test_invalid_timeout_fails_before_ready_clock_load_or_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
    expected_type: type[Exception],
    expected_message: str,
) -> None:
    store = _store(tmp_path)
    ready = StartupReady(STARTUP_ID, PID, PORT)
    calls: list[str] = []
    monkeypatch.setattr(
        verification_module,
        "_rebuild_ready",
        lambda *_args: calls.append("ready"),
    )
    monkeypatch.setattr(
        verification_module,
        "_read_monotonic",
        lambda: calls.append("clock"),
    )
    monkeypatch.setattr(
        verification_module,
        "_load_state",
        lambda *_args: calls.append("load"),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_state",
        lambda *_args: calls.append("probe"),
    )
    with pytest.raises(expected_type) as captured:
        verify_ready_startup(store, ready, invalid)  # type: ignore[arg-type]
    assert str(captured.value) == expected_message
    assert type(captured.value) is expected_type
    _assert_no_private_exception(captured.value, invalid)
    assert calls == []


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("startup_id", TOKEN),
        ("startup_id", _DerivedStr(STARTUP_ID)),
        ("startup_id", "A" * 32),
        ("sidecar_pid", True),
        ("sidecar_pid", _DerivedInt(PID)),
        ("sidecar_pid", 0),
        ("port", True),
        ("port", _DerivedInt(PORT)),
        ("port", 0),
    ],
)
def test_forged_exact_ready_fails_fixed_private_before_clock_load_or_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    field: str,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    ready = StartupReady(STARTUP_ID, PID, PORT)
    object.__setattr__(ready, field, invalid)
    calls: list[str] = []
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        verification_module,
        "_read_monotonic",
        lambda: calls.append("clock"),
    )
    monkeypatch.setattr(
        verification_module,
        "_load_state",
        lambda *_args: calls.append("load"),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_state",
        lambda *_args: calls.append("probe"),
    )
    with pytest.raises(ValueError, match="ready is invalid") as captured:
        verify_ready_startup(store, ready)
    _assert_no_private_exception(captured.value, invalid)
    assert calls == []
    output = capsys.readouterr()
    assert TOKEN not in output.out
    assert TOKEN not in output.err
    assert all(TOKEN not in record.getMessage() for record in caplog.records)


def test_ready_preflight_returns_a_distinct_equal_exact_copy() -> None:
    ready = StartupReady(STARTUP_ID, PID, PORT)
    rebuilt = verification_module._rebuild_ready(ready)
    assert type(rebuilt) is StartupReady
    assert rebuilt == ready
    assert rebuilt is not ready


@pytest.mark.parametrize("field", ["startup_id", "sidecar_pid", "port"])
def test_ready_slot_descriptor_ordinary_failure_is_fixed_private_and_zero_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    store = _store(tmp_path)
    ready = StartupReady(STARTUP_ID, PID, PORT)
    error = OSError(f"{PRIVATE}:{TOKEN}:{STARTUP_ID}:{PID}:{store.database_path}")
    calls: list[str] = []
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(StartupReady, field, _RaisingDescriptor(error))
    monkeypatch.setattr(
        verification_module,
        "_read_monotonic",
        lambda: calls.append("clock"),
    )
    monkeypatch.setattr(
        verification_module,
        "_load_state",
        lambda *_args: calls.append("load"),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_state",
        lambda *_args: calls.append("probe"),
    )
    with pytest.raises(ValueError) as captured:
        verify_ready_startup(store, ready)
    assert type(captured.value) is ValueError
    assert str(captured.value) == "ready is invalid"
    _assert_no_private_exception(captured.value, store.database_path)
    assert calls == []
    output = capsys.readouterr()
    assert TOKEN not in output.out
    assert TOKEN not in output.err
    assert all(TOKEN not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("field", ["startup_id", "sidecar_pid", "port"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_ready_slot_descriptor_process_control_preserves_identity_and_zero_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    ready = StartupReady(STARTUP_ID, PID, PORT)
    error = error_factory()
    calls: list[str] = []
    monkeypatch.setattr(StartupReady, field, _RaisingDescriptor(error))
    monkeypatch.setattr(
        verification_module,
        "_read_monotonic",
        lambda: calls.append("clock"),
    )
    monkeypatch.setattr(
        verification_module,
        "_load_state",
        lambda *_args: calls.append("load"),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_state",
        lambda *_args: calls.append("probe"),
    )
    with pytest.raises(error_factory) as captured:
        verify_ready_startup(store, ready)
    assert captured.value is error
    assert calls == []


def test_exact_load_probe_load_order_and_second_object_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    events, clock = _install_flow(monkeypatch, [first, second])

    result = verify_ready_startup(store, _ready(first), 1.0)

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
def test_valid_timeout_is_normalized_and_forwarded_as_current_remaining_budget(
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
    assert verify_ready_startup(store, _ready(first), timeout) is second
    probes = [event for event in events if event[0] == "probe"]
    assert len(probes) == 1
    assert probes[0][1] is first
    assert probes[0][2] == pytest.approx(float(timeout) - 0.25)


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
    monkeypatch.setattr(verification_module, "_probe_state", lambda *_args: True)
    _install_clock(monkeypatch, [0.0, 0.1, 0.2, 0.3])

    result = verify_ready_startup(store, _ready(state), 1.0)

    assert type(result) is SidecarState
    assert result == state
    assert result is not state
    assert injected_calls == []
    assert verification_module._STATE_STORE_LOAD is not StateStore.load


@pytest.mark.parametrize("invalid", [None, object()])
def test_invalid_first_load_result_exits_before_probe_or_second_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    events, _clock = _install_flow(monkeypatch, [invalid])
    assert verify_ready_startup(store, _ready(state)) is None
    assert [event[0] for event in events] == ["clock", "load"]


def test_derived_first_load_result_is_rejected_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    derived = _DerivedState(**_state(store).to_wire())  # type: ignore[arg-type]
    events, _clock = _install_flow(monkeypatch, [derived])
    assert verify_ready_startup(store, StartupReady(STARTUP_ID, PID, PORT)) is None
    assert [event[0] for event in events] == ["clock", "load"]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("project_id", object()),
        ("startup_id", TOKEN),
        ("pid", True),
        ("port", 0),
        ("token", object()),
        ("database_path", object()),
        ("started_at_ns", 0),
    ],
)
def test_forged_exact_first_state_fails_revalidation_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    forged = _state(store)
    object.__setattr__(forged, field, invalid)
    events, _clock = _install_flow(monkeypatch, [forged])
    real_revalidate = verification_module._revalidate_loaded
    revalidation_calls = 0

    def revalidate(state: SidecarState) -> SidecarState:
        nonlocal revalidation_calls
        revalidation_calls += 1
        return real_revalidate(state)

    monkeypatch.setattr(verification_module, "_revalidate_loaded", revalidate)
    assert verify_ready_startup(store, StartupReady(STARTUP_ID, PID, PORT)) is None
    assert [event[0] for event in events] == ["clock", "load"]
    assert revalidation_calls == 1


def _invalid_revalidation_result(kind: str, raw: SidecarState) -> SidecarState:
    if kind == "raw-same-object":
        return raw
    if kind == "derived":
        return _DerivedState(**raw.to_wire())  # type: ignore[arg-type]
    if kind == "unequal-exact":
        values = raw.to_wire()
        values["token"] = OTHER_TOKEN
        return SidecarState.from_wire(values)
    raise AssertionError(f"unknown revalidation result: {kind}")


@pytest.mark.parametrize("kind", ["raw-same-object", "derived", "unequal-exact"])
def test_revalidate_loaded_rejects_inexact_or_nonfresh_from_wire_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    raw = _state(_store(tmp_path))
    invalid = _invalid_revalidation_result(kind, raw)

    def from_wire(_cls: type[SidecarState], _value: object) -> SidecarState:
        return invalid

    monkeypatch.setattr(SidecarState, "from_wire", classmethod(from_wire))
    with pytest.raises(verification_module._ReadyVerificationFailure):
        verification_module._revalidate_loaded(raw)


@pytest.mark.parametrize("position", ["first", "second"])
@pytest.mark.parametrize("kind", ["raw-same-object", "derived", "unequal-exact"])
def test_bad_from_wire_result_fails_at_exact_flow_prefix(
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
    invalid = _invalid_revalidation_result(kind, target_raw)
    events, _clock = _install_flow(monkeypatch, [first, second])
    real_revalidate = verification_module._revalidate_loaded
    revalidation_calls = 0

    def revalidate(raw: SidecarState) -> SidecarState:
        nonlocal revalidation_calls
        revalidation_calls += 1
        if revalidation_calls != target_call:
            return real_revalidate(raw)

        def from_wire(_cls: type[SidecarState], _value: object) -> SidecarState:
            return invalid

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(SidecarState, "from_wire", classmethod(from_wire))
            return real_revalidate(raw)

    monkeypatch.setattr(verification_module, "_revalidate_loaded", revalidate)
    assert verify_ready_startup(store, _ready(first)) is None
    if position == "first":
        assert [event[0] for event in events] == ["clock", "load"]
        assert revalidation_calls == 1
    else:
        assert [event[0] for event in events] == [
            "clock",
            "load",
            "clock",
            "probe",
            "clock",
            "load",
        ]
        assert revalidation_calls == 2


@pytest.mark.parametrize(
    "mismatch",
    [
        StartupReady(OTHER_STARTUP_ID, PID, PORT),
        StartupReady(STARTUP_ID, PID + 1, PORT),
        StartupReady(STARTUP_ID, PID, PORT + 1),
    ],
)
def test_ready_mismatch_makes_zero_network_attempts_and_zero_second_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: StartupReady,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    load_calls = 0
    probe_calls = 0
    network_attempts = 0

    def load(_store: StateStore) -> SidecarState:
        nonlocal load_calls
        load_calls += 1
        return first

    def connection(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal network_attempts
        network_attempts += 1
        raise AssertionError("READY mismatch reached HTTP connection")

    def probe(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal probe_calls
        probe_calls += 1
        raise AssertionError("READY mismatch reached health probe")

    monkeypatch.setattr(verification_module, "_load_state", load)
    monkeypatch.setattr(verification_module, "_probe_state", probe)
    monkeypatch.setattr(
        "flowsight.sidecar.health.http.client.HTTPConnection",
        connection,
    )
    _install_clock(monkeypatch, [0.0])

    assert verify_ready_startup(store, mismatch) is None
    assert load_calls == 1
    assert probe_calls == 0
    assert network_attempts == 0


@pytest.mark.parametrize("probe_result", [False, None, 1, "true", object()])
def test_only_exact_true_probe_result_can_reach_second_load(
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
    assert verify_ready_startup(store, _ready(first)) is None
    assert [event[0] for event in events] == ["clock", "load", "clock", "probe"]


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
def test_deadline_expiry_at_each_admission_boundary_rejects_late_success(
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
    assert verify_ready_startup(store, _ready(first), 1.0) is None
    assert [event[0] for event in events] == expected_names


@pytest.mark.parametrize("invalid_clock", [True, 0, math.nan, math.inf, -math.inf])
def test_invalid_initial_clock_result_fails_before_load(
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
    assert verify_ready_startup(store, _ready(state)) is None
    assert [event[0] for event in events] == ["clock"]


def test_clock_rollback_fails_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    events, _clock = _install_flow(
        monkeypatch,
        [state],
        clock_values=[1.0, 0.9],
    )
    assert verify_ready_startup(store, _ready(state)) is None
    assert [event[0] for event in events] == ["clock", "load", "clock"]


def test_deadline_overflow_fails_before_load(
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
    assert verify_ready_startup(store, _ready(state), 30.0) is None
    assert [event[0] for event in events] == ["clock"]


@pytest.mark.parametrize("invalid", [None, object()])
def test_invalid_second_load_result_fails_after_one_probe_and_two_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    events, _clock = _install_flow(monkeypatch, [first, invalid])
    assert verify_ready_startup(store, _ready(first)) is None
    assert [event[0] for event in events] == [
        "clock",
        "load",
        "clock",
        "probe",
        "clock",
        "load",
    ]


def test_derived_second_load_result_is_rejected_after_one_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    derived = _DerivedState(**first.to_wire())  # type: ignore[arg-type]
    events, _clock = _install_flow(monkeypatch, [first, derived])
    assert verify_ready_startup(store, _ready(first)) is None
    assert [event[0] for event in events].count("load") == 2
    assert [event[0] for event in events].count("probe") == 1


def test_same_raw_object_reused_by_second_load_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    events, _clock = _install_flow(monkeypatch, [first, first])
    assert verify_ready_startup(store, _ready(first)) is None
    assert [event[0] for event in events].count("load") == 2
    assert [event[0] for event in events].count("probe") == 1


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
    assert verify_ready_startup(store, _ready(first)) is None
    assert [event[0] for event in events].count("load") == 2
    assert [event[0] for event in events].count("probe") == 1


FAILURE_STAGES = [
    "clock-start",
    "load-first",
    "revalidate-first",
    "clock-probe",
    "probe",
    "clock-second",
    "load-second",
    "revalidate-second",
    "comparison",
    "clock-final",
]


def _install_stage_failure(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    store: StateStore,
    first: SidecarState,
    second: SidecarState,
    error: BaseException,
) -> tuple[list[str], list[str], list[str]]:
    loads: list[str] = []
    probes: list[str] = []
    clocks: list[str] = []
    loaded = [first, second]
    revalidation_calls = 0

    def load(_store: StateStore) -> SidecarState:
        index = len(loads)
        loads.append("load")
        if stage == "load-first" and index == 0:
            raise error
        if stage == "load-second" and index == 1:
            raise error
        return loaded[index]

    def probe(_state: SidecarState, _timeout: float) -> object:
        probes.append("probe")
        if stage == "probe":
            raise error
        return True

    real_revalidate = verification_module._revalidate_loaded

    def revalidate(state: SidecarState) -> SidecarState:
        nonlocal revalidation_calls
        revalidation_calls += 1
        if stage == "revalidate-first" and revalidation_calls == 1:
            raise error
        if stage == "revalidate-second" and revalidation_calls == 2:
            raise error
        return real_revalidate(state)

    clock_stage = {
        "clock-start": 1,
        "clock-probe": 2,
        "clock-second": 3,
        "clock-final": 4,
    }.get(stage)

    def clock() -> float:
        clocks.append("clock")
        if clock_stage == len(clocks):
            raise error
        return 0.1 * (len(clocks) - 1)

    monkeypatch.setattr(verification_module, "_load_state", load)
    monkeypatch.setattr(verification_module, "_probe_state", probe)
    monkeypatch.setattr(verification_module, "_revalidate_loaded", revalidate)
    monkeypatch.setattr(
        "flowsight.sidecar.startup_verification.time.monotonic",
        clock,
    )
    if stage == "comparison":
        real_equal = SidecarState.__eq__
        equality_calls = 0

        def equal(left: SidecarState, right: object) -> object:
            nonlocal equality_calls
            equality_calls += 1
            if equality_calls == 3:
                raise error
            return real_equal(left, right)

        monkeypatch.setattr(SidecarState, "__eq__", equal)
    return loads, probes, clocks


def _expected_stage_counts(stage: str) -> tuple[int, int, int]:
    if stage == "clock-start":
        return 0, 0, 1
    if stage in {"load-first", "revalidate-first"}:
        return 1, 0, 1
    if stage == "clock-probe":
        return 1, 0, 2
    if stage == "probe":
        return 1, 1, 2
    if stage == "clock-second":
        return 1, 1, 3
    if stage in {"load-second", "revalidate-second"}:
        return 2, 1, 3
    if stage in {"comparison", "clock-final"}:
        return 2, 1, 4 if stage == "clock-final" else 3
    raise AssertionError(f"unknown failure stage: {stage}")


@pytest.mark.parametrize("stage", FAILURE_STAGES)
def test_ordinary_failure_at_every_stage_is_private_none_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    store = _store(tmp_path)
    first = _state(store)
    second = _copy_state(first)
    error = OSError(f"{PRIVATE}:{TOKEN}:{STARTUP_ID}:{PID}:{store.database_path}")
    caplog.set_level(logging.DEBUG)
    loads, probes, clocks = _install_stage_failure(
        monkeypatch,
        stage,
        store,
        first,
        second,
        error,
    )
    assert verify_ready_startup(store, _ready(first), 1.0) is None
    expected_loads, expected_probes, expected_clocks = _expected_stage_counts(stage)
    assert len(loads) == expected_loads
    assert len(probes) == expected_probes
    assert len(clocks) == expected_clocks
    output = capsys.readouterr()
    sensitive = (PRIVATE, TOKEN, STARTUP_ID, str(PID), str(store.database_path))
    for value in sensitive:
        assert value not in output.out
        assert value not in output.err
        assert all(value not in record.getMessage() for record in caplog.records)
    assert all(value is not error for value in vars(verification_module).values())


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
    loads, probes, clocks = _install_stage_failure(
        monkeypatch,
        stage,
        store,
        first,
        second,
        error,
    )
    with pytest.raises(error_factory) as captured:
        verify_ready_startup(store, _ready(first), 1.0)
    assert captured.value is error
    expected_loads, expected_probes, expected_clocks = _expected_stage_counts(stage)
    assert len(loads) == expected_loads
    assert len(probes) == expected_probes
    assert len(clocks) == expected_clocks


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
    monkeypatch.setattr(verification_module, "_load_state", lambda _store: first)
    monkeypatch.setattr(
        verification_module,
        "_probe_state",
        lambda *_args: _raise(error),
    )
    _install_clock(monkeypatch, [0.0, 0.1])
    with pytest.raises(error_factory) as captured:
        verify_ready_startup(store, _ready(first), 1.0)
    assert captured.value is error
    assert captured.value.__notes__ == ["sidecar health probe cleanup failed"]


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
    server_errors: list[BaseException] = []
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
        ready = _ready(published)
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
            name="startup-verification-test-server",
            daemon=True,
        )
        thread.start()
        thread_started = True
        assert started.wait(1.0)
        monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
        result = verify_ready_startup(store, ready, 1.0)
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
    assert server_errors == []
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


def test_production_verifier_has_no_policy_mutation_retry_or_cache_behavior() -> None:
    source_path = Path(verification_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_import_roots = {
        "asyncio",
        "logging",
        "multiprocessing",
        "sqlite3",
        "spikes",
        "subprocess",
        "threading",
        "uvicorn",
        "webbrowser",
    }
    forbidden_calls = {
        "acquire",
        "bind",
        "close",
        "create_startup_state",
        "ensure_private_directory",
        "kill",
        "listen",
        "open_startup_channel",
        "poll",
        "print",
        "publish",
        "remove_if_owned",
        "send",
        "shutdown",
        "sleep",
        "spawn",
        "start",
        "terminate",
        "wait",
    }
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.For, ast.AsyncFor, ast.While))
        if isinstance(node, ast.Import):
            assert all(
                alias.name.split(".", 1)[0] not in forbidden_import_roots for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".", 1)[0] not in forbidden_import_roots
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls
                assert node.func.attr != "load"
            elif isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls
    public = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    assert public == ["verify_ready_startup"]

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    load_calls = [
        node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "_load_state"
    ]
    probe_calls = [
        node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "_probe_state"
    ]
    assert len(load_calls) == 2
    assert len(probe_calls) == 1

    frozen_loads = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == "_STATE_STORE_LOAD"
    ]
    assert len(frozen_loads) == 1
    frozen_value = frozen_loads[0].value
    assert isinstance(frozen_value, ast.Attribute)
    assert isinstance(frozen_value.value, ast.Name)
    assert frozen_value.value.id == "StateStore"
    assert frozen_value.attr == "load"
    assert verification_module._STATE_STORE_LOAD is StateStore.load
