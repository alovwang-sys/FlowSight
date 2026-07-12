from __future__ import annotations

import ast
import copy
import gc
import hashlib
import inspect
import math
import os
import pickle
import re
import sys
import tomllib
import traceback
import weakref
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, get_type_hints

import platformdirs
import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    SidecarRuntimeConfig,
    child_entry,
    parent_handoff,
    prepare_sidecar_runtime_config,
)
from flowsight.sidecar import runtime_config as config_module

PROJECT_TYPE_ERROR = "project_root must be an exact built-in str or platform Path"
PROJECT_VALUE_ERROR = "project_root is invalid"
PORT_ERROR = "requested_port must be None or an exact built-in int in 0..65535"
TIMEOUT_TYPE_ERROR = "startup_timeout must be a built-in int or float"
TIMEOUT_VALUE_ERROR = "startup_timeout must be finite, positive, and at most 30 seconds"
RUNTIME_ERROR = "sidecar runtime configuration failed"

EXPECTED_USR_PROJECT_ID = (
    "project-v1-cde940fec370737ffb974f44f2a1677b83e0df97fce4afcba8a4f3a1ff9c3bd3"
)
EXPECTED_FIXED_PROJECT_ID = (
    "project-v1-7deba48c3d385cbdb47973f30cdc2a60f0ea6c3c208f056e5d54e87fbd7ad343"
)
PLATFORM_PATH_TYPE = type(Path())
OPERATION_SEQUENCE = (
    "_fspath",
    "_fsencode",
    "_resolve_project_root",
    "_fsencode",
    "_is_absolute",
    "_dirname",
    "_read_project_root_stat",
    "_is_directory",
    "_derive_project_digest",
    "_query_user_runtime_path",
    "_fspath",
    "_fsencode",
    "_is_absolute",
    "_dirname",
    "_construct_state_store",
    "_read_state_store_fields",
    "_fspath",
    "_fsencode",
    "_is_absolute",
    "_dirname",
    "_make_config",
    "_read_config_slots",
)
OPERATION_POINTS = tuple(
    (name, OPERATION_SEQUENCE[: index + 1].count(name))
    for index, name in enumerate(OPERATION_SEQUENCE)
)
EXPECTED_SIDECAR_EXPORTS = (
    "LOOPBACK_HOST",
    "MAX_PRIVATE_BODY_BYTES",
    "MAX_STARTUP_SIGNAL_BYTES",
    "MAX_STATE_BYTES",
    "CHILD_BOOTSTRAP_SCHEMA_VERSION",
    "DEFAULT_SIDECAR_PORT",
    "ListenerBindError",
    "ListenerErrorCode",
    "OwnerLock",
    "OwnerLockError",
    "OwnerLockErrorCode",
    "SidecarChildBootstrap",
    "SidecarRuntimeConfig",
    "OwnerElectionError",
    "OwnerElectionErrorCode",
    "PROTOCOL_VERSION",
    "STATE_SCHEMA_VERSION",
    "STARTUP_CHANNEL_SCHEMA_VERSION",
    "InvalidStateError",
    "SidecarState",
    "StateBusyError",
    "StateStorageError",
    "StateStore",
    "StartupChannelError",
    "StartupChannelErrorCode",
    "StartupAdmissionError",
    "StartupAdmissionErrorCode",
    "StartupFailure",
    "StartupFailureCode",
    "StartupReader",
    "StartupReady",
    "StartupStateError",
    "StartupStateErrorCode",
    "StartupWriter",
    "admit_configured_incumbent_port",
    "adopt_sidecar_child_descriptors",
    "bind_loopback_listener",
    "create_sidecar_app",
    "create_startup_state",
    "decode_sidecar_child_bootstrap",
    "discover_existing_startup",
    "encode_sidecar_child_bootstrap",
    "open_startup_channel",
    "probe_sidecar_health",
    "prepare_sidecar_child",
    "prepare_sidecar_runtime_config",
    "receive_startup_outcome",
    "resolve_owner_election",
    "run_sidecar_child",
    "serve_owned_prebound_sidecar_app",
    "serve_prebound_sidecar_app",
    "verify_ready_startup",
    "wait_for_owner_election",
)
EXPECTED_SIDECAR_SUBMODULES = {
    "app",
    "child_adoption",
    "child_bootstrap",
    "child_entry",
    "child_preparation",
    "child_runtime",
    "health",
    "incumbent_port",
    "listener",
    "owner_lock",
    "parent_handoff",
    "runtime_config",
    "server_runtime",
    "startup_admission",
    "startup_channel",
    "startup_discovery",
    "startup_election",
    "startup_state",
    "startup_verification",
    "startup_wait",
    "state",
}


class _TextSubclass(str):
    pass


class _BytesSubclass(bytes):
    pass


class _IntSubclass(int):
    pass


class _FloatSubclass(float):
    pass


class _PathSubclass(PLATFORM_PATH_TYPE):
    pass


class _DuckPath:
    def __fspath__(self) -> str:
        raise AssertionError("duck path protocol must not run")


class _Control(BaseException):
    pass


class _TrackedFailure(Exception):
    references: list[weakref.ReferenceType[_TrackedFailure]] = []

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.references.append(weakref.ref(self))


def _assert_exact_error(
    error: BaseException,
    expected_type: type[BaseException],
    message: str,
) -> None:
    assert type(error) is expected_type
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert getattr(error, "__notes__", []) == []


def _project(tmp_path: Path, name: str = "project") -> Path:
    project_root = tmp_path / name
    project_root.mkdir()
    return project_root


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
        metadata.st_blocks,
    )


def _install_runtime_provider(
    monkeypatch: pytest.MonkeyPatch,
    runtime_root: Path,
) -> list[tuple[str, object, object]]:
    calls: list[tuple[str, object, object]] = []

    def provider(
        appname: str,
        appauthor: bool,
        ensure_exists: bool,
    ) -> Path:
        assert type(appname) is str and appname == "flowsight"
        assert type(appauthor) is bool and appauthor is False
        assert type(ensure_exists) is bool and ensure_exists is False
        calls.append((appname, appauthor, ensure_exists))
        return runtime_root

    monkeypatch.setattr(config_module, "_USER_RUNTIME_PATH", provider)
    return calls


def _record_invalid_prefix_later_work(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    events: list[str] = []

    def record(name: str) -> Callable[..., object]:
        def operation(*_args: object, **_kwargs: object) -> object:
            events.append(name)
            return object()

        return operation

    for name in (
        "_resolve_project_root",
        "_derive_project_digest",
        "_query_user_runtime_path",
        "_construct_state_store",
        "_make_config",
    ):
        monkeypatch.setattr(config_module, name, record(name))
    return events


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    project_root: str | Path | None = None,
    runtime_root: Path | None = None,
    requested_port: int | None = None,
    startup_timeout: float = 5.0,
) -> tuple[SidecarRuntimeConfig, Path, list[tuple[str, object, object]]]:
    selected_project = _project(tmp_path) if project_root is None else project_root
    selected_runtime = tmp_path / "runtime-root" if runtime_root is None else runtime_root
    calls = _install_runtime_provider(monkeypatch, selected_runtime)
    result = prepare_sidecar_runtime_config(
        selected_project,
        requested_port=requested_port,
        startup_timeout=startup_timeout,
    )
    return result, selected_runtime, calls


