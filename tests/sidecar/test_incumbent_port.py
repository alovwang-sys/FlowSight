from __future__ import annotations

import ast
import gc
import inspect
import stat
import sys
import traceback
import weakref
from pathlib import Path
from typing import NoReturn, get_type_hints

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import (
    SidecarRuntimeConfig,
    SidecarState,
    admit_configured_incumbent_port,
    prepare_sidecar_runtime_config,
)
from flowsight.sidecar import incumbent_port as port_module
from flowsight.sidecar import runtime_config as config_module
from flowsight.sidecar import state as state_module

CONFIG_TYPE_ERROR = "config must be an exact SidecarRuntimeConfig"
INCUMBENT_TYPE_ERROR = "incumbent must be an exact SidecarState"
PORT_ERROR = "configured incumbent port is incompatible"
_MISSING = object()


class _IntSubclass(int):
    pass


class _DerivedConfig(SidecarRuntimeConfig):
    pass


class _DerivedState(SidecarState):
    pass


class _Control(BaseException):
    pass


class _TrackedFailure(Exception):
    pass


class _ExplodingProxy:
    def __getattribute__(self, name: str) -> NoReturn:
        raise AssertionError(f"proxy dispatch ran: {name}")


class _HostileScalar:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _trip(self, operation: str) -> NoReturn:
        self.calls.append(operation)
        raise AssertionError(f"scalar protocol ran: {operation}")

    def __lt__(self, _other: object) -> NoReturn:
        self._trip("lt")

    def __le__(self, _other: object) -> NoReturn:
        self._trip("le")

    def __eq__(self, _other: object) -> NoReturn:
        self._trip("eq")

    def __ne__(self, _other: object) -> NoReturn:
        self._trip("ne")

    def __gt__(self, _other: object) -> NoReturn:
        self._trip("gt")

    def __ge__(self, _other: object) -> NoReturn:
        self._trip("ge")

    def __index__(self) -> NoReturn:
        self._trip("index")

    def __int__(self) -> NoReturn:
        self._trip("int")

    def __bool__(self) -> NoReturn:
        self._trip("bool")


def _valid_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    requested_port: int | None = 4040,
    incumbent_port: int = 4040,
) -> tuple[SidecarRuntimeConfig, SidecarState]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setattr(
        config_module,
        "_USER_RUNTIME_PATH",
        lambda *_args, **_kwargs: runtime_root,
    )
    config = prepare_sidecar_runtime_config(project, requested_port=requested_port)
    incumbent = SidecarState(
        project_id=config.project_id,
        startup_id="startup-v1",
        pid=321,
        port=incumbent_port,
        token="token-secret-" + "x" * 32,
        database_path=str((runtime_root / "project-state" / "flowsight.sqlite3").absolute()),
        started_at_ns=987_654_321,
    )
    return config, incumbent


def _forge_slot_instance(
    selected_type: type[SidecarRuntimeConfig] | type[SidecarState],
    slot_name: str,
    value: object = _MISSING,
) -> SidecarRuntimeConfig | SidecarState:
    result = object.__new__(selected_type)
    if value is not _MISSING:
        object.__setattr__(result, slot_name, value)
    return result


