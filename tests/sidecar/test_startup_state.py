from __future__ import annotations

import ast
import hashlib
import inspect
import logging
import os
import socket
import sys
from pathlib import Path
from typing import NoReturn, cast

import pytest

from flowsight.sidecar import (
    LOOPBACK_HOST,
    PROTOCOL_VERSION,
    STATE_SCHEMA_VERSION,
    SidecarState,
    StartupReady,
    StartupStateError,
    StartupStateErrorCode,
    StateStore,
    bind_loopback_listener,
    create_sidecar_app,
    create_startup_state,
)
from flowsight.sidecar import startup_state as state_module

STARTUP_ID = "0123456789abcdef0123456789abcdef"
TOKEN = "A" * 43
PRIVATE = "private-token-path-pid-errno-detail"
PID = 4321
STARTED_AT_NS = 1_700_000_000_000_000_000
TOKEN_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
REAL_PLATFORM = sys.platform


class _DerivedInt(int):
    pass


class _DerivedStr(str):
    pass


class _DerivedStore(StateStore):
    pass


class _DerivedSocket(socket.socket):
    pass


class _DerivedPath(type(Path())):
    pass


class _ExplosiveValue:
    calls = 0

    def _explode(self) -> NoReturn:
        type(self).calls += 1
        raise AssertionError("unknown caller method was invoked")

    def __str__(self) -> str:
        self._explode()

    def __repr__(self) -> str:
        self._explode()

    def __iter__(self) -> NoReturn:
        self._explode()

    def __fspath__(self) -> str:
        self._explode()


def _store(tmp_path: Path, project_id: str = "project-alpha") -> StateStore:
    return StateStore(tmp_path / "runtime-does-not-exist", project_id=project_id)


def _listener() -> socket.socket:
    return bind_loopback_listener(0)


def _listener_snapshot(listener: socket.socket) -> tuple[object, ...]:
    try:
        if REAL_PLATFORM == "darwin":
            connection_info = socket.TCP_CONNECTION_INFO
            accepting: object = listener.getsockopt(socket.IPPROTO_TCP, connection_info, 1)
        else:
            accepting = listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    except OSError as error:
        accepting = ("error", error.errno)
    return (
        listener.fileno(),
        listener.family,
        listener.type,
        listener.proto,
        listener.getsockname(),
        listener.get_inheritable(),
        listener.gettimeout(),
        listener.getblocking(),
        accepting,
        listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR),
        listener.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE),
    )


def _patch_valid_generation(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int | None]]:
    calls: list[tuple[str, int | None]] = []

    def token_hex(amount: int) -> str:
        calls.append(("token_hex", amount))
        return STARTUP_ID

    def token_urlsafe(amount: int) -> str:
        calls.append(("token_urlsafe", amount))
        return TOKEN

    def getpid() -> int:
        calls.append(("getpid", None))
        return PID

    def time_ns() -> int:
        calls.append(("time_ns", None))
        return STARTED_AT_NS

    monkeypatch.setattr(state_module.secrets, "token_hex", token_hex)
    monkeypatch.setattr(state_module.secrets, "token_urlsafe", token_urlsafe)
    monkeypatch.setattr(state_module.os, "getpid", getpid)
    monkeypatch.setattr(state_module.time, "time_ns", time_ns)
    return calls


def _forbid_store_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("startup-state factory called StateStore I/O")

    for name, value in vars(StateStore).items():
        if name != "__init__" and (
            inspect.isfunction(value) or isinstance(value, (classmethod, staticmethod))
        ):
            monkeypatch.setattr(StateStore, name, fail)


def _assert_private_error(error: StartupStateError, *dynamic_forbidden: str) -> None:
    code = StartupStateErrorCode.STARTUP_STATE_GENERATION_FAILED
    assert error.code is code
    assert str(error) == f"sidecar startup state failed ({code})"
    for value in (PRIVATE, TOKEN, STARTUP_ID, str(PID), *dynamic_forbidden):
        assert value not in str(error)
        assert value not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def _assert_runtime_absent(store: StateStore) -> None:
    assert not store.runtime_root.exists()
    assert not store.runtime_dir.exists()
    assert not store.state_path.exists()
    assert not store.database_path.exists()


