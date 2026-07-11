from __future__ import annotations

import http.client
import json
import math
import os
import socket
import threading
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest

from flowsight.sidecar import InvalidStateError, SidecarState, probe_sidecar_health
from flowsight.sidecar import health as health_module

TOKEN = "private-capability-token-value-0123456789"


def _state(tmp_path: Path, *, port: int = 4040) -> SidecarState:
    return SidecarState(
        project_id="project-0123456789abcdef",
        startup_id="startup-0123456789abcdef",
        pid=os.getpid(),
        port=port,
        token=TOKEN,
        database_path=str(tmp_path / "not-created" / "events.sqlite3"),
        started_at_ns=123_456_789,
    )


def _health_object(state: SidecarState) -> dict[str, object]:
    return {
        "status": "ok",
        "protocol_version": state.protocol_version,
        "state_schema_version": state.state_schema_version,
        "project_id": state.project_id,
        "startup_id": state.startup_id,
        "sidecar_pid": state.pid,
        "host": state.host,
        "port": state.port,
    }


def _encoded_health(state: SidecarState) -> bytes:
    return json.dumps(_health_object(state), separators=(",", ":")).encode()


def _response_bytes(
    body: bytes,
    *,
    status: bytes = b"HTTP/1.1 200 OK",
    headers: list[bytes] | None = None,
) -> bytes:
    response_headers = (
        [
            b"Content-Type: application/json",
            f"Content-Length: {len(body)}".encode("ascii"),
        ]
        if headers is None
        else headers
    )
    return b"\r\n".join([status, *response_headers, b"", body])


class _FakeSocket:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        failures: dict[str, BaseException] | None = None,
    ) -> None:
        self._chunks = [bytearray(chunk) for chunk in chunks]
        self.failures = {} if failures is None else failures
        self.timeouts: list[float] = []
        self.blocking: list[bool] = []
        self.recv_amounts: list[int] = []
        self.recv_count = 0

    def _fail(self, stage: str) -> None:
        error = self.failures.get(stage)
        if error is not None:
            raise error

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)
        self._fail("settimeout")

    def setblocking(self, flag: bool) -> None:
        self.blocking.append(flag)
        self._fail("setblocking")

    def recv(self, amount: int) -> bytes:
        self.recv_count += 1
        self.recv_amounts.append(amount)
        self._fail("recv")
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        result = bytes(chunk[:amount])
        del chunk[:amount]
        if not chunk:
            self._chunks.pop(0)
        return result


class _FakeConnection:
    def __init__(
        self,
        connection_socket: _FakeSocket | None,
        *,
        failures: dict[str, BaseException] | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.sock = connection_socket
        self.failures = {} if failures is None else failures
        self.close_error = close_error
        self.debuglevel = 1
        self.debuglevels: list[int] = []
        self.connect_count = 0
        self.requests: list[tuple[str, str, bool, bool]] = []
        self.headers: list[tuple[str, str]] = []
        self.endheaders_count = 0
        self.close_count = 0

    def _fail(self, stage: str) -> None:
        error = self.failures.get(stage)
        if error is not None:
            raise error

    def set_debuglevel(self, level: int) -> None:
        self.debuglevels.append(level)
        self.debuglevel = level
        self._fail("set_debuglevel")

    def connect(self) -> None:
        self.connect_count += 1
        self._fail("connect")

    def putrequest(
        self,
        method: str,
        target: str,
        *,
        skip_host: bool,
        skip_accept_encoding: bool,
    ) -> None:
        self.requests.append((method, target, skip_host, skip_accept_encoding))
        self._fail("putrequest")

    def putheader(self, name: str, value: str) -> None:
        self.headers.append((name, value))
        if self.debuglevel:
            print(f"{name}: {value}")
        self._fail(f"putheader:{name.lower()}")

    def endheaders(self) -> None:
        self.endheaders_count += 1
        self._fail("endheaders")

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class _Clock:
    def __init__(self, current: float = 100.0) -> None:
        self.current = current
        self.calls = 0

    def monotonic(self) -> float:
        self.calls += 1
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    state: SidecarState,
    *,
    chunks: list[bytes] | None = None,
    socket_failures: dict[str, BaseException] | None = None,
    connection_failures: dict[str, BaseException] | None = None,
    close_error: BaseException | None = None,
    constructor_error: BaseException | None = None,
    missing_socket: bool = False,
    select_error: BaseException | None = None,
    select_readable: bool = True,
    clock: _Clock | None = None,
) -> tuple[_FakeConnection | None, _FakeSocket, list[tuple[str, int, float]], list[float]]:
    raw_response = _response_bytes(_encoded_health(state))
    connection_socket = _FakeSocket(
        [raw_response] if chunks is None else chunks,
        failures=socket_failures,
    )
    connection = (
        None
        if constructor_error is not None
        else _FakeConnection(
            None if missing_socket else connection_socket,
            failures=connection_failures,
            close_error=close_error,
        )
    )
    constructor_calls: list[tuple[str, int, float]] = []
    select_timeouts: list[float] = []

    def factory(host: str, port: int, *, timeout: float) -> _FakeConnection:
        constructor_calls.append((host, port, timeout))
        if constructor_error is not None:
            raise constructor_error
        assert connection is not None
        return connection

    def fake_select(
        readers: list[object],
        writers: list[object],
        exceptional: list[object],
        timeout: float,
    ) -> tuple[list[object], list[object], list[object]]:
        assert writers == []
        assert exceptional == []
        assert readers == [connection_socket]
        select_timeouts.append(timeout)
        if select_error is not None:
            raise select_error
        return (readers if select_readable else []), [], []

    monkeypatch.setattr(health_module.http.client, "HTTPConnection", factory)
    monkeypatch.setattr(health_module.select, "select", fake_select)
    if clock is not None:
        monkeypatch.setattr(health_module.time, "monotonic", clock.monotonic)
    return connection, connection_socket, constructor_calls, select_timeouts


