from __future__ import annotations

import ast
import errno
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
    StartupAdmissionError,
    StartupAdmissionErrorCode,
    StartupChannelError,
    StartupChannelErrorCode,
    StartupFailure,
    StartupFailureCode,
    StartupReader,
    StartupReady,
    StartupWriter,
    StateStore,
    create_startup_state,
    open_startup_channel,
    receive_startup_outcome,
)
from flowsight.sidecar import startup_admission as admission_module
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
PRIVATE = "private-token-path-pid-fd-errno-detail"

REAL_CLOSE = os.close
REAL_FSTAT = os.fstat


class _DerivedInt(int):
    pass


class _DerivedFloat(float):
    pass


class _DerivedStore(StateStore):
    pass


class _DerivedReader(StartupReader):
    pass


class _DerivedFailure(StartupFailure):
    pass


class _DerivedReady(StartupReady):
    pass


class _DerivedState(SidecarState):
    pass


class _DerivedChannelError(StartupChannelError):
    pass


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
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "runtime", project_id=PROJECT_ID)


def _state(
    store: StateStore,
    *,
    startup_id: str = STARTUP_ID,
    token: str = TOKEN,
) -> SidecarState:
    return SidecarState(
        project_id=PROJECT_ID,
        startup_id=startup_id,
        pid=PID,
        port=PORT,
        token=token,
        database_path=str(store.database_path),
        started_at_ns=STARTED_AT_NS,
    )


def _copy_state(state: SidecarState) -> SidecarState:
    copied = SidecarState.from_wire(state.to_wire())
    assert copied is not state
    return copied


def _ready(state: SidecarState) -> StartupReady:
    return StartupReady(state.startup_id, state.pid, state.port)


def _failure() -> StartupFailure:
    return StartupFailure(StartupFailureCode.SIDECAR_STARTUP_FAILED)


def _raise(error: BaseException) -> NoReturn:
    raise error


def _assert_raw_descriptor_closed(file_descriptor: int) -> None:
    with pytest.raises(OSError) as captured:
        REAL_FSTAT(file_descriptor)
    assert captured.value.errno == errno.EBADF


def _assert_no_private_exception(error: BaseException, *values: object) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    for value in (PRIVATE, TOKEN, STARTUP_ID, str(PID), *values):
        try:
            encoded = str(value)
        except Exception:
            continue
        if len(encoded) > 1:
            assert encoded not in str(error)
            assert encoded not in repr(error)


def _builtins_retain_identity(
    value: object,
    target: object,
    seen: set[int],
) -> bool:
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
        items: list[object] | tuple[object, ...] | set[object] | frozenset[object] = value
        return any(_builtins_retain_identity(item, target, seen) for item in items)
    return False


def _assert_module_does_not_retain_identity(target: object) -> None:
    for name, value in vars(admission_module).items():
        if name != "__builtins__":
            assert not _builtins_retain_identity(value, target, set())


def _assert_reader_closed(reader: StartupReader) -> StartupChannelError:
    with pytest.raises(StartupChannelError) as captured:
        reader.fileno()
    assert type(captured.value) is StartupChannelError
    assert captured.value.code is StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED
    return captured.value


def test_public_shape_exports_and_error_contract_are_exact() -> None:
    assert sidecar_package.receive_startup_outcome is receive_startup_outcome
    assert sidecar_package.StartupAdmissionError is StartupAdmissionError
    assert sidecar_package.StartupAdmissionErrorCode is StartupAdmissionErrorCode
    assert sidecar_package.__all__.count("receive_startup_outcome") == 1
    assert sidecar_package.__all__.count("StartupAdmissionError") == 1
    assert sidecar_package.__all__.count("StartupAdmissionErrorCode") == 1

    signature = inspect.signature(receive_startup_outcome)
    assert tuple(signature.parameters) == ("store", "reader", "timeout")
    assert signature.parameters["store"].default is inspect.Parameter.empty
    assert signature.parameters["reader"].default is inspect.Parameter.empty
    assert signature.parameters["timeout"].default == 0.5
    assert get_type_hints(receive_startup_outcome) == {
        "store": StateStore,
        "reader": StartupReader,
        "timeout": float,
        "return": SidecarState | StartupFailure | None,
    }
    assert tuple(StartupAdmissionErrorCode) == (
        StartupAdmissionErrorCode.STARTUP_ADMISSION_DEADLINE_FAILED,
    )

    error_signature = inspect.signature(StartupAdmissionError)
    assert tuple(error_signature.parameters) == ("code",)
    error = StartupAdmissionError(StartupAdmissionErrorCode.STARTUP_ADMISSION_DEADLINE_FAILED)
    assert type(error) is StartupAdmissionError
    assert error.code is StartupAdmissionErrorCode.STARTUP_ADMISSION_DEADLINE_FAILED
    assert str(error) == ("sidecar startup admission failed (STARTUP_ADMISSION_DEADLINE_FAILED)")
    _assert_no_private_exception(error)


@pytest.mark.parametrize("invalid", [None, object(), "code", True])
def test_admission_error_rejects_inexact_code_without_private_context(invalid: object) -> None:
    with pytest.raises(TypeError) as captured:
        StartupAdmissionError(invalid)  # type: ignore[arg-type]
    assert str(captured.value) == "code must be an exact StartupAdmissionErrorCode"
    _assert_no_private_exception(captured.value)


