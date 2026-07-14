# TRIAL-005 Tracepoint Backend Result

## Decision Status

Final decision: **go with the five approved scope reductions**.

Scope approval: **the user explicitly approved all five reductions on
2026-07-11**.

Selected backend: **`sys.monitoring` per-code `LINE` events only**. There is no
`sys.settrace` fallback and no interpreter-wide event mask.

The code in this directory is executable Phase 4 architecture evidence. It is
not the production tracepoint service, API, or UI.

## Approved v1 Support Matrix

The approved subset supports the following only on standard GIL-enabled CPython 3.12
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

PERF-003 supersedes only the schema-v3 no-hit aggregation. The current schema-v4
harness identity is
`sha256:3bcbcc7d7f6ae1b14ef0672994e00f45ad42b2c73827e451a98fd4414380ec9d`.
The digest covers the complete benchmark module, backend module, and shared
safe-summary module. Its component digests are benchmark
`sha256:94197663c071dde43b877e0815fb4d579977ec8328ef8e1a56009b7afa1347b1`,
backend `sha256:ee95f9263fe94984af8efcb193b66a5c08ed3b3d22995ba7d576fb3cf2075446`,
and safe-summary
`sha256:d62bca6b3c1eaa8f7b22d7734c8ac3b656078517ded12e0315ded45b693a869a`.
Only the benchmark component changed from schema v3.

The harness uses five warmups and 21 effective samples. Each no-hit sample is a
same-seed symmetric four-leg ABBA or BAAB block built from one AB and one BA
pair. Its decision ratio is `(active_1 + active_2) / (baseline_1 + baseline_2)`;
all four raw legs remain in the report. This cancels reciprocal position or CPU
frequency bias inside each sample without cancelling a common active-side CPU
factor. Configured-but-unscoped and captured-hit cases retain their 21
alternating two-leg pairs. Nearest-rank p95, at least 50 ms current-thread CPU
calibration selects the shared iteration count used by every no-hit leg,
32,768 calls per configured-but-unscoped sample,
and 1,024 calls per captured-hit sample remain unchanged. GC is disabled
uniformly during measurement and restored afterward. Backend startup and
cleanup remain outside each timed leg.

Every pair records two explicitly named clocks. `thread_time_ns` measures CPU
consumed by the current benchmark thread and is the only calibration and budget
input. `perf_counter_ns` remains a monotonic wall diagnostic that includes
scheduler wait and is never read by a budget check. Clock implementation,
resolution, monotonicity, and adjustability are emitted in report metadata.

Historical schema-v3 serial local macOS runs produced:

| Runtime and case | Median thread-CPU | p95 thread-CPU | Median wall diagnostic | p95 wall diagnostic |
| --- | ---: | ---: | ---: | ---: |
| CPython 3.13.5, unconfigured code | 0.993× | 1.046× | 0.995× | 1.040× |
| CPython 3.13.5, configured code outside a request | 4,412 ns/call | 4,594 ns/call | 4,448 ns/call | 4,670 ns/call |
| CPython 3.13.5, captured hit | 54,554 ns/call | 60,632 ns/call | 55,225 ns/call | 61,505 ns/call |
| CPython 3.12.11, unconfigured code | 0.999× | 1.017× | 0.999× | 1.018× |
| CPython 3.12.11, configured code outside a request | 5,076 ns/call | 5,230 ns/call | 5,118 ns/call | 5,278 ns/call |
| CPython 3.12.11, captured hit | 57,392 ns/call | 59,096 ns/call | 57,917 ns/call | 59,642 ns/call |

The six accepted numeric maxima are unchanged and now explicitly apply to
thread CPU:

| Case | Median maximum | p95 maximum |
| --- | ---: | ---: |
| Unconfigured code | 1.15× paired thread CPU | 1.75× paired thread CPU |
| Configured code outside a request | 15,000 thread-CPU ns/call | 25,000 thread-CPU ns/call |
| Captured hit | 200,000 thread-CPU ns/call | 300,000 thread-CPU ns/call |

Both historical schema-v3 local runtimes passed all six checks. The schema-v4
exact sink invariant remains:
all 26,624 expected scoped hits are observed, while calibration,
unconfigured-code, and configured-but-unscoped cases emit zero snapshots.
These limits are synchronous callback/hit CPU regression guards, not the Phase
5 10 ms wall-clock request SLA, not a production queue/transport budget, and
not permission to multiply the 300 µs ceiling by an arbitrary hit count.

