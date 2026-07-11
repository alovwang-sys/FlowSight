# Task: Retry Owner Election Within One Bounded Window

## Task Metadata

```yaml
task_id: P0-013
release: v1
task_type: implementation
status: planned
primary_phase: phase0
impacted_phases: []
depends_on: [P0-012, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-013

## Phase

Phase 0

## Goal

Wait only after exact owner-lock contention and retry the canonical P0-012
election inside one bounded retry-admission window, returning the exact healthy
incumbent or newly elected owner unchanged.

## Context

- P0-012 makes one non-waiting `discover -> acquire once -> discover` decision
  and intentionally leaves `OWNER_LOCK_HELD` visible. Its risk section leaves
  bounded re-election for the case where the current holder exits before it can
  publish state.
- TRIAL-004 proved the architecture with a 25 ms attach poll: a loser waits,
  observes the current owner if it becomes healthy, or acquires the same
  project lock after that owner exits.
- This task composes P0-012 only. It does not launch a process, publish state,
  or decide requested-port compatibility.
- The fixed public function is `wait_for_owner_election(store, timeout=0.5) ->
  SidecarState | OwnerLock`.
- One outer monotonic deadline bounds admission to retries and every wait. Each
  P0-012 attempt receives the current positive remaining budget and keeps its
  own reviewed success-admission deadline. A successful exact P0-012 result is
  returned directly without a second outer admission read, so this wrapper does
  not add a cleanup/revalidation step to the move-only owner transfer. As with
  P0-012, synchronous filesystem/health work is not hard-interruptible.
- Source of truth: MVP design section 4.2 and Phase 0, plus TRIAL-004 promotion
  requirements.

## Related Fact IDs

- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_wait.py`
- `tests/sidecar/test_startup_wait.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_wait.py`
- `tests/sidecar/test_startup_wait.py`

## Forbidden

Focused tests may use real temporary P0-004 locks and deterministic private
clock/wait/election seams. They may use threads only with observable events and
bounded joins. They may not use sleep-based races or start a subprocess.

- Do not start, inspect, poll, signal, terminate, kill, wait for, or reap a
  process; build a command/environment; call `Popen`; transfer/adopt a
  descriptor; or add a child/READY protocol.
- Do not load, publish, remove, repair, or retain state; inspect PID liveness;
  recover stale files; bind a listener; choose/check a requested port; start
  Uvicorn/ASGI; or create a startup channel.
- Do not open SQLite or add a writer, queue, sender, lease, SDK lifecycle, OTel,
  reload, shutdown, UI, browser, ingest, or trace behavior.
- Do not retry after any value except an exact canonical `OwnerLockError` whose
  exact code is `OWNER_LOCK_HELD`. Do not turn discovery `None`, a malformed
  value, deadline failure, wrapper failure, storage error, cleanup error, or
  process-control exception into wait or launch authority.
- Do not add exponential backoff, randomness, callbacks, a background thread,
  mutable module state, caller-configurable poll cadence, or an unbounded wait.
- Do not close, inspect, adopt, or call a method on a returned owner. Do not
  reconstruct or inspect a returned state. P0-012 owns those postconditions.
- Apart from the exact successful P0-012 state/owner result, do not expose a
  token, path, PID, port, descriptor, errno, raw exception, timeout value, or
  caller input in an error, repr, log, output, callback, side result, or cache.
- Do not import spike code, change dependencies, refactor existing primitives,
  or broaden P0-004/P0-010/P0-012 behavior.

## Acceptance Criteria

- [ ] `wait_for_owner_election()` accepts only an exact `StateStore` and exact
  built-in `int` or `float` timeout. Wrong types and invalid/non-finite/
  non-positive/over-30-second values fail with P0-012's fixed preflight messages
  before clock, election, wait, or retry work.
- [ ] `wait_for_owner_election` is an identical `flowsight.sidecar` export,
  occurs exactly once in `__all__`, and has the exact signature
  `(store: StateStore, timeout: float = 0.5) -> SidecarState | OwnerLock`.
  This task adds no public result wrapper, `None`, boolean, callback, error
  type, error code, or poll-cadence argument.