@pytest.mark.parametrize("invalid", [None, object(), "store", True])
def test_wrong_store_type_fails_before_reader_clock_or_runtime_work(
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(admission_module, "_reader_fileno", lambda *_args: calls.append("fileno"))
    monkeypatch.setattr(admission_module.time, "monotonic", lambda: calls.append("clock"))
    monkeypatch.setattr(
        admission_module,
        "_reader_receive",
        lambda *_args: calls.append("receive"),
    )
    monkeypatch.setattr(admission_module, "_verify_ready", lambda *_args: calls.append("verify"))
    with pytest.raises(TypeError, match="exact StateStore"):
        receive_startup_outcome(invalid, object())  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.parametrize("invalid", [None, object(), "reader", True])
def test_wrong_reader_type_fails_before_reader_fields_clock_or_runtime_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(admission_module, "_reader_fileno", lambda *_args: calls.append("fileno"))
    monkeypatch.setattr(admission_module.time, "monotonic", lambda: calls.append("clock"))
    monkeypatch.setattr(
        admission_module,
        "_reader_receive",
        lambda *_args: calls.append("receive"),
    )
    monkeypatch.setattr(admission_module, "_verify_ready", lambda *_args: calls.append("verify"))
    with pytest.raises(TypeError, match="exact StartupReader"):
        receive_startup_outcome(_store(tmp_path), invalid)  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.parametrize(
    ("invalid", "error_type"),
    [
        (True, TypeError),
        (False, TypeError),
        ("0.5", TypeError),
        (object(), TypeError),
        (_DerivedInt(1), TypeError),
        (_DerivedFloat(0.5), TypeError),
        (0, ValueError),
        (0.0, ValueError),
        (-0.1, ValueError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        (-math.inf, ValueError),
        (30.1, ValueError),
        pytest.param(10**10_000, ValueError, id="huge-int"),
    ],
)
def test_invalid_timeout_leaves_real_reader_usable_for_later_roundtrip(
    tmp_path: Path,
    invalid: object,
    error_type: type[Exception],
) -> None:
    reader, writer = open_startup_channel()
    with reader, writer:
        with pytest.raises(error_type) as captured:
            receive_startup_outcome(_store(tmp_path), reader, invalid)  # type: ignore[arg-type]
        expected_message = (
            "timeout must be a built-in int or float"
            if error_type is TypeError
            else "timeout must be finite, positive, and at most 30 seconds"
        )
        assert type(captured.value) is error_type
        assert str(captured.value) == expected_message
        _assert_no_private_exception(captured.value, invalid)
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


def test_store_and_reader_subclasses_are_rejected_before_work(tmp_path: Path) -> None:
    reader, writer = open_startup_channel()
    try:
        derived_store = _DerivedStore(tmp_path / "derived", project_id=PROJECT_ID)
        with pytest.raises(TypeError, match="exact StateStore"):
            receive_startup_outcome(derived_store, reader)

        derived_reader = object.__new__(_DerivedReader)
        object.__setattr__(derived_reader, "_descriptor", reader.fileno())
        with pytest.raises(TypeError, match="exact StartupReader"):
            receive_startup_outcome(_store(tmp_path), derived_reader)
    finally:
        reader.close()
        writer.close()


def _fake_reader() -> StartupReader:
    reader = object.__new__(StartupReader)
    object.__setattr__(reader, "_descriptor", -1)
    return reader


def _install_unit_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: object,
    candidate: object = None,
    clock_values: list[object] | None = None,
) -> tuple[StartupReader, list[tuple[object, ...]], _Clock]:
    reader = _fake_reader()
    events: list[tuple[object, ...]] = []

    def fileno(observed_reader: StartupReader) -> tuple[object | None, object | None]:
        events.append(("fileno", observed_reader))
        return 11, None

    def receive(
        observed_reader: StartupReader,
        timeout: float,
    ) -> tuple[object | None, object | None]:
        events.append(("receive", observed_reader, timeout))
        return message, None

    def verify(
        store: StateStore,
        ready: StartupReady,
        timeout: float,
    ) -> object:
        events.append(("verify", store, ready, timeout))
        return candidate

    monkeypatch.setattr(admission_module, "_reader_fileno", fileno)
    monkeypatch.setattr(admission_module, "_reader_receive", receive)
    monkeypatch.setattr(admission_module, "_verify_ready", verify)
    clock = _Clock(
        [0.0, 0.1, 0.2, 0.3] if clock_values is None else clock_values,
        events,
    )
    monkeypatch.setattr(admission_module, "_read_monotonic", clock.read)
    return reader, events, clock


def test_exact_ready_flow_shares_one_deadline_and_returns_verifier_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    candidate = _state(store)
    ready = _ready(candidate)
    reader, events, clock = _install_unit_flow(
        monkeypatch,
        message=ready,
        candidate=candidate,
    )

    assert receive_startup_outcome(store, reader, 1.0) is candidate
    assert [event[0] for event in events] == [
        "fileno",
        "clock",
        "clock",
        "receive",
        "clock",
        "verify",
        "clock",
    ]
    receive_event = events[3]
    verify_event = events[5]
    assert receive_event[1] is reader
    assert receive_event[2] == pytest.approx(0.9)
    assert verify_event[1] is store
    assert verify_event[2] is ready
    assert verify_event[3] == pytest.approx(0.8)
    assert clock.calls == 4


def test_exact_failure_returns_received_identity_without_post_receive_clock_or_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    failure = _failure()
    reader, events, clock = _install_unit_flow(
        monkeypatch,
        message=failure,
        clock_values=[0.0, 0.1],
    )

    assert receive_startup_outcome(store, reader, 1.0) is failure
    assert [event[0] for event in events] == ["fileno", "clock", "clock", "receive"]
    assert events[-1][1] is reader
    assert events[-1][2] == pytest.approx(0.9)
    assert clock.calls == 2


@pytest.mark.parametrize(
    "message",
    [
        None,
        object(),
        _DerivedFailure(StartupFailureCode.SIDECAR_STARTUP_FAILED),
        _DerivedReady(STARTUP_ID, PID, PORT),
    ],
)
def test_inexact_or_unknown_message_exits_without_post_receive_clock_or_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: object,
) -> None:
    reader, events, clock = _install_unit_flow(
        monkeypatch,
        message=message,
        clock_values=[0.0, 0.1],
    )
    assert receive_startup_outcome(_store(tmp_path), reader, 1.0) is None
    assert [event[0] for event in events] == ["fileno", "clock", "clock", "receive"]
    assert clock.calls == 2


