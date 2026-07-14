from __future__ import annotations

import asyncio
import json
import os
import queue
import socket
import sqlite3
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from flowsight.sidecar import (
    MAX_PRIVATE_BODY_BYTES,
    SidecarState,
    create_sidecar_app,
)
from flowsight.sidecar import app as sidecar_app_module

TOKEN = "private-capability-token-value-0123456789"


class _ResponseHeaderRewriteMiddleware:
    def __init__(self, app: ASGIApp, *, replacement: object) -> None:
        self._app = app
        self._replacement = replacement

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def rewrite_headers(message: Message) -> None:
            if message.get("type") == "http.response.start":
                message = {**message, "headers": self._replacement}
            await send(message)

        await self._app(scope, receive, rewrite_headers)


def _state(tmp_path: Path) -> SidecarState:
    return SidecarState(
        project_id="project-0123456789abcdef",
        startup_id="startup-0123456789abcdef",
        pid=os.getpid(),
        port=4040,
        token=TOKEN,
        database_path=str(tmp_path / "not-created" / "events.sqlite3"),
        started_at_ns=123_456_789,
    )


def _base_headers(
    state: SidecarState,
    *,
    content_length: bytes | None = b"0",
    origin: str | None = None,
) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"host", state.authority.encode("ascii")),
        (b"authorization", f"Bearer {state.token}".encode()),
    ]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    return headers


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    receive_count: int

    def json_object(self) -> dict[str, object]:
        decoded = json.loads(self.body or b"{}")
        assert type(decoded) is dict
        return decoded


def _request(
    app: FastAPI,
    state: SidecarState,
    method: str,
    path: str,
    *,
    headers: object | None = None,
    body: bytes = b"",
    messages: tuple[object, ...] | None = None,
    forbid_receive: bool = False,
    after_messages: str = "error",
) -> _Response:
    if messages is not None and body:
        raise AssertionError("body and messages are mutually exclusive")
    request_headers = (
        _base_headers(state, content_length=str(len(body)).encode("ascii"))
        if headers is None
        else headers
    )
    request_messages = (
        ({"type": "http.request", "body": body, "more_body": False},)
        if messages is None
        else messages
    )

    async def invoke() -> _Response:
        sent: list[dict[str, object]] = []
        receive_count = 0

        async def receive() -> object:
            nonlocal receive_count
            if forbid_receive:
                raise AssertionError("request body was read before boundary rejection")
            receive_count += 1
            if receive_count <= len(request_messages):
                return request_messages[receive_count - 1]
            if after_messages == "disconnect":
                return {"type": "http.disconnect"}
            if after_messages == "block":
                await asyncio.Event().wait()
                raise AssertionError("unreachable blocked receive")
            raise AssertionError("request consumed beyond the supplied ASGI messages")

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": request_headers,
            "client": ("127.0.0.1", 12345),
            "server": (state.host, state.port),
            "root_path": "",
        }
        await app(scope, receive, send)  # type: ignore[arg-type]
        response_starts = [message for message in sent if message["type"] == "http.response.start"]
        assert len(response_starts) == 1
        assert sent[0] is response_starts[0]
        response_start = response_starts[0]
        response_headers = response_start.get("headers", [])
        assert type(response_headers) is list
        body_messages = [message for message in sent if message["type"] == "http.response.body"]
        assert body_messages
        assert body_messages[-1].get("more_body", False) is False
        body_parts = [message.get("body", b"") for message in body_messages]
        assert all(type(part) is bytes for part in body_parts)
        response_body = b"".join(body_parts)  # type: ignore[arg-type]
        assert type(response_body) is bytes
        status = response_start["status"]
        assert type(status) is int
        return _Response(
            status=status,
            headers=tuple(response_headers),  # type: ignore[arg-type]
            body=response_body,
            receive_count=receive_count,
        )

    async def bounded_invoke() -> _Response:
        return await asyncio.wait_for(invoke(), timeout=5.0)

    return asyncio.run(bounded_invoke())


def _fault_code(response: _Response) -> str:
    detail = response.json_object()["detail"]
    assert type(detail) is dict
    code = detail["code"]
    assert type(code) is str
    return code


