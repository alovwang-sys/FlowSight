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


def test_public_api_exports_flowsight() -> None:
    assert "FlowSight" in flowsight.__all__
    assert flowsight.FlowSight is FlowSight


def test_constructor_keeps_local_configuration_without_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unexpected_runtime_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("TRIAL-001 must not start runtime infrastructure")

    app = FastAPI()
    monkeypatch.setattr(threading, "Thread", unexpected_runtime_call)
    monkeypatch.setattr(subprocess, "Popen", unexpected_runtime_call)
    monkeypatch.setattr(socket, "socket", unexpected_runtime_call)
    monkeypatch.setattr(sqlite3, "connect", unexpected_runtime_call)
    monkeypatch.setattr(webbrowser, "open", unexpected_runtime_call)

    sdk = FlowSight(project_root=tmp_path, ui_port=4040)
    sdk.init_app(app)

    assert sdk.project_root == tmp_path
    assert sdk.ui_port == 4040


def test_init_app_is_callable_idempotent_and_does_not_mutate_fastapi() -> None:
    app = FastAPI()
    sdk = FlowSight()
    routes_before = tuple(app.routes)
    middleware_before = tuple(app.user_middleware)
    state_before = dict(app.state._state)

    assert sdk.init_app(app) is None
    assert sdk.init_app(app) is None

    assert tuple(app.routes) == routes_before
    assert tuple(app.user_middleware) == middleware_before
    assert app.state._state == state_before


def test_init_app_rejects_non_fastapi_objects() -> None:
    with pytest.raises(TypeError, match="fastapi.FastAPI"):
        FlowSight().init_app(object())  # type: ignore[arg-type]
