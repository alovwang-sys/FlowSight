from __future__ import annotations

import ast
import copy
import errno
import fcntl
import math
import os
import pickle
import resource
import socket
import stat
from pathlib import Path
from typing import NoReturn

import pytest

from flowsight.sidecar import (
    MAX_STARTUP_SIGNAL_BYTES,
    STARTUP_CHANNEL_SCHEMA_VERSION,
    StartupChannelError,
    StartupChannelErrorCode,
    StartupFailure,
    StartupFailureCode,
    StartupReader,
    StartupReady,
    StartupWriter,
    open_startup_channel,
)
from flowsight.sidecar import startup_channel as channel_module

TOKEN = "private-capability-token-value-0123456789"
STARTUP_ID = "0123456789abcdef0123456789abcdef"
READY = StartupReady(startup_id=STARTUP_ID, sidecar_pid=1234, port=4040)
FAILURE = StartupFailure(code=StartupFailureCode.SIDECAR_STARTUP_FAILED)
READY_FRAME = (
    b'{"startup_channel_schema_version":1,"status":"ready",'
    b'"startup_id":"0123456789abcdef0123456789abcdef",'
    b'"sidecar_pid":1234,"port":4040}\n'
)
FAILURE_FRAME = (
    b'{"startup_channel_schema_version":1,"status":"error","code":"SIDECAR_STARTUP_FAILED"}\n'
)

REAL_CLOSE = os.close
REAL_FSTAT = os.fstat
REAL_PIPE = os.pipe
REAL_READ = os.read
REAL_WRITE = os.write
REAL_FCNTL = fcntl.fcntl
REAL_FPATHCONF = os.fpathconf


class _DerivedInt(int):
    pass


class _DerivedStr(str):
    pass