def _assert_no_cors(response: _Response) -> None:
    assert all(not name.lower().startswith(b"access-control-") for name, _ in response.headers)


def _assert_no_private_values(
    response: _Response,
    state: SidecarState,
    *extra_values: str,
) -> None:
    wire_bytes = b"\n".join(
        [
            response.body,
            *(name + b":" + value for name, value in response.headers),
        ]
    )
    for value in (state.token, state.database_path, *extra_values):
        assert value.encode("utf-8") not in wire_bytes


def test_factory_is_exact_inert_docs_disabled_and_ships_only_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)

    def unexpected_runtime_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("the ASGI app factory must not start runtime infrastructure")

    with monkeypatch.context() as context:
        context.setattr(socket, "socket", unexpected_runtime_call)
        context.setattr(subprocess, "Popen", unexpected_runtime_call)
        context.setattr(threading, "Thread", unexpected_runtime_call)
        context.setattr(queue, "Queue", unexpected_runtime_call)
        context.setattr(sqlite3, "connect", unexpected_runtime_call)
        app = create_sidecar_app(state)

    assert isinstance(app, FastAPI)
    assert [route.path for route in app.routes] == ["/internal/v1/health"]
    assert TOKEN not in repr(app)
    assert not Path(state.database_path).parent.exists()
    assert not Path(state.database_path).exists()
    with pytest.raises(TypeError, match="exact SidecarState"):
        create_sidecar_app(object())  # type: ignore[arg-type]

    for path in ("/docs", "/redoc", "/openapi.json"):
        response = _request(
            app,
            state,
            "GET",
            path,
            headers=[(b"host", state.authority.encode("ascii"))],
        )
        assert response.status == 404
        _assert_no_cors(response)


