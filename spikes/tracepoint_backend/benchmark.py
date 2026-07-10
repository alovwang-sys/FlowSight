"""Frozen overhead benchmark for the TRIAL-005 tracepoint backend spike.

The harness intentionally has no tuning flags.  A result is comparable only when
``schema_version`` and ``workload_digest`` match, so every workload and sampling
constant is kept in this module and included in the digest.
"""

from __future__ import annotations

import gc
import hashlib
import inspect
import json
import math
import platform
import statistics
import sys
import sysconfig
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import get_clock_info, perf_counter_ns
from typing import cast

from flowsight.security import safe_summary as benchmark_safe_summary

from .backend import (
    MonitoringTracepointBackend,
    TracepointSnapshot,
    TracepointSpec,
)

SCHEMA_VERSION = 2
WARMUP_REPEATS = 5
PAIRED_REPEATS = 21
CALIBRATION_MIN_NS = 50_000_000
CALIBRATION_INITIAL_ITERATIONS = 64
CALIBRATION_MAX_ITERATIONS = 1 << 30
HIT_ITERATIONS = 1_024
UNSCOPED_TARGET_ITERATIONS = 32_768
TRACEPOINT_HIT_LIMIT = 2_048
STOP_TIMEOUT_SECONDS = 5.0

NO_HIT_MEDIAN_RATIO_MAX = 1.15
NO_HIT_P95_RATIO_MAX = 1.75
UNSCOPED_MEDIAN_NS_PER_CALL_MAX = 15_000.0
UNSCOPED_P95_NS_PER_CALL_MAX = 25_000.0
HIT_MEDIAN_NS_PER_CALL_MAX = 200_000.0
HIT_P95_NS_PER_CALL_MAX = 300_000.0

_MASK_32 = (1 << 32) - 1
_NO_HIT_STEPS = 32
_TRACEPOINT_TARGET_MARKER = "TRACEPOINT_BENCHMARK_TARGET"
_TRACEPOINT_ID = "benchmark-hit"
_VARIABLE_NAMES = ("watched",)
_PAIR_SEED_MULTIPLIER = 1_000_003

Workload = Callable[[int], int]


def _no_hit_workload(seed: int) -> int:
    """Deterministic work in a code object with no configured tracepoint."""

    value = seed & _MASK_32
    for index in range(_NO_HIT_STEPS):
        value = ((value * 1_664_525) + index + 1_013_904_223) & _MASK_32
    return value


def _hit_workload(seed: int) -> int:
    """Small deterministic function that crosses one tracepoint exactly once."""

    watched = seed & _MASK_32
    observed = watched + 17  # TRACEPOINT_BENCHMARK_TARGET
    return (observed * 33) & _MASK_32


def _run_batch(workload: Workload, iterations: int, seed: int) -> int:
    checksum = 0
    for offset in range(iterations):
        checksum ^= workload(seed + offset)
    return checksum


@dataclass(frozen=True, slots=True)
class _TimedResult:
    elapsed_ns: int
    checksum: int


@dataclass(frozen=True, slots=True)
class _PairResult:
    repeat: int
    order: str
    iterations: int
    baseline: _TimedResult
    active: _TimedResult

    def as_json(self) -> dict[str, object]:
        baseline_ns_per_call = self.baseline.elapsed_ns / self.iterations
        active_ns_per_call = self.active.elapsed_ns / self.iterations
        return {
            "repeat": self.repeat,
            "order": self.order,
            "iterations": self.iterations,
            "baseline_elapsed_ns": self.baseline.elapsed_ns,
            "active_elapsed_ns": self.active.elapsed_ns,
            "baseline_ns_per_call": baseline_ns_per_call,
            "active_ns_per_call": active_ns_per_call,
            "paired_ratio_active_over_baseline": (
                self.active.elapsed_ns / self.baseline.elapsed_ns
            ),
            "overhead_ns_per_call": active_ns_per_call - baseline_ns_per_call,
        }