class _Clock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def monotonic(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def _assert_closed(file_descriptor: int) -> None:
    with pytest.raises(OSError) as captured:
        REAL_FSTAT(file_descriptor)
    assert captured.value.errno == errno.EBADF


def _assert_private_error(
    error: StartupChannelError,
    code: StartupChannelErrorCode,
) -> None:
    assert error.code is code
    assert str(error) == f"sidecar startup channel failed ({code})"
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def _reader_with_frame(frame: bytes) -> StartupReader:
    reader, writer = open_startup_channel()
    written = REAL_WRITE(writer.fileno(), frame)
    assert written == len(frame)
    writer.close()
    return reader


def _receive_frame(frame: bytes, *, timeout: float = 0.5) -> object:
    return _reader_with_frame(frame).receive(timeout)


def test_real_ready_and_failure_round_trips_are_one_shot() -> None:
    for message in (READY, FAILURE):
        reader, writer = open_startup_channel()
        writer.send(message)

        assert reader.receive(1.0) == message
        with pytest.raises(StartupChannelError) as writer_closed:
            writer.fileno()
        with pytest.raises(StartupChannelError) as reader_closed:
            reader.fileno()
        _assert_private_error(
            writer_closed.value,
            StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED,
        )
        _assert_private_error(
            reader_closed.value,
            StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED,
        )
        writer.close()
        reader.close()


@pytest.mark.parametrize(
    ("message", "expected"),
    [(READY, READY_FRAME), (FAILURE, FAILURE_FRAME)],
)
def test_writer_emits_one_exact_canonical_frame(
    message: StartupReady | StartupFailure,
    expected: bytes,
) -> None:
    reader, writer = open_startup_channel()

    writer.send(message)

    assert REAL_READ(reader.fileno(), MAX_STARTUP_SIGNAL_BYTES) == expected
    assert REAL_READ(reader.fileno(), 1) == b""
    assert len(expected) <= MAX_STARTUP_SIGNAL_BYTES
    reader.close()


def test_factory_returns_exact_safe_directional_pipe_handles() -> None:
    reader, writer = open_startup_channel()
    try:
        read_descriptor = reader.fileno()
        write_descriptor = writer.fileno()
        assert read_descriptor >= 3
        assert write_descriptor >= 3
        assert read_descriptor != write_descriptor
        assert stat.S_ISFIFO(REAL_FSTAT(read_descriptor).st_mode)
        assert stat.S_ISFIFO(REAL_FSTAT(write_descriptor).st_mode)
        assert REAL_FCNTL(read_descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        assert REAL_FCNTL(write_descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
        assert os.get_blocking(read_descriptor) is False
        assert os.get_blocking(write_descriptor) is False
        assert os.get_inheritable(read_descriptor) is False
        assert os.get_inheritable(write_descriptor) is False
        assert REAL_FPATHCONF(read_descriptor, "PC_PIPE_BUF") >= MAX_STARTUP_SIGNAL_BYTES
        assert REAL_FPATHCONF(write_descriptor, "PC_PIPE_BUF") >= MAX_STARTUP_SIGNAL_BYTES
        assert repr(reader) == "<StartupReader active>"
        assert repr(writer) == "<StartupWriter active>"
        assert str(read_descriptor) not in repr(reader)
        assert str(write_descriptor) not in repr(writer)
    finally:
        reader.close()
        writer.close()
    assert repr(reader) == "<StartupReader closed>"
    assert repr(writer) == "<StartupWriter closed>"


@pytest.mark.parametrize("handle_type", [StartupReader, StartupWriter])
def test_handles_cannot_be_constructed_directly(handle_type: type[object]) -> None:
    with pytest.raises(TypeError, match="must be"):
        handle_type()


@pytest.mark.parametrize("copier", [copy.copy, copy.deepcopy, pickle.dumps])
def test_handles_are_move_only_and_reject_copy_or_pickle(copier: object) -> None:
    reader, writer = open_startup_channel()
    try:
        for handle in (reader, writer):
            with pytest.raises(TypeError, match="move-only"):
                copier(handle)  # type: ignore[operator]
        assert reader.fileno() >= 3
        assert writer.fileno() >= 3
    finally:
        reader.close()
        writer.close()


def test_context_and_explicit_close_are_idempotent() -> None:
    reader, writer = open_startup_channel()
    with reader as entered_reader:
        assert entered_reader is reader
    with writer as entered_writer:
        assert entered_writer is writer
    reader.close()
    writer.close()


def test_real_round_trip_accepts_descriptors_above_select_fd_set_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_soft, original_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    minimum_high_descriptor = 1200
    required_limit = minimum_high_descriptor + 4
    if original_hard != resource.RLIM_INFINITY and original_hard < required_limit:
        pytest.skip("hard descriptor limit cannot reach the high-FD regression boundary")

    reader: StartupReader | None = None
    writer: StartupWriter | None = None
    low_read = -1
    low_write = -1
    high_read = -1
    high_write = -1
    try:
        if original_soft != resource.RLIM_INFINITY and original_soft < required_limit:
            resource.setrlimit(resource.RLIMIT_NOFILE, (required_limit, original_hard))
        low_read, low_write = REAL_PIPE()
        high_read = REAL_FCNTL(
            low_read,
            fcntl.F_DUPFD_CLOEXEC,
            minimum_high_descriptor,
        )
        high_write = REAL_FCNTL(
            low_write,
            fcntl.F_DUPFD_CLOEXEC,
            minimum_high_descriptor,
        )
        REAL_CLOSE(low_read)
        low_read = -1
        REAL_CLOSE(low_write)
        low_write = -1
        returned = (high_read, high_write)
        monkeypatch.setattr(channel_module.os, "pipe", lambda: returned)
        high_read = -1
        high_write = -1

        reader, writer = open_startup_channel()
        assert reader.fileno() >= minimum_high_descriptor
        assert writer.fileno() >= minimum_high_descriptor
        writer.send(READY)
        assert reader.receive() == READY
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        for descriptor in (low_read, low_write, high_read, high_write):
            if descriptor >= 0:
                REAL_CLOSE(descriptor)
        resource.setrlimit(resource.RLIMIT_NOFILE, (original_soft, original_hard))
    with pytest.raises(StartupChannelError) as read_error:
        reader.__enter__()
    with pytest.raises(StartupChannelError) as write_error:
        writer.__enter__()
    _assert_private_error(read_error.value, StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)
    _assert_private_error(write_error.value, StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)


@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    [
        ("startup_id", "", ValueError),
        ("startup_id", "x" * 129, ValueError),
        ("startup_id", "not safe", ValueError),
        ("startup_id", "not/safe", ValueError),
        ("startup_id", "é", ValueError),
        ("startup_id", "a" * 31, ValueError),
        ("startup_id", "a" * 33, ValueError),
        ("startup_id", "A" * 32, ValueError),
        ("startup_id", "_" * 32, ValueError),
        ("startup_id", _DerivedStr("a" * 32), TypeError),
        ("startup_id", object(), TypeError),
        ("sidecar_pid", 0, ValueError),
        ("sidecar_pid", 2**31, ValueError),
        ("sidecar_pid", True, TypeError),
        ("sidecar_pid", _DerivedInt(1), TypeError),
        ("port", 0, ValueError),
        ("port", 65_536, ValueError),
        ("port", True, TypeError),
        ("port", _DerivedInt(4040), TypeError),
    ],
)
def test_ready_message_rejects_unsafe_or_inexact_scalars(
    field: str,
    value: object,
    error_type: type[Exception],
) -> None:
    values: dict[str, object] = {
        "startup_id": STARTUP_ID,
        "sidecar_pid": 1,
        "port": 1,
    }
    values[field] = value
    with pytest.raises(error_type):
        StartupReady(**values)  # type: ignore[arg-type]


def test_message_boundaries_and_failure_code_are_exact() -> None:
    assert StartupReady("a" * 32, 1, 1) == StartupReady("a" * 32, 1, 1)
    assert StartupReady("f" * 32, 2**31 - 1, 65_535).port == 65_535
    assert STARTUP_CHANNEL_SCHEMA_VERSION == 1
    with pytest.raises(TypeError, match="exact StartupFailureCode"):
        StartupFailure("SIDECAR_STARTUP_FAILED")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exact StartupChannelErrorCode"):
        StartupChannelError(TOKEN)  # type: ignore[arg-type]


def test_invalid_or_forged_message_leaves_writer_usable() -> None:
    reader, writer = open_startup_channel()
    with pytest.raises(TypeError, match="exact startup message"):
        writer.send(object())  # type: ignore[arg-type]
    forged = StartupReady(STARTUP_ID, 1, 1)
    object.__setattr__(forged, "startup_id", TOKEN)
    with pytest.raises(ValueError, match="lowercase hex"):
        writer.send(forged)
    assert writer.fileno() >= 3
    writer.send(READY)
    assert reader.receive() == READY


@pytest.mark.parametrize(
    "frame",
    [
        b"{}\n",
        b"[]\n",
        b"null\n",
        b"not-json\n",
        b"\xff\n",
        b"\xef\xbb\xbf{}\n",
        b"{\x00}\n",
        b'{"startup_channel_schema_version":NaN}\n',
        READY_FRAME[:-1],
        READY_FRAME[:-1] + b"\r\n",
        b" " + READY_FRAME,
        READY_FRAME[:-1] + b" \n",
        READY_FRAME + b"x",
        READY_FRAME + FAILURE_FRAME,
        READY_FRAME.replace(b'"status":"ready"', b'"status":"ready","status":"ready"'),
        READY_FRAME.replace(b'"startup_channel_schema_version":1', b'"x":1'),
        READY_FRAME.replace(
            b'"startup_channel_schema_version":1', b'"startup_channel_schema_version":2'
        ),
        READY_FRAME.replace(
            b'"startup_channel_schema_version":1', b'"startup_channel_schema_version":true'
        ),
        READY_FRAME.replace(b'"status":"ready"', b'"status":"unknown"'),
        READY_FRAME.replace(b'"sidecar_pid":1234', b'"sidecar_pid":true'),
        READY_FRAME.replace(b'"port":4040', b'"port":true'),
        FAILURE_FRAME.replace(b"SIDECAR_STARTUP_FAILED", b"PRIVATE_RAW_FAILURE"),
        FAILURE_FRAME.replace(b'"code":', b'"extra":1,"code":'),
        (
            b'{"status":"ready","startup_channel_schema_version":1,'
            b'"startup_id":"0123456789abcdef0123456789abcdef",'
            b'"sidecar_pid":1234,"port":4040}\n'
        ),
    ],
)
def test_noncanonical_or_invalid_frames_fail_closed(frame: bytes) -> None:
    with pytest.raises(StartupChannelError) as captured:
        _receive_frame(frame)
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID,
    )


def test_empty_eof_and_oversized_frame_have_distinct_fixed_errors() -> None:
    reader, writer = open_startup_channel()
    writer.close()
    with pytest.raises(StartupChannelError) as empty:
        reader.receive()
    _assert_private_error(empty.value, StartupChannelErrorCode.STARTUP_CHANNEL_CLOSED)

    oversized = b"x" * MAX_STARTUP_SIGNAL_BYTES + b"\n"
    with pytest.raises(StartupChannelError) as too_large:
        _receive_frame(oversized)
    _assert_private_error(
        too_large.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_TOO_LARGE,
    )


def test_exact_frame_succeeds_when_read_one_byte_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _reader_with_frame(READY_FRAME)
    amounts: list[int] = []

    def one_byte(file_descriptor: int, amount: int) -> bytes:
        amounts.append(amount)
        return REAL_READ(file_descriptor, min(amount, 1))

    monkeypatch.setattr(channel_module.os, "read", one_byte)
    assert reader.receive(1.0) == READY
    assert len(amounts) == len(READY_FRAME) + 1
    assert all(1 <= amount <= MAX_STARTUP_SIGNAL_BYTES + 1 for amount in amounts)


@pytest.mark.parametrize("one_byte", [False, True])
def test_trailing_bytes_fail_independently_of_packetization(
    monkeypatch: pytest.MonkeyPatch,
    one_byte: bool,
) -> None:
    reader = _reader_with_frame(READY_FRAME + b"later")
    if one_byte:
        monkeypatch.setattr(
            channel_module.os,
            "read",
            lambda file_descriptor, amount: REAL_READ(file_descriptor, min(amount, 1)),
        )
    with pytest.raises(StartupChannelError) as captured:
        reader.receive(1.0)
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_MESSAGE_INVALID,
    )