def test_public_exports_and_signature_are_exact() -> None:
    assert sidecar_package.SidecarRuntimeConfig is SidecarRuntimeConfig
    assert sidecar_package.child_entry is child_entry
    assert sidecar_package.parent_handoff is parent_handoff
    assert sidecar_package.prepare_sidecar_runtime_config is prepare_sidecar_runtime_config
    assert sidecar_package.__all__.count("SidecarRuntimeConfig") == 1
    assert sidecar_package.__all__.count("prepare_sidecar_runtime_config") == 1
    assert tuple(sidecar_package.__all__) == EXPECTED_SIDECAR_EXPORTS
    assert {name for name in vars(sidecar_package) if not name.startswith("_")} == set(
        EXPECTED_SIDECAR_EXPORTS
    ) | EXPECTED_SIDECAR_SUBMODULES
    assert [
        name
        for name in sidecar_package.__all__
        if getattr(sidecar_package, name) is SidecarRuntimeConfig
    ] == ["SidecarRuntimeConfig"]
    assert [
        name
        for name in sidecar_package.__all__
        if getattr(sidecar_package, name) is prepare_sidecar_runtime_config
    ] == ["prepare_sidecar_runtime_config"]
    assert [
        name
        for name, value in vars(sidecar_package).items()
        if not name.startswith("_") and value is SidecarRuntimeConfig
    ] == ["SidecarRuntimeConfig"]
    assert [
        name
        for name, value in vars(sidecar_package).items()
        if not name.startswith("_") and value is prepare_sidecar_runtime_config
    ] == ["prepare_sidecar_runtime_config"]

    signature = inspect.signature(prepare_sidecar_runtime_config)
    assert tuple(signature.parameters) == (
        "project_root",
        "requested_port",
        "startup_timeout",
    )
    assert signature.parameters["project_root"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["requested_port"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["requested_port"].default is None
    assert signature.parameters["startup_timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["startup_timeout"].default == 5.0
    hints = get_type_hints(prepare_sidecar_runtime_config)
    assert hints == {
        "project_root": str | Path,
        "requested_port": int | None,
        "startup_timeout": float,
        "return": SidecarRuntimeConfig,
    }


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ((), {}),
        (("project",), {}),
        ((), {"project_root": "project"}),
        ((1, 2, 3, 4, 5), {}),
        ((), {"unexpected": object()}),
        ((), {"cls": object()}),
        ((), {"self": object()}),
        ((object(),), {"self": object()}),
    ],
)
def test_direct_construction_always_raises_fixed_error(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(TypeError) as captured:
        SidecarRuntimeConfig(*args, **kwargs)
    _assert_exact_error(
        captured.value,
        TypeError,
        "SidecarRuntimeConfig must be prepared",
    )
    with pytest.raises(TypeError) as direct_new_captured:
        SidecarRuntimeConfig.__new__(SidecarRuntimeConfig, *args, **kwargs)
    _assert_exact_error(
        direct_new_captured.value,
        TypeError,
        "SidecarRuntimeConfig must be prepared",
    )


def test_config_is_frozen_slot_only_value_with_private_repr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, runtime_root, calls = _prepare(
        monkeypatch,
        tmp_path,
        requested_port=0,
        startup_timeout=7,
    )
    duplicate = prepare_sidecar_runtime_config(
        config.project_root,
        requested_port=0,
        startup_timeout=7.0,
    )
    default_config = prepare_sidecar_runtime_config(config.project_root)

    assert type(config) is SidecarRuntimeConfig
    assert SidecarRuntimeConfig.__annotations__ == {
        "project_root": "str",
        "runtime_root": "str",
        "project_id": "str",
        "requested_port": "int | None",
        "startup_timeout": "float",
    }
    assert SidecarRuntimeConfig.__slots__ == (
        "project_root",
        "runtime_root",
        "project_id",
        "requested_port",
        "startup_timeout",
    )
    assert not hasattr(config, "__dict__")
    assert not hasattr(config, "__weakref__")
    assert "__dataclass_fields__" not in SidecarRuntimeConfig.__dict__
    assert "__dataclass_params__" not in SidecarRuntimeConfig.__dict__
    for generated_or_alternate_method in (
        "__replace__",
        "__setstate__",
        "to_wire",
        "from_wire",
    ):
        assert generated_or_alternate_method not in SidecarRuntimeConfig.__dict__
    assert "__reduce__" in SidecarRuntimeConfig.__dict__
    assert "__reduce_ex__" in SidecarRuntimeConfig.__dict__
    assert "__getstate__" in SidecarRuntimeConfig.__dict__
    assert repr(config) == "<SidecarRuntimeConfig>"
    assert repr(default_config) == "<SidecarRuntimeConfig>"
    assert config == duplicate
    assert hash(config) == hash(duplicate)
    assert config.requested_port == 0
    assert type(config.startup_timeout) is float and config.startup_timeout == 7.0
    assert config.runtime_root == str(runtime_root)
    assert calls == [
        ("flowsight", False, False),
        ("flowsight", False, False),
        ("flowsight", False, False),
    ]
    with pytest.raises(AttributeError, match="^SidecarRuntimeConfig is immutable$"):
        config.project_root = "replacement"
    with pytest.raises(AttributeError, match="^SidecarRuntimeConfig is immutable$"):
        del config.project_root
    with pytest.raises(TypeError):
        weakref.ref(config)


def test_each_config_field_participates_in_value_equality(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _runtime_root, _calls = _prepare(monkeypatch, tmp_path)
    original = {
        field_name: object.__getattribute__(config, field_name)
        for field_name in SidecarRuntimeConfig.__slots__
    }
    replacements: dict[str, object] = {
        "project_root": f"{config.project_root}-different",
        "runtime_root": f"{config.runtime_root}-different",
        "project_id": "project-v1-" + "0" * 64,
        "requested_port": 0,
        "startup_timeout": 6.0,
    }

    for changed_field, replacement in replacements.items():
        variant = object.__new__(config_module._CONFIG_TYPE)
        for field_name, value in original.items():
            object.__setattr__(
                variant,
                field_name,
                replacement if field_name == changed_field else value,
            )
        assert config != variant
        assert variant != config

    assert config.__eq__(object()) is NotImplemented


@pytest.mark.parametrize(
    "operation",
    [
        copy.copy,
        copy.deepcopy,
        pickle.dumps,
        lambda value: value.__reduce__(),
        lambda value: value.__reduce_ex__(pickle.HIGHEST_PROTOCOL),
        lambda value: value.__getstate__(),
    ],
    ids=[
        "copy",
        "deepcopy",
        "pickle-dumps",
        "direct-reduce",
        "direct-reduce-ex",
        "direct-getstate",
    ],
)
def test_copy_and_pickle_are_rejected_before_exposing_config_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: Callable[[object], object],
) -> None:
    config, _runtime_root, _calls = _prepare(monkeypatch, tmp_path)

    with pytest.raises(TypeError) as captured:
        operation(config)
    _assert_exact_error(
        captured.value,
        TypeError,
        "SidecarRuntimeConfig cannot be serialized",
    )
    assert config.project_root not in str(captured.value)
    assert config.runtime_root not in str(captured.value)


def test_dependency_pin_is_one_exact_runtime_requirement() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_requirements = metadata["project"]["dependencies"]

    def distribution_name(requirement: str) -> str:
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        assert match is not None
        return re.sub(r"[-_.]+", "-", match.group(1)).lower()

    platformdirs_requirements = [
        requirement
        for requirement in runtime_requirements
        if distribution_name(requirement) == "platformdirs"
    ]
    assert platformdirs_requirements == ["platformdirs==4.10.0"]
    assert ";" not in platformdirs_requirements[0]
    assert all(
        distribution_name(requirement) != "platformdirs"
        for requirements in metadata["project"]["optional-dependencies"].values()
        for requirement in requirements
    )


def test_relative_absolute_and_symlink_aliases_have_one_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(project_root, target_is_directory=True)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)
    monkeypatch.chdir(tmp_path)

    relative = prepare_sidecar_runtime_config("project")
    absolute = prepare_sidecar_runtime_config(project_root)
    linked = prepare_sidecar_runtime_config(alias)

    assert relative == absolute == linked
    assert relative.project_root == str(project_root.resolve(strict=True))
    assert type(relative.project_root) is str
    assert type(relative.runtime_root) is str
    assert type(relative.project_id) is str
    assert relative.project_id.startswith("project-v1-")
    assert len(relative.project_id) == len("project-v1-") + 64
    retained_scalars = (
        linked.project_root,
        linked.runtime_root,
        linked.project_id,
        linked.requested_port,
        linked.startup_timeout,
    )
    replacement = _project(tmp_path, "replacement")
    alias.unlink()
    alias.symlink_to(replacement, target_is_directory=True)
    assert (
        linked.project_root,
        linked.runtime_root,
        linked.project_id,
        linked.requested_port,
        linked.startup_timeout,
    ) == retained_scalars


def test_project_resolution_uses_exact_strict_true_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)
    real_realpath = config_module._REALPATH
    calls: list[tuple[str, bool]] = []

    def strict_realpath(value: str, *, strict: bool) -> str:
        assert type(value) is str and value == str(project_root)
        assert type(strict) is bool and strict is True
        calls.append((value, strict))
        return real_realpath(value, strict=strict)

    monkeypatch.setattr(config_module, "_REALPATH", strict_realpath)

    config = prepare_sidecar_runtime_config(project_root)

    assert type(config) is SidecarRuntimeConfig
    assert calls == [(str(project_root), True)]


