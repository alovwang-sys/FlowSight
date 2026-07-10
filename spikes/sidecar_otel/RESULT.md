# TRIAL-004 Sidecar / OTel Lifecycle Result

## Decision Status

Final decision: **pending the required GitHub Actions matrix**.

Local candidate decision: **go**.

Scope reductions: **none proposed**. The code in this directory is executable
architecture evidence, not production Phase 0/1 code.

## What the Spike Proves

- One project-scoped sidecar can exclusively own its loopback API and SQLite
  writer while ordinary and reloading Uvicorn workers reconnect to it.
- Atomic file locking, authenticated state validation, stale recovery, port
  conflict handling, producer leases, abandoned-election recovery, and explicit
  multi-worker rejection have bounded, observable outcomes.
- Producer lease renewal requires an exact `(producer_id, lease_id)` match and
  extends the same lease. Retrying after a lost response is idempotent with
  respect to lease identity and ownership: renewal never allocates, replaces,
  or reacquires a lease. Expired or mismatched pairs fail with
  `INVALID_LEASE`; reacquisition remains a `hello` operation subject to the
  single-producer rule.
- The private path is viable as
  `FlowSightSpanProcessor -> bounded sender -> authenticated sidecar -> SQLite`.
  ACK means the receipt transaction committed; retrying the same batch remains
  idempotent.
- A user-owned SDK `TracerProvider` and exporter remain installed and continue
  to work. FlowSight registers one reusable processor, makes it no-op after
  deactivation, reactivates it with a new delivery target, and never shuts down
  the user's provider.
- FastAPI instrumentation remains idempotent, creates no competing root span,
  and preserves request ownership for sync, async, and thread-pool children. A
  bounded transition barrier drains callbacks that began before FastAPI gating,
  then positive middleware admission uses an immutable app-session token; a
  retired or unbound wrapper cannot enter a replacement session. Only one app
  is active in a lifecycle session, while a completed shutdown can bind a
  different app.
  Local request identity includes the local server span, so two local requests
  sharing one upstream OTel trace do not merge failure state.
- Function spans disable automatic exception recording and status descriptions.
  Existing exporters receive only span identity/timing/status code; safe
  args/return/exception summaries stay on the private channel.
- Association/orphan state is bounded. Exactly attributable capacity loss and
  sender/storage failure mark the affected request incomplete. If the bounded
  exact loss-attribution set itself overflows, the processor sets sticky
  `completion_uncertain` health instead of guessing a request; downstream code
  must then refuse to finalize any trace as complete for that session.
- Provisional pre-middleware roots, descendants, and nested server boundaries
  are also bounded and resolved iteratively. Deep parent chains cannot exhaust
  Python recursion, and a missing child is attributed to its nearest local
  server request rather than another root in the same OTel trace.
- Final lifecycle publication is retryable across `BaseException`: telemetry
  drain, sidecar flush, heartbeat stop/archive, exact lease release, registry
  release, and visible loss reporting complete before the active or pending
  cleanup marker is atomically cleared.

## Local Evidence

The final local candidate working tree passed on macOS on 2026-07-10:

- CPython 3.13.5: `89 passed in 58.52s` in the lifecycle harness.
- Isolated CPython 3.12.11: `89 passed in 62.30s` in the same harness.
- Independent adversarial review reran the complete 89-test harness, focused
  20-test telemetry/admission and 48-test lifecycle/admission slices, and a
  custom 1,050-level provisional-chain probe; no P0/P1 remained.
- `make check` passed agent-system validation, Ruff format/lint, strict mypy,
  and `186 passed in 48.30s`. Its emitted partial-scaffold warning is retained:
  this command is repository evidence, not Phase 0 product acceptance.

The harness includes separate ordinary Uvicorn and real `--reload` process
tests, plus a finished server-span test that reaches SQLite through the entire
private path and becomes queryable in under one second.

## Review Findings Incorporated

- Replaced a binding weak-value cache that could register a second processor
  after handle garbage collection with a weak-provider-keyed, provider-free
  registration record. A GC regression proves processor reuse.
