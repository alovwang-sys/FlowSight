"""Executable evidence for the isolated TRIAL-005 tracepoint backend."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import FrameType, FunctionType

import pytest

import spikes.tracepoint_backend.backend as backend_module
import spikes.tracepoint_backend.benchmark as benchmark_module
from flowsight.security import safe_summary as benchmark_safe_summary
from spikes.tracepoint_backend import (
    MonitoringTracepointBackend,
    MonitoringUnavailableError,
    TracepointSnapshot,
    TracepointSpec,
    UnsupportedTracepointError,
)
from spikes.tracepoint_backend.benchmark import (
    HIT_ITERATIONS,
    PAIRED_REPEATS,
    SCHEMA_VERSION,
    WARMUP_REPEATS,
    run_benchmark,
)
from spikes.tracepoint_backend.settrace_probe import run_naive_settrace_overlap_probe

_EXPECTED_WORKLOAD_DIGEST = (
    "sha256:3bcbcc7d7f6ae1b14ef0672994e00f45ad42b2c73827e451a98fd4414380ec9d"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _SnapshotCollector:
    def __init__(self, *, accept: bool = True) -> None:
        self._lock = threading.Lock()
        self._snapshots: list[TracepointSnapshot] = []
        self._accept = accept

    def __call__(self, snapshot: TracepointSnapshot) -> bool:
        with self._lock:
            self._snapshots.append(snapshot)
            return self._accept

    def snapshot(self) -> tuple[TracepointSnapshot, ...]:
        with self._lock:
            return tuple(self._snapshots)


class _Bomb:
    repr_called = False
    iter_called = False
    getattr_called = False

    def __repr__(self) -> str:
        type(self).repr_called = True
        raise AssertionError("repr must not run")

    def __iter__(self):
        type(self).iter_called = True
        raise AssertionError("iter must not run")

    def __getattr__(self, _name: str) -> object:
        type(self).getattr_called = True
        raise AssertionError("getattr must not run")


def _before_line_target(
    initial: int,
    password: str,
    watched: object,
    unwatched: object,
) -> int:
    # TP_COMMENT_ONLY
    value = initial
    result = value + 1  # TP_BEFORE_ASSIGN
    assert unwatched is not None
    return result


def _inner_target(value: int) -> int:
    doubled = value * 2  # TP_INNER
    return doubled


def _outer_target(value: int) -> int:
    outer_value = value + 1  # TP_OUTER
    return _inner_target(outer_value)


def _recursive_target(depth: int) -> int:
    current = depth  # TP_RECURSIVE
    if depth:
        return _recursive_target(depth - 1) + current
    return current


async def _async_target(
    label: str,
    entered: asyncio.Event,
    release: asyncio.Event,
) -> str:
    before = f"{label}-before"
    entered.set()
    await release.wait()
    result = f"{before}:after"  # TP_ASYNC
    return result


def _thread_target(label: str) -> str:
    before = f"{label}-before"
    result = f"{before}:after"  # TP_THREAD
    return result


def _loop_target(count: int) -> int:
    total = 0
    for index in range(count):
        total += index  # TP_LOOP
    return total


def _make_closure_target(prefix: str):
    def closure_target(value: int) -> str:
        result = f"{prefix}:{value}"  # TP_CLOSURE
        return result

    return closure_target


_closure_target = _make_closure_target("closed")


def _cell_target(value: int) -> int:
    shared = value

    def nested() -> int:
        return shared

    result = shared + 1  # TP_CELL
    return nested() + result


def _exception_target(value: int) -> None:
    observed = value  # TP_EXCEPTION
    raise ValueError(f"raw-exception-{observed}")


def _generator_target():
    yield 1


async def _async_generator_target():
    yield 1


def _one_line_target():
    return 1  # noqa: E704


class _Methods:
    def instance_target(self, value: int) -> int:
        result = value + 1  # TP_INSTANCE
        return result

    @classmethod
    def class_target(cls, value: int) -> int:
        result = value + 2  # TP_CLASS
        return result

    @staticmethod
    def static_target(value: int) -> int:
        result = value + 3  # TP_STATIC
        return result


def _line(function: object, marker: str) -> int:
    resolved = function.__func__ if inspect.ismethod(function) else function
    assert isinstance(resolved, FunctionType)
    source, first_line = inspect.getsourcelines(resolved)
    matches = [
        first_line + offset for offset, source_line in enumerate(source) if marker in source_line
    ]
    assert len(matches) == 1
    return matches[0]


def _spec(
    tracepoint_id: str,
    function: object,
    marker: str,
    variable_names: tuple[str, ...],
    *,
    hit_limit: int = 128,
) -> TracepointSpec:
    return TracepointSpec.from_function(
        tracepoint_id,
        function,
        _line(function, marker),
        variable_names,
        hit_limit=hit_limit,
    )


def _probe_python(source: str, python: str = sys.executable) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    completed = subprocess.run(
        [python, "-c", source],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30.0,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_spec_rejects_non_executable_and_expression_shapes() -> None:
    valid_line = _line(_before_line_target, "TP_BEFORE_ASSIGN")
    spec = TracepointSpec.from_function(
        "valid",
        _before_line_target,
        valid_line,
        ("value", "result"),
    )
    assert spec.function_kind == "sync"

    invalid_lines = (
        _before_line_target.__code__.co_firstlineno,
        _line(_before_line_target, "TP_COMMENT_ONLY"),
        1,
    )
    for line_no in invalid_lines:
        with pytest.raises(UnsupportedTracepointError, match="executable"):
            TracepointSpec.from_function("invalid-line", _before_line_target, line_no, ("value",))

    for names in (("value + 1",), ("watched.attr",), ("value[0]",), ("unknown",)):
        with pytest.raises(UnsupportedTracepointError):
            TracepointSpec.from_function("invalid-name", _before_line_target, valid_line, names)
    with pytest.raises(UnsupportedTracepointError, match="unique"):
        TracepointSpec.from_function(
            "duplicate-name", _before_line_target, valid_line, ("value", "value")
        )
    with pytest.raises(UnsupportedTracepointError, match="generator"):
        TracepointSpec.from_function(
            "generator", _generator_target, _generator_target.__code__.co_firstlineno + 1, ()
        )
    with pytest.raises(UnsupportedTracepointError, match="generator"):
        TracepointSpec.from_function(
            "async-generator",
            _async_generator_target,
            _async_generator_target.__code__.co_firstlineno + 1,
            (),
        )
    with pytest.raises(UnsupportedTracepointError):
        TracepointSpec.from_function("lambda", lambda: 1, valid_line, ("value",))
    with pytest.raises(UnsupportedTracepointError, match="executable"):
        TracepointSpec.from_function(
            "one-line",
            _one_line_target,
            _one_line_target.__code__.co_firstlineno,
            (),
        )

    with pytest.raises(UnsupportedTracepointError, match="generator"):
        TracepointSpec(
            tracepoint_id="direct-generator",
            code=_generator_target.__code__,
            line_no=_generator_target.__code__.co_firstlineno + 1,
            variable_names=("value",),
            function_name=_generator_target.__qualname__,
            function_kind="sync",
        )
    for overrides in (
        {"variable_names": ("value + 1",)},
        {"hit_limit": 999_999},
        {"function_name": "forged.function"},
    ):
        fields = {
            "tracepoint_id": spec.tracepoint_id,
            "code": spec.code,
            "line_no": spec.line_no,
            "variable_names": spec.variable_names,
            "function_name": spec.function_name,
            "function_kind": spec.function_kind,
            "hit_limit": spec.hit_limit,
        }
        fields.update(overrides)
        with pytest.raises(UnsupportedTracepointError):
            TracepointSpec(**fields)

    non_function_code = spec.code.replace(
        co_flags=spec.code.co_flags & ~(inspect.CO_OPTIMIZED | inspect.CO_NEWLOCALS)
    )
    with pytest.raises(UnsupportedTracepointError, match="function code"):
        TracepointSpec(
            tracepoint_id="direct-class-body-shape",
            code=non_function_code,
            line_no=spec.line_no,
            variable_names=spec.variable_names,
            function_name=non_function_code.co_qualname,
            function_kind="sync",
            hit_limit=spec.hit_limit,
        )


def test_before_line_named_only_safe_summary_retains_no_runtime_values() -> None:
    _Bomb.repr_called = False
    _Bomb.iter_called = False
    _Bomb.getattr_called = False
    watched = _Bomb()
    unwatched = _Bomb()
    watched_ref = weakref.ref(watched)
    unwatched_ref = weakref.ref(unwatched)
    collector = _SnapshotCollector()
    spec = _spec(
        "before",
        _before_line_target,
        "TP_BEFORE_ASSIGN",
        ("value", "result", "password", "watched"),
    )
    backend = MonitoringTracepointBackend((spec,), collector)
    backend.start()
    try:
        with backend.request_scope(
            "request-before",
            otel_trace_id="trace-before",
            span_id="span-before",
        ):
            assert _before_line_target(4, "raw-password-sentinel", watched, unwatched) == 5
    finally:
        assert backend.stop()

    [snapshot] = collector.snapshot()
    payload = json.loads(snapshot.payload_json)
    variables = payload["data"]["snapshot"]
    assert variables["value"] == 4
    assert variables["password"] == "<REDACTED>"
    assert variables["watched"] == {
        "$type": f"{_Bomb.__module__}.{_Bomb.__qualname__}",
        "$uninspected": True,
    }
    assert "result" not in variables
    assert snapshot.captured_names == ("value", "password", "watched")
    assert snapshot.missing_names == ("result",)
    assert snapshot.request_trace_id == "request-before"
    assert snapshot.otel_trace_id == "trace-before"
    assert snapshot.span_id == "span-before"
    assert "raw-password-sentinel" not in snapshot.payload_json
    assert "unwatched" not in snapshot.payload_json
    assert not _Bomb.repr_called
    assert not _Bomb.iter_called
    assert not _Bomb.getattr_called

    del watched
    del unwatched
    gc.collect()
    assert watched_ref() is None
    assert unwatched_ref() is None


def test_sync_nested_and_recursive_targets_keep_exact_request_ownership() -> None:
    collector = _SnapshotCollector()
    specs = (
        _spec("outer", _outer_target, "TP_OUTER", ("value", "outer_value")),
        _spec("inner", _inner_target, "TP_INNER", ("value", "doubled")),
        _spec("recursive", _recursive_target, "TP_RECURSIVE", ("depth", "current")),
    )
    backend = MonitoringTracepointBackend(specs, collector)
    backend.start()
    try:
        with backend.request_scope("request-nested"):
            assert _outer_target(2) == 6
            assert _recursive_target(3) == 6
        _outer_target(10)
    finally:
        assert backend.stop()

    snapshots = collector.snapshot()
    assert [snapshot.tracepoint_id for snapshot in snapshots] == [
        "outer",
        "inner",
        "recursive",
        "recursive",
        "recursive",
        "recursive",
    ]
    assert {snapshot.request_trace_id for snapshot in snapshots} == {"request-nested"}
    health = backend.health_snapshot()
    assert health.ignored_without_context_count >= 2
    assert health.frame_mismatch_count == 0


@pytest.mark.anyio
async def test_async_requests_interleave_without_cross_capture() -> None:
    collector = _SnapshotCollector()
    spec = _spec(
        "async",
        _async_target,
        "TP_ASYNC",
        ("label", "before", "result"),
    )
    backend = MonitoringTracepointBackend((spec,), collector)
    backend.start()
    entered_a = asyncio.Event()
    entered_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    async def invoke(label: str, entered: asyncio.Event, release: asyncio.Event) -> str:
        with backend.request_scope(f"request-{label}"):
            return await _async_target(label, entered, release)

    try:
        task_a = asyncio.create_task(invoke("A", entered_a, release_a))
        task_b = asyncio.create_task(invoke("B", entered_b, release_b))
        await entered_a.wait()
        await entered_b.wait()
        release_b.set()
        release_a.set()
        assert await asyncio.gather(task_a, task_b) == ["A-before:after", "B-before:after"]
    finally:
        assert backend.stop()

    snapshots = collector.snapshot()
    assert len(snapshots) == 2
    by_request = {snapshot.request_trace_id: snapshot for snapshot in snapshots}
    assert set(by_request) == {"request-A", "request-B"}
    for label in ("A", "B"):
        payload = json.loads(by_request[f"request-{label}"].payload_json)
        assert payload["data"]["snapshot"] == {
            "before": f"{label}-before",
            "label": label,
        }
        assert by_request[f"request-{label}"].missing_names == ("result",)


@pytest.mark.anyio
async def test_cancellation_and_inherited_late_context_do_not_leak() -> None:
    collector = _SnapshotCollector()
    spec = _spec("async", _async_target, "TP_ASYNC", ("label", "before"))
    backend = MonitoringTracepointBackend((spec,), collector)
    backend.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    try:
        with backend.request_scope("request-cancel"):
            cancelled = asyncio.create_task(_async_target("cancel", entered, release))
            await entered.wait()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled

        late_entered = asyncio.Event()
        late_release = asyncio.Event()
        with backend.request_scope("request-late"):
            late = asyncio.create_task(_async_target("late", late_entered, late_release))
            await late_entered.wait()
        late_release.set()
        assert await late == "late-before:after"

        valid_entered = asyncio.Event()
        valid_release = asyncio.Event()
        with backend.request_scope("request-valid"):
            valid = asyncio.create_task(_async_target("valid", valid_entered, valid_release))
            await valid_entered.wait()
            valid_release.set()
            assert await valid == "valid-before:after"
    finally:
        release.set()
        assert backend.stop()

    [snapshot] = collector.snapshot()
    assert snapshot.request_trace_id == "request-valid"
    assert backend.health_snapshot().inactive_context_drop_count >= 1


def test_thread_pool_requires_explicit_context_propagation() -> None:
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label", "before"))
    backend = MonitoringTracepointBackend((spec,), collector)
    backend.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            with backend.request_scope("request-thread"):
                copied = contextvars.copy_context()
                assert executor.submit(copied.run, _thread_target, "copied").result() == (
                    "copied-before:after"
                )
            assert executor.submit(_thread_target, "raw").result() == "raw-before:after"
    finally:
        assert backend.stop()

    [snapshot] = collector.snapshot()
    assert snapshot.request_trace_id == "request-thread"
    assert json.loads(snapshot.payload_json)["data"]["snapshot"]["label"] == "copied"
    assert backend.health_snapshot().ignored_without_context_count >= 1


def test_late_copied_thread_context_is_invalid_after_request_exit() -> None:
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    release = threading.Event()
    entered = threading.Event()

    def delayed() -> str:
        entered.set()
        assert release.wait(2.0)
        return _thread_target("late-thread")

    backend.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with backend.request_scope("request-late-thread"):
                copied = contextvars.copy_context()
                future = executor.submit(copied.run, delayed)
                assert entered.wait(1.0)
            release.set()
            assert future.result(timeout=2.0) == "late-thread-before:after"
    finally:
        release.set()
        assert backend.stop()

    assert collector.snapshot() == ()
    assert backend.health_snapshot().inactive_context_drop_count >= 1


def test_existing_sys_settrace_is_preserved_and_receives_target_events() -> None:
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    events: list[str] = []

    def existing(frame: FrameType, event: str, _argument: object):
        if frame.f_code is _thread_target.__code__:
            events.append(event)
        return existing

    previous = sys.gettrace()
    sys.settrace(existing)
    try:
        backend.start()
        assert sys.gettrace() is existing
        with backend.request_scope("request-existing-tracer"):
            assert _thread_target("traced") == "traced-before:after"
        assert sys.gettrace() is existing
        assert backend.stop()
        assert sys.gettrace() is existing
    finally:
        backend.stop()
        sys.settrace(previous)

    assert {"call", "line", "return"} <= set(events)
    assert len(collector.snapshot()) == 1


def test_monitoring_tool_id_conflicts_are_rejected_without_stealing() -> None:
    claimed: list[int] = []
    for tool_id in (3, 4):
        if sys.monitoring.get_tool(tool_id) is not None:
            pytest.skip(f"monitoring tool ID {tool_id} is already occupied by the test runner")
        sys.monitoring.use_tool_id(tool_id, f"trial005-conflict-{tool_id}")
        claimed.append(tool_id)
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    try:
        with pytest.raises(MonitoringUnavailableError):
            backend.start()
        assert sys.monitoring.get_tool(3) == "trial005-conflict-3"
        assert sys.monitoring.get_tool(4) == "trial005-conflict-4"
        assert backend.health_snapshot().active is False
    finally:
        for tool_id in claimed:
            sys.monitoring.set_events(tool_id, 0)
            sys.monitoring.free_tool_id(tool_id)


def test_free_threaded_runtime_is_rejected_before_claiming_a_tool_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: False, raising=False)
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    before = {tool_id: sys.monitoring.get_tool(tool_id) for tool_id in (3, 4)}
    with pytest.raises(MonitoringUnavailableError, match="free-threaded"):
        backend.start()
    assert {tool_id: sys.monitoring.get_tool(tool_id) for tool_id in (3, 4)} == before


def test_sysconfig_free_threaded_fallback_rejects_before_tool_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "_is_gil_enabled", None, raising=False)
    monkeypatch.setattr(backend_module.sysconfig, "get_config_var", lambda _name: 1)
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    before = {tool_id: sys.monitoring.get_tool(tool_id) for tool_id in (3, 4)}
    with pytest.raises(MonitoringUnavailableError, match="free-threaded"):
        backend.start()
    assert {tool_id: sys.monitoring.get_tool(tool_id) for tool_id in (3, 4)} == before


def test_free_threaded_build_is_rejected_even_when_runtime_gil_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    monkeypatch.setattr(backend_module.sysconfig, "get_config_var", lambda _name: 1)
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    before = {tool_id: sys.monitoring.get_tool(tool_id) for tool_id in (3, 4)}
    with pytest.raises(MonitoringUnavailableError, match="free-threaded"):
        backend.start()
    assert {tool_id: sys.monitoring.get_tool(tool_id) for tool_id in (3, 4)} == before


def test_busy_tool_id_three_falls_back_to_four() -> None:
    if sys.monitoring.get_tool(3) is not None or sys.monitoring.get_tool(4) is not None:
        pytest.skip("generic monitoring IDs are occupied by the test environment")
    sys.monitoring.use_tool_id(3, "trial005-busy-three")
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    try:
        backend.start()
        assert backend.health_snapshot().tool_id == 4
        with backend.request_scope("request-tool-four"):
            _thread_target("tool-four")
        assert len(collector.snapshot()) == 1
    finally:
        backend.stop()
        sys.monitoring.set_events(3, 0)
        sys.monitoring.free_tool_id(3)


def test_unclaimed_id_callback_is_not_stolen_or_deleted() -> None:
    if sys.monitoring.get_tool(3) is not None:
        pytest.skip("generic monitoring ID 3 is occupied by the test environment")

    def foreign_callback(_code, _line_no: int) -> None:
        return None

    previous = sys.monitoring.register_callback(3, sys.monitoring.events.LINE, foreign_callback)
    if previous is not None:
        sys.monitoring.register_callback(3, sys.monitoring.events.LINE, previous)
        pytest.skip("unclaimed monitoring ID 3 retained an external callback")
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector, candidate_tool_ids=(3,))
    try:
        with pytest.raises(MonitoringUnavailableError):
            backend.start()
        assert sys.monitoring.get_tool(3) is None
        retained = sys.monitoring.register_callback(3, sys.monitoring.events.LINE, None)
        assert retained is foreign_callback
    finally:
        sys.monitoring.register_callback(3, sys.monitoring.events.LINE, None)
        if sys.monitoring.get_tool(3) is not None:
            sys.monitoring.set_events(3, 0)
            sys.monitoring.free_tool_id(3)


def test_unclaimed_id_retained_global_events_are_not_cleared() -> None:
    result = _probe_python(
        """
