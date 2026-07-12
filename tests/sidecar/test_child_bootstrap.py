from __future__ import annotations

import ast
import copy
import gc
import hashlib
import inspect
import os
import pickle
import traceback
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    CHILD_BOOTSTRAP_SCHEMA_VERSION,
    SidecarChildBootstrap,
    SidecarRuntimeConfig,
    decode_sidecar_child_bootstrap,
    encode_sidecar_child_bootstrap,
    prepare_sidecar_runtime_config,
)
from flowsight.sidecar import child_bootstrap as bootstrap_module
from flowsight.sidecar import runtime_config as config_module

INVALID_ERROR = "sidecar child bootstrap is invalid"
CONSTRUCTION_ERROR = "SidecarChildBootstrap must be decoded"
SERIALIZATION_ERROR = "SidecarChildBootstrap cannot be serialized"
IMMUTABILITY_ERROR = "SidecarChildBootstrap is immutable"
BOOTSTRAP_MARKER = "flowsight-sidecar-bootstrap-v2"
MAX_DESCRIPTOR = 2_147_483_647
EXPECTED_ARGUMENT_PREFIXES = (
    "",
    "project-root=",
    "runtime-root=",
    "project-id=",
    "requested-port=",
    "startup-timeout=",
    "owner-lock-fd=",
    "startup-writer-fd=",
    "parent-handoff-fd=",
)


class _Control(BaseException):
    pass


class _TupleSubclass(tuple[object, ...]):
    pass


class _TextSubclass(str):
    pass


class _IntSubclass(int):
    pass


class _BytesSubclass(bytes):
    pass


class _TrackedFailure(Exception):
    references: list[weakref.ReferenceType[_TrackedFailure]] = []

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.references.append(weakref.ref(self))


def _project(tmp_path: Path, name: str = "project") -> Path:
    result = tmp_path / name
    result.mkdir()
    return result


def _install_runtime_root(monkeypatch: pytest.MonkeyPatch, runtime_root: Path) -> None:
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    project_root: Path | None = None,
    runtime_root: Path | None = None,
    requested_port: int | None = None,
    startup_timeout: float = 5.0,
) -> SidecarRuntimeConfig:
    selected_project = _project(tmp_path) if project_root is None else project_root
    selected_runtime = tmp_path / "runtime-root" if runtime_root is None else runtime_root
    _install_runtime_root(monkeypatch, selected_runtime)
    return prepare_sidecar_runtime_config(
        selected_project,
        requested_port=requested_port,
        startup_timeout=startup_timeout,
    )


def _arguments(
    config: SidecarRuntimeConfig,
    *,
    owner_lock_fd: int = 3,
    startup_writer_fd: int = 4,
    parent_handoff_fd: int = 5,
) -> tuple[str, ...]:
    return encode_sidecar_child_bootstrap(
        config,
        owner_lock_fd=owner_lock_fd,
        startup_writer_fd=startup_writer_fd,
        parent_handoff_fd=parent_handoff_fd,
    )


def _replace(arguments: tuple[str, ...], index: int, value: object) -> tuple[object, ...]:
    mutable: list[object] = list(arguments)
    mutable[index] = value
    return tuple(mutable)