@pytest.mark.parametrize("invalid", [None, True, 2, _DerivedInt(7)])
def test_malformed_reader_result_is_fixed_and_real_reader_remains_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    reader, writer = open_startup_channel()
    with reader, writer:
        with monkeypatch.context() as patcher:
            patcher.setattr(admission_module, "_reader_fileno", lambda _reader: (invalid, None))
            with pytest.raises(ValueError) as captured:
                receive_startup_outcome(_store(tmp_path), reader)
            assert type(captured.value) is ValueError
            assert str(captured.value) == "reader is invalid"
            _assert_no_private_exception(captured.value, invalid)
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


def test_ordinary_reader_preflight_failure_is_fixed_and_context_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    private_error = OSError(f"{PRIVATE}:{reader.fileno()}")
    with reader, writer:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                admission_module,
                "_reader_fileno",
                lambda _reader: _raise(private_error),
            )
            with pytest.raises(ValueError) as captured:
                receive_startup_outcome(_store(tmp_path), reader)
            assert type(captured.value) is ValueError
            assert str(captured.value) == "reader is invalid"
            _assert_no_private_exception(captured.value, reader.fileno())
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


def test_exact_tagged_channel_error_from_preflight_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    error = StartupChannelError(StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)
    error.add_note("sidecar startup channel cleanup failed")
    with reader, writer:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                admission_module,
                "_reader_fileno",
                lambda _reader: (None, error),
            )
            with pytest.raises(StartupChannelError) as captured:
                receive_startup_outcome(_store(tmp_path), reader)
            assert captured.value is error
            assert captured.value.__notes__ == ["sidecar startup channel cleanup failed"]
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


def test_real_closed_reader_preflight_preserves_canonical_channel_error(tmp_path: Path) -> None:
    reader, writer = open_startup_channel()
    reader.close()
    try:
        with pytest.raises(StartupChannelError) as captured:
            receive_startup_outcome(_store(tmp_path), reader, 1.0)
        assert type(captured.value) is StartupChannelError
        assert captured.value.code is StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED
        assert str(captured.value) == ("sidecar startup channel failed (STARTUP_CHANNEL_CLOSED)")
        _assert_no_private_exception(captured.value)
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("tagged", [False, True])
def test_derived_channel_error_at_preflight_is_malformed_reader_not_channel_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tagged: bool,
) -> None:
    reader, writer = open_startup_channel()
    error = _DerivedChannelError(StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)
    with reader, writer:
        with monkeypatch.context() as patcher:
            if tagged:
                patcher.setattr(
                    admission_module,
                    "_reader_fileno",
                    lambda _reader: (None, error),
                )
            else:
                patcher.setattr(
                    admission_module,
                    "_reader_fileno",
                    lambda _reader: _raise(error),
                )
            with pytest.raises(ValueError, match="reader is invalid") as captured:
                receive_startup_outcome(_store(tmp_path), reader)
            assert type(captured.value) is ValueError
            _assert_no_private_exception(captured.value)
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


@pytest.mark.parametrize(
    ("values", "timeout"),
    [
        ([True], 1.0),
        ([0], 1.0),
        ([_DerivedFloat(0.1)], 1.0),
        ([math.nan], 1.0),
        ([math.inf], 1.0),
        ([-math.inf], 1.0),
        ([OSError(PRIVATE)], 1.0),
        ([0.0, True], 1.0),
        ([0.0, math.nan], 1.0),
        ([0.0, OSError(PRIVATE)], 1.0),
        ([1.0, 0.9], 1.0),
        ([0.0, 1.0], 0.5),
        ([float.fromhex("0x1.fffffffffffffp+1023")], 30.0),
    ],
)
def test_every_pretransfer_clock_failure_is_fixed_and_reader_can_roundtrip_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: list[object],
    timeout: float,
) -> None:
    reader, writer = open_startup_channel()
    receive_calls: list[object] = []
    verify_calls: list[object] = []
    with reader, writer:
        with monkeypatch.context() as patcher:
            clock = _Clock(values)
            patcher.setattr(admission_module, "_read_monotonic", clock.read)
            patcher.setattr(
                admission_module,
                "_reader_receive",
                lambda *args: receive_calls.append(args),
            )
            patcher.setattr(
                admission_module,
                "_verify_ready",
                lambda *args: verify_calls.append(args),
            )
            with pytest.raises(StartupAdmissionError) as captured:
                receive_startup_outcome(_store(tmp_path), reader, timeout)
            assert type(captured.value) is StartupAdmissionError
            assert (
                captured.value.code is StartupAdmissionErrorCode.STARTUP_ADMISSION_DEADLINE_FAILED
            )
            _assert_no_private_exception(captured.value)
            assert reader.fileno() >= 3
            assert receive_calls == []
            assert verify_calls == []
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


