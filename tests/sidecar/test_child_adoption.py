from __future__ import annotations

import ast
import errno
import gc
import inspect
import os
import socket
import traceback
import weakref
from collections import Counter
from pathlib import Path
from typing import NoReturn

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    OwnerLock,
    OwnerLockError,
    OwnerLockErrorCode,
    SidecarChildBootstrap,
    SidecarRuntimeConfig,
    StartupReader,
    StartupWriter,
    StateStore,
    adopt_sidecar_child_descriptors,
    decode_sidecar_child_bootstrap,
    encode_sidecar_child_bootstrap,
    open_startup_channel,
    prepare_sidecar_runtime_config,
)
from flowsight.sidecar import child_adoption as adoption_module
from flowsight.sidecar import child_bootstrap as bootstrap_module
from flowsight.sidecar import runtime_config as config_module
from flowsight.sidecar import state as state_module

ADOPTION_ERROR = "sidecar child descriptor adoption failed"
CLEANUP_NOTE = "sidecar child descriptor adoption cleanup failed"

type _RealPair = tuple[
    SidecarRuntimeConfig,
    StateStore,
    SidecarChildBootstrap,
    StartupReader,
    int,
    int,
]


class _Control(BaseException):
    pass


class _HostileControl(BaseException):
    override_calls = 0

    def add_note(self, note: str) -> None:
        del note
        type(self).override_calls += 1
        raise RuntimeError("hostile add_note override")


class _TrackedFailure(Exception):
    references: list[weakref.ReferenceType[_TrackedFailure]] = []

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.references.append(weakref.ref(self))


class _DuckBootstrap:
    def __getattribute__(self, name: str) -> NoReturn:
        raise AssertionError(f"duck bootstrap dispatch ran: {name}")


class _DerivedBootstrap(SidecarChildBootstrap):
    pass


class _DerivedRuntimeConfig(SidecarRuntimeConfig):
    pass


class _DerivedStore(StateStore):
    pass


class _DerivedText(str):
    pass


class _DerivedTuple(tuple[object, ...]):
    pass


def _project(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    project.mkdir()
    return project


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    project_name: str = "project",
    runtime_name: str = "runtime-root",
) -> tuple[SidecarRuntimeConfig, StateStore]:
    runtime_root = tmp_path / runtime_name
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )
    config = prepare_sidecar_runtime_config(_project(tmp_path, project_name))
    store = StateStore(config.runtime_root, project_id=config.project_id)
    store.ensure_private_directory()
    return config, store