@pytest.mark.parametrize(
    "invalid_timeout",
    [
        True,
        False,
        "0.5",
        object(),
        0,
        0.0,
        -0.1,
        math.nan,
        math.inf,
        -math.inf,
        channel_module.MAX_STARTUP_CHANNEL_TIMEOUT_SECONDS + 0.1,
        pytest.param(10**10_000, id="huge-int"),
    ],
)
def test_invalid_timeout_leaves_reader_usable(invalid_timeout: object) -> None:
    reader, writer = open_startup_channel()
    with pytest.raises((TypeError, ValueError)):
        reader.receive(invalid_timeout)  # type: ignore[arg-type]
    assert reader.fileno() >= 3
    writer.send(READY)
    assert reader.receive(1.0) == READY


def test_direct_poll_timeout_consumes_reader_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    reads = 0

    def unexpected_read(_descriptor: int, _amount: int) -> NoReturn:
        nonlocal reads
        reads += 1
        raise AssertionError("poll timeout performed a read")

    monkeypatch.setattr(channel_module, "_poll_readable", lambda *_args: False)
    monkeypatch.setattr(channel_module.os, "read", unexpected_read)
    with pytest.raises(StartupChannelError) as captured:
        reader.receive(0.25)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_TIMEOUT)
    assert reads == 0
    writer.close()


def test_readable_slow_drip_cannot_extend_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _reader_with_frame(READY_FRAME)
    clock = _Clock()
    real_poll_readable = channel_module._poll_readable
    timeouts: list[float] = []

    def slow_poll(file_descriptor: int, timeout: float) -> bool:
        timeouts.append(timeout)
        clock.advance(0.4)
        return real_poll_readable(file_descriptor, timeout)

    monkeypatch.setattr(channel_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(channel_module, "_poll_readable", slow_poll)
    monkeypatch.setattr(
        channel_module.os,
        "read",
        lambda file_descriptor, amount: REAL_READ(file_descriptor, min(amount, 1)),
    )
    with pytest.raises(StartupChannelError) as captured:
        reader.receive(1.0)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_TIMEOUT)
    assert timeouts == pytest.approx([1.0, 0.6, 0.2])
    assert clock.current == pytest.approx(1.2)


