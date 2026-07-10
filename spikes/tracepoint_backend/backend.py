"""Restricted ``sys.monitoring`` backend evidence for TRIAL-005 only."""

from __future__ import annotations

import dis
import inspect
import math
import platform
import sys
import sysconfig
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import CodeType, FrameType, FunctionType, MethodType
from typing import cast

from flowsight.security import safe_summary

_SUPPORTED_VERSIONS = {(3, 12), (3, 13)}
_GENERIC_TOOL_IDS = (3, 4)
_MAX_VARIABLE_NAMES = 32
_MAX_IDENTIFIER_CHARS = 128
_MAX_TRACEPOINT_ID_CHARS = 128
_MAX_FUNCTION_NAME_CHARS = 512


class UnsupportedTracepointError(ValueError):
    """The requested source/function shape is outside the proven subset."""


class MonitoringUnavailableError(RuntimeError):
    """The current interpreter cannot safely activate the proven backend."""


class MonitoringCleanupError(RuntimeError):
    """Monitoring state could not be released without ambiguity."""


@dataclass(frozen=True, slots=True)
class TracepointSpec:
    """One immutable, identifier-only tracepoint configuration."""

    tracepoint_id: str
    code: CodeType
    line_no: int
    variable_names: tuple[str, ...]
    function_name: str
    function_kind: str
    hit_limit: int = 128

    def __post_init__(self) -> None:
        """Enforce the complete support boundary for every construction path."""

        if type(self.tracepoint_id) is not str or not self.tracepoint_id:
            raise UnsupportedTracepointError("tracepoint_id must be a non-empty built-in str")
        if len(self.tracepoint_id) > _MAX_TRACEPOINT_ID_CHARS:
            raise UnsupportedTracepointError("tracepoint_id is too long")
        if type(self.code) is not CodeType:
            raise UnsupportedTracepointError("tracepoints require an exact Python code object")
        function_flags = inspect.CO_OPTIMIZED | inspect.CO_NEWLOCALS
        if self.code.co_flags & function_flags != function_flags:
            raise UnsupportedTracepointError("tracepoints require exact Python function code")
        if self.code.co_name.startswith("<"):
            raise UnsupportedTracepointError("lambda/comprehension code is unsupported")
        generator_flags = inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR
        if self.code.co_flags & generator_flags:
            raise UnsupportedTracepointError(
                "generator and async-generator tracepoints are unsupported"
            )
        if type(self.line_no) is not int or self.line_no <= 0:
            raise UnsupportedTracepointError("line_no must be a positive built-in int")
        executable_lines = {
            candidate_line
            for offset, candidate_line in dis.findlinestarts(self.code)
            if offset > 0
            and candidate_line is not None
            and candidate_line != self.code.co_firstlineno
        }
        if self.line_no not in executable_lines:
            raise UnsupportedTracepointError(
                "tracepoint line must be an executable non-definition line"
            )
        if type(self.variable_names) is not tuple or not self.variable_names:
            raise UnsupportedTracepointError("variable_names must be a non-empty built-in tuple")
        if len(self.variable_names) > _MAX_VARIABLE_NAMES:
            raise UnsupportedTracepointError("too many variable names")
        available_names = (
            set(self.code.co_varnames) | set(self.code.co_cellvars) | set(self.code.co_freevars)
        )
        seen: set[str] = set()
        for name in self.variable_names:
            if type(name) is not str or not name or len(name) > _MAX_IDENTIFIER_CHARS:
                raise UnsupportedTracepointError(
                    "variable names must be bounded non-empty built-in strings"
                )
            if not name.isidentifier():
                raise UnsupportedTracepointError(
                    "variable names must be identifiers, never Python expressions"
                )
            if name in seen:
                raise UnsupportedTracepointError("variable names must be unique")
            if name not in available_names:
                raise UnsupportedTracepointError(
                    f"variable name is not local to the target code: {name}"
                )
            seen.add(name)
        if (
            type(self.function_name) is not str
            or not self.function_name
            or len(self.function_name) > _MAX_FUNCTION_NAME_CHARS
            or self.function_name != self.code.co_qualname
        ):
            raise UnsupportedTracepointError("function_name must match the target code")
        expected_kind = "coroutine" if self.code.co_flags & inspect.CO_COROUTINE else "sync"
        if type(self.function_kind) is not str or self.function_kind != expected_kind:
            raise UnsupportedTracepointError("function_kind must match the target code")
        if type(self.hit_limit) is not int or not 1 <= self.hit_limit <= 4096:
            raise UnsupportedTracepointError("hit_limit must be a built-in int from 1 to 4096")

    @classmethod
    def from_function(
        cls,
        tracepoint_id: str,
        function: object,
        line_no: int,
        variable_names: tuple[str, ...],
        *,
        hit_limit: int = 128,
    ) -> TracepointSpec:
        """Validate a Python function and an executable non-definition line."""

        if type(function) is FunctionType:
            resolved = function
        elif type(function) is MethodType:
            resolved = cast(FunctionType, function.__func__)
        else:
            raise UnsupportedTracepointError(
                "tracepoints require an exact Python function or bound Python method"
            )
        code = resolved.__code__
        function_kind = "coroutine" if code.co_flags & inspect.CO_COROUTINE else "sync"
        return cls(
            tracepoint_id=tracepoint_id,
            code=code,
            line_no=line_no,
            variable_names=variable_names,
            function_name=resolved.__qualname__,
            function_kind=function_kind,
            hit_limit=hit_limit,
        )


