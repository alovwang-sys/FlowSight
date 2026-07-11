# Task: Discover One Existing Sidecar Startup

## Task Metadata

```yaml
task_id: P0-010
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-005, P0-008, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-010

## Phase

Phase 0

## Goal

Return one exact current `SidecarState` only when two equal published-state
observations bracket one authenticated health result inside one read-only
success-admission deadline.

## Context

- P0-009 admits one startup-channel outcome after a future launcher starts a
  child. An ordinary or Uvicorn reload worker attaching to an already-published
  incumbent has no child reader, so it still needs one neutral production
  primitive for discovering that sidecar.
- P0-001 provides the strict project-scoped state boundary. Its canonical
  `StateStore.load()` returns either a newly decoded exact state or `None` for
  absent, malformed, unstable, inaccessible, or cleanup-ambiguous input.
- P0-005 provides one bounded authenticated health probe for one exact state.
  P0-008 established the reviewed `load -> probe -> load` observation-window
  semantics for a child READY hint. This task applies that same two-read success
  rule directly to existing-startup discovery without manufacturing a
  `StartupReady`, consuming a channel, or changing P0-008.
- The fixed public shape is `discover_existing_startup(store, timeout=0.5) ->
  SidecarState | None`.
- One shared monotonic success-admission deadline begins before the first load.
  It rejects a late success but is not a hard interruptible bound over either
  synchronous state load.
- Success proves only one observation window: two fully equal trusted state
  snapshots bracket one authenticated health result. It does not prove inode
  continuity, exclude an ABA replacement, guarantee health after return, prove
  owner-lock continuity, or grant election, stale cleanup, or launch authority.
- `None` means only "this observation window did not prove an existing healthy
  startup." A later election task must separately acquire the canonical owner
  lock and repeat its required checks before producing any winner authority.
- A successful result is health/discovery evidence only. This API does not
  accept `ui_port` or prove compatibility with an explicitly requested port;
  later SDK attachment/election policy must check any requested-port mismatch
  before using the state.
- Source of truth: MVP design sections 4.2, 7.4, 11 Phase 0, and TRIAL-004
  promotion requirements.

## Related Fact IDs

- FS-007
- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_discovery.py`
- `tests/sidecar/test_startup_discovery.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_discovery.py`
- `tests/sidecar/test_startup_discovery.py`

## Forbidden

The following restrictions apply to production. Focused tests may publish exact
temporary state and use one bounded `127.0.0.1:0` health-server thread with
observable readiness and deterministic whole-lifetime cleanup. They may not
start a subprocess.

- Do not acquire, adopt, inspect, transfer, or close an `OwnerLock`; elect,
  re-contend, wait for contention, or produce a winner/launch-authority result.
- Do not publish, remove, repair, or rename state; recover stale state; inspect
  PID liveness; unlink a persistent lock file; or treat `None` as permission for
  any mutation.
- Do not manufacture or accept a `StartupReady`, open or consume a startup
  channel, receive a child outcome, or change P0-008/P0-009 behavior.
- Do not accept, start, poll, signal, terminate, kill, wait for, or reap a
  process; build a command/environment; call `Popen`; or add inherited
  descriptors, sessions, or reapers.
- Do not bind or mutate a production listener, start Uvicorn/ASGI, select a
  requested port, open SQLite, create a writer/queue/sender/lease, or add SDK,
  OTel, reload, stop, idle-shutdown, UI, browser, or trace behavior.
- Do not claim that a discovered state is compatible with an explicit
  `ui_port`/requested port; this function accepts no port-policy input.
- Do not add polling, retry, backoff, sleep, loops, callbacks, background work,
  caches, or mutable module state.
- Apart from the exact successful `SidecarState` result required for later
  attachment, do not expose token, path, PID, port, state contents, errno, raw
  exceptions, or caller values in an error, repr, log, output, callback,
  separate result, or retained module state.
- Do not import spike code, change dependencies, refactor existing verification
  modules, or broaden the P0-001/P0-005/P0-008 contracts.

## Acceptance Criteria

- [ ] `discover_existing_startup()` accepts only an exact `StateStore` plus an
  exact built-in `int` or `float` timeout. Wrong top-level types and
  invalid/non-finite/non-positive/over-30-second timeouts fail before clock,
  load, or health work. The fixed errors are
  `TypeError("store must be an exact StateStore")`,
  `TypeError("timeout must be a built-in int or float")`, and
  `ValueError("timeout must be finite, positive, and at most 30 seconds")`.
- [ ] `discover_existing_startup` is an identical export from
  `flowsight.sidecar`, appears exactly once in `__all__`, and has the exact
  signature `(store: StateStore, timeout: float = 0.5) -> SidecarState | None`.
  This task adds no public error or authority taxonomy.
- [ ] Production captures the canonical unbound `StateStore.load` and canonical
  `probe_sidecar_health` callables at import, then invokes them only through
  separately patchable private helpers. This freezes only the wrapper's direct
  dispatch; their already-reviewed internal method graphs remain trusted.
- [ ] After input preflight, one exact finite non-regressing monotonic deadline
  begins before the first load. A positive remaining budget is required before
  the probe, before the second load, and after final comparison. Expiry,
  rollback, a non-float/non-finite observation, or deadline overflow returns
  `None` without starting later work.
