from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.resources
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import site
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_INDEX = "https://pypi.org/simple"
PYTEST_REQUIREMENT = "pytest==9.1.1"

BUILD_TIMEOUT_SECONDS = 180
VENV_TIMEOUT_SECONDS = 60
INSTALL_TIMEOUT_SECONDS = 240
PROBE_TIMEOUT_SECONDS = 60
PREFLIGHT_TIMEOUT_SECONDS = 30
TERMINATE_GRACE_SECONDS = 1
REAP_TIMEOUT_SECONDS = 5

_MANIFEST_NAME = "probe-manifest.json"
_INNER_MODE = Path.cwd().name == "probe" and (Path.cwd() / _MANIFEST_NAME).is_file()
_GENERATED_PARTS = {"__pycache__", "build", "dist"}
_GENERATED_SUFFIXES = (".egg-info", ".dist-info")
_GENERATED_FILE_SUFFIXES = (".pyc", ".pyo", ".whl")
_ASSET_REFERENCE = re.compile(rb"(?:src|href)=\"(/assets/[^\"]+)\"")

_PREINSTALL_PROBE = r"""
import importlib.metadata
import importlib.util
from pathlib import Path
import site
import sys

expected_venv = Path(sys.argv[1])
forbidden_roots = tuple(Path(value).resolve() for value in sys.argv[2:])

assert Path(sys.executable) == expected_venv / "bin" / "python"
assert Path(sys.prefix).resolve() == expected_venv
assert sys.base_prefix != sys.prefix
assert site.ENABLE_USER_SITE is False

assert importlib.util.find_spec("flowsight") is None
try:
    importlib.metadata.distribution("flowsight")
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError("FlowSight unexpectedly exists in the fresh environment")

for entry in sys.path:
    if entry:
        resolved = Path(entry).resolve()
        assert all(not resolved.is_relative_to(root) for root in forbidden_roots)
"""


