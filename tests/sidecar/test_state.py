from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

from flowsight.sidecar import (
    LOOPBACK_HOST,
    MAX_STATE_BYTES,
    PROTOCOL_VERSION,
    STATE_SCHEMA_VERSION,
    InvalidStateError,
    SidecarState,
    StateBusyError,
    StateStorageError,
    StateStore,
)

PROJECT_ID = "project-0123456789abcdef"
TOKEN = "token-value-that-is-long-enough-0123456789"
CHILD_TOKEN = "child-token-value-that-is-long-enough-012345"


class _TextSubclass(str):
    pass


def _state(
    store: StateStore,
    *,
    startup_id: str = "startup-a",
    token: str = TOKEN,
) -> SidecarState:
    return SidecarState(
        project_id=store.project_id,
        startup_id=startup_id,
        pid=1234,
        port=4040,
        token=token,
        database_path=str(store.database_path),
        started_at_ns=123_456_789,
    )


def _wire_bytes(store: StateStore) -> bytes:
    return json.dumps(_state(store).to_wire(), separators=(",", ":")).encode()


def _write_raw(store: StateStore, payload: bytes, *, mode: int = 0o600) -> None:
    store.ensure_private_directory()
    store.state_path.write_bytes(payload)
    store.state_path.chmod(mode)


def _temporary_state_paths(store: StateStore) -> tuple[Path, ...]:
    return tuple(
        path
        for path in store.runtime_dir.glob(".sidecar-state-*")
        if path != store.mutation_lock_path
    )


