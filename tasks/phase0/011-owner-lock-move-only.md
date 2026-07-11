# Task: Make OwnerLock Move-Only

## Task Metadata

```yaml
task_id: P0-011
release: v1
task_type: safety
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-004, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-011

## Phase

Phase 0

## Goal

Prevent unmodified standard-library `copy.copy`, `copy.deepcopy`, and default
pickle reduction entry points from accidentally creating a second `OwnerLock`
object that claims the same descriptor integer.

## Context

- P0-004 established canonical owner-lock acquisition, inherited adoption,
  exact close-once cleanup, and gapless open-file-description continuity.
- Pre-election review proved a missing Python object boundary: current
  `copy.copy()`, `copy.deepcopy()`, and pickle roundtrips each create a distinct
  `OwnerLock` whose `_descriptor` equals the original. Closing either object can
  make the other close a reused unrelated descriptor later.
- P0-006 already uses explicit `__copy__`, `__deepcopy__`, `__reduce__`, and
  `__reduce_ex__` guards for its move-only startup-channel handles. This task
  applies the same fixed fail-closed contract to `OwnerLock` before an election
  API begins returning that handle across a new ownership boundary.
- The fixed public error text is exact and input-independent:
  `TypeError("OwnerLock is move-only")`; no new error enum or result type is
  introduced. Each guard raises it `from None` so Python suppresses an existing
  caller exception from formatted traceback output.
- This is a fail-fast API guard for cooperative same-process callers, not an
  adversarial Python security boundary. Its guarantees assume the class and
  stdlib dispatch registries are unmodified and no pre-populated deepcopy memo
  substitutes a result. It does not claim to defeat reflection, direct
  `object.__new__`, monkeypatching, a custom pickler dispatch table, an
  externally registered reducer, a third-party serializer, direct descriptor
  duplication, or a caller-controlled finalizer.
- Python may attach a caller's already-active exception as `__context__` even
  to an error raised `from None`. The guards neither inspect nor store that
  caller-owned context; they only suppress it from formatted traceback output.
- This task only hardens Python object duplication. It does not claim to make
  untrusted pickle input safe, and it does not change the existing
  `fork`/`exec` inherited-descriptor continuity explicitly supported by P0-004.
- Source of truth: MVP design sections 4.2 and 11 Phase 0, P0-004, and
  TRIAL-004 promotion requirements.

## Related Fact IDs

- FS-008

## Allowed Files

- `flowsight/sidecar/owner_lock.py`
- `tests/sidecar/test_owner_lock.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/owner_lock.py`
- `tests/sidecar/test_owner_lock.py`

## Forbidden

- Do not change owner-lock acquisition, adoption, canonical inode validation,
  flock operations, inheritable flags, repr, fileno, close, context-manager,
  cleanup-note, or error-code behavior.
- Do not close, duplicate, detach, inherit, unlock, or inspect the descriptor
  while rejecting a copy/serialization attempt.
- Do not add `__getstate__`, `__setstate__`, `__getnewargs__`,
  `__getnewargs_ex__`, `__getinitargs__`, `__replace__`, a clone/copy helper,
  or any alternate reconstruction path.
- Do not import `copy`, `pickle`, `copyreg`, multiprocessing, subprocess, or
  serialization libraries in production. They are test-only callers.
- Do not register, inspect, replace, or attempt to police caller-owned copy,
  copyreg, Pickler, serializer, or private dispatch tables.
- Do not add election, discovery, waiting, retry, stale-state cleanup, listener,
  startup channel, process launch, SQLite, SDK, OTel, UI, or lifecycle behavior.
- Do not change dependencies, import spike code, mutate unrelated tests, or
  broaden runtime/platform support.

## Acceptance Criteria

- [x] Exact `OwnerLock.__copy__(self) -> NoReturn`,
  `OwnerLock.__deepcopy__(self, memo: dict[int, object]) -> NoReturn`,
  `OwnerLock.__reduce__(self) -> NoReturn`, and
  `OwnerLock.__reduce_ex__(self, protocol: object) -> NoReturn` methods exist
  with no defaults, varargs, kwargs, decorators, or alternate return path.
- [x] Each guard immediately raises exact input-independent
  `TypeError("OwnerLock is move-only") from None`. The deepcopy memo and reduce
  protocol inputs are only deleted as unused locals; they are not otherwise
  inspected, mutated, represented, dynamically invoked, or intentionally
  retained.
- [x] A Cartesian active/closed-owner matrix covers `copy.copy`, default
  `copy.deepcopy`, all four direct guards, default `pickle.dumps`, protocol
  `-1`, and every protocol from `0` through `pickle.HIGHEST_PROTOCOL`. Every
  path fails with the fixed TypeError and returns no copy or serialized result.
  Closed owners remain closed and never reacquire, reopen, or touch a
  descriptor. In a separate compatibility probe, where the running CPython
  exposes `copy.replace`, its own unsupported-type TypeError rejects the owner
  without adding `__replace__`; that library error is not the fixed guard error.
- [x] On an active owner, the rejected standard paths leave the exact original
  descriptor, inode, inheritable flag, safe repr, and lock ownership unchanged;
  an independent contender is still rejected until the original closes. A
  caller-supplied default-deepcopy memo remains exactly unchanged.
- [x] Copy/deepcopy/pickle failures perform zero `fileno`, flock, open, close,
  unlink, filesystem, logging, output, direct callback, cache, or background
  work. These claims cover the exact built-in memo/protocol values supplied by
  stdlib plus direct-call opaque sentinels that the test retains strongly; they
  do not claim control over destruction of a hostile last-reference argument.
  None of these rejected standard calls creates another supported handle in the
  calling process, and no memo, protocol, error, or temporary test object
  remains retained after the caller releases the captured exception. These
  calling-process claims do not change P0-004's explicit `fork`/`exec`
  inheritance contract.
- [x] Fixed public error surfaces (`type`, `str`, `repr`, and `args`) expose no
  path, descriptor, inode, errno, raw serializer detail, or caller memo/protocol
  value. The guard introduces no cause or note; context is absent when the
  caller invokes it without an already-active exception. With an already-active
  caller exception, Python-managed `__context__` is permitted,
  `__suppress_context__` is exact `True`, and formatted traceback output hides
  the context. This is not a claim against deliberate traceback/frame
  introspection.
- [x] Deterministic descriptor-reuse evidence proves no rejected duplication
  path closes the original or a replacement integer; the original still closes
  exactly once through existing P0-004 behavior and a successor can then
  acquire the same persistent canonical lock.
- [x] Exact AST evidence fixes the complete existing `OwnerLock` method set plus
  the four new methods and their signatures. Apart from adding `NoReturn` to the
  existing typing import and adding those methods, production statements remain
  byte-for-byte unchanged. Each new body permits only deletion of its unused
  `memo`/`protocol` input where present plus the fixed `TypeError` raise from
  `None`, with no return, control flow, descriptor access, assignment, mutable
  state, dynamic dispatch, serialization import, or additional public API.
- [x] All existing P0-004 owner-lock acquisition/adoption/context/exec-continuity
  tests remain unchanged and pass, along with focused tests, `make test-phase0`,
  `make check`, and the sustained Phase 0 gate on CPython 3.12/3.13 and the
  macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_owner_lock.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
move-only owner-lock tests and all repository checks pass
```

