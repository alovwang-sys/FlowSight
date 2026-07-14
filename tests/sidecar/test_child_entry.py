from __future__ import annotations

import ast
import inspect
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

import flowsight.sidecar as sidecar_package
from flowsight.sidecar import child_entry as entry_module
from flowsight.sidecar import child_runtime as runtime_module


class _Control(BaseException):
    pass


def test_private_entry_adds_no_sidecar_public_surface() -> None:
    assert "child_entry" not in sidecar_package.__all__
    public_functions = [
        name
        for name, value in vars(entry_module).items()
        if inspect.isfunction(value) and not name.startswith("_")
    ]
    assert public_functions == []


def test_import_time_capture_is_not_redirected_by_runtime_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = entry_module._RUN_SIDECAR_CHILD

    def replacement(_arguments: tuple[str, ...]) -> None:
        raise AssertionError("replacement must not run")

    monkeypatch.setattr(runtime_module, "run_sidecar_child", replacement)

    assert entry_module._RUN_SIDECAR_CHILD is captured


def test_module_execution_forwards_one_exact_suffix_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run_child(arguments: tuple[str, ...]) -> None:
        calls.append(arguments)

    monkeypatch.setattr(runtime_module, "run_sidecar_child", run_child)
    monkeypatch.setattr(sys, "argv", ["child-entry", "first", "second"])
    monkeypatch.delitem(sys.modules, "flowsight.sidecar.child_entry")

    runpy.run_module("flowsight.sidecar.child_entry", run_name="__main__")

    assert calls == [("first", "second")]


@pytest.mark.parametrize("error", [RuntimeError("fixed error"), _Control("control")])
def test_module_execution_preserves_error_and_control_identity(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    def run_child(_arguments: tuple[str, ...]) -> None:
        raise error

    monkeypatch.setattr(runtime_module, "run_sidecar_child", run_child)
    monkeypatch.setattr(sys, "argv", ["child-entry", "opaque"])
    monkeypatch.delitem(sys.modules, "flowsight.sidecar.child_entry")

    with pytest.raises(type(error)) as captured:
        runpy.run_module("flowsight.sidecar.child_entry", run_name="__main__")

    assert captured.value is error


def test_isolated_module_execution_leaks_no_malformed_suffix(
    tmp_path: Path,
) -> None:
    opaque_suffix = "private-bootstrap-suffix"
    completed = subprocess.run(
        (
            sys.executable,
            "-I",
            "-m",
            "flowsight.sidecar.child_entry",
            opaque_suffix,
        ),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert b"sidecar child transaction failed" in completed.stderr
    assert opaque_suffix.encode() not in completed.stderr


def test_child_entry_ast_freezes_opaque_forwarding_boundary() -> None:
    source = Path(entry_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert imports == {"sys"}
    imported_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported_modules == {"__future__", "typing", "child_runtime"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert [node.name for node in functions] == ["_execute"]
    assert functions[0].args.args == []
    assert functions[0].args.kwonlyargs == []
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Lambda)]
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert sum(isinstance(call.func, ast.Name) and call.func.id == "tuple" for call in calls) == 1
    assert (
        sum(
            isinstance(call.func, ast.Name) and call.func.id == "_RUN_SIDECAR_CHILD"
            for call in calls
        )
        == 1
    )
    tuple_call = next(
        call for call in calls if isinstance(call.func, ast.Name) and call.func.id == "tuple"
    )
    assert len(tuple_call.args) == 1
    suffix = tuple_call.args[0]
    assert isinstance(suffix, ast.Subscript)
    assert isinstance(suffix.value, ast.Attribute)
    assert isinstance(suffix.value.value, ast.Name)
    assert suffix.value.value.id == "sys"
    assert suffix.value.attr == "argv"
    assert isinstance(suffix.slice, ast.Slice)
    assert isinstance(suffix.slice.lower, ast.Constant)
    assert suffix.slice.lower.value == 1
    assert suffix.slice.upper is None
    forbidden_names = {
        "argparse",
        "os",
        "subprocess",
        "signal",
        "print",
        "logging",
        "sqlite3",
        "uvicorn",
    }
    assert not any(
        isinstance(node, ast.Name) and node.id in forbidden_names for node in ast.walk(tree)
    )