def test_distinct_project_roots_have_distinct_fixed_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _project(tmp_path, "first")
    second = _project(tmp_path, "second")
    _install_runtime_provider(monkeypatch, tmp_path / "runtime-root")

    first_config = prepare_sidecar_runtime_config(first)
    second_config = prepare_sidecar_runtime_config(second)

    for root, config in ((first, first_config), (second, second_config)):
        canonical_root = str(root.resolve(strict=True))
        expected_digest = hashlib.sha256(
            b"flowsight-project-v1\x00" + os.fsencode(canonical_root)
        ).hexdigest()
        assert config.project_id == f"project-v1-{expected_digest}"
    assert first_config.project_id != second_config.project_id
    assert first_config.project_root != second_config.project_root


def test_usr_project_id_matches_independent_literal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_runtime_provider(monkeypatch, tmp_path / "runtime-root")

    config = prepare_sidecar_runtime_config("/usr")

    assert config.project_root == "/usr"
    assert config.project_id == EXPECTED_USR_PROJECT_ID


def test_second_fixed_material_has_independent_project_id_literal() -> None:
    assert config_module._derive_project_id(b"/fixed/project") == EXPECTED_FIXED_PROJECT_ID


@pytest.mark.parametrize(
    "invalid_root",
    [None, True, 1, 1.0, _TextSubclass("project"), _PathSubclass("/tmp"), _DuckPath()],
)
def test_project_root_rejects_every_inexact_top_level_type_before_later_work(
    monkeypatch: pytest.MonkeyPatch,
    invalid_root: object,
) -> None:
    events = _record_invalid_prefix_later_work(monkeypatch)

    with pytest.raises(TypeError) as captured:
        prepare_sidecar_runtime_config(invalid_root)  # type: ignore[arg-type]

    _assert_exact_error(captured.value, TypeError, PROJECT_TYPE_ERROR)
    assert events == []


@pytest.mark.parametrize(
    "invalid_port",
    [True, False, -1, 65_536, 1.0, "4040", _IntSubclass(4040)],
)
def test_requested_port_rejects_invalid_values_before_timeout_or_path(
    monkeypatch: pytest.MonkeyPatch,
    invalid_port: object,
) -> None:
    events = _record_invalid_prefix_later_work(monkeypatch)

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(
            "missing-project",
            requested_port=invalid_port,  # type: ignore[arg-type]
            startup_timeout=object(),  # type: ignore[arg-type]
        )

    _assert_exact_error(captured.value, ValueError, PORT_ERROR)
    assert events == []


@pytest.mark.parametrize("port", [None, 0, 1, 65_535])
def test_requested_port_accepts_exact_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    port: int | None,
) -> None:
    config, _runtime_root, _calls = _prepare(
        monkeypatch,
        tmp_path,
        requested_port=port,
    )
    assert config.requested_port == port
    assert type(config.requested_port) is type(port)


@pytest.mark.parametrize(
    "invalid_timeout",
    [None, True, False, "5", object(), _IntSubclass(5), _FloatSubclass(5.0)],
)
def test_startup_timeout_rejects_inexact_types_before_path_work(
    monkeypatch: pytest.MonkeyPatch,
    invalid_timeout: object,
) -> None:
    events = _record_invalid_prefix_later_work(monkeypatch)

    with pytest.raises(TypeError) as captured:
        prepare_sidecar_runtime_config(
            "missing-project",
            startup_timeout=invalid_timeout,  # type: ignore[arg-type]
        )

    _assert_exact_error(captured.value, TypeError, TIMEOUT_TYPE_ERROR)
    assert events == []


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -1, 31, 10**1_000, 0.0, -0.0, -1.0, 30.000_001, math.inf, -math.inf, math.nan],
)
def test_startup_timeout_rejects_invalid_exact_numbers_without_overflow(
    monkeypatch: pytest.MonkeyPatch,
    invalid_timeout: int | float,
) -> None:
    events = _record_invalid_prefix_later_work(monkeypatch)

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(
            "missing-project",
            startup_timeout=invalid_timeout,
        )

    _assert_exact_error(captured.value, ValueError, TIMEOUT_VALUE_ERROR)
    assert events == []


@pytest.mark.parametrize("timeout", [1, 30, 5e-324, 0.001, 1.0, 30.0])
def test_startup_timeout_accepts_exact_boundaries_as_float(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    timeout: int | float,
) -> None:
    config, _runtime_root, _calls = _prepare(
        monkeypatch,
        tmp_path,
        startup_timeout=timeout,
    )
    assert type(config.startup_timeout) is float
    assert config.startup_timeout == float(timeout)


@pytest.mark.parametrize(
    "invalid_text",
    [
        "",
        "bad\x00root",
        "bad\nroot",
        "bad\x1froot",
        "bad\x7froot",
        "x" * 4097,
        "é" * 3000,
    ],
)
def test_project_root_rejects_invalid_text_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
    invalid_text: str,
) -> None:
    events = _record_invalid_prefix_later_work(monkeypatch)

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(invalid_text)

    _assert_exact_error(captured.value, ValueError, PROJECT_VALUE_ERROR)
    assert events == []


@pytest.mark.parametrize(
    "malformed_canonical",
    [
        object(),
        _TextSubclass("/tmp/project"),
        "relative-project",
        "/",
        "/tmp/../project",
        "/tmp/private\nproject",
        "/" + "x" * 4097,
        "/" + "é" * 3000,
    ],
)
def test_malformed_canonical_resolution_result_fails_before_stat_or_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed_canonical: object,
) -> None:
    project_root = _project(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        config_module,
        "_resolve_project_root",
        lambda _value: malformed_canonical,
    )
    monkeypatch.setattr(
        config_module,
        "_read_project_root_stat",
        lambda _value: events.append("stat"),
    )
    monkeypatch.setattr(
        config_module,
        "_query_user_runtime_path",
        lambda: events.append("runtime"),
    )

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, ValueError, PROJECT_VALUE_ERROR)
    assert events == []


def test_malformed_project_stat_result_fails_before_digest_or_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(config_module, "_read_project_root_stat", lambda _value: object())
    monkeypatch.setattr(
        config_module,
        "_derive_project_digest",
        lambda _value: events.append("digest"),
    )

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, ValueError, PROJECT_VALUE_ERROR)
    assert events == []


@pytest.mark.parametrize(
    ("occurrence", "error_type", "message", "expected_events"),
    [
        (1, ValueError, PROJECT_VALUE_ERROR, []),
        (2, ValueError, PROJECT_VALUE_ERROR, []),
        (3, RuntimeError, RUNTIME_ERROR, ["runtime"]),
        (4, RuntimeError, RUNTIME_ERROR, ["runtime", "store"]),
    ],
)
def test_inexact_fsencoded_bytes_fail_at_each_path_stage_without_later_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    occurrence: int,
    error_type: type[Exception],
    message: str,
    expected_events: list[str],
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    real_fsencode = config_module._fsencode
    real_store = config_module._construct_state_store
    real_make = config_module._make_config
    count = 0
    events: list[str] = []

    def derived_bytes(value: str) -> object:
        nonlocal count
        count += 1
        encoded = real_fsencode(value)
        assert type(encoded) is bytes
        if count == occurrence:
            return _BytesSubclass(encoded)
        return encoded

    def provider() -> Path:
        events.append("runtime")
        return runtime_root

    def store_factory(root: Path, project_id: str) -> object:
        events.append("store")
        return real_store(root, project_id)

    def make_config(**kwargs: object) -> SidecarRuntimeConfig:
        events.append("make")
        return real_make(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(config_module, "_fsencode", derived_bytes)
    monkeypatch.setattr(config_module, "_query_user_runtime_path", provider)
    monkeypatch.setattr(config_module, "_construct_state_store", store_factory)
    monkeypatch.setattr(config_module, "_make_config", make_config)

    with pytest.raises(error_type) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, error_type, message)
    assert count == occurrence
    assert events == expected_events