def _assert_fixed_error(error: BaseException, message: str = INVALID_ERROR) -> None:
    assert type(error) is ValueError
    assert str(error) == message
    assert error.args == (message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", []) == []


def _assert_hook_error(
    error: BaseException, expected_type: type[BaseException], message: str
) -> None:
    assert type(error) is expected_type
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", []) == []


def _production_frames(error: BaseException) -> list[tuple[str, dict[str, object]]]:
    production_path = Path(bootstrap_module.__file__).resolve()
    frames: list[tuple[str, dict[str, object]]] = []
    current = error.__traceback__
    while current is not None:
        if Path(current.tb_frame.f_code.co_filename).resolve() == production_path:
            frames.append((current.tb_frame.f_code.co_name, dict(current.tb_frame.f_locals)))
        current = current.tb_next
    return frames


def _assert_production_traceback_has_no_locals(
    error: BaseException,
    expected_functions: tuple[str, ...],
) -> None:
    frames = _production_frames(error)
    assert tuple(name for name, _locals in frames) == expected_functions
    assert all(local_values == {} for _name, local_values in frames)


def _expected_project_id(project_root: str) -> str:
    material = b"flowsight-project-v1\x00" + os.fsencode(project_root)
    return f"project-v1-{hashlib.sha256(material).hexdigest()}"


def _lstat_signature(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_atime_ns,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def test_public_exports_schema_and_signatures_are_exact() -> None:
    assert type(CHILD_BOOTSTRAP_SCHEMA_VERSION) is int
    assert CHILD_BOOTSTRAP_SCHEMA_VERSION == 2
    assert BOOTSTRAP_MARKER == "flowsight-sidecar-bootstrap-v2"
    assert sidecar_package.CHILD_BOOTSTRAP_SCHEMA_VERSION is CHILD_BOOTSTRAP_SCHEMA_VERSION
    assert sidecar_package.SidecarChildBootstrap is SidecarChildBootstrap
    assert sidecar_package.encode_sidecar_child_bootstrap is encode_sidecar_child_bootstrap
    assert sidecar_package.decode_sidecar_child_bootstrap is decode_sidecar_child_bootstrap
    for name in (
        "CHILD_BOOTSTRAP_SCHEMA_VERSION",
        "SidecarChildBootstrap",
        "encode_sidecar_child_bootstrap",
        "decode_sidecar_child_bootstrap",
    ):
        assert sidecar_package.__all__.count(name) == 1

    encode_signature = inspect.signature(encode_sidecar_child_bootstrap)
    assert tuple(encode_signature.parameters) == (
        "config",
        "owner_lock_fd",
        "startup_writer_fd",
        "parent_handoff_fd",
    )
    assert encode_signature.parameters["config"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert encode_signature.parameters["owner_lock_fd"].kind is inspect.Parameter.KEYWORD_ONLY
    assert encode_signature.parameters["startup_writer_fd"].kind is inspect.Parameter.KEYWORD_ONLY
    assert encode_signature.parameters["parent_handoff_fd"].kind is inspect.Parameter.KEYWORD_ONLY
    assert encode_signature.return_annotation == "tuple[str, ...]"

    decode_signature = inspect.signature(decode_sidecar_child_bootstrap)
    assert tuple(decode_signature.parameters) == ("arguments",)
    assert decode_signature.parameters["arguments"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert decode_signature.return_annotation == "SidecarChildBootstrap"


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: SidecarChildBootstrap(),
        lambda: SidecarChildBootstrap(object()),
        lambda: SidecarChildBootstrap(config=object()),
    ],
)
def test_direct_result_construction_is_rejected(invoke: Callable[[], object]) -> None:
    with pytest.raises(TypeError) as captured:
        invoke()
    _assert_hook_error(captured.value, TypeError, CONSTRUCTION_ERROR)


def test_canonical_known_vector_round_trips_a_new_exact_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    runtime_root = tmp_path / "private-runtime"
    _install_runtime_root(monkeypatch, runtime_root)
    config = prepare_sidecar_runtime_config(
        project,
        requested_port=0,
        startup_timeout=5.0,
    )
    canonical_project = str(project.resolve(strict=True))
    expected_project_id = _expected_project_id(canonical_project)
    expected = (
        BOOTSTRAP_MARKER,
        f"project-root={canonical_project}",
        f"runtime-root={runtime_root}",
        f"project-id={expected_project_id}",
        "requested-port=0",
        "startup-timeout=0x1.4000000000000p+2",
        "owner-lock-fd=3",
        "startup-writer-fd=2147483647",
        "parent-handoff-fd=4",
    )

    encoded = encode_sidecar_child_bootstrap(
        config,
        owner_lock_fd=3,
        startup_writer_fd=MAX_DESCRIPTOR,
        parent_handoff_fd=4,
    )
    assert type(encoded) is tuple
    assert encoded == expected
    assert all(type(argument) is str for argument in encoded)
    assert sum(len(os.fsencode(argument)) + 1 for argument in encoded) <= 9216

    decoded = decode_sidecar_child_bootstrap(encoded)
    assert type(decoded) is SidecarChildBootstrap
    assert repr(decoded) == "<SidecarChildBootstrap>"
    assert decoded.config is not config
    assert type(decoded.config) is SidecarRuntimeConfig
    assert decoded.config == config
    assert decoded.owner_lock_fd == 3
    assert decoded.startup_writer_fd == MAX_DESCRIPTOR
    assert decoded.parent_handoff_fd == 4
    assert not hasattr(decoded, "__dict__")


@pytest.mark.parametrize("requested_port", [None, 0, 1, 65_535])
@pytest.mark.parametrize(
    "startup_timeout",
    [float.fromhex("0x0.0000000000001p-1022"), 0.1, 5.0, 30.0],
)
def test_ports_and_canonical_float_hex_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested_port: int | None,
    startup_timeout: float,
) -> None:
    config = _prepare(
        monkeypatch,
        tmp_path,
        requested_port=requested_port,
        startup_timeout=startup_timeout,
    )
    arguments = _arguments(config)
    expected_port = "none" if requested_port is None else str(requested_port)
    assert arguments[4] == f"requested-port={expected_port}"
    assert arguments[5] == f"startup-timeout={startup_timeout.hex()}"
    decoded = decode_sidecar_child_bootstrap(arguments)
    assert decoded.config.requested_port is requested_port or (
        requested_port is not None and decoded.config.requested_port == requested_port
    )
    assert decoded.config.startup_timeout == startup_timeout


def test_alias_and_complex_path_values_use_canonical_prefix_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "project = 多=字节")
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    runtime_root = tmp_path / "runtime = 多=字节"
    config = _prepare(
        monkeypatch,
        tmp_path,
        project_root=alias,
        runtime_root=runtime_root,
    )
    arguments = _arguments(config)
    assert arguments[1] == f"project-root={project.resolve(strict=True)}"
    assert arguments[2] == f"runtime-root={runtime_root}"
    assert arguments[1].count("=") > 2
    assert arguments[2].count("=") > 2
    decoded = decode_sidecar_child_bootstrap(arguments)
    assert decoded.config.project_root == str(project.resolve(strict=True))
    assert decoded.config.runtime_root == str(runtime_root)


def test_supported_surrogateescape_path_round_trips_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    encoded_runtime = os.fsencode(tmp_path) + b"/runtime-\xff=raw"
    runtime_text = os.fsdecode(encoded_runtime)
    config = _prepare(monkeypatch, tmp_path, runtime_root=Path(runtime_text))
    arguments = _arguments(config)
    assert arguments[2] == f"runtime-root={runtime_text}"
    assert os.fsencode(arguments[2]).endswith(b"runtime-\xff=raw")
    decoded = decode_sidecar_child_bootstrap(arguments)
    assert decoded.config.runtime_root == runtime_text


def test_result_hooks_are_exact_and_retain_only_four_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = decode_sidecar_child_bootstrap(_arguments(_prepare(monkeypatch, tmp_path)))
    assert SidecarChildBootstrap.__slots__ == (
        "config",
        "owner_lock_fd",
        "startup_writer_fd",
        "parent_handoff_fd",
    )
    assert set(inspect.get_annotations(SidecarChildBootstrap)) == {
        "config",
        "owner_lock_fd",
        "startup_writer_fd",
        "parent_handoff_fd",
    }

    for operation in (
        lambda: copy.copy(result),
        lambda: copy.deepcopy(result),
        lambda: pickle.dumps(result),
        lambda: result.__reduce__(),
        lambda: result.__reduce_ex__(5),
        lambda: result.__getstate__(),
    ):
        with pytest.raises(TypeError) as captured:
            operation()
        _assert_hook_error(captured.value, TypeError, SERIALIZATION_ERROR)

    with pytest.raises(AttributeError) as set_error:
        result.owner_lock_fd = 99
    _assert_hook_error(set_error.value, AttributeError, IMMUTABILITY_ERROR)
    with pytest.raises(AttributeError) as delete_error:
        del result.owner_lock_fd
    _assert_hook_error(delete_error.value, AttributeError, IMMUTABILITY_ERROR)