def _contains_nested_exact_value(value: object, expected: str, seen: set[int]) -> bool:
    if type(value) is str:
        return value == expected
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if type(value) is dict:
        items = cast(dict[object, object], value)
        return any(
            _contains_nested_exact_value(item, expected, seen)
            for pair in items.items()
            for item in pair
        )
    if type(value) in {list, tuple, set, frozenset}:
        values = cast(list[object] | tuple[object, ...] | set[object] | frozenset[object], value)
        return any(_contains_nested_exact_value(item, expected, seen) for item in values)
    return False


def _raise(error: BaseException) -> NoReturn:
    raise error


def test_public_shape_and_error_code_are_exact() -> None:
    signature = inspect.signature(create_startup_state)
    assert tuple(signature.parameters) == ("store", "listener")
    assert list(StartupStateErrorCode) == [StartupStateErrorCode.STARTUP_STATE_GENERATION_FAILED]
    with pytest.raises(TypeError, match="exact StartupStateErrorCode"):
        StartupStateError("STARTUP_STATE_GENERATION_FAILED")  # type: ignore[arg-type]


def test_real_store_listener_generation_is_exact_private_and_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    calls = _patch_valid_generation(monkeypatch)
    _forbid_store_io(monkeypatch)
    caplog.set_level(logging.DEBUG)
    try:
        state = create_startup_state(store, listener)
        assert type(state) is SidecarState
        assert state.project_id == store.project_id
        assert state.startup_id == STARTUP_ID
        assert state.pid == PID
        assert state.port == listener.getsockname()[1]
        assert state.token == TOKEN
        assert state.database_path == str(store.database_path)
        assert state.started_at_ns == STARTED_AT_NS
        assert state.host == LOOPBACK_HOST
        assert state.protocol_version == PROTOCOL_VERSION
        assert state.state_schema_version == STATE_SCHEMA_VERSION
        assert calls == [
            ("token_hex", 16),
            ("token_urlsafe", 32),
            ("getpid", None),
            ("time_ns", None),
        ]
        assert _listener_snapshot(listener) == before
        assert TOKEN not in repr(state)
        assert SidecarState.from_wire(state.to_wire()) == state
        assert StartupReady(state.startup_id, state.pid, state.port) == StartupReady(
            STARTUP_ID,
            PID,
            state.port,
        )
        assert create_sidecar_app(state) is not None
        _assert_runtime_absent(store)
        assert not _contains_nested_exact_value(vars(state_module), TOKEN, set())
        captured = capsys.readouterr()
        assert TOKEN not in captured.out
        assert TOKEN not in captured.err
        assert all(TOKEN not in record.getMessage() for record in caplog.records)

        client = socket.create_connection(
            cast(tuple[str, int], listener.getsockname()), timeout=0.5
        )
        accepted, _ = listener.accept()
        accepted.close()
        client.close()
    finally:
        listener.close()


def test_real_standard_library_generators_match_supported_grammar(tmp_path: Path) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    try:
        state = create_startup_state(store, listener)
        assert type(state.startup_id) is str
        assert len(state.startup_id) == 32
        assert set(state.startup_id) <= set("0123456789abcdef")
        assert type(state.token) is str
        assert len(state.token) == 43
        assert set(state.token) <= TOKEN_CHARACTERS
        assert state.pid == os.getpid()
        assert 0 < state.started_at_ns <= 2**63 - 1
        assert _listener_snapshot(listener) == before
        _assert_runtime_absent(store)
    finally:
        listener.close()


def test_wrong_store_type_fails_before_touching_listener_or_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def unexpected(*_args: object) -> NoReturn:
        nonlocal touched
        touched = True
        raise AssertionError("wrong store type reached implementation")

    monkeypatch.setattr(state_module, "_generate_state", unexpected)
    for invalid in (None, object(), "store", True):
        with pytest.raises(TypeError, match="exact StateStore") as captured:
            create_startup_state(invalid, object())  # type: ignore[arg-type]
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
    assert touched is False


def test_store_subclass_is_rejected_without_reading_its_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _DerivedStore(tmp_path / "derived", project_id="project")
    listener = _listener()
    monkeypatch.setattr(
        state_module,
        "_generate_state",
        lambda *_args: (_ for _ in ()).throw(AssertionError("subclass was inspected")),
    )
    try:
        with pytest.raises(TypeError, match="exact StateStore"):
            create_startup_state(store, listener)
    finally:
        listener.close()


