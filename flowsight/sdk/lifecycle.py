"""Phase 0 SDK lifecycle contract."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from weakref import WeakSet

from fastapi import FastAPI

from flowsight.sidecar import (
    SidecarState,
    prepare_sidecar_runtime_config,
    start_or_attach_sidecar,
)

_PREPARE_SIDECAR_RUNTIME_CONFIG = prepare_sidecar_runtime_config
_START_OR_ATTACH_SIDECAR = start_or_attach_sidecar


class FlowSight:
    """Configure FlowSight for a local FastAPI application."""

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        ui_port: int | None = None,
    ) -> None:
        self.project_root = Path.cwd() if project_root is None else Path(project_root)
        self.ui_port = ui_port
        self._initialized_apps: WeakSet[FastAPI] = WeakSet()
        self._initialization_lock = Lock()
        self._sidecar_state: SidecarState | None = None

    def init_app(self, app: FastAPI) -> None:
        """Start or attach the project sidecar and register one FastAPI app."""

        if not isinstance(app, FastAPI):
            raise TypeError("FlowSight.init_app() requires a fastapi.FastAPI instance")
        with self._initialization_lock:
            if app in self._initialized_apps:
                return
            config = _PREPARE_SIDECAR_RUNTIME_CONFIG(
                self.project_root,
                requested_port=self.ui_port,
            )
            state = _START_OR_ATTACH_SIDECAR(config)
            if type(state) is not SidecarState:
                raise RuntimeError("FlowSight.init_app() failed to admit a sidecar") from None
            self._initialized_apps.add(app)
            self._sidecar_state = state