def test_success_leaves_project_and_runtime_targets_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    runtime_root = tmp_path / "runtime-does-not-exist"
    before = _lstat_signature(project)
    config = _prepare(
        monkeypatch,
        tmp_path,
        project_root=project,
        runtime_root=runtime_root,
    )
    decoded = decode_sidecar_child_bootstrap(_arguments(config))
    assert type(decoded) is SidecarChildBootstrap
    assert _lstat_signature(project) == before
    assert not runtime_root.exists()


def test_encode_and_decode_each_call_canonical_factory_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    original = bootstrap_module._prepare_runtime_config
    calls: list[tuple[str, int | None, float]] = []

    def recording_factory(
        project_root: str,
        requested_port: int | None,
        startup_timeout: float,
    ) -> object:
        calls.append((project_root, requested_port, startup_timeout))
        return original(project_root, requested_port, startup_timeout)

    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", recording_factory)
    arguments = _arguments(config)
    assert calls == [(config.project_root, None, 5.0)]
    calls.clear()
    result = decode_sidecar_child_bootstrap(arguments)
    assert type(result) is SidecarChildBootstrap
    assert calls == [(config.project_root, None, 5.0)]


def test_captured_factory_and_fsencode_ignore_public_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("public replacement must not run")

    monkeypatch.setattr(sidecar_package, "prepare_sidecar_runtime_config", forbidden)
    monkeypatch.setattr(config_module, "prepare_sidecar_runtime_config", forbidden)
    monkeypatch.setattr(bootstrap_module, "prepare_sidecar_runtime_config", forbidden)
    monkeypatch.setattr(os, "fsencode", forbidden)
    arguments = _arguments(config)
    result = decode_sidecar_child_bootstrap(arguments)
    assert result.config == config


@pytest.mark.parametrize(
    ("owner_lock_fd", "startup_writer_fd"),
    [
        (True, 4),
        (_IntSubclass(3), 4),
        (None, 4),
        (object(), 4),
        (-1, 4),
        (0, 4),
        (1, 4),
        (2, 4),
        (MAX_DESCRIPTOR + 1, 4),
        (3, False),
        (3, _IntSubclass(4)),
        (3, None),
        (3, object()),
        (3, -1),
        (3, 0),
        (3, 1),
        (3, 2),
        (3, MAX_DESCRIPTOR + 1),
        (3, 3),
        (MAX_DESCRIPTOR, MAX_DESCRIPTOR),
    ],
)
def test_encode_rejects_inexact_invalid_or_equal_descriptors_before_config_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    owner_lock_fd: object,
    startup_writer_fd: object,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_read_config_slots",
        lambda _config: events.append("config-read"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: events.append("factory"),
    )

    with pytest.raises(ValueError) as captured:
        encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=owner_lock_fd,  # type: ignore[arg-type]
            startup_writer_fd=startup_writer_fd,  # type: ignore[arg-type]
            parent_handoff_fd=5,
        )

    _assert_fixed_error(captured.value)
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("encode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    assert events == []


@pytest.mark.parametrize(
    "parent_handoff_fd",
    [True, _IntSubclass(5), None, object(), -1, 0, 1, 2, MAX_DESCRIPTOR + 1, 3, 4],
)
def test_encode_rejects_invalid_or_non_distinct_parent_handoff_before_config_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parent_handoff_fd: object,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_read_config_slots",
        lambda _config: events.append("config-read"),
    )

    with pytest.raises(ValueError) as captured:
        encode_sidecar_child_bootstrap(
            config,
            owner_lock_fd=3,
            startup_writer_fd=4,
            parent_handoff_fd=parent_handoff_fd,  # type: ignore[arg-type]
        )

    _assert_fixed_error(captured.value)
    assert events == []


@pytest.mark.parametrize("wrong", [None, True, object()])
def test_encode_rejects_wrong_config_before_descriptor_or_factory_work(
    monkeypatch: pytest.MonkeyPatch,
    wrong: object,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_descriptor",
        lambda _value: events.append("descriptor"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: events.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        encode_sidecar_child_bootstrap(
            wrong,  # type: ignore[arg-type]
            owner_lock_fd=3,
            startup_writer_fd=4,
            parent_handoff_fd=5,
        )
    _assert_fixed_error(captured.value)
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("encode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    assert events == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_root", _TextSubclass("/private/project")),
        ("runtime_root", _TextSubclass("/private/runtime")),
        ("project_id", _TextSubclass("project-v1-" + "0" * 64)),
        ("requested_port", True),
        ("requested_port", _IntSubclass(4)),
        ("startup_timeout", 5),
    ],
)
def test_encode_rejects_forged_exact_config_slots_without_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    object.__setattr__(config, field, value)
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        _arguments(config)
    _assert_fixed_error(captured.value)
    assert calls == []


def test_encode_compares_exact_fields_without_config_equality_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path)

    def forbidden_eq(_self: object, _other: object) -> NoReturn:
        raise AssertionError("config equality must not run")

    monkeypatch.setattr(SidecarRuntimeConfig, "__eq__", forbidden_eq)
    arguments = _arguments(config)
    assert arguments[1] == f"project-root={config.project_root}"