def test_probe_makes_one_exact_token_safe_request_with_debug_off_and_one_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    clock = _Clock()
    connection, connection_socket, constructor_calls, select_timeouts = _install_fake(
        monkeypatch,
        state,
        clock=clock,
    )
    assert connection is not None

    assert probe_sidecar_health(state) is True

    assert constructor_calls == [(state.host, state.port, 0.5)]
    assert connection.debuglevels == [0]
    assert connection.connect_count == 1
    assert connection.requests == [("GET", "/internal/v1/health", True, True)]
    assert connection.headers == [
        ("Host", state.authority),
        ("Authorization", f"Bearer {state.token}"),
        ("Content-Length", "0"),
    ]
    assert connection.endheaders_count == 1
    assert connection_socket.timeouts == [0.5]
    assert connection_socket.blocking == [False]
    assert select_timeouts == [0.5]
    assert connection.close_count == 1
    captured = capsys.readouterr()
    assert state.token not in captured.out
    assert state.token not in captured.err
    assert state.token not in repr(state)
    assert state.token not in repr(probe_sidecar_health)
    assert all(name != "Origin" for name, _value in connection.headers)
    assert state.token not in connection.requests[0][1]


class _DerivedState(SidecarState):
    pass


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
        math.inf,
        -math.inf,
        math.nan,
        health_module.MAX_HEALTH_PROBE_TIMEOUT_SECONDS + 0.001,
    ],
)
def test_invalid_timeout_fails_before_connection_with_fixed_private_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_timeout: object,
) -> None:
    state = _state(tmp_path)
    calls = 0

    def unexpected_connection(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid input opened a connection")

    monkeypatch.setattr(health_module.http.client, "HTTPConnection", unexpected_connection)
    with pytest.raises((TypeError, ValueError)) as captured:
        probe_sidecar_health(state, invalid_timeout)  # type: ignore[arg-type]
    assert calls == 0
    assert state.token not in str(captured.value)
    assert captured.value.__cause__ is None


def test_huge_exact_int_timeout_is_rejected_without_float_conversion_or_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    calls = 0

    def unexpected_connection(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal calls
        calls += 1
        raise AssertionError("huge timeout opened a connection")

    monkeypatch.setattr(health_module.http.client, "HTTPConnection", unexpected_connection)
    with pytest.raises(ValueError, match="at most 30 seconds"):
        probe_sidecar_health(state, pow(10, 10_000))
    assert calls == 0


def test_nonexact_state_and_unsafe_token_fail_before_connection_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    derived = _DerivedState(**state.to_wire())
    calls = 0

    def unexpected_connection(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid input opened a connection")

    monkeypatch.setattr(health_module.http.client, "HTTPConnection", unexpected_connection)
    with pytest.raises(TypeError, match="exact SidecarState"):
        probe_sidecar_health(derived)
    with pytest.raises(TypeError, match="exact SidecarState"):
        probe_sidecar_health(object())  # type: ignore[arg-type]
    for token in ("é" * 32, "token value " * 4, "token/value" * 4):
        unsafe = replace(state, token=token)
        with pytest.raises(ValueError, match="HTTP bearer-safe") as captured:
            probe_sidecar_health(unsafe)
        assert token not in str(captured.value)
    assert calls == 0


def test_forged_exact_state_is_revalidated_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    object.__setattr__(state, "host", "192.0.2.1")
    calls = 0

    def unexpected_connection(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal calls
        calls += 1
        raise AssertionError("forged state opened a connection")

    monkeypatch.setattr(health_module.http.client, "HTTPConnection", unexpected_connection)
    with pytest.raises(InvalidStateError) as captured:
        probe_sidecar_health(state)
    assert calls == 0
    assert state.token not in str(captured.value)


@pytest.mark.parametrize("timeout", [1, 1.0, health_module.MAX_HEALTH_PROBE_TIMEOUT_SECONDS])
def test_valid_builtin_timeout_is_forwarded_with_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    timeout: int | float,
) -> None:
    state = _state(tmp_path)
    clock = _Clock()
    connection, _socket, calls, _select_timeouts = _install_fake(
        monkeypatch,
        state,
        clock=clock,
    )
    assert connection is not None

    assert probe_sidecar_health(state, timeout) is True
    assert calls == [(state.host, state.port, float(timeout))]
    assert connection.connect_count == 1


@pytest.mark.parametrize("field", sorted(health_module._EXPECTED_FIELDS))
def test_missing_and_extra_fields_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    state = _state(tmp_path)
    value = _health_object(state)
    del value[field]
    body = json.dumps(value, separators=(",", ":")).encode()
    _install_fake(monkeypatch, state, chunks=[_response_bytes(body)])
    assert probe_sidecar_health(state) is False

    value = _health_object(state)
    value["extra"] = field
    body = json.dumps(value, separators=(",", ":")).encode()
    _install_fake(monkeypatch, state, chunks=[_response_bytes(body)])
    assert probe_sidecar_health(state) is False


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("status", 1),
        ("protocol_version", True),
        ("state_schema_version", True),
        ("project_id", 1),
        ("startup_id", 1),
        ("sidecar_pid", True),
        ("host", 1),
        ("port", True),
    ],
)
def test_each_wrong_builtin_field_type_fails_even_if_equal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    wrong_value: object,
) -> None:
    state = replace(_state(tmp_path), pid=1, port=1)
    value = _health_object(state)
    value[field] = wrong_value
    body = json.dumps(value, separators=(",", ":")).encode()
    _install_fake(monkeypatch, state, chunks=[_response_bytes(body)])

    assert probe_sidecar_health(state) is False