def _real_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _RealPair:
    config, store = _prepare(monkeypatch, tmp_path)
    parent_owner = OwnerLock.acquire(store)
    reader, parent_writer = open_startup_channel()
    owner_fd = -1
    writer_fd = -1
    try:
        owner_fd = os.dup(parent_owner.fileno())
        writer_fd = os.dup(parent_writer.fileno())
        parent_owner.close()
        parent_writer.close()
        bootstrap = decode_sidecar_child_bootstrap(
            encode_sidecar_child_bootstrap(
                config,
                owner_lock_fd=owner_fd,
                startup_writer_fd=writer_fd,
            )
        )
        return config, store, bootstrap, reader, owner_fd, writer_fd
    except BaseException:
        parent_owner.close()
        parent_writer.close()
        reader.close()
        for descriptor in (writer_fd, owner_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _forge_bootstrap(
    config: object,
    owner_lock_fd: object,
    startup_writer_fd: object,
    *,
    omit: str | None = None,
) -> SidecarChildBootstrap:
    result: SidecarChildBootstrap = object.__new__(SidecarChildBootstrap)
    if omit != "config":
        object.__setattr__(result, "config", config)
    if omit != "owner_lock_fd":
        object.__setattr__(result, "owner_lock_fd", owner_lock_fd)
    if omit != "startup_writer_fd":
        object.__setattr__(result, "startup_writer_fd", startup_writer_fd)
    return result


def _is_open(file_descriptor: int) -> bool:
    try:
        os.fstat(file_descriptor)
    except OSError:
        return False
    return True


def _assert_fixed_error(
    error: BaseException,
    *,
    context: BaseException | None = None,
) -> None:
    assert type(error) is RuntimeError
    assert str(error) == ADOPTION_ERROR
    assert error.args == (ADOPTION_ERROR,)
    assert error.__cause__ is None
    assert error.__context__ is context
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", []) == []


def _production_frames(error: BaseException) -> list[tuple[str, dict[str, object]]]:
    production_path = Path(adoption_module.__file__).resolve()
    frames: list[tuple[str, dict[str, object]]] = []
    current = error.__traceback__
    while current is not None:
        if Path(current.tb_frame.f_code.co_filename).resolve() == production_path:
            frames.append((current.tb_frame.f_code.co_name, dict(current.tb_frame.f_locals)))
        current = current.tb_next
    return frames


def _assert_fixed_frames_are_empty(error: BaseException) -> None:
    frames = _production_frames(error)
    assert tuple(name for name, _locals in frames) == (
        "adopt_sidecar_child_descriptors",
        "_raise_adoption_failure",
    )
    assert all(local_values == {} for _name, local_values in frames)


def _assert_successor_can_acquire(store: StateStore) -> None:
    successor = OwnerLock.acquire(store)
    successor.close()


def test_public_surface_is_exact() -> None:
    assert sidecar_package.adopt_sidecar_child_descriptors is adopt_sidecar_child_descriptors
    assert sidecar_package.__all__.count("adopt_sidecar_child_descriptors") == 1
    signature = inspect.signature(adopt_sidecar_child_descriptors)
    assert tuple(signature.parameters) == ("bootstrap",)
    assert signature.parameters["bootstrap"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["bootstrap"].annotation == "SidecarChildBootstrap"
    assert signature.return_annotation == "tuple[OwnerLock, StartupWriter]"


@pytest.mark.parametrize(
    "case",
    [
        "wrong-type",
        "bootstrap-subclass",
        "missing-config",
        "missing-owner",
        "missing-writer",
        "wrong-config",
        "config-subclass",
        "bool-owner",
        "bool-writer",
        "owner-two",
        "writer-two",
        "negative-owner",
        "negative-writer",
        "owner-too-large",
        "writer-too-large",
        "equal",
    ],
)
def test_shallow_admission_is_the_only_preconsume_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    descriptor_two_identity = None
    if case in {"owner-two", "writer-two"}:
        descriptor_two = os.fstat(2)
        descriptor_two_identity = (
            descriptor_two.st_dev,
            descriptor_two.st_ino,
            descriptor_two.st_mode,
            descriptor_two.st_rdev,
        )
    if case == "wrong-type":
        candidate: object = _DuckBootstrap()
    elif case == "bootstrap-subclass":
        derived = object.__new__(_DerivedBootstrap)
        object.__setattr__(derived, "config", config)
        object.__setattr__(derived, "owner_lock_fd", owner_fd)
        object.__setattr__(derived, "startup_writer_fd", writer_fd)
        candidate = derived
    elif case == "missing-config":
        candidate = _forge_bootstrap(config, owner_fd, writer_fd, omit="config")
    elif case == "missing-owner":
        candidate = _forge_bootstrap(config, owner_fd, writer_fd, omit="owner_lock_fd")
    elif case == "missing-writer":
        candidate = _forge_bootstrap(
            config,
            owner_fd,
            writer_fd,
            omit="startup_writer_fd",
        )
    elif case == "wrong-config":
        candidate = _forge_bootstrap(object(), owner_fd, writer_fd)
    elif case == "config-subclass":
        derived_config = object.__new__(_DerivedRuntimeConfig)
        for slot in (
            "project_root",
            "runtime_root",
            "project_id",
            "requested_port",
            "startup_timeout",
        ):
            object.__setattr__(
                derived_config,
                slot,
                object.__getattribute__(config, slot),
            )
        candidate = _forge_bootstrap(derived_config, owner_fd, writer_fd)
    elif case == "bool-owner":
        candidate = _forge_bootstrap(config, True, writer_fd)
    elif case == "bool-writer":
        candidate = _forge_bootstrap(config, owner_fd, False)
    elif case == "owner-two":
        candidate = _forge_bootstrap(config, 2, writer_fd)
    elif case == "writer-two":
        candidate = _forge_bootstrap(config, owner_fd, 2)
    elif case == "negative-owner":
        candidate = _forge_bootstrap(config, -1, writer_fd)
    elif case == "negative-writer":
        candidate = _forge_bootstrap(config, owner_fd, -1)
    elif case == "owner-too-large":
        candidate = _forge_bootstrap(config, 2_147_483_648, writer_fd)
    elif case == "writer-too-large":
        candidate = _forge_bootstrap(config, owner_fd, 2_147_483_648)
    else:
        candidate = _forge_bootstrap(config, owner_fd, owner_fd)

    events: list[str] = []

    def forbidden(name: str):
        def operation(*_args: object, **_kwargs: object) -> NoReturn:
            events.append(name)
            raise AssertionError(f"post-admission operation ran: {name}")

        return operation

    for name in (
        "_encode_bootstrap",
        "_construct_state_store",
        "_adopt_owner",
        "_adopt_writer",
        "_close_raw",
        "_close_owner",
        "_close_writer",
    ):
        monkeypatch.setattr(adoption_module, name, forbidden(name))

    with pytest.raises(RuntimeError) as captured:
        adopt_sidecar_child_descriptors(candidate)  # type: ignore[arg-type]

    _assert_fixed_error(captured.value)
    _assert_fixed_frames_are_empty(captured.value)
    assert events == []
    assert _is_open(owner_fd)
    assert _is_open(writer_fd)
    if descriptor_two_identity is not None:
        descriptor_two = os.fstat(2)
        assert (
            descriptor_two.st_dev,
            descriptor_two.st_ino,
            descriptor_two.st_mode,
            descriptor_two.st_rdev,
        ) == descriptor_two_identity
    os.close(writer_fd)
    os.close(owner_fd)
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize(
    "failure_point",
    [
        "encoder-error",
        "encoder-shape",
        "encoder-list",
        "encoder-derived-tuple",
        "encoder-derived-text",
        "config-shape",
        "config-runtime",
        "store-error",
        "store-project",
        "store-runtime",
        "store-runtime-type",
        "store-derived",
    ],
)
def test_owned_preflight_failure_retires_raw_pair_writer_then_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    real_encode = adoption_module._encode_bootstrap
    real_read_identity = adoption_module._read_config_identity
    real_construct = adoption_module._construct_state_store
    real_close_raw = adoption_module._close_raw
    events: list[tuple[str, int | None]] = []

    def fail_encoder(*_args: object, **_kwargs: object) -> NoReturn:
        events.append(("encoder-error", None))
        raise OSError("private encoder failure")

    def malformed_encoder(*_args: object, **_kwargs: object) -> object:
        events.append((failure_point, None))
        if failure_point == "encoder-list":
            return ["invalid"] * 8
        if failure_point == "encoder-derived-tuple":
            return _DerivedTuple(("invalid",) * 8)
        if failure_point == "encoder-derived-text":
            return (*(["valid"] * 7), _DerivedText("derived"))
        return ("invalid",) * 7

    def read_identity(candidate_config: SidecarRuntimeConfig) -> object:
        if failure_point == "config-shape":
            events.append(("config-shape", None))
            return (candidate_config.runtime_root,)
        if failure_point == "config-runtime":
            events.append(("config-runtime", None))
            return (object(), candidate_config.project_id)
        return real_read_identity(candidate_config)

    def construct(runtime_root: str, project_id: str) -> object:
        if failure_point == "store-error":
            events.append(("store-error", None))
            raise OSError("private store failure")
        if failure_point == "store-project":
            events.append(("store-project", None))
            return StateStore(runtime_root, project_id=f"{project_id}-other")
        if failure_point == "store-runtime":
            events.append(("store-runtime", None))
            return StateStore(str(tmp_path / "other-runtime"), project_id=project_id)
        if failure_point == "store-runtime-type":
            events.append(("store-runtime-type", None))
            candidate_store = StateStore(runtime_root, project_id=project_id)
            candidate_store.runtime_root = runtime_root  # type: ignore[assignment]
            return candidate_store
        if failure_point == "store-derived":
            events.append(("store-derived", None))
            return _DerivedStore(runtime_root, project_id=project_id)
        return real_construct(runtime_root, project_id)

    def close_raw(file_descriptor: int) -> None:
        events.append(("close", file_descriptor))
        real_close_raw(file_descriptor)

    if failure_point == "encoder-error":
        monkeypatch.setattr(adoption_module, "_encode_bootstrap", fail_encoder)
    elif failure_point.startswith("encoder-"):
        monkeypatch.setattr(adoption_module, "_encode_bootstrap", malformed_encoder)
    elif failure_point.startswith("config-"):
        monkeypatch.setattr(adoption_module, "_encode_bootstrap", real_encode)
        monkeypatch.setattr(adoption_module, "_read_config_identity", read_identity)
    else:
        monkeypatch.setattr(adoption_module, "_encode_bootstrap", real_encode)
        monkeypatch.setattr(adoption_module, "_construct_state_store", construct)
    monkeypatch.setattr(adoption_module, "_close_raw", close_raw)
    monkeypatch.setattr(
        adoption_module,
        "_adopt_owner",
        lambda *_args: pytest.fail("owner adoption ran after preflight failure"),
    )
    monkeypatch.setattr(
        adoption_module,
        "_adopt_writer",
        lambda *_args: pytest.fail("writer adoption ran after preflight failure"),
    )

    with pytest.raises(RuntimeError) as captured:
        adopt_sidecar_child_descriptors(bootstrap)

    _assert_fixed_error(captured.value)
    _assert_fixed_frames_are_empty(captured.value)
    assert events[-2:] == [("close", writer_fd), ("close", owner_fd)]
    assert not _is_open(writer_fd)
    assert not _is_open(owner_fd)
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize("stage", ["encoder", "store"])
@pytest.mark.parametrize("control_kind", ["keyboard", "system-exit"])
def test_preflight_process_control_preserves_identity_after_raw_pair_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    control_kind: str,
) -> None:
    _config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    control: BaseException = (
        KeyboardInterrupt("preflight control")
        if control_kind == "keyboard"
        else SystemExit("preflight control")
    )
    real_close_raw = adoption_module._close_raw
    events: list[int] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise control

    def close_raw(file_descriptor: int) -> None:
        events.append(file_descriptor)
        real_close_raw(file_descriptor)

    if stage == "encoder":
        monkeypatch.setattr(adoption_module, "_encode_bootstrap", fail)
    else:
        monkeypatch.setattr(adoption_module, "_construct_state_store", fail)
    monkeypatch.setattr(adoption_module, "_close_raw", close_raw)

    if isinstance(control, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt) as captured:
            adopt_sidecar_child_descriptors(bootstrap)
    else:
        with pytest.raises(SystemExit) as captured:
            adopt_sidecar_child_descriptors(bootstrap)

    assert captured.value is control
    assert getattr(captured.value, "__notes__", []) == []
    assert events == [writer_fd, owner_fd]
    assert not _is_open(writer_fd)
    assert not _is_open(owner_fd)
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize("active_kind", ["ordinary", "control"])
def test_raw_cleanup_control_continues_to_owner_and_obeys_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_kind: str,
) -> None:
    _config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    active_error: BaseException = (
        OSError("ordinary preflight failure")
        if active_kind == "ordinary"
        else KeyboardInterrupt("preflight control")
    )
    cleanup_control = SystemExit("raw writer cleanup control")
    real_close_raw = adoption_module._close_raw
    events: list[int] = []

    def fail_preflight(*_args: object, **_kwargs: object) -> NoReturn:
        raise active_error

    def close_raw(file_descriptor: int) -> None:
        events.append(file_descriptor)
        real_close_raw(file_descriptor)
        if file_descriptor == writer_fd:
            raise cleanup_control

    monkeypatch.setattr(adoption_module, "_encode_bootstrap", fail_preflight)
    monkeypatch.setattr(adoption_module, "_close_raw", close_raw)

    if active_kind == "ordinary":
        with pytest.raises(SystemExit) as captured:
            adopt_sidecar_child_descriptors(bootstrap)
        assert captured.value is cleanup_control
        assert getattr(captured.value, "__notes__", []) == []
    else:
        with pytest.raises(KeyboardInterrupt) as captured:
            adopt_sidecar_child_descriptors(bootstrap)
        assert captured.value is active_error
        assert captured.value.__notes__ == [CLEANUP_NOTE]

    assert events == [writer_fd, owner_fd]
    assert not _is_open(writer_fd)
    assert not _is_open(owner_fd)
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize("stage", ["owner", "writer", "result"])
@pytest.mark.parametrize("failure_kind", ["ordinary", "control"])
def test_partial_adoption_state_machine_is_ordered_and_cleans_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    failure_kind: str,
) -> None:
    config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    real_adopt_owner = adoption_module._adopt_owner
    real_adopt_writer = adoption_module._adopt_writer
    real_close_raw = adoption_module._close_raw
    real_close_owner = adoption_module._close_owner
    real_close_writer = adoption_module._close_writer
    events: list[str] = []
    failure: BaseException = (
        OSError("private adoption failure")
        if failure_kind == "ordinary"
        else _Control("private adoption control")
    )

    def raise_failure() -> NoReturn:
        raise failure

    def adopt_owner(candidate_store: StateStore, file_descriptor: int) -> OwnerLock:
        events.append("adopt-owner")
        if stage == "owner":
            os.close(file_descriptor)
            raise_failure()
        return real_adopt_owner(candidate_store, file_descriptor)

    def adopt_writer(file_descriptor: int) -> StartupWriter:
        events.append("adopt-writer")
        if stage == "writer":
            os.close(file_descriptor)
            raise_failure()
        return real_adopt_writer(file_descriptor)

    def make_result(owner: OwnerLock, writer: StartupWriter) -> object:
        del owner, writer
        events.append("make-result")
        if stage == "result":
            raise_failure()
        pytest.fail("unexpected result seam")

    def close_raw(file_descriptor: int) -> None:
        events.append("close-raw")
        real_close_raw(file_descriptor)

    def close_owner(owner: OwnerLock) -> None:
        events.append("close-owner")
        real_close_owner(owner)

    def close_writer(writer: StartupWriter) -> None:
        events.append("close-writer")
        real_close_writer(writer)

    monkeypatch.setattr(adoption_module, "_adopt_owner", adopt_owner)
    monkeypatch.setattr(adoption_module, "_adopt_writer", adopt_writer)
    monkeypatch.setattr(adoption_module, "_make_result", make_result)
    monkeypatch.setattr(adoption_module, "_close_raw", close_raw)
    monkeypatch.setattr(adoption_module, "_close_owner", close_owner)
    monkeypatch.setattr(adoption_module, "_close_writer", close_writer)

    if failure_kind == "ordinary":
        with pytest.raises(RuntimeError) as captured:
            adopt_sidecar_child_descriptors(bootstrap)
        _assert_fixed_error(captured.value)
        _assert_fixed_frames_are_empty(captured.value)
    else:
        with pytest.raises(_Control) as captured:
            adopt_sidecar_child_descriptors(bootstrap)
        assert captured.value is failure
        assert getattr(captured.value, "__notes__", []) == []

    expected = {
        "owner": ["adopt-owner", "close-raw"],
        "writer": ["adopt-owner", "adopt-writer", "close-owner"],
        "result": [
            "adopt-owner",
            "adopt-writer",
            "make-result",
            "close-writer",
            "close-owner",
        ],
    }
    assert events == expected[stage]
    reader.close()
    _assert_successor_can_acquire(store)


def test_real_pair_success_is_functional_and_retains_exact_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    result = adopt_sidecar_child_descriptors(bootstrap)
    assert type(result) is tuple
    assert len(result) == 2
    owner, writer = result
    assert type(owner) is OwnerLock
    assert type(writer) is StartupWriter
    assert owner.fileno() == owner_fd
    assert writer.fileno() == writer_fd
    assert os.get_inheritable(owner_fd) is False
    assert os.get_inheritable(writer_fd) is False

    with pytest.raises(OwnerLockError) as held:
        OwnerLock.acquire(store)
    assert held.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD
    writer.close()
    owner.close()
    reader.close()
    _assert_successor_can_acquire(store)


def test_canonical_preflight_runs_once_with_only_snapshotted_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    real_encode = adoption_module._encode_bootstrap
    real_construct = adoption_module._construct_state_store
    calls: list[tuple[object, ...]] = []

    def encode(
        candidate_config: SidecarRuntimeConfig,
        candidate_owner_fd: int,
        candidate_writer_fd: int,
    ) -> object:
        calls.append(
            (
                "encode",
                candidate_config,
                candidate_owner_fd,
                candidate_writer_fd,
            )
        )
        return real_encode(
            candidate_config,
            candidate_owner_fd,
            candidate_writer_fd,
        )

    def construct(runtime_root: str, project_id: str) -> object:
        calls.append(("store", runtime_root, project_id))
        return real_construct(runtime_root, project_id)

    monkeypatch.setattr(adoption_module, "_encode_bootstrap", encode)
    monkeypatch.setattr(adoption_module, "_construct_state_store", construct)

    owner, writer = adopt_sidecar_child_descriptors(bootstrap)

    assert calls == [
        ("encode", config, owner_fd, writer_fd),
        ("store", config.runtime_root, config.project_id),
    ]
    writer.close()
    owner.close()
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize("shape", ["list", "derived", "reversed", "extra"])
def test_malformed_result_is_rejected_after_writer_then_owner_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shape: str,
) -> None:
    _config, store, bootstrap, reader, _owner_fd, _writer_fd = _real_pair(monkeypatch, tmp_path)
    real_close_owner = adoption_module._close_owner
    real_close_writer = adoption_module._close_writer
    events: list[str] = []

    def make_result(owner: OwnerLock, writer: StartupWriter) -> object:
        if shape == "list":
            return [owner, writer]
        if shape == "derived":
            return _DerivedTuple((owner, writer))
        if shape == "reversed":
            return (writer, owner)
        return (owner, writer, owner)

    def close_writer(writer: StartupWriter) -> None:
        events.append("close-writer")
        real_close_writer(writer)

    def close_owner(owner: OwnerLock) -> None:
        events.append("close-owner")
        real_close_owner(owner)

    monkeypatch.setattr(adoption_module, "_make_result", make_result)
    monkeypatch.setattr(adoption_module, "_close_writer", close_writer)
    monkeypatch.setattr(adoption_module, "_close_owner", close_owner)

    with pytest.raises(RuntimeError) as captured:
        adopt_sidecar_child_descriptors(bootstrap)

    _assert_fixed_error(captured.value)
    _assert_fixed_frames_are_empty(captured.value)
    assert events == ["close-writer", "close-owner"]
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize(
    "case",
    [
        "swapped",
        "closed-owner",
        "closed-writer",
        "regular-owner",
        "socket-owner",
        "regular-writer",
        "socket-writer",
        "duplicate-owner-description",
        "duplicate-writer-description",
    ],
)
def test_invalid_real_descriptor_pair_is_retired_without_unrelated_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    config, store, _bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    extra_socket: socket.socket | None = None
    other_socket: socket.socket | None = None
    if case == "swapped":
        candidate_owner, candidate_writer = writer_fd, owner_fd
    elif case == "closed-owner":
        os.close(owner_fd)
        candidate_owner, candidate_writer = owner_fd, writer_fd
    elif case == "closed-writer":
        os.close(writer_fd)
        candidate_owner, candidate_writer = owner_fd, writer_fd
    elif case in {"regular-owner", "regular-writer"}:
        replacement_target = owner_fd if case == "regular-owner" else writer_fd
        os.close(replacement_target)
        regular_fd = os.open(tmp_path / f"{case}.bin", os.O_RDWR | os.O_CREAT, 0o600)
        if case == "regular-owner":
            candidate_owner, candidate_writer = regular_fd, writer_fd
        else:
            candidate_owner, candidate_writer = owner_fd, regular_fd
    elif case in {"socket-owner", "socket-writer"}:
        replacement_target = owner_fd if case == "socket-owner" else writer_fd
        os.close(replacement_target)
        extra_socket, other_socket = socket.socketpair()
        socket_fd = extra_socket.detach()
        if case == "socket-owner":
            candidate_owner, candidate_writer = socket_fd, writer_fd
        else:
            candidate_owner, candidate_writer = owner_fd, socket_fd
    elif case == "duplicate-owner-description":
        os.close(writer_fd)
        candidate_owner = owner_fd
        candidate_writer = os.dup(owner_fd)
    else:
        os.close(owner_fd)
        candidate_owner = os.dup(writer_fd)
        candidate_writer = writer_fd

    candidate = _forge_bootstrap(config, candidate_owner, candidate_writer)
    sentinel = os.open(os.devnull, os.O_RDONLY)
    try:
        with pytest.raises(RuntimeError) as captured:
            adopt_sidecar_child_descriptors(candidate)
        _assert_fixed_error(captured.value)
        assert _is_open(sentinel)
        assert not _is_open(candidate_owner)
        assert not _is_open(candidate_writer)
    finally:
        os.close(sentinel)
        if other_socket is not None:
            other_socket.close()
        if extra_socket is not None:
            extra_socket.close()
        reader.close()
    _assert_successor_can_acquire(store)


def _reuse_closed_descriptor(file_descriptor: int) -> int:
    replacement = os.open(os.devnull, os.O_RDONLY)
    if replacement != file_descriptor:
        os.dup2(replacement, file_descriptor)
        os.close(replacement)
    assert _is_open(file_descriptor)
    return file_descriptor


@pytest.mark.parametrize("resource", ["raw", "writer", "owner"])
def test_ambiguous_close_reuse_is_never_retried_and_cleanup_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resource: str,
) -> None:
    _config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    real_close_raw = adoption_module._close_raw
    real_close_owner = adoption_module._close_owner
    real_close_writer = adoption_module._close_writer
    events: list[str] = []
    reused_descriptor = -1

    def fail_preflight(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("private preflight failure")

    def close_raw(file_descriptor: int) -> None:
        nonlocal reused_descriptor
        label = "writer" if file_descriptor == writer_fd else "owner"
        events.append(f"close-raw-{label}")
        real_close_raw(file_descriptor)
        if resource == "raw" and file_descriptor == writer_fd:
            reused_descriptor = _reuse_closed_descriptor(file_descriptor)
            raise OSError("ambiguous raw close")

    def fail_writer(file_descriptor: int) -> NoReturn:
        events.append("adopt-writer-fail")
        os.close(file_descriptor)
        raise OSError("writer adoption consumed then failed")

    def close_writer(writer: StartupWriter) -> None:
        nonlocal reused_descriptor
        events.append("close-writer")
        file_descriptor = writer.fileno()
        real_close_writer(writer)
        if resource == "writer":
            reused_descriptor = _reuse_closed_descriptor(file_descriptor)
            raise OSError("ambiguous writer close")

    def close_owner(owner: OwnerLock) -> None:
        nonlocal reused_descriptor
        events.append("close-owner")
        file_descriptor = owner.fileno()
        real_close_owner(owner)
        if resource == "owner":
            reused_descriptor = _reuse_closed_descriptor(file_descriptor)
            raise OSError("ambiguous owner close")

    if resource == "raw":
        monkeypatch.setattr(adoption_module, "_encode_bootstrap", fail_preflight)
        monkeypatch.setattr(adoption_module, "_close_raw", close_raw)
    elif resource == "writer":
        monkeypatch.setattr(
            adoption_module,
            "_make_result",
            lambda *_args: (_ for _ in ()).throw(OSError("result failure")),
        )
        monkeypatch.setattr(adoption_module, "_close_writer", close_writer)
        monkeypatch.setattr(adoption_module, "_close_owner", close_owner)
    else:
        monkeypatch.setattr(adoption_module, "_adopt_writer", fail_writer)
        monkeypatch.setattr(adoption_module, "_close_owner", close_owner)

    with pytest.raises(RuntimeError) as captured:
        adopt_sidecar_child_descriptors(bootstrap)

    _assert_fixed_error(captured.value)
    assert reused_descriptor >= 3
    assert _is_open(reused_descriptor)
    assert (
        events
        == {
            "raw": ["close-raw-writer", "close-raw-owner"],
            "writer": ["close-writer", "close-owner"],
            "owner": ["adopt-writer-fail", "close-owner"],
        }[resource]
    )
    os.close(reused_descriptor)
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize(
    (
        "active_kind",
        "writer_cleanup_kind",
        "owner_cleanup_kind",
        "expected_kind",
        "expect_note",
    ),
    [
        ("ordinary", "ordinary", "success", "fixed", False),
        ("ordinary", "system-exit", "success", "writer-control", False),
        ("ordinary", "ordinary", "system-exit", "owner-control", True),
        ("ordinary", "system-exit", "keyboard", "writer-control", True),
        ("keyboard", "system-exit", "ordinary", "active-control", True),
        ("keyboard", "success", "success", "active-control", False),
    ],
)
def test_operation_and_cleanup_failure_precedence_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_kind: str,
    writer_cleanup_kind: str,
    owner_cleanup_kind: str,
    expected_kind: str,
    expect_note: bool,
) -> None:
    _config, store, bootstrap, reader, _owner_fd, _writer_fd = _real_pair(monkeypatch, tmp_path)
    active_error: BaseException = (
        OSError("ordinary operation failure")
        if active_kind == "ordinary"
        else KeyboardInterrupt("operation control")
    )
    writer_error: BaseException | None = {
        "success": None,
        "ordinary": OSError("writer cleanup failure"),
        "system-exit": SystemExit("writer cleanup control"),
    }[writer_cleanup_kind]
    owner_error: BaseException | None = {
        "success": None,
        "ordinary": OSError("owner cleanup failure"),
        "system-exit": SystemExit("owner cleanup control"),
        "keyboard": KeyboardInterrupt("owner cleanup control"),
    }[owner_cleanup_kind]
    selected: BaseException | None = {
        "fixed": None,
        "writer-control": writer_error,
        "owner-control": owner_error,
        "active-control": active_error,
    }[expected_kind]
    real_close_owner = adoption_module._close_owner
    real_close_writer = adoption_module._close_writer
    events: list[str] = []

    def fail_result(*_args: object) -> NoReturn:
        raise active_error

    def close_writer(writer: StartupWriter) -> None:
        events.append("close-writer")
        real_close_writer(writer)
        if writer_error is not None:
            raise writer_error

    def close_owner(owner: OwnerLock) -> None:
        events.append("close-owner")
        real_close_owner(owner)
        if owner_error is not None:
            raise owner_error

    monkeypatch.setattr(adoption_module, "_make_result", fail_result)
    monkeypatch.setattr(adoption_module, "_close_writer", close_writer)
    monkeypatch.setattr(adoption_module, "_close_owner", close_owner)

    if selected is None:
        with pytest.raises(RuntimeError) as captured:
            adopt_sidecar_child_descriptors(bootstrap)
        _assert_fixed_error(captured.value)
    elif isinstance(selected, SystemExit):
        with pytest.raises(SystemExit) as captured_control:
            adopt_sidecar_child_descriptors(bootstrap)
        assert captured_control.value is selected
        assert getattr(captured_control.value, "__notes__", []) == (
            [CLEANUP_NOTE] if expect_note else []
        )
    else:
        with pytest.raises(KeyboardInterrupt) as captured_control:
            adopt_sidecar_child_descriptors(bootstrap)
        assert captured_control.value is selected
        assert getattr(captured_control.value, "__notes__", []) == (
            [CLEANUP_NOTE] if expect_note else []
        )

    assert events == ["close-writer", "close-owner"]
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize("baseline_kind", ["value-error", "keyboard"])
@pytest.mark.parametrize(
    "outcome",
    ["success", "ordinary", "process-control", "cleanup-failure"],
)
def test_caller_active_exception_never_participates_in_arbitration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    baseline_kind: str,
    outcome: str,
) -> None:
    _config, store, bootstrap, reader, _owner_fd, _writer_fd = _real_pair(monkeypatch, tmp_path)
    baseline: BaseException = (
        ValueError("caller baseline")
        if baseline_kind == "value-error"
        else KeyboardInterrupt("caller baseline")
    )
    BaseException.add_note(baseline, "caller note")
    operation_control = _Control("operation control")
    real_close_writer = adoption_module._close_writer

    def fail_ordinary(*_args: object) -> NoReturn:
        raise OSError("ordinary result failure")

    def fail_control(*_args: object) -> NoReturn:
        raise operation_control

    def close_writer_then_fail(writer: StartupWriter) -> NoReturn:
        real_close_writer(writer)
        raise OSError("ordinary cleanup failure")

    if outcome in {"ordinary", "cleanup-failure"}:
        monkeypatch.setattr(adoption_module, "_make_result", fail_ordinary)
    elif outcome == "process-control":
        monkeypatch.setattr(adoption_module, "_make_result", fail_control)
    if outcome == "cleanup-failure":
        monkeypatch.setattr(
            adoption_module,
            "_close_writer",
            close_writer_then_fail,
        )

    try:
        raise baseline
    except BaseException as active_baseline:
        assert active_baseline is baseline
        if outcome == "success":
            owner, writer = adopt_sidecar_child_descriptors(bootstrap)
            writer.close()
            owner.close()
        elif outcome == "process-control":
            with pytest.raises(_Control) as captured_control:
                adopt_sidecar_child_descriptors(bootstrap)
            assert captured_control.value is operation_control
            assert getattr(captured_control.value, "__notes__", []) == []
        else:
            with pytest.raises(RuntimeError) as captured:
                adopt_sidecar_child_descriptors(bootstrap)
            _assert_fixed_error(captured.value, context=baseline)
            _assert_fixed_frames_are_empty(captured.value)

    assert getattr(baseline, "__notes__", []) == ["caller note"]
    reader.close()
    _assert_successor_can_acquire(store)