@pytest.mark.parametrize(
    "field",
    [
        "project_root",
        "runtime_root",
        "project_id",
        "requested_port",
        "startup_timeout",
    ],
)
def test_encode_rejects_each_exact_field_mismatch_after_one_factory_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    original = bootstrap_module._prepare_runtime_config
    calls: list[tuple[str, int | None, float]] = []
    fsencode_calls: list[str] = []

    def mismatching_factory(
        project_root: str,
        requested_port: int | None,
        startup_timeout: float,
    ) -> object:
        calls.append((project_root, requested_port, startup_timeout))
        candidate = original(project_root, requested_port, startup_timeout)
        replacements: dict[str, object] = {
            "project_root": f"{config.project_root}-other",
            "runtime_root": f"{config.runtime_root}-other",
            "project_id": config.project_id[:-1] + ("0" if config.project_id[-1] != "0" else "1"),
            "requested_port": 1,
            "startup_timeout": 6.0,
        }
        object.__setattr__(candidate, field, replacements[field])
        return candidate

    def recording_fsencode(value: str) -> bytes:
        fsencode_calls.append(value)
        return os.fsencode(value)

    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", mismatching_factory)
    monkeypatch.setattr(bootstrap_module, "_fsencode", recording_fsencode)
    with pytest.raises(ValueError) as captured:
        _arguments(config)

    _assert_fixed_error(captured.value)
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("encode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    assert calls == [(config.project_root, None, 5.0)]
    assert fsencode_calls == []


def test_encode_ordinary_factory_failure_is_fixed_private_and_not_retained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    marker = "private-project-path-and-fd-9341"
    _TrackedFailure.references.clear()

    def fail_factory(*_args: object, **_kwargs: object) -> NoReturn:
        raise _TrackedFailure(marker)

    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", fail_factory)
    with pytest.raises(ValueError) as captured:
        _arguments(config, owner_lock_fd=9341, startup_writer_fd=9342)

    _assert_fixed_error(captured.value)
    assert marker not in "".join(traceback.format_exception(captured.value))
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("encode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    gc.collect()
    assert _TrackedFailure.references and _TrackedFailure.references[-1]() is None


def test_decode_rejects_inexact_container_arity_and_item_types_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    candidates: list[object] = [
        None,
        True,
        list(arguments),
        _TupleSubclass(arguments),
        arguments[:-1],
        (*arguments, "extra=value"),
        _replace(arguments, 1, _TextSubclass(arguments[1])),
        _replace(arguments, 2, b"runtime-root=/private/runtime"),
        _replace(arguments, 3, object()),
        _replace(arguments, 4, 0),
    ]
    candidates.extend(
        _replace(arguments, index, _TextSubclass(arguments[index])) for index in range(9)
    )
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )
    for candidate in candidates:
        with pytest.raises(ValueError) as captured:
            decode_sidecar_child_bootstrap(candidate)  # type: ignore[arg-type]
        _assert_fixed_error(captured.value)
        _assert_production_traceback_has_no_locals(
            captured.value,
            ("decode_sidecar_child_bootstrap", "_raise_invalid"),
        )
    assert calls == []


@pytest.mark.parametrize("index", range(9))
@pytest.mark.parametrize("control", ["\x00", "\n", "\x7f"])
def test_decode_rejects_control_text_at_every_position_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    index: int,
    control: str,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    malformed = _replace(arguments, index, f"{arguments[index]}{control}")
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )

    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(malformed)  # type: ignore[arg-type]

    _assert_fixed_error(captured.value)
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("decode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    assert calls == []


@pytest.mark.parametrize(
    ("index", "replacement"),
    [
        (0, "flowsight-sidecar-bootstrap-v1"),
        (0, ""),
        (1, "project-root="),
        (1, "runtime-root=/private/project"),
        (2, "runtime-root="),
        (2, "project-root=/private/runtime"),
        (3, "project-id="),
        (4, "startup-timeout=none"),
        (5, "requested-port=0x1.4000000000000p+2"),
        (6, "startup-writer-fd=3"),
        (7, "owner-lock-fd=4"),
        (1, "project-root=/private/project\x00suffix"),
        (2, "runtime-root=/private/runtime\nnext"),
        (3, "project-id=project-v1-" + "0" * 63 + "\x7f"),
    ],
)
def test_decode_rejects_marker_prefix_order_and_control_errors_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    index: int,
    replacement: str,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    malformed = _replace(arguments, index, replacement)
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(malformed)  # type: ignore[arg-type]
    _assert_fixed_error(captured.value)
    assert calls == []


@pytest.mark.parametrize(
    ("index", "prefix", "raw"),
    [
        (4, "requested-port=", ""),
        (4, "requested-port=", "None"),
        (4, "requested-port=", "none "),
        (4, "requested-port=", "00"),
        (4, "requested-port=", "01"),
        (4, "requested-port=", "+1"),
        (4, "requested-port=", "-1"),
        (4, "requested-port=", "１"),
        (4, "requested-port=", "65536"),
        (4, "requested-port=", "00000"),
        (5, "startup-timeout=", ""),
        (5, "startup-timeout=", "5.0"),
        (5, "startup-timeout=", " 0x1.4000000000000p+2"),
        (5, "startup-timeout=", "0x1.4000000000000p+2 "),
        (5, "startup-timeout=", "0X1.4000000000000P+2"),
        (5, "startup-timeout=", "0x1.4p+2"),
        (5, "startup-timeout=", "nan"),
        (5, "startup-timeout=", "inf"),
        (5, "startup-timeout=", "-0x0.0p+0"),
        (5, "startup-timeout=", "0x0.0p+0"),
        (5, "startup-timeout=", "0x1.f000000000000p+4"),
        (6, "owner-lock-fd=", ""),
        (6, "owner-lock-fd=", "03"),
        (6, "owner-lock-fd=", "+3"),
        (6, "owner-lock-fd=", "-3"),
        (6, "owner-lock-fd=", "３"),
        (6, "owner-lock-fd=", "2"),
        (6, "owner-lock-fd=", "2147483648"),
        (7, "startup-writer-fd=", ""),
        (7, "startup-writer-fd=", "04"),
        (7, "startup-writer-fd=", "+4"),
        (7, "startup-writer-fd=", "-4"),
        (7, "startup-writer-fd=", "４"),
        (7, "startup-writer-fd=", "2"),
        (7, "startup-writer-fd=", "2147483648"),
        (7, "startup-writer-fd=", "3"),
    ],
)
def test_decode_rejects_noncanonical_numeric_fields_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    index: int,
    prefix: str,
    raw: str,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    malformed = _replace(arguments, index, f"{prefix}{raw}")
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(malformed)  # type: ignore[arg-type]
    _assert_fixed_error(captured.value)
    assert calls == []


@pytest.mark.parametrize(
    "replacement",
    [
        "parent-handoff-fd=",
        "parent-handoff-fd=05",
        "parent-handoff-fd=+5",
        "parent-handoff-fd=2",
        "parent-handoff-fd=2147483648",
        "parent-handoff-fd=3",
        "parent-handoff-fd=4",
    ],
)
def test_decode_rejects_invalid_or_non_distinct_parent_handoff_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement: str,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )

    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(_replace(arguments, 8, replacement))  # type: ignore[arg-type]

    _assert_fixed_error(captured.value)
    assert calls == []