def test_factory_lifespan_is_plain_fastapi_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []

    def unexpected_runtime_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("lifespan must not start runtime infrastructure")

    async def run_lifespan() -> list[dict[str, object]]:
        incoming = [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            if not incoming:
                raise AssertionError("lifespan consumed too many messages")
            return incoming.pop(0)

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        with monkeypatch.context() as context:
            context.setattr(socket, "socket", unexpected_runtime_call)
            context.setattr(subprocess, "Popen", unexpected_runtime_call)
            context.setattr(threading, "Thread", unexpected_runtime_call)
            context.setattr(queue, "Queue", unexpected_runtime_call)
            context.setattr(sqlite3, "connect", unexpected_runtime_call)
            await asyncio.wait_for(
                app(
                    {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                    receive,
                    send,
                ),
                timeout=5.0,
            )
        return sent

    sent = asyncio.run(run_lifespan())

    assert [message["type"] for message in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert not Path(state.database_path).parent.exists()
    assert not Path(state.database_path).exists()


@pytest.mark.parametrize("invalid_token", ["é" * 32, "token value " * 4, "token/value" * 4])
def test_factory_rejects_non_url_safe_bearer_tokens_without_echo(
    tmp_path: Path,
    invalid_token: str,
) -> None:
    state = replace(_state(tmp_path), token=invalid_token)

    with pytest.raises(ValueError, match="HTTP bearer-safe") as error:
        create_sidecar_app(state)

    assert invalid_token not in str(error.value)


def test_authenticated_health_has_an_exact_safe_schema(tmp_path: Path) -> None:
    state = replace(
        _state(tmp_path),
        project_id="😀" * 256,
        startup_id="🚀" * 128,
    )
    app = create_sidecar_app(state)

    response = _request(app, state, "GET", "/internal/v1/health")

    assert response.status == 200
    assert response.json_object() == {
        "status": "ok",
        "protocol_version": state.protocol_version,
        "state_schema_version": state.state_schema_version,
        "project_id": state.project_id,
        "startup_id": state.startup_id,
        "sidecar_pid": state.pid,
        "host": state.host,
        "port": state.port,
    }
    serialized = response.body.decode("utf-8")
    assert TOKEN not in serialized
    assert str(state.database_path) not in serialized
    for forbidden in (
        "producer",
        "lease",
        "writer",
        "storage",
        "sqlite",
        "database",
        "token",
    ):
        assert forbidden not in serialized.lower()
    assert len(response.body) <= 4096
    _assert_no_cors(response)
    _assert_no_private_values(response, state)


@pytest.mark.parametrize("case", ["missing", "wrong", "duplicate"])
def test_every_http_path_requires_exactly_one_expected_host_before_receive(
    tmp_path: Path,
    case: str,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    headers: list[tuple[bytes, bytes]] = []
    if case == "wrong":
        headers.append((b"host", b"attacker.invalid"))
    elif case == "duplicate":
        headers.extend(
            [
                (b"host", state.authority.encode("ascii")),
                (b"Host", state.authority.encode("ascii")),
            ]
        )

    response = _request(
        app,
        state,
        "GET",
        "/future-ui-asset.js",
        headers=headers,
        messages=(),
        forbid_receive=True,
    )

    assert response.status == 403
    assert _fault_code(response) == "HOST_REJECTED"
    _assert_no_cors(response)
    _assert_no_private_values(response, state, "attacker.invalid")


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("missing_host", 403, "HOST_REJECTED"),
        ("wrong_host", 403, "HOST_REJECTED"),
        ("duplicate_host", 403, "HOST_REJECTED"),
        ("missing_auth", 401, "AUTH_REJECTED"),
        ("wrong_auth", 401, "AUTH_REJECTED"),
        ("duplicate_auth", 401, "AUTH_REJECTED"),
        ("malformed_headers", 400, "HEADERS_INVALID"),
    ],
)
def test_private_auth_rejects_before_receive_without_echoing_secrets(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    case: str,
    expected_status: int,
    expected_code: str,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    headers: object = _base_headers(state)
    assert type(headers) is list
    if case == "missing_host":
        headers = [item for item in headers if item[0] != b"host"]
    elif case == "wrong_host":
        headers[0] = (b"host", b"localhost:4040")
    elif case == "duplicate_host":
        headers.append((b"Host", state.authority.encode("ascii")))
    elif case == "missing_auth":
        headers = [item for item in headers if item[0] != b"authorization"]
    elif case == "wrong_auth":
        headers[1] = (b"authorization", f"Bearer wrong-{TOKEN}".encode())
    elif case == "duplicate_auth":
        headers.append((b"Authorization", f"Bearer {TOKEN}".encode()))
    else:
        headers = [b"not-a-header-pair"]

    response = _request(
        app,
        state,
        "GET",
        "/internal/v1/health",
        headers=headers,
        messages=(),
        forbid_receive=True,
    )

    captured = capsys.readouterr()
    assert response.status == expected_status
    assert _fault_code(response) == expected_code
    assert TOKEN not in response.body.decode() + captured.out + captured.err + caplog.text
    assert str(state.database_path) not in response.body.decode()
    _assert_no_cors(response)
    _assert_no_private_values(response, state, "localhost:4040", f"wrong-{TOKEN}")


def test_header_names_are_case_insensitive_but_values_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    comparisons: list[tuple[bytes, bytes]] = []
    real_compare = sidecar_app_module.secrets.compare_digest

    def recording_compare(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(sidecar_app_module.secrets, "compare_digest", recording_compare)
    headers = [
        (b"Host", state.authority.encode("ascii")),
        (b"Authorization", f"Bearer {TOKEN}".encode()),
        (b"Content-Length", b"0"),
    ]

    response = _request(app, state, "GET", "/internal/v1/health", headers=headers)

    assert response.status == 200
    assert len(comparisons) == 2
    assert comparisons[0][0] == state.authority.encode("ascii")
    assert comparisons[1][0] == f"Bearer {TOKEN}".encode()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "CONNECT", "post"])
def test_every_non_safe_browser_method_requires_exact_origin(
    tmp_path: Path,
    method: str,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    missing = _request(
        app,
        state,
        method,
        "/api/v1/not-implemented",
        messages=(),
        forbid_receive=True,
    )
    wrong = _request(
        app,
        state,
        method,
        "/api/v1/not-implemented",
        headers=_base_headers(state, origin="http://attacker.invalid"),
        messages=(),
        forbid_receive=True,
    )
    duplicate_headers = _base_headers(state, origin=state.origin)
    duplicate_headers.append((b"Origin", state.origin.encode("ascii")))
    duplicate = _request(
        app,
        state,
        method,
        "/api/v1/not-implemented",
        headers=duplicate_headers,
        messages=(),
        forbid_receive=True,
    )
    accepted = _request(
        app,
        state,
        method,
        "/api/v1/not-implemented",
        headers=_base_headers(state, origin=state.origin),
    )

    for rejected in (missing, wrong, duplicate):
        assert rejected.status == 403
        assert _fault_code(rejected) == "ORIGIN_REJECTED"
        _assert_no_cors(rejected)
    assert accepted.status == 404
    _assert_no_cors(accepted)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_browser_methods_and_internal_writes_do_not_require_origin(
    tmp_path: Path,
    method: str,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    browser = _request(app, state, method, "/api/v1/not-implemented")
    internal = _request(app, state, "POST", "/internal/v1/not-implemented")

    assert browser.status == 404
    assert internal.status == 404
    _assert_no_cors(browser)
    _assert_no_cors(internal)


def test_exact_private_namespace_roots_are_not_a_slash_bypass(tmp_path: Path) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    host_only = [(b"host", state.authority.encode("ascii"))]

    internal = _request(
        app,
        state,
        "GET",
        "/internal/v1",
        headers=host_only,
        messages=(),
        forbid_receive=True,
    )
    browser_read = _request(
        app,
        state,
        "GET",
        "/api/v1",
        headers=host_only,
        messages=(),
        forbid_receive=True,
    )
    browser_write = _request(
        app,
        state,
        "POST",
        "/api/v1",
        messages=(),
        forbid_receive=True,
    )

    assert internal.status == 401
    assert _fault_code(internal) == "AUTH_REJECTED"
    assert browser_read.status == 401
    assert _fault_code(browser_read) == "AUTH_REJECTED"
    assert browser_write.status == 403
    assert _fault_code(browser_write) == "ORIGIN_REJECTED"


@pytest.mark.parametrize(
    ("path", "is_private"),
    [
        ("/internal/v1/", True),
        ("/internal/v1//future", True),
        ("/api/v1/", True),
        ("/api/v1//future", True),
        ("/internal/v10", False),
        ("/internal/v1evil", False),
        ("/api/v10", False),
        ("/api/v1evil", False),
    ],
)
def test_private_namespace_matching_is_segment_exact(
    tmp_path: Path,
    path: str,
    is_private: bool,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    host_only = [(b"host", state.authority.encode("ascii"))]

    response = _request(
        app,
        state,
        "GET",
        path,
        headers=host_only,
        messages=(),
        forbid_receive=is_private,
    )

    assert response.status == (401 if is_private else 404)
    if is_private:
        assert _fault_code(response) == "AUTH_REJECTED"


def test_boundary_rejections_happen_before_downstream_or_body_parser(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    parser_calls = 0
    downstream_calls = 0

    @app.middleware("http")
    async def count_downstream(request: object, call_next: object) -> object:
        nonlocal downstream_calls
        downstream_calls += 1
        return await call_next(request)  # type: ignore[operator]

    @app.post("/internal/v1/parse")
    async def parse_body(payload: dict[str, object]) -> dict[str, object]:
        nonlocal parser_calls
        parser_calls += 1
        return payload

    wrong_auth_headers = _base_headers(state, content_length=b"9")
    wrong_auth_headers[1] = (b"authorization", b"Bearer wrong-token")
    rejected_auth = _request(
        app,
        state,
        "POST",
        "/internal/v1/parse",
        headers=wrong_auth_headers,
        messages=(),
        forbid_receive=True,
    )
    rejected_size = _request(
        app,
        state,
        "POST",
        "/internal/v1/parse",
        headers=_base_headers(
            state,
            content_length=str(MAX_PRIVATE_BODY_BYTES + 1).encode("ascii"),
        ),
        messages=(),
        forbid_receive=True,
    )
    rejected_stream_overflow = _request(
        app,
        state,
        "POST",
        "/internal/v1/parse",
        headers=_base_headers(state, content_length=None),
        messages=(
            {
                "type": "http.request",
                "body": b"x" * (MAX_PRIVATE_BODY_BYTES + 1),
                "more_body": False,
            },
        ),
    )
    rejected_mismatch = _request(
        app,
        state,
        "POST",
        "/internal/v1/parse",
        headers=_base_headers(state, content_length=b"2"),
        messages=({"type": "http.request", "body": b"x", "more_body": False},),
    )
    rejected_disconnect = _request(
        app,
        state,
        "POST",
        "/internal/v1/parse",
        headers=_base_headers(state, content_length=b"1"),
        messages=({"type": "http.disconnect"},),
    )
    encoded = b'{"safe":"value"}'
    accepted_headers = _base_headers(state, content_length=str(len(encoded)).encode("ascii"))
    accepted_headers.append((b"content-type", b"application/json"))
    accepted = _request(
        app,
        state,
        "POST",
        "/internal/v1/parse",
        headers=accepted_headers,
        body=encoded,
    )

    assert rejected_auth.status == 401
    assert rejected_size.status == 413
    assert rejected_stream_overflow.status == 413
    assert _fault_code(rejected_stream_overflow) == "BODY_TOO_LARGE"
    assert rejected_mismatch.status == 400
    assert _fault_code(rejected_mismatch) == "BODY_LENGTH_MISMATCH"
    assert rejected_disconnect.status == 400
    assert _fault_code(rejected_disconnect) == "BODY_INCOMPLETE"
    assert downstream_calls == 1
    assert parser_calls == 1
    assert accepted.status == 200
    assert accepted.json_object() == {"safe": "value"}


@pytest.mark.parametrize(
    ("values", "expected_status", "expected_code"),
    [
        ((b"0", b"0"), 400, "CONTENT_LENGTH_INVALID"),
        ((b"",), 400, "CONTENT_LENGTH_INVALID"),
        ((b"-1",), 400, "CONTENT_LENGTH_INVALID"),
        ((b"+1",), 400, "CONTENT_LENGTH_INVALID"),
        ((b" 1",), 400, "CONTENT_LENGTH_INVALID"),
        ((b"1 ",), 400, "CONTENT_LENGTH_INVALID"),
        ((b"1x",), 400, "CONTENT_LENGTH_INVALID"),
        ((b"\xff",), 400, "CONTENT_LENGTH_INVALID"),
        ((b"9" * 1000,), 400, "CONTENT_LENGTH_INVALID"),
        ((str(MAX_PRIVATE_BODY_BYTES + 1).encode("ascii"),), 413, "BODY_TOO_LARGE"),
        ((b"0001048577",), 413, "BODY_TOO_LARGE"),
    ],
)
def test_content_length_grammar_and_pre_receive_limit(
    tmp_path: Path,
    values: tuple[bytes, ...],
    expected_status: int,
    expected_code: str,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    headers = _base_headers(state, content_length=None)
    headers.extend((b"content-length", value) for value in values)

    response = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=headers,
        messages=(),
        forbid_receive=True,
    )

    assert response.status == expected_status
    assert _fault_code(response) == expected_code
    _assert_no_cors(response)


@pytest.mark.parametrize("with_content_length", [False, True])
def test_transfer_encoding_is_always_rejected_before_receive(
    tmp_path: Path,
    with_content_length: bool,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    headers = _base_headers(
        state,
        content_length=b"0" if with_content_length else None,
    )
    headers.append((b"Transfer-Encoding", b"chunked"))

    response = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=headers,
        messages=(),
        forbid_receive=True,
    )

    assert response.status == 400
    assert _fault_code(response) == "REQUEST_FRAMING_INVALID"
    _assert_no_cors(response)


def test_content_length_must_match_received_bytes_exactly(tmp_path: Path) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    too_short = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=b"2"),
        body=b"x",
    )
    too_long = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=b"1"),
        body=b"xy",
    )
    leading_zero = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=b"0001"),
        body=b"x",
    )
    without_length = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=None),
        messages=(
            {"type": "http.request", "body": b"x", "more_body": True},
            {"type": "http.request", "body": b"y", "more_body": False},
        ),
    )

    assert too_short.status == 400
    assert _fault_code(too_short) == "BODY_LENGTH_MISMATCH"
    assert too_long.status == 400
    assert _fault_code(too_long) == "BODY_LENGTH_MISMATCH"
    assert leading_zero.status == 404
    assert without_length.status == 404