def _assert_fixed_error(
    error: BaseException,
    expected_type: type[TypeError] | type[RuntimeError],
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
    production_path = Path(port_module.__file__).resolve()
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


def _assert_production_frames_hide(error: BaseException, *targets: object) -> None:
    frames = _production_frames(error)
    assert frames
    exact_targets = tuple(targets)
    assert all(
        not _contains_identity(value, exact_targets)
        for _name, local_values in frames
        for value in local_values.values()
    )


def _tree_signature(root: Path) -> tuple[tuple[object, ...], ...]:
    signature: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*")):
        metadata = path.stat()
        relative = str(path.relative_to(root))
        content = path.read_bytes() if path.is_file() else None
        signature.append(
            (
                relative,
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_size,
                content,
            )
        )
    return tuple(signature)


def _config_snapshot(config: SidecarRuntimeConfig) -> tuple[object, ...]:
    return tuple(
        object.__getattribute__(config, name)
        for name in (
            "project_root",
            "runtime_root",
            "project_id",
            "requested_port",
            "startup_timeout",
        )
    )


def _state_snapshot(incumbent: SidecarState) -> tuple[object, ...]:
    return tuple(
        object.__getattribute__(incumbent, name)
        for name in (
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
        )
    )


def _call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    return "<dynamic>"


def test_public_surface_signature_and_exact_type_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, incumbent = _valid_pair(monkeypatch, tmp_path)
    assert sidecar_package.admit_configured_incumbent_port is admit_configured_incumbent_port
    assert sidecar_package.__all__.count("admit_configured_incumbent_port") == 1
    assert port_module.admit_configured_incumbent_port is admit_configured_incumbent_port

    signature = inspect.signature(admit_configured_incumbent_port)
    assert tuple(signature.parameters) == ("config", "incumbent")
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in signature.parameters.values()
    )
    assert signature.parameters["config"].annotation == "SidecarRuntimeConfig"
    assert signature.parameters["incumbent"].annotation == "SidecarState"
    assert signature.return_annotation == "SidecarState"
    assert get_type_hints(admit_configured_incumbent_port) == {
        "config": SidecarRuntimeConfig,
        "incumbent": SidecarState,
        "return": SidecarState,
    }

    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append("getter")
        raise AssertionError("slot getter ran")

    derived_config = object.__new__(_DerivedConfig)
    derived_state = object.__new__(_DerivedState)
    wrong_configs = (object(), derived_config, _ExplodingProxy())
    wrong_incumbents = (object(), derived_state, _ExplodingProxy())
    incumbent_sentinel = _ExplodingProxy()
    with monkeypatch.context() as preflight_patch:
        preflight_patch.setattr(port_module, "_read_requested_port", forbidden)
        preflight_patch.setattr(port_module, "_read_incumbent_port", forbidden)
        for wrong_config in wrong_configs:
            with pytest.raises(TypeError) as captured:
                admit_configured_incumbent_port(
                    wrong_config,  # type: ignore[arg-type]
                    incumbent_sentinel,  # type: ignore[arg-type]
                )
            _assert_fixed_error(captured.value, TypeError, CONFIG_TYPE_ERROR)
            _assert_production_frames_hide(
                captured.value,
                wrong_config,
                incumbent_sentinel,
            )
        for wrong_incumbent in wrong_incumbents:
            with pytest.raises(TypeError) as captured:
                admit_configured_incumbent_port(
                    config,
                    wrong_incumbent,  # type: ignore[arg-type]
                )
            _assert_fixed_error(captured.value, TypeError, INCUMBENT_TYPE_ERROR)
            _assert_production_frames_hide(captured.value, config, wrong_incumbent)
    assert calls == []
    assert _state_snapshot(incumbent)[3] == 4040


def test_captured_slot_getters_survive_public_replacement_and_run_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, incumbent = _valid_pair(monkeypatch, tmp_path)
    canonical_config_reader = port_module._read_requested_port
    canonical_state_reader = port_module._read_incumbent_port
    calls: list[str] = []

    def read_config(value: SidecarRuntimeConfig) -> object:
        calls.append("config")
        return canonical_config_reader(value)

    def read_state(value: SidecarState) -> object:
        calls.append("incumbent")
        return canonical_state_reader(value)

    def redirected(_value: object) -> NoReturn:
        raise AssertionError("public slot replacement ran")

    monkeypatch.setattr(port_module, "_read_requested_port", read_config)
    monkeypatch.setattr(port_module, "_read_incumbent_port", read_state)
    monkeypatch.setattr(sidecar_package, "SidecarRuntimeConfig", object)
    monkeypatch.setattr(sidecar_package, "SidecarState", object)
    monkeypatch.setattr(config_module, "SidecarRuntimeConfig", object)
    monkeypatch.setattr(state_module, "SidecarState", object)
    monkeypatch.setattr(port_module, "SidecarRuntimeConfig", object)
    monkeypatch.setattr(port_module, "SidecarState", object)
    monkeypatch.setattr(SidecarRuntimeConfig, "requested_port", property(redirected))
    monkeypatch.setattr(SidecarState, "port", property(redirected))

    result = admit_configured_incumbent_port(config, incumbent)

    assert result is incumbent
    assert calls == ["config", "incumbent"]