def test_wrong_listener_type_fails_before_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    touched = False

    def unexpected(*_args: object) -> NoReturn:
        nonlocal touched
        touched = True
        raise AssertionError("wrong listener type reached implementation")

    monkeypatch.setattr(state_module, "_generate_state", unexpected)
    for invalid in (None, object(), "socket", True):
        with pytest.raises(TypeError, match="exact built-in socket.socket") as captured:
            create_startup_state(store, invalid)  # type: ignore[arg-type]
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
    assert touched is False


def test_socket_subclass_is_rejected_without_inspection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    listener = _DerivedSocket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(TypeError, match="exact built-in socket.socket"):
            create_startup_state(store, listener)
    finally:
        listener.close()


STORE_FORGERIES = [
    "missing-project",
    "non-string-key",
    "empty-project",
    "overlong-project",
    "control-project",
    "derived-project",
    "unknown-project",
    "runtime-root-type",
    "runtime-root-derived",
    "missing-runtime-root",
    "runtime-root-relative",
    "runtime-dir-type",
    "runtime-dir-derived",
    "missing-runtime-dir",
    "runtime-dir-mismatch",
    "database-type",
    "database-derived",
    "missing-database",
    "database-relative",
    "database-mismatch",
    "control-path",
    "overlong-path",
    "consistent-dotdot",
]


def _forge_store(store: StateStore, shape: str) -> None:
    fields = object.__getattribute__(store, "__dict__")
    if shape == "missing-project":
        del fields["project_id"]
    elif shape == "non-string-key":
        fields[_ExplosiveValue()] = object()
    elif shape == "empty-project":
        fields["project_id"] = ""
    elif shape == "overlong-project":
        fields["project_id"] = "p" * 257
    elif shape == "control-project":
        fields["project_id"] = "project\nsecret"
    elif shape == "derived-project":
        fields["project_id"] = _DerivedStr("project")
    elif shape == "unknown-project":
        fields["project_id"] = _ExplosiveValue()
    elif shape == "runtime-root-type":
        fields["runtime_root"] = _ExplosiveValue()
    elif shape == "runtime-root-derived":
        fields["runtime_root"] = _DerivedPath(str(fields["runtime_root"]))
    elif shape == "missing-runtime-root":
        del fields["runtime_root"]
    elif shape == "runtime-root-relative":
        fields["runtime_root"] = Path("relative")
    elif shape == "runtime-dir-type":
        fields["runtime_dir"] = _ExplosiveValue()
    elif shape == "runtime-dir-derived":
        fields["runtime_dir"] = _DerivedPath(str(fields["runtime_dir"]))
    elif shape == "missing-runtime-dir":
        del fields["runtime_dir"]
    elif shape == "runtime-dir-mismatch":
        fields["runtime_dir"] = cast(Path, fields["runtime_root"]) / "project-wrong"
    elif shape == "database-type":
        fields["database_path"] = _ExplosiveValue()
    elif shape == "database-derived":
        fields["database_path"] = _DerivedPath(str(fields["database_path"]))
    elif shape == "missing-database":
        del fields["database_path"]
    elif shape == "database-relative":
        fields["database_path"] = Path("events.sqlite3")
    elif shape == "database-mismatch":
        fields["database_path"] = cast(Path, fields["runtime_dir"]) / "wrong.sqlite3"
    elif shape in {"control-path", "overlong-path", "consistent-dotdot"}:
        project_id = cast(str, fields["project_id"])
        digest = hashlib.sha256(project_id.encode()).hexdigest()[:32]
        if shape == "control-path":
            runtime_root = Path("/tmp/runtime\nsecret")
        elif shape == "overlong-path":
            runtime_root = Path("/") / ("r" * 4100)
        else:
            runtime_root = Path("/tmp") / ".." / "forged-runtime"
        runtime_dir = runtime_root / f"project-{digest}"
        fields["runtime_root"] = runtime_root
        fields["runtime_dir"] = runtime_dir
        fields["database_path"] = runtime_dir / "events.sqlite3"
    else:
        raise AssertionError(f"unknown forgery shape: {shape}")