def test_final_decode_work_is_inside_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _reader_with_frame(READY_FRAME)
    clock = _Clock()
    real_decode = channel_module._decode_message

    def late_decode(frame: bytes) -> object:
        result = real_decode(frame)
        clock.advance(1.01)
        return result

    monkeypatch.setattr(channel_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(channel_module, "_decode_message", late_decode)
    with pytest.raises(StartupChannelError) as captured:
        reader.receive(1.0)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_TIMEOUT)


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_clock_process_control_consumes_reader_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    reader, writer = open_startup_channel()
    reader_descriptor = reader.fileno()
    error = error_factory()
    monkeypatch.setattr(
        channel_module.time,
        "monotonic",
        lambda: (_ for _ in ()).throw(error),
    )
    with pytest.raises(error_factory) as captured:
        reader.receive(1.0)
    assert captured.value is error
    with pytest.raises(StartupChannelError):
        reader.fileno()
    _assert_closed(reader_descriptor)
    writer.close()


def test_ordinary_clock_failure_is_fixed_and_consumes_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    monkeypatch.setattr(
        channel_module.time,
        "monotonic",
        lambda: (_ for _ in ()).throw(OSError(f"private {TOKEN}")),
    )
    with pytest.raises(StartupChannelError) as captured:
        reader.receive(1.0)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED)
    writer.close()


def test_io_reasserts_nonblocking_and_noninheritable_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    os.set_blocking(writer.fileno(), True)
    os.set_inheritable(writer.fileno(), True)
    write_calls = 0

    def checked_write(file_descriptor: int, encoded: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        assert os.get_blocking(file_descriptor) is False
        assert os.get_inheritable(file_descriptor) is False
        return REAL_WRITE(file_descriptor, encoded)

    monkeypatch.setattr(channel_module.os, "write", checked_write)
    writer.send(READY)
    assert write_calls == 1

    os.set_blocking(reader.fileno(), True)
    os.set_inheritable(reader.fileno(), True)

    def checked_read(file_descriptor: int, amount: int) -> bytes:
        assert os.get_blocking(file_descriptor) is False
        assert os.get_inheritable(file_descriptor) is False
        return REAL_READ(file_descriptor, amount)

    monkeypatch.setattr(channel_module.os, "read", checked_read)
    assert reader.receive() == READY


@pytest.mark.parametrize("write_result", [0, 1, True, len(READY_FRAME) + 1])
def test_zero_partial_or_invalid_write_result_fails_once_and_consumes_writer(
    monkeypatch: pytest.MonkeyPatch,
    write_result: object,
) -> None:
    reader, writer = open_startup_channel()
    calls = 0

    def fake_write(_descriptor: int, _encoded: bytes) -> object:
        nonlocal calls
        calls += 1
        return write_result

    monkeypatch.setattr(channel_module.os, "write", fake_write)
    with pytest.raises(StartupChannelError) as captured:
        writer.send(READY)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED)
    assert calls == 1
    with pytest.raises(StartupChannelError):
        writer.fileno()
    reader.close()


@pytest.mark.parametrize(
    "error", [BlockingIOError(), BrokenPipeError(), OSError(f"private {TOKEN}")]
)
def test_ordinary_write_failures_are_fixed_private_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    reader, writer = open_startup_channel()
    calls = 0

    def failing_write(_descriptor: int, _encoded: bytes) -> NoReturn:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(channel_module.os, "write", failing_write)
    with pytest.raises(StartupChannelError) as captured:
        writer.send(READY)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED)
    assert calls == 1
    reader.close()


def test_real_full_pipe_backpressure_fails_immediately_without_retry() -> None:
    reader, writer = open_startup_channel()
    descriptor = writer.fileno()
    while True:
        try:
            REAL_WRITE(descriptor, b"x" * MAX_STARTUP_SIGNAL_BYTES)
        except BlockingIOError:
            break
    with pytest.raises(StartupChannelError) as captured:
        writer.send(READY)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED)
    reader.close()


def test_real_reader_close_causes_fixed_writer_failure() -> None:
    reader, writer = open_startup_channel()
    reader.close()
    with pytest.raises(StartupChannelError) as captured:
        writer.send(READY)
    _assert_private_error(captured.value, StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED)


def test_adopted_writer_restores_flags_and_completes_round_trip() -> None:
    reader, parent_writer = open_startup_channel()
    inherited = os.dup(parent_writer.fileno())
    parent_writer.close()
    os.set_inheritable(inherited, True)
    os.set_blocking(inherited, True)

    child_writer = StartupWriter.adopt_inherited(inherited)
    assert child_writer.fileno() == inherited
    assert os.get_inheritable(inherited) is False
    assert os.get_blocking(inherited) is False
    child_writer.send(READY)
    assert reader.receive() == READY


@pytest.mark.parametrize("invalid_descriptor", [True, False, "3", object(), -1, 0, 1, 2])
def test_invalid_adoption_input_does_not_touch_a_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    invalid_descriptor: object,
) -> None:
    monkeypatch.setattr(
        channel_module.os,
        "fstat",
        lambda _descriptor: (_ for _ in ()).throw(
            AssertionError("invalid input touched a descriptor")
        ),
    )
    with pytest.raises((TypeError, ValueError)):
        StartupWriter.adopt_inherited(invalid_descriptor)  # type: ignore[arg-type]


