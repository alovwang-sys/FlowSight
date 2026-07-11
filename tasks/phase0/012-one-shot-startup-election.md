# Task: Resolve One Nonblocking Startup Election

## Task Metadata

```yaml
task_id: P0-012
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-004, P0-010, P0-011, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-012

## Phase

Phase 0

## Goal

Make one one-shot, non-waiting lock-election decision that returns either exact
incumbent evidence or one exact acquired owner lock, while keeping contention,
deadline failure, and wrapper failure structured and observable.

## Context

- P0-010 returns one exact already-published healthy `SidecarState`, or `None`
  as internal negative evidence only. Its `None` never grants launch authority.
- P0-004 provides one canonical nonblocking `OwnerLock.acquire()` and one
  active-error-aware `OwnerLock.__exit__()` cleanup path. P0-011 makes the
  returned owner move-only at the Python object boundary.
- TRIAL-004 established the composition order: discover, attempt the canonical
  owner lock once, then discover again while that lock remains held. Acquiring
  the lock, not discovery `None`, is the source of winner authority.
- The fixed public function is `resolve_owner_election(store, timeout=0.5) ->
  SidecarState | OwnerLock`. Canonical discovery `None` is consumed internally
  and never escapes as a public result.
- A fixed `OwnerElectionError` reports wrapper-owned failures with two exact
  codes: `OWNER_ELECTION_DEADLINE_FAILED` for clock/deadline admission and
  `OWNER_ELECTION_FAILED` for malformed or ordinary composition failures.
- Exact canonical P0-004 `OwnerLockError` values remain visible. In particular,
  `OWNER_LOCK_HELD` is the explicit loser/contention outcome, never `None`, a
  retry hint, or an election error.
- One outer timeout constrains success admission. It is not a hard interruptible
  wall-clock bound over synchronous discovery or filesystem work.
- This is a cooperative production boundary. Import-time canonical bindings are
  frozen against later public-attribute replacement; private seams exist only
  for deterministic fault injection and are not a provenance/security boundary.
- P0-012 trusts the reviewed success postconditions of P0-010 and P0-004. It
  performs exact top-level result-type checks but does not re-read state fields,
  rebuild state wire values, or revalidate the owner descriptor.
- Cleanup guarantees cover the named production calls once an exact owner has
  been bound locally. The raw P0-004 return API cannot make the interpreter gap
  between call return and local binding, or final return and caller receipt,
  atomic against arbitrary asynchronous interruption; this task documents and
  does not widen that limitation.
- Source of truth: MVP design section 4.2 and Phase 0, plus TRIAL-004 promotion
  requirements.

## Related Fact IDs

- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_election.py`
- `tests/sidecar/test_startup_election.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_election.py`
- `tests/sidecar/test_startup_election.py`

## Forbidden

Focused tests may construct exact in-memory state and use real temporary P0-004
locks. They may not start a subprocess, bind a server, publish state, or use a
sleep-based race.

- Do not wait, poll, retry, re-contend, back off, sleep, loop, or turn
  `OWNER_LOCK_HELD` into health, absence, retry, cleanup, or launch authority.
- Do not start, inspect, poll, signal, terminate, kill, wait for, or reap a
  process; construct a command/environment; call `Popen`; transfer/inherit a
  descriptor; or add a child/READY protocol.
- Do not call P0-001 load/publish/remove APIs, inspect state fields, call state
  `to_wire`/`from_wire`, retain a state copy, recover stale state, inspect PID
  liveness, or mutate any state file.
- Do not call `OwnerLock.fileno`, `OwnerLock.close`, `OwnerLock.adopt_inherited`,
  `fcntl`, `LOCK_UN`, or raw `os.close`; duplicate, detach, inspect, or rebuild a
  descriptor; or retry an ambiguous cleanup. Non-winner cleanup uses only the
  frozen canonical P0-004 context-exit path.
- Do not add a public `None`, boolean, sentinel, callback, result wrapper, raw
  descriptor, or any error/result category beyond the exact function, error,
  and two error codes named by this task.