@pytest.mark.parametrize(
    ("index", "replacement"),
    [
        (3, "project-id=project-v1-" + "0" * 63),
        (3, "project-id=project-v1-" + "0" * 65),
        (3, "project-id=project-v1-" + "0" * 63 + "A"),
        (3, "project-id=project-v2-" + "0" * 64),
        (3, "project-id=project-v1-" + "g" * 64),
    ],
)
def test_decode_rejects_invalid_project_identity_grammar_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    index: int,
    replacement: str,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(_replace(arguments, index, replacement))  # type: ignore[arg-type]
    _assert_fixed_error(captured.value)
    assert calls == []


@pytest.mark.parametrize("proof_index", [2, 3])
def test_decode_rederives_and_rejects_changed_runtime_or_project_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    proof_index: int,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    arguments = _arguments(config)
    replacement = (
        f"runtime-root={tmp_path / 'other-runtime'}"
        if proof_index == 2
        else "project-id=project-v1-" + "0" * 64
    )
    original = bootstrap_module._prepare_runtime_config
    calls: list[tuple[str, int | None, float]] = []

    def recording_factory(
        project_root: str,
        requested_port: int | None,
        startup_timeout: float,
    ) -> object:
        calls.append((project_root, requested_port, startup_timeout))
        return original(project_root, requested_port, startup_timeout)

    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", recording_factory)
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(_replace(arguments, proof_index, replacement))  # type: ignore[arg-type]
    _assert_fixed_error(captured.value)
    assert calls == [(config.project_root, None, 5.0)]


def test_decode_rejects_noncanonical_project_alias_after_one_factory_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    config = _prepare(monkeypatch, tmp_path, project_root=project)
    arguments = _arguments(config)
    original = bootstrap_module._prepare_runtime_config
    calls: list[str] = []

    def recording_factory(
        project_root: str,
        requested_port: int | None,
        startup_timeout: float,
    ) -> object:
        calls.append(project_root)
        return original(project_root, requested_port, startup_timeout)

    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", recording_factory)
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(
            _replace(arguments, 1, f"project-root={alias}")  # type: ignore[arg-type]
        )
    _assert_fixed_error(captured.value)
    assert calls == [str(alias)]


def test_decode_missing_project_fails_but_same_path_replacement_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    config = _prepare(monkeypatch, tmp_path, project_root=project)
    arguments = _arguments(config)
    project.rmdir()
    with pytest.raises(ValueError) as missing:
        decode_sidecar_child_bootstrap(arguments)
    _assert_fixed_error(missing.value)

    project.mkdir()
    decoded = decode_sidecar_child_bootstrap(arguments)
    assert decoded.config.project_root == config.project_root


def test_decode_rejects_child_runtime_root_disagreement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path, runtime_root=tmp_path / "runtime-parent")
    arguments = _arguments(config)
    _install_runtime_root(monkeypatch, tmp_path / "runtime-child")
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(arguments)
    _assert_fixed_error(captured.value)


@pytest.mark.parametrize("target_bytes", [9216, 9217])
def test_decode_byte_budget_boundary_precedes_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_bytes: int,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    fsencode_calls: list[str] = []
    encoded_lengths: list[int] = []

    def sized_fsencode(value: str) -> bytes:
        fsencode_calls.append(value)
        encoded = b"x" * (target_bytes - 17) if len(fsencode_calls) == 1 else b"x"
        encoded_lengths.append(len(encoded))
        return encoded

    factory = bootstrap_module._prepare_runtime_config
    factory_calls: list[str] = []

    def recording_factory(
        project_root: str,
        requested_port: int | None,
        startup_timeout: float,
    ) -> object:
        factory_calls.append(project_root)
        return factory(project_root, requested_port, startup_timeout)

    monkeypatch.setattr(bootstrap_module, "_fsencode", sized_fsencode)
    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", recording_factory)
    if target_bytes == 9216:
        result = decode_sidecar_child_bootstrap(arguments)
        assert type(result) is SidecarChildBootstrap
        assert factory_calls == [result.config.project_root]
    else:
        with pytest.raises(ValueError) as captured:
            decode_sidecar_child_bootstrap(arguments)
        _assert_fixed_error(captured.value)
        assert factory_calls == []
    assert len(fsencode_calls) == 9
    assert all(length > 0 for length in encoded_lengths)
    assert sum(encoded_lengths) + len(encoded_lengths) == target_bytes


def test_real_over_budget_arguments_fail_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    oversized = _replace(arguments, 2, f"runtime-root=/{'x' * 9200}")
    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(oversized)  # type: ignore[arg-type]
    _assert_fixed_error(captured.value)
    assert calls == []


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        object(),
        bytearray(b"value"),
        memoryview(b"value"),
        _BytesSubclass(b"value"),
        b"value\x00suffix",
    ],
)
def test_decode_rejects_malformed_private_fsencode_results_before_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    encoded: object,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(bootstrap_module, "_fsencode", lambda _value: encoded)
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: calls.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(arguments)
    _assert_fixed_error(captured.value)
    assert calls == []


@pytest.mark.parametrize("occurrence", range(1, 9))
def test_fsencode_ordinary_failure_at_every_decode_argument_is_fixed_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    occurrence: int,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    original = bootstrap_module._fsencode
    calls = 0
    factory_calls: list[str] = []

    def fail_at(value: str) -> object:
        nonlocal calls
        calls += 1
        if calls == occurrence:
            raise OSError("private-fsencode-value")
        return original(value)

    monkeypatch.setattr(bootstrap_module, "_fsencode", fail_at)
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_runtime_config",
        lambda *_args: factory_calls.append("factory"),
    )
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(arguments)
    _assert_fixed_error(captured.value)
    assert calls == occurrence
    assert factory_calls == []