@pytest.mark.parametrize("malformed_encoded", [b"", b"x" * 4097, object()])
def test_malformed_fsencoded_project_text_stops_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed_encoded: object,
) -> None:
    project_root = _project(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(config_module, "_fsencode", lambda _value: malformed_encoded)
    monkeypatch.setattr(
        config_module,
        "_resolve_project_root",
        lambda _value: events.append("resolve"),
    )
    monkeypatch.setattr(
        config_module,
        "_query_user_runtime_path",
        lambda: events.append("runtime"),
    )

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, ValueError, PROJECT_VALUE_ERROR)
    assert events == []


@pytest.mark.parametrize(
    "malformed_digest",
    [object(), _TextSubclass("0" * 64), "0" * 63, "G" * 64],
)
def test_malformed_digest_fails_fixed_before_platformdirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed_digest: object,
) -> None:
    project_root = _project(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        config_module,
        "_derive_project_digest",
        lambda _value: malformed_digest,
    )
    monkeypatch.setattr(
        config_module,
        "_query_user_runtime_path",
        lambda: events.append("runtime"),
    )

    with pytest.raises(RuntimeError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, RuntimeError, RUNTIME_ERROR)
    assert events == []


@pytest.mark.parametrize("allowed_character", [" ", "\x80"], ids=["space", "ord-128"])
def test_control_free_rule_allows_boundary_characters_for_project_and_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_character: str,
) -> None:
    project_root = _project(tmp_path, f"project-{allowed_character}-allowed")
    runtime_root = tmp_path / f"runtime-{allowed_character}-allowed"
    _install_runtime_provider(monkeypatch, runtime_root)

    config = prepare_sidecar_runtime_config(project_root)

    assert config.project_root == str(project_root.resolve(strict=True))
    assert config.runtime_root == str(runtime_root)


@pytest.mark.parametrize(
    ("seam", "error_type", "message"),
    [
        ("_ISFINITE", ValueError, TIMEOUT_VALUE_ERROR),
        ("_is_absolute", ValueError, PROJECT_VALUE_ERROR),
        ("_is_directory", ValueError, PROJECT_VALUE_ERROR),
    ],
)
def test_integer_one_never_impersonates_exact_boolean_collaborator_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seam: str,
    error_type: type[Exception],
    message: str,
) -> None:
    project_root = _project(tmp_path)
    _install_runtime_provider(monkeypatch, tmp_path / "runtime-root")
    monkeypatch.setattr(config_module, seam, lambda *_args, **_kwargs: 1)

    with pytest.raises(error_type) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, error_type, message)


def test_project_root_rejects_missing_file_root_and_root_alias_without_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    file_path = tmp_path / "private-file"
    file_path.write_text("not a directory", encoding="utf-8")
    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(Path("/"), target_is_directory=True)
    missing = tmp_path / "private-missing"

    for invalid in (missing, file_path, Path("/"), "//", root_alias):
        with pytest.raises(ValueError) as captured:
            prepare_sidecar_runtime_config(invalid)
        _assert_exact_error(captured.value, ValueError, PROJECT_VALUE_ERROR)
        assert str(invalid) not in str(captured.value)

    assert capsys.readouterr() == ("", "")


def test_existing_control_character_directory_is_rejected_privately(
    tmp_path: Path,
) -> None:
    control_root = tmp_path / "private\nroot"
    control_root.mkdir()

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(control_root)

    _assert_exact_error(captured.value, ValueError, PROJECT_VALUE_ERROR)
    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    "runtime_path",
    [
        Path("relative-runtime"),
        Path("/"),
        Path("/tmp/../runtime"),
        Path("/tmp/runtime\nprivate"),
        Path("/" + "é" * 3000),
        _PathSubclass("/tmp/runtime"),
        "/tmp/runtime",
        _DuckPath(),
    ],
)
def test_platformdirs_malformed_paths_fail_before_state_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime_path: object,
) -> None:
    project_root = _project(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        config_module,
        "_query_user_runtime_path",
        lambda: runtime_path,
    )
    monkeypatch.setattr(
        config_module,
        "_construct_state_store",
        lambda *_args: events.append("store"),
    )

    with pytest.raises(RuntimeError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, RuntimeError, RUNTIME_ERROR)
    assert events == []


def test_platformdirs_and_state_store_are_called_once_without_creating_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    source_path = project_root / "source.py"
    source_bytes = b"def unchanged():\n    return 'private source bytes'\n"
    source_path.write_bytes(source_bytes)
    runtime_root = tmp_path / "private-runtime" / "flowsight"
    calls = _install_runtime_provider(monkeypatch, runtime_root)
    store_calls: list[tuple[Path, str]] = []
    expected_project_id = (
        "project-v1-"
        + hashlib.sha256(
            b"flowsight-project-v1\x00" + os.fsencode(str(project_root.resolve(strict=True)))
        ).hexdigest()
    )

    def construct_store(root: Path, project_id: str) -> object:
        assert type(root) is PLATFORM_PATH_TYPE
        assert root is runtime_root
        assert type(project_id) is str and project_id == expected_project_id
        store_calls.append((root, project_id))
        return config_module._STATE_STORE_TYPE(root, project_id=project_id)

    monkeypatch.setattr(config_module, "_construct_state_store", construct_store)
    runtime_parent = runtime_root.parent
    assert not runtime_parent.exists()
    assert not runtime_root.exists()
    before_paths = set(tmp_path.rglob("*"))
    before_signatures = {
        path: _lstat_signature(path) for path in (tmp_path, project_root, source_path)
    }

    config = prepare_sidecar_runtime_config(project_root, requested_port=65_535)

    assert calls == [("flowsight", False, False)]
    assert store_calls == [(runtime_root, expected_project_id)]
    assert config.runtime_root == str(runtime_root)
    assert config.requested_port == 65_535
    assert set(tmp_path.rglob("*")) == before_paths
    assert {
        path: _lstat_signature(path) for path in (tmp_path, project_root, source_path)
    } == before_signatures
    assert source_path.read_bytes() == source_bytes
    assert not runtime_parent.exists()
    assert not runtime_root.exists()


def test_state_store_normalizes_an_existing_runtime_parent_alias_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    canonical_parent = tmp_path / "canonical-runtime-parent"
    canonical_parent.mkdir()
    alias_parent = tmp_path / "runtime-parent-alias"
    alias_parent.symlink_to(canonical_parent, target_is_directory=True)
    requested_runtime_root = alias_parent / "flowsight"
    canonical_runtime_root = canonical_parent / "flowsight"
    _install_runtime_provider(monkeypatch, requested_runtime_root)

    config = prepare_sidecar_runtime_config(project_root)

    assert config.runtime_root == str(canonical_runtime_root)
    assert config.runtime_root != str(requested_runtime_root)
    assert not requested_runtime_root.exists()
    assert not canonical_runtime_root.exists()