@pytest.mark.parametrize("stage", ["start", "pre-receive"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_pretransfer_clock_process_control_preserves_identity_and_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    reader, writer = open_startup_channel()
    error = error_factory()
    values: list[object] = [error] if stage == "start" else [0.0, error]
    with reader, writer:
        with monkeypatch.context() as patcher:
            patcher.setattr(admission_module, "_read_monotonic", _Clock(values).read)
            with pytest.raises(error_factory) as captured:
                receive_startup_outcome(_store(tmp_path), reader, 1.0)
            assert captured.value is error
            assert reader.fileno() >= 3
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


def test_frozen_direct_dispatch_bindings_are_canonical() -> None:
    assert admission_module._STARTUP_READER_FILENO is StartupReader.fileno
    assert admission_module._STARTUP_READER_RECEIVE is StartupReader.receive
    assert admission_module._VERIFY_READY_STARTUP is verify_ready_startup


def test_frozen_receive_ignores_later_class_receive_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    with reader, writer:
        writer.send(_failure())
        monkeypatch.setattr(
            StartupReader,
            "receive",
            lambda *_args: (_ for _ in ()).throw(AssertionError("replacement called")),
        )
        assert receive_startup_outcome(_store(tmp_path), reader, 1.0) == _failure()


def test_frozen_preflight_ignores_later_class_fileno_replacement_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    with reader, writer:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                StartupReader,
                "fileno",
                lambda *_args: (_ for _ in ()).throw(AssertionError("replacement called")),
            )
            patcher.setattr(admission_module, "_read_monotonic", _Clock([OSError(PRIVATE)]).read)
            with pytest.raises(StartupAdmissionError):
                receive_startup_outcome(_store(tmp_path), reader, 1.0)
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


def test_frozen_verifier_ignores_later_module_function_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    monkeypatch.setattr(
        verification_module,
        "verify_ready_startup",
        lambda *_args: (_ for _ in ()).throw(AssertionError("replacement called")),
    )
    assert admission_module._verify_ready(store, _ready(state), 0.1) is None


def test_private_message_and_state_rebuilders_reject_inexact_and_forged_values(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    failure = _failure()
    ready = _ready(state)
    assert admission_module._rebuild_failure(failure) is failure
    assert admission_module._rebuild_ready(ready) is ready
    assert admission_module._rebuild_state(state) is state

    assert (
        admission_module._rebuild_failure(
            _DerivedFailure(StartupFailureCode.SIDECAR_STARTUP_FAILED)
        )
        is None
    )
    assert admission_module._rebuild_ready(_DerivedReady(STARTUP_ID, PID, PORT)) is None
    assert (
        admission_module._rebuild_state(
            _DerivedState(**state.to_wire())  # type: ignore[arg-type]
        )
        is None
    )

    forged_failure = object.__new__(StartupFailure)
    object.__setattr__(forged_failure, "code", PRIVATE)
    forged_ready = object.__new__(StartupReady)
    object.__setattr__(forged_ready, "startup_id", OTHER_TOKEN)
    object.__setattr__(forged_ready, "sidecar_pid", PID)
    object.__setattr__(forged_ready, "port", PORT)
    forged_state = _copy_state(state)
    object.__setattr__(forged_state, "token", "short")
    assert admission_module._rebuild_failure(forged_failure) is None
    assert admission_module._rebuild_ready(forged_ready) is None
    assert admission_module._rebuild_state(forged_state) is None


@pytest.mark.parametrize("candidate_kind", ["none", "object", "derived", "forged"])
def test_invalid_verifier_result_never_reaches_final_success_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    store = _store(tmp_path)
    valid = _state(store)
    if candidate_kind == "none":
        candidate: object = None
    elif candidate_kind == "object":
        candidate = object()
    elif candidate_kind == "derived":
        candidate = _DerivedState(**valid.to_wire())  # type: ignore[arg-type]
    else:
        candidate = _copy_state(valid)
        object.__setattr__(candidate, "token", "short")
    reader, events, clock = _install_unit_flow(
        monkeypatch,
        message=_ready(valid),
        candidate=candidate,
        clock_values=[0.0, 0.1, 0.2],
    )
    assert receive_startup_outcome(store, reader, 1.0) is None
    assert [event[0] for event in events] == [
        "fileno",
        "clock",
        "clock",
        "receive",
        "clock",
        "verify",
    ]
    assert clock.calls == 3


@pytest.mark.parametrize("tagged", [False, True])
def test_exact_receive_channel_error_propagates_only_when_canonical_stage_tags_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tagged: bool,
) -> None:
    error = StartupChannelError(StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
    error.add_note("sidecar startup channel cleanup failed")
    reader, _events, _clock = _install_unit_flow(
        monkeypatch,
        message=None,
        clock_values=[0.0, 0.1],
    )
    if tagged:
        monkeypatch.setattr(
            admission_module,
            "_reader_receive",
            lambda *_args: (None, error),
        )
        with pytest.raises(StartupChannelError) as captured:
            receive_startup_outcome(_store(tmp_path), reader, 1.0)
        assert captured.value is error
        assert captured.value.__notes__ == ["sidecar startup channel cleanup failed"]
    else:
        monkeypatch.setattr(
            admission_module,
            "_reader_receive",
            lambda *_args: _raise(error),
        )
        assert receive_startup_outcome(_store(tmp_path), reader, 1.0) is None


def test_tagged_derived_receive_channel_error_is_not_public_channel_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _DerivedChannelError(StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
    reader, _events, _clock = _install_unit_flow(
        monkeypatch,
        message=None,
        clock_values=[0.0, 0.1],
    )
    monkeypatch.setattr(
        admission_module,
        "_reader_receive",
        lambda *_args: (None, error),
    )
    assert receive_startup_outcome(_store(tmp_path), reader, 1.0) is None


class _RaisingDescriptor:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __get__(self, _instance: object, _owner: object) -> NoReturn:
        raise self.error


POST_RECEIVE_FAILURE_STAGES = [
    "receive",
    "failure-field",
    "failure-comparison",
    "ready-field",
    "ready-comparison",
    "clock-verify",
    "verify",
    "state-wire",
    "state-from-wire",
    "state-comparison",
    "clock-final",
]


def _install_post_receive_stage_failure(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    store: StateStore,
    error: BaseException,
) -> tuple[StartupReader, list[tuple[object, ...]]]:
    state = _state(store)
    message: object = _failure() if stage.startswith("failure-") else _ready(state)
    if stage == "clock-verify":
        clock_values: list[object] = [0.0, 0.1, error]
    elif stage == "clock-final":
        clock_values = [0.0, 0.1, 0.2, error]
    else:
        clock_values = [0.0, 0.1, 0.2, 0.3]
    reader, events, _clock = _install_unit_flow(
        monkeypatch,
        message=message,
        candidate=state,
        clock_values=clock_values,
    )
    if stage == "receive":

        def fail_receive(
            observed_reader: StartupReader,
            timeout: float,
        ) -> NoReturn:
            events.append(("receive", observed_reader, timeout))
            raise error

        monkeypatch.setattr(
            admission_module,
            "_reader_receive",
            fail_receive,
        )
    elif stage == "failure-field":
        monkeypatch.setattr(StartupFailure, "code", _RaisingDescriptor(error))
    elif stage == "failure-comparison":
        monkeypatch.setattr(
            StartupFailure,
            "__eq__",
            lambda *_args: _raise(error),
        )
    elif stage == "ready-field":
        monkeypatch.setattr(StartupReady, "startup_id", _RaisingDescriptor(error))
    elif stage == "ready-comparison":
        monkeypatch.setattr(
            StartupReady,
            "__eq__",
            lambda *_args: _raise(error),
        )
    elif stage == "verify":

        def fail_verify(
            observed_store: StateStore,
            ready: StartupReady,
            timeout: float,
        ) -> NoReturn:
            events.append(("verify", observed_store, ready, timeout))
            raise error

        monkeypatch.setattr(
            admission_module,
            "_verify_ready",
            fail_verify,
        )
    elif stage == "state-wire":
        monkeypatch.setattr(
            SidecarState,
            "to_wire",
            lambda *_args: _raise(error),
        )
    elif stage == "state-from-wire":
        monkeypatch.setattr(
            SidecarState,
            "from_wire",
            classmethod(lambda *_args: _raise(error)),
        )
    elif stage == "state-comparison":
        monkeypatch.setattr(
            SidecarState,
            "__eq__",
            lambda *_args: _raise(error),
        )
    elif stage not in {"clock-verify", "clock-final"}:
        raise AssertionError(f"unknown stage: {stage}")
    return reader, events


def _expected_post_receive_event_names(stage: str) -> list[str]:
    names = ["fileno", "clock", "clock", "receive"]
    before_verification = {
        "receive",
        "failure-field",
        "failure-comparison",
        "ready-field",
        "ready-comparison",
    }
    if stage in before_verification:
        return names
    names.append("clock")
    if stage == "clock-verify":
        return names
    names.append("verify")
    if stage in {"verify", "state-wire", "state-from-wire", "state-comparison"}:
        return names
    assert stage == "clock-final"
    names.append("clock")
    return names


@pytest.mark.parametrize("stage", POST_RECEIVE_FAILURE_STAGES)
def test_ordinary_failure_at_every_post_receive_stage_is_private_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    store = _store(tmp_path)
    error = OSError(f"{PRIVATE}:{TOKEN}:{STARTUP_ID}:{PID}:{store.database_path}")
    caplog.set_level(logging.DEBUG)
    with monkeypatch.context() as patcher:
        reader, events = _install_post_receive_stage_failure(patcher, stage, store, error)
        assert receive_startup_outcome(store, reader, 1.0) is None
    assert [event[0] for event in events] == _expected_post_receive_event_names(stage)
    assert [event[0] for event in events].count("receive") == 1
    assert [event[0] for event in events].count("verify") <= 1
    output = capsys.readouterr()
    sensitive = (PRIVATE, TOKEN, STARTUP_ID, str(PID), str(store.database_path))
    for value in sensitive:
        assert value not in output.out
        assert value not in output.err
        assert all(value not in record.getMessage() for record in caplog.records)
    _assert_module_does_not_retain_identity(error)


@pytest.mark.parametrize("stage", POST_RECEIVE_FAILURE_STAGES)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_at_every_post_receive_stage_preserves_identity_and_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    error = error_factory()
    reader, events = _install_post_receive_stage_failure(monkeypatch, stage, store, error)
    with pytest.raises(error_factory) as captured:
        receive_startup_outcome(store, reader, 1.0)
    assert captured.value is error
    assert getattr(captured.value, "__notes__", []) == []
    assert [event[0] for event in events] == _expected_post_receive_event_names(stage)
    assert [event[0] for event in events].count("receive") == 1
    assert [event[0] for event in events].count("verify") <= 1


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_reader_preflight_process_control_preserves_identity_and_real_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    reader, writer = open_startup_channel()
    error = error_factory()
    with reader, writer:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                admission_module,
                "_reader_fileno",
                lambda *_args: _raise(error),
            )
            with pytest.raises(error_factory) as captured:
                receive_startup_outcome(_store(tmp_path), reader, 1.0)
            assert captured.value is error
            assert reader.fileno() >= 3
        writer.send(_failure())
        assert reader.receive(1.0) == _failure()


@pytest.mark.parametrize(
    "stage",
    [
        "receive",
        "failure-comparison",
        "ready-comparison",
        "clock-verify",
        "verify",
        "state-comparison",
        "clock-final",
    ],
)
@pytest.mark.parametrize("derived", [False, True])
def test_channel_error_from_nonchannel_stage_is_ordinary_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    derived: bool,
) -> None:
    error_type = _DerivedChannelError if derived else StartupChannelError
    error = error_type(StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
    reader, events = _install_post_receive_stage_failure(
        monkeypatch,
        stage,
        _store(tmp_path),
        error,
    )
    assert receive_startup_outcome(_store(tmp_path), reader, 1.0) is None
    assert [event[0] for event in events] == _expected_post_receive_event_names(stage)


@pytest.mark.parametrize("result_kind", ["same", "derived", "unequal"])
def test_state_reconstruction_rejects_nonfresh_inexact_or_unequal_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_kind: str,
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    if result_kind == "same":
        rebuilt: SidecarState = state
    elif result_kind == "derived":
        rebuilt = _DerivedState(**state.to_wire())  # type: ignore[arg-type]
    else:
        rebuilt = _state(store, token=OTHER_TOKEN)
    reader, events, clock = _install_unit_flow(
        monkeypatch,
        message=_ready(state),
        candidate=state,
        clock_values=[0.0, 0.1, 0.2],
    )
    monkeypatch.setattr(
        SidecarState,
        "from_wire",
        classmethod(lambda *_args: rebuilt),
    )
    assert receive_startup_outcome(store, reader, 1.0) is None
    assert [event[0] for event in events] == [
        "fileno",
        "clock",
        "clock",
        "receive",
        "clock",
        "verify",
    ]
    assert clock.calls == 3


@pytest.mark.parametrize(
    ("clock_values", "expected_names"),
    [
        (
            [0.0, 0.1, 0.5],
            ["fileno", "clock", "clock", "receive", "clock"],
        ),
        (
            [1.0, 1.1, 1.05],
            ["fileno", "clock", "clock", "receive", "clock"],
        ),
        (
            [0.0, 0.1, _DerivedFloat(0.2)],
            ["fileno", "clock", "clock", "receive", "clock"],
        ),
        (
            [0.0, 0.1, 0.2, 0.5],
            ["fileno", "clock", "clock", "receive", "clock", "verify", "clock"],
        ),
        (
            [1.0, 1.1, 1.2, 1.15],
            ["fileno", "clock", "clock", "receive", "clock", "verify", "clock"],
        ),
        (
            [0.0, 0.1, 0.2, math.nan],
            ["fileno", "clock", "clock", "receive", "clock", "verify", "clock"],
        ),
    ],
)
def test_post_receive_expiry_rollback_or_invalid_clock_never_admits_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_values: list[object],
    expected_names: list[str],
) -> None:
    store = _store(tmp_path)
    state = _state(store)
    reader, events, _clock = _install_unit_flow(
        monkeypatch,
        message=_ready(state),
        candidate=state,
        clock_values=clock_values,
    )
    assert receive_startup_outcome(store, reader, 0.5) is None
    assert [event[0] for event in events] == expected_names
    assert [event[0] for event in events].count("receive") == 1
    assert [event[0] for event in events].count("verify") <= 1


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_existing_channel_cleanup_note_on_process_control_is_not_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    error = error_factory()
    error.add_note("sidecar startup channel cleanup failed")
    reader, _events, _clock = _install_unit_flow(
        monkeypatch,
        message=None,
        clock_values=[0.0, 0.1],
    )
    monkeypatch.setattr(
        admission_module,
        "_reader_receive",
        lambda *_args: _raise(error),
    )
    with pytest.raises(error_factory) as captured:
        receive_startup_outcome(_store(tmp_path), reader, 1.0)
    assert captured.value is error
    assert captured.value.__notes__ == ["sidecar startup channel cleanup failed"]


def _install_real_receive_then_raise_after_ownership_linearizes(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    events: list[tuple[object, ...]],
) -> None:
    canonical_receive = admission_module._reader_receive

    def consume_then_raise(reader: StartupReader, timeout: float) -> NoReturn:
        message, channel_error = canonical_receive(reader, timeout)
        events.append(("canonical-receive-returned", reader, timeout, message, channel_error))
        assert type(message) is StartupFailure
        assert channel_error is None
        raise error

    monkeypatch.setattr(admission_module, "_reader_receive", consume_then_raise)


def test_real_receive_consumes_before_later_ordinary_failure_returns_private_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    reader, writer = open_startup_channel()
    read_descriptor = reader.fileno()
    write_descriptor = writer.fileno()
    error = OSError(f"{PRIVATE}:{read_descriptor}:{store.database_path}")
    events: list[tuple[object, ...]] = []
    caplog.set_level(logging.DEBUG)
    try:
        writer.send(_failure())
        with monkeypatch.context() as patcher:
            _install_real_receive_then_raise_after_ownership_linearizes(
                patcher,
                error,
                events,
            )
            assert receive_startup_outcome(store, reader, 1.0) is None
        assert len(events) == 1
        assert events[0][0] == "canonical-receive-returned"
        assert events[0][1] is reader
        assert type(events[0][3]) is StartupFailure
        assert events[0][4] is None
        _assert_reader_closed(reader)
        _assert_raw_descriptor_closed(read_descriptor)
        _assert_raw_descriptor_closed(write_descriptor)
        output = capsys.readouterr()
        for value in (PRIVATE, str(read_descriptor), str(store.database_path)):
            assert value not in output.out
            assert value not in output.err
            assert all(value not in record.getMessage() for record in caplog.records)
        _assert_module_does_not_retain_identity(error)
    finally:
        try:
            reader.close()
        finally:
            writer.close()


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_real_receive_consumes_before_later_process_control_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    error_factory: type[BaseException],
) -> None:
    reader, writer = open_startup_channel()
    read_descriptor = reader.fileno()
    write_descriptor = writer.fileno()
    error = error_factory(PRIVATE)
    events: list[tuple[object, ...]] = []
    caplog.set_level(logging.DEBUG)
    try:
        writer.send(_failure())
        with monkeypatch.context() as patcher:
            _install_real_receive_then_raise_after_ownership_linearizes(
                patcher,
                error,
                events,
            )
            with pytest.raises(error_factory) as captured:
                receive_startup_outcome(_store(tmp_path), reader, 1.0)
        assert captured.value is error
        assert getattr(captured.value, "__notes__", []) == []
        assert len(events) == 1
        assert events[0][0] == "canonical-receive-returned"
        assert events[0][1] is reader
        assert type(events[0][3]) is StartupFailure
        assert events[0][4] is None
        _assert_reader_closed(reader)
        _assert_raw_descriptor_closed(read_descriptor)
        _assert_raw_descriptor_closed(write_descriptor)
        output = capsys.readouterr()
        assert PRIVATE not in output.out
        assert PRIVATE not in output.err
        assert all(PRIVATE not in record.getMessage() for record in caplog.records)
        _assert_module_does_not_retain_identity(error)
    finally:
        try:
            reader.close()
        finally:
            writer.close()