@pytest.mark.parametrize("shape", STORE_FORGERIES)
def test_forged_store_snapshot_fails_before_listener_or_entropy_without_unknown_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    store = _store(tmp_path)
    _forge_store(store, shape)
    listener = _listener()
    before = _listener_snapshot(listener)
    listener_reads = 0
    entropy_calls = 0
    _ExplosiveValue.calls = 0

    def listener_read(_listener: socket.socket) -> int:
        nonlocal listener_reads
        listener_reads += 1
        return _listener.fileno()

    def entropy(_amount: int) -> str:
        nonlocal entropy_calls
        entropy_calls += 1
        return STARTUP_ID

    monkeypatch.setattr(state_module, "_read_listener_fileno", listener_read)
    monkeypatch.setattr(state_module.secrets, "token_hex", entropy)
    _forbid_store_io(monkeypatch)
    try:
        with pytest.raises(StartupStateError) as captured:
            create_startup_state(store, listener)
        _assert_private_error(captured.value)
        assert listener_reads == 0
        assert entropy_calls == 0
        assert _ExplosiveValue.calls == 0
        assert _listener_snapshot(listener) == before
    finally:
        listener.close()


def test_exact_project_and_database_path_boundaries_succeed_lexically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    fields = object.__getattribute__(store, "__dict__")
    project_id = "p" * 256
    digest = hashlib.sha256(project_id.encode()).hexdigest()[:32]
    suffix = f"/project-{digest}/events.sqlite3"
    runtime_root = Path("/" + "r" * (4096 - len(suffix) - 1))
    runtime_dir = runtime_root / f"project-{digest}"
    database_path = runtime_dir / "events.sqlite3"
    assert len(str(database_path)) == 4096
    fields["project_id"] = project_id
    fields["runtime_root"] = runtime_root
    fields["runtime_dir"] = runtime_dir
    fields["database_path"] = database_path
    listener = _listener()
    before = _listener_snapshot(listener)
    _patch_valid_generation(monkeypatch)
    _forbid_store_io(monkeypatch)
    try:
        state = create_startup_state(store, listener)
        assert state.project_id == project_id
        assert state.database_path == str(database_path)
        assert _listener_snapshot(listener) == before
    finally:
        listener.close()


def _invalid_listener(shape: str) -> socket.socket:
    if shape == "unix":
        return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if shape == "udp":
        result = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        result.bind((LOOPBACK_HOST, 0))
        return result
    result = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result.set_inheritable(False)
    if shape == "closed":
        result.close()
    elif shape == "unbound":
        pass
    elif shape == "bound":
        result.bind((LOOPBACK_HOST, 0))
    elif shape == "inheritable":
        result.bind((LOOPBACK_HOST, 0))
        result.listen()
        result.set_inheritable(True)
    return result


@pytest.mark.parametrize(
    "shape",
    ["closed", "unbound", "bound", "udp", "unix", "inheritable"],
)
def test_structurally_invalid_listener_is_rejected_without_close_or_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    store = _store(tmp_path)
    listener = _invalid_listener(shape)
    descriptor = listener.fileno()
    before = None if descriptor < 0 else _listener_snapshot(listener)
    generation_calls = 0

    def entropy(_amount: int) -> str:
        nonlocal generation_calls
        generation_calls += 1
        return STARTUP_ID

    monkeypatch.setattr(state_module.secrets, "token_hex", entropy)
    try:
        with pytest.raises(StartupStateError) as captured:
            create_startup_state(store, listener)
        _assert_private_error(captured.value)
        assert generation_calls == 0
        if descriptor < 0:
            assert listener.fileno() == -1
        else:
            assert listener.fileno() == descriptor
            assert _listener_snapshot(listener) == before
    finally:
        listener.close()


LISTENER_VALUE_FAULTS: list[tuple[str, object]] = [
    ("_read_listener_fileno", True),
    ("_read_listener_fileno", -1),
    ("_read_listener_family", int(socket.AF_INET)),
    ("_read_listener_family", socket.AF_INET6),
    ("_read_listener_type", int(socket.SOCK_STREAM)),
    ("_read_listener_type", socket.SOCK_DGRAM),
    ("_read_listener_protocol", True),
    ("_read_listener_protocol", -1),
    ("_read_listener_address", [LOOPBACK_HOST, 4040]),
    ("_read_listener_address", (LOOPBACK_HOST,)),
    ("_read_listener_address", (_DerivedStr(LOOPBACK_HOST), 4040)),
    ("_read_listener_address", ("0.0.0.0", 4040)),
    ("_read_listener_address", (LOOPBACK_HOST, True)),
    ("_read_listener_address", (LOOPBACK_HOST, 0)),
    ("_read_listener_inheritable", 0),
    ("_read_listener_inheritable", True),
]