- [ ] The frozen canonical loader is called at most twice in the exact order
  `first load -> health probe -> second load`, with no retry or hidden extra
  read. A first `None`, inexact result, ordinary failure, or invalid exact state
  returns `None` with zero health probes and zero second loads.
- [ ] An exact first `SidecarState` is independently reconstructed through the
  strict state schema as a distinct fully equal copy before network work.
  Forged, derived, schema-invalid, self-returning, or unequal reconstruction
  cannot reach the health probe.
- [ ] The frozen canonical P0-005 health probe is invoked exactly once with the
  exact first loaded object and the current positive remaining budget. Only an
  exact built-in `True` may continue; `False`, an inexact result, ordinary
  failure, or expiry returns `None` with zero second loads.
- [ ] After health success and another positive remaining check, the frozen
  canonical loader is invoked exactly once more. The second value must be an
  exact `SidecarState`, be a distinct object from the first load, independently
  reconstruct as a distinct equal copy, and equal the first state across every
  field, including token, database path, timestamp, startup ID, PID, port,
  host, and schema/protocol versions.
- [ ] Success returns the exact second loaded object unchanged only after one
  final positive non-regressed remaining-budget observation. Same-object,
  derived, inexact, schema-invalid, partially equal, rotated, or late second
  state returns `None`.
- [ ] After caller preflight, every ordinary clock/load/reconstruction/probe/
  comparison failure returns only `None`, with no log, output, repr, callback,
  retained exception, secret detail, or additional public error category.
  `KeyboardInterrupt` and `SystemExit` preserve object identity at every stage
  and this wrapper adds or changes no note. P0-005's existing fixed
  `sidecar health probe cleanup failed` cleanup note may remain unchanged.
- [ ] Every normal `None` result remains explicitly negative evidence only.
  Production performs zero lock, state mutation, PID/process, listener,
  channel, SQLite, lease, retry, runtime, or SDK work and returns no boolean,
  enum, handle, callback, or other value that could be mistaken for permission
  to elect, clean up, or launch.
- [ ] One unpatched real composition runs public API -> two real temporary
  P0-001 state reads around one bounded real P0-005 loopback health response,
  then proves an exact result fully equal to but distinct from the published
  fixture, stable on-disk metadata, a dead helper thread, deterministic socket
  cleanup, and no token/path output. A separate no-state path proves zero
  network work.
- [ ] Deterministic unit matrices prove exact call order/counts, shared deadline,
  full-field rotation rejection, same-object rejection, exact collaborator
  results, process-control propagation, no hidden retry, no retained exceptions,
  and captured first/second objects with `result is second` and
  `result is not first`. Static AST evidence fixes one direct frozen-loader
  dispatch inside `_load_state`, two `_load_state` call sites in the public
  function, one direct frozen-probe dispatch inside `_probe_state`, and one
  `_probe_state` call site in the public function. Production imports are
  allowlisted to `__future__`, `math`, `time`, `typing`, `.health`, and `.state`;
  call/module/class/function allowlists admit only fixed validation/deadline/
  reconstruction helpers, the captured state/health operations, strict state
  wire reconstruction, fixed `TypeError`/`ValueError`, and necessary safe
  built-ins. Alias-aware AST checks reject `owner_lock`, `startup_channel`,
  `startup_admission`, `startup_state`, `startup_verification`, `listener`,
  `subprocess`, OS/PID/process, state `publish`/`remove_if_owned`, logging,
  printing/output, callback, mutation, and cache calls/imports. They also reject
  `for`, `async for`, `while`, every comprehension, mutable defaults,
  global/nonlocal declarations, and attribute/subscript stores.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_startup_discovery.py
make test-phase0
make gate-phase0
```

Expected result:

```text
existing-startup discovery tests and all repository checks pass
```

## Risks

- Treating `None` as proof that no sidecar exists or as launch permission can
  create split brain; this API deliberately returns evidence, not policy.
- Comparing only startup ID/PID/port can accept a token, database-path,
  timestamp, host, or version rotation. Success requires full-state equality.
- Giving both the probe and wrapper separate full timeouts can expand startup
  latency; every delegated step must share the one outer deadline.
- Returning the first object, accepting the same object twice, or skipping
  strict reconstruction can make injected collaborator values look like fresh
  filesystem observations.
- This two-read window cannot prove filesystem inode continuity or exclude an
  ABA replacement between observations. Later election remains responsible for
  owner-lock continuity and its own post-lock recheck.

## Reviewer Focus

- Can any first/second state forgery, partial match, same-object result, late
  result, hidden retry, or inexact health value pass?
- Can any path turn `None` or ordinary failure into election, cleanup, launch,
  PID/process, or mutation authority?
- Can token, path, PID, port, errno, raw exception, or caller callback enter an
  error, repr, output, log, cache, retained exception, or side result?
- Did the code remain exactly one read-only `load -> probe -> load` observation
  window without refactoring P0-008 or entering runtime lifecycle policy?

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
- Notes: proves one existing-startup discovery window only, never election,
  stale cleanup, process launch, attachment, reload, or complete Phase 0

## Failure Queue Items

- none