@dataclass(frozen=True, slots=True)
class TracepointSnapshot:
    """Already-sanitized snapshot that retains no frame or runtime value."""

    tracepoint_id: str
    request_trace_id: str
    otel_trace_id: str | None
    span_id: str | None
    function_name: str
    line_no: int
    captured_names: tuple[str, ...]
    missing_names: tuple[str, ...]
    captured_at_ns: int
    payload_json: str
    payload_bytes: int
    redacted_count: int
    truncation_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class BackendHealth:
    """Bounded scalar health without callbacks, frames, or raw values."""

    active: bool
    accepting: bool
    draining: bool
    tool_id: int | None
    active_scope_count: int
    callback_count: int
    captured_snapshot_count: int
    queue_drop_count: int
    hit_limit_drop_count: int
    ignored_without_context_count: int
    inactive_context_drop_count: int
    frame_mismatch_count: int
    callback_error_count: int
    cleanup_error_count: int
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class _RequestCapture:
    marker: object
    token: object
    request_trace_id: str
    otel_trace_id: str | None
    span_id: str | None


@dataclass(slots=True)
class _ActiveScope:
    capture: _RequestCapture
    hit_counts: dict[str, int] = field(default_factory=dict)


_CURRENT_CAPTURE: ContextVar[_RequestCapture | None] = ContextVar(
    "flowsight_trial005_request_capture", default=None
)


def _frame_probe_target() -> str:
    known_local = "probe-before"
    observed_local = known_local
    return observed_local


type SnapshotSink = Callable[[TracepointSnapshot], bool | None]