- [ ] Production captures canonical `resolve_owner_election`, monotonic clock,
  and wait operation at import and calls them only through private test seams.
  Replacing public attributes later cannot change direct dispatch. Private seam
  replacement remains fault injection, not a provenance/security boundary.
- [ ] After preflight, one exact finite non-regressing monotonic deadline bounds
  all retry admission and waiting. Before every election and wait there is a
  positive remaining budget; each election receives that current budget. Clock
  exception, inexact/non-finite observation, rollback, overflow, no progress
  across a completed wait, or expiry raises exact
  `OWNER_ELECTION_DEADLINE_FAILED` with no later election/wait.
- [ ] The first canonical P0-012 election runs immediately with no wait. An
  exact `SidecarState` or `OwnerLock` result is returned unchanged and ends the
  operation. The wrapper does not read fields, call `fileno`, close/release the
  owner, or make another clock/election/wait call after success.
- [ ] Only an exact `OwnerLockError` carrying exact
  `OwnerLockErrorCode.OWNER_LOCK_HELD` enters the wait path. Every other exact
  P0-004 or P0-012 error preserves identity/code/message/cause/context with zero
  wait/retry; derived, malformed, or ordinary private-seam failure becomes the
  fixed `OWNER_ELECTION_FAILED`; process-control preserves identity.
- [ ] Each contention performs one wait of exactly
  `min(0.025, current_remaining)` seconds through the frozen operation. The
  interval is always a positive built-in float. Exact `None` plus strict clock
  progress permits the next retry; ordinary/malformed wait failure becomes the
  fixed election failure, while process-control preserves identity.
- [ ] Repeated contention cannot busy-loop or outlive the cooperative retry
  window: every completed wait must advance the clock, each iteration recomputes
  remaining time, and expiry reports exact deadline failure rather than the
  last contention object. No path performs an extra wait/election after expiry.
- [ ] Deterministic matrices cover immediate incumbent/winner, contention then
  incumbent/winner, repeated contention to expiry, every clock boundary,
  poll-capping, strict progress, exact error taxonomy/identity, malformed seam
  values, process-control, call order/budgets, and no retention after restoring
  seams. Tests use no sleep.
- [ ] A real temporary-lock test starts with one canonical owner held, reaches
  an observable bounded wait, releases that owner without a sleep race, and
  proves the waiter returns one exact active owner that blocks another canonical
  acquire until caller close. A separate injected sequence proves state
  published during contention is returned unchanged.
- [ ] Static evidence permits only validation, clock/deadline arithmetic,
  canonical P0-012 election, one fixed 25 ms wait, fixed errors, and the bounded
  retry loop. It rejects state/process/listener/channel/SQLite/runtime/SDK calls,
  owner inspection/cleanup/adoption, logging/output, callbacks, mutable state,
  comprehensions, async work, and caches without copying P0-012 internals.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/sidecar/test_startup_wait.py
make test-phase0
make gate-phase0
```

Expected result:

```text
bounded owner re-contention tests and all repository checks pass
```

## Risks

- Treating any negative discovery or malformed error as retry authority can
  create split brain. Only exact canonical lock contention may wait.
- A wait that returns without monotonic progress can busy-loop under a broken
  clock/seam; strict progress fails closed.
- The outer deadline bounds retry admission, not arbitrary synchronous work.
  Each admitted P0-012 attempt independently receives the remaining budget and
  enforces its reviewed cooperative success deadline.
- The move-only owner still has P0-012's documented Python return-transfer gap
  under arbitrary asynchronous interruption. This wrapper must not widen that
  surface with owner inspection or cleanup.

## Reviewer Focus

- Can anything except exact `OWNER_LOCK_HELD` trigger a wait or retry?
- Can clock stalling/rollback, malformed wait output, or deadline expiry create
  a busy-loop, extra attempt, raw error, or false authority?
- Does the wrapper pass one decreasing shared budget without duplicating or
  weakening P0-012's owner/state/error postconditions?
- Did this stay pure election policy with no process, launch, state mutation,
  stale recovery, port policy, descriptor handoff, or SDK/runtime behavior?

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

- Command: TBD
- Result: TBD
- Notes: TBD

## Failure Queue Items

- none