def test_initial_adoption_ebadf_never_closes_a_reused_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    companion, candidate = REAL_PIPE()
    replacement_read, replacement_write = REAL_PIPE()
    REAL_CLOSE(candidate)
    close_calls: list[int] = []

    def stale_fstat(file_descriptor: int) -> NoReturn:
        assert file_descriptor == candidate
        os.dup2(replacement_write, candidate, inheritable=False)
        raise OSError(errno.EBADF, f"private {TOKEN}")

    def tracking_close(file_descriptor: int) -> None:
        close_calls.append(file_descriptor)
        REAL_CLOSE(file_descriptor)

    monkeypatch.setattr(channel_module.os, "fstat", stale_fstat)
    monkeypatch.setattr(channel_module.os, "close", tracking_close)
    try:
        with pytest.raises(StartupChannelError) as captured:
            StartupWriter.adopt_inherited(candidate)
        _assert_private_error(
            captured.value,
            StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID,
        )
        assert close_calls == []
        assert REAL_FSTAT(candidate).st_mode == REAL_FSTAT(replacement_write).st_mode
    finally:
        REAL_CLOSE(candidate)
        REAL_CLOSE(replacement_write)
        REAL_CLOSE(replacement_read)
        REAL_CLOSE(companion)


@pytest.mark.parametrize("kind", ["read-end", "file", "directory", "socket", "closed"])
def test_adoption_rejects_and_consumes_wrong_or_closed_endpoint(
    tmp_path: Path,
    kind: str,
) -> None:
    companion = -1
    if kind == "read-end":
        candidate, companion = REAL_PIPE()
    elif kind == "file":
        candidate = os.open(tmp_path / "regular", os.O_RDWR | os.O_CREAT, 0o600)
    elif kind == "directory":
        candidate = os.open(tmp_path, os.O_RDONLY)
    elif kind == "socket":
        candidate = socket.socket().detach()
    else:
        read_descriptor, candidate = REAL_PIPE()
        REAL_CLOSE(read_descriptor)
        REAL_CLOSE(candidate)
    try:
        with pytest.raises(StartupChannelError) as captured:
            StartupWriter.adopt_inherited(candidate)
        _assert_private_error(
            captured.value,
            StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID,
        )
        _assert_closed(candidate)
    finally:
        if companion >= 0:
            REAL_CLOSE(companion)


@pytest.mark.parametrize("stage", ["fcntl", "fpathconf", "set_inheritable", "set_blocking"])
def test_adoption_closes_once_when_later_validation_reports_ebadf(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    read_descriptor, candidate = REAL_PIPE()
    close_calls: list[int] = []

    def tracking_close(file_descriptor: int) -> None:
        close_calls.append(file_descriptor)
        REAL_CLOSE(file_descriptor)

    monkeypatch.setattr(channel_module.os, "close", tracking_close)
    if stage == "fcntl":
        monkeypatch.setattr(
            channel_module.fcntl,
            "fcntl",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EBADF, "private")),
        )
    elif stage == "fpathconf":
        monkeypatch.setattr(
            channel_module.os,
            "fpathconf",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EBADF, "private")),
        )
    elif stage == "set_inheritable":
        monkeypatch.setattr(
            channel_module.os,
            "set_inheritable",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EBADF, "private")),
        )
    else:
        monkeypatch.setattr(
            channel_module.os,
            "set_blocking",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EBADF, "private")),
        )
    try:
        with pytest.raises(StartupChannelError) as captured:
            StartupWriter.adopt_inherited(candidate)
        _assert_private_error(
            captured.value,
            StartupChannelErrorCode.STARTUP_CHANNEL_DESCRIPTOR_INVALID,
        )
        assert close_calls == [candidate]
        _assert_closed(candidate)
    finally:
        REAL_CLOSE(read_descriptor)


@pytest.mark.parametrize("shape", ["one", "three", "list", "mixed", "duplicate"])
def test_malformed_pipe_results_close_every_safely_returned_descriptor_once(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    first_read, first_write = REAL_PIPE()
    extra_read, extra_write = REAL_PIPE()
    if shape == "one":
        returned: object = (first_read,)
        owned = [first_read]
    elif shape == "three":
        returned = (first_read, first_write, extra_read)
        owned = [first_read, first_write, extra_read]
    elif shape == "list":
        returned = [first_read, first_write]
        owned = [first_read, first_write]
    elif shape == "mixed":
        returned = (first_read, object())
        owned = [first_read]
    else:
        returned = (first_read, first_read)
        owned = [first_read]
    monkeypatch.setattr(channel_module.os, "pipe", lambda: returned)
    try:
        with pytest.raises(StartupChannelError) as captured:
            open_startup_channel()
        _assert_private_error(
            captured.value,
            StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED,
        )
        for descriptor in owned:
            _assert_closed(descriptor)
    finally:
        for descriptor in (first_write, extra_read, extra_write):
            if descriptor not in owned:
                try:
                    REAL_CLOSE(descriptor)
                except OSError:
                    pass


def test_closed_stdio_pipe_descriptors_are_promoted_before_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate_calls: list[tuple[int, int, int]] = []
    close_calls: list[int] = []

    monkeypatch.setattr(channel_module.os, "pipe", lambda: (0, 1))

    def duplicate(file_descriptor: int, operation: int, minimum: int = 0) -> int:
        duplicate_calls.append((file_descriptor, operation, minimum))
        return 10 + file_descriptor

    monkeypatch.setattr(channel_module.fcntl, "fcntl", duplicate)
    monkeypatch.setattr(channel_module, "_prepare_endpoint", lambda *_args: None)
    monkeypatch.setattr(channel_module.os, "close", close_calls.append)

    reader, writer = open_startup_channel()
    assert reader.fileno() == 10
    assert writer.fileno() == 11
    reader.close()
    writer.close()
    assert duplicate_calls == [
        (0, fcntl.F_DUPFD_CLOEXEC, 3),
        (1, fcntl.F_DUPFD_CLOEXEC, 3),
    ]
    assert close_calls == [0, 1, 10, 11]
    assert 2 not in close_calls


def test_low_descriptor_close_failure_never_retries_source_or_leaks_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_calls: list[int] = []
    monkeypatch.setattr(channel_module.os, "pipe", lambda: (0, 1))
    monkeypatch.setattr(channel_module.fcntl, "fcntl", lambda *_args: 10)

    def ambiguous_close(file_descriptor: int) -> None:
        close_calls.append(file_descriptor)
        if file_descriptor == 0:
            raise OSError("ambiguous")

    monkeypatch.setattr(channel_module.os, "close", ambiguous_close)
    with pytest.raises(StartupChannelError) as captured:
        open_startup_channel()
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_CLEANUP_FAILED,
    )
    assert close_calls == [0, 10, 1]