@pytest.mark.parametrize(("helper", "invalid"), LISTENER_VALUE_FAULTS)
def test_malformed_listener_inspection_value_is_fixed_private_and_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    entropy_calls = 0

    def entropy(_amount: int) -> str:
        nonlocal entropy_calls
        entropy_calls += 1
        return STARTUP_ID

    monkeypatch.setattr(state_module, helper, lambda *_args: invalid)
    monkeypatch.setattr(state_module.secrets, "token_hex", entropy)
    try:
        with pytest.raises(StartupStateError) as captured:
            create_startup_state(store, listener)
        _assert_private_error(captured.value)
        assert entropy_calls == 0
        assert _listener_snapshot(listener) == before
    finally:
        listener.close()


@pytest.mark.parametrize(
    ("platform", "accepting"),
    [
        ("darwin", b""),
        ("darwin", b"\x00"),
        ("darwin", 1),
        ("linux", 0),
        ("linux", True),
        ("linux", b"\x01"),
        ("other", 1),
    ],
)
def test_platform_listening_proof_rejects_malformed_or_non_listen_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    accepting: object,
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    monkeypatch.setattr(state_module.sys, "platform", platform)
    monkeypatch.setattr(state_module, "_read_listener_accepting", lambda _listener: accepting)
    try:
        with pytest.raises(StartupStateError) as captured:
            create_startup_state(store, listener)
        _assert_private_error(captured.value)
        assert _listener_snapshot(listener) == before
    finally:
        listener.close()


@pytest.mark.parametrize(("platform", "accepting"), [("darwin", b"\x01"), ("linux", 1)])
def test_supported_platform_listen_state_shapes_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    accepting: object,
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    _patch_valid_generation(monkeypatch)
    monkeypatch.setattr(state_module.sys, "platform", platform)
    monkeypatch.setattr(state_module, "_read_listener_accepting", lambda _listener: accepting)
    try:
        assert create_startup_state(store, listener).port == listener.getsockname()[1]
        assert _listener_snapshot(listener) == before
    finally:
        listener.close()


class _GetSockOptProbe:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def getsockopt(self, *args: object) -> object:
        self.calls.append(args)
        return self.result


def test_platform_helpers_use_only_the_exact_read_only_kernel_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    darwin = _GetSockOptProbe(b"\x01")
    monkeypatch.setattr(state_module.sys, "platform", "darwin")
    monkeypatch.setattr(state_module, "_TCP_CONNECTION_INFO", 262)
    assert state_module._read_listener_accepting(cast(socket.socket, darwin)) == b"\x01"
    assert darwin.calls == [(socket.IPPROTO_TCP, 262, 1)]

    linux = _GetSockOptProbe(1)
    monkeypatch.setattr(state_module.sys, "platform", "linux")
    assert state_module._read_listener_accepting(cast(socket.socket, linux)) == 1
    assert linux.calls == [(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)]


INVALID_GENERATED_VALUES: list[tuple[str, object]] = [
    ("token_hex", True),
    ("token_hex", _DerivedStr(STARTUP_ID)),
    ("token_hex", "a" * 31),
    ("token_hex", "A" * 32),
    ("token_hex", "g" * 32),
    ("token_urlsafe", True),
    ("token_urlsafe", _DerivedStr(TOKEN)),
    ("token_urlsafe", "A" * 42),
    ("token_urlsafe", "A" * 44),
    ("token_urlsafe", "A" * 42 + "+"),
    ("getpid", True),
    ("getpid", _DerivedInt(PID)),
    ("getpid", 0),
    ("getpid", 2**31),
    ("time_ns", True),
    ("time_ns", _DerivedInt(STARTED_AT_NS)),
    ("time_ns", 0),
    ("time_ns", 2**63),
]

GENERATION_CALLS = [
    ("token_hex", 16),
    ("token_urlsafe", 32),
    ("getpid", None),
    ("time_ns", None),
]