def test_streamed_body_limit_allows_exact_max_and_rejects_overflow(tmp_path: Path) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    exact_body = b"x" * MAX_PRIVATE_BODY_BYTES

    exact = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(
            state,
            content_length=str(MAX_PRIVATE_BODY_BYTES).encode("ascii"),
        ),
        body=exact_body,
    )
    overflow = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=None),
        messages=(
            {"type": "http.request", "body": exact_body, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ),
    )
    declared_overflow = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(
            state,
            content_length=str(MAX_PRIVATE_BODY_BYTES).encode("ascii"),
        ),
        messages=(
            {"type": "http.request", "body": exact_body, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ),
    )

    assert exact.status == 404
    assert overflow.status == 413
    assert _fault_code(overflow) == "BODY_TOO_LARGE"
    assert overflow.receive_count == 2
    assert declared_overflow.status == 413
    assert _fault_code(declared_overflow) == "BODY_TOO_LARGE"


def test_exact_max_json_replays_once_to_real_fastapi_parser(tmp_path: Path) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    handler_calls = 0

    @app.post("/internal/v1/max-json")
    async def parse_max_json(payload: dict[str, object]) -> dict[str, object]:
        nonlocal handler_calls
        handler_calls += 1
        value = payload["value"]
        assert type(value) is str
        return {"value_length": len(value)}

    prefix = b'{"value":"'
    suffix = b'"}'
    value_length = MAX_PRIVATE_BODY_BYTES - len(prefix) - len(suffix)
    encoded = prefix + b"x" * value_length + suffix
    assert len(encoded) == MAX_PRIVATE_BODY_BYTES
    headers = _base_headers(
        state,
        content_length=str(len(encoded)).encode("ascii"),
    )
    headers.append((b"content-type", b"application/json"))

    response = _request(
        app,
        state,
        "POST",
        "/internal/v1/max-json",
        headers=headers,
        body=encoded,
    )

    assert response.status == 200
    assert response.json_object() == {"value_length": value_length}
    assert handler_calls == 1