@pytest.mark.parametrize("field", sorted(health_module._EXPECTED_FIELDS))
def test_each_mismatched_startup_value_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    state = _state(tmp_path)
    value = _health_object(state)
    original = value[field]
    value[field] = original + 1 if type(original) is int else f"{original}-other"
    body = json.dumps(value, separators=(",", ":")).encode()
    _install_fake(monkeypatch, state, chunks=[_response_bytes(body)])

    assert probe_sidecar_health(state) is False


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not-json",
        b"\xff",
        b"[]",
        b"null",
        b'"ok"',
        b'{"status":"ok","status":"ok"}',
        b'{"status":NaN}',
    ],
)
def test_invalid_utf8_json_nonobject_and_duplicate_keys_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: bytes,
) -> None:
    state = _state(tmp_path)
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[_response_bytes(body)],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is False
    assert connection.close_count == 1


def test_exact_matching_json_at_4096_byte_limit_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    encoded = _encoded_health(state)
    body = encoded + b" " * (health_module.MAX_HEALTH_RESPONSE_BYTES - len(encoded))
    connection, connection_socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[_response_bytes(body)],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is True
    assert max(connection_socket.recv_amounts) <= 4096
    assert connection.close_count == 1


@pytest.mark.parametrize("status", [100, 199, 201, 204, 301, 302, 307, 401, 500])
def test_non_200_does_not_redirect_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
) -> None:
    state = _state(tmp_path)
    raw = _response_bytes(
        _encoded_health(state),
        status=f"HTTP/1.1 {status} Other".encode("ascii"),
    )
    connection, _socket, calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[raw],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is False
    assert len(calls) == 1
    assert connection.connect_count == 1
    assert len(connection.requests) == 1


