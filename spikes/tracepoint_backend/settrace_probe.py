"""Negative async-overlap evidence for rejecting a ``sys.settrace`` fallback."""

from __future__ import annotations

import asyncio
import dis
import sys
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any


@dataclass(frozen=True, slots=True)
class SetTraceOverlapResult:
    """Observable ownership corruption from two overlapping async installers."""

    events: tuple[tuple[str, str, int], ...]
    final_tracer_leaked: bool
    request_a_seen_by_b: bool
    request_b_lost_after_a_cleanup: bool


async def _overlap_target(
    label: str,
    entered: asyncio.Event,
    release: asyncio.Event,
) -> str:
    before = f"{label}-before"
    entered.set()
    await release.wait()
    after = f"{label}-after"
    return f"{before}:{after}"


async def run_naive_settrace_overlap_probe() -> SetTraceOverlapResult:
    """Reproduce why per-coroutine ``settrace`` install/restore is not composable."""

    if sys.gettrace() is not None:
        raise RuntimeError("negative settrace probe requires an initially empty trace slot")
    entered_a = asyncio.Event()
    entered_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    events: list[tuple[str, str, int]] = []
    tracers: dict[str, Any] = {}

    def make_tracer(owner: str) -> Callable[[FrameType, str, object], Any]:
        def tracer(frame: FrameType, event: str, _argument: object) -> Any:
            if frame.f_code is _overlap_target.__code__ and event == "line":
                request_label = frame.f_locals.get("label", "<missing>")
                events.append((owner, str(request_label), frame.f_lineno))
            return tracer

        return tracer

    async def invoke(
        label: str,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> str:
        tracer = make_tracer(label)
        tracers[label] = tracer
        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            return await _overlap_target(label, entered, release)
        finally:
            sys.settrace(previous)

    task_a = asyncio.create_task(invoke("A", entered_a, release_a))
    await entered_a.wait()
    task_b = asyncio.create_task(invoke("B", entered_b, release_b))
    await entered_b.wait()
    release_a.set()
    await task_a
    release_b.set()
    await task_b
    leaked = sys.gettrace() is not None
    sys.settrace(None)
    executable_lines = sorted(
        {line for _, line in dis.findlinestarts(_overlap_target.__code__) if line is not None}
    )
    post_resume_lines = set(executable_lines[-2:])
    return SetTraceOverlapResult(
        events=tuple(events),
        final_tracer_leaked=leaked,
        request_a_seen_by_b=any(owner == "B" and request == "A" for owner, request, _ in events),
        request_b_lost_after_a_cleanup=not any(
            owner == "B" and request == "B" and line in post_resume_lines
            for owner, request, line in events
        ),
    )
