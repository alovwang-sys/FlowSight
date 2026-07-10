# TRIAL-005 Tracepoint Backend Result

## Decision Status

Final decision: **pending the immutable GitHub Actions matrix**.

Local candidate decision: **go with scope reductions**.

Scope approval: **the user explicitly approved all five reductions on
2026-07-11**.

Candidate backend: **`sys.monitoring` per-code `LINE` events only**. There is no
`sys.settrace` fallback and no interpreter-wide event mask.

The code in this directory is executable Phase 4 architecture evidence. It is
not the production tracepoint service, API, or UI.

## Local Candidate Support Matrix

The candidate supports the following only on standard GIL-enabled CPython 3.12
and 3.13, after a startup self-probe succeeds:

| Shape or environment | Candidate result | Evidence boundary |
| --- | --- | --- |
| Exact Python synchronous function | Supported | Before-line capture, nested calls, recursion, exceptions, hit limits |
| Exact Python coroutine function | Supported | Two interleaved requests, cancellation, inherited late-task rejection |
| Bound Python instance/class method and static function | Supported | Exact code/line matching; no descriptor or wrapper introspection |
| Context-propagated thread-pool call | Supported | Caller explicitly runs the function in `contextvars.copy_context()` |
| Raw/unpropagated executor work | Rejected as request work | It executes normally but emits no snapshot |
| Work that outlives its request, even with copied context | Rejected as request work | Active request-token registry invalidates the inherited context |
| Synthetic existing `sys.settrace` callback | Mechanism coexists | FlowSight never reads, replaces, restores, or clears that tracer slot; real debugpy/coverage integrations remain unverified and unsupported |
| Synthetic second `sys.monitoring` tool on a different ID | Mechanism coexists | FlowSight claims only generic ID 3 or 4 and never steals an occupied ID |
| Lambda, comprehension, generator, async generator, one-line/own-definition-line target | Unsupported | Rejected when the tracepoint specification is created; nested pure-definition lines require the later source-aware Phase 4 validator |
| Non-CPython, versions outside 3.12/3.13, free-threaded build | Unsupported | Backend activation fails closed |
| No safe generic ID remains because each candidate is occupied, retains detectable state, or fails its probe | Unsupported for that process | Activation fails without altering the foreign owner; the API cannot enumerate stale local masks on unrelated code |

Tracepoint configuration accepts only bounded identifier names that belong to
the target code. At a hit, the callback rechecks exact code and line, looks up
only those names, immediately passes the selected built-in mapping through the
shared safe-summary boundary, and retains neither a frame nor raw runtime
values. Assignment targets that do not yet exist are reported as missing,
which proves the documented “before this line executes” semantics.

