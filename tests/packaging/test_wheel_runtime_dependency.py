from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_INDEX = "https://pypi.org/simple"
EXPECTED_RUNTIME_REQUIREMENTS = (
    "fastapi==0.139.0",
    "platformdirs==4.10.0",
    "pydantic==2.13.4",
    "starlette==1.3.1",
    "uvicorn==0.51.0",
)
EXPECTED_DEV_REQUIREMENTS = (
    "asgiref==3.11.1",
    "build==1.5.1",
    "mypy==2.2.0",
    "opentelemetry-api==1.43.0",
    "opentelemetry-instrumentation-fastapi==0.64b0",
    "opentelemetry-sdk==1.43.0",
    "pytest==9.1.1",
    "ruff==0.15.21",
    "wrapt==2.2.2",
)

BUILD_TIMEOUT_SECONDS = 180
VENV_TIMEOUT_SECONDS = 60
INSTALL_TIMEOUT_SECONDS = 240
PROBE_TIMEOUT_SECONDS = 30
PIP_CHECK_TIMEOUT_SECONDS = 30
TERMINATE_GRACE_SECONDS = 1
REAP_TIMEOUT_SECONDS = 5

_GENERATED_PARTS = {"__pycache__", "build", "dist"}
_GENERATED_SUFFIXES = (".egg-info", ".dist-info")
_GENERATED_FILE_SUFFIXES = (".pyc", ".pyo", ".whl")

PREINSTALL_PROBE = r"""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import site
import sys

expected_venv = Path(sys.argv[1])
forbidden_roots = tuple(Path(value).resolve() for value in sys.argv[2:])

assert Path(sys.executable) == expected_venv / "bin" / "python"
assert Path(sys.prefix).resolve() == expected_venv
assert sys.base_prefix != sys.prefix
assert site.ENABLE_USER_SITE is False

for package_name in ("flowsight", "uvicorn"):
    assert importlib.util.find_spec(package_name) is None
    try:
        importlib.metadata.distribution(package_name)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError(f"unexpected installed distribution: {package_name}")

resolved_sys_path = []
for entry in sys.path:
    if not entry:
        continue
    resolved = Path(entry).resolve()
    resolved_sys_path.append(str(resolved))
    assert all(not resolved.is_relative_to(root) for root in forbidden_roots)

print(json.dumps({
    "executable": sys.executable,
    "prefix": str(Path(sys.prefix).resolve()),
    "sys_path": resolved_sys_path,
}, sort_keys=True))
"""

POSTINSTALL_PROBE = r"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import site
import sys
from urllib.parse import unquote, urlparse

expected_venv = Path(sys.argv[1])
expected_wheel = Path(sys.argv[2]).resolve()
expected_wheel_sha256 = sys.argv[3]
expected_metadata_sha256 = sys.argv[4]
forbidden_roots = tuple(Path(value).resolve() for value in sys.argv[5:])

assert Path(sys.executable) == expected_venv / "bin" / "python"
assert Path(sys.prefix).resolve() == expected_venv
assert sys.base_prefix != sys.prefix
assert site.ENABLE_USER_SITE is False

def audit(event, args):
    del args
    if event.startswith("socket.") or event.startswith("subprocess."):
        raise AssertionError(f"forbidden import-time audit event: {event}")

sys.addaudithook(audit)

import flowsight
import flowsight.sidecar
import uvicorn
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server as UvicornServer

assert isinstance(uvicorn.Config, type)
assert isinstance(uvicorn.Server, type)
assert uvicorn.Config is UvicornConfig
assert uvicorn.Server is UvicornServer
assert uvicorn.__version__ == "0.51.0"
assert importlib.metadata.version("uvicorn") == "0.51.0"

prefix = expected_venv

def contained(path_value):
    path = Path(path_value).resolve()
    assert path.is_relative_to(prefix)
    assert all(not path.is_relative_to(root) for root in forbidden_roots)
    return path