@pytest.mark.parametrize(
    ("requested_port", "incumbent_port", "accepted"),
    [
        (None, 1, True),
        (None, 4040, True),
        (None, 65_535, True),
        (0, 1, True),
        (0, 4040, True),
        (0, 65_535, True),
        (1, 1, True),
        (4040, 4040, True),
        (65_535, 65_535, True),
        (1, 2, False),
        (4040, 4041, False),
        (65_535, 1, False),
    ],
)
def test_port_policy_matrix_returns_identity_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    requested_port: int | None,
    incumbent_port: int,
    accepted: bool,
) -> None:
    config, incumbent = _valid_pair(
        monkeypatch,
        tmp_path,
        requested_port=requested_port,
        incumbent_port=incumbent_port,
    )
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"unchanged")
    before_tree = _tree_signature(tmp_path)
    before_config = _config_snapshot(config)
    before_state = _state_snapshot(incumbent)
    capsys.readouterr()
    caplog.clear()

    if accepted:
        assert admit_configured_incumbent_port(config, incumbent) is incumbent
    else:
        with pytest.raises(RuntimeError) as captured:
            admit_configured_incumbent_port(config, incumbent)
        _assert_fixed_error(captured.value, RuntimeError, PORT_ERROR)
        _assert_production_frames_hide(
            captured.value,
            config,
            incumbent,
            requested_port,
            incumbent_port,
        )

    assert _config_snapshot(config) == before_config
    assert _state_snapshot(incumbent) == before_state
    assert _tree_signature(tmp_path) == before_tree
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.parametrize(
    ("target", "value", "requested_port"),
    [
        pytest.param("config", _MISSING, 4040, id="config-missing"),
        pytest.param("config", True, 4040, id="config-bool"),
        pytest.param("config", _IntSubclass(0), 4040, id="config-int-subclass"),
        pytest.param("config", -1, 4040, id="config-negative"),
        pytest.param("config", 65_536, 4040, id="config-too-large"),
        pytest.param("config", 1.0, 4040, id="config-float"),
        pytest.param("incumbent", _MISSING, None, id="incumbent-missing"),
        pytest.param("incumbent", None, None, id="incumbent-none"),
        pytest.param("incumbent", True, 1, id="incumbent-bool-equal"),
        pytest.param(
            "incumbent",
            _IntSubclass(1),
            1,
            id="incumbent-int-subclass-equal",
        ),
        pytest.param("incumbent", 0, None, id="incumbent-zero"),
        pytest.param("incumbent", 65_536, None, id="incumbent-too-large"),
        pytest.param("incumbent", 1.0, 1, id="incumbent-float-equal"),
    ],
)
def test_malformed_exact_port_slots_fail_closed_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    value: object,
    requested_port: int | None,
) -> None:
    config, incumbent = _valid_pair(
        monkeypatch,
        tmp_path,
        requested_port=requested_port,
    )
    if target == "config":
        config = _forge_slot_instance(
            SidecarRuntimeConfig,
            "requested_port",
            value,
        )  # type: ignore[assignment]
        expected_calls = ["config"]
    else:
        incumbent = _forge_slot_instance(
            SidecarState,
            "port",
            value,
        )  # type: ignore[assignment]
        expected_calls = ["config", "incumbent"]

    canonical_config_reader = port_module._read_requested_port
    canonical_state_reader = port_module._read_incumbent_port
    calls: list[str] = []

    def read_config(candidate: SidecarRuntimeConfig) -> object:
        calls.append("config")
        return canonical_config_reader(candidate)

    def read_state(candidate: SidecarState) -> object:
        calls.append("incumbent")
        return canonical_state_reader(candidate)

    monkeypatch.setattr(port_module, "_read_requested_port", read_config)
    monkeypatch.setattr(port_module, "_read_incumbent_port", read_state)
    with pytest.raises(RuntimeError) as captured:
        admit_configured_incumbent_port(config, incumbent)

    _assert_fixed_error(captured.value, RuntimeError, PORT_ERROR)
    _assert_production_frames_hide(captured.value, config, incumbent, value)
    assert calls == expected_calls


