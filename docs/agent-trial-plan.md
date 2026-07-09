# FlowSight Pre-Implementation Trial Plan

Before building the full MVP, run small trial tasks through the complete agent workflow:

- 1 Implementer.
- 2 Adversarial Reviewers.
- 1 Fixer.
- 1 Verifier.
- 1 Quality Governor reviewing process drift.

The goal is not speed. The goal is to discover where agents overreach, skip checks, or misunderstand FlowSight boundaries, then write those lessons back into `AGENTS.md`, `docs/agent-facts.tsv`, or future contract docs.

The trials open explicit gates; merely creating a task card does not open a gate. Every listed task must be complete, every spike opener must decide `go` or `go-with-scope-reductions`, and every fact in the validator's canonical gate-fact set must have `command:` verification plus concrete existing test paths. A completed `no-go` records a valid research result but deliberately leaves that capability gate closed:

| Gate | Required completed tasks | Blocks |
|---|---|---|
| `phase0-sustained` | TRIAL-001, TRIAL-002, TRIAL-004 | Sustained Phase 0 implementation |
| `phase1-runtime-ingest` | TRIAL-003, TRIAL-004 | Persisting any real runtime telemetry |
| `phase4-tracepoint` | TRIAL-003, TRIAL-005 | Phase 4 tracepoint implementation |

## Trial 1: Phase 0 SDK Skeleton

Goal:

- Create the minimal Python package and a `FlowSight` class with `init_app(app)`.

Allowed scope:

- Package scaffold.
- Demo FastAPI app.
- Phase 0 tests.

Acceptance:

- `from flowsight import FlowSight` works.
- `FlowSight().init_app(app)` is callable.
- No OTel ingest, tracepoints, or UI behavior is implemented.

Reviewer attacks:

- Did the implementer invent Phase 1 collector logic?
- Did the package shape make future modules awkward?
- Is the public API consistent with the MVP doc?

## Trial 2: Phase 0 SQLite WAL Writer

Goal:

- Implement a single-writer SQLite queue for fake events.

Allowed scope:

- Storage module.
- Storage tests.
- Minimal schema or migration helper.

Acceptance:

- WAL mode is enabled and schema versioning exists.
- Multiple concurrent producers can queue 100 fake events with no loss or duplicate.
- Tests prove one writer identity, bounded queue behavior, close/enqueue races, and graceful shutdown flush without sleeps.
- SQLite failure injection is visible to callers/health state.

Reviewer attacks:

- Are writes actually serialized?
- Can queued events be lost on shutdown?
- Are storage errors visible rather than swallowed?

## Trial 3: Phase 0 Runtime Safe-Summary Spike

Goal:

- Implement the shared pre-queue safe-summary/redaction primitive for every future runtime payload, without OTel or tracepoint integration.

Allowed scope:

- Serializer/redactor module.
- Serializer tests.

Acceptance:

- Names/paths and token-like values are redacted for password, token, secret, cookie, session, auth/credential and API-key shapes.
- Depth, element count, per-value, per-summary and total payload limits are enforced deterministically.
- Exact built-in primitives/containers can be summarized; subclasses and unknown objects fall back to a type-only placeholder.
- Malicious `__repr__`, property, iterator, `__getattribute__` and custom serializer fixtures prove user code is not invoked.
- Raw input objects and unredacted originals never appear in the result or retained state.

Reviewer attacks:

- Does it accidentally store raw objects?
- Does it call properties or methods with side effects?
- Does it claim tracepoints are implemented?

## Trial 4: Phase 0 Sidecar / OTel Lifecycle Spike

Goal:

- Produce a go/no-go decision for the Python SDK + independent Python sidecar architecture before sustained runtime work.

Allowed scope:

- Isolated spike/harness code.
- Subprocess, reload, lifecycle and OTel coexistence tests.
- An ADR-style result that updates the design/facts after review.

Acceptance:

- Ordinary Uvicorn startup and one real `--reload` attach to the same sidecar PID/port/SQLite owner.
- Atomic launch lock prevents duplicate sidecars; stale state recovers; duplicate `init_app` is idempotent.
- A second live producer is explicitly rejected as unsupported multi-worker, while reload can wait for the previous lease to release.
- Default/explicit port conflicts, token authentication, bounded graceful flush and sidecar idle/explicit stop behavior are exercised.
- Existing and FlowSight-owned OTel providers are tested without replacement, duplicate instrumentation/root spans or self-capture recursion.
- Existing exporters see only explicitly authorized function child-span identity/timing/status code; exception auto-recording/status descriptions are disabled, and args/return/safe exception detail remain private FlowSight enrichment.
- Request context and bounded span association keep sync/async/thread-pool children attached to the correct local server span, including concurrent requests that share one upstream OTel trace ID.
- The spike chooses exactly one data path: `FlowSightSpanProcessor` → bounded sender → private sidecar protocol. It does not implement a production OTLP receiver.
- A finished fake request becomes queryable within one second.
- The lifecycle harness passes on macOS/Linux with CPython 3.12/3.13 in CI.
- The result is explicit: `go`, `go with listed scope reductions`, or `no-go`.

Reviewer attacks:

- Does the spike accidentally ship Phase 1 production ingest?
- Does it confuse reload reconnection with cross-process thread reuse?
- Can two SQLite writers, duplicate processors, orphan sidecars or unbounded shutdown still occur?

## Trial 5: Phase 4 Tracepoint Backend Go/No-Go Spike

Goal:

- Decide which tracepoint backend and function shapes can satisfy the v1 semantics without global tracing or async cross-request leakage.

Allowed scope:

- Isolated tracepoint backend experiments and tests on CPython 3.12/3.13.
- A support-matrix result; no production tracepoint UI/API.

Acceptance:

- Standard GIL-enabled CPython 3.12/3.13 sync, async/await, thread-pool, nested calls and concurrent requests are tested; free-threaded builds are outside v1.
- Tests prove before-line semantics, named-vars-only capture, and zero captures from unrelated functions/requests.
- Existing debugger/coverage tracer, exceptions, cancellation, enable/disable and shutdown cleanup are tested or explicitly rejected with evidence.
- `sys.monitoring` frame-locals feasibility and any scoped `sys.settrace` isolation are measured rather than assumed.
- Performance overhead is recorded against a frozen test harness.
- The result is exactly one of: supported backend, supported narrowed subset, or Phase 4 no-go.

Reviewer attacks:

- Does a thread-level tracer survive an `await` or overwrite another tracer?
- Does the proposed fallback depend on unstable frame discovery or global tracing?
- Does “experimental” hide a missing go/no-go decision?

## Trial Exit Criteria

For any gate to open:

- Every required task card has `status: complete`, all acceptance boxes checked, two reviewer results (or an explicit low-risk waiver), fixer/governor output, and exact verifier evidence.
- All dependencies are complete and the gate-specific validation command succeeds.
- Every discovered agent failure becomes a new rule, fact, queue rule, or test convention.
- A spike's go/no-go decision and scope reductions are written back to the MVP design and facts before dependent work starts.

In particular:

- Do not begin sustained Phase 0 until `phase0-sustained` opens.
- Do not persist real telemetry until `phase1-runtime-ingest` opens.
- Do not implement tracepoints until `phase4-tracepoint` opens; a Trial 5 no-go requires a product-scope revision, not an assumed fallback.