module_paths = {
    "flowsight": contained(flowsight.__file__),
    "flowsight.sidecar": contained(flowsight.sidecar.__file__),
    "uvicorn": contained(uvicorn.__file__),
}

def metadata_path(distribution):
    matches = [
        entry
        for entry in (distribution.files or ())
        if entry.name == "METADATA" and entry.parent.name.endswith(".dist-info")
    ]
    assert len(matches) == 1
    return contained(distribution.locate_file(matches[0]))

flowsight_distribution = importlib.metadata.distribution("flowsight")
uvicorn_distribution = importlib.metadata.distribution("uvicorn")
flowsight_metadata_path = metadata_path(flowsight_distribution)
uvicorn_metadata_path = metadata_path(uvicorn_distribution)

metadata_text = flowsight_distribution.read_text("METADATA")
assert metadata_text is not None
assert hashlib.sha256(metadata_text.encode("utf-8")).hexdigest() == expected_metadata_sha256
requirements = flowsight_distribution.metadata.get_all("Requires-Dist") or []
uvicorn_requirements = [
    requirement
    for requirement in requirements
    if requirement.split(";", 1)[0].split("[", 1)[0].split("=", 1)[0].strip().lower()
    == "uvicorn"
]
assert uvicorn_requirements == ["uvicorn==0.51.0"]

direct_url_text = flowsight_distribution.read_text("direct_url.json")
assert direct_url_text is not None
direct_url = json.loads(direct_url_text)
parsed_url = urlparse(direct_url["url"])
assert parsed_url.scheme == "file"
assert parsed_url.netloc in ("", "localhost")
assert Path(unquote(parsed_url.path)).resolve() == expected_wheel
archive_info = direct_url["archive_info"]
assert archive_info["hash"] == f"sha256={expected_wheel_sha256}"
assert archive_info["hashes"] == {"sha256": expected_wheel_sha256}

for development_only in ("build", "mypy", "pytest", "ruff", "opentelemetry-sdk"):
    try:
        importlib.metadata.distribution(development_only)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError(f"development extra leaked into wheel install: {development_only}")

resolved_sys_path = []
for entry in sys.path:
    if not entry:
        continue
    resolved = Path(entry).resolve()
    resolved_sys_path.append(str(resolved))
    assert all(not resolved.is_relative_to(root) for root in forbidden_roots)

print(json.dumps({
    "direct_url": direct_url,
    "flowsight_metadata": str(flowsight_metadata_path),
    "module_paths": {name: str(path) for name, path in module_paths.items()},
    "prefix": str(Path(sys.prefix).resolve()),
    "requirements": requirements,
    "sys_path": resolved_sys_path,
    "uvicorn_metadata": str(uvicorn_metadata_path),
}, sort_keys=True))
"""

_STUBBORN_PROCESS_TREE = r"""
import signal
import subprocess
import sys

child = subprocess.Popen([
    sys.executable,
    "-I",
    "-c",
    "import signal; signal.pause()",
])

def handle_term(signum, frame):
    del signum, frame
    return_code = child.wait(timeout=5)
    print(f"DESCENDANT_REAPED={child.pid}:{return_code}", flush=True)

signal.signal(signal.SIGTERM, handle_term)
print(f"TREE_READY={child.pid}", flush=True)
while True:
    signal.pause()
"""

_EXITING_LEADER_PROCESS_TREE = r"""
import signal
import subprocess
import sys