def test_decode_ordinary_factory_failure_is_fixed_private_and_not_retained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path), owner_lock_fd=8123)
    marker = "private-decoder-project-and-fd-8123"
    _TrackedFailure.references.clear()

    def fail_factory(*_args: object, **_kwargs: object) -> NoReturn:
        raise _TrackedFailure(marker)

    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", fail_factory)
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(arguments)

    _assert_fixed_error(captured.value)
    assert marker not in "".join(traceback.format_exception(captured.value))
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("decode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    gc.collect()
    assert _TrackedFailure.references and _TrackedFailure.references[-1]() is None


@pytest.mark.parametrize(
    "seam",
    ["_make_bootstrap", "_read_bootstrap_slots"],
)
def test_decode_result_construction_failures_are_fixed_and_retain_no_partial_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seam: str,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("private-result-construction")

    monkeypatch.setattr(bootstrap_module, seam, fail)
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(arguments)
    _assert_fixed_error(captured.value)
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("decode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    assert "private-result-construction" not in "".join(traceback.format_exception(captured.value))


def test_decode_releases_a_rejected_partial_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments = _arguments(_prepare(monkeypatch, tmp_path))
    reference: weakref.ReferenceType[object] | None = None

    class Candidate:
        pass

    def make_candidate(*_args: object) -> object:
        nonlocal reference
        candidate = Candidate()
        reference = weakref.ref(candidate)
        return candidate

    monkeypatch.setattr(bootstrap_module, "_make_bootstrap", make_candidate)
    with pytest.raises(ValueError) as captured:
        decode_sidecar_child_bootstrap(arguments)

    _assert_fixed_error(captured.value)
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("decode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    gc.collect()
    assert reference is not None
    assert reference() is None


@pytest.mark.parametrize("direction", ["encode", "decode"])
def test_fixed_failure_suppresses_but_does_not_inspect_caller_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    direction: str,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    arguments = _arguments(config)

    def fail_factory(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("internal-private-value")

    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", fail_factory)
    caller_error = RuntimeError("caller-private-context")
    try:
        raise caller_error
    except RuntimeError:
        with pytest.raises(ValueError) as captured:
            if direction == "encode":
                _arguments(config)
            else:
                decode_sidecar_child_bootstrap(arguments)

    assert captured.value.__context__ is caller_error
    assert captured.value.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(captured.value))
    assert "caller-private-context" not in rendered
    assert "internal-private-value" not in rendered


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, _Control])
@pytest.mark.parametrize("direction", ["encode", "decode"])
@pytest.mark.parametrize("seam", ["_prepare_runtime_config", "_fsencode"])
def test_process_control_from_captured_seams_preserves_identity_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    control_type: type[BaseException],
    direction: str,
    seam: str,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    arguments = _arguments(config)
    control = control_type("caller-owned-private-payload")
    events: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        events.append(seam)
        raise control

    monkeypatch.setattr(bootstrap_module, seam, fail)
    other_seam = "_fsencode" if seam == "_prepare_runtime_config" else "_prepare_runtime_config"
    original_other = getattr(bootstrap_module, other_seam)

    def record_other(*args: object, **kwargs: object) -> object:
        events.append(other_seam)
        return original_other(*args, **kwargs)

    monkeypatch.setattr(bootstrap_module, other_seam, record_other)
    with pytest.raises(control_type) as captured:
        if direction == "encode":
            _arguments(config)
        else:
            decode_sidecar_child_bootstrap(arguments)

    assert captured.value is control
    assert events.count(seam) == 1
    if direction == "encode" and seam == "_prepare_runtime_config":
        assert other_seam not in events
    if direction == "decode" and seam == "_fsencode":
        assert other_seam not in events
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, _Control])
@pytest.mark.parametrize(
    ("direction", "seam", "occurrence"),
    [
        ("encode", "_read_config_slots", 1),
        ("encode", "_read_config_slots", 2),
        ("decode", "_read_config_slots", 1),
        ("decode", "_make_bootstrap", 1),
        ("decode", "_read_bootstrap_slots", 1),
    ],
)
def test_process_control_from_every_object_operation_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    control_type: type[BaseException],
    direction: str,
    seam: str,
    occurrence: int,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    arguments = _arguments(config)
    control = control_type("caller-owned-object-operation")
    original = getattr(bootstrap_module, seam)
    calls = 0

    def fail_at(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == occurrence:
            raise control
        return original(*args, **kwargs)

    monkeypatch.setattr(bootstrap_module, seam, fail_at)
    with pytest.raises(control_type) as captured:
        if direction == "encode":
            _arguments(config)
        else:
            decode_sidecar_child_bootstrap(arguments)
    assert captured.value is control
    assert calls == occurrence


@pytest.mark.parametrize("occurrence", range(1, 9))
def test_fsencode_ordinary_failure_at_every_encode_argument_is_fixed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    occurrence: int,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    original = bootstrap_module._fsencode
    calls = 0

    def fail_at(value: str) -> object:
        nonlocal calls
        calls += 1
        if calls == occurrence:
            raise OSError("private-encoded-project")
        return original(value)

    monkeypatch.setattr(bootstrap_module, "_fsencode", fail_at)
    with pytest.raises(ValueError) as captured:
        _arguments(config)
    _assert_fixed_error(captured.value)
    _assert_production_traceback_has_no_locals(
        captured.value,
        ("encode_sidecar_child_bootstrap", "_raise_invalid"),
    )
    assert calls == occurrence


def test_encode_over_budget_fails_after_one_factory_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    fsencode_calls = 0

    def oversized_fsencode(_value: str) -> bytes:
        nonlocal fsencode_calls
        fsencode_calls += 1
        return b"x" * 9216

    original = bootstrap_module._prepare_runtime_config
    factory_calls: list[str] = []

    def recording_factory(
        project_root: str,
        requested_port: int | None,
        startup_timeout: float,
    ) -> object:
        factory_calls.append(project_root)
        return original(project_root, requested_port, startup_timeout)

    monkeypatch.setattr(bootstrap_module, "_fsencode", oversized_fsencode)
    monkeypatch.setattr(bootstrap_module, "_prepare_runtime_config", recording_factory)
    with pytest.raises(ValueError) as captured:
        _arguments(config)
    _assert_fixed_error(captured.value)
    assert factory_calls == [config.project_root]
    assert fsencode_calls == 1


def test_config_attribute_and_equality_overrides_cannot_redirect_snapshot_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path)

    def forbidden_getattribute(_self: object, name: str) -> NoReturn:
        raise AssertionError(f"dynamic attribute dispatch must not run: {name}")

    def forbidden_eq(_self: object, _other: object) -> NoReturn:
        raise AssertionError("config equality must not run")

    monkeypatch.setattr(SidecarRuntimeConfig, "__getattribute__", forbidden_getattribute)
    monkeypatch.setattr(SidecarRuntimeConfig, "__eq__", forbidden_eq)
    arguments = _arguments(config)
    decoded = decode_sidecar_child_bootstrap(arguments)
    assert object.__getattribute__(decoded.config, "project_root") == object.__getattribute__(
        config, "project_root"
    )