def test_replayed_body_delegates_connection_state_to_original_receive(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    @app.post("/internal/v1/connection-probe")
    async def connection_probe(request: Request) -> dict[str, object]:
        body = await request.body()
        return {
            "body": body.decode("ascii"),
            "disconnected": await request.is_disconnected(),
        }

    connected = _request(
        app,
        state,
        "POST",
        "/internal/v1/connection-probe",
        body=b"connected",
        after_messages="block",
    )
    disconnected = _request(
        app,
        state,
        "POST",
        "/internal/v1/connection-probe",
        headers=_base_headers(state, content_length=b"12"),
        messages=(
            {"type": "http.request", "body": b"disconnected", "more_body": False},
            {"type": "http.disconnect"},
        ),
    )

    assert connected.status == 200
    assert connected.json_object() == {"body": "connected", "disconnected": False}
    assert connected.receive_count == 2
    assert disconnected.status == 200
    assert disconnected.json_object() == {"body": "disconnected", "disconnected": True}
    assert disconnected.receive_count == 2


@pytest.mark.parametrize(
    ("messages", "expected_code"),
    [
        (({"type": "http.disconnect"},), "BODY_INCOMPLETE"),
        (
            (
                {"type": "http.request", "body": b"x", "more_body": True},
                {"type": "http.disconnect"},
            ),
            "BODY_INCOMPLETE",
        ),
        (({},), "BODY_INVALID"),
        (({"type": "websocket.receive", "body": b""},), "BODY_INVALID"),
        (({"type": "http.request", "body": "text"},), "BODY_INVALID"),
        (({"type": "http.request", "body": bytearray()},), "BODY_INVALID"),
        (({"type": "http.request", "body": None},), "BODY_INVALID"),
        (({"type": "http.request", "body": b"", "more_body": 1},), "BODY_INVALID"),
        (([],), "BODY_INVALID"),
    ],
)
def test_invalid_asgi_body_messages_fail_with_fixed_errors(
    tmp_path: Path,
    messages: tuple[object, ...],
    expected_code: str,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    response = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=None),
        messages=messages,
    )

    assert response.status == 400
    assert _fault_code(response) == expected_code
    assert TOKEN not in response.body.decode()