import dis
import json
import sys
from spikes.tracepoint_backend import (
    MonitoringTracepointBackend,
    MonitoringUnavailableError,
    TracepointSpec,
)

def target(value):
    before = value
    result = before + 1
    return result

line = [
    line
    for offset, line in dis.findlinestarts(target.__code__)
    if offset > 0 and line != target.__code__.co_firstlineno
][1]
spec = TracepointSpec.from_function("retained", target, line, ("value", "before"))

def foreign(_code, _line):
    return None

sys.monitoring.use_tool_id(3, "retained-owner")
sys.monitoring.register_callback(3, sys.monitoring.events.LINE, foreign)
sys.monitoring.set_events(3, sys.monitoring.events.LINE)
sys.monitoring.set_local_events(3, target.__code__, sys.monitoring.events.LINE)
sys.monitoring.free_tool_id(3)
before = sys.monitoring.get_events(3)
local_before = sys.monitoring.get_local_events(3, target.__code__)
backend = MonitoringTracepointBackend((spec,), lambda _snapshot: True, candidate_tool_ids=(3,))
error = None
try:
    backend.start()
except MonitoringUnavailableError as caught:
    error = type(caught).__name__
after = sys.monitoring.get_events(3)
local_after = sys.monitoring.get_local_events(3, target.__code__)
retained = sys.monitoring.register_callback(3, sys.monitoring.events.LINE, None)
sys.monitoring.use_tool_id(3, "retained-cleanup")
sys.monitoring.set_events(3, 0)
sys.monitoring.set_local_events(3, target.__code__, 0)
sys.monitoring.free_tool_id(3)
print(json.dumps({
    "error": error,
    "before": before,
    "after": after,
    "local_before": local_before,
    "local_after": local_after,
    "callback_retained": retained is foreign,
}))
"""
    )
    assert result == {
        "error": "MonitoringUnavailableError",
        "before": sys.monitoring.events.LINE,
        "after": sys.monitoring.events.LINE,
        "local_before": sys.monitoring.events.LINE,
        "local_after": sys.monitoring.events.LINE,
        "callback_retained": True,
    }


def test_second_monitoring_tool_coexists_on_a_separate_id() -> None:
    other_id = next(
        (tool_id for tool_id in (0, 1, 2, 5) if sys.monitoring.get_tool(tool_id) is None),
        None,
    )
    if other_id is None:
        pytest.skip("no reserved monitoring ID is free for coexistence evidence")
    other_hits: list[int] = []

    def other_callback(_code, line_no: int) -> None:
        other_hits.append(line_no)

    sys.monitoring.use_tool_id(other_id, "trial005-other-tool")
    sys.monitoring.register_callback(other_id, sys.monitoring.events.LINE, other_callback)
    sys.monitoring.set_local_events(other_id, _thread_target.__code__, sys.monitoring.events.LINE)
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    try:
        backend.start()
        with backend.request_scope("request-other-tool"):
            _thread_target("coexist")
        assert collector.snapshot()
        assert _line(_thread_target, "TP_THREAD") in other_hits
    finally:
        backend.stop()
        sys.monitoring.set_local_events(other_id, _thread_target.__code__, 0)
        sys.monitoring.set_events(other_id, 0)
        sys.monitoring.register_callback(other_id, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(other_id)


def test_enable_disable_reenable_and_cleanup_are_exact() -> None:
    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), collector)
    backend.start()
    first_tool = backend.health_snapshot().tool_id
    assert first_tool in {3, 4}
    assert sys.monitoring.get_events(first_tool) == 0
    assert (
        sys.monitoring.get_local_events(first_tool, _thread_target.__code__)
        == sys.monitoring.events.LINE
    )
    with backend.request_scope("request-first"):
        _thread_target("first")
    assert backend.stop()
    assert backend.stop()
    assert backend.stop(timeout=threading.TIMEOUT_MAX * 2)
    assert sys.monitoring.get_tool(first_tool) is None
    assert sys.monitoring.get_local_events(first_tool, _thread_target.__code__) == 0
    _thread_target("disabled")

    backend.start()
    with backend.request_scope("request-second"):
        _thread_target("second")
    assert backend.stop()
    assert [snapshot.request_trace_id for snapshot in collector.snapshot()] == [
        "request-first",
        "request-second",
    ]


def test_successful_stop_drains_callback_blocked_before_context_admission() -> None:
    entered = threading.Event()
    release = threading.Event()
    handler_exited = threading.Event()
    stop_completed = threading.Event()
    stop_results: list[bool] = []

    class BlockingEntryBackend(MonitoringTracepointBackend):
        def _admit_line_callback(self, code, line_no: int):
            entered.set()
            try:
                assert release.wait(2.0)
                return super()._admit_line_callback(code, line_no)
            finally:
                handler_exited.set()

    collector = _SnapshotCollector()
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = BlockingEntryBackend((spec,), collector)
    backend.start()
    target_thread = threading.Thread(target=_thread_target, args=("unscoped",))

    def stop_backend() -> None:
        try:
            stop_results.append(backend.stop(timeout=2.0))
        finally:
            stop_completed.set()

    stop_thread = threading.Thread(target=stop_backend)
    try:
        target_thread.start()
        assert entered.wait(1.0)
        stop_thread.start()
        deadline = time.monotonic() + 1.0
        while not backend.health_snapshot().draining:
            assert time.monotonic() < deadline
            threading.Event().wait(0.001)
        assert not stop_completed.is_set()
        release.set()
        target_thread.join(2.0)
        stop_thread.join(2.0)
        assert not target_thread.is_alive()
        assert not stop_thread.is_alive()
        assert stop_results == [True]
        assert handler_exited.is_set()
        health_after_stop = backend.health_snapshot()
        _thread_target("after-stop")
        assert backend.health_snapshot() == health_after_stop
    finally:
        release.set()
        target_thread.join(2.0)
        stop_thread.join(2.0)
        backend.stop(timeout=1.0)


def test_sink_exception_and_rejection_are_visible_without_changing_business_result() -> None:
    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))

    def failed_sink(_snapshot: TracepointSnapshot) -> bool:
        raise RuntimeError("sink failed")

    failing = MonitoringTracepointBackend((spec,), failed_sink)
    failing.start()
    try:
        with failing.request_scope("request-failed-sink"):
            assert _thread_target("failed") == "failed-before:after"
    finally:
        assert failing.stop()
    failed_health = failing.health_snapshot()
    assert failed_health.callback_error_count == 1
    assert failed_health.queue_drop_count == 1
    assert failed_health.captured_snapshot_count == 0

    rejecting_collector = _SnapshotCollector(accept=False)
    rejecting = MonitoringTracepointBackend((spec,), rejecting_collector)
    rejecting.start()
    try:
        with rejecting.request_scope("request-rejected"):
            assert _thread_target("rejected") == "rejected-before:after"
    finally:
        assert rejecting.stop()
    rejected_health = rejecting.health_snapshot()
    assert rejected_health.queue_drop_count == 1
    assert rejected_health.captured_snapshot_count == 0


def test_hit_limit_bounds_loop_snapshots_and_reports_drops() -> None:
    collector = _SnapshotCollector()
    spec = _spec("loop", _loop_target, "TP_LOOP", ("index", "total"), hit_limit=2)
    backend = MonitoringTracepointBackend((spec,), collector)
    backend.start()
    try:
        with backend.request_scope("request-loop"):
            assert _loop_target(5) == 10
    finally:
        assert backend.stop()
    assert len(collector.snapshot()) == 2
    health = backend.health_snapshot()
    assert health.hit_limit_drop_count == 3
    assert health.callback_count == 2


def test_stop_timeout_is_retryable_while_nonblocking_sink_callback_is_inflight() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_sink(_snapshot: TracepointSnapshot) -> bool:
        entered.set()
        assert release.wait(2.0)
        return True

    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), blocked_sink)
    backend.start()

    def invoke() -> str:
        with backend.request_scope("request-blocked"):
            return _thread_target("blocked")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(invoke)
            assert entered.wait(1.0)
            assert backend.stop(timeout=0.01) is False
            health = backend.health_snapshot()
            assert health.draining is True
            assert health.last_error_code == "CALLBACK_DRAIN_TIMEOUT"
            release.set()
            assert future.result(timeout=2.0) == "blocked-before:after"
        assert backend.stop(timeout=1.0) is True
    finally:
        release.set()
        backend.stop(timeout=1.0)


def test_concurrent_stop_calls_are_serialized_and_idempotent() -> None:
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(3)

    def blocked_sink(_snapshot: TracepointSnapshot) -> bool:
        entered.set()
        assert release.wait(2.0)
        return True

    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), blocked_sink)
    backend.start()

    def invoke() -> str:
        with backend.request_scope("request-concurrent-stop"):
            return _thread_target("concurrent-stop")

    def stop_together() -> bool:
        barrier.wait(timeout=2.0)
        return backend.stop(timeout=2.0)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            invocation = executor.submit(invoke)
            assert entered.wait(1.0)
            first = executor.submit(stop_together)
            second = executor.submit(stop_together)
            barrier.wait(timeout=2.0)
            release.set()
            assert invocation.result(timeout=2.0) == "concurrent-stop-before:after"
            assert first.result(timeout=2.0) is True
            assert second.result(timeout=2.0) is True
    finally:
        release.set()
        backend.stop(timeout=1.0)
    assert backend.health_snapshot().cleanup_error_count == 0


def test_concurrent_short_stop_timeout_bounds_lifecycle_lock_wait() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_sink(_snapshot: TracepointSnapshot) -> bool:
        entered.set()
        assert release.wait(2.0)
        return True

    spec = _spec("thread", _thread_target, "TP_THREAD", ("label",))
    backend = MonitoringTracepointBackend((spec,), blocked_sink)
    backend.start()

    def invoke() -> str:
        with backend.request_scope("request-short-stop"):
            return _thread_target("short-stop")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            invocation = executor.submit(invoke)
            assert entered.wait(1.0)
            first_stop = executor.submit(backend.stop, 1.0)
            deadline = time.monotonic() + 1.0
            while not backend.health_snapshot().draining:
                assert time.monotonic() < deadline
                threading.Event().wait(0.001)
            started = time.monotonic()
            assert backend.stop(timeout=0.01) is False
            elapsed = time.monotonic() - started
            assert elapsed < 0.2
            assert backend.health_snapshot().last_error_code == "LIFECYCLE_LOCK_TIMEOUT"
            release.set()
            assert invocation.result(timeout=2.0) == "short-stop-before:after"
            assert first_stop.result(timeout=2.0) is True
    finally:
        release.set()
        backend.stop(timeout=1.0)


def test_original_function_exception_and_process_control_sink_exception_propagate() -> None:
    collector = _SnapshotCollector()
    spec = _spec("exception", _exception_target, "TP_EXCEPTION", ("value", "observed"))
    backend = MonitoringTracepointBackend((spec,), collector)
    backend.start()
    try:
        with backend.request_scope("request-exception"):
            with pytest.raises(ValueError, match="raw-exception-9"):
                _exception_target(9)
    finally:
        assert backend.stop()
    [snapshot] = collector.snapshot()
    assert "raw-exception" not in snapshot.payload_json

    def interrupting_sink(_snapshot: TracepointSnapshot) -> bool:
        raise KeyboardInterrupt("process control")

    interrupting = MonitoringTracepointBackend((spec,), interrupting_sink)
    interrupting.start()
    try:
        with interrupting.request_scope("request-interrupt"):
            with pytest.raises(KeyboardInterrupt, match="process control"):
                _exception_target(1)
    finally:
        assert interrupting.stop()


def test_bound_instance_class_and_static_method_shapes_are_supported() -> None:
    instance = _Methods()
    specs = (
        _spec("instance", instance.instance_target, "TP_INSTANCE", ("self", "value")),
        _spec("class", _Methods.class_target, "TP_CLASS", ("cls", "value")),
        _spec("static", _Methods.static_target, "TP_STATIC", ("value",)),
    )
    collector = _SnapshotCollector()
    backend = MonitoringTracepointBackend(specs, collector)
    backend.start()
    try:
        with backend.request_scope("request-methods"):
            assert instance.instance_target(1) == 2
            assert _Methods.class_target(1) == 3
            assert _Methods.static_target(1) == 4
    finally:
        assert backend.stop()
    assert [snapshot.tracepoint_id for snapshot in collector.snapshot()] == [
        "instance",
        "class",
        "static",
    ]


def test_closure_freevars_and_cellvars_are_supported() -> None:
    specs = (
        _spec("closure", _closure_target, "TP_CLOSURE", ("prefix", "value", "result")),
        _spec("cell", _cell_target, "TP_CELL", ("shared", "result")),
    )
    collector = _SnapshotCollector()
    backend = MonitoringTracepointBackend(specs, collector)
    backend.start()
    try:
        with backend.request_scope("request-closure"):
            assert _closure_target(4) == "closed:4"
            assert _cell_target(4) == 9
    finally:
        assert backend.stop()
    closure, cell = collector.snapshot()
    assert json.loads(closure.payload_json)["data"]["snapshot"] == {
        "prefix": "closed",
        "value": 4,
    }
    assert closure.missing_names == ("result",)
    assert json.loads(cell.payload_json)["data"]["snapshot"] == {"shared": 4}
    assert cell.missing_names == ("result",)


def test_audit_denial_rejects_backend_during_startup_probe_without_leaking_tool_ids() -> None:
    result = _probe_python(
        """