- Changed loser startup behavior to re-contend for the election lock when a
  prior lock holder exits before publishing state. A deterministic regression
  proves the waiter becomes the new owner.
- Added ordinary non-reload Uvicorn, active association-capacity pressure, new
  delivery-target reactivation, and the real processor-to-SQLite integration
  path after acceptance-evidence review.
- Made process-local lifecycle ownership shared and generation-aware across SDK
  instances, with one active FastAPI app per provider/project session,
  session-token isolation for retired app wrappers, per-session producer IDs,
  exact-lease heartbeat renewal, and phase-aware shutdown retry.
  Failed or ambiguous hello/goodbye, permanent flush loss, provider-first
  shutdown, expired leases, and failed initialization all have bounded cleanup
  regressions.
- Removed request attribution guesses: explicit missing parents wait by exact
  parent chain, active associations become incomplete at shutdown, and exact
  loss-attribution overflow remains sticky global uncertainty rather than a
  probabilistic request assignment.
- Replaced generic SERVER-root acceptance with positive per-app/session
  admission. Provisional state preserves exact root and nearest nested SERVER
  boundaries, has independent capacity/TTL/drop health, resolves deep chains
  iteratively, and converts unattributable loss into visible uncertainty.
- Added a bounded callback-drain barrier for the one-time ungated-to-FastAPI
  transition. A materialized pre-gate emission completes before the transition
  linearizes; timeout restores callback admission, and a single asynchronous
  interruption completes the mutation fail-closed before propagating.
- Made ambiguous provider registration fail closed, released registry strong
  handles only through retryable lifecycle finalization, and made active plus
  failed-initialization cleanup resilient to interruption before or after that
  release.
- Capped coordinator waits to the platform thread maximum and recovered exact
  lease ownership after lost final-renew responses without creating a new
  lease.

## Promotion Requirements

These are implementation requirements for later phase tasks, not reductions to
the approved v1 scope:

- Promote the architecture out of `spikes/` through phase-bounded production
  tasks; do not import or ship this harness as the SDK runtime.
- Define strict per-event private payload schemas in Phase 1. In particular,
  production `trace.drop_notice` must require the affected request identity and
  explicit producer-sequence range rather than relying only on the generic safe
  envelope.
- Keep the public `FlowSight.init_app` orchestration, sender ownership, and
  shutdown wiring in sustained Phase 0/1 work. This spike proves the underlying
  idempotent process, instrumentation, processor, and delivery lifecycles.
- Require the user-owned `TracerProvider` to still be live when FlowSight first
  binds it. Public OTel SDK APIs expose no reliable terminal-provider state, so
  a provider already shut down before first bind is not distinguishable; the
  supported provider-first shutdown case begins only after a successful
  FlowSight bind.
- Avoid holding a process-global registry lock across an overridable provider
  registration call in production. The spike's v1 single-provider path is
  correct, but a stuck custom provider could otherwise delay unrelated registry
  release work beyond a lifecycle deadline.
- Retain OTel suppression around internal transport and add an integration test
  for whichever concrete HTTP client instrumentation Phase 1 selects.
- Preserve `POST /internal/v1/renew` as an exact-match, response-loss-safe
  extension of the current lease. Renewal must never become implicit lease
  reacquisition; after `INVALID_LEASE`, acquisition returns to `hello`.
- Keep Windows, free-threaded CPython, multi-worker aggregation, generic OTLP
  ingest, and cross-service tracing outside v1 as already documented.

## Final Evidence Still Required

Before changing the decision to `go` and completing TRIAL-004:

1. Commit the reviewed implementation and record its immutable SHA.
2. Pass GitHub Actions on macOS and Linux with CPython 3.12 and 3.13 for that
   SHA; record the run URL/ID.
3. Rerun `make check` for the immutable SHA and promote the planned TRIAL-004
   fact evidence to command-backed evidence only after the matrix passes.
4. Complete the separate tooling correction that decouples the validator test
   from the real `phase0-sustained` gate before opening Phase 0/1 gates.