def test_codec_never_calls_descriptor_or_process_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., NoReturn]:
        def operation(*_args: object, **_kwargs: object) -> NoReturn:
            calls.append(name)
            raise AssertionError(f"forbidden operation ran: {name}")

        return operation

    for name in ("close", "dup", "fstat", "set_inheritable"):
        monkeypatch.setattr(os, name, forbidden(name))
    arguments = _arguments(
        config,
        owner_lock_fd=91,
        startup_writer_fd=92,
        parent_handoff_fd=93,
    )
    decoded = decode_sidecar_child_bootstrap(arguments)
    assert (decoded.owner_lock_fd, decoded.startup_writer_fd, decoded.parent_handoff_fd) == (
        91,
        92,
        93,
    )
    assert calls == []


def test_success_and_fixed_failure_emit_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _prepare(monkeypatch, tmp_path)
    arguments = _arguments(config)
    assert type(decode_sidecar_child_bootstrap(arguments)) is SidecarChildBootstrap
    with pytest.raises(ValueError):
        decode_sidecar_child_bootstrap(_replace(arguments, 7, "startup-writer-fd=3"))  # type: ignore[arg-type]
    assert capsys.readouterr() == ("", "")


def test_hook_failures_delete_caller_values_from_production_frames(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = decode_sidecar_child_bootstrap(_arguments(_prepare(monkeypatch, tmp_path)))
    secret = object()
    with pytest.raises(TypeError) as construction:
        SidecarChildBootstrap(secret)
    _assert_hook_error(construction.value, TypeError, CONSTRUCTION_ERROR)
    _assert_production_traceback_has_no_locals(construction.value, ("__new__",))

    with pytest.raises(AttributeError) as mutation:
        result.config = secret  # type: ignore[assignment]
    _assert_hook_error(mutation.value, AttributeError, IMMUTABILITY_ERROR)
    _assert_production_traceback_has_no_locals(mutation.value, ("__setattr__",))


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted_name(node.value)
        if owner is not None:
            return f"{owner}.{node.attr}"
    return None


def test_source_is_one_inert_codec_with_exact_dependency_and_call_allowlists() -> None:
    source_path = Path(bootstrap_module.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.lstrip(".").split(".", 1)[0])
    assert imported_roots == {"__future__", "os", "runtime_config", "typing"}
    assert imported_roots.isdisjoint(
        {
            "argparse",
            "asyncio",
            "fastapi",
            "fcntl",
            "json",
            "logging",
            "selectors",
            "socket",
            "sqlite3",
            "subprocess",
            "sys",
            "threading",
            "time",
            "uvicorn",
        }
    )

    call_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    call_names = [name for node in call_nodes if (name := _dotted_name(node.func)) is not None]
    assert len(call_names) == len(call_nodes)
    calls = set(call_names)
    assert calls == {
        "AttributeError",
        "TypeError",
        "ValueError",
        "_FSENCODE",
        "_PREPARE_SIDECAR_RUNTIME_CONFIG",
        "_argument_byte_lengths",
        "_decode_bootstrap",
        "_descriptor",
        "_encode_bootstrap",
        "_field",
        "_format_arguments",
        "_fsencode",
        "_make_bootstrap",
        "_parse_decimal",
        "_parse_port",
        "_parse_timeout",
        "_prepare_config_snapshot",
        "_prepare_runtime_config",
        "_raise_invalid",
        "_read_bootstrap_slots",
        "_read_config_slots",
        "_snapshot_config",
        "_snapshots_equal",
        "_valid_project_id",
        "_valid_wire_text",
        "all",
        "any",
        "cast",
        "float.fromhex",
        "float.hex",
        "frozenset",
        "int",
        "len",
        "lengths.append",
        "object.__getattribute__",
        "object.__new__",
        "object.__setattr__",
        "ord",
        "result.hex",
        "str",
        "tuple",
        "type",
    }
    assert {name: call_names.count(name) for name in calls} == {
        "AttributeError": 2,
        "TypeError": 7,
        "ValueError": 1,
        "_FSENCODE": 1,
        "_PREPARE_SIDECAR_RUNTIME_CONFIG": 1,
        "_argument_byte_lengths": 2,
        "_decode_bootstrap": 1,
        "_descriptor": 3,
        "_encode_bootstrap": 1,
        "_field": 8,
        "_format_arguments": 2,
        "_fsencode": 1,
        "_make_bootstrap": 1,
        "_parse_decimal": 4,
        "_parse_port": 1,
        "_parse_timeout": 1,
        "_prepare_config_snapshot": 2,
        "_prepare_runtime_config": 1,
        "_raise_invalid": 2,
        "_read_bootstrap_slots": 1,
        "_read_config_slots": 1,
        "_snapshot_config": 2,
        "_snapshots_equal": 2,
        "_valid_project_id": 1,
        "_valid_wire_text": 1,
        "all": 2,
        "any": 3,
        "cast": 3,
        "float.fromhex": 1,
        "float.hex": 1,
        "frozenset": 1,
        "int": 1,
        "len": 15,
        "lengths.append": 1,
        "object.__getattribute__": 9,
        "object.__new__": 1,
        "object.__setattr__": 4,
        "ord": 2,
        "result.hex": 1,
        "str": 2,
        "tuple": 1,
        "type": 19,
    }
    os_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    }
    assert os_attributes == {"fsencode"}

    top_level_definitions = [
        node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    ]
    assert top_level_definitions == [
        "_BootstrapFailure",
        "_fsencode",
        "_prepare_runtime_config",
        "_read_config_slots",
        "_make_bootstrap",
        "_read_bootstrap_slots",
        "SidecarChildBootstrap",
        "_raise_invalid",
        "_descriptor",
        "_snapshot_config",
        "_prepare_config_snapshot",
        "_snapshots_equal",
        "_format_arguments",
        "_argument_byte_lengths",
        "_encode_bootstrap",
        "_valid_wire_text",
        "_field",
        "_parse_decimal",
        "_parse_port",
        "_parse_timeout",
        "_valid_project_id",
        "_decode_bootstrap",
        "encode_sidecar_child_bootstrap",
        "decode_sidecar_child_bootstrap",
    ]
    assert sorted(
        node.name for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    ) == sorted(
        [
            *top_level_definitions,
            "__copy__",
            "__deepcopy__",
            "__delattr__",
            "__getstate__",
            "__init__",
            "__new__",
            "__reduce__",
            "__reduce_ex__",
            "__repr__",
            "__setattr__",
        ]
    )
    top_level_assignments = [
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert top_level_assignments == [
        "CHILD_BOOTSTRAP_SCHEMA_VERSION",
        "_BOOTSTRAP_MARKER",
        "_PROJECT_ROOT_PREFIX",
        "_RUNTIME_ROOT_PREFIX",
        "_PROJECT_ID_PREFIX",
        "_REQUESTED_PORT_PREFIX",
        "_STARTUP_TIMEOUT_PREFIX",
        "_OWNER_LOCK_FD_PREFIX",
        "_STARTUP_WRITER_FD_PREFIX",
        "_PARENT_HANDOFF_FD_PREFIX",
        "_ARGUMENT_COUNT",
        "_MAX_ARGUMENT_BYTES",
        "_MAX_PATH_CHARACTERS",
        "_MAX_PATH_BYTES",
        "_MAX_DESCRIPTOR",
        "_PROJECT_ID_VALUE_PREFIX",
        "_PROJECT_ID_HEX_CHARACTERS",
        "_CONFIG_TYPE",
        "_FSENCODE",
        "_PREPARE_SIDECAR_RUNTIME_CONFIG",
        "_BOOTSTRAP_TYPE",
    ]
    assert not any(isinstance(node, (ast.Assign, ast.AugAssign)) for node in tree.body)
    allowed_top_level_nodes = (
        ast.AnnAssign,
        ast.ClassDef,
        ast.Expr,
        ast.FunctionDef,
        ast.Import,
        ast.ImportFrom,
        ast.TypeAlias,
    )
    assert all(isinstance(node, allowed_top_level_nodes) for node in tree.body)
    module_expressions = [node for node in tree.body if isinstance(node, ast.Expr)]
    assert len(module_expressions) == 1
    assert isinstance(module_expressions[0].value, ast.Constant)
    assert type(module_expressions[0].value.value) is str
    assert not any(
        isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Store)
        for node in ast.walk(tree)
    )
    assignments = {
        node.target.id: node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }
    assert _dotted_name(assignments["_FSENCODE"]) == "os.fsencode"
    assert (
        _dotted_name(assignments["_PREPARE_SIDECAR_RUNTIME_CONFIG"])
        == "prepare_sidecar_runtime_config"
    )
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    assert all(not node.args.defaults for node in functions)
    assert all(all(default is None for default in node.args.kw_defaults) for node in functions)
    assert all(not node.finalbody for node in ast.walk(tree) if isinstance(node, ast.Try))

    result_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SidecarChildBootstrap"
    )
    assert [node.name for node in result_class.body if isinstance(node, ast.FunctionDef)] == [
        "__new__",
        "__init__",
        "__repr__",
        "__setattr__",
        "__delattr__",
        "__copy__",
        "__deepcopy__",
        "__reduce__",
        "__reduce_ex__",
        "__getstate__",
    ]
    assert [
        node.target.id
        for node in result_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ] == ["config", "owner_lock_fd", "startup_writer_fd", "parent_handoff_fd"]
    assert [
        target.id
        for node in result_class.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ] == ["__slots__"]
    failure_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_BootstrapFailure"
    )
    assert len(failure_class.body) == 1
    assert isinstance(failure_class.body[0], ast.Pass)
    assert not any(
        isinstance(
            node,
            (
                ast.AsyncFunctionDef,
                ast.Await,
                ast.Global,
                ast.Lambda,
                ast.Nonlocal,
                ast.TryStar,
                ast.With,
                ast.Yield,
                ast.YieldFrom,
            ),
        )
        for node in ast.walk(tree)
    )

    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert handlers
    assert all(handler.name is None for handler in handlers)
    assert all(
        isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
        for handler in handlers
    )
    assert all(
        not any(isinstance(child, (ast.Call, ast.Raise)) for child in ast.walk(handler))
        for handler in handlers
    )