@pytest.mark.parametrize(
    ("status_line", "headers"),
    [
        (b"", None),
        (b"HTTP/2 200 OK", None),
        (b"HTTP/1.1 OK", None),
        (b"HTTP/1.1 20 OK", None),
        (b"HTTP/1.1 099 No", None),
        (b"HTTP/1.1 600 No", None),
        (b"HTTP/1.1 200 OK", []),
        (b"HTTP/1.1 200 OK", [b"Content-Type: application/json"]),
        (
            b"HTTP/1.1 200 OK",
            [
                b"Content-Type: application/json",
                b"Content-Length: 1",
                b"Content-Length: 1",
            ],
        ),
        (
            b"HTTP/1.1 200 OK",
            [
                b"Content-Type: application/json",
                b"Content-Length: 1",
                b"Transfer-Encoding: chunked",
            ],
        ),
        (b"HTTP/1.1 200 OK", [b"Content-Length: 1"]),
        (
            b"HTTP/1.1 200 OK",
            [
                b"Content-Type: application/json",
                b"Content-Type: application/json",
                b"Content-Length: 1",
            ],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b"Content-Type: text/plain", b"Content-Length: 1"],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b"Content-Type: application/json; charset=utf-8", b"Content-Length: 1"],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b"Content-Type: application/json", b"Content-Length: +1"],
        ),
        (
            b"HTTP/1.1 200 OK",
            [
                b"Content-Type: application/json",
                "Content-Length: １２".encode(),
            ],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b"Content-Type: application/json", b"Content-Length: 00001"],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b"Content-Type: application/json", b"Content-Length: 4097"],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b" Content-Type: application/json", b"Content-Length: 1"],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b"Bad Header: value", b"Content-Length: 1"],
        ),
        (
            b"HTTP/1.1 200 OK",
            [b"Malformed", b"Content-Length: 1"],
        ),
    ],
)
def test_malformed_status_and_headers_fail_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_line: bytes,
    headers: list[bytes] | None,
) -> None:
    state = _state(tmp_path)
    raw = _response_bytes(b"x", status=status_line, headers=headers)
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[raw],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is False
    assert connection.close_count == 1


@pytest.mark.parametrize("location", ["reason", "ignored-header"])
@pytest.mark.parametrize("control", [b"\n", b"\r", b"\x00", b"\x7f"])
def test_control_bytes_fail_even_with_an_exact_matching_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    location: str,
    control: bytes,
) -> None:
    state = _state(tmp_path)
    body = _encoded_health(state)
    status = b"HTTP/1.1 200 OK" + (control if location == "reason" else b"")
    headers = [
        b"Content-Type: application/json",
        f"Content-Length: {len(body)}".encode("ascii"),
    ]
    if location == "ignored-header":
        headers.append(b"X-Ignored: safe" + control + b"value")
    raw = _response_bytes(body, status=status, headers=headers)
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[raw],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is False
    assert connection.close_count == 1


@pytest.mark.parametrize(
    ("declared_length", "body"),
    [(1, b""), (5, b"{}"), (1, b"{}")],
)
def test_truncated_or_incomplete_declared_body_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    declared_length: int,
    body: bytes,
) -> None:
    state = _state(tmp_path)
    raw = _response_bytes(
        body,
        headers=[
            b"Content-Type: application/json",
            f"Content-Length: {declared_length}".encode("ascii"),
        ],
    )
    connection, connection_socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[raw],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is False
    assert connection_socket.recv_count <= 2
    assert connection.close_count == 1


@pytest.mark.parametrize("coalesced", [False, True])
def test_bytes_after_declared_matching_body_are_ignored_without_reuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    coalesced: bool,
) -> None:
    state = _state(tmp_path)
    framed = _response_bytes(_encoded_health(state))
    extra = b"another-unrequested-response"
    chunks = [framed + extra] if coalesced else [framed, extra]
    connection, connection_socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=chunks,
    )
    assert connection is not None

    assert probe_sidecar_health(state) is True
    assert connection_socket.recv_count == 1
    assert connection.close_count == 1