## Risks

- Blocking only `copy.copy` leaves deepcopy and pickle reconstruction paths
  capable of cloning the descriptor integer.
- A guard that reads `fileno()` or changes `_descriptor` can close, invalidate,
  or leak a perfectly valid owner merely because a caller attempted copying.
- Returning a serialized placeholder or descriptor would create a second
  authority channel and violate P0-004's single-owner cleanup contract.
- A hostile custom reducer, modified dispatch registry, monkeypatch, reflection,
  or pre-populated memo can bypass cooperative dunder guards and is outside this
  non-security task.
- Tightening Python copy protocols must not block the separately reviewed
  `fork`/`exec` inheritance of the same open-file description; that path passes
  an integer descriptor explicitly and never pickles the `OwnerLock` object.

## Reviewer Focus

- Can any unmodified standard copy/deepcopy/default-pickle path still create a
  distinct object with the same descriptor integer?
- Does any guard inspect, mutate, close, duplicate, or expose the active
  descriptor, or do anything with a memo/protocol beyond deleting its local?
- Do active/closed failure paths have the exact same fixed private error and
  leave all P0-004 ownership/cleanup semantics unchanged?
- Did this remain only a two-file Phase 0 ownership hardening task with no
  election or process-lifecycle behavior?

## Role Outputs

Implementer:
- Codex primary added only `NoReturn` plus the four fixed move-only guards and
  the focused default-protocol safety evidence in candidate `b1680be`.

Adversarial Reviewer:
- Reviewer 1: boundary reviewer reported P0/P1/P2 = 0 after verifying exact
  `from None` bodies,
  unchanged P0-004 semantics, honest default-dispatch scope, and exception
  context/privacy behavior.
- Reviewer 2: test adversary strengthened object-count, no-I/O/no-output, active/closed,
  descriptor-reuse, and exact-order AST checks; the primary consolidated its
  concurrent draft before the final stable verification run.

Fixer:
- Codex primary accepted the planning findings that custom dispatch and
  Python-managed caller context cannot be blocked, narrowed the contract,
  removed duplicate concurrent test drafts, and found no remaining P0/P1/P2.

Quality Governor:
- Independent governor: P0/P1/P2 = 0; confirmed the two-file product allowlist,
  no P0-012/election behavior in the candidate, and no new trial, fact, rule,
  dependency, or scope override requirement.

## Verifier Evidence

- Command: focused owner-lock tests; `make test-phase0`; `make gate-phase0`
  (including full `make check`); pre-commit `make check-fast`; candidate GitHub
  Actions matrix
- Result: passed
- Notes: focused tests passed 128/128; `make test-phase0` passed 1,307 tests;
  embedded `make check` passed 1,432 tests plus formatting, lint, typing, and
  agent checks; the sustained Phase 0 gate passed on local CPython 3.13.5.
  Candidate `b1680be547a9f92ca6c98d3f7bf5896e939f1756` passed
  [run 29155099578](https://github.com/alovwang-sys/FlowSight/actions/runs/29155099578)
  with jobs
  `86550798430` (macOS 3.13), `86550798433` (Ubuntu 3.12),
  `86550798434` (macOS 3.12), and `86550798438` (Ubuntu 3.13).
  The full check correctly retains the partial-scaffold limitation. This proves
  default Python object duplication is blocked only; it does not prove
  election, discovery, process launch, attachment, or complete Phase 0.

## Failure Queue Items

- none