- Do not manufacture/consume a startup channel or outcome, call P0-009, bind or
  mutate a listener, start Uvicorn/ASGI, choose/check a requested port, open
  SQLite, or add writer/queue/sender/lease, SDK, OTel, reload, shutdown, UI,
  browser, or trace behavior.
- Apart from exact successful `SidecarState`/`OwnerLock` results and existing
  fixed `OwnerLockError`, do not expose token, path, PID, port, descriptor,
  inode, errno, raw exception, or caller value in an error, repr, log, output,
  callback, side result, or module cache.
- Do not claim private-seam monkeypatch resistance, import spike code, change
  dependencies, refactor existing primitives, or broaden P0-004/P0-010.

## Acceptance Criteria

- [ ] `resolve_owner_election()` accepts only an exact `StateStore` plus an
  exact built-in `int` or `float` timeout. Wrong top-level types and
  invalid/non-finite/non-positive/over-30-second timeouts fail with the same
  fixed preflight messages as P0-010 before clock, discovery, acquire, or
  cleanup work.
- [ ] `resolve_owner_election`, `OwnerElectionError`, and
  `OwnerElectionErrorCode` are identical exports from `flowsight.sidecar` and
  occur exactly once in `__all__`. The function signature is exactly
  `(store: StateStore, timeout: float = 0.5) -> SidecarState | OwnerLock`.
  The error constructor accepts one exact code and exposes only the fixed
  message `sidecar owner election failed (<code>)`.
- [ ] `OwnerElectionErrorCode` contains exactly
  `OWNER_ELECTION_DEADLINE_FAILED` and `OWNER_ELECTION_FAILED`. Deadline/clock
  failures use only the first; malformed results and ordinary wrapper failures
  use only the second. Exact canonical P0-004 errors never change category.
- [ ] Production captures canonical `discover_existing_startup`, bound
  `OwnerLock.acquire`, and unbound `OwnerLock.__exit__` at import, and invokes
  each only through a separately patchable private call seam. Replacing the
  public attributes later cannot change this wrapper's direct dispatch. Private
  seam replacement is fault injection, not a supported anti-forgery boundary.
- [ ] One finite non-regressing monotonic deadline starts after preflight.
  Every discovery receives the current positive remaining budget; a positive
  remainder is required before acquire, before post-lock discovery, and before
  every successful return. Clock exception, inexact/non-finite value, rollback,
  overflow, or expiry raises exact deadline failure. If an owner is already
  bound, it is cleaned up once before that error escapes.
- [ ] Pre-lock discovery runs exactly once. A normally returned exact
  `SidecarState` is returned unchanged after final deadline admission with zero
  acquire/post-lock-discovery/cleanup calls. A normally returned exact `None`
  is the only result that advances to acquire. Inexact/malformed results or
  ordinary discovery failure raise fixed election failure, never authority.
- [ ] After trusted pre-lock `None`, canonical `OwnerLock.acquire` is called
  exactly once. Exact `OwnerLockError` raised directly by that call preserves
  identity/code/message/cause/context, including visible `OWNER_LOCK_HELD`, and
  performs zero post-lock discovery or cleanup. Derived/inexact or ordinary
  acquire failures become fixed election failure.
- [ ] A normal acquire result must have exact type `OwnerLock`; P0-012 then
  trusts P0-004's active-owner postcondition and performs no `fileno` or field
  validation. Inexact/unknown values are never invoked and raise fixed election
  failure. Under unmodified private seams, the exact locally bound result is the
  sole cooperative Python owner until cleanup or successful return transfer.
- [ ] While that owner remains held, post-lock discovery runs exactly once. A
  normally returned exact `None` returns the exact acquired owner unchanged and
  performs zero cleanup; a real second contender remains blocked until caller
  close. A normally returned exact `SidecarState` is returned unchanged only
  after canonical cleanup and final deadline admission. Inexact/malformed or
  ordinary results cancel authority, clean up once, and raise fixed failure.
