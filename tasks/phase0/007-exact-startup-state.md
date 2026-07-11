# Task: Create One Exact Sidecar Startup State

## Task Metadata

```yaml
task_id: P0-007
release: v1
task_type: implementation
status: planned
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-002, P0-003, P0-006, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-007

## Phase

Phase 0

## Goal

Create one exact in-memory `SidecarState` for an already-bound sidecar listener,
without publishing state or starting runtime lifecycle behavior.

## Context

- P0-001 supplies the strict project state/store boundary and P0-002 supplies
  the bearer-safe private HTTP boundary.
- P0-003 returns the exact already-listening loopback socket whose atomically
  selected nonzero port must enter state; accepting a caller-provided port would
  weaken that bind-to-publication identity.
- P0-006 fixes the startup-channel identifier subset at exactly 32 lowercase
  hexadecimal characters. This factory is the single production generator for
  that identifier and for the per-startup capability token.
- The synchronous call assumes exclusive caller ownership of the exact store
  and listener for its duration. Concurrent mutation, close, or FD reuse by
  another thread is outside this primitive's contract.
- P0-004 and P0-005 are complete but are not direct dependencies because this
  slice neither owns a lock nor probes health.
- The fixed public shape is `create_startup_state(store, listener) ->
  SidecarState`, plus fixed `StartupStateError` and `StartupStateErrorCode`
  types for private ordinary generation failures.
- Source of truth: MVP design section 4.2 and Phase 0, plus the TRIAL-004
  promotion requirements.

## Related Fact IDs

- FS-007
- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_state.py`
- `tests/sidecar/test_startup_state.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_state.py`
- `tests/sidecar/test_startup_state.py`

## Forbidden

- Do not create, load, publish, replace, remove, or poll a state file, and do
  not call any `StateStore` filesystem method.
- Do not accept caller-provided startup ID, capability token, PID, port,
  timestamp, database path, host, or protocol/schema version.
- Do not bind, listen on, mutate, detach, consume, duplicate, inherit, or close
  the caller-owned listener.
- Do not acquire/adopt/release an owner lock, send a startup-channel message,
  call the health probe, or make an HTTP request.
- Do not spawn, poll, wait, signal, terminate, kill, or reap a process.
- Do not start Uvicorn/ASGI, a thread, queue, SQLite handle, sender, SDK
  lifecycle, retry loop, or sleep.
- Do not generate a URL, open a browser, log generated values, retain a token
  cache, import a spike, or create a launcher/runtime/config abstraction.

## Acceptance Criteria

- [ ] `create_startup_state()` accepts only an exact `StateStore` and exact
  built-in `socket.socket` structurally satisfying the observable P0-003
  postcondition. Wrong top-level object types raise fixed `TypeError` before
  any store field access, socket inspection, entropy, PID, or clock call.
- [ ] The listener is revalidated as an open, non-inheritable, listening IPv4
  TCP socket bound exactly to `127.0.0.1` and a nonzero port. Its port is derived
  from that socket. The factory calls no bind/listen/set/dup/detach/close
  operation; for a listener valid on entry, the same object and FD remain open
  with address, listening state, inheritable flag, and timeout/blocking state
  unchanged after success or failure.
- [ ] Store validation snapshots exact built-in `project_id` plus exact
  platform-`Path` `runtime_root`, `runtime_dir`, and `database_path` values. It
  validates project text, absolute paths, and purely recomputes
  `project-<sha256(project_id)[:32]>/events.sqlite3` for exact lexical equality;
  it never reconstructs `StateStore`, resolves/stats/tests a path, or calls a
  store method. The returned project ID and database path come only from that
  snapshot; this does not claim to re-prove filesystem trust.
- [ ] Each successful call invokes exactly one `secrets.token_hex(16)`, one
  `secrets.token_urlsafe(32)`, one `os.getpid()`, and one `time.time_ns()`.
- [ ] The startup ID is an exact built-in 32-character lowercase hexadecimal
  string compatible with `StartupReady`; the capability token is an exact
  built-in 43-character URL-safe bearer string carrying the fixed 32-byte
  generator request.
- [ ] PID and timestamp are exact positive built-in integers within the
  `SidecarState` bounds. Host and protocol/state versions remain their exact v1
  defaults, and the result is an exact freshly revalidated `SidecarState`.
- [ ] Raising, wrong-exact-type, malformed, or out-of-range monkeypatched
  generator, PID, clock, store-field, listener-inspection, or final-schema
  results fail closed and never return a partial state. Entropy quality is
  trusted to the standard-library `secrets` implementation; evidence proves
  exact calls and grammar, not the entropy of a monkeypatched valid constant.
- [ ] After exact top-level types are accepted, every structural store/listener
  rejection and ordinary inspection, entropy, PID, clock, generated-value, or
  final-schema failure exposes only a fixed private
  `STARTUP_STATE_GENERATION_FAILED` error with no generated value, token, path,
  PID, errno, OS text, cause, or context. `KeyboardInterrupt` and `SystemExit`
  preserve identity.
- [ ] The factory accepts and invokes no caller-provided callback and never
  logs, caches, prints, or includes the capability token in repr or errors; no
  unknown caller object method is invoked while validating exact inputs.
- [ ] Real listener/store integration proves
  `StartupReady(state.startup_id, state.pid, state.port)` constructs,
  `SidecarState.from_wire(state.to_wire()) == state`, and
  `create_sidecar_app(state)` succeeds, while no `StateStore` method is called
  and the project runtime/data paths remain absent.
- [ ] Deterministic fault matrices cover every input/generation stage and prove
  zero hidden state I/O, listener mutation/cleanup, process orchestration,
  retry, or sleep behavior.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_startup_state.py
make test-phase0
make check
```

Expected result:

```text
exact in-memory startup-state tests and all repository checks pass
```

## Risks

- Accepting a port separately from the retained listener can publish an address
  the sidecar does not own.
- A weak startup-ID grammar can make READY and published state incomparable.
- An unvalidated token can fail the private HTTP boundary or leak through an
  exception, repr, or log.
- Hidden state publication in this factory would make later startup ordering
  and rollback impossible to review independently.

## Reviewer Focus

- Can state/READY identity diverge from the exact listener, process, store, or
  generated startup ID?
- Can a token, path, PID, errno, or OS error text enter a public exception,
  repr, output, log, retained callback, or exception chain?
- Can invalid input consume entropy or can any success/failure mutate or close
  the listener or touch the filesystem?
- Did this remain one in-memory factory with no hidden publication, startup
  channel, lock, health, process, runtime, SDK, or retry behavior?

## Role Outputs

Implementer:
- TBD

Adversarial Reviewer:
- Reviewer 1: TBD
- Reviewer 2: TBD

Fixer:
- TBD

Quality Governor:
- TBD

## Verifier Evidence

- Command: pending
- Result: pending
- Notes: proves in-memory startup identity generation only, not publication or
  runtime startup

## Failure Queue Items

- none
