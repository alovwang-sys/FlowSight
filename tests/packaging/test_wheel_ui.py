from __future__ import annotations

import functools
import http.server
import os
import re
import shutil
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

_ASSET_REFERENCE = re.compile(r'(?:href|src)="([^"]+)"')


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _clean_wheel_context() -> tuple[Path, Path]:
    expected_site_packages_raw = os.environ.get("FLOWSIGHT_EXPECTED_SITE_PACKAGES")
    if expected_site_packages_raw is None:
        pytest.skip("the isolated wheel probe is run separately by make check")

    expected_site_packages = Path(expected_site_packages_raw).resolve()
    workspace_root = Path(os.environ["FLOWSIGHT_WORKSPACE_ROOT"]).resolve()

    assert expected_site_packages.name in {"site-packages", "dist-packages"}
    assert expected_site_packages.is_relative_to(Path(sys.prefix).resolve())
    assert not expected_site_packages.is_relative_to(workspace_root)
    if os.environ.get("FLOWSIGHT_EXPECT_NO_NODE") == "1":
        assert shutil.which("node") is None

    return expected_site_packages, workspace_root


def test_clean_wheel_serves_bundled_ui_without_workspace_or_node() -> None:
    expected_site_packages, workspace_root = _clean_wheel_context()

    import flowsight

    module_file = Path(flowsight.__file__).resolve()
    assert module_file.is_relative_to(expected_site_packages)
    assert not module_file.is_relative_to(workspace_root)

    static_root = module_file.parent / "static"
    index_path = static_root / "index.html"
    assert index_path.is_file()
    assert not list(static_root.rglob("*.map"))

    index = index_path.read_text(encoding="utf-8")
    references = _ASSET_REFERENCE.findall(index)
    assert references
    assert all(reference.startswith("./assets/") for reference in references)
    assert all((static_root / reference.removeprefix("./")).is_file() for reference in references)
    bundled_assets = {
        f"./{path.relative_to(static_root).as_posix()}"
        for path in static_root.joinpath("assets").iterdir()
        if path.is_file()
    }
    assert bundled_assets == set(references)

    handler = functools.partial(_QuietStaticHandler, directory=str(static_root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="wheel-ui-probe", daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/", timeout=2.0) as response:
            served_index = response.read().decode("utf-8")
        assert served_index == index

        with opener.open(
            f"http://{host}:{port}/{references[0].removeprefix('./')}", timeout=2.0
        ) as response:
            assert response.read(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