class MonitoringTracepointBackend:
    """Use only per-code LINE events and request-scoped positive admission."""

    def __init__(
        self,
        specs: tuple[TracepointSpec, ...],
        sink: SnapshotSink,
        *,
        candidate_tool_ids: tuple[int, ...] = _GENERIC_TOOL_IDS,
    ) -> None:
        if type(specs) is not tuple or not specs:
            raise ValueError("specs must be a non-empty built-in tuple")
        if not callable(sink):
            raise TypeError("sink must be callable")
        if (
            type(candidate_tool_ids) is not tuple
            or not candidate_tool_ids
            or len(set(candidate_tool_ids)) != len(candidate_tool_ids)
            or any(
                type(tool_id) is not int or tool_id not in _GENERIC_TOOL_IDS
                for tool_id in candidate_tool_ids
            )
        ):
            raise ValueError("candidate_tool_ids must be a unique non-empty subset of (3, 4)")
        locations: dict[CodeType, dict[int, TracepointSpec]] = {}
        tracepoint_ids: set[str] = set()
        for spec in specs:
            if type(spec) is not TracepointSpec:
                raise TypeError("specs must contain exact TracepointSpec values")
            if spec.tracepoint_id in tracepoint_ids:
                raise ValueError("tracepoint IDs must be unique")
            tracepoint_ids.add(spec.tracepoint_id)
            by_line = locations.setdefault(spec.code, {})
            if spec.line_no in by_line:
                raise ValueError("only one tracepoint is allowed per code/line")
            by_line[spec.line_no] = spec

        self._specs = specs
        self._locations = locations
        self._sink = sink
        self._candidate_tool_ids = candidate_tool_ids
        self._marker = object()
        self._lifecycle_lock = threading.Lock()
        self._condition = threading.Condition(threading.RLock())
        self._callback_condition = threading.Condition(threading.Lock())
        self._publication_lock = threading.Lock()
        self._active_scopes: dict[object, _ActiveScope] = {}
        self._active = False
        self._accepting = False
        self._draining = False
        self._tool_id: int | None = None
        self._callback_admission_open = False
        self._callbacks_in_flight = 0
        self._callback_count = 0
        self._captured_snapshot_count = 0
        self._queue_drop_count = 0
        self._hit_limit_drop_count = 0
        self._ignored_without_context_count = 0
        self._inactive_context_drop_count = 0
        self._frame_mismatch_count = 0
        self._callback_error_count = 0
        self._cleanup_error_count = 0
        self._last_error_code: str | None = None

    def start(self) -> None:
        """Claim a neutral tool ID, self-probe frame access, and enable local LINE."""

        with self._lifecycle_lock:
            self._start()

    def _start(self) -> None:
        self._validate_runtime()
        with self._condition:
            if self._active:
                if self._accepting and not self._draining:
                    return
                raise RuntimeError("backend cleanup must finish before restart")
            tool_id = self._claim_and_probe_tool_id()
            enabled_codes: list[CodeType] = []
            callback_registered = False
            try:
                previous = sys.monitoring.register_callback(
                    tool_id, sys.monitoring.events.LINE, self._line_callback
                )
                if previous is not None:
                    sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, previous)
                    raise MonitoringUnavailableError(
                        "claimed monitoring ID retained another LINE callback"
                    )
                callback_registered = True
                if sys.monitoring.get_events(tool_id) != 0:
                    raise MonitoringUnavailableError("claimed monitoring ID has global events")
                for code in self._locations:
                    if sys.monitoring.get_local_events(tool_id, code) != 0:
                        raise MonitoringUnavailableError(
                            "claimed monitoring ID retained local events for a target"
                        )
                    sys.monitoring.set_local_events(tool_id, code, sys.monitoring.events.LINE)
                    enabled_codes.append(code)
            except BaseException:
                for code in enabled_codes:
                    sys.monitoring.set_local_events(tool_id, code, 0)
                sys.monitoring.set_events(tool_id, 0)
                if callback_registered:
                    sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, None)
                sys.monitoring.free_tool_id(tool_id)
                raise
            self._tool_id = tool_id
            self._active = True
            self._accepting = True
            self._draining = False
            self._last_error_code = None
            with self._callback_condition:
                self._callback_admission_open = True

    @contextmanager
    def request_scope(
        self,
        request_trace_id: str,
        *,
        otel_trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Iterator[None]:
        """Admit one context-propagated request and invalidate inherited late work."""

        self._validate_identity(request_trace_id, "request_trace_id", required=True)
        self._validate_identity(otel_trace_id, "otel_trace_id", required=False)
        self._validate_identity(span_id, "span_id", required=False)
        scope_token = object()
        capture = _RequestCapture(
            marker=self._marker,
            token=scope_token,
            request_trace_id=request_trace_id,
            otel_trace_id=otel_trace_id,
            span_id=span_id,
        )
        with self._condition:
            if not self._active or not self._accepting or self._draining:
                raise RuntimeError("tracepoint backend is not accepting request scopes")
            self._active_scopes[scope_token] = _ActiveScope(capture)
        context_token: Token[_RequestCapture | None] = _CURRENT_CAPTURE.set(capture)
        try:
            yield
        finally:
            _CURRENT_CAPTURE.reset(context_token)
            with self._publication_lock:
                with self._condition:
                    self._active_scopes.pop(scope_token, None)

    def stop(self, timeout: float = 2.0) -> bool:
        """Disable local events, drain callbacks, then unregister before freeing ID."""

        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite non-negative built-in number")
        deadline = time.monotonic() + timeout
        remaining = min(
            max(0.0, deadline - time.monotonic()),
            threading.TIMEOUT_MAX,
        )
        if not self._lifecycle_lock.acquire(timeout=remaining):
            with self._condition:
                self._last_error_code = "LIFECYCLE_LOCK_TIMEOUT"
            return False
        try:
            return self._stop(deadline)
        finally:
            self._lifecycle_lock.release()

    def _stop(self, deadline: float) -> bool:
        with self._condition:
            tool_id = self._tool_id
            if tool_id is None:
                with self._callback_condition:
                    self._callback_admission_open = False
                return True
            self._accepting = False
            self._draining = True
        with self._callback_condition:
            self._callback_admission_open = False
        with self._condition:
            try:
                for code in self._locations:
                    sys.monitoring.set_local_events(tool_id, code, 0)
                sys.monitoring.set_events(tool_id, 0)
            except Exception as error:
                self._cleanup_error_count += 1
                self._last_error_code = type(error).__name__
                raise MonitoringCleanupError("failed to disable monitoring events") from None
        with self._callback_condition:
            while self._callbacks_in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._condition:
                        self._last_error_code = "CALLBACK_DRAIN_TIMEOUT"
                    return False
                self._callback_condition.wait(min(remaining, threading.TIMEOUT_MAX))
        with self._publication_lock:
            try:
                sys.monitoring.set_events(tool_id, 0)
                sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, None)
                sys.monitoring.free_tool_id(tool_id)
            except Exception as error:
                with self._condition:
                    self._cleanup_error_count += 1
                    self._last_error_code = type(error).__name__
                raise MonitoringCleanupError("failed to release monitoring tool ID") from None
            with self._condition:
                self._active_scopes.clear()
                self._tool_id = None
                self._active = False
                self._draining = False
                self._last_error_code = None
                self._condition.notify_all()
        return True

    shutdown = stop

    def health_snapshot(self) -> BackendHealth:
        with self._condition:
            return BackendHealth(
                active=self._active,
                accepting=self._accepting,
                draining=self._draining,
                tool_id=self._tool_id,
                active_scope_count=len(self._active_scopes),
                callback_count=self._callback_count,
                captured_snapshot_count=self._captured_snapshot_count,
                queue_drop_count=self._queue_drop_count,
                hit_limit_drop_count=self._hit_limit_drop_count,
                ignored_without_context_count=self._ignored_without_context_count,
                inactive_context_drop_count=self._inactive_context_drop_count,
                frame_mismatch_count=self._frame_mismatch_count,
                callback_error_count=self._callback_error_count,
                cleanup_error_count=self._cleanup_error_count,
                last_error_code=self._last_error_code,
            )

    def _line_callback(self, code: CodeType, line_no: int) -> None:
        with self._callback_condition:
            if not self._callback_admission_open:
                return
            self._callbacks_in_flight += 1
        try:
            admission = self._admit_line_callback(code, line_no)
            if admission is None:
                return
            spec, capture = admission
            try:
                frame = sys._getframe(1)
                try:
                    if frame.f_code is not code or frame.f_lineno != line_no:
                        with self._condition:
                            self._frame_mismatch_count += 1
                            self._last_error_code = "FRAME_MISMATCH"
                        return
                    self._capture_snapshot(spec, capture, frame, line_no)
                finally:
                    del frame
            except Exception as error:
                with self._condition:
                    self._callback_error_count += 1
                    self._last_error_code = type(error).__name__
        finally:
            with self._callback_condition:
                self._callbacks_in_flight -= 1
                self._callback_condition.notify_all()

    def _admit_line_callback(
        self, code: CodeType, line_no: int
    ) -> tuple[TracepointSpec, _RequestCapture] | None:
        by_line = self._locations.get(code)
        if by_line is None:
            return None
        spec = by_line.get(line_no)
        if spec is None:
            return None
        capture = _CURRENT_CAPTURE.get()
        if capture is None or capture.marker is not self._marker:
            with self._condition:
                self._ignored_without_context_count += 1
            return None
        with self._condition:
            if not self._active or not self._accepting:
                return None
            active_scope = self._active_scopes.get(capture.token)
            if active_scope is None or active_scope.capture is not capture:
                self._inactive_context_drop_count += 1
                return None
            hit_count = active_scope.hit_counts.get(spec.tracepoint_id, 0)
            if hit_count >= spec.hit_limit:
                self._hit_limit_drop_count += 1
                return None
            active_scope.hit_counts[spec.tracepoint_id] = hit_count + 1
            self._callback_count += 1
        return spec, capture

    def _capture_snapshot(
        self,
        spec: TracepointSpec,
        capture: _RequestCapture,
        frame: FrameType,
        line_no: int,
    ) -> None:
        local_values = frame.f_locals
        selected: dict[str, object] = {}
        missing: list[str] = []
        for name in spec.variable_names:
            try:
                selected[name] = local_values[name]
            except KeyError:
                missing.append(name)
        summary = safe_summary({"snapshot": selected})
        snapshot = TracepointSnapshot(
            tracepoint_id=spec.tracepoint_id,
            request_trace_id=capture.request_trace_id,
            otel_trace_id=capture.otel_trace_id,
            span_id=capture.span_id,
            function_name=spec.function_name,
            line_no=line_no,
            captured_names=tuple(selected),
            missing_names=tuple(missing),
            captured_at_ns=time.time_ns(),
            payload_json=summary.payload_json,
            payload_bytes=summary.payload_bytes,
            redacted_count=summary.redacted_count,
            truncation_count=summary.truncation_count,
            truncated=summary.truncated,
        )
        del selected
        del local_values
        with self._publication_lock:
            with self._condition:
                active_scope = self._active_scopes.get(capture.token)
                if (
                    not self._active
                    or not self._accepting
                    or active_scope is None
                    or active_scope.capture is not capture
                ):
                    self._inactive_context_drop_count += 1
                    return
            try:
                accepted = self._sink(snapshot) is not False
            except Exception as error:
                with self._condition:
                    self._callback_error_count += 1
                    self._queue_drop_count += 1
                    self._last_error_code = type(error).__name__
                return
            with self._condition:
                if accepted:
                    self._captured_snapshot_count += 1
                else:
                    self._queue_drop_count += 1

    def _claim_and_probe_tool_id(self) -> int:
        errors: list[str] = []
        for tool_id in self._candidate_tool_ids:
            try:
                sys.monitoring.use_tool_id(tool_id, "flowsight-trial005")
            except ValueError:
                errors.append(f"TOOL_ID_{tool_id}_BUSY")
                continue
            except Exception as error:
                errors.append(type(error).__name__)
                continue
            retained_mask = (
                sys.monitoring.get_events(tool_id) != 0
                or sys.monitoring.get_local_events(tool_id, _frame_probe_target.__code__) != 0
                or any(
                    sys.monitoring.get_local_events(tool_id, code) != 0 for code in self._locations
                )
            )
            if retained_mask:
                errors.append(f"TOOL_ID_{tool_id}_RETAINED_EVENTS")
                sys.monitoring.free_tool_id(tool_id)
                continue
            if self._has_retained_line_callback(tool_id):
                errors.append(f"TOOL_ID_{tool_id}_RETAINED_CALLBACK")
                sys.monitoring.free_tool_id(tool_id)
                continue
            try:
                self._probe_frame_access(tool_id)
            except BaseException as error:
                errors.append(type(error).__name__)
                try:
                    sys.monitoring.set_local_events(tool_id, _frame_probe_target.__code__, 0)
                finally:
                    try:
                        sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, None)
                    finally:
                        sys.monitoring.free_tool_id(tool_id)
                if not isinstance(error, Exception):
                    raise
                continue
            return tool_id
        with self._condition:
            self._last_error_code = errors[-1] if errors else "NO_TOOL_ID"
        raise MonitoringUnavailableError(
            "no safe generic sys.monitoring tool ID/frame contract is available"
        )

    @staticmethod
    def _has_retained_line_callback(tool_id: int) -> bool:
        def placeholder(_code: CodeType, _line_no: int) -> None:
            return None

        previous = sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.LINE, placeholder
        )
        if previous is None:
            sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, None)
            return False
        sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, previous)
        return True

    @staticmethod
    def _probe_frame_access(tool_id: int) -> None:
        if sys.monitoring.get_events(tool_id) != 0:
            raise MonitoringUnavailableError("probe tool ID retained global events")
        if sys.monitoring.get_local_events(tool_id, _frame_probe_target.__code__) != 0:
            raise MonitoringUnavailableError("probe tool ID retained local events")
        observations: list[bool] = []

        def probe_callback(code: CodeType, line_no: int) -> None:
            try:
                frame = sys._getframe(1)
                try:
                    observations.append(
                        frame.f_code is code
                        and frame.f_lineno == line_no
                        and (
                            "known_local" not in frame.f_locals
                            or frame.f_locals["known_local"] == "probe-before"
                        )
                    )
                finally:
                    del frame
            except Exception:
                observations.append(False)

        previous = sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.LINE, probe_callback
        )
        if previous is not None:
            sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, previous)
            raise MonitoringUnavailableError("probe tool ID retained a LINE callback")
        try:
            sys.monitoring.set_local_events(
                tool_id, _frame_probe_target.__code__, sys.monitoring.events.LINE
            )
            if _frame_probe_target() != "probe-before":
                raise MonitoringUnavailableError("frame probe target returned unexpectedly")
        finally:
            sys.monitoring.set_local_events(tool_id, _frame_probe_target.__code__, 0)
            sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, None)
        if not observations or not all(observations):
            raise MonitoringUnavailableError("direct monitoring callback frame probe failed")

    @staticmethod
    def _validate_runtime() -> None:
        if sys.implementation.name != "cpython" or sys.version_info[:2] not in _SUPPORTED_VERSIONS:
            raise MonitoringUnavailableError("tracepoints require standard CPython 3.12 or 3.13")
        configured = sysconfig.get_config_var("Py_GIL_DISABLED")
        if configured is True or configured == 1 or configured == "1":
            raise MonitoringUnavailableError("free-threaded CPython builds are unsupported")
        gil_probe = getattr(sys, "_is_gil_enabled", None)
        if callable(gil_probe) and not cast(Callable[[], bool], gil_probe)():
            raise MonitoringUnavailableError("free-threaded CPython is unsupported")
        if not hasattr(sys, "monitoring"):
            raise MonitoringUnavailableError("sys.monitoring is unavailable")
        if platform.python_implementation() != "CPython":
            raise MonitoringUnavailableError("non-CPython runtimes are unsupported")

    @staticmethod
    def _validate_identity(value: str | None, name: str, *, required: bool) -> None:
        if value is None and not required:
            return
        if type(value) is not str or not value or len(value) > 128:
            raise ValueError(f"{name} must be a bounded non-empty built-in str")