def test_preexisting_runtime_target_bytes_and_metadata_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir(mode=0o700)
    sentinel = runtime_root / "sentinel.bin"
    sentinel.write_bytes(b"private-sentinel-bytes")
    sentinel.chmod(0o600)
    _install_runtime_provider(monkeypatch, runtime_root)

    def metadata(path: Path) -> tuple[int, ...]:
        value = path.lstat()
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_nlink,
            value.st_size,
            value.st_atime_ns,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_blocks,
        )

    before_names = tuple(path.name for path in runtime_root.iterdir())
    before_bytes = sentinel.read_bytes()
    before_root = metadata(runtime_root)
    before_sentinel = metadata(sentinel)

    config = prepare_sidecar_runtime_config(project_root)

    assert type(config) is SidecarRuntimeConfig
    assert metadata(runtime_root) == before_root
    assert metadata(sentinel) == before_sentinel
    assert tuple(path.name for path in runtime_root.iterdir()) == before_names
    assert sentinel.read_bytes() == before_bytes


def test_success_releases_caller_and_platform_path_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)
    project_references = sys.getrefcount(project_root)
    runtime_references = sys.getrefcount(runtime_root)

    config = prepare_sidecar_runtime_config(project_root)
    gc.collect()

    assert type(config) is SidecarRuntimeConfig
    assert config.project_root == str(project_root)
    assert config.runtime_root == str(runtime_root)
    assert sys.getrefcount(project_root) == project_references
    assert sys.getrefcount(runtime_root) == runtime_references


def test_public_attribute_replacement_cannot_redirect_frozen_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    captured_runtime_provider = config_module._USER_RUNTIME_PATH
    captured_store_constructor = config_module._STATE_STORE_CONSTRUCTOR
    assert captured_runtime_provider is platformdirs.user_runtime_path
    assert captured_store_constructor is sidecar_package.StateStore

    def public_bomb(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("public replacement must not run")

    monkeypatch.setattr(platformdirs, "user_runtime_path", public_bomb)
    monkeypatch.setattr(config_module, "user_runtime_path", public_bomb)
    monkeypatch.setattr(config_module, "StateStore", public_bomb)
    monkeypatch.setattr(sidecar_package, "StateStore", public_bomb)

    captured_runtime_result = config_module._query_user_runtime_path()
    captured_store_result = config_module._construct_state_store(
        tmp_path / "captured-store-root",
        "project-captured-provenance",
    )
    assert type(captured_runtime_result) is PLATFORM_PATH_TYPE
    assert type(captured_store_result) is config_module._STATE_STORE_TYPE
    assert config_module._USER_RUNTIME_PATH is captured_runtime_provider
    assert config_module._STATE_STORE_CONSTRUCTOR is captured_store_constructor

    monkeypatch.setattr(config_module, "_query_user_runtime_path", lambda: runtime_root)

    config = prepare_sidecar_runtime_config(project_root)

    assert type(config) is SidecarRuntimeConfig
    assert config.runtime_root == str(runtime_root)


def test_public_config_class_replacement_cannot_redirect_captured_factory_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)

    class ReplacementConfig:
        def __new__(cls, *args: object, **kwargs: object) -> object:
            del cls, args, kwargs
            raise AssertionError("public class replacement must not construct")

    assert config_module._CONFIG_TYPE is SidecarRuntimeConfig
    monkeypatch.setattr(config_module, "SidecarRuntimeConfig", ReplacementConfig)

    config = prepare_sidecar_runtime_config(project_root)

    assert type(config) is SidecarRuntimeConfig
    assert config_module.SidecarRuntimeConfig is ReplacementConfig
    assert config_module._CONFIG_TYPE is SidecarRuntimeConfig


@pytest.mark.parametrize(
    ("malformation", "field_name"),
    [
        ("uninitialized", None),
        *(("missing", field_name) for field_name in SidecarRuntimeConfig.__slots__),
        *(("derived", field_name) for field_name in SidecarRuntimeConfig.__slots__),
        *(("opaque", field_name) for field_name in SidecarRuntimeConfig.__slots__),
        *(("unequal", field_name) for field_name in SidecarRuntimeConfig.__slots__),
    ],
)
@pytest.mark.parametrize("requested_port", [None, 0, 4040])
def test_forged_exact_config_with_missing_inexact_or_mismatched_slot_fails_fixed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformation: str,
    field_name: str | None,
    requested_port: int | None,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)

    def forge_config(**expected: object) -> SidecarRuntimeConfig:
        forged = object.__new__(config_module._CONFIG_TYPE)
        if malformation == "uninitialized":
            return forged
        assert field_name is not None
        fields = dict(expected)
        if malformation == "missing":
            fields.pop(field_name)
        elif malformation == "derived":
            value = fields[field_name]
            if type(value) is str:
                fields[field_name] = _TextSubclass(value)
            elif type(value) is int:
                fields[field_name] = _IntSubclass(value)
            elif value is None:
                fields[field_name] = object()
            else:
                assert type(value) is float
                fields[field_name] = _FloatSubclass(value)
        elif malformation == "opaque":
            fields[field_name] = object()
        else:
            assert malformation == "unequal"
            unequal = {
                "project_root": "/unequal-project-root",
                "runtime_root": "/unequal-runtime-root",
                "project_id": "project-v1-" + "0" * 64,
                "requested_port": 1 if requested_port is None else None,
                "startup_timeout": 6.0,
            }
            fields[field_name] = unequal[field_name]
        for name, value in fields.items():
            object.__setattr__(forged, name, value)
        return forged

    monkeypatch.setattr(config_module, "_make_config", forge_config)

    with pytest.raises(RuntimeError) as captured:
        prepare_sidecar_runtime_config(project_root, requested_port=requested_port)

    _assert_exact_error(captured.value, RuntimeError, RUNTIME_ERROR)
    assert str(project_root) not in "".join(traceback.format_exception(captured.value))


def test_state_store_result_is_exact_matching_and_only_runtime_root_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)
    stores: list[weakref.ReferenceType[object]] = []
    runtime_dirs: list[str] = []

    def store_factory(root: Path, project_id: str) -> object:
        store = config_module._STATE_STORE_TYPE(root, project_id=project_id)
        stores.append(weakref.ref(store))
        runtime_dirs.append(str(store.runtime_dir))
        return store

    monkeypatch.setattr(config_module, "_construct_state_store", store_factory)
    config = prepare_sidecar_runtime_config(project_root)
    gc.collect()

    assert config.runtime_root == str(runtime_root)
    assert runtime_dirs and config.runtime_root != runtime_dirs[0]
    assert stores and stores[0]() is None
    assert all(
        type(value) in {str, int, float, type(None)}
        for value in (
            config.project_root,
            config.runtime_root,
            config.project_id,
            config.requested_port,
            config.startup_timeout,
        )
    )


@pytest.mark.parametrize(
    "malformation",
    [
        "wrong_type",
        "derived_store",
        "wrong_project",
        "derived_project",
        "missing_project",
        "missing_root",
        "relative_root",
        "filesystem_root",
        "parent_root",
        "derived_root",
        "bad_root",
    ],
)
def test_malformed_injected_state_store_result_fails_without_dynamic_path_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformation: str,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)

    class MaliciousPath:
        def __fspath__(self) -> str:
            raise AssertionError("malformed path protocol must not run")

    class DerivedStateStore(config_module._STATE_STORE_TYPE):
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(f"derived store protocol must not run: {name}")

    def store_factory(root: Path, project_id: str) -> object:
        if malformation == "wrong_type":
            return object()
        if malformation == "derived_store":
            return object.__new__(DerivedStateStore)
        store = config_module._STATE_STORE_TYPE(root, project_id=project_id)
        fields = object.__getattribute__(store, "__dict__")
        if malformation == "wrong_project":
            fields["project_id"] = "project-v1-" + "0" * 64
        elif malformation == "derived_project":
            fields["project_id"] = _TextSubclass(project_id)
        elif malformation == "missing_project":
            del fields["project_id"]
        elif malformation == "missing_root":
            del fields["runtime_root"]
        elif malformation == "relative_root":
            fields["runtime_root"] = Path("relative-runtime")
        elif malformation == "filesystem_root":
            fields["runtime_root"] = Path("/")
        elif malformation == "parent_root":
            fields["runtime_root"] = Path("/tmp/../runtime")
        elif malformation == "derived_root":
            fields["runtime_root"] = _PathSubclass("/tmp/runtime")
        else:
            assert malformation == "bad_root"
            fields["runtime_root"] = MaliciousPath()
        return store

    monkeypatch.setattr(config_module, "_construct_state_store", store_factory)

    with pytest.raises(RuntimeError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, RuntimeError, RUNTIME_ERROR)


