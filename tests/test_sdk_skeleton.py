from __future__ import annotations

import socket
import sqlite3
import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI

import flowsight
from flowsight import FlowSight
from flowsight import sdk as sdk_package
from flowsight.sdk import lifecycle as lifecycle_module
from flowsight.sidecar import SidecarState


def _state(tmp_path: Path, *, token: str = "s" * 32) -> SidecarState:
    return SidecarState(
        project_id="project",
        startup_id="startup",
        pid=12345,
        port=4040,
        token=token,
        database_path=str((tmp_path / "flowsight.sqlite3").absolute()),
        started_at_ns=1,
    )


def test_public_api_exports_flowsight() -> None:
    assert "FlowSight" in flowsight.__all__
    assert flowsight.FlowSight is FlowSight


def test_constructor_keeps_local_configuration_without_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unexpected_runtime_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("TRIAL-001 must not start runtime infrastructure")

    monkeypatch.setattr(threading, "Thread", unexpected_runtime_call)
    monkeypatch.setattr(subprocess, "Popen", unexpected_runtime_call)
    monkeypatch.setattr(socket, "socket", unexpected_runtime_call)
    monkeypatch.setattr(sqlite3, "connect", unexpected_runtime_call)
    monkeypatch.setattr(webbrowser, "open", unexpected_runtime_call)

    sdk = FlowSight(project_root=tmp_path, ui_port=4040)

    assert sdk.project_root == tmp_path
    assert sdk.ui_port == 4040


def test_init_app_starts_once_with_exact_configuration_and_does_not_mutate_fastapi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FastAPI()
    sdk = FlowSight(project_root=tmp_path, ui_port=4040)
    config = object()
    state = _state(tmp_path)
    events: list[tuple[object, ...]] = []

    def prepare(project_root: object, *, requested_port: object) -> object:
        events.append(("prepare", project_root, requested_port))
        return config

    def start(actual_config: object) -> SidecarState:
        events.append(("start", actual_config))
        return state

    monkeypatch.setattr(lifecycle_module, "_PREPARE_SIDECAR_RUNTIME_CONFIG", prepare)
    monkeypatch.setattr(lifecycle_module, "_START_OR_ATTACH_SIDECAR", start)
    monkeypatch.setattr(
        webbrowser,
        "open",
        lambda *_args, **_kwargs: pytest.fail("init_app must not open a browser"),
    )
    routes_before = tuple(app.routes)
    middleware_before = tuple(app.user_middleware)
    state_before = dict(app.state._state)

    assert sdk.init_app(app) is None
    assert sdk.init_app(app) is None

    assert events == [("prepare", tmp_path, 4040), ("start", config)]
    assert tuple(app.routes) == routes_before
    assert tuple(app.user_middleware) == middleware_before
    assert app.state._state == state_before
    assert not hasattr(sdk, "sidecar_state")
    assert state.token not in repr(sdk)


def test_init_app_failure_is_retryable_and_commits_only_exact_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FastAPI()
    sdk = FlowSight(project_root=tmp_path)
    config = object()
    state = _state(tmp_path)
    prepare_calls: list[object] = []
    start_calls: list[object] = []

    def prepare(project_root: object, *, requested_port: object) -> object:
        assert requested_port is None
        prepare_calls.append(project_root)
        return config

    def start(actual_config: object) -> SidecarState:
        start_calls.append(actual_config)
        if len(start_calls) == 1:
            raise RuntimeError("sidecar parent startup failed")
        return state

    monkeypatch.setattr(lifecycle_module, "_PREPARE_SIDECAR_RUNTIME_CONFIG", prepare)
    monkeypatch.setattr(lifecycle_module, "_START_OR_ATTACH_SIDECAR", start)

    with pytest.raises(RuntimeError, match="sidecar parent startup failed"):
        sdk.init_app(app)
    assert sdk.init_app(app) is None
    assert sdk.init_app(app) is None

    assert prepare_calls == [tmp_path, tmp_path]
    assert start_calls == [config, config]

    second = FlowSight(project_root=tmp_path)
    monkeypatch.setattr(lifecycle_module, "_START_OR_ATTACH_SIDECAR", lambda _config: object())
    with pytest.raises(RuntimeError, match="failed to admit a sidecar"):
        second.init_app(app)


def test_concurrent_duplicate_init_dispatches_one_startup_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = FastAPI()
    sdk = FlowSight(project_root=tmp_path)
    config = object()
    state = _state(tmp_path)
    callers_ready = threading.Barrier(3)
    startup_entered = threading.Event()
    release_startup = threading.Event()
    start_calls: list[object] = []
    results: list[object] = [None, None]

    monkeypatch.setattr(
        lifecycle_module,
        "_PREPARE_SIDECAR_RUNTIME_CONFIG",
        lambda *_args, **_kwargs: config,
    )

    def start(actual_config: object) -> SidecarState:
        start_calls.append(actual_config)
        startup_entered.set()
        assert release_startup.wait(5.0)
        return state

    def call(index: int) -> None:
        try:
            callers_ready.wait()
            results[index] = sdk.init_app(app)
        except BaseException as error:  # noqa: BLE001 - retained for the assertion
            results[index] = error

    monkeypatch.setattr(lifecycle_module, "_START_OR_ATTACH_SIDECAR", start)
    callers = [threading.Thread(target=call, args=(index,)) for index in range(2)]
    for caller in callers:
        caller.start()
    callers_ready.wait()
    assert startup_entered.wait(5.0)
    release_startup.set()
    for caller in callers:
        caller.join(5.0)

    assert all(not caller.is_alive() for caller in callers)
    assert results == [None, None]
    assert start_calls == [config]


def test_init_app_rejects_non_fastapi_objects() -> None:
    with pytest.raises(TypeError, match="fastapi.FastAPI"):
        FlowSight().init_app(object())  # type: ignore[arg-type]


def test_sdk_public_surface_stays_limited_to_flowsight() -> None:
    assert sdk_package.__all__ == ["FlowSight"]
    assert flowsight.__all__ == ["FlowSight"]