def test_source_reads_and_writes_only_the_exact_reviewed_slots() -> None:
    tree = ast.parse(Path(bootstrap_module.__file__).read_text(encoding="utf-8"))
    getattribute_fields = [
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _dotted_name(node.func) == "object.__getattribute__"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
        and type(node.args[1].value) is str
    ]
    setattr_fields = [
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _dotted_name(node.func) == "object.__setattr__"
        and len(node.args) == 3
        and isinstance(node.args[1], ast.Constant)
        and type(node.args[1].value) is str
    ]
    assert getattribute_fields == [
        "project_root",
        "runtime_root",
        "project_id",
        "requested_port",
        "startup_timeout",
        "config",
        "owner_lock_fd",
        "startup_writer_fd",
        "parent_handoff_fd",
    ]
    assert setattr_fields == [
        "config",
        "owner_lock_fd",
        "startup_writer_fd",
        "parent_handoff_fd",
    ]


def test_source_does_not_reenter_public_encoder_from_decoder() -> None:
    tree = ast.parse(Path(bootstrap_module.__file__).read_text(encoding="utf-8"))
    decode = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_decode_bootstrap"
    )
    called_names = {
        name
        for node in ast.walk(decode)
        if isinstance(node, ast.Call) and (name := _dotted_name(node.func)) is not None
    }
    assert "encode_sidecar_child_bootstrap" not in called_names
    assert "_encode_bootstrap" not in called_names