def test_state_store_dictionary_requires_exact_builtin_string_keys_without_protocols(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)
    equality_calls = 0
    events: list[str] = []

    class HostileProjectKey:
        def __hash__(self) -> int:
            return hash("project_id")

        def __eq__(self, other: object) -> bool:
            nonlocal equality_calls
            del other
            equality_calls += 1
            return True

    hostile_key = HostileProjectKey()

    def malformed_fields(_store: object) -> object:
        return {
            hostile_key: "project-v1-" + "0" * 64,
            "runtime_root": runtime_root,
        }

    monkeypatch.setattr(config_module, "_read_state_store_fields", malformed_fields)
    monkeypatch.setattr(config_module, "_make_config", lambda **_kwargs: events.append("make"))

    with pytest.raises(RuntimeError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, RuntimeError, RUNTIME_ERROR)
    assert equality_calls == 0
    assert events == []


def test_state_store_dictionary_subclass_is_rejected_before_private_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    _install_runtime_provider(monkeypatch, runtime_root)
    real_read_fields = config_module._read_state_store_fields
    events: list[str] = []

    class DerivedDictionary(dict[object, object]):
        pass

    def derived_fields(store: object) -> object:
        fields = real_read_fields(store)  # type: ignore[arg-type]
        assert type(fields) is dict
        return DerivedDictionary(fields)

    monkeypatch.setattr(config_module, "_read_state_store_fields", derived_fields)
    monkeypatch.setattr(config_module, "_make_config", lambda **_kwargs: events.append("make"))

    with pytest.raises(RuntimeError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, RuntimeError, RUNTIME_ERROR)
    assert events == []


def test_runtime_failure_hides_and_releases_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _TrackedFailure.references.clear()
    project_root = _project(tmp_path)
    secret_marker = "flowsight-p0-014-runtime-secret-8f2c1a"

    def failing_provider(*_args: object, **_kwargs: object) -> NoReturn:
        raise _TrackedFailure(secret_marker)

    monkeypatch.setattr(config_module, "_query_user_runtime_path", failing_provider)
    with pytest.raises(RuntimeError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, RuntimeError, RUNTIME_ERROR)
    assert secret_marker not in "".join(traceback.format_exception(captured.value))
    gc.collect()
    assert _TrackedFailure.references and _TrackedFailure.references[0]() is None


def test_project_resolution_failure_hides_raw_exception_and_stops_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    events: list[str] = []
    secret_marker = "flowsight-p0-014-project-secret-51d9e7"

    def fail_resolution(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(secret_marker)

    monkeypatch.setattr(config_module, "_resolve_project_root", fail_resolution)
    monkeypatch.setattr(
        config_module,
        "_query_user_runtime_path",
        lambda: events.append("runtime"),
    )

    with pytest.raises(ValueError) as captured:
        prepare_sidecar_runtime_config(project_root)

    _assert_exact_error(captured.value, ValueError, PROJECT_VALUE_ERROR)
    assert secret_marker not in "".join(traceback.format_exception(captured.value))
    assert events == []


def test_fixed_failure_suppresses_but_does_not_inspect_caller_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = _project(tmp_path)
    monkeypatch.setattr(
        config_module,
        "_query_user_runtime_path",
        lambda: (_ for _ in ()).throw(ValueError("runtime failure")),
    )
    caller_error = ValueError("caller-private-context")

    try:
        raise caller_error
    except ValueError:
        with pytest.raises(RuntimeError) as captured:
            prepare_sidecar_runtime_config(project_root)

    assert captured.value.__context__ is caller_error
    assert captured.value.__suppress_context__ is True
    assert "caller-private-context" not in "".join(traceback.format_exception(captured.value))


@pytest.mark.parametrize(
    "dependency",
    [
        "_DIRNAME",
        "_FSENCODE",
        "_FSPATH",
        "_ISABS",
        "_ISFINITE",
        "_IS_DIRECTORY",
        "_REALPATH",
        "_SHA256",
        "_SHA256_HEXDIGEST",
        "_STAT",
        "_STATE_STORE_CONSTRUCTOR",
        "_USER_RUNTIME_PATH",
    ],
)
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, _Control])
def test_process_control_from_each_captured_dependency_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dependency: str,
    control_type: type[BaseException],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    control = control_type("caller-owned-/private/path")

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise control

    if dependency == "_SHA256_HEXDIGEST":

        class FailingDigest:
            def hexdigest(self) -> NoReturn:
                raise control

        monkeypatch.setattr(config_module, "_SHA256", lambda _material: FailingDigest())
    else:
        monkeypatch.setattr(config_module, dependency, fail)
    if dependency != "_USER_RUNTIME_PATH":
        monkeypatch.setattr(
            config_module,
            "_USER_RUNTIME_PATH",
            lambda *_args, **_kwargs: runtime_root,
        )

    with pytest.raises(control_type) as captured:
        prepare_sidecar_runtime_config(project_root)

    assert captured.value is control
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(("seam", "occurrence"), OPERATION_POINTS)
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, _Control])
def test_process_control_from_every_operation_preserves_identity_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seam: str,
    occurrence: int,
    control_type: type[BaseException],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    events: list[str] = []
    counts: dict[str, int] = {}
    control = control_type("caller-owned-/private/path")
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )

    def recording_operation(name: str, operation: object) -> object:
        def wrapper(*args: object, **kwargs: object) -> object:
            counts[name] = counts.get(name, 0) + 1
            events.append(name)
            if name == seam and counts[name] == occurrence:
                raise control
            return operation(*args, **kwargs)  # type: ignore[operator]

        return wrapper

    for operation_name in dict.fromkeys(OPERATION_SEQUENCE):
        monkeypatch.setattr(
            config_module,
            operation_name,
            recording_operation(operation_name, getattr(config_module, operation_name)),
        )

    with pytest.raises(control_type) as captured:
        prepare_sidecar_runtime_config(project_root)

    assert captured.value is control
    target_index = OPERATION_POINTS.index((seam, occurrence))
    assert events == list(OPERATION_SEQUENCE[: target_index + 1])
    assert capsys.readouterr() == ("", "")