def test_json_trailing_content_inside_declared_body_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    body = _encoded_health(state) + b" trailing"
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[_response_bytes(body)],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is False
    assert connection.close_count == 1


def test_response_packetized_one_byte_at_a_time_succeeds_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    raw = _response_bytes(_encoded_health(state))
    chunks = [raw[index : index + 1] for index in range(len(raw))]
    connection, connection_socket, _calls, timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=chunks,
    )
    assert connection is not None

    assert probe_sidecar_health(state, 1.0) is True
    assert connection_socket.recv_count == len(raw)
    assert len(timeouts) == len(raw)
    assert connection.close_count == 1


@pytest.mark.parametrize("over_limit", [False, True])
def test_response_header_byte_limit_has_an_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    over_limit: bool,
) -> None:
    state = _state(tmp_path)
    body = _encoded_health(state)
    fixed_lines = [
        b"HTTP/1.1 200 OK",
        b"Content-Type: application/json",
        f"Content-Length: {len(body)}".encode("ascii"),
        b"X-Pad: ",
    ]
    fixed_length = len(b"\r\n".join(fixed_lines))
    target = health_module._MAX_RESPONSE_HEADER_BYTES + int(over_limit)
    padding = b"a" * (target - fixed_length)
    head = b"\r\n".join([*fixed_lines[:-1], fixed_lines[-1] + padding])
    assert len(head) == target
    raw = head + b"\r\n\r\n" + body
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[raw],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is (not over_limit)
    assert connection.close_count == 1


def test_exact_header_limit_accepts_a_split_crlf_terminator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    body = _encoded_health(state)
    fixed_lines = [
        b"HTTP/1.1 200 OK",
        b"Content-Type: application/json",
        f"Content-Length: {len(body)}".encode("ascii"),
        b"X-Pad: ",
    ]
    fixed_length = len(b"\r\n".join(fixed_lines))
    padding = b"a" * (health_module._MAX_RESPONSE_HEADER_BYTES - fixed_length)
    head = b"\r\n".join([*fixed_lines[:-1], fixed_lines[-1] + padding])
    assert len(head) == health_module._MAX_RESPONSE_HEADER_BYTES
    connection, connection_socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[head + b"\r", b"\n", b"\r", b"\n" + body],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is True
    assert connection_socket.recv_count >= 4
    assert connection.close_count == 1


@pytest.mark.parametrize("over_limit", [False, True])
def test_response_header_count_limit_has_an_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    over_limit: bool,
) -> None:
    state = _state(tmp_path)
    body = _encoded_health(state)
    extra_count = health_module._MAX_RESPONSE_HEADERS - 2 + int(over_limit)
    headers = [
        b"Content-Type: application/json",
        f"Content-Length: {len(body)}".encode("ascii"),
        *(f"X-{index}: v".encode("ascii") for index in range(extra_count)),
    ]
    assert len(headers) == health_module._MAX_RESPONSE_HEADERS + int(over_limit)
    raw = _response_bytes(body, headers=headers)
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[raw],
    )
    assert connection is not None

    assert probe_sidecar_health(state) is (not over_limit)
    assert connection.close_count == 1