def _expected_generation_prefix(stage: str) -> list[tuple[str, int | None]]:
    call_name = {
        "token-hex": "token_hex",
        "token-urlsafe": "token_urlsafe",
        "pid": "getpid",
        "clock": "time_ns",
    }.get(stage, stage)
    names = [name for name, _argument in GENERATION_CALLS]
    if call_name in names:
        return GENERATION_CALLS[: names.index(call_name) + 1]
    if stage == "revalidate":
        return GENERATION_CALLS
    return []


@pytest.mark.parametrize(("stage", "invalid"), INVALID_GENERATED_VALUES)
def test_invalid_generated_value_fails_fixed_private_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    calls = _patch_valid_generation(monkeypatch)
    if stage in {"token_hex", "token_urlsafe"}:

        def invalid_token(amount: int) -> object:
            calls.append((stage, amount))
            return invalid

        monkeypatch.setattr(state_module.secrets, stage, invalid_token)
    elif stage == "getpid":
        monkeypatch.setattr(
            state_module.os,
            stage,
            lambda: (calls.append((stage, None)), invalid)[1],
        )
    else:
        monkeypatch.setattr(
            state_module.time,
            stage,
            lambda: (calls.append((stage, None)), invalid)[1],
        )
    try:
        with pytest.raises(StartupStateError) as captured:
            create_startup_state(store, listener)
        _assert_private_error(captured.value)
        assert calls == _expected_generation_prefix(stage)
        assert _listener_snapshot(listener) == before
        _assert_runtime_absent(store)
    finally:
        listener.close()


FAILURE_STAGES = [
    "store-fields",
    "hash",
    "fileno",
    "family",
    "type",
    "protocol",
    "address",
    "accepting",
    "inheritable",
    "token-hex",
    "token-urlsafe",
    "pid",
    "clock",
    "revalidate",
    "generate",
]


def _patch_failure_stage(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error: BaseException,
    calls: list[tuple[str, int | None]],
) -> None:
    if stage in {"token-hex", "token-urlsafe"}:
        call_name = "token_hex" if stage == "token-hex" else "token_urlsafe"

        def fail_token(amount: int) -> NoReturn:
            calls.append((call_name, amount))
            raise error

        owner: object = state_module.secrets
        name = call_name
        monkeypatch.setattr(owner, name, fail_token)
        return
    if stage in {"pid", "clock"}:
        call_name = "getpid" if stage == "pid" else "time_ns"

        def fail_scalar() -> NoReturn:
            calls.append((call_name, None))
            raise error

        owner = state_module.os if stage == "pid" else state_module.time
        monkeypatch.setattr(owner, call_name, fail_scalar)
        return

    fail = lambda *_args, **_kwargs: _raise(error)  # noqa: E731
    targets: dict[str, tuple[object, str]] = {
        "store-fields": (state_module, "_read_store_fields"),
        "hash": (state_module.hashlib, "sha256"),
        "fileno": (state_module, "_read_listener_fileno"),
        "family": (state_module, "_read_listener_family"),
        "type": (state_module, "_read_listener_type"),
        "protocol": (state_module, "_read_listener_protocol"),
        "address": (state_module, "_read_listener_address"),
        "accepting": (state_module, "_read_listener_accepting"),
        "inheritable": (state_module, "_read_listener_inheritable"),
        "token-hex": (state_module.secrets, "token_hex"),
        "token-urlsafe": (state_module.secrets, "token_urlsafe"),
        "pid": (state_module.os, "getpid"),
        "clock": (state_module.time, "time_ns"),
        "revalidate": (state_module, "_revalidate_state"),
        "generate": (state_module, "_generate_state"),
    }
    owner, name = targets[stage]
    monkeypatch.setattr(owner, name, fail)