def test_real_unverified_ready_returns_none_after_reader_consumption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = _state(store)
    reader, writer = open_startup_channel()
    read_descriptor = reader.fileno()
    write_descriptor = writer.fileno()
    try:
        writer.send(_ready(state))
        assert receive_startup_outcome(store, reader, 1.0) is None
        _assert_reader_closed(reader)
        _assert_raw_descriptor_closed(read_descriptor)
        _assert_raw_descriptor_closed(write_descriptor)
    finally:
        try:
            reader.close()
        finally:
            writer.close()


def test_real_channel_error_preserves_fixed_error_and_consumes_reader(tmp_path: Path) -> None:
    reader, writer = open_startup_channel()
    read_descriptor = reader.fileno()
    write_descriptor = writer.fileno()
    try:
        writer.close()
        with pytest.raises(StartupChannelError) as captured:
            receive_startup_outcome(_store(tmp_path), reader, 1.0)
        assert type(captured.value) is StartupChannelError
        assert captured.value.code is StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED
        _assert_no_private_exception(captured.value, read_descriptor, write_descriptor)
        _assert_reader_closed(reader)
        _assert_raw_descriptor_closed(read_descriptor)
        _assert_raw_descriptor_closed(write_descriptor)
    finally:
        try:
            reader.close()
        finally:
            writer.close()