def test_state_wire_round_trip_is_exact_and_repr_hides_token(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    state = _state(store)

    assert SidecarState.from_wire(state.to_wire()) == state
    assert TOKEN not in repr(state)
    assert "token=" not in repr(state)
    assert state.host == LOOPBACK_HOST
    assert state.protocol_version == PROTOCOL_VERSION
    assert state.state_schema_version == STATE_SCHEMA_VERSION
    assert state.authority == "127.0.0.1:4040"
    assert state.origin == "http://127.0.0.1:4040"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("project_id", ""),
        ("project_id", "project\nnewline"),
        ("project_id", "p" * 257),
        ("startup_id", "startup\x00nul"),
        ("startup_id", "s" * 129),
        ("pid", True),
        ("pid", 0),
        ("pid", 2**31),
        ("port", 0),
        ("port", 65_536),
        ("token", "too-short"),
        ("token", "t" * 513),
        ("token", _TextSubclass(TOKEN)),
        ("database_path", "relative.sqlite3"),
        ("database_path", "/" + "d" * 4096),
        ("started_at_ns", 0),
        ("started_at_ns", 2**63),
        ("host", "0.0.0.0"),
        ("host", _TextSubclass(LOOPBACK_HOST)),
        ("protocol_version", True),
        ("protocol_version", 1.0),
        ("protocol_version", "1"),
        ("protocol_version", PROTOCOL_VERSION + 1),
        ("state_schema_version", True),
        ("state_schema_version", 1.0),
        ("state_schema_version", "1"),
        ("state_schema_version", STATE_SCHEMA_VERSION + 1),
    ],
)
def test_state_rejects_invalid_exact_fields(
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    wire = _state(store).to_wire()
    wire[field_name] = invalid_value

    with pytest.raises(InvalidStateError) as error:
        SidecarState.from_wire(wire)

    assert TOKEN not in str(error.value)


@pytest.mark.parametrize(
    ("field_name", "boundary_value"),
    [
        ("project_id", "p" * 256),
        ("startup_id", "s" * 128),
        ("pid", 2**31 - 1),
        ("port", 65_535),
        ("token", "t" * 32),
        ("token", "t" * 512),
        ("database_path", "/" + "d" * 4095),
        ("started_at_ns", 2**63 - 1),
    ],
)
def test_state_accepts_exact_bounded_field_limits(
    tmp_path: Path,
    field_name: str,
    boundary_value: object,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    wire = _state(store).to_wire()
    wire[field_name] = boundary_value

    parsed = SidecarState.from_wire(wire)

    assert parsed.to_wire()[field_name] == boundary_value


def test_state_rejects_missing_extra_and_non_object_roots(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    wire = _state(store).to_wire()
    missing = dict(wire)
    missing.pop("port")
    extra = {**wire, "unexpected": "field"}

    for invalid in (missing, extra, list(wire.items()), None):
        with pytest.raises(InvalidStateError):
            SidecarState.from_wire(invalid)


def test_project_ids_derive_distinct_private_state_lock_and_database_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    first = StateStore(root, project_id="project-a")
    second = StateStore(root, project_id="project-b")
    first_state = _state(first, startup_id="first")
    second_state = _state(second, startup_id="second")

    assert first.runtime_dir != second.runtime_dir
    assert first.state_path != second.state_path
    assert first.lock_path != second.lock_path
    assert first.mutation_lock_path != second.mutation_lock_path
    assert first.database_path != second.database_path

    first.publish(first_state)
    second.publish(second_state)
    assert first.load() == first_state
    assert second.load() == second_state
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(first.runtime_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(second.runtime_dir).st_mode) == 0o700


def test_publish_orders_file_fsync_replace_and_directory_fsync_on_exact_inodes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    store.publish(_state(store, startup_id="previous"))
    replacement = _state(store, startup_id="replacement")
    events: list[tuple[str, tuple[int, int]]] = []
    real_write = os.write
    real_fsync = os.fsync
    real_replace = os.replace

    def identity(file_descriptor: int) -> tuple[int, int]:
        metadata = os.fstat(file_descriptor)
        return metadata.st_dev, metadata.st_ino

    def recording_write(file_descriptor: int, data: bytes | bytearray | memoryview) -> int:
        events.append(("write", identity(file_descriptor)))
        return real_write(file_descriptor, data)

    def recording_fsync(file_descriptor: int) -> None:
        metadata = os.fstat(file_descriptor)
        kind = "directory_fsync" if stat.S_ISDIR(metadata.st_mode) else "file_fsync"
        events.append((kind, (metadata.st_dev, metadata.st_ino)))
        real_fsync(file_descriptor)

    def recording_replace(
        source: str | bytes,
        destination: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd is not None
        source_metadata = os.stat(source, dir_fd=src_dir_fd, follow_symlinks=False)
        events.append(("replace", (source_metadata.st_dev, source_metadata.st_ino)))
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "write", recording_write)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)
    store.publish(replacement)

    labels = [label for label, _identity in events]
    assert labels == ["write", "file_fsync", "replace", "directory_fsync"]
    state_metadata = os.lstat(store.state_path)
    state_identity = (state_metadata.st_dev, state_metadata.st_ino)
    project_metadata = os.lstat(store.runtime_dir)
    project_identity = (project_metadata.st_dev, project_metadata.st_ino)
    assert events[0][1] == events[1][1] == events[2][1] == state_identity
    assert events[3][1] == project_identity
    assert stat.S_IMODE(state_metadata.st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(store.mutation_lock_path).st_mode) == 0o600
    assert store.load() == replacement
    assert not _temporary_state_paths(store)


def test_first_publish_fsyncs_each_created_directory_before_its_parent_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(
        tmp_path / "level-a" / "level-b" / "runtime",
        project_id=PROJECT_ID,
    )
    identity = tuple[int, int]
    events: list[tuple[str, identity, identity | None]] = []
    real_mkdir = os.mkdir
    real_fsync = os.fsync

    def inode(file_descriptor: int) -> identity:
        metadata = os.fstat(file_descriptor)
        return metadata.st_dev, metadata.st_ino

    def recording_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        assert dir_fd is not None
        parent_identity = inode(dir_fd)
        real_mkdir(path, mode=mode, dir_fd=dir_fd)
        child_metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        events.append(
            (
                "mkdir",
                (child_metadata.st_dev, child_metadata.st_ino),
                parent_identity,
            )
        )

    def recording_fsync(file_descriptor: int) -> None:
        events.append(("fsync", inode(file_descriptor), None))
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "mkdir", recording_mkdir)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    store.publish(_state(store))

    mkdir_events = [
        (index, child, parent)
        for index, (kind, child, parent) in enumerate(events)
        if kind == "mkdir"
    ]
    assert len(mkdir_events) == 4
    for mkdir_index, child_identity, parent_identity in mkdir_events:
        assert parent_identity is not None
        child_fsync_index = next(
            index
            for index, event in enumerate(events)
            if index > mkdir_index and event[:2] == ("fsync", child_identity)
        )
        parent_fsync_index = next(
            index
            for index, event in enumerate(events)
            if index > child_fsync_index and event[:2] == ("fsync", parent_identity)
        )
        assert mkdir_index < child_fsync_index < parent_fsync_index


def test_publish_uses_descriptor_permissions_not_path_chmod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)

    def unexpected_path_chmod(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("state publication must use fchmod on anchored descriptors")

    monkeypatch.setattr(os, "chmod", unexpected_path_chmod)
    state = _state(store)
    store.publish(state)
    assert store.load() == state


def test_publish_keeps_previous_complete_record_visible_until_relative_atomic_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    previous = _state(store, startup_id="previous")
    replacement = _state(
        store,
        startup_id="replacement",
        token="replacement-token-value-012345678901234",
    )
    store.publish(previous)
    replace_entered = threading.Event()
    release_replace = threading.Event()
    real_replace = os.replace

    def blocking_replace(
        source: str | bytes,
        destination: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if destination == "sidecar-state.json":
            replace_entered.set()
            if not release_replace.wait(2.0):
                raise AssertionError("fixture replace release timed out")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", blocking_replace)
    with ThreadPoolExecutor(max_workers=1) as executor:
        publication = executor.submit(store.publish, replacement)
        try:
            assert replace_entered.wait(1.0)
            assert store.load() == previous
            assert json.loads(store.state_path.read_bytes()) == previous.to_wire()
        finally:
            release_replace.set()
        publication.result(timeout=2.0)

    assert store.load() == replacement


def test_store_rejects_state_for_another_project_or_database(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    state = _state(store)

    for foreign in (
        SidecarState.from_wire({**state.to_wire(), "project_id": "another-project"}),
        SidecarState.from_wire(
            {**state.to_wire(), "database_path": str(tmp_path / "another.sqlite3")}
        ),
    ):
        with pytest.raises(InvalidStateError, match="does not belong") as error:
            store.publish(foreign)
        assert TOKEN not in str(error.value)

        _write_raw(store, json.dumps(foreign.to_wire()).encode())
        assert store.load() is None
        store.state_path.unlink()


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o700, 0o4600])
def test_load_requires_exact_state_mode_0600(tmp_path: Path, mode: int) -> None:
    store = StateStore(tmp_path / f"mode-{mode:o}", project_id=PROJECT_ID)
    _write_raw(store, _wire_bytes(store), mode=mode)

    assert store.load() is None


def test_load_fails_closed_for_missing_symlink_directory_hardlink_size_and_zero(
    tmp_path: Path,
) -> None:
    missing = StateStore(tmp_path / "missing", project_id=PROJECT_ID)
    assert missing.load() is None

    symlink = StateStore(tmp_path / "symlink", project_id=PROJECT_ID)
    symlink.ensure_private_directory()
    target = tmp_path / "outside-state.json"
    target.write_bytes(_wire_bytes(symlink))
    target.chmod(0o600)
    symlink.state_path.symlink_to(target)
    assert symlink.load() is None

    directory = StateStore(tmp_path / "directory", project_id=PROJECT_ID)
    directory.ensure_private_directory()
    directory.state_path.mkdir(mode=0o700)
    assert directory.load() is None

    hardlink = StateStore(tmp_path / "hardlink", project_id=PROJECT_ID)
    _write_raw(hardlink, _wire_bytes(hardlink))
    os.link(hardlink.state_path, hardlink.runtime_dir / "second-link.json")
    assert hardlink.load() is None

    oversized = StateStore(tmp_path / "oversized", project_id=PROJECT_ID)
    _write_raw(oversized, b"{" + b"x" * MAX_STATE_BYTES + b"}")
    assert oversized.load() is None

    empty = StateStore(tmp_path / "empty", project_id=PROJECT_ID)
    _write_raw(empty, b"")
    assert empty.load() is None


def test_load_opens_nonregular_fifo_without_blocking(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "fifo", project_id=PROJECT_ID)
    store.ensure_private_directory()
    os.mkfifo(store.state_path, mode=0o600)
    executor = ThreadPoolExecutor(max_workers=1)
    reader = executor.submit(store.load)
    release_descriptor = -1
    try:
        try:
            result = reader.result(timeout=0.5)
        except FutureTimeoutError:
            release_descriptor = os.open(store.state_path, os.O_RDWR | os.O_NONBLOCK)
            reader.result(timeout=1.0)
            pytest.fail("state FIFO open blocked before descriptor validation")
        assert result is None
    finally:
        if release_descriptor >= 0:
            os.close(release_descriptor)
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize(
    "duplicate_field",
    [
        "state_schema_version",
        "protocol_version",
        "project_id",
        "startup_id",
        "pid",
        "host",
        "port",
        "token",
        "database_path",
        "started_at_ns",
    ],
)
def test_load_rejects_every_duplicate_json_field_even_when_values_match(
    tmp_path: Path,
    duplicate_field: str,
) -> None:
    store = StateStore(tmp_path / duplicate_field, project_id=PROJECT_ID)
    wire = _state(store).to_wire()
    members: list[str] = []
    for key, value in wire.items():
        member = f"{json.dumps(key)}:{json.dumps(value)}"
        members.append(member)
        if key == duplicate_field:
            members.append(member)
    _write_raw(store, ("{" + ",".join(members) + "}").encode())

    assert store.load() is None


def test_malformed_parser_failures_are_silent_and_fail_closed(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    huge_integer = StateStore(tmp_path / "huge-int", project_id=PROJECT_ID)
    huge_payload = _wire_bytes(huge_integer).replace(
        b'"pid":1234',
        b'"pid":' + b"9" * 5000,
    )
    nested = StateStore(tmp_path / "nested", project_id=PROJECT_ID)
    malformed = StateStore(tmp_path / "malformed", project_id=PROJECT_ID)

    cases = (
        (huge_integer, huge_payload),
        (nested, b"[" * 10_000 + b"0" + b"]" * 10_000),
        (malformed, b"\xffraw-secret-token"),
    )
    for store, payload in cases:
        _write_raw(store, payload)
        assert store.load() is None

    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "raw-secret-token" not in captured.out + captured.err
    assert TOKEN not in caplog.text
    assert "raw-secret-token" not in caplog.text


def test_load_rechecks_opened_inode_after_mode_and_size_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mode_store = StateStore(tmp_path / "mode-change", project_id=PROJECT_ID)
    _write_raw(mode_store, _wire_bytes(mode_store))
    real_read = os.read
    mode_changed = False

    def chmod_after_read(file_descriptor: int, size: int) -> bytes:
        nonlocal mode_changed
        data = real_read(file_descriptor, size)
        if data and not mode_changed:
            mode_changed = True
            os.fchmod(file_descriptor, 0o640)
        return data

    with monkeypatch.context() as context:
        context.setattr(os, "read", chmod_after_read)
        assert mode_store.load() is None
    assert mode_changed is True

    size_store = StateStore(tmp_path / "size-change", project_id=PROJECT_ID)
    _write_raw(size_store, _wire_bytes(size_store))
    appended = False

    def append_after_eof(file_descriptor: int, size: int) -> bytes:
        nonlocal appended
        data = real_read(file_descriptor, size)
        if not data and not appended:
            appended = True
            append_descriptor = os.open(size_store.state_path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(append_descriptor, b"x" * (MAX_STATE_BYTES + 1))
            finally:
                os.close(append_descriptor)
        return data

    with monkeypatch.context() as context:
        context.setattr(os, "read", append_after_eof)
        assert size_store.load() is None
    assert appended is True


def test_load_rejects_equal_length_rewrite_and_atomic_path_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rewritten = StateStore(tmp_path / "rewritten", project_id=PROJECT_ID)
    original_bytes = _wire_bytes(rewritten)
    _write_raw(rewritten, original_bytes)
    real_read = os.read
    did_rewrite = False

    def rewrite_after_read(file_descriptor: int, size: int) -> bytes:
        nonlocal did_rewrite
        data = real_read(file_descriptor, size)
        if data and not did_rewrite:
            did_rewrite = True
            replacement = original_bytes.replace(b'"startup-a"', b'"startup-b"')
            assert len(replacement) == len(original_bytes)
            rewrite_descriptor = os.open(rewritten.state_path, os.O_WRONLY)
            try:
                os.pwrite(rewrite_descriptor, replacement, 0)
                os.fsync(rewrite_descriptor)
            finally:
                os.close(rewrite_descriptor)
        return data

    with monkeypatch.context() as context:
        context.setattr(os, "read", rewrite_after_read)
        assert rewritten.load() is None
    assert did_rewrite is True

    atomic = StateStore(tmp_path / "atomic", project_id=PROJECT_ID)
    previous = _state(atomic, startup_id="previous")
    successor = _state(atomic, startup_id="successor")
    atomic.publish(previous)
    replaced = False

    def replace_path_after_read(file_descriptor: int, size: int) -> bytes:
        nonlocal replaced
        data = real_read(file_descriptor, size)
        if data and not replaced:
            replaced = True
            atomic.publish(successor)
        return data

    with monkeypatch.context() as context:
        context.setattr(os, "read", replace_path_after_read)
        assert atomic.load() is None
    assert replaced is True
    assert atomic.load() == successor


def test_runtime_root_and_project_directory_require_exact_mode_and_no_symlink(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    _write_raw(store, _wire_bytes(store))
    store.runtime_dir.chmod(0o755)
    assert store.load() is None
    store.runtime_dir.chmod(0o700)
    store.runtime_root.chmod(0o750)
    assert store.load() is None

    trusted_root = tmp_path / "trusted-root"
    trusted = StateStore(trusted_root, project_id=PROJECT_ID)
    trusted.ensure_private_directory()
    link_root = tmp_path / "runtime-link"
    link_root.symlink_to(trusted_root, target_is_directory=True)
    linked = StateStore(link_root, project_id=PROJECT_ID)
    linked.state_path.parent.mkdir(mode=0o700, exist_ok=True)
    linked.state_path.write_bytes(_wire_bytes(linked))
    linked.state_path.chmod(0o600)
    assert linked.load() is None
    with pytest.raises(StateStorageError, match="directory"):
        linked.ensure_private_directory()


def test_runtime_root_canonicalizes_existing_ancestor_alias_once(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    store = StateStore(alias / "nested" / "runtime", project_id=PROJECT_ID)
    alias.unlink()
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    alias.symlink_to(replacement, target_is_directory=True)

    state = _state(store)
    store.publish(state)

    assert store.runtime_root == external / "nested" / "runtime"
    assert store.load() == state
    assert store.state_path.is_relative_to(external)
    assert not (replacement / "nested" / "runtime").exists()


def test_state_store_supports_platform_temporary_directory_alias() -> None:
    with tempfile.TemporaryDirectory(
        prefix="flowsight-state-",
        dir=tempfile.gettempdir(),
    ) as temporary_directory:
        requested_root = Path(temporary_directory) / "runtime"
        store = StateStore(requested_root, project_id=PROJECT_ID)
        state = _state(store)

        store.publish(state)

        assert store.load() == state


def test_runtime_root_creation_tolerates_a_cooperating_creator_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    real_mkdir = os.mkdir
    real_fsync = os.fsync
    raced_names: set[str] = set()
    raced_pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    fsynced: list[tuple[int, int]] = []
    race_candidates = {store.runtime_root.name, store.runtime_dir.name}

    def create_then_report_existing(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        name = os.fsdecode(path)
        if name in race_candidates and name not in raced_names:
            assert dir_fd is not None
            parent_metadata = os.fstat(dir_fd)
            real_mkdir(path, mode=mode, dir_fd=dir_fd)
            child_metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            raced_names.add(name)
            raced_pairs.append(
                (
                    (child_metadata.st_dev, child_metadata.st_ino),
                    (parent_metadata.st_dev, parent_metadata.st_ino),
                )
            )
            raise FileExistsError(path)
        real_mkdir(path, mode=mode, dir_fd=dir_fd)

    def recording_fsync(file_descriptor: int) -> None:
        metadata = os.fstat(file_descriptor)
        fsynced.append((metadata.st_dev, metadata.st_ino))
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "mkdir", create_then_report_existing)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    state = _state(store)
    store.publish(state)

    assert raced_names == race_candidates
    assert len(raced_pairs) == 2
    for child_identity, parent_identity in raced_pairs:
        child_fsync_index = fsynced.index(child_identity)
        assert parent_identity in fsynced[child_fsync_index + 1 :]
    assert store.load() == state


def test_directory_close_failure_does_not_retry_a_reused_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    real_close = os.close
    replacement_descriptor = -1
    injected = False

    def close_then_reuse(file_descriptor: int) -> None:
        nonlocal injected, replacement_descriptor
        metadata = os.fstat(file_descriptor)
        if not injected and stat.S_ISDIR(metadata.st_mode):
            real_close(file_descriptor)
            replacement_descriptor = os.open(os.devnull, os.O_RDONLY)
            assert replacement_descriptor == file_descriptor
            injected = True
            raise OSError("directory close reported failure after release")
        real_close(file_descriptor)

    monkeypatch.setattr(os, "close", close_then_reuse)
    with pytest.raises(StateStorageError, match="cleanup failed"):
        store.ensure_private_directory()

    assert injected is True
    try:
        assert stat.S_ISCHR(os.fstat(replacement_descriptor).st_mode)
    finally:
        real_close(replacement_descriptor)


def test_publish_durably_repairs_existing_runtime_and_project_directory_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    store.ensure_private_directory()
    store.runtime_root.chmod(0o750)
    store.runtime_dir.chmod(0o755)
    expected = {
        (os.lstat(store.runtime_root).st_dev, os.lstat(store.runtime_root).st_ino),
        (os.lstat(store.runtime_dir).st_dev, os.lstat(store.runtime_dir).st_ino),
    }
    synced_directories: set[tuple[int, int]] = set()
    real_fsync = os.fsync

    def recording_fsync(file_descriptor: int) -> None:
        metadata = os.fstat(file_descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.add((metadata.st_dev, metadata.st_ino))
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    state = _state(store)
    store.publish(state)

    assert expected <= synced_directories
    assert stat.S_IMODE(os.lstat(store.runtime_root).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(store.runtime_dir).st_mode) == 0o700
    assert store.load() == state


def test_publish_reports_canonical_project_path_swap_after_anchored_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    store.ensure_private_directory()
    moved_directory = store.runtime_root / "trusted-project-moved"
    external_directory = tmp_path / "external-project"
    external_directory.mkdir(mode=0o700)
    original_acquire = store._acquire_mutation_lock
    swapped = False

    def swap_then_acquire(directory_descriptor: int, *, exclusive: bool) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            store.runtime_dir.rename(moved_directory)
            store.runtime_dir.symlink_to(external_directory, target_is_directory=True)
        return original_acquire(directory_descriptor, exclusive=exclusive)

    monkeypatch.setattr(store, "_acquire_mutation_lock", swap_then_acquire)
    state = _state(store)
    with pytest.raises(StateStorageError, match="changed"):
        store.publish(state)

    assert swapped is True
    detached_state = moved_directory / "sidecar-state.json"
    assert detached_state.is_file()
    assert stat.S_IMODE(os.lstat(detached_state).st_mode) == 0o600
    assert not (external_directory / "sidecar-state.json").exists()
    store.runtime_dir.unlink()
    moved_directory.rename(store.runtime_dir)
    assert store.load() == state


def test_publish_reports_runtime_root_replacement_after_anchored_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    store.ensure_private_directory()
    moved_root = tmp_path / "trusted-runtime-moved"
    original_acquire = store._acquire_mutation_lock
    swapped = False

    def swap_then_acquire(directory_descriptor: int, *, exclusive: bool) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            store.runtime_root.rename(moved_root)
            store.runtime_root.mkdir(mode=0o700)
            store.runtime_dir.mkdir(mode=0o700)
        return original_acquire(directory_descriptor, exclusive=exclusive)

    monkeypatch.setattr(store, "_acquire_mutation_lock", swap_then_acquire)
    state = _state(store)
    with pytest.raises(StateStorageError, match="changed"):
        store.publish(state)

    assert swapped is True
    detached_project = moved_root / store.runtime_dir.name
    detached_state = detached_project / "sidecar-state.json"
    assert detached_state.is_file()
    assert stat.S_IMODE(os.lstat(detached_state).st_mode) == 0o600
    assert not store.state_path.exists()
    store.runtime_dir.rmdir()
    store.runtime_root.rmdir()
    moved_root.rename(store.runtime_root)
    assert store.load() == state


def test_remove_if_owned_leaves_unowned_state_and_removes_owned_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    successor = _state(store, startup_id="successor")
    store.publish(successor)

    assert store.remove_if_owned("old-owner") is False
    assert store.load() == successor
    assert store.remove_if_owned("successor") is True
    assert store.load() is None
    assert store.remove_if_owned("successor") is False


def test_mutation_lock_makes_publish_busy_during_remove_then_successor_retry_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    old_state = _state(store, startup_id="old-owner")
    successor = _state(
        store,
        startup_id="new-owner",
        token="successor-token-value-0123456789012345",
    )
    store.publish(old_state)
    unlink_entered = threading.Event()
    release_unlink = threading.Event()
    real_unlink = os.unlink

    def blocking_unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        if path == "sidecar-state.json":
            unlink_entered.set()
            if not release_unlink.wait(2.0):
                raise AssertionError("fixture unlink release timed out")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", blocking_unlink)
    with ThreadPoolExecutor(max_workers=1) as executor:
        removal = executor.submit(store.remove_if_owned, "old-owner")
        try:
            assert unlink_entered.wait(1.0)
            with pytest.raises(StateBusyError, match="busy"):
                store.publish(successor)
        finally:
            release_unlink.set()
        assert removal.result(timeout=2.0) is True

    store.publish(successor)
    assert store.load() == successor


def test_mutation_lock_is_cross_process_nonblocking_and_released_on_close(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    store.publish(_state(store, startup_id="old-owner"))
    lock_descriptor = os.open(store.mutation_lock_path, os.O_RDWR | os.O_NONBLOCK)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    script = """
import sys
from pathlib import Path
from flowsight.sidecar import SidecarState, StateBusyError, StateStore

store = StateStore(Path(sys.argv[1]), project_id=sys.argv[2])
state = SidecarState(
    project_id=store.project_id,
    startup_id="child-successor",
    pid=2345,
    port=4041,
    token=sys.argv[4],
    database_path=str(store.database_path),
    started_at_ns=987654321,
)
try:
    store.publish(state)
except StateBusyError:
    outcome = "busy"
else:
    outcome = "published"
raise SystemExit(0 if outcome == sys.argv[3] else 7)
"""

    def run_child(expected: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(store.runtime_root),
                PROJECT_ID,
                expected,
                CHILD_TOKEN,
            ],
            cwd=Path(__file__).parents[2],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    try:
        busy = run_child("busy")
    finally:
        os.close(lock_descriptor)
    assert busy.returncode == 0
    assert CHILD_TOKEN not in busy.stdout + busy.stderr

    published = run_child("published")
    assert published.returncode == 0
    assert CHILD_TOKEN not in published.stdout + published.stderr
    loaded = store.load()
    assert loaded is not None
    assert loaded.startup_id == "child-successor"


def test_publish_failure_is_safe_keeps_previous_state_and_cleans_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    previous = _state(store, startup_id="previous")
    replacement = _state(store, startup_id="replacement")
    store.publish(previous)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"raw failure {TOKEN}")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(StateStorageError, match="publication failed") as error:
        store.publish(replacement)

    captured = capsys.readouterr()
    assert TOKEN not in str(error.value)
    assert TOKEN not in captured.out + captured.err + caplog.text
    assert store.load() == previous
    assert not _temporary_state_paths(store)


def test_publish_surfaces_temporary_unlink_failure_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    real_unlink = os.unlink

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    def fail_temporary_unlink(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if str(path).startswith(".sidecar-state-"):
            raise OSError("temporary unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as context:
        context.setattr(os, "replace", fail_replace)
        context.setattr(os, "unlink", fail_temporary_unlink)
        with pytest.raises(StateStorageError, match="cleanup failed") as error:
            store.publish(_state(store))

        captured = capsys.readouterr()
        assert TOKEN not in str(error.value)
        assert TOKEN not in captured.out + captured.err + caplog.text
        leftovers = _temporary_state_paths(store)
        assert len(leftovers) == 1
        assert stat.S_IMODE(os.lstat(leftovers[0]).st_mode) == 0o600
        assert TOKEN.encode() in leftovers[0].read_bytes()
    real_unlink(leftovers[0])
    retry = StateStore(store.runtime_root, project_id=PROJECT_ID)
    successor = _state(retry, startup_id="successor-after-cleanup-failure")
    retry.publish(successor)
    assert retry.load() == successor


def test_temporary_setup_cleanup_double_failure_hides_raw_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    store.publish(_state(store, startup_id="previous"))
    real_unlink = os.unlink

    def fail_fchmod(_file_descriptor: int, _mode: int) -> None:
        raise OSError(f"raw temporary setup failure {TOKEN}")

    def fail_temporary_unlink(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if os.fsdecode(path).startswith(".sidecar-state-"):
            raise OSError("temporary unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as context:
        context.setattr(os, "fchmod", fail_fchmod)
        context.setattr(os, "unlink", fail_temporary_unlink)
        with pytest.raises(StateStorageError, match="cleanup failed") as error:
            store.publish(_state(store, startup_id="replacement"))

        rendered = "".join(traceback.format_exception(error.value))
        captured = capsys.readouterr()
        assert TOKEN not in str(error.value)
        assert TOKEN not in rendered
        assert TOKEN not in captured.out + captured.err + caplog.text
        leftovers = _temporary_state_paths(store)
        assert len(leftovers) == 1
    real_unlink(leftovers[0])
    retry = StateStore(store.runtime_root, project_id=PROJECT_ID)
    successor = _state(retry, startup_id="successor-after-double-failure")
    retry.publish(successor)
    assert retry.load() == successor


def test_publish_normalizes_lock_close_failure_and_the_close_releases_flock(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    first = _state(store, startup_id="first")
    successor = _state(store, startup_id="successor")
    original_acquire = store._acquire_mutation_lock
    real_close = os.close
    lock_descriptor = -1
    injected = False

    def record_lock(directory_descriptor: int, *, exclusive: bool) -> int:
        nonlocal lock_descriptor
        lock_descriptor = original_acquire(directory_descriptor, exclusive=exclusive)
        return lock_descriptor

    def close_then_report_failure(file_descriptor: int) -> None:
        nonlocal injected
        real_close(file_descriptor)
        if file_descriptor == lock_descriptor and not injected:
            injected = True
            raise OSError(f"raw lock close failure {TOKEN}")

    monkeypatch.setattr(store, "_acquire_mutation_lock", record_lock)
    monkeypatch.setattr(os, "close", close_then_report_failure)
    with pytest.raises(StateStorageError, match="cleanup failed") as error:
        store.publish(first)

    captured = capsys.readouterr()
    assert injected is True
    assert TOKEN not in str(error.value)
    assert TOKEN not in captured.out + captured.err + caplog.text
    assert store.load() == first
    store.publish(successor)
    assert store.load() == successor


def test_publish_file_close_failure_does_not_retry_a_reused_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    previous = _state(store, startup_id="previous")
    store.publish(previous)
    real_close = os.close
    replacement_descriptor = -1
    injected = False

    def close_then_reuse(file_descriptor: int) -> None:
        nonlocal injected, replacement_descriptor
        metadata = os.fstat(file_descriptor)
        if not injected and stat.S_ISREG(metadata.st_mode):
            real_close(file_descriptor)
            replacement_descriptor = os.open(os.devnull, os.O_RDONLY)
            assert replacement_descriptor == file_descriptor
            injected = True
            raise OSError(f"raw close failure {TOKEN}")
        real_close(file_descriptor)

    monkeypatch.setattr(os, "close", close_then_reuse)
    with pytest.raises(StateStorageError, match="file close failed") as error:
        store.publish(_state(store, startup_id="replacement"))

    assert injected is True
    assert TOKEN not in "".join(traceback.format_exception(error.value))
    try:
        assert stat.S_ISCHR(os.fstat(replacement_descriptor).st_mode)
    finally:
        real_close(replacement_descriptor)
    assert store.load() == previous
    assert not _temporary_state_paths(store)


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_publish_cleanup_preserves_process_control_with_only_a_safe_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    control_type: type[BaseException],
) -> None:
    store = StateStore(tmp_path / "runtime", project_id=PROJECT_ID)
    store.publish(_state(store, startup_id="previous"))
    real_unlink = os.unlink

    def interrupt_write(
        _file_descriptor: int,
        _data: bytes | bytearray | memoryview,
    ) -> int:
        raise control_type("requested stop")

    def fail_temporary_unlink(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if os.fsdecode(path).startswith(".sidecar-state-"):
            raise OSError(f"raw cleanup failure {TOKEN}")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "write", interrupt_write)
    monkeypatch.setattr(os, "unlink", fail_temporary_unlink)
    with pytest.raises(control_type) as error:
        store.publish(_state(store, startup_id="replacement"))

    notes = getattr(error.value, "__notes__", [])
    rendered = "".join(traceback.format_exception(error.value))
    assert notes == ["state publication cleanup failed"]
    assert TOKEN not in rendered
    leftovers = _temporary_state_paths(store)
    assert len(leftovers) == 1
    real_unlink(leftovers[0])