import dis
import json
import sys
from spikes.tracepoint_backend import (
    MonitoringTracepointBackend,
    MonitoringUnavailableError,
    TracepointSpec,
)

def target(value):
    before = value
    result = before + 1
    return result

line = [
    line
    for offset, line in dis.findlinestarts(target.__code__)
    if offset > 0 and line != target.__code__.co_firstlineno
][1]
spec = TracepointSpec.from_function("audit", target, line, ("value", "before"))

def audit(event, _args):
    if event == "sys._getframe":
        raise PermissionError("audit denied frame access")

sys.addaudithook(audit)
backend = MonitoringTracepointBackend((spec,), lambda _snapshot: True)
error = None
try:
    backend.start()
except MonitoringUnavailableError as caught:
    error = type(caught).__name__
print(json.dumps({
    "error": error,
    "result": target(4),
    "tool3": sys.monitoring.get_tool(3),
    "tool4": sys.monitoring.get_tool(4),
    "local3": sys.monitoring.get_local_events(3, target.__code__),
    "local4": sys.monitoring.get_local_events(4, target.__code__),
}))
"""
    )
    assert result == {
        "error": "MonitoringUnavailableError",
        "result": 5,
        "tool3": None,
        "tool4": None,
        "local3": 0,
        "local4": 0,
    }


def test_late_audit_denial_is_fail_open_and_visible() -> None:
    result = _probe_python(
        """