class _CommandFailure(AssertionError):
    pass


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(process_group_id: int) -> None:
    deadline = time.monotonic() + REAP_TIMEOUT_SECONDS
    while _process_group_exists(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise AssertionError("command process group could not be reaped")
        threading.Event().wait(min(0.01, remaining))


def _terminate_process_group(process: subprocess.Popen[str]) -> str:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    output = ""
    try:
        output, _ = process.communicate(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if not _process_group_exists(process.pid):
        if process.returncode is None:
            try:
                output, _ = process.communicate(timeout=REAP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                raise AssertionError("command process group could not be reaped") from None
        return output

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        output, _ = process.communicate(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise AssertionError("command process group could not be reaped") from None
    _wait_for_process_group_exit(process.pid)
    return output


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
) -> str:
    exact_command = tuple(command)
    assert exact_command
    assert all(type(argument) is str and argument for argument in exact_command)
    process = subprocess.Popen(
        exact_command,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        start_new_session=True,
    )
    assert os.getpgid(process.pid) == process.pid
    assert process.pid != os.getpgrp()
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        output = _terminate_process_group(process)
        raise _CommandFailure(
            f"command timed out after {timeout} seconds: {shlex.join(exact_command)}\n{output}"
        ) from None
    except BaseException as control:
        try:
            _terminate_process_group(process)
        except BaseException as cleanup_error:
            control.add_note(f"process-group cleanup failed: {type(cleanup_error).__name__}")
        raise

    if _process_group_exists(process.pid):
        descendant_output = _terminate_process_group(process)
        raise _CommandFailure(
            f"command left a running descendant: {shlex.join(exact_command)}\n"
            f"{output}{descendant_output}"
        )
    if process.returncode != 0:
        raise _CommandFailure(
            f"command exited {process.returncode}: {shlex.join(exact_command)}\n{output}"
        )
    return output


def _controlled_environment(temporary_root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON", "XDG_"))
        and key
        not in {
            "HOME",
            "NETRC",
            "SSH_AUTH_SOCK",
            "VIRTUAL_ENV",
            "__PYVENV_LAUNCHER__",
        }
    }
    controlled_root = temporary_root.resolve()
    home = controlled_root / "home"
    xdg_config = controlled_root / "xdg-config"
    xdg_cache = controlled_root / "xdg-cache"
    xdg_data = controlled_root / "xdg-data"
    pip_cache = controlled_root / "pip-cache"
    for directory in (home, xdg_config, xdg_cache, xdg_data, pip_cache):
        directory.mkdir(parents=True, exist_ok=False)
    environment.update(
        {
            "HOME": str(home),
            "PIP_CACHE_DIR": str(pip_cache),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DEFAULT_TIMEOUT": "15",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_INDEX_URL": PUBLIC_INDEX,
            "PIP_KEYRING_PROVIDER": "disabled",
            "PIP_NO_INPUT": "1",
            "PIP_RETRIES": "2",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_DATA_HOME": str(xdg_data),
        }
    )
    return environment


def _is_generated(relative_path: Path) -> bool:
    return (
        any(part in _GENERATED_PARTS for part in relative_path.parts)
        or any(part.endswith(_GENERATED_SUFFIXES) for part in relative_path.parts)
        or relative_path.name.endswith(_GENERATED_FILE_SUFFIXES)
    )


def _is_eligible_package_file(relative_path: Path) -> bool:
    if relative_path.suffix == ".py":
        return True
    if relative_path == Path("flowsight/py.typed"):
        return True
    if relative_path == Path("flowsight/static/index.html"):
        return True
    return relative_path.parent == Path("flowsight/static/assets")


def _copy_wheel_inputs(repository_root: Path, destination: Path) -> set[Path]:
    resolved_repository = repository_root.resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=False)
    resolved_destination = destination.resolve(strict=True)
    assert not resolved_destination.is_relative_to(resolved_repository)

    copied: set[Path] = set()

    def copy_regular(source: Path, relative_path: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise AssertionError(f"wheel build input must be regular: {relative_path}")
        resolved_source = source.resolve(strict=True)
        assert resolved_source.is_relative_to(resolved_repository)
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.add(relative_path)

    pyproject = repository_root / "pyproject.toml"
    package_root = repository_root / "flowsight"
    if pyproject.is_symlink() or not pyproject.is_file():
        raise AssertionError("pyproject.toml must be a regular wheel input")
    if package_root.is_symlink() or not package_root.is_dir():
        raise AssertionError("flowsight must be a regular wheel input directory")
    copy_regular(pyproject, Path("pyproject.toml"))

    for source in sorted(package_root.rglob("*")):
        relative_path = source.relative_to(repository_root)
        if _is_generated(relative_path):
            continue
        if source.is_symlink():
            raise AssertionError(f"wheel build input must not be a symlink: {relative_path}")
        if source.is_dir():
            continue
        if not source.is_file():
            raise AssertionError(f"wheel build input must be regular: {relative_path}")
        if not _is_eligible_package_file(relative_path):
            raise AssertionError(f"unexpected wheel build input: {relative_path}")
        copy_regular(source, relative_path)

    assert {path.name for path in destination.iterdir()} == {"flowsight", "pyproject.toml"}
    static_inputs = {path for path in copied if path.is_relative_to(Path("flowsight/static"))}
    assert Path("flowsight/static/index.html") in static_inputs
    assert any(path.parent == Path("flowsight/static/assets") for path in static_inputs)
    return copied


def _venv_python(venv_path: Path) -> Path:
    return venv_path.resolve(strict=True) / "bin" / "python"


def _pip_install_command(
    venv_python: Path,
    *requirements: str,
) -> tuple[str, ...]:
    return (
        str(venv_python),
        "-I",
        "-m",
        "pip",
        "--isolated",
        "--disable-pip-version-check",
        "--no-input",
        "--keyring-provider",
        "disabled",
        "--retries",
        "2",
        "--timeout",
        "15",
        "install",
        "--index-url",
        PUBLIC_INDEX,
        *requirements,
    )


def _wheel_manifest(wheel_path: Path) -> dict[str, object]:
    wheel_sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel_path) as archive:
        static_information = [
            information
            for information in archive.infolist()
            if information.filename == "flowsight/static/index.html"
            or information.filename.startswith("flowsight/static/assets/")
        ]
        metadata_information = [
            information
            for information in archive.infolist()
            if information.filename.endswith(".dist-info/METADATA")
            and information.filename.startswith("flowsight-")
        ]
        assert len(metadata_information) == 1
        assert static_information
        static_digests: dict[str, str] = {}
        for information in static_information:
            assert not information.is_dir()
            mode = information.external_attr >> 16
            assert not stat.S_ISLNK(mode)
            static_digests[information.filename] = hashlib.sha256(
                archive.read(information)
            ).hexdigest()
        metadata_sha256 = hashlib.sha256(archive.read(metadata_information[0])).hexdigest()

    assert "flowsight/static/index.html" in static_digests
    assert any(name.startswith("flowsight/static/assets/") for name in static_digests)
    return {
        "metadata_sha256": metadata_sha256,
        "static_digests": static_digests,
        "wheel_sha256": wheel_sha256,
    }


def _distribution_metadata_path(distribution: importlib.metadata.Distribution) -> Path:
    matches = [
        entry
        for entry in (distribution.files or ())
        if entry.name == "METADATA" and entry.parent.name.endswith(".dist-info")
    ]
    assert len(matches) == 1
    return Path(distribution.locate_file(matches[0])).resolve(strict=True)


def _contained(path: Path, prefix: Path, forbidden_roots: tuple[Path, ...]) -> Path:
    resolved = path.resolve(strict=True)
    assert resolved.is_relative_to(prefix)
    assert all(not resolved.is_relative_to(root) for root in forbidden_roots)
    return resolved


def _fetch(url: str, *, host: str, method: str = "GET", token: str | None = None) -> bytes:
    headers = {"Host": host}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        return response.read()


if _INNER_MODE:

    def test_installed_wheel_serves_byte_exact_ui_without_node() -> None:
        manifest = json.loads((Path.cwd() / _MANIFEST_NAME).read_text(encoding="utf-8"))
        assert type(manifest) is dict
        expected_venv = Path(manifest["venv"])
        expected_wheel = Path(manifest["wheel"]).resolve(strict=True)
        expected_wheel_sha256 = manifest["wheel_sha256"]
        expected_metadata_sha256 = manifest["metadata_sha256"]
        expected_static_digests = manifest["static_digests"]
        assert type(expected_static_digests) is dict
        forbidden_roots = tuple(
            Path(manifest[name]).resolve() for name in ("workspace", "source", "wheel_outdir")
        )

        assert Path(sys.executable) == expected_venv / "bin" / "python"
        assert Path(sys.prefix).resolve() == expected_venv
        assert sys.base_prefix != sys.prefix
        assert site.ENABLE_USER_SITE is False
        assert shutil.which("node") is None
        assert "conftest" not in sys.modules
        assert importlib.util.find_spec("conftest") is None
        for entry in sys.path:
            if entry:
                resolved_entry = Path(entry).resolve()
                assert all(not resolved_entry.is_relative_to(root) for root in forbidden_roots)

        flowsight = importlib.import_module("flowsight")
        flowsight_sidecar = importlib.import_module("flowsight.sidecar")
        uvicorn = importlib.import_module("uvicorn")
        SidecarState = flowsight_sidecar.SidecarState
        create_sidecar_app = flowsight_sidecar.create_sidecar_app

        prefix = expected_venv
        _contained(Path(flowsight.__file__), prefix, forbidden_roots)
        _contained(Path(flowsight_sidecar.__file__), prefix, forbidden_roots)
        _contained(Path(uvicorn.__file__), prefix, forbidden_roots)

        distribution = importlib.metadata.distribution("flowsight")
        metadata_path = _contained(
            _distribution_metadata_path(distribution),
            prefix,
            forbidden_roots,
        )
        assert hashlib.sha256(metadata_path.read_bytes()).hexdigest() == expected_metadata_sha256
        direct_url_text = distribution.read_text("direct_url.json")
        assert direct_url_text is not None
        direct_url = json.loads(direct_url_text)
        assert direct_url["url"] == expected_wheel.as_uri()
        assert direct_url["archive_info"]["hash"] == f"sha256={expected_wheel_sha256}"
        assert direct_url["archive_info"]["hashes"] == {"sha256": expected_wheel_sha256}

        static_root = _contained(
            Path(str(importlib.resources.files("flowsight").joinpath("static"))),
            prefix,
            forbidden_roots,
        )
        assert static_root.is_dir() and not static_root.is_symlink()
        installed_bodies: dict[str, bytes] = {}
        for wheel_name, expected_digest in expected_static_digests.items():
            assert type(wheel_name) is str
            assert type(expected_digest) is str
            relative_name = wheel_name.removeprefix("flowsight/static/")
            installed_path = _contained(
                static_root.joinpath(*relative_name.split("/")),
                prefix,
                forbidden_roots,
            )
            assert installed_path.is_file() and not installed_path.is_symlink()
            body = installed_path.read_bytes()
            assert hashlib.sha256(body).hexdigest() == expected_digest
            installed_bodies[relative_name] = body

        with zipfile.ZipFile(expected_wheel) as archive:
            for relative_name, installed_body in installed_bodies.items():
                assert archive.read(f"flowsight/static/{relative_name}") == installed_body

        index_body = installed_bodies["index.html"]
        asset_routes = {
            reference.decode("ascii") for reference in _ASSET_REFERENCE.findall(index_body)
        }
        installed_asset_routes = {
            f"/{relative_name}"
            for relative_name in installed_bodies
            if relative_name.startswith("assets/")
        }
        assert asset_routes == installed_asset_routes

        token = "wheel-probe-capability-token-0123456789"
        state = SidecarState(
            project_id="wheel-probe-project",
            startup_id="wheel-probe-startup",
            pid=os.getpid(),
            port=4040,
            token=token,
            database_path=str((Path.cwd() / "not-created.sqlite3").resolve()),
            started_at_ns=1,
        )
        app = create_sidecar_app(state)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            access_log=False,
            log_level="critical",
            lifespan="off",
        )
        server = uvicorn.Server(config)
        server_errors: list[BaseException] = []

        def run_server() -> None:
            try:
                server.run()
            except BaseException as error:
                server_errors.append(error)

        thread = threading.Thread(target=run_server, name="wheel-ui-probe", daemon=False)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "installed Uvicorn did not report readiness"
                threading.Event().wait(min(0.02, remaining))
            assert server.started
            assert not server_errors
            sockets = [
                bound_socket
                for server_instance in server.servers
                for bound_socket in server_instance.sockets
            ]
            assert len(sockets) == 1
            address = sockets[0].getsockname()
            assert address[0] == "127.0.0.1"
            actual_port = int(address[1])
            assert actual_port != 0
            base_url = f"http://127.0.0.1:{actual_port}"

            assert _fetch(base_url + "/", host=state.authority) == index_body
            assert _fetch(base_url + "/", host=state.authority, method="HEAD") == b""
            for route in sorted(asset_routes):
                relative_name = route.removeprefix("/")
                assert (
                    _fetch(base_url + route, host=state.authority)
                    == installed_bodies[relative_name]
                )
            health = _fetch(
                base_url + "/internal/v1/health",
                host=state.authority,
                token=token,
            )
            assert json.loads(health)["status"] == "ok"

            write_request = urllib.request.Request(
                base_url + "/",
                data=b"",
                headers={"Host": state.authority},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as write_error:
                urllib.request.urlopen(write_request, timeout=5)
            assert write_error.value.code == 405
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert not server_errors
        assert not (Path.cwd() / "not-created.sqlite3").exists()


else:

    def test_frontend_manifest_is_root_owned_exact_and_minimal() -> None:
        manifest = json.loads((REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8"))
        assert manifest["private"] is True
        assert manifest["scripts"] == {
            "check": "tsc --project ui/tsconfig.json --noEmit",
            "test": "vitest run --config ui/vite.config.ts",
            "build": "vite build --config ui/vite.config.ts",
        }
        assert manifest["dependencies"] == {
            "react": "19.2.7",
            "react-dom": "19.2.7",
        }
        assert manifest["devDependencies"] == {
            "@types/node": "26.1.1",
            "@types/react": "19.2.17",
            "@types/react-dom": "19.2.3",
            "typescript": "7.0.2",
            "vite": "8.1.4",
            "vitest": "4.1.10",
        }
        lockfile = json.loads((REPOSITORY_ROOT / "package-lock.json").read_text(encoding="utf-8"))
        assert lockfile["lockfileVersion"] == 3
        assert lockfile["packages"][""]["dependencies"] == manifest["dependencies"]
        assert lockfile["packages"][""]["devDependencies"] == manifest["devDependencies"]
        for package_path, package_record in lockfile["packages"].items():
            if not package_path:
                continue
            assert package_path.startswith("node_modules/")
            assert type(package_record.get("version")) is str
            assert type(package_record.get("resolved")) is str
            assert type(package_record.get("integrity")) is str
        assert not (REPOSITORY_ROOT / "ui" / "package.json").exists()
        assert not (REPOSITORY_ROOT / "ui" / "package-lock.json").exists()

    def test_make_phase0_frontend_chain_and_ui_probe_are_ordered_once() -> None:
        lines = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
        target_index = lines.index("test-phase0:")
        recipe = lines[target_index + 1].removeprefix("\t")
        assert lines[target_index + 1].startswith("\t")
        assert "tests/packaging/test_wheel_ui.py" in recipe
        assert recipe.count("tests/packaging/test_wheel_ui.py") == 1
        frontend_check = recipe.index("npm run check")
        frontend_test = recipe.index("npm test")
        frontend_build = recipe.index("npm run build")
        python_tests = recipe.index("tests/test_sdk_skeleton.py")
        ui_probe = recipe.index("tests/packaging/test_wheel_ui.py")
        assert frontend_check < frontend_test < frontend_build < python_tests < ui_probe

    def test_probe_runner_timeout_reaps_its_process_group(tmp_path: Path) -> None:
        child_script = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)']); "
            "print(f'CHILD={child.pid}',flush=True); time.sleep(30)"
        )

        with pytest.raises(_CommandFailure, match="timed out") as error:
            _run_checked(
                (sys.executable, "-I", "-c", child_script),
                cwd=tmp_path,
                environment=os.environ,
                timeout=0.2,
            )

        match = re.search(r"CHILD=(\d+)", str(error.value))
        assert match is not None
        child_pid = int(match.group(1))
        deadline = time.monotonic() + REAP_TIMEOUT_SECONDS
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            remaining = deadline - time.monotonic()
            assert remaining > 0, "probe runner left its descendant alive"
            threading.Event().wait(min(0.01, remaining))

    def test_clean_wheel_serves_bundled_ui_from_isolated_node_free_runtime(
        tmp_path: Path,
    ) -> None:
        workspace = REPOSITORY_ROOT.resolve(strict=True)
        temporary_root = tmp_path.resolve(strict=True)
        assert not temporary_root.is_relative_to(workspace)
        source_root = temporary_root / "source"
        wheel_outdir = temporary_root / "wheel-out"
        venv_path = temporary_root / "venv"
        probe_root = temporary_root / "probe"
        wheel_outdir.mkdir()
        probe_root.mkdir()

        copied = _copy_wheel_inputs(workspace, source_root)
        assert Path("flowsight/static/index.html") in copied
        assert wheel_outdir != source_root
        assert venv_path != source_root
        assert probe_root != source_root
        environment = _controlled_environment(temporary_root)

        _run_checked(
            (
                sys.executable,
                "-I",
                "-m",
                "build",
                "--wheel",
                "--installer",
                "pip",
                "--outdir",
                str(wheel_outdir),
                str(source_root),
            ),
            cwd=temporary_root,
            environment=environment,
            timeout=BUILD_TIMEOUT_SECONDS,
        )
        wheels = list(wheel_outdir.glob("flowsight-*.whl"))
        assert len(wheels) == 1
        wheel_path = wheels[0].resolve(strict=True)
        assert wheel_path.is_file() and not wheel_path.is_symlink()

        _run_checked(
            (sys.executable, "-I", "-m", "venv", str(venv_path)),
            cwd=temporary_root,
            environment=environment,
            timeout=VENV_TIMEOUT_SECONDS,
        )
        venv_python = _venv_python(venv_path)
        forbidden_roots = (workspace, source_root.resolve(), wheel_outdir.resolve())
        _run_checked(
            (
                str(venv_python),
                "-I",
                "-c",
                _PREINSTALL_PROBE,
                str(venv_path.resolve()),
                *(str(path) for path in forbidden_roots),
            ),
            cwd=probe_root,
            environment=environment,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )

        _run_checked(
            _pip_install_command(venv_python, str(wheel_path)),
            cwd=temporary_root,
            environment=environment,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        _run_checked(
            (
                str(venv_python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                "--keyring-provider",
                "disabled",
                "check",
            ),
            cwd=temporary_root,
            environment=environment,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
        _run_checked(
            _pip_install_command(venv_python, PYTEST_REQUIREMENT),
            cwd=temporary_root,
            environment=environment,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )

        manifest = {
            **_wheel_manifest(wheel_path),
            "source": str(source_root.resolve()),
            "venv": str(venv_path.resolve()),
            "wheel": str(wheel_path),
            "wheel_outdir": str(wheel_outdir.resolve()),
            "workspace": str(workspace),
        }
        (probe_root / _MANIFEST_NAME).write_text(
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )
        shutil.copy2(Path(__file__).resolve(strict=True), probe_root / "test_wheel_ui.py")
        probe_environment = dict(environment)
        probe_environment["PATH"] = str(venv_path.resolve() / "bin")
        output = _run_checked(
            (
                str(venv_python),
                "-I",
                "-m",
                "pytest",
                "--rootdir",
                str(probe_root),
                "--import-mode=importlib",
                str(probe_root / "test_wheel_ui.py"),
            ),
            cwd=probe_root,
            environment=probe_environment,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        assert "1 passed" in output