class _SinkCounter:
    """Minimal accepting sink whose exact invocation count is benchmark evidence."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, snapshot: TracepointSnapshot) -> bool:
        # Touch the frozen wire fields so the benchmark includes materializing the
        # snapshot consumed by the production-facing sink contract.
        if snapshot.payload_bytes < 0 or not snapshot.payload_json:
            raise RuntimeError("tracepoint backend emitted an invalid snapshot")
        self.count += 1
        return True


def _target_line_no() -> int:
    source_lines, first_line = inspect.getsourcelines(_hit_workload)
    matches = [
        first_line + offset
        for offset, source_line in enumerate(source_lines)
        if _TRACEPOINT_TARGET_MARKER in source_line
    ]
    if len(matches) != 1:
        raise RuntimeError("benchmark tracepoint target marker must occur exactly once")
    return matches[0]


def _workload_sources() -> dict[str, str]:
    return {
        "benchmark_module": inspect.getsource(sys.modules[__name__]),
        "backend_module": inspect.getsource(sys.modules[MonitoringTracepointBackend.__module__]),
        "safe_summary_module": inspect.getsource(sys.modules[benchmark_safe_summary.__module__]),
    }


def _workload_component_digests() -> dict[str, str]:
    return {
        name: f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"
        for name, source in _workload_sources().items()
    }


def _workload_digest() -> str:
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "sources": _workload_sources(),
    }
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _time_batch(workload: Workload, iterations: int, seed: int) -> _TimedResult:
    started_ns = perf_counter_ns()
    checksum = _run_batch(workload, iterations, seed)
    elapsed_ns = perf_counter_ns() - started_ns
    if elapsed_ns <= 0:
        raise RuntimeError("perf_counter_ns did not advance during a benchmark sample")
    return _TimedResult(elapsed_ns=elapsed_ns, checksum=checksum)


def _new_backend(
    spec: TracepointSpec,
    sink: _SinkCounter,
) -> MonitoringTracepointBackend:
    return MonitoringTracepointBackend(specs=(spec,), sink=sink)


def _active_sample(
    backend: MonitoringTracepointBackend,
    workload: Workload,
    iterations: int,
    seed: int,
    request_trace_id: str | None,
) -> _TimedResult:
    backend.start()
    try:
        if request_trace_id is None:
            return _time_batch(workload, iterations, seed)
        with backend.request_scope(request_trace_id):
            return _time_batch(workload, iterations, seed)
    finally:
        if not backend.stop(timeout=STOP_TIMEOUT_SECONDS):
            raise RuntimeError("tracepoint backend did not stop within the frozen timeout")


def _measure_pair(
    *,
    spec: TracepointSpec,
    sink: _SinkCounter,
    workload: Workload,
    iterations: int,
    repeat: int,
    request_prefix: str | None,
) -> _PairResult:
    seed = (repeat + 1) * _PAIR_SEED_MULTIPLIER
    backend = _new_backend(spec, sink)
    request_trace_id = None if request_prefix is None else f"{request_prefix}-{repeat}"
    if repeat % 2 == 0:
        order = "AB"
        baseline = _time_batch(workload, iterations, seed)
        active = _active_sample(
            backend,
            workload,
            iterations,
            seed,
            request_trace_id,
        )
    else:
        order = "BA"
        active = _active_sample(
            backend,
            workload,
            iterations,
            seed,
            request_trace_id,
        )
        baseline = _time_batch(workload, iterations, seed)

    if baseline.checksum != active.checksum:
        raise RuntimeError("baseline and active benchmark workloads diverged")
    return _PairResult(
        repeat=repeat,
        order=order,
        iterations=iterations,
        baseline=baseline,
        active=active,
    )


def _calibrate_no_hit(
    spec: TracepointSpec,
    sink: _SinkCounter,
) -> tuple[int, list[dict[str, object]]]:
    iterations = CALIBRATION_INITIAL_ITERATIONS
    attempts: list[dict[str, object]] = []
    attempt = 0
    while True:
        pair = _measure_pair(
            spec=spec,
            sink=sink,
            workload=_no_hit_workload,
            iterations=iterations,
            repeat=attempt,
            request_prefix="calibration-no-hit",
        )
        attempts.append(pair.as_json())
        if (
            pair.baseline.elapsed_ns >= CALIBRATION_MIN_NS
            and pair.active.elapsed_ns >= CALIBRATION_MIN_NS
        ):
            return iterations, attempts
        if iterations >= CALIBRATION_MAX_ITERATIONS:
            raise RuntimeError("could not calibrate both no-hit cases to at least 50ms")
        iterations = min(iterations * 2, CALIBRATION_MAX_ITERATIONS)
        attempt += 1


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0.0 < percentile <= 1.0:
        raise ValueError("percentile must be in (0, 1]")
    rank = math.ceil(percentile * len(values))
    return sorted(values)[rank - 1]


def _case_json(pairs: Sequence[_PairResult]) -> dict[str, object]:
    if not pairs:
        raise ValueError("benchmark case requires at least one pair")
    iterations = pairs[0].iterations
    if any(pair.iterations != iterations for pair in pairs):
        raise ValueError("benchmark case pairs must use one frozen iteration count")

    baseline_ns_per_call = [pair.baseline.elapsed_ns / iterations for pair in pairs]
    active_ns_per_call = [pair.active.elapsed_ns / iterations for pair in pairs]
    paired_ratios = [pair.active.elapsed_ns / pair.baseline.elapsed_ns for pair in pairs]
    overhead_ns_per_call = [
        active - baseline
        for baseline, active in zip(baseline_ns_per_call, active_ns_per_call, strict=True)
    ]
    return {
        "iterations": iterations,
        "sample_count": len(pairs),
        "raw": {
            "order": [pair.order for pair in pairs],
            "baseline_elapsed_ns": [pair.baseline.elapsed_ns for pair in pairs],
            "active_elapsed_ns": [pair.active.elapsed_ns for pair in pairs],
            "baseline_ns_per_call": baseline_ns_per_call,
            "active_ns_per_call": active_ns_per_call,
            "paired_ratio_active_over_baseline": paired_ratios,
            "overhead_ns_per_call": overhead_ns_per_call,
        },
        "median": {
            "baseline_ns_per_call": statistics.median(baseline_ns_per_call),
            "active_ns_per_call": statistics.median(active_ns_per_call),
            "paired_ratio_active_over_baseline": statistics.median(paired_ratios),
            "overhead_ns_per_call": statistics.median(overhead_ns_per_call),
        },
        "p95_nearest_rank": {
            "baseline_ns_per_call": _nearest_rank(baseline_ns_per_call, 0.95),
            "active_ns_per_call": _nearest_rank(active_ns_per_call, 0.95),
            "paired_ratio_active_over_baseline": _nearest_rank(paired_ratios, 0.95),
            "overhead_ns_per_call": _nearest_rank(overhead_ns_per_call, 0.95),
        },
        "pairs": [pair.as_json() for pair in pairs],
    }


def _case_metric(case: dict[str, object], aggregate: str, metric: str) -> float:
    aggregate_values = case.get(aggregate)
    if not isinstance(aggregate_values, dict):
        raise RuntimeError(f"benchmark case is missing aggregate {aggregate}")
    value = aggregate_values.get(metric)
    if type(value) not in {int, float}:
        raise RuntimeError(f"benchmark case is missing numeric metric {aggregate}.{metric}")
    return float(cast(float, value))


def _performance_budget(
    no_hit_case: dict[str, object],
    unscoped_target_case: dict[str, object],
    hit_case: dict[str, object],
) -> dict[str, object]:
    definitions = {
        "active_no_hit.median.paired_ratio_active_over_baseline": (
            _case_metric(no_hit_case, "median", "paired_ratio_active_over_baseline"),
            NO_HIT_MEDIAN_RATIO_MAX,
        ),
        "active_no_hit.p95_nearest_rank.paired_ratio_active_over_baseline": (
            _case_metric(
                no_hit_case,
                "p95_nearest_rank",
                "paired_ratio_active_over_baseline",
            ),
            NO_HIT_P95_RATIO_MAX,
        ),
        "active_unscoped_target.median.active_ns_per_call": (
            _case_metric(unscoped_target_case, "median", "active_ns_per_call"),
            UNSCOPED_MEDIAN_NS_PER_CALL_MAX,
        ),
        "active_unscoped_target.p95_nearest_rank.active_ns_per_call": (
            _case_metric(
                unscoped_target_case,
                "p95_nearest_rank",
                "active_ns_per_call",
            ),
            UNSCOPED_P95_NS_PER_CALL_MAX,
        ),
        "active_hit.median.active_ns_per_call": (
            _case_metric(hit_case, "median", "active_ns_per_call"),
            HIT_MEDIAN_NS_PER_CALL_MAX,
        ),
        "active_hit.p95_nearest_rank.active_ns_per_call": (
            _case_metric(hit_case, "p95_nearest_rank", "active_ns_per_call"),
            HIT_P95_NS_PER_CALL_MAX,
        ),
    }
    checks = {
        name: {
            "observed": observed,
            "maximum": maximum,
            "passed": observed <= maximum,
        }
        for name, (observed, maximum) in definitions.items()
    }
    return {
        "kind": "TRIAL-005 Phase 4 spike regression guard",
        "not_a_request_sla": True,
        "checks": checks,
        "passed": all(bool(check["passed"]) for check in checks.values()),
    }


def _gil_status() -> tuple[bool, bool, str]:
    configured = sysconfig.get_config_var("Py_GIL_DISABLED")
    free_threaded_build = configured is True or configured == 1 or configured == "1"
    probe = getattr(sys, "_is_gil_enabled", None)
    if callable(probe):
        return (
            bool(cast(Callable[[], object], probe)()),
            free_threaded_build,
            "sys._is_gil_enabled+sysconfig:Py_GIL_DISABLED",
        )
    return not free_threaded_build, free_threaded_build, "sysconfig:Py_GIL_DISABLED"


def _metadata() -> dict[str, object]:
    gil_enabled, free_threaded_build, gil_probe = _gil_status()
    clock = get_clock_info("perf_counter")
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "implementation_cache_tag": sys.implementation.cache_tag,
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "gil_enabled": gil_enabled,
        "free_threaded_build": free_threaded_build,
        "gil_probe": gil_probe,
        "v1_runtime_in_scope": (
            platform.python_implementation() == "CPython"
            and sys.version_info[:2] in {(3, 12), (3, 13)}
            and gil_enabled
            and not free_threaded_build
        ),
        "timer": "perf_counter_ns",
        "timer_monotonic": clock.monotonic,
        "timer_adjustable": clock.adjustable,
        "timer_resolution_seconds": clock.resolution,
    }


def run_benchmark() -> dict[str, object]:
    """Run the immutable benchmark and return its JSON-compatible report."""

    sink = _SinkCounter()
    spec = TracepointSpec.from_function(
        tracepoint_id=_TRACEPOINT_ID,
        function=_hit_workload,
        line_no=_target_line_no(),
        variable_names=_VARIABLE_NAMES,
        hit_limit=TRACEPOINT_HIT_LIMIT,
    )

    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        no_hit_iterations, calibration_attempts = _calibrate_no_hit(spec, sink)
        if sink.count != 0:
            raise RuntimeError("no-hit calibration unexpectedly emitted a snapshot")

        for warmup in range(WARMUP_REPEATS):
            _measure_pair(
                spec=spec,
                sink=sink,
                workload=_no_hit_workload,
                iterations=no_hit_iterations,
                repeat=warmup,
                request_prefix="warmup-no-hit",
            )
            _measure_pair(
                spec=spec,
                sink=sink,
                workload=_hit_workload,
                iterations=UNSCOPED_TARGET_ITERATIONS,
                repeat=warmup,
                request_prefix=None,
            )
            _measure_pair(
                spec=spec,
                sink=sink,
                workload=_hit_workload,
                iterations=HIT_ITERATIONS,
                repeat=warmup,
                request_prefix="warmup-hit",
            )

        expected_warmup_sink_count = WARMUP_REPEATS * HIT_ITERATIONS
        sink_count_after_warmups = sink.count
        if sink_count_after_warmups != expected_warmup_sink_count:
            raise RuntimeError(
                "tracepoint warmup sink count mismatch: "
                f"expected {expected_warmup_sink_count}, "
                f"observed {sink_count_after_warmups}"
            )

        no_hit_pairs = [
            _measure_pair(
                spec=spec,
                sink=sink,
                workload=_no_hit_workload,
                iterations=no_hit_iterations,
                repeat=repeat,
                request_prefix="measured-no-hit",
            )
            for repeat in range(PAIRED_REPEATS)
        ]
        sink_count_after_no_hit = sink.count
        if sink_count_after_no_hit != sink_count_after_warmups:
            raise RuntimeError("active no-hit measurement unexpectedly emitted a snapshot")
        unscoped_target_pairs = [
            _measure_pair(
                spec=spec,
                sink=sink,
                workload=_hit_workload,
                iterations=UNSCOPED_TARGET_ITERATIONS,
                repeat=repeat,
                request_prefix=None,
            )
            for repeat in range(PAIRED_REPEATS)
        ]
        sink_count_after_unscoped_target = sink.count
        if sink_count_after_unscoped_target != sink_count_after_no_hit:
            raise RuntimeError("unscoped target-code measurement unexpectedly emitted a snapshot")
        hit_pairs = [
            _measure_pair(
                spec=spec,
                sink=sink,
                workload=_hit_workload,
                iterations=HIT_ITERATIONS,
                repeat=repeat,
                request_prefix="measured-hit",
            )
            for repeat in range(PAIRED_REPEATS)
        ]
    finally:
        if gc_was_enabled:
            gc.enable()

    expected_measured_hit_sink_count = PAIRED_REPEATS * HIT_ITERATIONS
    expected_sink_count = expected_warmup_sink_count + expected_measured_hit_sink_count
    if sink.count != expected_sink_count:
        raise RuntimeError(
            f"tracepoint sink count mismatch: expected {expected_sink_count}, observed {sink.count}"
        )

    no_hit_case = _case_json(no_hit_pairs)
    unscoped_target_case = _case_json(unscoped_target_pairs)
    hit_case = _case_json(hit_pairs)
    performance_budget = _performance_budget(
        no_hit_case,
        unscoped_target_case,
        hit_case,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "workload_digest": _workload_digest(),
        "workload_component_digests": _workload_component_digests(),
        "metadata": _metadata(),
        "config": {
            "warmup_repeats": WARMUP_REPEATS,
            "paired_repeats": PAIRED_REPEATS,
            "pair_order": "alternating AB/BA; A=baseline, B=active",
            "calibration_min_ns": CALIBRATION_MIN_NS,
            "calibration_initial_iterations": CALIBRATION_INITIAL_ITERATIONS,
            "hit_iterations": HIT_ITERATIONS,
            "unscoped_target_iterations": UNSCOPED_TARGET_ITERATIONS,
            "tracepoint_hit_limit_per_backend": TRACEPOINT_HIT_LIMIT,
            "tracepoint_variable_names": list(_VARIABLE_NAMES),
            "tracepoint_line_no": spec.line_no,
            "gc_enabled_before": gc_was_enabled,
            "gc_enabled_during_measurement": False,
            "gc_restored_after": gc.isenabled() == gc_was_enabled,
            "percentile_method": "nearest_rank",
        },
        "calibration": {
            "iterations": no_hit_iterations,
            "attempts": calibration_attempts,
            "minimum_reached_by_both_cases": True,
        },
        "cases": {
            "active_no_hit": no_hit_case,
            "active_unscoped_target": unscoped_target_case,
            "active_hit": hit_case,
        },
        "performance_budget": performance_budget,
        "sink_count": {
            "calibration_expected": 0,
            "calibration_observed": 0,
            "warmup_expected": expected_warmup_sink_count,
            "warmup_observed": sink_count_after_warmups,
            "measured_no_hit_expected": 0,
            "measured_no_hit_observed": sink_count_after_no_hit - sink_count_after_warmups,
            "measured_unscoped_target_expected": 0,
            "measured_unscoped_target_observed": (
                sink_count_after_unscoped_target - sink_count_after_no_hit
            ),
            "measured_hit_expected": expected_measured_hit_sink_count,
            "measured_hit_observed": sink.count - sink_count_after_unscoped_target,
            "expected": expected_sink_count,
            "observed": sink.count,
            "exact": sink.count == expected_sink_count,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "error",
                    "error": "this benchmark is frozen and accepts no arguments",
                },
                sort_keys=True,
            )
        )
        return 2

    try:
        result = run_benchmark()
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "error",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                sort_keys=True,
            )
        )
        return 1

    result["status"] = "ok"
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