@pytest.mark.parametrize("stage", FAILURE_STAGES)
def test_ordinary_stage_failure_is_fixed_private_context_free_and_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    caplog.set_level(logging.DEBUG)
    calls = _patch_valid_generation(monkeypatch)
    _patch_failure_stage(
        monkeypatch,
        stage,
        OSError(f"{PRIVATE}:{TOKEN}:{STARTUP_ID}:{PID}:{store.database_path}"),
        calls,
    )
    try:
        with pytest.raises(StartupStateError) as captured:
            create_startup_state(store, listener)
        _assert_private_error(captured.value, str(store.database_path))
        assert calls == _expected_generation_prefix(stage)
        output = capsys.readouterr()
        sensitive = (PRIVATE, TOKEN, STARTUP_ID, str(PID), str(store.database_path))
        for value in sensitive:
            assert value not in output.out
            assert value not in output.err
            assert all(value not in record.getMessage() for record in caplog.records)
        assert _listener_snapshot(listener) == before
        _assert_runtime_absent(store)
    finally:
        listener.close()


@pytest.mark.parametrize("stage", FAILURE_STAGES)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_stage_failure_preserves_identity_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    before = _listener_snapshot(listener)
    calls = _patch_valid_generation(monkeypatch)
    error = error_factory()
    _patch_failure_stage(monkeypatch, stage, error, calls)
    try:
        with pytest.raises(error_factory) as captured:
            create_startup_state(store, listener)
        assert captured.value is error
        assert calls == _expected_generation_prefix(stage)
        assert _listener_snapshot(listener) == before
        _assert_runtime_absent(store)
    finally:
        listener.close()


def test_inexact_internal_final_result_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    listener = _listener()
    monkeypatch.setattr(state_module, "_generate_state", lambda *_args: object())
    try:
        with pytest.raises(StartupStateError) as captured:
            create_startup_state(store, listener)
        _assert_private_error(captured.value)
    finally:
        listener.close()


def test_revalidation_returns_a_distinct_exact_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = SidecarState(
        project_id=store.project_id,
        startup_id=STARTUP_ID,
        pid=PID,
        port=4040,
        token=TOKEN,
        database_path=str(store.database_path),
        started_at_ns=STARTED_AT_NS,
    )
    revalidated = state_module._revalidate_state(candidate)
    assert type(revalidated) is SidecarState
    assert revalidated == candidate
    assert revalidated is not candidate


def test_production_factory_has_no_forbidden_or_mutating_behavior() -> None:
    source_path = Path(state_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for statement in tree.body:
        assigned: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            assigned = statement.value
        elif isinstance(statement, ast.AnnAssign):
            assigned = statement.value
        if assigned is not None:
            assert not isinstance(
                assigned,
                (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp),
            )
            if isinstance(assigned, ast.Call):
                if isinstance(assigned.func, ast.Name):
                    assert assigned.func.id not in {"list", "dict", "set", "deque", "defaultdict"}
                elif isinstance(assigned.func, ast.Attribute):
                    assert assigned.func.attr not in {
                        "token_hex",
                        "token_urlsafe",
                        "token_bytes",
                    }
    forbidden_import_roots = {
        "asyncio",
        "http",
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
        "accept",
        "access",
        "acquire",
        "bind",
        "close",
        "connect",
        "critical",
        "chmod",
        "chown",
        "detach",
        "debug",
        "dup",
        "error",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "exists",
        "exception",
        "ensure_private_directory",
        "is_dir",
        "is_file",
        "info",
        "kill",
        "killpg",
        "lchmod",
        "lchown",
        "link",
        "fork",
        "forkpty",
        "fchmod",
        "fchown",
        "lstat",
        "listen",
        "load",
        "log",
        "listdir",
        "mkdir",
        "makedirs",
        "open",
        "poll",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "print",
        "publish",
        "read_text",
        "readlink",
        "rename",
        "remove",
        "removedirs",
        "replace",
        "remove_if_owned",
        "resolve",
        "rmdir",
        "scandir",
        "send",
        "setblocking",
        "set_blocking",
        "set_inheritable",
        "setsockopt",
        "settimeout",
        "shutdown",
        "sleep",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "start",
        "stat",
        "symlink",
        "system",
        "touch",
        "truncate",
        "unlink",
        "utime",
        "wait",
        "wait3",
        "wait4",
        "waitpid",
        "warning",
        "walk",
        "write",
        "write_text",
        "_STATE_STORE_TYPE",
        "StateStore",
    }
    forbidden_calls.update(
        name
        for name, value in vars(StateStore).items()
        if not name.startswith("__")
        and (inspect.isfunction(value) or isinstance(value, (classmethod, staticmethod)))
    )
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
            elif isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls
