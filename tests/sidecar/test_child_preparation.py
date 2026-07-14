from __future__ import annotations

import ast
import gc
import inspect
import os
import signal
import stat
import subprocess
import sys
import traceback
import weakref
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    OwnerLock,
    OwnerLockError,
    OwnerLockErrorCode,
    SidecarChildBootstrap,
    SidecarRuntimeConfig,
    StartupChannelError,
    StartupChannelErrorCode,
    StartupFailure,
    StartupFailureCode,
    StartupReader,
    StartupWriter,
    StateStore,
    encode_sidecar_child_bootstrap,
    open_startup_channel,
    prepare_sidecar_child,
    prepare_sidecar_runtime_config,
)
from flowsight.sidecar import child_adoption as adoption_module
from flowsight.sidecar import child_bootstrap as bootstrap_module
from flowsight.sidecar import child_preparation as preparation_module
from flowsight.sidecar import runtime_config as config_module

DECODE_ERROR = "sidecar child bootstrap is invalid"
ADOPTION_ERROR = "sidecar child descriptor adoption failed"
STARTUP_FAILURE = StartupFailure(code=StartupFailureCode.SIDECAR_STARTUP_FAILED)
CHILD_TIMEOUT = 10.0

type _RawPair = tuple[
    SidecarRuntimeConfig,
    StateStore,
    tuple[str, ...],
    StartupReader,
    int,
    int,
    int,
]


class _Control(BaseException):
    pass


class _TrackedFailure(Exception):
    pass


class _TupleSubclass(tuple[str, ...]):
    pass


def _project(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    project.mkdir()
    return project


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[SidecarRuntimeConfig, StateStore]:
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )
    config = prepare_sidecar_runtime_config(_project(tmp_path))
    store = StateStore(config.runtime_root, project_id=config.project_id)
    store.ensure_private_directory()
    return config, store


def _raw_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _RawPair:
    config, store = _prepare(monkeypatch, tmp_path)
    parent_owner = OwnerLock.acquire(store)
    reader, parent_writer = open_startup_channel()
    owner_fd = -1
    writer_fd = -1
    handoff_fd = -1
    handoff_writer = -1
    try:
        owner_fd = os.dup(parent_owner.fileno())
        writer_fd = os.dup(parent_writer.fileno())
        handoff_fd, handoff_writer = os.pipe()
        parent_owner.close()
        parent_writer.close()
        os.close(handoff_writer)
        handoff_writer = -1
        arguments = encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=owner_fd,
            startup_writer_fd=writer_fd,
            parent_handoff_fd=handoff_fd,
        )
        return config, store, arguments, reader, owner_fd, writer_fd, handoff_fd
    except BaseException:
        parent_owner.close()
        parent_writer.close()
        reader.close()
        _close_raw(handoff_writer)
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)
        raise


def _close_raw(file_descriptor: int) -> None:
    if file_descriptor < 0:
        return
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _is_open(file_descriptor: int) -> bool:
    try:
        os.fstat(file_descriptor)
    except OSError:
        return False
    return True


def _assert_successor_can_acquire(store: StateStore) -> None:
    successor = OwnerLock.acquire(store)
    successor.close()


def _assert_fixed_error(
    error: BaseException,
    expected_type: type[ValueError] | type[RuntimeError],
    expected_text: str,
    *,
    context: BaseException | None = None,
) -> None:
    assert type(error) is expected_type
    assert str(error) == expected_text
    assert error.args == (expected_text,)
    assert error.__cause__ is None
    assert error.__context__ is context
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", []) == []


def _production_frames(error: BaseException) -> list[tuple[str, dict[str, object]]]:
    production_path = Path(preparation_module.__file__).resolve()
    frames: list[tuple[str, dict[str, object]]] = []
    current = error.__traceback__
    while current is not None:
        if Path(current.tb_frame.f_code.co_filename).resolve() == production_path:
            frames.append((current.tb_frame.f_code.co_name, dict(current.tb_frame.f_locals)))
        current = current.tb_next
    return frames


def _contains_identity(value: object, targets: tuple[object, ...]) -> bool:
    if any(value is target for target in targets):
        return True
    if type(value) in {tuple, list}:
        return any(_contains_identity(item, targets) for item in value)
    if type(value) is dict:
        mapping = value
        return any(
            _contains_identity(key, targets) or _contains_identity(item, targets)
            for key, item in mapping.items()  # type: ignore[union-attr]
        )
    return False


def _assert_frames_hide(error: BaseException, *targets: object) -> None:
    frames = _production_frames(error)
    assert frames
    assert {name for name, _locals in frames} <= {
        "prepare_sidecar_child",
        "_raise_decode_failure",
        "_raise_adoption_failure",
    }
    exact_targets = tuple(targets)
    assert all(
        not _contains_identity(value, exact_targets)
        for _name, local_values in frames
        for value in local_values.values()
    )


def _adopt_and_close_raw_pair(
    store: StateStore,
    reader: StartupReader,
    owner_fd: int,
    writer_fd: int,
) -> None:
    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    try:
        owner = OwnerLock.adopt_inherited(store, owner_fd)
        writer = StartupWriter.adopt_inherited(writer_fd)
        writer.close()
        writer = None
        owner.close()
        owner = None
    finally:
        if writer is not None:
            writer.close()
        if owner is not None:
            owner.close()
        reader.close()


def test_public_surface_is_exact() -> None:
    assert sidecar_package.prepare_sidecar_child is prepare_sidecar_child
    assert sidecar_package.__all__.count("prepare_sidecar_child") == 1
    assert preparation_module.prepare_sidecar_child is prepare_sidecar_child

    signature = inspect.signature(prepare_sidecar_child)
    assert tuple(signature.parameters) == ("arguments",)
    assert signature.parameters["arguments"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["arguments"].annotation == "tuple[str, ...]"
    assert signature.return_annotation == ("tuple[SidecarRuntimeConfig, OwnerLock, StartupWriter]")
    assert get_type_hints(prepare_sidecar_child) == {
        "arguments": tuple[str, ...],
        "return": tuple[SidecarRuntimeConfig, OwnerLock, StartupWriter],
    }
    defined_public_functions = [
        node.name
        for node in ast.parse(Path(preparation_module.__file__).read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    assert defined_public_functions == ["prepare_sidecar_child"]


def test_same_process_success_returns_exact_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    canonical_decoder = preparation_module._decode_bootstrap
    calls: list[tuple[str, object]] = []

    def delegate(value: tuple[str, ...]) -> object:
        calls.append(("arguments", value))
        bootstrap = canonical_decoder(value)
        calls.append(("bootstrap", bootstrap))
        return bootstrap

    monkeypatch.setattr(preparation_module, "_decode_bootstrap", delegate)
    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    expected_owner_fd = owner_fd
    expected_writer_fd = writer_fd
    try:
        try:
            result = prepare_sidecar_child(arguments)
        except RuntimeError:
            owner_fd = -1
            writer_fd = -1
            raise
        owner_fd = -1
        writer_fd = -1
        assert type(result) is tuple
        assert len(result) == 3
        returned_config, owner, writer = result
        bootstrap = calls[1][1]
        assert calls[0] == ("arguments", arguments)
        assert calls[0][1] is arguments
        assert type(bootstrap) is SidecarChildBootstrap
        assert returned_config is object.__getattribute__(bootstrap, "config")
        assert returned_config is not config
        assert returned_config == config
        assert type(owner) is OwnerLock
        assert type(writer) is StartupWriter
        assert owner.fileno() == expected_owner_fd
        assert writer.fileno() == expected_writer_fd
        assert os.get_inheritable(owner.fileno()) is False
        assert os.get_inheritable(writer.fileno()) is False

        with pytest.raises(OwnerLockError) as held:
            OwnerLock.acquire(store)
        assert held.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD

        writer.send(STARTUP_FAILURE)
        writer = None
        assert reader.receive(timeout=1.0) == STARTUP_FAILURE
        owner.close()
        owner = None
        _assert_successor_can_acquire(store)
    finally:
        if writer is not None:
            writer.close()
        if owner is not None:
            owner.close()
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


def test_handoff_failure_precedes_descriptor_adoption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    calls: list[str] = []

    def fail_handoff(_bootstrap: SidecarChildBootstrap) -> NoReturn:
        calls.append("handoff")
        raise RuntimeError("private handoff failure")

    def unexpected_adoption(_bootstrap: SidecarChildBootstrap) -> NoReturn:
        raise AssertionError("adoption must not run after handoff failure")

    monkeypatch.setattr(preparation_module, "_AWAIT_PARENT_HANDOFF", fail_handoff)
    monkeypatch.setattr(preparation_module, "_ADOPT_DESCRIPTORS", unexpected_adoption)
    try:
        with pytest.raises(RuntimeError) as captured:
            prepare_sidecar_child(arguments)
        _assert_fixed_error(captured.value, RuntimeError, ADOPTION_ERROR)
        assert calls == ["handoff"]
        assert _is_open(owner_fd)
        assert _is_open(writer_fd)
        transferred_owner_fd = owner_fd
        transferred_writer_fd = writer_fd
        owner_fd = -1
        writer_fd = -1
        _adopt_and_close_raw_pair(
            store,
            reader,
            transferred_owner_fd,
            transferred_writer_fd,
        )
        _assert_successor_can_acquire(store)
    finally:
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


@pytest.mark.parametrize("case", ["list", "tuple-subclass", "missing", "marker"])
def test_decode_rejection_never_touches_the_descriptor_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    _config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    if case == "list":
        rejected: object = list(arguments)
    elif case == "tuple-subclass":
        rejected = _TupleSubclass(arguments)
    elif case == "missing":
        rejected = arguments[:-1]
    else:
        rejected = ("not-flowsight", *arguments[1:])

    try:
        with pytest.raises(ValueError) as captured:
            prepare_sidecar_child(rejected)  # type: ignore[arg-type]
        _assert_fixed_error(captured.value, ValueError, DECODE_ERROR)
        assert _is_open(owner_fd)
        assert _is_open(writer_fd)
        transferred_owner_fd = owner_fd
        transferred_writer_fd = writer_fd
        owner_fd = -1
        writer_fd = -1
        _adopt_and_close_raw_pair(
            store,
            reader,
            transferred_owner_fd,
            transferred_writer_fd,
        )
        _assert_successor_can_acquire(store)
    finally:
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


@pytest.mark.parametrize("stage", ["decode", "adoption"])
def test_stage_ordinary_failures_are_private(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    marker = f"p0-018-{stage}-secret /private/stage"
    raw_error = _TrackedFailure(marker)
    raw_reference = weakref.ref(raw_error)
    raw_errors = [raw_error]
    decoded: list[object] = []
    canonical_decoder = preparation_module._decode_bootstrap

    def delegate(value: tuple[str, ...]) -> object:
        bootstrap = canonical_decoder(value)
        decoded.append(bootstrap)
        return bootstrap

    def fail_decode(_value: tuple[str, ...]) -> NoReturn:
        raise raw_errors[0]

    def fail_adoption(*_args: object, **_kwargs: object) -> NoReturn:
        raise raw_errors[0]

    capsys.readouterr()
    try:
        with monkeypatch.context() as stage_patch:
            if stage == "decode":
                stage_patch.setattr(preparation_module, "_decode_bootstrap", fail_decode)
                expected_type: type[ValueError] | type[RuntimeError] = ValueError
                expected_text = DECODE_ERROR
            else:
                stage_patch.setattr(preparation_module, "_decode_bootstrap", delegate)
                stage_patch.setattr(adoption_module, "_encode_bootstrap", fail_adoption)
                expected_type = RuntimeError
                expected_text = ADOPTION_ERROR
                owner_fd = -1
                writer_fd = -1
            with pytest.raises(expected_type) as captured:
                prepare_sidecar_child(arguments)

        error = captured.value
        _assert_fixed_error(error, expected_type, expected_text)
        _assert_frames_hide(error, arguments, config, raw_error, *decoded)
        assert marker not in "".join(traceback.format_exception(error))
        assert capsys.readouterr() == ("", "")

        if stage == "decode":
            assert _is_open(owner_fd)
            assert _is_open(writer_fd)
            transferred_owner_fd = owner_fd
            transferred_writer_fd = writer_fd
            owner_fd = -1
            writer_fd = -1
            _adopt_and_close_raw_pair(
                store,
                reader,
                transferred_owner_fd,
                transferred_writer_fd,
            )
        else:
            with pytest.raises(StartupChannelError) as closed:
                reader.receive(timeout=1.0)
            assert closed.value.code is StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED
        _assert_successor_can_acquire(store)

        raw_errors.clear()
        del error, captured, raw_error, fail_decode, fail_adoption, delegate
        gc.collect()
        assert raw_reference() is None
    finally:
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


@pytest.mark.parametrize("stage", ["decoder", "adopter"])
@pytest.mark.parametrize("control_factory", [KeyboardInterrupt, SystemExit, _Control])
def test_process_control_identity_and_ownership_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    control_factory: type[BaseException],
) -> None:
    _config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    control = control_factory()
    control.add_note("existing-control-note")

    def interrupt(*_args: object, **_kwargs: object) -> NoReturn:
        raise control

    try:
        with monkeypatch.context() as stage_patch:
            if stage == "decoder":
                stage_patch.setattr(preparation_module, "_decode_bootstrap", interrupt)
            else:
                stage_patch.setattr(adoption_module, "_encode_bootstrap", interrupt)
                owner_fd = -1
                writer_fd = -1
            with pytest.raises(control_factory) as captured:
                prepare_sidecar_child(arguments)

        assert captured.value is control
        assert control.__notes__ == ["existing-control-note"]
        if stage == "decoder":
            assert _is_open(owner_fd)
            assert _is_open(writer_fd)
            transferred_owner_fd = owner_fd
            transferred_writer_fd = writer_fd
            owner_fd = -1
            writer_fd = -1
            _adopt_and_close_raw_pair(
                store,
                reader,
                transferred_owner_fd,
                transferred_writer_fd,
            )
        else:
            with pytest.raises(StartupChannelError) as closed:
                reader.receive(timeout=1.0)
            assert closed.value.code is StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED
        _assert_successor_can_acquire(store)
    finally:
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


@pytest.mark.parametrize("baseline_factory", [ValueError, KeyboardInterrupt])
@pytest.mark.parametrize(
    "outcome",
    ["success", "decode-failure", "adoption-failure", "process-control"],
)
def test_caller_baseline_never_selects_the_composition_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    baseline_factory: type[BaseException],
    outcome: str,
) -> None:
    _config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    baseline = baseline_factory("caller baseline")
    baseline.add_note("caller-note")
    process_control = _Control()

    def fail_decode(_value: tuple[str, ...]) -> NoReturn:
        raise RuntimeError("decode-private")

    def fail_adoption(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("adoption-private")

    def interrupt_decode(_value: tuple[str, ...]) -> NoReturn:
        raise process_control

    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    observed: BaseException | None = None
    try:
        with monkeypatch.context() as stage_patch:
            if outcome == "decode-failure":
                stage_patch.setattr(preparation_module, "_decode_bootstrap", fail_decode)
            elif outcome == "adoption-failure":
                stage_patch.setattr(adoption_module, "_encode_bootstrap", fail_adoption)
                owner_fd = -1
                writer_fd = -1
            elif outcome == "process-control":
                stage_patch.setattr(preparation_module, "_decode_bootstrap", interrupt_decode)
            try:
                raise baseline
            except BaseException as active:
                assert active is baseline
                assert sys.exception() is baseline
                try:
                    result = prepare_sidecar_child(arguments)
                except BaseException as error:
                    observed = error
                else:
                    _returned_config, owner, writer = result
                    owner_fd = -1
                    writer_fd = -1
                assert sys.exception() is baseline

        assert baseline.__notes__ == ["caller-note"]
        if outcome == "success":
            assert observed is None
            assert owner is not None
            assert writer is not None
            writer.close()
            writer = None
            owner.close()
            owner = None
        elif outcome == "decode-failure":
            assert observed is not None
            _assert_fixed_error(observed, ValueError, DECODE_ERROR, context=baseline)
            transferred_owner_fd = owner_fd
            transferred_writer_fd = writer_fd
            owner_fd = -1
            writer_fd = -1
            _adopt_and_close_raw_pair(
                store,
                reader,
                transferred_owner_fd,
                transferred_writer_fd,
            )
        elif outcome == "adoption-failure":
            assert observed is not None
            _assert_fixed_error(observed, RuntimeError, ADOPTION_ERROR, context=baseline)
        else:
            assert observed is process_control
            assert _is_open(owner_fd)
            assert _is_open(writer_fd)
            transferred_owner_fd = owner_fd
            transferred_writer_fd = writer_fd
            owner_fd = -1
            writer_fd = -1
            _adopt_and_close_raw_pair(
                store,
                reader,
                transferred_owner_fd,
                transferred_writer_fd,
            )
        _assert_successor_can_acquire(store)
    finally:
        if writer is not None:
            writer.close()
        if owner is not None:
            owner.close()
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


def test_public_dependency_replacement_cannot_redirect_captured_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append("replacement")
        raise AssertionError("public replacement ran")

    monkeypatch.setattr(sidecar_package, "decode_sidecar_child_bootstrap", forbidden)
    monkeypatch.setattr(bootstrap_module, "decode_sidecar_child_bootstrap", forbidden)
    monkeypatch.setattr(sidecar_package, "adopt_sidecar_child_descriptors", forbidden)
    monkeypatch.setattr(adoption_module, "adopt_sidecar_child_descriptors", forbidden)

    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    expected_owner_fd = owner_fd
    expected_writer_fd = writer_fd
    try:
        try:
            _returned_config, owner, writer = prepare_sidecar_child(arguments)
        except RuntimeError:
            owner_fd = -1
            writer_fd = -1
            raise
        owner_fd = -1
        writer_fd = -1
        assert calls == []
        assert owner.fileno() == expected_owner_fd
        assert writer.fileno() == expected_writer_fd
        writer.close()
        writer = None
        owner.close()
        owner = None
        _assert_successor_can_acquire(store)
    finally:
        if writer is not None:
            writer.close()
        if owner is not None:
            owner.close()
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


def test_invocation_preserves_existing_paths_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    config, store, arguments, reader, owner_fd, writer_fd, handoff_fd = _raw_pair(
        monkeypatch, tmp_path
    )
    state_bytes = b'{"sentinel":"unchanged"}\n'
    store.state_path.write_bytes(state_bytes)
    store.state_path.chmod(0o600)

    def snapshot(path: Path) -> tuple[int, int, int, bytes]:
        metadata = path.stat()
        return metadata.st_dev, metadata.st_ino, stat.S_IMODE(metadata.st_mode), path.read_bytes()

    before_entries = tuple(sorted(path.name for path in store.runtime_dir.iterdir()))
    before_state = snapshot(store.state_path)
    before_lock = snapshot(store.lock_path)
    mutation_calls: list[str] = []

    def forbidden(self: StateStore, *_args: object, **_kwargs: object) -> NoReturn:
        del self
        mutation_calls.append("state-mutation")
        raise AssertionError("state mutation ran")

    for method_name in ("ensure_private_directory", "load", "publish", "remove_if_owned"):
        monkeypatch.setattr(StateStore, method_name, forbidden)
    capsys.readouterr()
    caplog.clear()
    owner: OwnerLock | None = None
    writer: StartupWriter | None = None
    try:
        try:
            returned_config, owner, writer = prepare_sidecar_child(arguments)
        except RuntimeError:
            owner_fd = -1
            writer_fd = -1
            raise
        owner_fd = -1
        writer_fd = -1
        assert returned_config is not config
        assert returned_config == config
        assert type(owner) is OwnerLock
        assert type(writer) is StartupWriter
        writer.close()
        writer = None
        owner.close()
        owner = None

        assert mutation_calls == []
        assert tuple(sorted(path.name for path in store.runtime_dir.iterdir())) == before_entries
        assert snapshot(store.state_path) == before_state
        assert snapshot(store.lock_path) == before_lock
        assert capsys.readouterr() == ("", "")
        assert caplog.records == []
        _assert_successor_can_acquire(store)
    finally:
        if writer is not None:
            writer.close()
        if owner is not None:
            owner.close()
        reader.close()
        _close_raw(handoff_fd)
        _close_raw(writer_fd)
        _close_raw(owner_fd)


def _call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return f"{function.value.id}.{function.attr}"
    return "<dynamic>"


def test_production_ast_enforces_only_the_composition_boundary() -> None:
    tree = ast.parse(Path(preparation_module.__file__).read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(node in tree.body for node in imports)
    assert not any(isinstance(node, ast.Import) for node in imports)
    import_records = {
        (
            node.level,
            node.module,
            tuple((alias.name, alias.asname) for alias in node.names),
        )
        for node in imports
        if isinstance(node, ast.ImportFrom)
    }
    assert import_records == {
        (0, "__future__", (("annotations", None),)),
        (0, "typing", (("Final", None), ("NoReturn", None))),
        (1, "child_adoption", (("adopt_sidecar_child_descriptors", None),)),
        (
            1,
            "child_bootstrap",
            (
                ("SidecarChildBootstrap", None),
                ("decode_sidecar_child_bootstrap", None),
            ),
        ),
        (1, "parent_handoff", (("await_parent_handoff", None),)),
        (1, "owner_lock", (("OwnerLock", None),)),
        (1, "runtime_config", (("SidecarRuntimeConfig", None),)),
        (1, "startup_channel", (("StartupWriter", None),)),
    }

    top_level_expressions = [node for node in tree.body if isinstance(node, ast.Expr)]
    assert len(top_level_expressions) == 1
    assert isinstance(top_level_expressions[0].value, ast.Constant)
    assert type(top_level_expressions[0].value.value) is str
    assert all(
        isinstance(
            node,
            (
                ast.Expr,
                ast.ImportFrom,
                ast.AnnAssign,
                ast.TypeAlias,
                ast.ClassDef,
                ast.FunctionDef,
            ),
        )
        for node in tree.body
    )

    captured_bindings = {
        node.target.id: node
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert set(captured_bindings) == {
        "_BOOTSTRAP_TYPE",
        "_CONFIG_TYPE",
        "_DECODE_BOOTSTRAP",
        "_AWAIT_PARENT_HANDOFF",
        "_ADOPT_DESCRIPTORS",
    }
    expected_binding_values = {
        "_BOOTSTRAP_TYPE": "SidecarChildBootstrap",
        "_CONFIG_TYPE": "SidecarRuntimeConfig",
        "_DECODE_BOOTSTRAP": "decode_sidecar_child_bootstrap",
        "_AWAIT_PARENT_HANDOFF": "await_parent_handoff",
        "_ADOPT_DESCRIPTORS": "adopt_sidecar_child_descriptors",
    }
    for name, node in captured_bindings.items():
        assert isinstance(node.annotation, ast.Name) and node.annotation.id == "Final"
        assert isinstance(node.value, ast.Name)
        assert node.value.id == expected_binding_values[name]
    type_aliases = [node for node in tree.body if isinstance(node, ast.TypeAlias)]
    assert len(type_aliases) == 1
    assert isinstance(type_aliases[0].name, ast.Name)
    assert type_aliases[0].name.id == "_PreparedChild"
    alias_value = type_aliases[0].value
    assert isinstance(alias_value, ast.Subscript)
    assert isinstance(alias_value.value, ast.Name) and alias_value.value.id == "tuple"
    assert isinstance(alias_value.slice, ast.Tuple)
    assert [element.id for element in alias_value.slice.elts if isinstance(element, ast.Name)] == [
        "SidecarRuntimeConfig",
        "OwnerLock",
        "StartupWriter",
    ]
    assert all(isinstance(element, ast.Name) for element in alias_value.slice.elts)
    assert not any(isinstance(node, ast.Assign) for node in tree.body)

    module_functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert set(module_functions) == {
        "_decode_bootstrap",
        "_decode_stage",
        "_await_handoff",
        "_prepare_child",
        "_raise_decode_failure",
        "_raise_adoption_failure",
        "prepare_sidecar_child",
    }
    module_classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert set(module_classes) == {
        "_DecodeStageFailure",
        "_HandoffStageFailure",
        "_AdoptionStageFailure",
    }
    assert all(
        len(node.bases) == 1
        and isinstance(node.bases[0], ast.Name)
        and node.bases[0].id == "Exception"
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Pass)
        and node.decorator_list == []
        and node.keywords == []
        for node in module_classes.values()
    )
    assert all(function.decorator_list == [] for function in module_functions.values())
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
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.Yield,
                ast.YieldFrom,
                ast.Await,
            ),
        )
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for function in module_functions.values()
        for node in ast.walk(function)
        if node is not function
    )
    assert not any(
        isinstance(node, (ast.Attribute, ast.Subscript))
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(tree)
    )
    calls_by_function = {
        name: sorted(_call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call))
        for name, function in module_functions.items()
    }
    assert calls_by_function == {
        "_decode_bootstrap": ["_DECODE_BOOTSTRAP"],
        "_decode_stage": sorted(
            [
                "_decode_bootstrap",
                "type",
                "object.__getattribute__",
                "type",
                "object.__getattribute__",
            ]
        ),
        "_await_handoff": ["_AWAIT_PARENT_HANDOFF"],
        "_prepare_child": sorted(["_decode_stage", "_await_handoff", "_ADOPT_DESCRIPTORS"]),
        "_raise_decode_failure": ["ValueError"],
        "_raise_adoption_failure": ["RuntimeError"],
        "prepare_sidecar_child": sorted(
            ["_prepare_child", "_raise_decode_failure", "_raise_adoption_failure"]
        ),
    }
    assert all("<dynamic>" not in calls for calls in calls_by_function.values())
    raises_by_function: dict[str, list[str]] = {}
    for name, function in module_functions.items():
        raised: list[str] = []
        for node in ast.walk(function):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            if isinstance(node.exc, ast.Name):
                raised.append(node.exc.id)
            elif isinstance(node.exc, ast.Call):
                raised.append(_call_name(node.exc))
            else:
                raised.append("<dynamic>")
        raises_by_function[name] = sorted(raised)
    assert raises_by_function == {
        "_decode_bootstrap": [],
        "_decode_stage": sorted(["ValueError", "ValueError", "ValueError", "_DecodeStageFailure"]),
        "_await_handoff": [],
        "_prepare_child": sorted(["_AdoptionStageFailure", "_HandoffStageFailure"]),
        "_raise_decode_failure": ["ValueError"],
        "_raise_adoption_failure": ["RuntimeError"],
        "prepare_sidecar_child": [],
    }
    assert all("<dynamic>" not in raised for raised in raises_by_function.values())

    decode_stage = module_functions["_decode_stage"]
    prepare_child = module_functions["_prepare_child"]
    decode_call = next(
        node
        for node in ast.walk(decode_stage)
        if isinstance(node, ast.Call) and _call_name(node) == "_decode_bootstrap"
    )
    config_reads = [
        node
        for node in ast.walk(decode_stage)
        if isinstance(node, ast.Call) and _call_name(node) == "object.__getattribute__"
    ]
    assert len(config_reads) == 2
    assert all(decode_call.lineno < node.lineno for node in config_reads)
    handoff_call = next(
        node
        for node in ast.walk(prepare_child)
        if isinstance(node, ast.Call) and _call_name(node) == "_await_handoff"
    )
    adoption_call = next(
        node
        for node in ast.walk(prepare_child)
        if isinstance(node, ast.Call) and _call_name(node) == "_ADOPT_DESCRIPTORS"
    )
    assert handoff_call.lineno < adoption_call.lineno

    handoff_try = next(
        node
        for node in ast.walk(prepare_child)
        if isinstance(node, ast.Try)
        and any(
            isinstance(candidate, ast.Call) and _call_name(candidate) == "_await_handoff"
            for candidate in ast.walk(node)
        )
    )
    assert len(handoff_try.body) == 1
    assert len(handoff_try.handlers) == 2
    assert handoff_try.orelse == []
    assert handoff_try.finalbody == []

    adopter_try = next(
        node
        for node in ast.walk(prepare_child)
        if isinstance(node, ast.Try)
        and any(
            isinstance(candidate, ast.Call) and _call_name(candidate) == "_ADOPT_DESCRIPTORS"
            for candidate in ast.walk(node)
        )
    )
    assert len(adopter_try.body) == 1
    assert len(adopter_try.handlers) == 2
    assert len(adopter_try.orelse) == 1
    assert isinstance(adopter_try.orelse[0], ast.Return)
    assert adopter_try.finalbody == []
    assert not any(isinstance(node, ast.Call) for node in ast.walk(adopter_try.orelse[0]))
    returned = adopter_try.orelse[0].value
    assert isinstance(returned, ast.Tuple)
    assert len(returned.elts) == 3
    assert isinstance(returned.elts[0], ast.Name) and returned.elts[0].id == "config"
    for element, expected_index in zip(returned.elts[1:], (0, 1), strict=True):
        assert isinstance(element, ast.Subscript)
        assert isinstance(element.value, ast.Name) and element.value.id == "pair"
        assert isinstance(element.slice, ast.Constant) and element.slice.value == expected_index
    assert not any(isinstance(node, ast.Starred) for node in ast.walk(prepare_child))

    handlers_by_function = {
        name: [node for node in ast.walk(function) if isinstance(node, ast.ExceptHandler)]
        for name, function in module_functions.items()
    }
    assert {name for name, handlers in handlers_by_function.items() if handlers} == {
        "_decode_stage",
        "_prepare_child",
        "prepare_sidecar_child",
    }
    decode_handlers = handlers_by_function["_decode_stage"]
    assert len(decode_handlers) == 1
    decode_handler = decode_handlers[0]
    assert isinstance(decode_handler.type, ast.Name) and decode_handler.type.id == "Exception"
    assert decode_handler.name is None
    assert len(decode_handler.body) == 1 and isinstance(decode_handler.body[0], ast.Pass)
    prepare_handlers = handlers_by_function["_prepare_child"]
    assert len(prepare_handlers) == 4
    assert [
        handler.type.id if isinstance(handler.type, ast.Name) else None
        for handler in prepare_handlers
    ] == ["Exception", "BaseException", "Exception", "BaseException"]
    assert all(handler.name is None for handler in prepare_handlers)
    assert all(len(handler.body) == 2 for handler in prepare_handlers)
    public_handlers = handlers_by_function["prepare_sidecar_child"]
    assert len(public_handlers) == 2
    assert isinstance(public_handlers[0].type, ast.Name)
    assert public_handlers[0].type.id == "_DecodeStageFailure"
    assert isinstance(public_handlers[1].type, ast.Tuple)
    assert [
        element.id for element in public_handlers[1].type.elts if isinstance(element, ast.Name)
    ] == [
        "_HandoffStageFailure",
        "_AdoptionStageFailure",
    ]
    for handler, expected_failure in zip(public_handlers, (1, 2), strict=True):
        assert handler.name is None
        assert len(handler.body) == 1 and isinstance(handler.body[0], ast.Assign)
        assignment = handler.body[0]
        assert len(assignment.targets) == 1
        assert isinstance(assignment.targets[0], ast.Name)
        assert assignment.targets[0].id == "failure"
        assert isinstance(assignment.value, ast.Constant)
        assert assignment.value.value == expected_failure


CHILD_SOURCE = r"""
import os
import signal
import sys
from pathlib import Path

from flowsight.sidecar import (
    OwnerLock,
    SidecarRuntimeConfig,
    StartupFailure,
    StartupFailureCode,
    StartupWriter,
    prepare_sidecar_child,
)
from flowsight.sidecar import child_preparation as preparation_module
from flowsight.sidecar import runtime_config as config_module

mode = sys.argv[1]
runtime_root = Path(sys.argv[2])
expected_origin = Path(sys.argv[3])
base = runtime_root.parent

checks = (
    sys.flags.isolated == 1,
    sys.flags.no_user_site == 1,
    os.environ.get("PYTHONPATH") == "",
    os.environ.get("PYTHONNOUSERSITE") == "1",
    os.environ.get("HOME") == str(base / "home"),
    os.environ.get("XDG_CACHE_HOME") == str(base / "xdg-cache"),
    os.environ.get("XDG_CONFIG_HOME") == str(base / "xdg-config"),
    os.environ.get("XDG_DATA_HOME") == str(base / "xdg-data"),
    os.environ.get("XDG_RUNTIME_DIR") == str(base / "xdg-runtime"),
    os.getcwd() == str(base / "child-cwd"),
    Path(preparation_module.__file__).resolve() == expected_origin,
    len(sys.argv[4:]) == 9,
)
if not all(checks):
    raise SystemExit(31)

config_module._USER_RUNTIME_PATH = lambda *_args, **_kwargs: runtime_root
arguments = tuple(sys.argv[4:])
try:
    config, owner, writer = prepare_sidecar_child(arguments)
except RuntimeError as error:
    if mode == "invalid" and type(error) is RuntimeError and str(error) == (
        "sidecar child descriptor adoption failed"
    ):
        raise SystemExit(23)
    raise SystemExit(32)
except BaseException:
    raise SystemExit(33)

if mode == "invalid":
    writer.close()
    owner.close()
    raise SystemExit(34)
if (
    type(config) is not SidecarRuntimeConfig
    or type(owner) is not OwnerLock
    or type(writer) is not StartupWriter
    or os.get_inheritable(owner.fileno())
    or os.get_inheritable(writer.fileno())
):
    writer.close()
    owner.close()
    raise SystemExit(35)
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
writer.send(StartupFailure(code=StartupFailureCode.SIDECAR_STARTUP_FAILED))
if sys.stdin.buffer.read(1) != b"x":
    owner.close()
    raise SystemExit(36)
owner.close()
raise SystemExit(0)
"""


def _isolated_environment(tmp_path: Path) -> dict[str, str]:
    selected = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("PYTHON")
        and name not in {"HOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
        and not name.startswith("XDG_")
    }
    directories = {
        "HOME": tmp_path / "home",
        "XDG_CACHE_HOME": tmp_path / "xdg-cache",
        "XDG_CONFIG_HOME": tmp_path / "xdg-config",
        "XDG_DATA_HOME": tmp_path / "xdg-data",
        "XDG_RUNTIME_DIR": tmp_path / "xdg-runtime",
    }
    for directory in directories.values():
        directory.mkdir()
    directories["XDG_RUNTIME_DIR"].chmod(0o700)
    selected.update({name: str(path) for name, path in directories.items()})
    selected.update(
        {
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
            "PWD": str(tmp_path / "child-cwd"),
        }
    )
    return selected


def _spawn_child(
    tmp_path: Path,
    *,
    mode: str,
    arguments: tuple[str, ...],
    pass_fds: tuple[int, ...],
) -> subprocess.Popen[bytes]:
    child_cwd = tmp_path / "child-cwd"
    child_cwd.mkdir(exist_ok=True)
    runtime_root = tmp_path / "runtime-root"
    expected_origin = Path(preparation_module.__file__).resolve()
    return subprocess.Popen(
        (
            sys.executable,
            "-I",
            "-c",
            CHILD_SOURCE,
            mode,
            str(runtime_root),
            str(expected_origin),
            *arguments,
        ),
        cwd=child_cwd,
        env=_isolated_environment(tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=pass_fds,
        start_new_session=True,
    )


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    active_error = sys.exception()
    cleanup_errors: list[BaseException] = []
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except BaseException as error:
                    cleanup_errors.append(error)
                try:
                    process.wait(timeout=2.0)
                except BaseException as error:
                    cleanup_errors.append(error)
            except BaseException as error:
                cleanup_errors.append(error)
    finally:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except BaseException as error:
                cleanup_errors.append(error)
    if process.poll() is None:
        cleanup_errors.append(AssertionError("child process was not reaped"))
    if cleanup_errors and active_error is None:
        raise cleanup_errors[0]


def _parent_exec_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[
    SidecarRuntimeConfig,
    StateStore,
    OwnerLock,
    StartupReader,
    StartupWriter,
    int,
    int,
]:
    config, store = _prepare(monkeypatch, tmp_path)
    owner = OwnerLock.acquire(store)
    try:
        reader, writer = open_startup_channel()
    except BaseException:
        owner.close()
        raise
    try:
        handoff_reader, handoff_writer = os.pipe()
    except BaseException:
        writer.close()
        reader.close()
        owner.close()
        raise
    return config, store, owner, reader, writer, handoff_reader, handoff_writer


def test_real_exec_child_alone_retains_the_owner_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, parent_owner, reader, parent_writer, handoff_reader, handoff_writer = (
        _parent_exec_resources(monkeypatch, tmp_path)
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        owner_metadata = os.fstat(parent_owner.fileno())
        arguments = encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=parent_owner.fileno(),
            startup_writer_fd=parent_writer.fileno(),
            parent_handoff_fd=handoff_reader,
        )
        process = _spawn_child(
            tmp_path,
            mode="success",
            arguments=arguments,
            pass_fds=(parent_owner.fileno(), parent_writer.fileno(), handoff_reader),
        )
        _close_raw(handoff_reader)
        handoff_reader = -1
        _close_raw(handoff_writer)
        handoff_writer = -1
        parent_writer.close()
        assert reader.receive(timeout=CHILD_TIMEOUT) == STARTUP_FAILURE
        parent_owner.close()

        with pytest.raises(OwnerLockError) as held:
            OwnerLock.acquire(store)
        assert held.value.code is OwnerLockErrorCode.OWNER_LOCK_HELD

        assert process.stdin is not None
        process.stdin.write(b"x")
        process.stdin.close()
        process.wait(timeout=CHILD_TIMEOUT)
        assert process.returncode == 0

        successor = OwnerLock.acquire(store)
        successor.close()
        lock_metadata = os.lstat(store.lock_path)
        assert (lock_metadata.st_dev, lock_metadata.st_ino) == (
            owner_metadata.st_dev,
            owner_metadata.st_ino,
        )
    finally:
        parent_writer.close()
        parent_owner.close()
        _close_raw(handoff_reader)
        _close_raw(handoff_writer)
        reader.close()
        if process is not None:
            _terminate_and_reap(process)


@pytest.mark.parametrize("case", ["swapped", "wrong-owner", "wrong-writer"])
def test_real_exec_invalid_pairs_fail_without_a_startup_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    config, store, parent_owner, reader, parent_writer, handoff_reader, handoff_writer = (
        _parent_exec_resources(monkeypatch, tmp_path)
    )
    sentinel_fd = -1
    process: subprocess.Popen[bytes] | None = None
    try:
        sentinel_path = tmp_path / "unrelated-descriptor"
        sentinel_fd = os.open(sentinel_path, os.O_CREAT | os.O_RDWR, 0o600)
        if case == "swapped":
            pair = (parent_writer.fileno(), parent_owner.fileno())
        elif case == "wrong-owner":
            pair = (sentinel_fd, parent_writer.fileno())
        else:
            pair = (parent_owner.fileno(), sentinel_fd)
        arguments = encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=pair[0],
            startup_writer_fd=pair[1],
            parent_handoff_fd=handoff_reader,
        )
        process = _spawn_child(
            tmp_path,
            mode="invalid",
            arguments=arguments,
            pass_fds=(*pair, handoff_reader),
        )
        _close_raw(handoff_reader)
        handoff_reader = -1
        _close_raw(handoff_writer)
        handoff_writer = -1
        parent_writer.close()
        with pytest.raises(StartupChannelError) as closed:
            reader.receive(timeout=CHILD_TIMEOUT)
        assert closed.value.code is StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED
        parent_owner.close()
        process.wait(timeout=CHILD_TIMEOUT)
        assert process.returncode == 23
        assert os.fstat(sentinel_fd).st_ino == os.lstat(sentinel_path).st_ino
        _assert_successor_can_acquire(store)
    finally:
        parent_writer.close()
        parent_owner.close()
        _close_raw(handoff_reader)
        _close_raw(handoff_writer)
        reader.close()
        _close_raw(sentinel_fd)
        if process is not None:
            _terminate_and_reap(process)


@pytest.mark.parametrize("injection", ["timeout", "assertion", "ordinary", "control"])
def test_real_exec_harness_always_performs_bounded_reaping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    injection: str,
) -> None:
    config, store, parent_owner, reader, parent_writer, handoff_reader, handoff_writer = (
        _parent_exec_resources(monkeypatch, tmp_path)
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        arguments = encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=parent_owner.fileno(),
            startup_writer_fd=parent_writer.fileno(),
            parent_handoff_fd=handoff_reader,
        )
        mode = "stubborn" if injection == "timeout" else "success"
        process = _spawn_child(
            tmp_path,
            mode=mode,
            arguments=arguments,
            pass_fds=(parent_owner.fileno(), parent_writer.fileno(), handoff_reader),
        )
        _close_raw(handoff_reader)
        handoff_reader = -1
        _close_raw(handoff_writer)
        handoff_writer = -1
        parent_writer.close()
        assert reader.receive(timeout=CHILD_TIMEOUT) == STARTUP_FAILURE
        parent_owner.close()

        if injection == "timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                try:
                    process.wait(timeout=0.01)
                finally:
                    _terminate_and_reap(process)
            assert process.returncode == -signal.SIGKILL
        else:
            if injection == "assertion":
                injected: BaseException = AssertionError("test assertion")
            elif injection == "ordinary":
                injected = RuntimeError("test ordinary failure")
            else:
                injected = _Control()
            with pytest.raises(type(injected)) as captured:
                try:
                    raise injected
                finally:
                    _terminate_and_reap(process)
            assert captured.value is injected

        assert process.poll() is not None
        assert process.stdin is not None and process.stdin.closed
        _assert_successor_can_acquire(store)
    finally:
        parent_writer.close()
        parent_owner.close()
        _close_raw(handoff_reader)
        _close_raw(handoff_writer)
        reader.close()
        if process is not None:
            _terminate_and_reap(process)