@pytest.mark.parametrize(
    "stage",
    [
        "constructor",
        "set_debuglevel",
        "connect",
        "missing_socket",
        "settimeout",
        "putrequest",
        "putheader:host",
        "putheader:authorization",
        "putheader:content-length",
        "endheaders",
        "setblocking",
        "select",
        "recv",
        "disconnect",
        "framing",
        "decode",
        "match",
    ],
)
def test_ordinary_failures_are_one_attempt_fixed_false_and_cleanup_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    stage: str,
) -> None:
    state = _state(tmp_path)
    error = OSError(f"private {stage} {TOKEN}")
    connection_stage = (
        stage
        if stage
        in {
            "set_debuglevel",
            "connect",
            "putrequest",
            "putheader:host",
            "putheader:authorization",
            "putheader:content-length",
            "endheaders",
        }
        else None
    )
    socket_stage = stage if stage in {"settimeout", "setblocking", "recv"} else None
    chunks = [b""] if stage == "disconnect" else None
    connection, _socket, calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=chunks,
        socket_failures={} if socket_stage is None else {socket_stage: error},
        connection_failures={} if connection_stage is None else {connection_stage: error},
        constructor_error=error if stage == "constructor" else None,
        missing_socket=stage == "missing_socket",
        select_error=error if stage == "select" else None,
    )
    if stage == "framing":
        monkeypatch.setattr(
            health_module, "_parse_response_head", lambda _value: (_ for _ in ()).throw(error)
        )
    elif stage == "decode":
        monkeypatch.setattr(
            health_module, "_decode_response", lambda _value: (_ for _ in ()).throw(error)
        )
    elif stage == "match":
        monkeypatch.setattr(
            health_module,
            "_matches_exact_startup",
            lambda _value, _state: (_ for _ in ()).throw(error),
        )

    assert probe_sidecar_health(state) is False
    assert len(calls) == 1
    if connection is not None:
        assert connection.close_count == 1
        assert connection.connect_count <= 1
        assert len(connection.requests) <= 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err + caplog.text
    assert state.token not in combined
    assert "private" not in combined


@pytest.mark.parametrize(
    "stage",
    [
        "constructor",
        "set_debuglevel",
        "connect",
        "settimeout",
        "putrequest",
        "putheader:host",
        "putheader:authorization",
        "putheader:content-length",
        "endheaders",
        "setblocking",
        "select",
        "recv",
        "framing",
        "decode",
        "match",
    ],
)
@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_process_control_propagates_at_every_stage_after_one_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    error_factory: type[BaseException],
) -> None:
    state = _state(tmp_path)
    error = error_factory()
    connection_stage = (
        stage
        if stage
        in {
            "set_debuglevel",
            "connect",
            "putrequest",
            "putheader:host",
            "putheader:authorization",
            "putheader:content-length",
            "endheaders",
        }
        else None
    )
    socket_stage = stage if stage in {"settimeout", "setblocking", "recv"} else None
    connection, _socket, calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        socket_failures={} if socket_stage is None else {socket_stage: error},
        connection_failures={} if connection_stage is None else {connection_stage: error},
        constructor_error=error if stage == "constructor" else None,
        select_error=error if stage == "select" else None,
    )
    if stage == "framing":
        monkeypatch.setattr(
            health_module, "_parse_response_head", lambda _value: (_ for _ in ()).throw(error)
        )
    elif stage == "decode":
        monkeypatch.setattr(
            health_module, "_decode_response", lambda _value: (_ for _ in ()).throw(error)
        )
    elif stage == "match":
        monkeypatch.setattr(
            health_module,
            "_matches_exact_startup",
            lambda _value, _state: (_ for _ in ()).throw(error),
        )

    with pytest.raises(error_factory) as captured:
        probe_sidecar_health(state)
    assert captured.value is error
    assert len(calls) == 1
    if connection is not None:
        assert connection.close_count == 1


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_active_process_control_wins_over_cleanup_and_gets_only_fixed_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_factory: type[BaseException],
) -> None:
    state = _state(tmp_path)
    error = error_factory()
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        socket_failures={"recv": error},
        close_error=OSError(f"private cleanup {TOKEN}"),
    )
    assert connection is not None

    with pytest.raises(error_factory) as captured:
        probe_sidecar_health(state)
    assert captured.value is error
    assert captured.value.__notes__ == ["sidecar health probe cleanup failed"]
    assert state.token not in str(captured.value)
    assert state.token not in repr(captured.value)
    assert connection.close_count == 1


@pytest.mark.parametrize("error_factory", [KeyboardInterrupt, SystemExit])
def test_cleanup_process_control_propagates_once_without_active_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_factory: type[BaseException],
) -> None:
    state = _state(tmp_path)
    error = error_factory()
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        close_error=error,
    )
    assert connection is not None

    with pytest.raises(error_factory) as captured:
        probe_sidecar_health(state)
    assert captured.value is error
    assert connection.close_count == 1


def test_ordinary_cleanup_failure_returns_false_and_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        close_error=OSError(f"private cleanup {TOKEN}"),
    )
    assert connection is not None

    assert probe_sidecar_health(state) is False
    assert connection.close_count == 1