@pytest.mark.parametrize("failure_stage", ["first-dup", "invalid-dup", "second-dup"])
def test_low_descriptor_promotion_failures_close_owned_endpoints_once_without_stdio(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    close_calls: list[int] = []
    duplicate_calls = 0
    monkeypatch.setattr(channel_module.os, "pipe", lambda: (0, 1))
    monkeypatch.setattr(channel_module.os, "close", close_calls.append)

    def duplicate(_descriptor: int, _operation: int, _minimum: int = 0) -> int:
        nonlocal duplicate_calls
        duplicate_calls += 1
        if failure_stage == "first-dup" or (failure_stage == "second-dup" and duplicate_calls == 2):
            raise OSError("duplicate failed")
        if failure_stage == "invalid-dup":
            return 2
        return 10

    monkeypatch.setattr(channel_module.fcntl, "fcntl", duplicate)
    with pytest.raises(StartupChannelError) as captured:
        open_startup_channel()
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED,
    )
    expected = [0, 1] if failure_stage != "second-dup" else [0, 10, 1]
    assert close_calls == expected
    assert 2 not in close_calls


def test_second_endpoint_setup_failure_closes_both_real_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptors: list[int] = []
    calls = 0

    def tracked_pipe() -> tuple[int, int]:
        pair = REAL_PIPE()
        descriptors.extend(pair)
        return pair

    def fail_second(_descriptor: int, _access: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(f"private {TOKEN}")

    monkeypatch.setattr(channel_module.os, "pipe", tracked_pipe)
    monkeypatch.setattr(channel_module, "_prepare_endpoint", fail_second)
    with pytest.raises(StartupChannelError) as captured:
        open_startup_channel()
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_SETUP_FAILED,
    )
    assert calls == 2
    for descriptor in descriptors:
        _assert_closed(descriptor)