def test_successful_flow_has_one_reviewed_operation_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = _project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    events: list[str] = []
    real_realpath = config_module._resolve_project_root
    real_stat = config_module._read_project_root_stat
    real_sha256 = config_module._derive_project_digest
    real_read_fields = config_module._read_state_store_fields
    real_make = config_module._make_config
    real_read_slots = config_module._read_config_slots

    def record(name: str, operation: object) -> object:
        def wrapper(*args: object, **kwargs: object) -> object:
            events.append(name)
            return operation(*args, **kwargs)  # type: ignore[operator]

        return wrapper

    monkeypatch.setattr(config_module, "_resolve_project_root", record("realpath", real_realpath))
    monkeypatch.setattr(config_module, "_read_project_root_stat", record("stat", real_stat))
    monkeypatch.setattr(config_module, "_derive_project_digest", record("sha256", real_sha256))
    monkeypatch.setattr(
        config_module,
        "_read_state_store_fields",
        record("fields", real_read_fields),
    )

    def provider() -> Path:
        events.append("runtime")
        return runtime_root

    def store_factory(root: Path, project_id: str) -> object:
        events.append("store")
        return config_module._STATE_STORE_TYPE(root, project_id=project_id)

    def make_config(**kwargs: object) -> SidecarRuntimeConfig:
        events.append("make")
        return real_make(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(config_module, "_query_user_runtime_path", provider)
    monkeypatch.setattr(config_module, "_construct_state_store", store_factory)
    monkeypatch.setattr(config_module, "_make_config", make_config)
    monkeypatch.setattr(
        config_module,
        "_read_config_slots",
        record("config-slots", real_read_slots),
    )

    config = prepare_sidecar_runtime_config(project_root)

    assert type(config) is SidecarRuntimeConfig
    assert events == [
        "realpath",
        "stat",
        "sha256",
        "runtime",
        "store",
        "fields",
        "make",
        "config-slots",
    ]
    assert capsys.readouterr() == ("", "")


def test_runtime_config_source_stays_inside_inert_phase0_boundary() -> None:
    source_path = Path(config_module.__file__).resolve()
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    def import_signature(
        node: ast.Import | ast.ImportFrom,
    ) -> tuple[str, int, str | None, tuple[tuple[str, str | None], ...]]:
        return (
            "import" if isinstance(node, ast.Import) else "from",
            0 if isinstance(node, ast.Import) else node.level,
            None if isinstance(node, ast.Import) else node.module,
            tuple((alias.name, alias.asname) for alias in node.names),
        )

    top_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    all_imports = [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert all_imports == top_level_imports
    assert [import_signature(node) for node in top_level_imports] == [
        ("from", 0, "__future__", (("annotations", None),)),
        ("import", 0, None, (("hashlib", None),)),
        ("import", 0, None, (("math", None),)),
        ("import", 0, None, (("os", None),)),
        ("import", 0, None, (("stat", None),)),
        ("from", 0, "pathlib", (("Path", None),)),
        (
            "from",
            0,
            "typing",
            (("Final", None), ("NoReturn", None), ("SupportsIndex", None), ("cast", None)),
        ),
        ("from", 0, "platformdirs", (("user_runtime_path", None),)),
        ("from", 1, "state", (("StateStore", None),)),
    ]

    top_level_definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert {node.name for node in top_level_definitions} == {
        "SidecarRuntimeConfig",
        "_RuntimeConfigurationFailure",
        "_admit_config",
        "_canonical_project_root",
        "_construct_state_store",
        "_derive_project_digest",
        "_derive_project_id",
        "_dirname",
        "_exact_absolute_path_text",
        "_fsencode",
        "_fspath",
        "_is_absolute",
        "_is_directory",
        "_is_filesystem_root",
        "_make_config",
        "_path_has_parent_reference",
        "_prepare_runtime_config",
        "_query_user_runtime_path",
        "_raise_port_invalid",
        "_raise_project_root_invalid",
        "_raise_project_root_type",
        "_raise_runtime_failure",
        "_raise_timeout_invalid",
        "_raise_timeout_type",
        "_read_config_slots",
        "_read_project_root_stat",
        "_read_state_store_fields",
        "_resolve_project_root",
        "_runtime_root",
        "_valid_path_text",
        "_validate_requested_port",
        "_validate_startup_timeout",
        "prepare_sidecar_runtime_config",
    }
    config_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SidecarRuntimeConfig"
    )
    assert {
        node.name
        for node in config_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } == {
        "__delattr__",
        "__eq__",
        "__hash__",
        "__init__",
        "__getstate__",
        "__new__",
        "__reduce__",
        "__reduce_ex__",
        "__repr__",
        "__setattr__",
    }
    config_methods = {
        node.name: node for node in config_class.body if isinstance(node, ast.FunctionDef)
    }

    def body_dump(nodes: list[ast.stmt]) -> list[str]:
        return [ast.dump(node, include_attributes=False) for node in nodes]

    def expected_body(source_text: str) -> list[str]:
        parsed = ast.parse(source_text)
        function = parsed.body[0]
        assert isinstance(function, ast.FunctionDef)
        return body_dump(function.body)

    assert body_dump(config_methods["__new__"].body) == expected_body(
        """
def expected():
    del cls, args, kwargs
    raise TypeError("SidecarRuntimeConfig must be prepared") from None
"""
    )
    assert body_dump(config_methods["__init__"].body) == expected_body(
        """
def expected():
    del self, args, kwargs
    raise TypeError("SidecarRuntimeConfig must be prepared") from None
"""
    )
    assert body_dump(config_methods["__repr__"].body) == expected_body(
        """
def expected():
    return "<SidecarRuntimeConfig>"
"""
    )
    assert body_dump(config_methods["__setattr__"].body) == expected_body(
        """
def expected():
    del name, value
    raise AttributeError("SidecarRuntimeConfig is immutable") from None
"""
    )
    assert body_dump(config_methods["__delattr__"].body) == expected_body(
        """
def expected():
    del name
    raise AttributeError("SidecarRuntimeConfig is immutable") from None
"""
    )
    assert body_dump(config_methods["__reduce__"].body) == expected_body(
        """
def expected():
    raise TypeError("SidecarRuntimeConfig cannot be serialized") from None
"""
    )
    assert body_dump(config_methods["__reduce_ex__"].body) == expected_body(
        """
def expected():
    del protocol
    raise TypeError("SidecarRuntimeConfig cannot be serialized") from None
"""
    )
    assert body_dump(config_methods["__getstate__"].body) == expected_body(
        """
def expected():
    raise TypeError("SidecarRuntimeConfig cannot be serialized") from None
"""
    )
    assert body_dump(config_methods["__eq__"].body) == expected_body(
        """
def expected():
    if type(other) is not _CONFIG_TYPE:
        return NotImplemented
    return (
        self.project_root == other.project_root
        and self.runtime_root == other.runtime_root
        and self.project_id == other.project_id
        and self.requested_port == other.requested_port
        and self.startup_timeout == other.startup_timeout
    )
"""
    )
    assert body_dump(config_methods["__hash__"].body) == expected_body(
        """
def expected():
    return hash(
        (
            self.project_root,
            self.runtime_root,
            self.project_id,
            self.requested_port,
            self.startup_timeout,
        )
    )
"""
    )
    allowed_function_nodes = {
        id(node)
        for node in (*top_level_definitions, *config_class.body)
        if isinstance(node, ast.FunctionDef)
    }
    assert all(
        id(node) in allowed_function_nodes
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    )
    assert not any(isinstance(node, ast.Lambda) for node in ast.walk(tree))

    top_level_assignments: set[str] = set()
    top_level_assignment_values: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            assert all(isinstance(target, ast.Name) for target in node.targets)
            top_level_assignments.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
            top_level_assignment_values.update(
                {target.id: node.value for target in node.targets if isinstance(target, ast.Name)}
            )
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            assert isinstance(node.target, ast.Name)
            top_level_assignments.add(node.target.id)
            assert node.value is not None
            top_level_assignment_values[node.target.id] = node.value
            value = node.value
        else:
            continue
        assert not any(
            isinstance(
                descendant,
                (
                    ast.Dict,
                    ast.DictComp,
                    ast.GeneratorExp,
                    ast.List,
                    ast.ListComp,
                    ast.Set,
                    ast.SetComp,
                ),
            )
            for descendant in ast.walk(value)
        )
    assert top_level_assignments == {
        "_CONFIG_TYPE",
        "_DIRNAME",
        "_FSENCODE",
        "_FSPATH",
        "_ISABS",
        "_ISFINITE",
        "_IS_DIRECTORY",
        "_MAX_PATH_BYTES",
        "_MAX_PATH_CHARACTERS",
        "_MAX_STARTUP_TIMEOUT_SECONDS",
        "_PATH_SEPARATOR",
        "_PLATFORM_PATH_TYPE",
        "_PROJECT_ID_HEX_CHARACTERS",
        "_PROJECT_ID_PREFIX",
        "_REALPATH",
        "_SHA256",
        "_STAT",
        "_STATE_STORE_CONSTRUCTOR",
        "_STATE_STORE_TYPE",
        "_STAT_RESULT_TYPE",
        "_USER_RUNTIME_PATH",
    }
    top_level_assignment_nodes = [
        node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    assert len(top_level_assignment_nodes) == len(top_level_assignments)
    assert all(isinstance(node, ast.AnnAssign) for node in top_level_assignment_nodes)
    assert all(
        isinstance(node.annotation, ast.Name) and node.annotation.id == "Final"
        for node in top_level_assignment_nodes
        if isinstance(node, ast.AnnAssign)
    )
    hexadecimal_binding = top_level_assignment_values["_PROJECT_ID_HEX_CHARACTERS"]
    assert isinstance(hexadecimal_binding, ast.Call)
    assert isinstance(hexadecimal_binding.func, ast.Name)
    assert hexadecimal_binding.func.id == "frozenset"
    assert hexadecimal_binding.keywords == []
    assert len(hexadecimal_binding.args) == 1
    assert isinstance(hexadecimal_binding.args[0], ast.Constant)
    assert hexadecimal_binding.args[0].value == "0123456789abcdef"
    project_id_prefix_binding = top_level_assignment_values["_PROJECT_ID_PREFIX"]
    assert isinstance(project_id_prefix_binding, ast.Constant)
    assert project_id_prefix_binding.value == b"flowsight-project-v1\x00"
    for constant_name, expected_value in {
        "_MAX_PATH_BYTES": 4096,
        "_MAX_PATH_CHARACTERS": 4096,
        "_MAX_STARTUP_TIMEOUT_SECONDS": 30.0,
    }.items():
        binding = top_level_assignment_values[constant_name]
        assert isinstance(binding, ast.Constant)
        assert type(binding.value) is type(expected_value)
        assert binding.value == expected_value

    functions_by_name = {
        node.name: node for node in top_level_definitions if isinstance(node, ast.FunctionDef)
    }
    assert body_dump(functions_by_name["_derive_project_digest"].body) == expected_body(
        """
def expected():
    return _SHA256(material).hexdigest()
"""
    )
    assert body_dump(functions_by_name["_derive_project_id"].body) == expected_body(
        """
def expected():
    digest = _derive_project_digest(_PROJECT_ID_PREFIX + encoded_project_root)
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in _PROJECT_ID_HEX_CHARACTERS for character in digest)
    ):
        raise _RuntimeConfigurationFailure
    return f"project-v1-{digest}"
"""
    )
    assert body_dump(functions_by_name["_valid_path_text"].body) == expected_body(
        """
def expected():
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_PATH_CHARACTERS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    try:
        encoded = _fsencode(value)
    except Exception:
        return None
    if type(encoded) is not bytes or not 1 <= len(encoded) <= _MAX_PATH_BYTES:
        return None
    return value, encoded
"""
    )

    assert not any(
        isinstance(node, (ast.List, ast.Dict, ast.ListComp, ast.DictComp, ast.SetComp))
        for node in ast.walk(tree)
    )
    assert [
        ast.dump(node, include_attributes=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Set)
    ] == [
        ast.dump(ast.parse("{int, float}", mode="eval").body, include_attributes=False),
        ast.dump(
            ast.parse("{str, _PLATFORM_PATH_TYPE}", mode="eval").body,
            include_attributes=False,
        ),
    ]
    assert not any(
        isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Store)
        for node in ast.walk(tree)
    )
    assert not any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(tree))
    assert not any(
        isinstance(node, ast.Name) and node.id in {"__builtins__", "breakpoint", "input"}
        for node in ast.walk(tree)
    )
    for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
        assert isinstance(handler.type, ast.Name)
        assert handler.type.id in {"Exception", "KeyError"}

    def dotted_name(value: ast.expr) -> str | None:
        if isinstance(value, ast.Name):
            return value.id
        if isinstance(value, ast.Attribute):
            prefix = dotted_name(value.value)
            return None if prefix is None else f"{prefix}.{value.attr}"
        return None

    all_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    called_paths = [path for node in all_calls if (path := dotted_name(node.func)) is not None]
    assert Counter(called_paths) == Counter(
        {
            "AttributeError": 2,
            "Path": 1,
            "RuntimeError": 1,
            "TypeError": 7,
            "ValueError": 3,
            "_DIRNAME": 1,
            "_FSENCODE": 1,
            "_FSPATH": 1,
            "_ISABS": 1,
            "_ISFINITE": 1,
            "_IS_DIRECTORY": 1,
            "_REALPATH": 1,
            "_SHA256": 1,
            "_STAT": 1,
            "_STATE_STORE_CONSTRUCTOR": 1,
            "_USER_RUNTIME_PATH": 1,
            "_admit_config": 1,
            "_canonical_project_root": 1,
            "_construct_state_store": 1,
            "_derive_project_digest": 1,
            "_derive_project_id": 1,
            "_dirname": 1,
            "_exact_absolute_path_text": 2,
            "_fsencode": 1,
            "_fspath": 2,
            "_is_absolute": 2,
            "_is_directory": 1,
            "_is_filesystem_root": 2,
            "_make_config": 1,
            "_path_has_parent_reference": 2,
            "_prepare_runtime_config": 1,
            "_query_user_runtime_path": 1,
            "_raise_port_invalid": 1,
            "_raise_project_root_invalid": 1,
            "_raise_project_root_type": 1,
            "_raise_runtime_failure": 1,
            "_raise_timeout_invalid": 2,
            "_raise_timeout_type": 1,
            "_read_config_slots": 1,
            "_read_project_root_stat": 1,
            "_read_state_store_fields": 1,
            "_resolve_project_root": 1,
            "_runtime_root": 1,
            "_valid_path_text": 3,
            "_validate_requested_port": 1,
            "_validate_startup_timeout": 1,
            "any": 3,
            "cast": 2,
            "float": 1,
            "frozenset": 1,
            "hash": 1,
            "len": 4,
            "object.__getattribute__": 6,
            "object.__new__": 1,
            "object.__setattr__": 5,
            "ord": 2,
            "type": 27,
            "value.split": 1,
        }
    )
    indirect_calls = [node for node in all_calls if dotted_name(node.func) is None]
    assert len(indirect_calls) == 1
    assert ast.dump(indirect_calls[0], include_attributes=False) == ast.dump(
        ast.parse("_SHA256(material).hexdigest()", mode="eval").body,
        include_attributes=False,
    )

    forbidden_store_fields = {
        "database_path",
        "lock_path",
        "mutation_lock_path",
        "runtime_dir",
        "state_path",
    }
    assert all(
        node.attr not in forbidden_store_fields
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    )
    assert called_paths.count("_USER_RUNTIME_PATH") == 1
    assert called_paths.count("_STATE_STORE_CONSTRUCTOR") == 1
    getattribute_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func) == "object.__getattribute__"
    ]
    assert len(getattribute_calls) == 6
    assert all(len(node.args) == 2 for node in getattribute_calls)
    assert all(isinstance(node.args[1], ast.Constant) for node in getattribute_calls)
    assert [node.args[1].value for node in getattribute_calls] == [
        "__dict__",
        "project_root",
        "runtime_root",
        "project_id",
        "requested_port",
        "startup_timeout",
    ]
    setattr_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func) == "object.__setattr__"
    ]
    assert [
        node.args[1].value
        for node in setattr_calls
        if len(node.args) == 3 and isinstance(node.args[1], ast.Constant)
    ] == [
        "project_root",
        "runtime_root",
        "project_id",
        "requested_port",
        "startup_timeout",
    ]
    assert not any(isinstance(node, (ast.AsyncFunctionDef, ast.Await)) for node in ast.walk(tree))
    assert all(
        isinstance(node, (ast.FunctionDef, ast.ClassDef))
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
    )


def test_validation_order_rejects_project_type_before_port() -> None:
    with pytest.raises(TypeError) as captured:
        prepare_sidecar_runtime_config(
            _DuckPath(),  # type: ignore[arg-type]
            requested_port=True,
            startup_timeout=object(),  # type: ignore[arg-type]
        )
    _assert_exact_error(captured.value, TypeError, PROJECT_TYPE_ERROR)


def test_process_control_payload_is_preserved_but_never_emitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = _project(tmp_path)
    control = _Control("caller-private-payload")

    def provider(*_args: object, **_kwargs: object) -> NoReturn:
        raise control

    monkeypatch.setattr(config_module, "_query_user_runtime_path", provider)
    with pytest.raises(_Control) as captured:
        prepare_sidecar_runtime_config(project_root)

    assert captured.value is control
    assert str(captured.value) == "caller-private-payload"
    assert capsys.readouterr() == ("", "")