child = subprocess.Popen(
    [
        sys.executable,
        "-I",
        "-c",
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()",
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(f"QUIET_TREE_READY={child.pid}", flush=True)
signal.pause()
"""


class _CommandFailure(AssertionError):
    def __init__(
        self,
        message: str,
        *,
        output: str,
        process_id: int,
        escalated: bool,
    ) -> None:
        super().__init__(message)
        self.output = output
        self.process_id = process_id
        self.escalated = escalated


class _Control(BaseException):
    pass


def _command_text(command: Sequence[str]) -> str:
    return shlex.join(command)


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


def _terminate_process_group(
    process: subprocess.Popen[str],
) -> tuple[str, bool]:
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
        return output, False

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        output, _ = process.communicate(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise AssertionError("command process group could not be reaped") from None
    _wait_for_process_group_exit(process.pid)
    return output, True


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
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        output, escalated = _terminate_process_group(process)
        message = (
            f"command timed out after {timeout} seconds: {_command_text(exact_command)}\n{output}"
        )
        raise _CommandFailure(
            message,
            output=output,
            process_id=process.pid,
            escalated=escalated,
        ) from None
    except BaseException as control:
        try:
            _terminate_process_group(process)
        except BaseException as cleanup_error:
            control.add_note(f"process-group cleanup failed: {type(cleanup_error).__name__}")
        raise

    if process.returncode != 0:
        message = f"command exited {process.returncode}: {_command_text(exact_command)}\n{output}"
        raise _CommandFailure(
            message,
            output=output,
            process_id=process.pid,
            escalated=False,
        ) from None
    return output


def _controlled_environment(
    temporary_root: Path,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if inherited is None else inherited
    environment = {
        key: value
        for key, value in source.items()
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
    return relative_path.is_relative_to(Path("flowsight/static/assets"))


def _copy_wheel_build_inputs(repository_root: Path, destination: Path) -> set[Path]:
    resolved_repository = repository_root.resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=False)
    resolved_destination = destination.resolve(strict=True)
    assert not resolved_destination.is_relative_to(resolved_repository)

    pyproject = repository_root / "pyproject.toml"
    package_root = repository_root / "flowsight"
    for required in (pyproject, package_root):
        if required.is_symlink():
            raise AssertionError(f"wheel build input must not be a symlink: {required.name}")
    if not pyproject.is_file() or not package_root.is_dir():
        raise AssertionError("wheel build inputs are incomplete")

    copied: set[Path] = set()

    def copy_regular(source: Path, relative_path: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise AssertionError(f"wheel build input must be a regular file: {relative_path}")
        resolved_source = source.resolve(strict=True)
        if not resolved_source.is_relative_to(resolved_repository):
            raise AssertionError(f"wheel build input escaped repository: {relative_path}")
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.add(relative_path)

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
            raise AssertionError(f"wheel build input must be a regular file: {relative_path}")
        if not _is_eligible_package_file(relative_path):
            raise AssertionError(f"unexpected wheel build input: {relative_path}")
        copy_regular(source, relative_path)

    assert {path.name for path in destination.iterdir()} == {"flowsight", "pyproject.toml"}
    return copied


def _build_command(source_root: Path, wheel_outdir: Path) -> tuple[str, ...]:
    return (
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
    )


def _venv_command(venv_path: Path) -> tuple[str, ...]:
    return (sys.executable, "-I", "-m", "venv", str(venv_path))


def _venv_python(venv_path: Path) -> Path:
    return venv_path.resolve(strict=True) / "bin" / "python"


def _pip_install_command(venv_python: Path, wheel_path: Path) -> tuple[str, ...]:
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
        str(wheel_path.resolve(strict=True)),
    )


def _pip_check_command(venv_python: Path) -> tuple[str, ...]:
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
        "check",
    )


def _probe_command(
    venv_python: Path,
    script: str,
    *arguments: Path | str,
) -> tuple[str, ...]:
    return (
        str(venv_python),
        "-I",
        "-c",
        script,
        *(str(argument) for argument in arguments),
    )


def _canonical_distribution_name(requirement: str) -> str:
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^;\s\[\]@]+)", requirement)
    if match is None:
        raise AssertionError(f"requirement is not one exact unmarked pin: {requirement}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _wheel_metadata(wheel_path: Path) -> tuple[bytes, tuple[str, ...]]:
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_names = [
            name
            for name in wheel.namelist()
            if name.endswith(".dist-info/METADATA") and name.startswith("flowsight-")
        ]
        assert len(metadata_names) == 1
        metadata_bytes = wheel.read(metadata_names[0])
    message = BytesParser(policy=default).parsebytes(metadata_bytes)
    requirements = tuple(message.get_all("Requires-Dist") or ())
    uvicorn_requirements = tuple(
        requirement
        for requirement in requirements
        if _canonical_distribution_name(requirement.split(";", 1)[0].strip()) == "uvicorn"
    )
    assert uvicorn_requirements == ("uvicorn==0.51.0",)
    return metadata_bytes, requirements


def _artifact_snapshot(repository_root: Path) -> tuple[tuple[object, ...], ...]:
    candidates: set[Path] = set()
    for pattern in ("build", "dist", "*.egg-info", "*.dist-info", "*.whl"):
        candidates.update(repository_root.glob(pattern))
    records: list[tuple[object, ...]] = []
    for candidate in sorted(candidates):
        paths = [candidate]
        if candidate.is_dir() and not candidate.is_symlink():
            paths.extend(sorted(candidate.rglob("*")))
        for path in paths:
            metadata = path.lstat()
            records.append(
                (
                    str(path.relative_to(repository_root)),
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
    return tuple(records)


def _json_output(output: str) -> dict[str, object]:
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines
    result = json.loads(lines[-1])
    assert type(result) is dict
    return result


def _assert_process_absent(process_id: int) -> None:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"process still exists after bounded cleanup: {process_id}")


def test_manifest_dependency_move_is_exact() -> None:
    metadata = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_requirements = tuple(metadata["project"]["dependencies"])
    dev_requirements = tuple(metadata["project"]["optional-dependencies"]["dev"])

    assert runtime_requirements == EXPECTED_RUNTIME_REQUIREMENTS
    assert dev_requirements == EXPECTED_DEV_REQUIREMENTS
    all_requirements = (*runtime_requirements, *dev_requirements)
    canonical_names = tuple(_canonical_distribution_name(item) for item in all_requirements)
    assert len(canonical_names) == len(set(canonical_names))
    assert canonical_names.count("uvicorn") == 1


def test_make_phase0_includes_probe_exactly_once() -> None:
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
    probe_path = "tests/packaging/test_wheel_runtime_dependency.py"
    assert makefile.count(probe_path) == 1
    lines = makefile.splitlines()
    target_index = lines.index("test-phase0:")
    recipe: list[str] = []
    for line in lines[target_index + 1 :]:
        if line and not line.startswith(("\t", " ")):
            break
        if line.startswith("\t"):
            recipe.append(line.removeprefix("\t"))
    assert len(recipe) == 1
    assert shlex.split(recipe[0])[-1] == probe_path


def test_controlled_environment_rejects_hostile_host_values(tmp_path: Path) -> None:
    hostile = dict(os.environ)
    hostile.update(
        {
            "HOME": "/caller/private-home",
            "NETRC": "/caller/private-netrc",
            "PIP_CONFIG_FILE": "/caller/pip.conf",
            "PIP_EXTRA_INDEX_URL": "https://user:secret@example.invalid/simple",
            "PIP_INDEX_URL": "https://user:secret@example.invalid/simple",
            "PYTHONHOME": "/caller/python-home",
            "PYTHONINSPECT": "1",
            "PYTHONPYCACHEPREFIX": str(REPOSITORY_ROOT / "private-cache"),
            "PYTHONPATH": str(REPOSITORY_ROOT),
            "SSH_AUTH_SOCK": "/caller/agent.sock",
            "VIRTUAL_ENV": "/caller/venv",
            "XDG_CONFIG_HOME": "/caller/xdg",
            "__PYVENV_LAUNCHER__": "/caller/python",
        }
    )
    environment = _controlled_environment(tmp_path / "controlled", hostile)
    inspected_names = {
        key
        for key in environment
        if key.startswith(("PIP_", "PYTHON", "XDG_"))
        or key
        in {
            "HOME",
            "NETRC",
            "SSH_AUTH_SOCK",
            "VIRTUAL_ENV",
            "__PYVENV_LAUNCHER__",
        }
    }
    expected = {
        "HOME",
        "PIP_CACHE_DIR",
        "PIP_CONFIG_FILE",
        "PIP_DEFAULT_TIMEOUT",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PIP_INDEX_URL",
        "PIP_KEYRING_PROVIDER",
        "PIP_NO_INPUT",
        "PIP_RETRIES",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
    assert inspected_names == expected
    output = _run_checked(
        (
            sys.executable,
            "-I",
            "-c",
            "import json, os, sys; print(json.dumps({key: os.environ.get(key) "
            "for key in sys.argv[1:]}, sort_keys=True))",
            *sorted(expected | {"NETRC", "SSH_AUTH_SOCK", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}),
        ),
        cwd=tmp_path,
        environment=environment,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    observed = _json_output(output)
    assert observed == {key: environment.get(key) for key in sorted(observed)}
    assert observed["PIP_CONFIG_FILE"] == os.devnull
    assert observed["PIP_INDEX_URL"] == PUBLIC_INDEX
    assert observed["PYTHONPATH"] == ""
    for absent in ("NETRC", "SSH_AUTH_SOCK", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        assert observed[absent] is None


def test_checked_command_kills_descendant_process_group_on_timeout(tmp_path: Path) -> None:
    environment = _controlled_environment(tmp_path / "controlled")
    with pytest.raises(_CommandFailure) as captured:
        _run_checked(
            (sys.executable, "-I", "-c", _STUBBORN_PROCESS_TREE),
            cwd=tmp_path,
            environment=environment,
            timeout=1,
        )

    failure = captured.value
    assert failure.escalated is True
    assert "TREE_READY=" in failure.output
    assert "DESCENDANT_REAPED=" in failure.output
    child_match = re.search(r"TREE_READY=(\d+)", failure.output)
    assert child_match is not None
    _assert_process_absent(int(child_match.group(1)))
    _assert_process_absent(failure.process_id)


def test_process_group_permission_error_is_not_treated_as_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_group_probe(process_group_id: int, signal_number: int) -> None:
        assert process_group_id == 12345
        assert signal_number == 0
        raise PermissionError

    monkeypatch.setattr(os, "killpg", deny_group_probe)

    assert _process_group_exists(12345) is True


def test_checked_command_kills_group_after_leader_exits_on_term(tmp_path: Path) -> None:
    environment = _controlled_environment(tmp_path / "controlled")
    with pytest.raises(_CommandFailure) as captured:
        _run_checked(
            (sys.executable, "-I", "-c", _EXITING_LEADER_PROCESS_TREE),
            cwd=tmp_path,
            environment=environment,
            timeout=1,
        )

    failure = captured.value
    assert failure.escalated is True
    child_match = re.search(r"QUIET_TREE_READY=(\d+)", failure.output)
    assert child_match is not None
    _assert_process_absent(int(child_match.group(1)))
    _assert_process_absent(failure.process_id)


def test_checked_command_cleans_real_group_on_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_popen = subprocess.Popen
    control = _Control("preserve identity")
    created: list[subprocess.Popen[str]] = []

    class InterruptingProcess:
        def __init__(self, *args: object, **kwargs: object) -> None:
            process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            created.append(process)
            self.process = process
            self.interrupted = False

        @property
        def pid(self) -> int:
            return self.process.pid

        @property
        def returncode(self) -> int | None:
            return self.process.returncode

        def communicate(self, *args: object, **kwargs: object) -> tuple[str, str | None]:
            if not self.interrupted:
                self.interrupted = True
                raise control
            return self.process.communicate(*args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr(subprocess, "Popen", InterruptingProcess)
    environment = _controlled_environment(tmp_path / "controlled")
    with pytest.raises(_Control) as captured:
        _run_checked(
            (sys.executable, "-I", "-c", "import signal; signal.pause()"),
            cwd=tmp_path,
            environment=environment,
            timeout=PROBE_TIMEOUT_SECONDS,
        )

    assert captured.value is control
    assert len(created) == 1
    assert created[0].returncode is not None
    _assert_process_absent(created[0].pid)


@pytest.mark.parametrize("malformation", ["symlink", "fifo", "unexpected"])
def test_wheel_input_copy_rejects_unsafe_package_inputs(
    tmp_path: Path,
    malformation: str,
) -> None:
    repository = tmp_path / "repository"
    package = repository / "flowsight"
    package.mkdir(parents=True)
    (repository / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    unsafe = package / "unsafe.py"
    if malformation == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("private", encoding="utf-8")
        unsafe.symlink_to(outside)
    elif malformation == "fifo":
        os.mkfifo(unsafe)
    else:
        unsafe = package / "unexpected.txt"
        unsafe.write_text("private", encoding="utf-8")

    with pytest.raises(AssertionError):
        _copy_wheel_build_inputs(repository, tmp_path / "copy")


def test_generated_inputs_are_excluded_from_exact_safe_copy(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    package = repository / "flowsight"
    package.mkdir(parents=True)
    (repository / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"generated")
    egg_info = package / "private.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("generated", encoding="utf-8")

    copied = _copy_wheel_build_inputs(repository, tmp_path / "copy")

    assert copied == {Path("pyproject.toml"), Path("flowsight/__init__.py")}
    assert not (tmp_path / "copy/flowsight/__pycache__").exists()
    assert not (tmp_path / "copy/flowsight/private.egg-info").exists()


def test_probe_source_and_external_commands_are_exactly_inert(tmp_path: Path) -> None:
    wheel = tmp_path / "flowsight-0.1.0a0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin/python"
    python.write_text("", encoding="utf-8")

    assert _build_command(tmp_path / "source", tmp_path / "wheelhouse") == (
        sys.executable,
        "-I",
        "-m",
        "build",
        "--wheel",
        "--installer",
        "pip",
        "--outdir",
        str(tmp_path / "wheelhouse"),
        str(tmp_path / "source"),
    )
    assert _venv_command(venv) == (sys.executable, "-I", "-m", "venv", str(venv))
    install_command = _pip_install_command(python, wheel)
    assert install_command == (
        str(python),
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
        str(wheel.resolve()),
    )
    assert _pip_check_command(python) == (
        str(python),
        "-I",
        "-m",
        "pip",
        "--isolated",
        "--disable-pip-version-check",
        "--no-input",
        "--keyring-provider",
        "disabled",
        "check",
    )
    assert _probe_command(python, "pass", "argument") == (
        str(python),
        "-I",
        "-c",
        "pass",
        "argument",
    )

    for probe_source in (PREINSTALL_PROBE, POSTINSTALL_PROBE):
        tree = ast.parse(probe_source)
        assert not any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and any(alias.name.split(".", 1)[0] in {"socket", "subprocess"} for alias in node.names)
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"Config", "Server", "UvicornConfig", "UvicornServer"}
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"Config", "Server", "bind", "listen", "run", "serve"}
                )
            )
            for node in ast.walk(tree)
        )

    source_tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    popen_calls = [
        node
        for node in ast.walk(source_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "Popen"
    ]
    assert len(popen_calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in popen_calls[0].keywords}
    assert isinstance(keywords["shell"], ast.Constant) and keywords["shell"].value is False
    assert (
        isinstance(keywords["start_new_session"], ast.Constant)
        and keywords["start_new_session"].value is True
    )
    forbidden_external_calls = {
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "popen",
        "run",
        "system",
    }
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_external_calls
        for node in ast.walk(source_tree)
    )


def test_clean_wheel_supplies_exact_uvicorn_runtime_dependency(tmp_path: Path) -> None:
    repository_snapshot = _artifact_snapshot(REPOSITORY_ROOT)
    resolved_repository = REPOSITORY_ROOT.resolve(strict=True)
    probe_root = (tmp_path / "clean-wheel-probe").resolve()
    source_root = probe_root / "source"
    wheel_outdir = probe_root / "wheelhouse"
    venv_path = probe_root / "venv"
    command_cwd = probe_root / "cwd"
    controlled_root = probe_root / "controlled"
    for directory in (probe_root, wheel_outdir, command_cwd):
        directory.mkdir(parents=True, exist_ok=False)
    for path in (source_root, wheel_outdir, venv_path, command_cwd):
        assert not path.resolve().is_relative_to(resolved_repository)
    assert len({source_root, wheel_outdir, venv_path, command_cwd}) == 4
    assert list(wheel_outdir.iterdir()) == []

    copied = _copy_wheel_build_inputs(REPOSITORY_ROOT, source_root)
    assert Path("pyproject.toml") in copied
    assert Path("flowsight/__init__.py") in copied
    environment = _controlled_environment(controlled_root)

    _run_checked(
        _build_command(source_root, wheel_outdir),
        cwd=command_cwd,
        environment=environment,
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    wheel_paths = sorted(wheel_outdir.glob("flowsight-*.whl"))
    assert len(wheel_paths) == 1
    wheel_path = wheel_paths[0]
    assert wheel_path.is_file() and not wheel_path.is_symlink()
    assert list(wheel_outdir.iterdir()) == [wheel_path]
    wheel_bytes = wheel_path.read_bytes()
    wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
    metadata_bytes, wheel_requirements = _wheel_metadata(wheel_path)
    metadata_sha256 = hashlib.sha256(metadata_bytes).hexdigest()
    assert "uvicorn==0.51.0" in wheel_requirements

    _run_checked(
        _venv_command(venv_path),
        cwd=command_cwd,
        environment=environment,
        timeout=VENV_TIMEOUT_SECONDS,
    )
    resolved_venv = venv_path.resolve(strict=True)
    venv_python = _venv_python(venv_path)
    assert venv_python == resolved_venv / "bin" / "python"
    assert venv_python.is_file()
    forbidden_roots = (resolved_repository, source_root, wheel_outdir)

    preinstall = _run_checked(
        _probe_command(venv_python, PREINSTALL_PROBE, resolved_venv, *forbidden_roots),
        cwd=command_cwd,
        environment=environment,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    preinstall_result = _json_output(preinstall)
    assert preinstall_result["prefix"] == str(resolved_venv)

    install_command = _pip_install_command(venv_python, wheel_path)
    assert install_command[-1] == str(wheel_path.resolve(strict=True))
    _run_checked(
        install_command,
        cwd=command_cwd,
        environment=environment,
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    pip_check = _run_checked(
        _pip_check_command(venv_python),
        cwd=command_cwd,
        environment=environment,
        timeout=PIP_CHECK_TIMEOUT_SECONDS,
    )
    assert "No broken requirements found." in pip_check

    postinstall = _run_checked(
        _probe_command(
            venv_python,
            POSTINSTALL_PROBE,
            resolved_venv,
            wheel_path.resolve(strict=True),
            wheel_sha256,
            metadata_sha256,
            *forbidden_roots,
        ),
        cwd=command_cwd,
        environment=environment,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    postinstall_result = _json_output(postinstall)
    assert postinstall_result["prefix"] == str(resolved_venv)
    assert postinstall_result["direct_url"] == {
        "archive_info": {
            "hash": f"sha256={wheel_sha256}",
            "hashes": {"sha256": wheel_sha256},
        },
        "url": wheel_path.resolve(strict=True).as_uri(),
    }
    assert postinstall_result["requirements"] == list(wheel_requirements)
    assert _artifact_snapshot(REPOSITORY_ROOT) == repository_snapshot