def test_total_deadline_stops_a_readable_slow_drip_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    clock = _Clock(0.0)
    raw = _response_bytes(_encoded_health(state))
    connection, connection_socket, _calls, select_timeouts = _install_fake(
        monkeypatch,
        state,
        chunks=[raw[index : index + 1] for index in range(len(raw))],
        clock=clock,
    )
    assert connection is not None

    real_select = health_module.select.select

    def slow_select(
        readers: list[object],
        writers: list[object],
        exceptional: list[object],
        timeout: float,
    ) -> tuple[list[object], list[object], list[object]]:
        clock.advance(0.4)
        return real_select(readers, writers, exceptional, timeout)

    monkeypatch.setattr(health_module.select, "select", slow_select)

    assert probe_sidecar_health(state, 1.0) is False
    assert connection_socket.recv_count == 3
    assert select_timeouts == pytest.approx([1.0, 0.6, 0.2])
    assert clock.current == pytest.approx(1.2)
    assert connection.close_count == 1


def test_select_timeout_without_readability_fails_closed_without_recv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    connection, connection_socket, calls, select_timeouts = _install_fake(
        monkeypatch,
        state,
        select_readable=False,
    )
    assert connection is not None

    assert probe_sidecar_health(state, 0.25) is False
    assert len(calls) == 1
    assert len(select_timeouts) == 1
    assert 0.0 < select_timeouts[0] <= 0.25
    assert connection_socket.recv_count == 0
    assert connection.close_count == 1


def test_deadline_is_recomputed_after_connect_before_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    clock = _Clock(0.0)
    connection, connection_socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        clock=clock,
    )
    assert connection is not None
    original_connect = connection.connect

    def advancing_connect() -> None:
        original_connect()
        clock.advance(0.25)

    monkeypatch.setattr(connection, "connect", advancing_connect)

    assert probe_sidecar_health(state, 1.0) is True
    assert connection_socket.timeouts == pytest.approx([0.75])
    assert connection.close_count == 1


def test_result_fails_closed_if_exact_matching_finishes_after_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    clock = _Clock(0.0)
    connection, _socket, _calls, _timeouts = _install_fake(
        monkeypatch,
        state,
        clock=clock,
    )
    assert connection is not None

    def late_match(_value: object, _state: SidecarState) -> bool:
        clock.advance(1.01)
        return True

    monkeypatch.setattr(health_module, "_matches_exact_startup", late_match)

    assert probe_sidecar_health(state, 1.0) is False
    assert connection.close_count == 1


def test_one_real_bounded_loopback_request_proves_exact_startup(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5.0)
    port = listener.getsockname()[1]
    assert type(port) is int
    state = _state(tmp_path, port=port)
    response_body = _encoded_health(state)
    captured_requests: list[bytes] = []
    server_errors: list[BaseException] = []
    finished = threading.Event()

    def serve_once() -> None:
        client: socket.socket | None = None
        try:
            client, _address = listener.accept()
            client.settimeout(5.0)
            request = bytearray()
            while b"\r\n\r\n" not in request:
                chunk = client.recv(1024)
                if not chunk or len(request) + len(chunk) > 4096:
                    raise AssertionError("request framing was incomplete or oversized")
                request.extend(chunk)
            captured_requests.append(bytes(request))
            client.sendall(_response_bytes(response_body) + b"ignored-overrun")
        except BaseException as error:
            server_errors.append(error)
        finally:
            if client is not None:
                client.close()
            finished.set()

    thread = threading.Thread(target=serve_once, name="health-test-server", daemon=True)
    thread.start()
    old_debuglevel = http.client.HTTPConnection.debuglevel
    http.client.HTTPConnection.debuglevel = 1
    try:
        assert probe_sidecar_health(state, 1.0) is True
        assert finished.wait(5.0)
    finally:
        http.client.HTTPConnection.debuglevel = old_debuglevel
        listener.close()
        thread.join(5.0)

    assert not thread.is_alive()
    assert server_errors == []
    assert captured_requests == [
        (
            f"GET /internal/v1/health HTTP/1.1\r\n"
            f"Host: {state.authority}\r\n"
            f"Authorization: Bearer {state.token}\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        ).encode("ascii")
    ]
    captured = capsys.readouterr()
    assert state.token not in captured.out
    assert state.token not in captured.err
    assert not Path(state.database_path).exists()
