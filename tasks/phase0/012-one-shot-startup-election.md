# Task: Try One Nonblocking Startup Election

## Task Metadata

```yaml
task_id: P0-012
release: v1
task_type: implementation
status: planned
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

Make one bounded nonblocking decision that returns exact incumbent evidence,
transfers one exact active owner lock after a required post-lock discovery, or
returns no authority.

## Context

- P0-010 proves one already-published healthy startup in a read-only
  observation window, but its `None` is negative evidence only and cannot grant
  election authority.
- P0-004 provides one descriptor-anchored `OwnerLock.acquire()` attempt with
  exact contention/storage/cleanup errors and close-only context cleanup. It
  deliberately stopped before composing state discovery and election policy.
- P0-011 makes `OwnerLock` move-only at the Python object boundary before this
  task can return it across a new ownership-transfer boundary.
- TRIAL-004 proved the required order: discover an incumbent, attempt the
  canonical nonblocking owner lock, and discover again while the acquired lock
  is still held. The second discovery closes the TOCTOU window in which another
  launcher published immediately before this caller acquired the lock.
- The fixed public shape is `elect_startup_once(store, timeout=0.5) ->
  SidecarState | OwnerLock | None`.
- Result meanings are exact and disjoint:
  - exact `SidecarState`: one incumbent observation; the function retains no
    owner lock and does not prove requested-port compatibility;
  - exact active `OwnerLock`: the only positive winner authority; ownership
    transfers only when the function successfully returns it to the caller,
    after a trusted post-lock discovery returned exact `None` inside the outer
    deadline;
  - exact `None`: indeterminate/no authority after a deadline, clock, ordinary
    collaborator, or malformed-result failure. It never means that state is
    absent, that an owner is healthy, or that the caller may wait/retry/clean up
    or launch.
- Exact P0-004 `OwnerLockError` values originating from the sole canonical
  acquire remain visible, including `OWNER_LOCK_HELD` as explicit contention.
  This task does not turn contention into `None`, add another error taxonomy,
  or implement waiting/re-contention.
- One shared monotonic success-admission deadline covers both discoveries and
  the acquire window. It rejects late incumbent/winner results but cannot hard
  interrupt synchronous state/health/filesystem work.
- Acquiring the lock, not P0-010's `None`, is the source of winner authority.
  P0-010 may internally collapse ordinary read/probe/clock failure to `None`;
  therefore the winner result does not claim to prove state absence, inode
  continuity, or exclude ABA replacement.
- A returned winner remains caller-owned until a future launcher transfers it
  to a child or the caller uses P0-004's context manager/one close. GC is not a
  cleanup guarantee. This task performs neither transfer nor launch.
- Process-control cleanup guarantees begin once the canonical acquire result has
  materialized in wrapper-owned local state and cover every named subsequent
  seam, validation, clock, and cleanup call. They do not claim atomicity against
  arbitrary bytecode-level asynchronous interruption between a Python call
  returning and its result being bound, or between this function returning and
  the caller binding its result; the existing P0-004 raw-return API cannot
  provide that stronger guarantee.
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

Focused tests may use exact in-memory states and real temporary P0-004 owner
locks. They may not start a subprocess, bind a server, publish state, or use a
sleep-based race.

- Do not wait, poll, retry, re-contend, back off, sleep, loop, or turn
  `OWNER_LOCK_HELD` into a health/absence/retry/launch result.
- Do not start, inspect, poll, signal, terminate, kill, wait for, or reap a
  process; construct a command/environment; call `Popen`; transfer/inherit a
  descriptor; or add a child/ready protocol.
- Do not publish, remove, repair, rename, or inspect a state file, raw JSON, or
  raw bytes; recover stale state; inspect PID liveness; unlink a persistent
  lock; or call P0-001 `load`/`publish`/`remove_if_owned` directly. The only
  permitted state-value access is bounded canonical schema reconstruction of an
  exact in-memory `SidecarState`; its transient wire dict is never retained,
  logged, returned, or exposed.
- Do not manufacture/consume a startup channel or outcome, call P0-009, bind or
  mutate a listener, start Uvicorn/ASGI, choose/check a requested port, open
  SQLite, or add a writer/queue/sender/lease, SDK, OTel, reload, shutdown, UI,
  browser, or trace behavior.
- Do not call `LOCK_UN`, `fcntl`, raw `os.close`, dynamic `OwnerLock.close`, or
  retry an ambiguous descriptor. Non-winner cleanup must invoke the frozen
  canonical P0-004 context-exit path exactly once.
- Do not enter an acquired owner for the first time through dynamic `with`
  dispatch or use a transfer flag plus broad `finally`. After the canonical
  acquire result is bound locally, add no fallible work outside the controlled
  owner-cleanup path. The unsupported interpreter-level call-to-binding gap is
  not widened or presented as solved.
- Do not add a caller-visible/public result wrapper, sentinel, callback,
  boolean, enum, raw file descriptor, PID, token, path, port-policy result, or
  new public error taxonomy. Fixed private/local provenance tags and tuples are
  permitted only inside the module and may never escape the exact public union.
- Apart from the exact successful `SidecarState`/`OwnerLock` results and the
  existing safe fixed `OwnerLockError`, do not expose token, path, PID, port,
  descriptor, inode, errno, raw exception, or caller value in an error, repr,
  log, output, callback, side result, or retained module state.
- Do not import spike code, change dependencies, refactor existing primitives,
  or broaden P0-004/P0-010 contracts.

## Acceptance Criteria

- [ ] `elect_startup_once()` accepts only an exact `StateStore` plus an exact
  built-in `int` or `float` timeout. Wrong top-level types and
  invalid/non-finite/non-positive/over-30-second timeouts fail with the same
  fixed `TypeError`/`ValueError` messages as P0-010 before clock, discovery,
  acquire, or cleanup work.
- [ ] `elect_startup_once` is an identical export from `flowsight.sidecar`,
  appears exactly once in `__all__`, and has the exact signature
  `(store: StateStore, timeout: float = 0.5) -> SidecarState | OwnerLock | None`.
  No new public type, sentinel, boolean, result wrapper, or error is added.
- [ ] Production captures canonical `discover_existing_startup`, bound
  `OwnerLock.acquire`, unbound `OwnerLock.fileno`, unbound `OwnerLock.__exit__`,
  unbound `SidecarState.to_wire`, and bound `SidecarState.from_wire` at import.
  Each collaborator is invoked only through a separately patchable private
  seam. Runtime replacement of those public attributes does not change direct
  dispatch in this wrapper.
- [ ] One exact finite non-regressing outer monotonic success deadline begins
  after preflight. Deadline overflow/expiry/rollback or an inexact/non-finite
  clock cannot produce a state or owner result. The exact success-path clock
  ordinals are fixed as follows:
  - pre-lock state: start, pre-discovery budget, final post-rebuild admission;
  - winner: start, pre-discovery budget, pre-acquire admission, post-acquire
    admission, post-validation discovery budget, and one final post-discovery/
    immediately-pre-return admission;
  - post-lock state: the winner prefix through post-validation discovery,
    followed by one post-discovery/post-rebuild pre-release admission, canonical
    release, and one final post-release admission before state return.
  No ordinal is silently merged beyond the explicitly shared checks above.
- [ ] The discovery seam records whether its frozen canonical call returned
  normally. Only a normally returned exact `None` may advance from pre-lock
  discovery to acquire or from post-lock discovery to winner. A seam-caught
  ordinary exception or any inexact/derived/forged result returns `None`; before
  acquire it performs no lock work, and after acquire it first releases the
  owner exactly once. Private provenance tags remain local and fixed.
- [ ] Pre-lock discovery is invoked exactly once with the current remaining
  budget. An exact valid state is independently reconstructed through the
  frozen canonical state seams as a distinct equal copy; the transient wire
  value is discarded, and the exact original state is returned unchanged
  within the outer deadline with zero acquire/post-discovery/cleanup calls.
- [ ] Only a trusted exact pre-lock `None` reaches the frozen canonical
  `OwnerLock.acquire`, exactly once. Exact errors directly raised by that call,
  including `OwnerLockError(OWNER_LOCK_HELD)`, preserve identity/code/message/
  cause/context under bare re-raise and produce no result. Derived/inexact or
  ordinary non-P0-004 failures never masquerade as contention and return only
  `None`.
- [ ] A normally returned acquire candidate must have exact type `OwnerLock`
  before any method is invoked. The frozen fileno seam is then called exactly
  once and must return an exact built-in `int >= 3`; this is the supported
  active-handle proof. Closed owners, derived/inexact values, booleans, derived
  integers, values below three, or ordinary validation failures produce no
  authority. Unknown objects are never invoked. Any exact locally owned owner
  rejected after binding is released once through the controlled cleanup path.
- [ ] Capability provenance comes from the normal frozen canonical acquire
  call, not from reconstructing an `OwnerLock` from fields. Private-seam
  monkeypatching and reflective manufacture are test hooks, not an adversarial
  security boundary. From successful active validation until a supported
  winner return or cleanup, P0-012 is the sole cooperative Python owner.
- [ ] While the exact acquired owner remains held, post-lock discovery is
  invoked exactly once with the current positive remaining budget. An exact
  state is independently reconstructed through the frozen canonical state
  seams, then the owner is successfully released through the frozen canonical
  context-exit seam before another final deadline observation and exact original
  state return.
- [ ] A trusted exact post-lock `None` may return only the exact acquired
  `OwnerLock` object, unchanged and still active, after the final outer deadline
  check. Successful function return is the supported ownership-transfer point;
  P0-012 performs zero cleanup on that winner path, and a real second contender
  remains blocked until the caller closes the result. No claim is made about
  arbitrary asynchronous interruption at the interpreter return/binding gap.
- [ ] Every non-winner path after a supported owner is bound invokes the frozen
  canonical P0-004 context-exit seam exactly once and makes at most one OS close
  attempt. Normal state/`None`/ordinary/deadline cleanup calls exact
  `(owner, None, None, None)` and requires exact `False`; cleanup failure or a
  malformed result cancels the pending result. Exact canonical `OwnerLockError`
  cleanup failures remain visible; unknown ordinary failures collapse to no
  authority. No cleanup call retries an ambiguous descriptor.
- [ ] `KeyboardInterrupt`, `SystemExit`, and other process-control
  `BaseException` values raised before any owner is bound preserve identity and
  perform no cleanup; this covers the named clock, discovery, and acquire seams.
  At every named seam/clock/validation/admission stage after owner binding, the
  active error preserves identity when cleanup succeeds or reports an ordinary
  ambiguous close through P0-004. Cleanup receives exact
  `(owner, type(error), error, None)` before bare re-raise. The fixed `None`
  avoids dynamic traceback access; canonical P0-004 exit discards that argument.
  The wrapper adds no note, while P0-004's one fixed
  `sidecar owner lock cleanup failed` note may remain. If cleanup itself raises
  a distinct process-control exception, canonical P0-004 context-exit semantics
  propagate that cleanup exception and may replace the prior active error; both
  identities cannot be promised simultaneously.
- [ ] Other ordinary clock/discovery/result-validation failures return only
  `None` without log, output, repr, callback, cause/context, retained exception,
  secret detail, or authority. If they occur after acquire, their exception is
  discarded before normal cleanup so a cleanup failure cannot retain it as
  context.
- [ ] Real temporary-lock tests capture the raw descriptor and canonical inode
  before fallback cleanup and prove: an empty store returns an exact active,
  non-inheritable winner whose descriptor remains open and blocks a second
  canonical acquire until caller close; a pre-held canonical owner raises the
  exact identity-preserved `OWNER_LOCK_HELD` with zero post-discovery; and a
  post-lock incumbent is returned only after `os.fstat(raw_fd)` gives exact
  `EBADF` and the lock is immediately reacquirable. Deadline/ordinary cleanup
  and cleanup-failure descriptor-reuse cases prove physical FD state before any
  deterministic fallback cleanup.
- [ ] Deterministic matrices prove exact event order/counts and all clock
  ordinals/budgets, discovery normal-return provenance, state
  `to_wire`/`from_wire`/equality reconstruction ordinals, acquire and fileno
  validation, cleanup argument tuples/results, all result identities, no third
  discovery/acquire retry, every exception prefix, and post-patch exception/
  state/owner/caller-input non-retention. They cover normal cleanup exact
  `False`, ordinary and exact cleanup failures, cleanup `KeyboardInterrupt`/
  `SystemExit` with and without an active process-control error, and the fixed
  precedence above. Restored-seam privacy checks assert no log/output or secret
  token/path/PID/port/FD/errno/raw detail.
- [ ] Exact AST evidence fixes imports, private tags/module bindings,
  functions/classes, per-function calls and frozen dispatch placement; rejects
  loops/comprehensions, mutable defaults/state, nested callbacks, public
  sentinels/wrappers, dynamic attribute/subscript stores, owner-lock adoption/
  dynamic close/`LOCK_UN`, raw state-file/health/process/listener/channel/
  SQLite/runtime/policy calls, logs, output, and caches.
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

- Omitting the post-lock discovery can start a second sidecar after another
  launcher publishes just before this caller acquires the owner lock.
- Treating discovery `None` as absence or authority can create split brain. The
  returned exact active owner lock is the only positive winner capability.
- A lock holder can exit before publishing. This one-shot slice raises exact
  contention and does not recover by waiting/retrying; bounded re-contention is
  a later task.
- Returning a post-lock incumbent before closing the acquired owner can block
  the real owner path; closing before the final deadline check without another
  observation can return late evidence.
- Setting a transfer flag before the return expression, dynamically entering a
  context, or using naked close/finally cleanup can leak, double-close, mask a
  process-control error, or retry a reused descriptor.
- The current P0-004 raw-return acquisition API cannot make Python call-result
  binding or caller receipt atomic against arbitrary asynchronous interruption;
  this task documents and does not widen that unsupported interpreter gap.
- A close error leaves physical descriptor state ambiguous. It must cancel all
  pending results and must never trigger a retry.
- The lock constrains cooperating launchers but cannot prove state inode
  continuity, exclude ABA replacement, or guarantee an observed incumbent
  still lives after return.

## Reviewer Focus

- Can any exception, malformed collaborator result, clock failure, or exact
  discovery `None` produce an owner without a normally returned canonical
  acquire, exact active fileno validation, and trusted post-lock check?
- Can any non-winner path leak/double-close the owner, retry an ambiguous FD,
  mask process-control identity, or return state before cleanup and deadline?
- Can runtime method replacement bypass frozen discovery/acquire/fileno/
  context-exit/state-reconstruction dispatch or inject an error that
  masquerades as P0-004 contention?
- Can token, path, PID, port, FD, errno, raw exception, callback, or retained
  object escape outside the exact existing public results/errors?
- Did this remain one nonblocking decision with no wait, retry, stale cleanup,
  requested-port policy, process, launch, state mutation, or runtime behavior?

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