The schema-v2 digest
`sha256:3993fd75a45b1e14be3e04d56534928cadc928a92dce5af6473398e5c14c30e9`
and GitHub Actions run `29114712575` remain historical TRIAL-005 evidence, but
they prove the old absolute-wall timing predicate and cannot prove schema v3 or
v4.
Three fresh-process serial v2 reproductions at host load 26--36/12 logical CPUs
kept both paired no-hit ratios within budget while configured-unscoped median
rose to 19.4--21.8 µs and captured-hit median to 271--385 µs. That isolated
scheduler-sensitive wall time as the false-failure source without changing the
tracepoint backend or thresholds. At candidate submission the schema v3
four-job immutable matrix was pending. It subsequently passed in run
`29135246585`; see PERF-001 Schema v3 Final Evidence below. Schema-v3 digest
`sha256:e3273869041f3b9bc8d4d65977a04e64f87e0c23268aa88586c7562d8e18e12e`
and its successful matrix remain historical evidence, but the later repeated
FSQ-0001 majority-order failures mean they cannot prove schema v4.

Benchmark suites and evidence runs must still execute sequentially. Any budget,
decision clock, sample construction, workload, backend, or serializer change
produces a new digest and requires separate review. Thread CPU excludes
descheduling but still includes real execution cost; symmetric blocks address
only reciprocal leg-order bias. They do not expand support to other threads,
Windows, free-threaded builds, or additional Python versions.

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

## Historical TRIAL-005 Schema v2 Local Evidence

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

The historical schema v2 reviewed implementation is committed at
`9f195d33ba77f11bde103b91ba94fe5872c73591`. An independent verifier confirmed
both focused runtimes and immutable `make check` (`217 passed`). Final matrix
evidence for that schema comes from GitHub Actions run `29114712575`, which passed
Ubuntu/macOS × CPython 3.12/3.13 on
`d5893e3d09ccb9a9a9c3399fa649129664d38c3f`.

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

## Historical TRIAL-005 Schema v2 Final Matrix Evidence

- Run: `https://github.com/alovwang-sys/FlowSight/actions/runs/29114712575`
- Ubuntu 3.12: success, job `86435155825`
- macOS 3.12: success, job `86435155866`
- Ubuntu 3.13: success, job `86435155872`
- macOS 3.13: success, job `86435155880`

## PERF-001 Schema v3 Final Evidence

- Digest:
  `sha256:e3273869041f3b9bc8d4d65977a04e64f87e0c23268aa88586c7562d8e18e12e`
- Candidate commit: `41d0c3b2b3ad06b2bf5697434fd9a743e01d7c54`.
- CPython 3.13.5 focused harness: `36 passed`.
- Isolated CPython 3.12.11 focused harness: `36 passed`.
- Full local `make check`: `222 passed`, with agent-system, format, lint, and
  strict mypy checks passing first.
- Two independent code/test reviews found no remaining P0/P1/P2 after the
  baseline/active wall-routing, two-sided CPU calibration, exact six-check
  mapping, real failure-message, and legacy-field regressions were added.
- Immutable schema v3 CI: GitHub Actions run `29135246585` completed successfully
  on the candidate commit. Ubuntu 3.12 job `86498180795`, macOS 3.12 job
  `86498180788`, Ubuntu 3.13 job `86498180825`, and macOS 3.13 job
  `86498180800` all passed. The historical run above does not satisfy or
  substitute for this v3 evidence.

## PERF-003 Schema v4 Candidate Evidence

- Digest:
  `sha256:3bcbcc7d7f6ae1b14ef0672994e00f45ad42b2c73827e451a98fd4414380ec9d`.
- Component digests: benchmark
  `sha256:94197663c071dde43b877e0815fb4d579977ec8328ef8e1a56009b7afa1347b1`,
  backend `sha256:ee95f9263fe94984af8efcb193b66a5c08ed3b3d22995ba7d576fb3cf2075446`,
  and safe-summary
  `sha256:d62bca6b3c1eaa8f7b22d7734c8ac3b656078517ded12e0315ded45b693a869a`.
- The same schema-v3 FSQ-0001 signature occurred at `1.3710400499700648`
  in run `29166073709` and `1.2023160302028435` in PR run `29297091756`.
  The latter SHA simultaneously passed the full push matrix in run
  `29297089621`, isolating the 11 AB/10 BA majority-order statistic.
- Deterministic tests prove reciprocal AB/BA bias makes the old 21-pair median
  report `1.2` with no real overhead, while each schema-v4 four-leg block
  reports `1.0`. A common active-side factor of `1.16` survives the same
  transformation and fails the unchanged `1.15` median maximum.
- CPython 3.13.5 and isolated CPython 3.12.11 each passed all 41 focused tests
  in 14.05 and 15.51 seconds respectively. `make test-trial005` passed 41 tests
  in 14.66 seconds; `make check` passed 2771 tests in 120.22 seconds;
  `make gate-phase4` passed 2771 tests in 119.10
  seconds plus the `phase4-tracepoint` gate.
- Candidate push/PR matrix evidence is pending. FSQ-0001 remains `fixed`, not
  `verified`, until that matrix passes.