@pytest.mark.parametrize("target", ["config", "incumbent"])
def test_malformed_slots_never_dispatch_scalar_protocols(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
) -> None:
    config, incumbent = _valid_pair(
        monkeypatch,
        tmp_path,
        requested_port=None if target == "incumbent" else 4040,
    )
    hostile = _HostileScalar()
    if target == "config":
        config = _forge_slot_instance(
            SidecarRuntimeConfig,
            "requested_port",
            hostile,
        )  # type: ignore[assignment]
    else:
        incumbent = _forge_slot_instance(
            SidecarState,
            "port",
            hostile,
        )  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as captured:
        admit_configured_incumbent_port(config, incumbent)

    _assert_fixed_error(captured.value, RuntimeError, PORT_ERROR)
    _assert_production_frames_hide(captured.value, config, incumbent, hostile)
    assert hostile.calls == []


@pytest.mark.parametrize("stage", ["config", "incumbent"])
def test_ordinary_getter_failure_is_private_and_collectable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    config, incumbent = _valid_pair(monkeypatch, tmp_path)
    secret = f"{stage}-getter-secret /private/incumbent-port"
    raw_error = _TrackedFailure(secret)
    raw_reference = weakref.ref(raw_error)
    raw_errors = [raw_error]
    calls: list[str] = []
    canonical_config_reader = port_module._read_requested_port
    canonical_state_reader = port_module._read_incumbent_port

    def fail_config(_candidate: SidecarRuntimeConfig) -> NoReturn:
        calls.append("config")
        raise raw_errors[0]

    def fail_state(_candidate: SidecarState) -> NoReturn:
        calls.append("incumbent")
        raise raw_errors[0]

    def read_config(candidate: SidecarRuntimeConfig) -> object:
        calls.append("config")
        return canonical_config_reader(candidate)

    def read_state(candidate: SidecarState) -> object:
        calls.append("incumbent")
        return canonical_state_reader(candidate)

    with monkeypatch.context() as getter_patch:
        if stage == "config":
            getter_patch.setattr(port_module, "_read_requested_port", fail_config)
            getter_patch.setattr(port_module, "_read_incumbent_port", read_state)
            expected_calls = ["config"]
        else:
            getter_patch.setattr(port_module, "_read_requested_port", read_config)
            getter_patch.setattr(port_module, "_read_incumbent_port", fail_state)
            expected_calls = ["config", "incumbent"]
        with pytest.raises(RuntimeError) as captured:
            admit_configured_incumbent_port(config, incumbent)

    error = captured.value
    _assert_fixed_error(error, RuntimeError, PORT_ERROR)
    _assert_production_frames_hide(error, config, incumbent, raw_error, 4040)
    assert secret not in "".join(traceback.format_exception(error))
    assert calls == expected_calls
    raw_errors.clear()
    del error, captured, raw_error, fail_config, fail_state, read_config, read_state
    gc.collect()
    assert raw_reference() is None