def test_captured_unbound_add_note_bypasses_hostile_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config, store, bootstrap, reader, _owner_fd, _writer_fd = _real_pair(monkeypatch, tmp_path)
    control = _HostileControl("hostile operation control")
    _HostileControl.override_calls = 0
    real_close_writer = adoption_module._close_writer

    def fail_result(*_args: object) -> NoReturn:
        raise control

    def fail_cleanup(writer: StartupWriter) -> NoReturn:
        real_close_writer(writer)
        raise OSError("cleanup failed")

    monkeypatch.setattr(adoption_module, "_make_result", fail_result)
    monkeypatch.setattr(adoption_module, "_close_writer", fail_cleanup)

    with pytest.raises(_HostileControl) as captured:
        adopt_sidecar_child_descriptors(bootstrap)

    assert captured.value is control
    assert captured.value.__notes__ == [CLEANUP_NOTE]
    assert _HostileControl.override_calls == 0
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize("note_failure_kind", ["invalid-notes", "ordinary", "control"])
def test_note_attachment_failure_never_replaces_active_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    note_failure_kind: str,
) -> None:
    _config, store, bootstrap, reader, _owner_fd, _writer_fd = _real_pair(monkeypatch, tmp_path)
    control = _Control("operation control")
    invalid_notes = object()
    if note_failure_kind == "invalid-notes":
        control.__notes__ = invalid_notes  # type: ignore[assignment]
    real_close_writer = adoption_module._close_writer

    def fail_result(*_args: object) -> NoReturn:
        raise control

    def fail_cleanup(writer: StartupWriter) -> NoReturn:
        real_close_writer(writer)
        raise OSError("cleanup failed")

    def fail_note(*_args: object) -> NoReturn:
        if note_failure_kind == "ordinary":
            raise RuntimeError("note failure")
        raise SystemExit("note control")

    monkeypatch.setattr(adoption_module, "_make_result", fail_result)
    monkeypatch.setattr(adoption_module, "_close_writer", fail_cleanup)
    if note_failure_kind != "invalid-notes":
        monkeypatch.setattr(adoption_module, "_add_note", fail_note)

    with pytest.raises(_Control) as captured:
        adopt_sidecar_child_descriptors(bootstrap)

    assert captured.value is control
    if note_failure_kind == "invalid-notes":
        assert captured.value.__notes__ is invalid_notes
    else:
        assert getattr(captured.value, "__notes__", []) == []
    reader.close()
    _assert_successor_can_acquire(store)


