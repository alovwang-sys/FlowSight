"""Minimal Phase 0 SDK lifecycle contract."""

from __future__ import annotations

from pathlib import Path
from weakref import WeakSet

from fastapi import FastAPI


class FlowSight:
    """Configure FlowSight for a local FastAPI application.

    TRIAL-001 intentionally stops at the public lifecycle shape. It does not
    start a sidecar, open a port, install instrumentation, or write storage.
    """

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        ui_port: int | None = None,
    ) -> None:
        self.project_root = Path.cwd() if project_root is None else Path(project_root)
        self.ui_port = ui_port
        self._initialized_apps: WeakSet[FastAPI] = WeakSet()

    def init_app(self, app: FastAPI) -> None:
        """Register a FastAPI app idempotently without collection side effects."""

        if not isinstance(app, FastAPI):
            raise TypeError("FlowSight.init_app() requires a fastapi.FastAPI instance")
        if app in self._initialized_apps:
            return
        self._initialized_apps.add(app)