@pytest.mark.parametrize("stage", ["config", "incumbent"])
@pytest.mark.parametrize("control_factory", [KeyboardInterrupt, SystemExit, _Control])
def test_process_control_preserves_identity_and_scrubs_production_locals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stage: str,
    control_factory: type[BaseException],
) -> None:
    config, incumbent = _valid_pair(monkeypatch, tmp_path)
    control = control_factory("control-payload")
    control.add_note("existing-control-note")
    calls: list[str] = []
    canonical_config_getter = port_module._CONFIG_PORT_GETTER

    def interrupt(_candidate: object, _owner: type[object]) -> NoReturn:
        calls.append(stage)
        raise control

    def read_config(candidate: object, owner: type[object]) -> object:
        calls.append("config")
        return canonical_config_getter(candidate, owner)

    def forbidden_state(_candidate: object, _owner: type[object]) -> NoReturn:
        calls.append("unexpected-incumbent")
        raise AssertionError("incumbent getter ran after control")

    capsys.readouterr()
    with monkeypatch.context() as control_patch:
        if stage == "config":
            control_patch.setattr(port_module, "_CONFIG_PORT_GETTER", interrupt)
            control_patch.setattr(port_module, "_STATE_PORT_GETTER", forbidden_state)
            expected_calls = ["config"]
        else:
            control_patch.setattr(port_module, "_CONFIG_PORT_GETTER", read_config)
            control_patch.setattr(port_module, "_STATE_PORT_GETTER", interrupt)
            expected_calls = ["config", "incumbent"]
        with pytest.raises(control_factory) as captured:
            admit_configured_incumbent_port(config, incumbent)

    assert captured.value is control
    assert control.args == ("control-payload",)
    assert control.__notes__ == ["existing-control-note"]
    assert calls == expected_calls
    assert all(local_values == {} for _name, local_values in _production_frames(control))
    traceback_names: list[str] = []
    current = control.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "interrupt" in traceback_names
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("baseline_factory", [ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("outcome", ["success", "fixed", "control"])
def test_caller_baseline_does_not_select_the_port_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    baseline_factory: type[BaseException],
    outcome: str,
) -> None:
    incumbent_port = 4041 if outcome == "fixed" else 4040
    config, incumbent = _valid_pair(
        monkeypatch,
        tmp_path,
        requested_port=4040,
        incumbent_port=incumbent_port,
    )
    baseline = baseline_factory("caller-baseline")
    baseline.add_note("caller-note")
    control = _Control("dependency-control")

    def interrupt(_candidate: SidecarState) -> NoReturn:
        raise control

    observed: BaseException | None = None
    result: SidecarState | None = None
    with monkeypatch.context() as baseline_patch:
        if outcome == "control":
            baseline_patch.setattr(port_module, "_read_incumbent_port", interrupt)
        try:
            raise baseline
        except BaseException as active:
            assert active is baseline
            assert sys.exception() is baseline
            try:
                result = admit_configured_incumbent_port(config, incumbent)
            except BaseException as error:
                observed = error
            assert sys.exception() is baseline

    assert baseline.__notes__ == ["caller-note"]
    if outcome == "success":
        assert observed is None
        assert result is incumbent
    elif outcome == "fixed":
        assert observed is not None
        _assert_fixed_error(observed, RuntimeError, PORT_ERROR, context=baseline)
    else:
        assert observed is control
        assert result is None


def test_production_ast_is_exactly_the_pure_port_boundary() -> None:
    source = Path(port_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
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
        (0, "collections.abc", (("Callable", None),)),
        (0, "typing", (("Final", None), ("NoReturn", None), ("cast", None))),
        (1, "runtime_config", (("SidecarRuntimeConfig", None),)),
        (1, "state", (("SidecarState", None),)),
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
                ast.TypeAlias,
                ast.AnnAssign,
                ast.ClassDef,
                ast.FunctionDef,
            ),
        )
        for node in tree.body
    )
    assert not any(isinstance(node, ast.Assign) for node in tree.body)

    aliases = [node for node in tree.body if isinstance(node, ast.TypeAlias)]
    assert len(aliases) == 1
    assert isinstance(aliases[0].name, ast.Name) and aliases[0].name.id == "_BoundSlotGetter"
    bindings = {
        node.target.id: node
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert set(bindings) == {
        "_CONFIG_TYPE",
        "_STATE_TYPE",
        "_CONFIG_PORT_GETTER",
        "_STATE_PORT_GETTER",
    }
    assert isinstance(bindings["_CONFIG_TYPE"].value, ast.Name)
    assert bindings["_CONFIG_TYPE"].value.id == "SidecarRuntimeConfig"
    assert isinstance(bindings["_STATE_TYPE"].value, ast.Name)
    assert bindings["_STATE_TYPE"].value.id == "SidecarState"
    for name, key in (
        ("_CONFIG_PORT_GETTER", "requested_port"),
        ("_STATE_PORT_GETTER", "port"),
    ):
        value = bindings[name].value
        assert isinstance(value, ast.Call) and _call_name(value) == "cast"
        assert len(value.args) == 2
        getter = value.args[1]
        assert isinstance(getter, ast.Attribute) and getter.attr == "__get__"
        assert isinstance(getter.value, ast.Subscript)
        assert isinstance(getter.value.slice, ast.Constant)
        assert getter.value.slice.value == key

    module_functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert set(module_functions) == {
        "_read_requested_port",
        "_read_incumbent_port",
        "_admit_port_stage",
        "_raise_config_type",
        "_raise_incumbent_type",
        "_raise_incompatible",
        "admit_configured_incumbent_port",
    }
    module_classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert set(module_classes) == {"_PortAdmissionFailure"}
    marker = module_classes["_PortAdmissionFailure"]
    assert len(marker.bases) == 1
    assert isinstance(marker.bases[0], ast.Name) and marker.bases[0].id == "Exception"
    assert len(marker.body) == 1 and isinstance(marker.body[0], ast.Pass)
    assert marker.decorator_list == [] and marker.keywords == []
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
                ast.Match,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.NamedExpr,
                ast.Yield,
                ast.YieldFrom,
                ast.Await,
                ast.JoinedStr,
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
        "_read_requested_port": ["_CONFIG_PORT_GETTER"],
        "_read_incumbent_port": ["_STATE_PORT_GETTER"],
        "_admit_port_stage": sorted(
            ["_read_requested_port", "type", "_read_incumbent_port", "type"]
        ),
        "_raise_config_type": ["TypeError"],
        "_raise_incumbent_type": ["TypeError"],
        "_raise_incompatible": ["RuntimeError"],
        "admit_configured_incumbent_port": sorted(
            [
                "type",
                "_raise_config_type",
                "type",
                "_raise_incumbent_type",
                "_admit_port_stage",
                "_raise_incompatible",
            ]
        ),
    }
    assert all("<dynamic>" not in calls for calls in calls_by_function.values())
    top_level_calls = [
        node
        for statement in tree.body
        if isinstance(statement, ast.AnnAssign)
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
    ]
    assert [_call_name(call) for call in top_level_calls] == ["cast", "cast"]

    forbidden_names = {
        "StateStore",
        "OwnerLock",
        "discover_existing_startup",
        "probe_sidecar_health",
        "wait_for_owner_election",
        "resolve_owner_election",
        "startup_timeout",
        "time",
        "sleep",
        "listener",
        "startup_channel",
        "subprocess",
        "threading",
        "asyncio",
        "getattr",
        "setattr",
        "hasattr",
        "vars",
        "repr",
        "print",
        "logging",
        "eval",
        "exec",
        "compile",
        "__import__",
    }
    assert not any(
        isinstance(node, ast.Name) and node.id in forbidden_names for node in ast.walk(tree)
    )
    attribute_names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert attribute_names == {"__dict__", "__get__"}

    stage = module_functions["_admit_port_stage"]
    stage_returns = [node for node in ast.walk(stage) if isinstance(node, ast.Return)]
    assert len(stage_returns) == 1
    assert isinstance(stage_returns[0].value, ast.Name)
    assert stage_returns[0].value.id == "incumbent"
    public = module_functions["admit_configured_incumbent_port"]
    public_returns = [node for node in ast.walk(public) if isinstance(node, ast.Return)]
    assert len(public_returns) == 1
    assert isinstance(public_returns[0].value, ast.Call)
    assert _call_name(public_returns[0].value) == "_admit_port_stage"

    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert handlers
    assert all(node.name is None and node.type is not None for node in handlers)
    base_handlers = [
        node
        for node in handlers
        if isinstance(node.type, ast.Name) and node.type.id == "BaseException"
    ]
    assert len(base_handlers) == 4
    assert all(
        isinstance(node.body[-1], ast.Raise) and node.body[-1].exc is None for node in base_handlers
    )
    assert all(
        any(isinstance(statement, ast.Delete) for statement in node.body[:-1])
        for node in base_handlers
    )
    assert not any(isinstance(node, ast.Try) and node.finalbody for node in ast.walk(tree))

    for error_helper, expected in (
        ("_raise_config_type", "TypeError"),
        ("_raise_incumbent_type", "TypeError"),
        ("_raise_incompatible", "RuntimeError"),
    ):
        raises = [
            node for node in ast.walk(module_functions[error_helper]) if isinstance(node, ast.Raise)
        ]
        assert len(raises) == 1
        assert isinstance(raises[0].exc, ast.Call)
        assert _call_name(raises[0].exc) == expected
        assert isinstance(raises[0].cause, ast.Constant) and raises[0].cause.value is None