import dis
import json
import sys
from spikes.tracepoint_backend import MonitoringTracepointBackend, TracepointSpec

snapshots = []

def target(value):
    before = value
    result = before + 1
    return result

line = [
    line
    for offset, line in dis.findlinestarts(target.__code__)
    if offset > 0 and line != target.__code__.co_firstlineno
][1]
spec = TracepointSpec.from_function("audit-late", target, line, ("value", "before"))

def audit(event, args):
    if event == "sys._getframe" and args and args[0].f_code is target.__code__:
        raise PermissionError("late audit denial")

sys.addaudithook(audit)
backend = MonitoringTracepointBackend((spec,), lambda snapshot: snapshots.append(snapshot))
backend.start()
with backend.request_scope("request-audit-late"):
    result = target(4)
health = backend.health_snapshot()
stopped = backend.stop()
print(json.dumps({
    "result": result,
    "snapshots": len(snapshots),
    "callback_errors": health.callback_error_count,
    "last_error": health.last_error_code,
    "stopped": stopped,
}))
"""
    )
    assert result == {
        "result": 5,
        "snapshots": 0,
        "callback_errors": 1,
        "last_error": "PermissionError",
        "stopped": True,
    }


def test_backend_never_enables_global_monitoring_or_settrace_fallback() -> None:
    from spikes.tracepoint_backend import backend as backend_module

    source = inspect.getsource(backend_module)
    assert "restart_events" not in source
    assert "sys.settrace" not in source
    assert "threading.settrace" not in source


@pytest.mark.anyio
async def test_naive_sys_settrace_async_overlap_corrupts_ownership_and_leaks_slot() -> None:
    result = await run_naive_settrace_overlap_probe()
    assert result.request_a_seen_by_b is True
    assert result.request_b_lost_after_a_cleanup is True
    assert result.final_tracer_leaked is True
    assert sys.gettrace() is None


def _synthetic_budget_case(
    *,
    median_ratio: float = 1.0,
    p95_ratio: float = 1.0,
    median_active_ns: float = 1.0,
    p95_active_ns: float = 1.0,
) -> dict[str, object]:
    return {
        "median": {
            "paired_thread_cpu_ratio_active_over_baseline": median_ratio,
            "active_thread_cpu_ns_per_call": median_active_ns,
        },
        "p95_nearest_rank": {
            "paired_thread_cpu_ratio_active_over_baseline": p95_ratio,
            "active_thread_cpu_ns_per_call": p95_active_ns,
        },
    }


def _all_failing_performance_budget() -> dict[str, object]:
    return benchmark_module._performance_budget(
        _synthetic_budget_case(median_ratio=1.16, p95_ratio=1.76),
        _synthetic_budget_case(median_active_ns=15_001.0, p95_active_ns=25_001.0),
        _synthetic_budget_case(median_active_ns=200_001.0, p95_active_ns=300_001.0),
    )


def _synthetic_crossover_pair(
    *,
    repeat: int,
    order: str,
    baseline_thread_cpu_ns: int,
    active_thread_cpu_ns: int,
    iterations: int = 1,
    checksum: int | None = None,
) -> benchmark_module._PairResult:
    selected_checksum = repeat + 1 if checksum is None else checksum
    return benchmark_module._PairResult(
        repeat=repeat,
        order=order,
        iterations=iterations,
        baseline=benchmark_module._TimedResult(
            thread_cpu_elapsed_ns=baseline_thread_cpu_ns,
            monotonic_wall_elapsed_ns=baseline_thread_cpu_ns,
            checksum=selected_checksum,
        ),
        active=benchmark_module._TimedResult(
            thread_cpu_elapsed_ns=active_thread_cpu_ns,
            monotonic_wall_elapsed_ns=active_thread_cpu_ns,
            checksum=selected_checksum,
        ),
    )


def _synthetic_crossover_block(
    *,
    repeat: int,
    active_factor_numerator: int,
    active_factor_denominator: int,
) -> benchmark_module._PairResult:
    scale = 1_000_000
    order_bias_numerator = 6
    order_bias_denominator = 5
    ab = _synthetic_crossover_pair(
        repeat=repeat,
        order="AB",
        baseline_thread_cpu_ns=scale,
        active_thread_cpu_ns=(
            scale
            * active_factor_numerator
            * order_bias_numerator
            // active_factor_denominator
            // order_bias_denominator
        ),
    )
    ba = _synthetic_crossover_pair(
        repeat=repeat,
        order="BA",
        baseline_thread_cpu_ns=scale * order_bias_numerator // order_bias_denominator,
        active_thread_cpu_ns=(scale * active_factor_numerator // active_factor_denominator),
    )
    first, second = (ab, ba) if repeat % 2 == 0 else (ba, ab)
    return benchmark_module._combine_crossover_pairs(first, second)


def test_symmetric_no_hit_blocks_cancel_reciprocal_order_bias() -> None:
    legacy_pairs = tuple(
        _synthetic_crossover_pair(
            repeat=repeat,
            order="AB" if repeat % 2 == 0 else "BA",
            baseline_thread_cpu_ns=1_000_000 if repeat % 2 == 0 else 1_200_000,
            active_thread_cpu_ns=1_200_000 if repeat % 2 == 0 else 1_000_000,
        )
        for repeat in range(PAIRED_REPEATS)
    )
    legacy_case = benchmark_module._case_json(legacy_pairs)
    assert legacy_case["median"]["paired_thread_cpu_ratio_active_over_baseline"] == 1.2
    assert (
        legacy_case["median"]["paired_thread_cpu_ratio_active_over_baseline"]
        > benchmark_module.NO_HIT_MEDIAN_RATIO_MAX
    )

    blocks = tuple(
        _synthetic_crossover_block(
            repeat=repeat,
            active_factor_numerator=1,
            active_factor_denominator=1,
        )
        for repeat in range(PAIRED_REPEATS)
    )
    balanced_case = benchmark_module._case_json(blocks)
    assert balanced_case["raw"]["order"] == [
        "ABBA" if repeat % 2 == 0 else "BAAB" for repeat in range(PAIRED_REPEATS)
    ]
    assert balanced_case["median"]["paired_thread_cpu_ratio_active_over_baseline"] == 1.0
    assert balanced_case["p95_nearest_rank"]["paired_thread_cpu_ratio_active_over_baseline"] == 1.0
    serialized_blocks = balanced_case["pairs"]
    assert isinstance(serialized_blocks, list)
    assert all(
        isinstance(block, dict) and isinstance(block.get("legs"), list) and len(block["legs"]) == 2
        for block in serialized_blocks
    )
    budget = benchmark_module._performance_budget(
        balanced_case,
        _synthetic_budget_case(median_active_ns=1.0, p95_active_ns=1.0),
        _synthetic_budget_case(median_active_ns=1.0, p95_active_ns=1.0),
    )
    assert budget["passed"] is True


def test_symmetric_no_hit_blocks_preserve_real_active_overhead() -> None:
    blocks = tuple(
        _synthetic_crossover_block(
            repeat=repeat,
            active_factor_numerator=29,
            active_factor_denominator=25,
        )
        for repeat in range(PAIRED_REPEATS)
    )
    no_hit_case = benchmark_module._case_json(blocks)
    observed = no_hit_case["median"]["paired_thread_cpu_ratio_active_over_baseline"]
    assert observed == pytest.approx(1.16)
    budget = benchmark_module._performance_budget(
        no_hit_case,
        _synthetic_budget_case(median_active_ns=1.0, p95_active_ns=1.0),
        _synthetic_budget_case(median_active_ns=1.0, p95_active_ns=1.0),
    )
    checks = budget["checks"]
    median_check = checks["active_no_hit.median.paired_thread_cpu_ratio_active_over_baseline"]
    assert budget["passed"] is False
    assert median_check["observed"] == pytest.approx(1.16)
    assert median_check["maximum"] == 1.15
    assert median_check["passed"] is False


def test_crossover_block_rejects_missing_or_mismatched_legs() -> None:
    ab = _synthetic_crossover_pair(
        repeat=0,
        order="AB",
        baseline_thread_cpu_ns=1_000,
        active_thread_cpu_ns=1_000,
    )
    other_ab = _synthetic_crossover_pair(
        repeat=0,
        order="AB",
        baseline_thread_cpu_ns=1_000,
        active_thread_cpu_ns=1_000,
    )
    wrong_repeat_ba = _synthetic_crossover_pair(
        repeat=1,
        order="BA",
        baseline_thread_cpu_ns=1_000,
        active_thread_cpu_ns=1_000,
    )
    wrong_iterations_ba = _synthetic_crossover_pair(
        repeat=0,
        order="BA",
        baseline_thread_cpu_ns=1_000,
        active_thread_cpu_ns=1_000,
        iterations=2,
    )
    wrong_seed_ba = _synthetic_crossover_pair(
        repeat=0,
        order="BA",
        baseline_thread_cpu_ns=1_000,
        active_thread_cpu_ns=1_000,
        checksum=99,
    )
    with pytest.raises(ValueError, match="one AB and one BA"):
        benchmark_module._combine_crossover_pairs(ab, other_ab)
    with pytest.raises(ValueError, match="repeats must match"):
        benchmark_module._combine_crossover_pairs(ab, wrong_repeat_ba)
    with pytest.raises(ValueError, match="iterations must match"):
        benchmark_module._combine_crossover_pairs(ab, wrong_iterations_ba)
    with pytest.raises(ValueError, match="same workload seed"):
        benchmark_module._combine_crossover_pairs(ab, wrong_seed_ba)


@pytest.mark.parametrize(
    ("repeat", "expected_orders", "expected_block_order"),
    (
        (0, ("AB", "BA"), "ABBA"),
        (1, ("BA", "AB"), "BAAB"),
    ),
)
def test_crossover_block_uses_same_seed_iterations_and_opposite_orders(
    monkeypatch: pytest.MonkeyPatch,
    repeat: int,
    expected_orders: tuple[str, str],
    expected_block_order: str,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_measure_pair(**kwargs: object) -> benchmark_module._PairResult:
        calls.append(kwargs)
        order = kwargs["order"]
        assert isinstance(order, str)
        return _synthetic_crossover_pair(
            repeat=int(kwargs["repeat"]),
            order=order,
            baseline_thread_cpu_ns=1_000,
            active_thread_cpu_ns=1_000,
            iterations=int(kwargs["iterations"]),
        )

    monkeypatch.setattr(benchmark_module, "_measure_pair", fake_measure_pair)
    block = benchmark_module._measure_crossover_block(
        spec=object(),
        sink=benchmark_module._SinkCounter(),
        workload=lambda seed: seed,
        iterations=17,
        repeat=repeat,
        request_prefix="same-seed",
    )
    assert [call["order"] for call in calls] == list(expected_orders)
    assert [call["repeat"] for call in calls] == [repeat, repeat]
    assert [call["iterations"] for call in calls] == [17, 17]
    assert [call["request_prefix"] for call in calls] == ["same-seed", "same-seed"]
    assert block.order == expected_block_order
    assert block.iterations == 34
    assert len(block.legs) == 2


def test_scheduler_wait_changes_wall_diagnostics_without_changing_cpu_budget() -> None:
    def timed_result(
        thread_cpu_elapsed_ns: int,
        monotonic_wall_elapsed_ns: int,
    ) -> benchmark_module._TimedResult:
        thread_cpu_ticks = iter((10_000, 10_000 + thread_cpu_elapsed_ns))
        monotonic_wall_ticks = iter((20_000, 20_000 + monotonic_wall_elapsed_ns))
        return benchmark_module._time_batch(
            lambda seed: seed,
            iterations=1,
            seed=7,
            thread_cpu_clock_ns=thread_cpu_ticks.__next__,
            monotonic_wall_clock_ns=monotonic_wall_ticks.__next__,
        )

    def case(
        active_thread_cpu_elapsed_ns: int,
        *,
        baseline_monotonic_wall_elapsed_ns: int,
        active_monotonic_wall_elapsed_ns: int,
    ) -> dict[str, object]:
        pair = benchmark_module._PairResult(
            repeat=0,
            order="AB",
            iterations=1,
            baseline=timed_result(1_000, baseline_monotonic_wall_elapsed_ns),
            active=timed_result(
                active_thread_cpu_elapsed_ns,
                active_monotonic_wall_elapsed_ns,
            ),
        )
        assert pair.baseline.checksum == pair.active.checksum
        return benchmark_module._case_json((pair,))

    def budget(
        *,
        baseline_monotonic_wall_elapsed_ns: int,
        active_monotonic_wall_elapsed_ns: int,
    ) -> dict[str, object]:
        return benchmark_module._performance_budget(
            case(
                1_100,
                baseline_monotonic_wall_elapsed_ns=baseline_monotonic_wall_elapsed_ns,
                active_monotonic_wall_elapsed_ns=active_monotonic_wall_elapsed_ns,
            ),
            case(
                14_000,
                baseline_monotonic_wall_elapsed_ns=baseline_monotonic_wall_elapsed_ns,
                active_monotonic_wall_elapsed_ns=active_monotonic_wall_elapsed_ns,
            ),
            case(
                190_000,
                baseline_monotonic_wall_elapsed_ns=baseline_monotonic_wall_elapsed_ns,
                active_monotonic_wall_elapsed_ns=active_monotonic_wall_elapsed_ns,
            ),
        )

    quiet_budget = budget(
        baseline_monotonic_wall_elapsed_ns=1_000,
        active_monotonic_wall_elapsed_ns=1_000,
    )
    baseline_delayed_budget = budget(
        baseline_monotonic_wall_elapsed_ns=5_000_000_000,
        active_monotonic_wall_elapsed_ns=1_000,
    )
    active_delayed_budget = budget(
        baseline_monotonic_wall_elapsed_ns=1_000,
        active_monotonic_wall_elapsed_ns=5_000_000_000,
    )
    assert baseline_delayed_budget == quiet_budget
    assert active_delayed_budget == quiet_budget
    assert active_delayed_budget["passed"] is True
    checks = active_delayed_budget["checks"]
    assert isinstance(checks, dict)
    assert all("thread_cpu" in name for name in checks)

    quiet_case = case(
        14_000,
        baseline_monotonic_wall_elapsed_ns=1_000,
        active_monotonic_wall_elapsed_ns=1_000,
    )
    scheduler_delayed_case = case(
        14_000,
        baseline_monotonic_wall_elapsed_ns=1_000,
        active_monotonic_wall_elapsed_ns=5_000_000_000,
    )
    assert (
        scheduler_delayed_case["median"]["active_monotonic_wall_ns_per_call"]
        > quiet_case["median"]["active_monotonic_wall_ns_per_call"]
    )
    assert (
        scheduler_delayed_case["median"]["active_thread_cpu_ns_per_call"]
        == quiet_case["median"]["active_thread_cpu_ns_per_call"]
    )
    legacy_fields = {
        "baseline_elapsed_ns",
        "active_elapsed_ns",
        "baseline_ns_per_call",
        "active_ns_per_call",
        "paired_ratio_active_over_baseline",
        "overhead_ns_per_call",
    }
    for section_name in ("raw", "median", "p95_nearest_rank"):
        section = scheduler_delayed_case[section_name]
        assert isinstance(section, dict)
        assert legacy_fields.isdisjoint(section)
    pairs = scheduler_delayed_case["pairs"]
    assert isinstance(pairs, list)
    assert all(isinstance(pair, dict) and legacy_fields.isdisjoint(pair) for pair in pairs)
    metadata = benchmark_module._metadata()
    assert {"timer", "timer_monotonic", "timer_adjustable", "timer_resolution_seconds"}.isdisjoint(
        metadata
    )


def test_cpu_overage_fails_even_when_wall_diagnostic_is_below_budget() -> None:
    def case(active_thread_cpu_ns: int) -> dict[str, object]:
        pair = benchmark_module._PairResult(
            repeat=0,
            order="AB",
            iterations=1,
            baseline=benchmark_module._TimedResult(
                thread_cpu_elapsed_ns=1_000,
                monotonic_wall_elapsed_ns=1_000,
                checksum=1,
            ),
            active=benchmark_module._TimedResult(
                thread_cpu_elapsed_ns=active_thread_cpu_ns,
                monotonic_wall_elapsed_ns=1_000,
                checksum=1,
            ),
        )
        return benchmark_module._case_json((pair,))

    performance_budget = benchmark_module._performance_budget(
        case(1_000),
        case(benchmark_module.UNSCOPED_MEDIAN_NS_PER_CALL_MAX + 1),
        case(benchmark_module.HIT_MEDIAN_NS_PER_CALL_MAX + 1),
    )
    checks = performance_budget["checks"]
    assert performance_budget["passed"] is False
    assert checks["active_unscoped_target.median.active_thread_cpu_ns_per_call"]["passed"] is False
    assert checks["active_hit.median.active_thread_cpu_ns_per_call"]["passed"] is False


def test_frozen_cpu_budget_check_mapping_and_maxima_are_unchanged() -> None:
    performance_budget = _all_failing_performance_budget()
    checks = performance_budget["checks"]
    expected = {
        "active_no_hit.median.paired_thread_cpu_ratio_active_over_baseline": (
            1.16,
            1.15,
            "ratio",
        ),
        "active_no_hit.p95_nearest_rank.paired_thread_cpu_ratio_active_over_baseline": (
            1.76,
            1.75,
            "ratio",
        ),
        "active_unscoped_target.median.active_thread_cpu_ns_per_call": (
            15_001.0,
            15_000.0,
            "ns_per_call",
        ),
        "active_unscoped_target.p95_nearest_rank.active_thread_cpu_ns_per_call": (
            25_001.0,
            25_000.0,
            "ns_per_call",
        ),
        "active_hit.median.active_thread_cpu_ns_per_call": (
            200_001.0,
            200_000.0,
            "ns_per_call",
        ),
        "active_hit.p95_nearest_rank.active_thread_cpu_ns_per_call": (
            300_001.0,
            300_000.0,
            "ns_per_call",
        ),
    }
    assert set(checks) == set(expected)
    for name, (observed, maximum, unit) in expected.items():
        assert checks[name] == {
            "observed": observed,
            "maximum": maximum,
            "unit": unit,
            "passed": False,
        }


def test_no_hit_calibration_uses_thread_cpu_not_monotonic_wall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_iterations: list[int] = []

    def fake_measure_pair(**kwargs: object) -> benchmark_module._PairResult:
        iterations = int(kwargs["iterations"])
        repeat = int(kwargs["repeat"])
        observed_iterations.append(iterations)
        minimum = benchmark_module.CALIBRATION_MIN_THREAD_CPU_NS
        thread_cpu_by_repeat = (
            (minimum, 1),
            (1, minimum),
            (minimum, minimum),
        )
        baseline_thread_cpu_ns, active_thread_cpu_ns = thread_cpu_by_repeat[repeat]
        wall_ns = minimum * 100 if repeat < 2 else 1
        baseline = benchmark_module._TimedResult(
            thread_cpu_elapsed_ns=baseline_thread_cpu_ns,
            monotonic_wall_elapsed_ns=wall_ns,
            checksum=repeat,
        )
        active = benchmark_module._TimedResult(
            thread_cpu_elapsed_ns=active_thread_cpu_ns,
            monotonic_wall_elapsed_ns=wall_ns,
            checksum=repeat,
        )
        return benchmark_module._PairResult(
            repeat=repeat,
            order="AB",
            iterations=iterations,
            baseline=baseline,
            active=active,
        )

    monkeypatch.setattr(benchmark_module, "_measure_pair", fake_measure_pair)
    iterations, attempts = benchmark_module._calibrate_no_hit(
        object(),
        benchmark_module._SinkCounter(),
    )
    assert observed_iterations == [
        benchmark_module.CALIBRATION_INITIAL_ITERATIONS,
        benchmark_module.CALIBRATION_INITIAL_ITERATIONS * 2,
        benchmark_module.CALIBRATION_INITIAL_ITERATIONS * 4,
    ]
    assert iterations == benchmark_module.CALIBRATION_INITIAL_ITERATIONS * 4
    assert len(attempts) == 3


def test_performance_budget_failure_message_lists_each_failed_check() -> None:
    performance_budget = _all_failing_performance_budget()
    message = benchmark_module._performance_budget_failure_message(performance_budget)
    checks = performance_budget["checks"]
    assert isinstance(checks, dict)
    for name, check in checks.items():
        expected = f"{name}: observed={check['observed']!r}, maximum={check['maximum']!r}"
        assert message.count(expected) == 1


def test_frozen_benchmark_schema_and_deterministic_counts() -> None:
    report = run_benchmark()
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["workload_digest"] == _EXPECTED_WORKLOAD_DIGEST
    root = Path(__file__).resolve().parents[2]
    for evidence_path in (
        root / "spikes/tracepoint_backend/RESULT.md",
        root / "docs/flowsight-mvp-design.md",
    ):
        assert _EXPECTED_WORKLOAD_DIGEST in evidence_path.read_text()
    safe_summary_source = inspect.getsource(sys.modules[benchmark_safe_summary.__module__]).encode()
    assert report["workload_component_digests"]["safe_summary_module"] == (
        f"sha256:{hashlib.sha256(safe_summary_source).hexdigest()}"
    )
    assert report["metadata"]["v1_runtime_in_scope"] is True
    assert report["config"]["warmup_repeats"] == WARMUP_REPEATS
    assert report["config"]["paired_repeats"] == PAIRED_REPEATS
    assert report["config"]["no_hit_legs_per_sample"] == 4
    assert report["config"]["no_hit_sample_order"] == (
        "alternating ABBA/BAAB four-leg crossover blocks"
    )
    assert report["cases"]["active_no_hit"]["sample_count"] == PAIRED_REPEATS
    assert report["cases"]["active_unscoped_target"]["sample_count"] == PAIRED_REPEATS
    assert report["cases"]["active_hit"]["sample_count"] == PAIRED_REPEATS
    expected_hits = (WARMUP_REPEATS + PAIRED_REPEATS) * HIT_ITERATIONS
    assert report["sink_count"]["expected"] == expected_hits
    assert report["sink_count"]["observed"] == expected_hits
    assert report["sink_count"]["measured_unscoped_target_observed"] == 0
    assert report["sink_count"]["exact"] is True
    performance_budget = report["performance_budget"]
    assert performance_budget["passed"] is True, (
        benchmark_module._performance_budget_failure_message(performance_budget)
    )
    assert performance_budget["measurement_clock"] == "thread_time_ns"
    assert performance_budget["monotonic_wall_diagnostic_only"] is True
    assert len(performance_budget["checks"]) == 6
    assert all("thread_cpu" in name for name in performance_budget["checks"])
    assert report["calibration"]["minimum_thread_cpu_reached_by_both_cases"] is True
    assert report["metadata"]["thread_cpu_clock"]["used_for_performance_budget"] is True
    assert report["metadata"]["monotonic_wall_clock"]["diagnostic_only"] is True
    no_hit_blocks = report["cases"]["active_no_hit"]["pairs"]
    assert all(
        isinstance(block, dict)
        and block["order"] in {"ABBA", "BAAB"}
        and isinstance(block.get("legs"), list)
        and len(block["legs"]) == 2
        for block in no_hit_blocks
    )
    assert report["config"]["gc_restored_after"] is True