The callback obtains the executing frame with `sys._getframe(1)`. That is a
CPython-specific contract rather than part of the documented
[`sys.monitoring` callback signature](https://docs.python.org/3.13/library/sys.monitoring.html),
so every activation performs a real local-line self-probe. Audit denial, a
wrong frame, a foreign callback, or unexpected event state fails closed or,
after startup, becomes visible fail-open health instead of changing the
business function result.

## Why `sys.settrace` Is Rejected

The frozen negative probe deliberately installs one thread-level tracer per
overlapping coroutine. On both CPython 3.12.11 and 3.13.5 it deterministically
shows all three failures: request A is observed by request B's tracer, request
B loses events when A restores its earlier slot, and a tracer remains installed
after both requests finish. This is consistent with the documented
thread-specific nature of [`sys.settrace`](https://docs.python.org/3/library/sys.html#sys.settrace)
and with `contextvars` task isolation not creating a separate trace-function
slot. Consequently, v1 must not silently fall back to `sys.settrace`.

## Frozen Benchmark

Harness identity:
`sha256:3993fd75a45b1e14be3e04d56534928cadc928a92dce5af6473398e5c14c30e9`.
The digest covers the complete benchmark module, backend module, and shared
safe-summary module, including orchestration, aggregation, constants, and
budgets. The harness uses five warmups, 21 alternating baseline/active pairs,
a nearest-rank p95, at least 50 ms calibration for the no-hit workload, 32,768
calls per configured-but-unscoped sample, and 1,024 calls per captured-hit
sample. GC is disabled uniformly during measurement and restored afterward.
The timer excludes backend startup and cleanup so the cases measure steady
execution cost.

One local macOS run produced:

| Runtime and case | Median active ns/call | Median active/baseline | p95 active ns/call | p95 active/baseline |
| --- | ---: | ---: | ---: | ---: |
| CPython 3.13.5, unconfigured code | — | 1.005 | — | 1.018 |
| CPython 3.13.5, configured code outside a request | 6,290 | diagnostic only | 6,448 | diagnostic only |
| CPython 3.13.5, captured hit | 74,604 | diagnostic only | 78,069 | diagnostic only |
| CPython 3.12.11, unconfigured code | — | 1.000 | — | 1.066 |
| CPython 3.12.11, configured code outside a request | 6,812 | diagnostic only | 7,908 | diagnostic only |
| CPython 3.12.11, captured hit | 76,855 | diagnostic only | 81,425 | diagnostic only |

Ratios compare different small workload shapes and are diagnostic, not a
product SLA. The exact sink invariant is stronger evidence of what each case
measures: all 26,624 expected scoped hits were observed, while calibration,
unconfigured-code, and configured-but-unscoped cases emitted zero snapshots.

The same harness enforces this frozen Phase 4 spike regression budget in every
in-scope CI job:

| Case | Median maximum | p95 maximum |
| --- | ---: | ---: |
| Unconfigured code | 1.15× paired active/baseline | 1.75× paired active/baseline |
| Configured code outside a request | 15,000 ns/call | 25,000 ns/call |
| Captured hit | 200,000 ns/call | 300,000 ns/call |

Both local runtimes pass all six checks. These limits are per callback/hit
regression guards, not the Phase 5 10 ms request SLA, not a production
queue/transport budget, and not permission to multiply the 300 µs ceiling by
an arbitrary hit count. Their absolute limits become accepted only if the
immutable four-job CI matrix passes; any relaxation changes the digest and
requires separate review.

Benchmark suites and evidence runs must execute sequentially on the local host;
running two timing harnesses concurrently invalidates their absolute metrics.
The four CI matrix jobs are isolated runners, so each enforces the budget
independently.

## Lifecycle and Isolation Contract

- Activation sets only per-code `LINE` masks; the global mask remains zero.
- Request admission requires both the current `ContextVar` value and a live,
  backend-owned request token. Context propagation alone cannot resurrect an
  ended request.
- Ordinary capture/sink/audit failures are visible and fail open. Process-control
  `BaseException` values continue to propagate.
- Shutdown first rejects new scopes, disables every local mask, drains in-flight
  callbacks with a bounded timeout, unregisters the callback, then frees the
  tool ID. A drain timeout is retryable.
- Snapshot publication and request-scope retirement share a lock, preventing a
  late callback from publishing after request exit.

## Approved Scope Reductions

The user approved these as the candidate v1 contract on 2026-07-11:

1. Use `sys.monitoring` only; provide no `sys.settrace` fallback.
2. Support exact non-generator Python sync/coroutine functions and bound Python
   methods only. Reject lambdas/comprehensions, generators/async generators,
   one-line/own-definition-line targets, wrappers that cannot be resolved to an
   exact function, and invalid direct specifications at configuration time.
3. Attribute thread-pool work only when the request context is explicitly
   propagated. Raw executor work and any work that resumes after request exit
   remain unobserved by tracepoints.
4. Require standard GIL-enabled CPython 3.12/3.13, one free generic monitoring
   tool ID (3 or 4), and a successful direct-frame self-probe. Otherwise expose
   tracepoints as unsupported for that process.
5. Treat real debugpy/coverage integration as unsupported until separately
   validated. The spike proves only coexistence with a synthetic existing
   `sys.settrace` callback and a synthetic second monitoring tool.

These reductions do not affect ordinary OTel spans or explicit
`@flowsight.trace` spans for unsupported tracepoint shapes.

## Local Evidence

- CPython 3.13.5: `31 passed` in the focused harness.
- Isolated CPython 3.12.11: the same `31 passed`.
- Ruff format/lint and strict mypy pass for the spike.
- The harness covers before-line/missing-name semantics, redaction and hostile
  values, unrelated requests, sync nesting/recursion, async interleaving,
  cancellation, thread-context propagation, late work, existing tracers,
  monitoring-ID conflicts/coexistence, activation/cleanup/restart, callback
  errors, backpressure, bounded drain, audit denial, and the negative fallback.

## Review Findings Incorporated

- Centralized every `TracepointSpec` invariant in `__post_init__`, so its public
  dataclass constructor cannot bypass supported-shape, identifier, line,
  function metadata, or hit-limit checks.
- Added a callback-entry admission/drain barrier and serialized the full
  start/stop lifecycle. Successful shutdown now waits for callbacks that began
  before request admission, and concurrent stop calls are idempotent.
- Preflights detectable masks and callbacks before the frame probe and preserves
  foreign retained global/local event state on rejection instead of clearing it.
- Expanded the digest to the complete harness/backend/serializer, lengthened the
  unscoped-target samples, and made the six numeric budget checks executable.
- Added closure free-variable/cell-variable evidence and narrowed coexistence
  claims to the synthetic mechanisms actually tested.

The reviewed implementation is committed at
`9f195d33ba77f11bde103b91ba94fe5872c73591`. An independent verifier confirmed
both focused runtimes and immutable `make check` (`217 passed`). The task still
requires explicit scope approval and the repository's macOS/Linux × CPython
3.12/3.13 GitHub Actions matrix before completion or gate opening.

## Promotion Requirements

- Promote this architecture through a phase-bounded production task; do not
  import the spike backend as the shipped SDK implementation.
- Preserve creation-time support checks, startup self-probe, positive request
  admission, per-code-only events, safe-summary-before-queue, bounded hit/drop
  accounting, and exact cleanup order.
- Integrate request identity from the Phase 0/1 lifecycle rather than creating
  a second request-owner mechanism.
- Keep unsupported shapes visible in status/API responses; never silently miss
  a requested tracepoint or claim a fallback.
- Use the Phase 4 source/AST validator to reject nested pure `def`/`class`
  definition lines; the backend's code-object-only validator can reject only
  the target's own definition line and one-line functions.
- Re-run the frozen harness if the callback frame contract, serializer, event
  filter, or supported Python matrix changes; a changed digest is a different
  benchmark.

## Final Evidence Still Required

1. Pass GitHub Actions on macOS and Linux with CPython 3.12 and 3.13 for that
   SHA; record the run URL/ID.
2. Promote command-backed facts, record the final decision, and
   complete the task/gate evidence only after the matrix passes.