- [ ] Normal state cleanup calls frozen canonical `OwnerLock.__exit__` exactly
  as `(owner, None, None, None)` and requires exact built-in `False`. Cleanup for
  a pending deadline/election error passes that exact error as the active
  exception with `(owner, type(error), error, None)`. Exact canonical
  `OwnerLockError` cleanup failure takes precedence; ordinary/malformed cleanup
  becomes fixed election failure; cleanup process-control propagates. No path
  retries cleanup or makes a second OS close attempt.
- [ ] `KeyboardInterrupt`, `SystemExit`, and other process-control
  `BaseException` values raised before owner binding preserve identity with zero
  cleanup. After owner binding, cleanup receives
  `(owner, type(error), error, None)` once. Exact `False` re-raises the original;
  ordinary/malformed cleanup preserves it with the one fixed P0-004 cleanup
  note, while a new cleanup process-control exception may replace it. The
  wrapper never reads a dynamic traceback attribute.
- [ ] Ordinary collaborator failures expose only fixed election errors with no
  raw cause, formatted context, note, log, output, callback, or retained detail.
  A caller's already-active Python-managed `__context__` may still exist, but
  fixed errors raised `from None` suppress it from formatted traceback output.
  Production retains no state, owner, exception, or caller input in module
  mutable state.
- [ ] Real temporary-lock tests prove: an empty store returns one exact active,
  non-inheritable winner that blocks a second canonical acquire until caller
  close; a pre-held canonical owner propagates exact `OWNER_LOCK_HELD` with zero
  post-lock discovery; and an injected post-lock incumbent returns only after
  the temporary owner has been closed and the canonical lock is immediately
  reacquirable. Cleanup ambiguity tests inspect FD reuse before deterministic
  fixture fallback and prove zero retry.
- [ ] Deterministic matrices cover the five terminal classes (incumbent, winner,
  contention/P0-004 error, deadline error, wrapper error), exact call order and
  budgets, every behavior-boundary expiry/rollback, cleanup arguments/results,
  process-control identity, no third discovery/acquire/cleanup retry, and no
  module retention after restoring fault seams.
- [ ] Static evidence permits only the fixed clock/discovery/acquire/cleanup and
  error helpers, and rejects loops/comprehensions, mutable module state/defaults,
  dynamic owner close/fileno/adoption, raw lock/state/process/listener/channel/
  SQLite/runtime/policy calls, logging, output, callbacks, waiting, and caches.
  It verifies the public export identities without freezing an implementation
  snapshot or duplicating P0-004/P0-010 internal tests.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_startup_election.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
one-shot startup-election tests and all repository checks pass
```

## Risks

- Omitting post-lock discovery can miss an incumbent published at the election
  boundary; treating discovery `None` itself as authority can create split
  brain. Only the returned exact owner is positive winner capability.
- A current lock holder can exit before publishing. This slice reports exact
  contention and never waits or re-contends; bounded re-election is later work.
- Returning post-lock state before cleanup can block the incumbent path.
  Cleanup ambiguity cancels every pending result and must never be retried.
- A raw owner return cannot make Python call-result binding or caller receipt
  atomic against arbitrary asynchronous interruption. This task keeps the
  transfer surface to one direct return and documents the unsupported gap.
- The lock constrains cooperating launchers but cannot prove state inode
  continuity, exclude ABA replacement, or guarantee observed health after
  return.

## Reviewer Focus

- Can any public `None`, malformed result, ordinary failure, or late result be
  mistaken for incumbent or winner authority?
- Can any non-winner path leak/double-close the owner, retry an ambiguous FD,
  mask process-control identity, or return state before cleanup?
- Does the wrapper trust canonical dependency postconditions without repeating
  state wire or owner-descriptor validation?
- Did this remain one non-waiting discover/acquire/discover composition with no
  retry, stale cleanup, requested-port policy, process, launch, or mutation?

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
- Notes: proves one incumbent-or-owner decision only, never waiting,
  re-contention, stale cleanup, requested-port policy, child launch/transfer,
  attachment, reload, or complete Phase 0

## Failure Queue Items

- none