@pytest.mark.parametrize("stage", ["pipe", "low-dup", "low-source-close", "configure"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_setup_process_control_preserves_identity_and_closes_owned_descriptors_once(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    error = error_factory()
    close_calls: list[int] = []
    real_descriptors: list[int] = []
    if stage == "pipe":
        monkeypatch.setattr(
            channel_module.os,
            "pipe",
            lambda: (_ for _ in ()).throw(error),
        )
    elif stage in {"low-dup", "low-source-close"}:
        monkeypatch.setattr(channel_module.os, "pipe", lambda: (0, 1))

        def duplicate(*_args: object) -> int:
            if stage == "low-dup":
                raise error
            return 10

        def close(file_descriptor: int) -> None:
            close_calls.append(file_descriptor)
            if stage == "low-source-close" and file_descriptor == 0:
                raise error

        monkeypatch.setattr(channel_module.fcntl, "fcntl", duplicate)
        monkeypatch.setattr(channel_module.os, "close", close)
    else:
        pair = REAL_PIPE()
        real_descriptors.extend(pair)
        monkeypatch.setattr(channel_module.os, "pipe", lambda: pair)
        monkeypatch.setattr(
            channel_module,
            "_prepare_endpoint",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    with pytest.raises(error_factory) as captured:
        open_startup_channel()
    assert captured.value is error
    if stage == "low-dup":
        assert close_calls == [0, 1]
    elif stage == "low-source-close":
        assert close_calls == [0, 10, 1]
    elif stage == "configure":
        for descriptor in real_descriptors:
            _assert_closed(descriptor)


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("followup_kind", ["ordinary", "process-control"])
def test_cleanup_origin_process_control_gets_note_after_another_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
    followup_kind: str,
) -> None:
    first_error = error_factory()
    if followup_kind == "ordinary":
        followup_error: BaseException = OSError(f"private {TOKEN}")
    elif error_factory is KeyboardInterrupt:
        followup_error = SystemExit()
    else:
        followup_error = KeyboardInterrupt()
    close_calls: list[int] = []

    monkeypatch.setattr(channel_module.os, "pipe", lambda: (10, 11))
    monkeypatch.setattr(
        channel_module,
        "_prepare_endpoint",
        lambda *_args: (_ for _ in ()).throw(OSError(f"private {TOKEN}")),
    )

    def fail_close(file_descriptor: int) -> NoReturn:
        close_calls.append(file_descriptor)
        if file_descriptor == 10:
            raise first_error
        raise followup_error

    monkeypatch.setattr(channel_module.os, "close", fail_close)
    with pytest.raises(error_factory) as captured:
        open_startup_channel()
    assert captured.value is first_error
    assert captured.value.__notes__ == ["sidecar startup channel cleanup failed"]
    assert close_calls == [10, 11]


@pytest.mark.parametrize("stage", ["initial-fstat", "fcntl", "configure"])
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_adoption_process_control_preserves_identity_and_consumes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    read_descriptor, candidate = REAL_PIPE()
    error = error_factory()
    if stage == "initial-fstat":
        monkeypatch.setattr(
            channel_module.os,
            "fstat",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    elif stage == "fcntl":
        monkeypatch.setattr(
            channel_module.fcntl,
            "fcntl",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    else:
        monkeypatch.setattr(
            channel_module.os,
            "set_blocking",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    try:
        with pytest.raises(error_factory) as captured:
            StartupWriter.adopt_inherited(candidate)
        assert captured.value is error
        _assert_closed(candidate)
    finally:
        REAL_CLOSE(read_descriptor)


@pytest.mark.parametrize(
    "stage",
    ["writer-prepare", "reader-prepare", "poll", "decode"],
)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_one_shot_process_control_at_transport_stages_preserves_identity_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    error = error_factory()
    if stage == "writer-prepare":
        reader, writer = open_startup_channel()
        target = writer
    else:
        reader = _reader_with_frame(READY_FRAME)
        writer = None
        target = reader
    target_descriptor = target.fileno()
    if stage in {"writer-prepare", "reader-prepare"}:
        monkeypatch.setattr(
            channel_module,
            "_prepare_endpoint",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    elif stage == "poll":
        monkeypatch.setattr(
            channel_module,
            "_poll_readable",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    else:
        monkeypatch.setattr(
            channel_module,
            "_decode_message",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    with pytest.raises(error_factory) as captured:
        if writer is None:
            reader.receive()
        else:
            writer.send(READY)
    assert captured.value is error
    with pytest.raises(StartupChannelError):
        target.fileno()
    _assert_closed(target_descriptor)
    if writer is not None:
        reader.close()


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_write_process_control_preserves_identity_and_one_cleanup_note(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    reader, writer = open_startup_channel()
    target = writer.fileno()
    error = error_factory()
    close_calls = 0

    def fail_write(_descriptor: int, _encoded: bytes) -> NoReturn:
        raise error

    def fail_close(file_descriptor: int) -> None:
        nonlocal close_calls
        if file_descriptor == target:
            close_calls += 1
            raise OSError("ambiguous")
        REAL_CLOSE(file_descriptor)

    with monkeypatch.context() as patcher:
        patcher.setattr(channel_module.os, "write", fail_write)
        patcher.setattr(channel_module.os, "close", fail_close)
        with pytest.raises(error_factory) as captured:
            writer.send(READY)
    assert captured.value is error
    assert captured.value.__notes__ == ["sidecar startup channel cleanup failed"]
    assert close_calls == 1
    REAL_CLOSE(target)
    reader.close()


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_read_process_control_preserves_identity_and_one_cleanup_note(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: type[BaseException],
) -> None:
    reader = _reader_with_frame(READY_FRAME)
    target = reader.fileno()
    error = error_factory()
    close_calls = 0

    def fail_read(_descriptor: int, _amount: int) -> NoReturn:
        raise error

    def fail_close(file_descriptor: int) -> None:
        nonlocal close_calls
        if file_descriptor == target:
            close_calls += 1
            raise OSError("ambiguous")
        REAL_CLOSE(file_descriptor)

    with monkeypatch.context() as patcher:
        patcher.setattr(channel_module.os, "read", fail_read)
        patcher.setattr(channel_module.os, "close", fail_close)
        with pytest.raises(error_factory) as captured:
            reader.receive()
    assert captured.value is error
    assert captured.value.__notes__ == ["sidecar startup channel cleanup failed"]
    assert close_calls == 1
    REAL_CLOSE(target)


def test_successful_write_with_ordinary_cleanup_failure_returns_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    target = writer.fileno()
    close_calls = 0

    def close_then_report(file_descriptor: int) -> None:
        nonlocal close_calls
        if file_descriptor == target:
            close_calls += 1
            REAL_CLOSE(file_descriptor)
            raise OSError("ambiguous cleanup")
        REAL_CLOSE(file_descriptor)

    with monkeypatch.context() as patcher:
        patcher.setattr(channel_module.os, "close", close_then_report)
        with pytest.raises(StartupChannelError) as captured:
            writer.send(READY)
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_CLEANUP_FAILED,
    )
    assert close_calls == 1
    assert reader.receive() == READY


def test_successful_receive_with_ordinary_cleanup_failure_returns_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _reader_with_frame(READY_FRAME)
    target = reader.fileno()
    close_calls = 0

    def close_then_report(file_descriptor: int) -> None:
        nonlocal close_calls
        if file_descriptor == target:
            close_calls += 1
            REAL_CLOSE(file_descriptor)
            raise OSError("ambiguous cleanup")
        REAL_CLOSE(file_descriptor)

    with monkeypatch.context() as patcher:
        patcher.setattr(channel_module.os, "close", close_then_report)
        with pytest.raises(StartupChannelError) as captured:
            reader.receive()
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_CLEANUP_FAILED,
    )
    assert close_calls == 1


def test_complete_ready_frame_without_writer_eof_times_out_and_never_succeeds() -> None:
    reader, writer = open_startup_channel()
    assert REAL_WRITE(writer.fileno(), READY_FRAME) == len(READY_FRAME)
    try:
        with pytest.raises(StartupChannelError) as captured:
            reader.receive(0.02)
        _assert_private_error(
            captured.value,
            StartupChannelErrorCode.STARTUP_CHANNEL_TIMEOUT,
        )
    finally:
        writer.close()


@pytest.mark.parametrize(
    "stage", ["poll", "read", "read-result", "reader-prepare", "writer-prepare"]
)
def test_ordinary_transport_and_reprepare_failures_are_fixed_private_and_consume(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    if stage == "writer-prepare":
        reader, writer = open_startup_channel()
        target: StartupReader | StartupWriter = writer
        expected = StartupChannelErrorCode.STARTUP_CHANNEL_WRITE_FAILED
    else:
        reader = _reader_with_frame(READY_FRAME)
        writer = None
        target = reader
        expected = StartupChannelErrorCode.STARTUP_CHANNEL_READ_FAILED
    private_error = OSError(f"private {TOKEN}")
    if stage in {"reader-prepare", "writer-prepare"}:
        monkeypatch.setattr(
            channel_module,
            "_prepare_endpoint",
            lambda *_args: (_ for _ in ()).throw(private_error),
        )
    elif stage == "poll":
        monkeypatch.setattr(
            channel_module,
            "_poll_readable",
            lambda *_args: (_ for _ in ()).throw(private_error),
        )
    elif stage == "read":
        monkeypatch.setattr(
            channel_module.os,
            "read",
            lambda *_args: (_ for _ in ()).throw(private_error),
        )
    else:
        monkeypatch.setattr(channel_module.os, "read", lambda *_args: bytearray(b"x"))
    with pytest.raises(StartupChannelError) as captured:
        if writer is None:
            reader.receive()
        else:
            writer.send(READY)
    _assert_private_error(captured.value, expected)
    with pytest.raises(StartupChannelError):
        target.fileno()
    if writer is not None:
        reader.close()


def test_maximal_legal_ready_message_remains_atomic_and_round_trips() -> None:
    message = StartupReady("f" * 32, 2**31 - 1, 65_535)
    reader, writer = open_startup_channel()
    writer.send(message)
    assert reader.receive() == message
    encoded = channel_module._encode_message(message)
    assert len(encoded) <= MAX_STARTUP_SIGNAL_BYTES


def test_cleanup_process_control_without_active_error_propagates() -> None:
    reader, writer = open_startup_channel()
    target = writer.fileno()
    cleanup_error = KeyboardInterrupt()

    def fail_close(file_descriptor: int) -> None:
        if file_descriptor == target:
            raise cleanup_error
        REAL_CLOSE(file_descriptor)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(channel_module.os, "close", fail_close)
        with pytest.raises(KeyboardInterrupt) as captured:
            writer.send(READY)
    assert captured.value is cleanup_error
    REAL_CLOSE(target)
    assert reader.receive() == READY


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_context_cleanup_control_overrides_ordinary_body_error(
    error_factory: type[BaseException],
) -> None:
    reader, writer = open_startup_channel()
    target = writer.fileno()
    cleanup_error = error_factory()

    def fail_close(file_descriptor: int) -> None:
        if file_descriptor == target:
            raise cleanup_error
        REAL_CLOSE(file_descriptor)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(channel_module.os, "close", fail_close)
        with pytest.raises(error_factory) as captured:
            with writer:
                raise ValueError("ordinary body")
    assert captured.value is cleanup_error
    REAL_CLOSE(target)
    reader.close()


def test_active_context_process_control_wins_with_fixed_cleanup_note() -> None:
    reader, writer = open_startup_channel()
    target = writer.fileno()
    active = KeyboardInterrupt()

    def fail_close(file_descriptor: int) -> None:
        if file_descriptor == target:
            raise OSError("ambiguous")
        REAL_CLOSE(file_descriptor)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(channel_module.os, "close", fail_close)
        with pytest.raises(KeyboardInterrupt) as captured:
            with writer:
                raise active
    assert captured.value is active
    assert active.__notes__ == ["sidecar startup channel cleanup failed"]
    REAL_CLOSE(target)
    reader.close()


def test_reported_close_failure_is_not_retried_after_descriptor_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = open_startup_channel()
    target = writer.fileno()
    replacement: list[int] = []
    close_calls = 0

    def ambiguous_close(file_descriptor: int) -> None:
        nonlocal close_calls
        if file_descriptor == target:
            close_calls += 1
            REAL_CLOSE(file_descriptor)
            reused = os.open("/dev/null", os.O_RDONLY)
            assert reused == target
            replacement.append(reused)
            raise OSError("reported ambiguous close")
        REAL_CLOSE(file_descriptor)

    with monkeypatch.context() as patcher:
        patcher.setattr(channel_module.os, "close", ambiguous_close)
        with pytest.raises(StartupChannelError) as captured:
            writer.close()
        writer.close()
    _assert_private_error(
        captured.value,
        StartupChannelErrorCode.STARTUP_CHANNEL_CLEANUP_FAILED,
    )
    assert close_calls == 1
    assert REAL_FSTAT(replacement[0]).st_mode
    REAL_CLOSE(replacement[0])
    reader.close()


def test_production_channel_has_no_runtime_or_process_orchestration_imports() -> None:
    source_path = Path(channel_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {
            "subprocess",
            "threading",
            "sqlite3",
            "uvicorn",
            "state",
            "health",
            "listener",
            "owner_lock",
        }
    )