def test_real_failure_signal_is_returned_exactly_and_consumes_both_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify_calls: list[object] = []
    monkeypatch.setattr(admission_module, "_verify_ready", lambda *args: verify_calls.append(args))
    reader, writer = open_startup_channel()
    read_descriptor = reader.fileno()
    write_descriptor = writer.fileno()
    sent = _failure()
    try:
        writer.send(sent)
        result = receive_startup_outcome(_store(tmp_path), reader, 1.0)
        assert type(result) is StartupFailure
        assert result == sent
        assert result is not sent
        assert verify_calls == []
        _assert_reader_closed(reader)
        _assert_raw_descriptor_closed(read_descriptor)
        _assert_raw_descriptor_closed(write_descriptor)
    finally:
        try:
            reader.close()
        finally:
            writer.close()


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


def test_unpatched_real_ready_pipe_state_and_loopback_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    listener: socket.socket | None = None
    reader: StartupReader | None = None
    writer: StartupWriter | None = None
    thread: threading.Thread | None = None
    thread_started = False
    published: SidecarState | None = None
    result: SidecarState | StartupFailure | None = None
    read_descriptor = -1
    write_descriptor = -1
    before_bytes: bytes | None = None
    before_signature: tuple[int, ...] | None = None
    captured_requests: list[bytes] = []
    server_errors: list[BaseException] = []
    started = threading.Event()
    finished = threading.Event()
    caplog.set_level(logging.DEBUG)
    store = _store(tmp_path)
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LOOPBACK_HOST, 0))
        listener.listen(1)
        listener.settimeout(2.0)
        server_listener = listener
        published = create_startup_state(store, server_listener)
        store.publish(published)
        before_bytes = store.state_path.read_bytes()
        metadata = os.stat(store.state_path)
        before_signature = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        body = _health_body(published)
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
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
                try:
                    if client is not None:
                        client.close()
                except BaseException as cleanup_error:
                    server_errors.append(cleanup_error)
                finally:
                    finished.set()

        thread = threading.Thread(
            target=serve_once,
            name="startup-admission-test-server",
            daemon=True,
        )
        thread.start()
        thread_started = True
        assert started.wait(1.0)
        reader, real_writer = open_startup_channel()
        writer = real_writer
        read_descriptor = reader.fileno()
        write_descriptor = real_writer.fileno()
        real_writer.send(_ready(published))
        monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
        result = receive_startup_outcome(store, reader, 1.0)
        assert finished.wait(3.0)
        assert type(result) is SidecarState
        _assert_reader_closed(reader)
        _assert_raw_descriptor_closed(read_descriptor)
        _assert_raw_descriptor_closed(write_descriptor)
    finally:
        try:
            try:
                if reader is not None:
                    reader.close()
            finally:
                if writer is not None:
                    writer.close()
        finally:
            try:
                if listener is not None:
                    listener.close()
            finally:
                if thread is not None and thread_started:
                    thread.join(3.0)

    assert thread is not None and not thread.is_alive()
    assert server_errors == []
    assert published is not None
    assert before_bytes is not None
    assert before_signature is not None
    assert type(result) is SidecarState
    assert result == published
    assert result is not published
    assert store.state_path.read_bytes() == before_bytes
    after = os.stat(store.state_path)
    assert (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) == before_signature
    assert captured_requests == [
        (
            f"GET /internal/v1/health HTTP/1.1\r\n"
            f"Host: {published.authority}\r\n"
            f"Authorization: Bearer {published.token}\r\n"
            "Content-Length: 0\r\n\r\n"
        ).encode("ascii")
    ]
    output = capsys.readouterr()
    assert published.token not in output.out
    assert published.token not in output.err
    assert all(published.token not in record.getMessage() for record in caplog.records)