def test_missing_optional_body_fields_use_asgi_defaults(tmp_path: Path) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    response = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=None),
        messages=({"type": "http.request"},),
    )

    assert response.status == 404


def test_zero_byte_stream_is_bounded_by_message_count(tmp_path: Path) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)
    messages = tuple({"type": "http.request", "body": b"", "more_body": True} for _ in range(1024))

    response = _request(
        app,
        state,
        "POST",
        "/internal/v1/not-implemented",
        headers=_base_headers(state, content_length=None),
        messages=messages,
    )

    assert response.status == 400
    assert _fault_code(response) == "BODY_MESSAGE_LIMIT"
    assert response.receive_count == 1024


def test_fixed_faults_not_found_and_method_not_allowed_never_enable_cors(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    fault = _request(app, state, "GET", "/internal/v1/health", headers=[])
    not_found = _request(app, state, "GET", "/internal/v1/not-found")
    method_not_allowed = _request(app, state, "POST", "/internal/v1/health")
    preflight = _request(
        app,
        state,
        "OPTIONS",
        "/api/v1/not-found",
        headers=[
            (b"host", state.authority.encode("ascii")),
            (b"origin", b"http://attacker.invalid"),
            (b"access-control-request-method", b"POST"),
        ],
        messages=(),
        forbid_receive=True,
    )

    assert fault.status == 403
    assert not_found.status == 404
    assert method_not_allowed.status == 405
    assert preflight.status == 401
    for response in (fault, not_found, method_not_allowed, preflight):
        _assert_no_cors(response)
        _assert_no_private_values(response, state, "attacker.invalid")


def test_outer_boundary_strips_cors_headers_from_all_downstream_responses(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    app = create_sidecar_app(state)

    @app.get("/api/v1/test-only-cors")
    async def private_cors_response() -> JSONResponse:
        return JSONResponse(
            {"status": "private"},
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "x-test",
            },
        )

    @app.get("/test-only-ui-cors")
    async def ui_cors_response() -> JSONResponse:
        return JSONResponse(
            {"status": "ui"},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    private = _request(app, state, "GET", "/api/v1/test-only-cors")
    ui = _request(
        app,
        state,
        "GET",
        "/test-only-ui-cors",
        headers=[(b"host", state.authority.encode("ascii"))],
    )

    assert private.status == 200
    assert ui.status == 200
    _assert_no_cors(private)
    _assert_no_cors(ui)


def test_outer_boundary_handles_tuple_and_malformed_downstream_headers(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    tuple_app = create_sidecar_app(state)
    tuple_app.add_middleware(
        _ResponseHeaderRewriteMiddleware,
        replacement=(
            (b"Content-Type", b"application/json"),
            (b"aCcEsS-CoNtRoL-aLlOw-OrIgIn", b"*"),
        ),
    )

    @tuple_app.get("/api/v1/tuple-headers")
    async def tuple_headers() -> dict[str, object]:
        return {"status": "ok"}

    tuple_response = _request(tuple_app, state, "GET", "/api/v1/tuple-headers")

    assert tuple_response.status == 200
    _assert_no_cors(tuple_response)

    malformed_app = create_sidecar_app(state)
    malformed_app.add_middleware(
        _ResponseHeaderRewriteMiddleware,
        replacement=(b"not-a-header-pair",),
    )

    @malformed_app.get("/api/v1/malformed-headers")
    async def malformed_headers() -> dict[str, object]:
        return {"status": "must-not-escape"}

    malformed_response = _request(
        malformed_app,
        state,
        "GET",
        "/api/v1/malformed-headers",
    )

    assert malformed_response.status == 500
    assert _fault_code(malformed_response) == "RESPONSE_HEADERS_INVALID"
    assert b"must-not-escape" not in malformed_response.body
    _assert_no_cors(malformed_response)