@pytest.mark.parametrize(
    "replacement_name",
    [
        "package-encoder",
        "module-encoder",
        "package-store",
        "module-store",
        "owner-adopter",
        "writer-adopter",
        "owner-close",
        "writer-close",
    ],
)
def test_public_dependency_replacement_cannot_redirect_frozen_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement_name: str,
) -> None:
    config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    events: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        events.append("replacement-called")
        raise AssertionError("public replacement redirected frozen dispatch")

    replacements: dict[str, tuple[object, str, object]] = {
        "package-encoder": (
            sidecar_package,
            "encode_sidecar_child_bootstrap",
            forbidden,
        ),
        "module-encoder": (
            bootstrap_module,
            "encode_sidecar_child_bootstrap",
            forbidden,
        ),
        "package-store": (sidecar_package, "StateStore", forbidden),
        "module-store": (state_module, "StateStore", forbidden),
        "owner-adopter": (OwnerLock, "adopt_inherited", classmethod(forbidden)),
        "writer-adopter": (
            StartupWriter,
            "adopt_inherited",
            classmethod(forbidden),
        ),
        "owner-close": (OwnerLock, "close", forbidden),
        "writer-close": (StartupWriter, "close", forbidden),
    }
    target, attribute, replacement = replacements[replacement_name]
    original = getattr(target, attribute)
    monkeypatch.setattr(target, attribute, replacement)

    if replacement_name == "module-store":
        constructed = adoption_module._construct_state_store(
            config.runtime_root,
            config.project_id,
        )
        assert type(constructed) is StateStore
        assert events == []
        os.close(writer_fd)
        os.close(owner_fd)
        reader.close()
        monkeypatch.setattr(target, attribute, original)
        successor = OwnerLock.acquire(store)
        adoption_module._close_owner(successor)
        return

    owner, writer = adopt_sidecar_child_descriptors(bootstrap)
    assert type(owner) is OwnerLock
    assert type(writer) is StartupWriter
    adoption_module._close_writer(writer)
    adoption_module._close_owner(owner)
    assert events == []
    reader.close()
    successor = OwnerLock.acquire(store)
    adoption_module._close_owner(successor)


def test_raw_close_dispatch_is_frozen_from_later_os_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_descriptor = os.open(os.devnull, os.O_RDONLY)
    events: list[str] = []

    def forbidden(_file_descriptor: int) -> NoReturn:
        events.append("replacement-called")
        raise AssertionError("public os.close replacement ran")

    with monkeypatch.context() as replacement:
        replacement.setattr(os, "close", forbidden)
        adoption_module._close_raw(file_descriptor)

    assert events == []
    assert not _is_open(file_descriptor)


def test_fixed_failure_releases_private_exception_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, store, bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    private_marker = f"private:{config.runtime_root}:{owner_fd}:{writer_fd}:errno=37"
    _TrackedFailure.references.clear()

    def fail_encoder(*_args: object, **_kwargs: object) -> NoReturn:
        raise _TrackedFailure(private_marker)

    monkeypatch.setattr(adoption_module, "_encode_bootstrap", fail_encoder)
    capsys.readouterr()

    with pytest.raises(RuntimeError) as captured:
        adopt_sidecar_child_descriptors(bootstrap)

    _assert_fixed_error(captured.value)
    _assert_fixed_frames_are_empty(captured.value)
    formatted = "".join(traceback.format_exception(captured.value))
    assert private_marker not in formatted
    assert config.project_root not in formatted
    assert config.runtime_root not in formatted
    assert str(owner_fd) not in str(captured.value)
    assert str(writer_fd) not in str(captured.value)
    assert capsys.readouterr() == ("", "")
    gc.collect()
    assert _TrackedFailure.references
    assert all(reference() is None for reference in _TrackedFailure.references)
    reader.close()
    _assert_successor_can_acquire(store)