def test_production_admission_has_exact_call_sites_and_no_policy_or_cache_scope() -> None:
    source_path = Path(admission_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports: list[tuple[str, int, str | None, tuple[tuple[str, str | None], ...]]] = []
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            imports.append(
                (
                    "import",
                    0,
                    None,
                    tuple((alias.name, alias.asname) for alias in statement.names),
                )
            )
        elif isinstance(statement, ast.ImportFrom):
            imports.append(
                (
                    "from",
                    statement.level,
                    statement.module,
                    tuple((alias.name, alias.asname) for alias in statement.names),
                )
            )
    assert imports == [
        ("from", 0, "__future__", (("annotations", None),)),
        ("import", 0, None, (("math", None),)),
        ("import", 0, None, (("time", None),)),
        ("from", 0, "enum", (("StrEnum", None),)),
        (
            "from",
            0,
            "typing",
            (("Final", None), ("NoReturn", None), ("cast", None)),
        ),
        (
            "from",
            1,
            "startup_channel",
            (
                ("MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS", None),
                ("StartupChannelError", None),
                ("StartupFailure", None),
                ("StartupReader", None),
                ("StartupReady", None),
            ),
        ),
        (
            "from",
            1,
            "startup_verification",
            (("verify_ready_startup", None),),
        ),
        (
            "from",
            1,
            "state",
            (("SidecarState", None), ("StateStore", None)),
        ),
    ]

    module_assignments = [
        statement
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign))
    ]
    assert all(isinstance(statement, ast.AnnAssign) for statement in module_assignments)
    annotated_assignments = [
        statement for statement in module_assignments if isinstance(statement, ast.AnnAssign)
    ]
    assert [
        statement.target.id
        for statement in annotated_assignments
        if isinstance(statement.target, ast.Name)
    ] == [
        "_STATE_STORE_TYPE",
        "_STARTUP_READER_TYPE",
        "_STARTUP_READY_TYPE",
        "_STARTUP_FAILURE_TYPE",
        "_STARTUP_CHANNEL_ERROR_TYPE",
        "_SIDECAR_STATE_TYPE",
        "_STARTUP_READER_FILENO",
        "_STARTUP_READER_RECEIVE",
        "_VERIFY_READY_STARTUP",
    ]
    for statement in annotated_assignments:
        assert isinstance(statement.target, ast.Name)
        assert isinstance(statement.annotation, ast.Name)
        assert statement.annotation.id == "Final"
        assert isinstance(statement.value, (ast.Name, ast.Attribute))

    module_classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    assert module_classes == ["StartupAdmissionErrorCode", "StartupAdmissionError"]
    module_functions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert [node.name for node in module_functions] == [
        "_validate_timeout",
        "_reader_fileno",
        "_reader_receive",
        "_verify_ready",
        "_read_monotonic",
        "_observe_monotonic",
        "_remaining",
        "_prepare_receive_window",
        "_raise_deadline_failure",
        "_preflight_reader",
        "_rebuild_failure",
        "_rebuild_ready",
        "_rebuild_state",
        "receive_startup_outcome",
    ]
    mutable_default_nodes = (
        ast.List,
        ast.Dict,
        ast.Set,
        ast.ListComp,
        ast.DictComp,
        ast.SetComp,
        ast.GeneratorExp,
    )
    for function in module_functions:
        defaults = [*function.args.defaults, *function.args.kw_defaults]
        assert all(
            default is None or not isinstance(default, mutable_default_nodes)
            for default in defaults
        )

    forbidden_calls = {
        "__import__",
        "__setitem__",
        "acquire",
        "add",
        "append",
        "adopt_inherited",
        "bind",
        "clear",
        "close",
        "compile",
        "delattr",
        "discard",
        "ensure_private_directory",
        "eval",
        "exec",
        "extend",
        "globals",
        "insert",
        "kill",
        "listen",
        "locals",
        "load",
        "open",
        "open_startup_channel",
        "poll",
        "pop",
        "print",
        "probe_sidecar_health",
        "publish",
        "read",
        "remove",
        "remove_if_owned",
        "reverse",
        "send",
        "setattr",
        "setdefault",
        "shutdown",
        "sleep",
        "sort",
        "spawn",
        "start",
        "terminate",
        "update",
        "vars",
        "wait",
        "write",
    }
    loop_nodes = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
        ast.comprehension,
    )
    attribute_stores: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        assert not isinstance(node, loop_nodes)
        assert not isinstance(
            node,
            (
                ast.Global,
                ast.Nonlocal,
                ast.Delete,
                ast.NamedExpr,
                ast.Lambda,
            ),
        )
        if isinstance(node, ast.Subscript):
            assert not isinstance(node.ctx, ast.Store)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            assert isinstance(node.value, ast.Name)
            attribute_stores.append((node.value.id, node.attr))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls
            elif isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls
    assert attribute_stores == [("self", "code")]

    public_functions = [node.name for node in module_functions if not node.name.startswith("_")]
    assert public_functions == ["receive_startup_outcome"]
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    canonical_receive_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "_STARTUP_READER_RECEIVE"
    ]
    canonical_verify_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "_VERIFY_READY_STARTUP"
    ]
    canonical_fileno_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "_STARTUP_READER_FILENO"
    ]
    assert len(canonical_receive_calls) == 1
    assert len(canonical_verify_calls) == 1
    assert len(canonical_fileno_calls) == 1

    frozen_bindings = {
        statement.target.id: statement.value
        for statement in tree.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id
        in {"_STARTUP_READER_FILENO", "_STARTUP_READER_RECEIVE", "_VERIFY_READY_STARTUP"}
    }
    assert set(frozen_bindings) == {
        "_STARTUP_READER_FILENO",
        "_STARTUP_READER_RECEIVE",
        "_VERIFY_READY_STARTUP",
    }
    fileno_binding = frozen_bindings["_STARTUP_READER_FILENO"]
    receive_binding = frozen_bindings["_STARTUP_READER_RECEIVE"]
    verify_binding = frozen_bindings["_VERIFY_READY_STARTUP"]
    assert isinstance(fileno_binding, ast.Attribute)
    assert isinstance(fileno_binding.value, ast.Name)
    assert (fileno_binding.value.id, fileno_binding.attr) == ("StartupReader", "fileno")
    assert isinstance(receive_binding, ast.Attribute)
    assert isinstance(receive_binding.value, ast.Name)
    assert (receive_binding.value.id, receive_binding.attr) == ("StartupReader", "receive")
    assert isinstance(verify_binding, ast.Name)
    assert verify_binding.id == "verify_ready_startup"