def test_descriptor_maximum_is_admitted_then_semantically_retired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, _bootstrap, reader, real_owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    maximum = 2_147_483_647
    candidate = _forge_bootstrap(config, maximum, writer_fd)
    real_adopt_owner = adoption_module._adopt_owner
    adopted: list[int] = []

    def adopt_owner(candidate_store: StateStore, file_descriptor: int) -> OwnerLock:
        adopted.append(file_descriptor)
        return real_adopt_owner(candidate_store, file_descriptor)

    monkeypatch.setattr(adoption_module, "_adopt_owner", adopt_owner)

    with pytest.raises(RuntimeError) as captured:
        adopt_sidecar_child_descriptors(candidate)

    _assert_fixed_error(captured.value)
    assert adopted == [maximum]
    assert not _is_open(maximum)
    assert not _is_open(writer_fd)
    os.close(real_owner_fd)
    reader.close()
    _assert_successor_can_acquire(store)


def test_real_owner_descriptor_three_is_admitted_and_transferred(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, _bootstrap, reader, owner_fd, writer_fd = _real_pair(monkeypatch, tmp_path)
    reader_fd = reader.fileno()
    external_backup = -1
    external_inheritable: bool | None = None
    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    installed_at_three = False
    descriptor_three_is_ours = 3 in {reader_fd, owner_fd, writer_fd}
    if not descriptor_three_is_ours:
        try:
            external_inheritable = os.get_inheritable(3)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            external_backup = os.dup(3)
    try:
        if reader_fd == 3:
            replacement_reader_fd = os.dup(reader_fd)
            object.__setattr__(reader, "_descriptor", replacement_reader_fd)
            os.close(reader_fd)
        if writer_fd == 3:
            replacement_writer_fd = os.dup(writer_fd)
            os.close(writer_fd)
            writer_fd = replacement_writer_fd
        if owner_fd != 3:
            os.dup2(owner_fd, 3, inheritable=True)
            os.close(owner_fd)
            owner_fd = 3
        installed_at_three = True
        candidate = _forge_bootstrap(config, owner_fd, writer_fd)

        owner, writer = adopt_sidecar_child_descriptors(candidate)

        assert owner.fileno() == 3
        assert os.get_inheritable(3) is False
        writer.close()
        writer = None
        owner.close()
        owner = None
        _assert_successor_can_acquire(store)
    finally:
        if writer is not None:
            adoption_module._close_writer(writer)
        if owner is not None:
            adoption_module._close_owner(owner)
        reader.close()
        if external_backup >= 0:
            assert external_inheritable is not None
            os.dup2(
                external_backup,
                3,
                inheritable=external_inheritable,
            )
            os.close(external_backup)
        elif installed_at_three and _is_open(3):
            os.close(3)


def test_adoption_does_not_mutate_state_paths_and_emits_no_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _config, store, bootstrap, reader, _owner_fd, _writer_fd = _real_pair(monkeypatch, tmp_path)
    state_bytes = b'{"sentinel":"unchanged"}\n'
    store.state_path.write_bytes(state_bytes)
    store.state_path.chmod(0o600)
    state_inode = store.state_path.stat().st_ino
    owner_inode = store.lock_path.stat().st_ino
    before_entries = {
        path.name: (path.stat().st_dev, path.stat().st_ino, path.stat().st_mode)
        for path in store.runtime_dir.iterdir()
    }
    calls: list[str] = []

    def forbidden(self: StateStore, *_args: object, **_kwargs: object) -> NoReturn:
        del self
        calls.append("state-mutation")
        raise AssertionError("state mutation method ran")

    for method_name in (
        "ensure_private_directory",
        "load",
        "publish",
        "remove_if_owned",
    ):
        monkeypatch.setattr(StateStore, method_name, forbidden)
    capsys.readouterr()

    owner, writer = adopt_sidecar_child_descriptors(bootstrap)
    adoption_module._close_writer(writer)
    adoption_module._close_owner(owner)

    after_entries = {
        path.name: (path.stat().st_dev, path.stat().st_ino, path.stat().st_mode)
        for path in store.runtime_dir.iterdir()
    }
    assert calls == []
    assert before_entries == after_entries
    assert store.state_path.stat().st_ino == state_inode
    assert store.state_path.read_bytes() == state_bytes
    assert store.lock_path.stat().st_ino == owner_inode
    assert capsys.readouterr() == ("", "")
    reader.close()
    _assert_successor_can_acquire(store)


def _call_target(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return f"{function.value.id}.{function.attr}"
    return "<dynamic>"


def test_production_ast_is_a_positive_allowlist() -> None:
    source = Path(adoption_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.append(("import", 0, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                (
                    "from",
                    node.level,
                    node.module,
                    tuple((alias.name, alias.asname) for alias in node.names),
                )
            )
    assert imports == [
        ("from", 0, "__future__", (("annotations", None),)),
        ("import", 0, ("os",)),
        ("from", 0, "pathlib", (("Path", None),)),
        (
            "from",
            0,
            "typing",
            (("Final", None), ("NoReturn", None)),
        ),
        (
            "from",
            1,
            "child_bootstrap",
            (
                ("SidecarChildBootstrap", None),
                ("encode_sidecar_child_bootstrap", None),
            ),
        ),
        ("from", 1, "owner_lock", (("OwnerLock", None),)),
        ("from", 1, "runtime_config", (("SidecarRuntimeConfig", None),)),
        ("from", 1, "startup_channel", (("StartupWriter", None),)),
        ("from", 1, "state", (("StateStore", None),)),
    ]
    assert sum(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)) == len(
        imports
    )

    module_functions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert [node.name for node in module_functions] == [
        "_encode_bootstrap",
        "_construct_state_store",
        "_adopt_owner",
        "_adopt_writer",
        "_close_raw",
        "_close_owner",
        "_close_writer",
        "_add_note",
        "_path_text",
        "_make_result",
        "_read_bootstrap_slots",
        "_shallow_admission",
        "_canonical_encoding",
        "_read_config_identity",
        "_admit_store",
        "_record_cleanup_failure",
        "_cleanup_resources",
        "_best_effort_cleanup_note",
        "_adopt_pair",
        "_raise_adoption_failure",
        "adopt_sidecar_child_descriptors",
    ]
    module_classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in module_classes] == ["_AdoptionFailure"]
    assert [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)] == module_classes
    assert len(module_classes[0].bases) == 1
    assert isinstance(module_classes[0].bases[0], ast.Name)
    assert module_classes[0].bases[0].id == "Exception"
    assert len(module_classes[0].body) == 1
    assert isinstance(module_classes[0].body[0], ast.Pass)
    assert all(node.decorator_list == [] for node in module_functions)
    assert module_classes[0].decorator_list == []
    expected_arguments = {
        "_encode_bootstrap": ("config", "owner_lock_fd", "startup_writer_fd"),
        "_construct_state_store": ("runtime_root", "project_id"),
        "_adopt_owner": ("store", "file_descriptor"),
        "_adopt_writer": ("file_descriptor",),
        "_close_raw": ("file_descriptor",),
        "_close_owner": ("owner",),
        "_close_writer": ("writer",),
        "_add_note": ("error", "note"),
        "_path_text": ("path",),
        "_make_result": ("owner", "writer"),
        "_read_bootstrap_slots": ("bootstrap",),
        "_shallow_admission": ("value",),
        "_canonical_encoding": ("value",),
        "_read_config_identity": ("config",),
        "_admit_store": ("value", "runtime_root", "project_id"),
        "_record_cleanup_failure": (
            "current_control",
            "cleanup_error",
            "prior_failure",
        ),
        "_cleanup_resources": (
            "writer",
            "raw_writer_fd",
            "owner",
            "raw_owner_fd",
        ),
        "_best_effort_cleanup_note": ("error",),
        "_adopt_pair": ("value",),
        "_raise_adoption_failure": (),
        "adopt_sidecar_child_descriptors": ("bootstrap",),
    }
    for function in module_functions:
        assert function.args.posonlyargs == []
        assert (
            tuple(argument.arg for argument in function.args.args)
            == expected_arguments[function.name]
        )
        assert function.args.vararg is None
        assert function.args.kwonlyargs == []
        assert function.args.kw_defaults == []
        assert function.args.kwarg is None
        assert function.args.defaults == []
    assert not any(
        isinstance(
            node,
            (
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.Global,
                ast.Nonlocal,
                ast.With,
                ast.AsyncWith,
                ast.Yield,
                ast.YieldFrom,
            ),
        )
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for function in module_functions
        for node in ast.walk(function)
        if node is not function
    )

    all_annotated_names = [
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    annotated = {
        node.target.id: node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }
    assert all_annotated_names == [
        "_MAX_DESCRIPTOR",
        "_CLEANUP_NOTE",
        "_BOOTSTRAP_TYPE",
        "_CONFIG_TYPE",
        "_STORE_TYPE",
        "_OWNER_TYPE",
        "_WRITER_TYPE",
        "_PATH_TYPE",
        "_ENCODE_BOOTSTRAP",
        "_STATE_STORE_CONSTRUCTOR",
        "_ADOPT_OWNER",
        "_ADOPT_WRITER",
        "_RAW_CLOSE",
        "_OWNER_CLOSE",
        "_WRITER_CLOSE",
        "_ADD_NOTE",
        "_FSPATH",
    ]
    assert list(annotated) == all_annotated_names
    module_type_aliases = [
        node.name.id
        for node in tree.body
        if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name)
    ]
    assert module_type_aliases == ["_AdoptionResult", "_ShallowAdmission"]
    assert sum(isinstance(node, ast.TypeAlias) for node in ast.walk(tree)) == len(
        module_type_aliases
    )
    frozen_bindings = {
        "_ENCODE_BOOTSTRAP": "encode_sidecar_child_bootstrap",
        "_STATE_STORE_CONSTRUCTOR": "StateStore",
        "_ADOPT_OWNER": "OwnerLock.adopt_inherited",
        "_ADOPT_WRITER": "StartupWriter.adopt_inherited",
        "_RAW_CLOSE": "os.close",
        "_OWNER_CLOSE": "OwnerLock.close",
        "_WRITER_CLOSE": "StartupWriter.close",
        "_ADD_NOTE": "BaseException.add_note",
        "_FSPATH": "os.fspath",
    }
    for binding, expected in frozen_bindings.items():
        value = annotated[binding]
        if isinstance(value, ast.Name):
            actual = value.id
        elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            actual = f"{value.value.id}.{value.attr}"
        else:
            actual = "<dynamic>"
        assert actual == expected
    assert not any(isinstance(node, (ast.Assign, ast.AugAssign)) for node in tree.body)
    assert not any(
        isinstance(
            node,
            (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp),
        )
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, (ast.Attribute, ast.Subscript))
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(tree)
    )
    module_expressions = [node for node in tree.body if isinstance(node, ast.Expr)]
    assert len(module_expressions) == 1
    assert isinstance(module_expressions[0].value, ast.Constant)
    assert type(module_expressions[0].value.value) is str
    assert all(
        isinstance(
            node,
            (
                ast.Expr,
                ast.Import,
                ast.ImportFrom,
                ast.AnnAssign,
                ast.TypeAlias,
                ast.ClassDef,
                ast.FunctionDef,
            ),
        )
        for node in tree.body
    )

    call_counts = Counter(
        _call_target(node) for node in ast.walk(tree) if isinstance(node, ast.Call)
    )
    assert call_counts == Counter(
        {
            "Path": 1,
            "RuntimeError": 1,
            "_ADD_NOTE": 1,
            "_ADOPT_OWNER": 1,
            "_ADOPT_WRITER": 1,
            "_AdoptionFailure": 1,
            "_ENCODE_BOOTSTRAP": 1,
            "_FSPATH": 1,
            "_OWNER_CLOSE": 1,
            "_RAW_CLOSE": 1,
            "_STATE_STORE_CONSTRUCTOR": 1,
            "_WRITER_CLOSE": 1,
            "_add_note": 1,
            "_admit_store": 1,
            "_adopt_owner": 1,
            "_adopt_pair": 1,
            "_adopt_writer": 1,
            "_best_effort_cleanup_note": 2,
            "_canonical_encoding": 1,
            "_cleanup_resources": 1,
            "_close_owner": 1,
            "_close_raw": 2,
            "_close_writer": 1,
            "_construct_state_store": 1,
            "_encode_bootstrap": 1,
            "_make_result": 1,
            "_path_text": 1,
            "_raise_adoption_failure": 1,
            "_read_bootstrap_slots": 1,
            "_read_config_identity": 1,
            "_record_cleanup_failure": 4,
            "_shallow_admission": 1,
            "all": 1,
            "isinstance": 2,
            "len": 4,
            "object.__getattribute__": 7,
            "type": 19,
        }
    )
    frozen_call_owners = {
        "_ENCODE_BOOTSTRAP": "_encode_bootstrap",
        "_STATE_STORE_CONSTRUCTOR": "_construct_state_store",
        "_ADOPT_OWNER": "_adopt_owner",
        "_ADOPT_WRITER": "_adopt_writer",
        "_RAW_CLOSE": "_close_raw",
        "_OWNER_CLOSE": "_close_owner",
        "_WRITER_CLOSE": "_close_writer",
        "_ADD_NOTE": "_add_note",
        "_FSPATH": "_path_text",
    }
    for call_target, expected_owner in frozen_call_owners.items():
        owners = [
            function.name
            for function in module_functions
            if any(
                isinstance(node, ast.Call) and _call_target(node) == call_target
                for node in ast.walk(function)
            )
        ]
        assert owners == [expected_owner]
    assert {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    } == {"close", "fspath"}

    adopt_pair = next(node for node in module_functions if node.name == "_adopt_pair")
    critical_calls = [
        (node.func.id, node.lineno)
        for node in ast.walk(adopt_pair)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_adopt_owner", "_adopt_writer", "_make_result"}
    ]
    assert [name for name, _line in sorted(critical_calls, key=lambda item: item[1])] == [
        "_adopt_owner",
        "_adopt_writer",
        "_make_result",
    ]
