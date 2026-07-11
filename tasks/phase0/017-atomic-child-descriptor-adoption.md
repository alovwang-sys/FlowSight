# Task: Atomically Adopt the Sidecar Child Descriptor Pair

## Task Metadata

```yaml
task_id: P0-017
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-004, P0-006, P0-011, P0-014, P0-016, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-017

## Phase

Phase 0

## Goal

Convert one exact decoded child bootstrap into the strictly adopted owner-lock
and startup-writer handles, retiring each admitted ownership locator through one
successful transfer or at most one cleanup attempt when a reviewed operation
succeeds, fails, or synchronously raises process control.

## Context

- P0-016 freezes and validates the inert child argv schema but deliberately
  leaves exact child-side descriptor adoption and partial-adoption cleanup to a
  later task. P0-004/P0-011 and P0-006 prove the owner-lock and startup-writer
  primitives separately; no production caller yet composes both transfers.
- This task begins with an already decoded `SidecarChildBootstrap`. It first
  admits only the exact result/config types and two exact distinct descriptor
  locators without touching a descriptor. That shallow admission establishes a
  post-shallow-admission pair-consume boundary: canonical encoder/store
  preflight and both adoptions may then succeed, fail, or synchronously raise,
  but neither locator returns to the caller. Owner adoption precedes writer
  adoption.
- Supported callers pass the unmodified result of the P0-016 decoder exactly
  once during single-threaded early child entry and never use either locator
  after this function is invoked. This cooperative ownership API does not claim
  to defeat reflection, direct `object.__new__`/`object.__setattr__`, private
  monkeypatching, hostile replacement of inherited `BaseException` machinery,
  or a caller that concurrently closes/reuses a descriptor.
- Process-control guarantees cover only synchronous exceptions raised from the
  captured encoder, store constructor, adopters, result-construction seam, and
  cleanup/note operations. Arbitrary asynchronous injection between Python
  bytecodes, fatal signals, `os._exit`, and unrecoverable allocation failure are
  outside this task; the future child-process boundary remains the final crash
  cleanup mechanism.
- The fixed internal surface is:

  ```python
  def adopt_sidecar_child_descriptors(
      bootstrap: SidecarChildBootstrap,
  ) -> tuple[OwnerLock, StartupWriter]: ...
  ```
- Every ordinary composition failure is exactly the built-in
  `RuntimeError("sidecar child descriptor adoption failed")`. No new public
  error or result type is added. Existing primitive errors remain internal to
  the composition boundary.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 4.1, 4.2, 4.4, 7.4, and Phase 0
  - `spikes/sidecar_otel/RESULT.md` promotion requirements
  - P0-004, P0-006, P0-011, P0-014, and P0-016 contracts

## Related Fact IDs

- FS-001
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/child_adoption.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_child_adoption.py`
- `tests/sidecar/test_runtime_config.py`

`flowsight/sidecar/__init__.py` may change only for the exact import and
`__all__` entry. `tests/sidecar/test_runtime_config.py` may change only for its
exact sidecar-export and public-submodule expectations.

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/child_adoption.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_child_adoption.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not add `sys.argv`, `argparse`, `__main__`, a CLI, an environment or command
  builder, interpreter selection, `subprocess`, `Popen`, fork, exec, `pass_fds`,
  process polling, signalling, termination, killing, waiting, reaping, or a
  process/thread/async task in production or the new task test. Existing
  unchanged P0-004 tests prove owner-lock exec continuity, while P0-006 proves
  writer adoption with a real pipe. This task tests pair composition with real
  same-process duplicates only; pair-after-exec belongs to the entrypoint or
  launcher task.
- Do not bind or inherit a listener; generate a startup ID/token; create,
  publish, load, remove, or repair sidecar state; send or receive READY; probe
  health; instantiate FastAPI/Uvicorn; or decide requested-port compatibility.
- Do not change the owner-lock, startup-channel, bootstrap, runtime-config, or
  state primitives. Do not add an alternate codec, descriptor abstraction,
  generic resource manager, callback, cache, registry, or mutable module state.
- Do not change `FlowSight`, SDK lifecycle, election, discovery, sender, lease,
  SQLite/writer/queue, OTel, reload, shutdown, source scanning, UI, packaging,
  dependencies, Makefile, or spike code.
- This module must not directly call `StateStore.ensure_private_directory`,
  `load`, `publish`, `remove_if_owned`, or another state/storage operation.
  Construction plus P0-004's already-reviewed internal canonical owner-file
  verification are the only state-boundary behavior permitted.
- Do not expose a path, extra raw descriptor scalar, bootstrap/config repr,
  encoded argv, errno, raw OS/primitive error, or caught exception through the
  fixed ordinary error, log, stdout/stderr, callback, cache, or side result.
  The two returned existing handles necessarily own and expose their descriptor
  through their already-reviewed `fileno()` contracts; no second scalar copy is
  returned or retained.
- An identity-preserved non-`Exception` `BaseException` may retain its original
  primitive/composition traceback and the fixed cleanup notes allowed by
  P0-004/P0-006. Production never formats, logs, caches, or inspects that
  traceback or its values.

## Acceptance Criteria

- [x] `adopt_sidecar_child_descriptors` is exported identically from
  `flowsight.sidecar`, occurs exactly once in `__all__`, has the fixed signature
  above, and is the only new production surface. It returns an exact built-in
  two-tuple; no resource bundle, alternate adopter, or public error type exists.
- [x] Shallow admission accepts only an exact `SidecarChildBootstrap`, reads
  only its exact `config`, `owner_lock_fd`, and `startup_writer_fd` slots without
  dynamic attribute dispatch, and accepts only an exact `SidecarRuntimeConfig`
  plus two distinct exact integers in `3..2147483647`. Missing, malformed,
  subclassed, or out-of-range slots fail before any descriptor, lock, pipe, or
  close operation. Such unsupported input contains no admitted ownership
  locator.
- [x] Once shallow admission succeeds, the caller has transferred both
  descriptors regardless of the eventual return or exception. The function
  tracks both owned raw locators immediately and attempts to close them
  writer-first then owner on every later canonical-preflight failure. Each
  locator is semantically retired after one successful adoption or one cleanup
  attempt; a reported close failure does not prove physical closure and is
  never retried. The caller
  must never close or reuse either locator after this boundary.
- [x] Owned canonical preflight calls the captured P0-016 encoder exactly once
  with the snapshotted config and descriptors. The captured encoder is the
  canonical authority; the adopter only requires an exact built-in tuple of
  eight exact built-in strings and then discards it. Public package/module
  replacement cannot redirect the binding; a private seam is fault injection
  only. The adopter neither copies the codec nor calls the decoder again.
- [x] Owned canonical preflight constructs exactly one captured canonical
  `StateStore` from only `config.runtime_root` and `config.project_id`, requires
  the exact store type, exact `project_id`, and exact string form of
  `runtime_root`, and directly performs no state/directory operation. Encoder,
  constructor, result-shape, or derived-field failure retires both admitted
  ownership locators with at most one close attempt apiece.
- [x] After canonical preflight, the owner-lock descriptor is transferred to
  the captured canonical
  `OwnerLock.adopt_inherited(store, fd)` exactly once before the startup writer
  is transferred to the captured canonical
  `StartupWriter.adopt_inherited(fd)` exactly once. A raw locator is invalidated
  locally before its adopter runs, so an adopter that consumes then fails is
  never followed by a duplicate close. These captured canonical adopters are
  trusted to honor their existing consume-on-entry contracts; fault injection
  may only call the canonical primitive or semantically retire the locator and
  then raise. It may never return a non-exact handle or raise before retirement.
- [x] Success returns `(owner_lock, startup_writer)` containing the exact two
  existing handle types. Both retain their original move-only, safe-repr,
  non-inheritable, close-once contracts; the tuple retains no bootstrap,
  config, store, encoding, callback, extra raw locator scalar, or extra owner;
  the handles' own private descriptor fields are the intentional authorities.
  One private result-construction seam exists only to prove post-adoption
  failure cleanup; it accepts no caller callback and may return only the exact
  tuple in production.
- [x] Every reviewed post-admission success, ordinary failure, and synchronous
  seam interruption retires each locator exactly once: canonical-preflight
  failure attempts to close raw writer then raw owner; owner failure attempts
  to close the not-yet-adopted writer; writer
  failure closes the adopted owner after the writer primitive consumes its
  descriptor; and post-adoption result construction/admission failure closes
  adopted writer then owner. Cleanup never calls `LOCK_UN`, detach, dup, or an
  adopter out of order. Each raw or handle cleanup receives at most one close
  attempt; an ambiguous reported close is semantically retired and never
  retried or reported as proven physically closed.
- [x] Production captures canonical raw `os.close`, `OwnerLock.close`,
  `StartupWriter.close`, and unbound `BaseException.add_note` cleanup dispatch at
  import. Public attribute replacement or an exception's override cannot
  redirect P0-017 cleanup/note dispatch; private seams exist only to prove
  writer-before-owner order, ordinary/process-control precedence, ambiguous
  close, descriptor reuse, and note-attachment failure. Note failure never
  replaces the active process-control identity. No fallible work occurs after
  the exact result tuple is admitted.
- [x] Every ordinary validation, encoder, store, adoption, result-admission, or
  cleanup failure becomes exactly
  `RuntimeError("sidecar child descriptor adoption failed")`, raised `from
  None` only after internal failure and cleanup frames are gone. Without a
  caller-active exception its cause, context, and notes are empty. A caller's
  already-active Python-managed context may remain, but is suppressed from
  formatted output and production never reads or caches it. Fixed-error text
  and P0-017 traceback frame locals contain no bootstrap, config, path, store,
  encoding, handle, descriptor, errno, raw primitive error, or caught exception.
- [x] `KeyboardInterrupt`, `SystemExit`, and non-`Exception` control values
  synchronously raised by a captured operation preserve identity across every
  P0-017-owned cleanup path and stop all later adoption work. Only an exception
  explicitly caught during this invocation participates in cleanup arbitration;
  a caller's Python-managed baseline `sys.exception()` is never treated as the
  active failure, inspected, changed, or given a note. The invocation's active
  process-control exception wins over every composition cleanup failure;
  P0-017 itself best-effort adds at most one fixed
  `sidecar child descriptor adoption cleanup failed` note when its subsequent
  cleanup is not fully successful. Existing fixed owner-lock/startup-channel
  cleanup notes, dependency-specific process-control behavior, and primitive
  traceback frames remain permitted. A cleanup-originated process-control
  exception wins over an ordinary active failure; another composition cleanup
  failure attempts only the fixed P0-017 note. Hostile `add_note` overrides and
  invalid pre-existing `__notes__` cannot replace the preserved identity.
- [x] Caller-active `ValueError` and `KeyboardInterrupt` matrices cover success,
  ordinary failure, process-control failure, and cleanup failure. The baseline
  caller exception retains identity and notes unchanged. A fixed ordinary
  RuntimeError may retain only Python's suppressed caller-managed context; it
  never uses that context for cleanup precedence or note attachment.
- [x] Deterministic real-descriptor tests cover correct owner/writer adoption,
  wrong/swapped/closed/socket/regular/duplicate descriptors, every preflight
  and partial-adoption stage, descriptor-number reuse,
  ambiguous close, public dependency replacement, and ordinary/process-control
  failures using real same-process owner-lock and pipe duplicates. Existing
  unchanged P0-004 tests remain the owner-lock fork/exec continuity evidence;
  pair-after-exec composition belongs to the future entrypoint/launcher task.
- [x] Descriptor-boundary tests distinguish `2` (rejected untouched), a real
  descriptor at `3` (admitted and transferred), and `2147483647` (range-admitted
  then semantically retired after fixed failure without requiring the operating
  system to allocate that descriptor number).
- [x] Tests prove the adopter directly calls no `StateStore` mutation method,
  creates/deletes no path, and leaves any pre-existing state file's inode and
  bytes plus the canonical owner-lock inode unchanged. No output is emitted,
  and exact positive AST allowlists reject
  process/CLI/listener/state/READY/FastAPI/Uvicorn/SDK/storage/OTel/UI behavior,
  alternate adopters, caches, callbacks, dynamic calls, or unreviewed imports.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass locally and on the macOS/Linux x CPython 3.12/3.13 CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/sidecar/test_child_adoption.py tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
atomic child descriptor-adoption tests and all repository checks pass
```

## Risks

- Treating separate successful primitive calls as an atomic composition can
  leak the writer, release the owner too early, or leave the project locked
  after the second adoption fails.
- Closing or retrying the wrong integer after an adopter consumed it can close
  an unrelated descriptor that reused the same number.
- Failing to make the post-shallow-admission pair-consume boundary precede
  canonical encoder/store
  work would make one fixed error ambiguously mean either retained or consumed
  descriptors. After shallow admission, all failures must semantically retire
  both locators through transfer or a single cleanup attempt.
- Python cannot make multiple bytecode-level FD handoffs atomic against an
  arbitrary asynchronously delivered exception. Evidence is deliberately
  limited to synchronous captured-operation seams; process exit remains the
  backstop for fatal interruption.
- A copied encoder, mutable dispatch, or permissive bootstrap snapshot can let
  a forged config select a different project owner file.
- This slice proves child-side resource ownership only. It does not prove a
  child command, executable entrypoint, listener, state publication, READY,
  serving runtime, parent handoff, failure termination/reaping, or reload.

## Reviewer Focus

- Is every descriptor untouched before exact shallow admission and consumed
  or semantically retired after the pair-consume boundary, including
  encoder/store and owner-first/writer-second partial failures, without
  claiming physical closure after an ambiguous close report?
- Can cleanup replace process-control identity, retry an ambiguous FD, unlock
  the inherited open-file description, or retain a handle in a traceback?
- Can malformed bootstrap slots reach the ownership boundary, or can mutable
  public dispatch redirect encoder/store/adoption/cleanup after it?
- Did the task stop before entrypoint, process, listener, state, READY,
  Uvicorn, SDK, storage, OTel, reload, shutdown, and UI behavior?

## Role Outputs

Implementer:
- Added exact child-side bootstrap admission, captured canonical encoder/store
  preflight, owner-before-writer adoption, and one writer-first cleanup state
  machine for raw and adopted authorities. Success returns the admitted exact
  handle tuple immediately; ordinary failures cross only the fixed error
  boundary, while synchronous process control follows the reviewed precedence.
- Added deterministic descriptor, reuse, cleanup, caller-context, privacy,
  provenance, filesystem, and recursive AST evidence without adding a child
  command, process, listener, state publication, READY, or serving runtime.

Adversarial Reviewer:
- Reviewer 1: resource-state review found READY test scope creep, incomplete
  preflight/raw cleanup-control evidence, and external FD 3 inheritable-state
  corruption. The fixes removed READY behavior, completed the precedence
  matrix, restored FD 3 exactly, and received final P0/P1/P2 = 0 and GO.
- Reviewer 2: scope/privacy review found exact AST gaps for alternate adopters,
  mutable class/default state, and nested imports/classes/type aliases. The
  recursive allowlist now freezes every reviewed structure and call boundary;
  final review reported P0/P1/P2 = 0 and GO.
- Three independent planning reviews recommended descriptor adoption before a
  child entrypoint, command specification, or launcher because the unproved
  partial-adoption cleanup is their direct safety prerequisite.
- Contract reviewers found ownership-boundary ambiguity, impossible arbitrary
  async-interruption and physical-close guarantees, dynamic note dispatch,
  caller-baseline contamination, adopter-seam provenance gaps, overstated
  primitive traceback privacy, and redundant exec evidence. The revised card
  uses one post-shallow-admission pair-consume boundary, semantic retirement,
  synchronous captured-operation evidence, canonical unbound note dispatch,
  and exact primitive/scope exceptions. Two independent final reviews reported
  P0/P1/P2 = 0 and GO.
- Three independent implementation reviews found and closed READY test scope
  creep, external FD 3 inheritable-state corruption, missing raw-preflight and
  cleanup-control cases, asymmetric exactness matrices, and AST allowlist gaps
  for helpers, mutable state, nested imports/classes, calls, and defaults. All
  three final reviews reported P0/P1/P2 = 0 and GO.

Fixer:
- Codex primary selected the smaller descriptor-pair composition and deferred
  all process/runtime behavior. It moved canonical preflight inside a single
  post-admission ownership boundary, removed redundant exec evidence, trusted
  existing adopter consume contracts, and narrowed error/privacy claims.
- Every P0/P1 finding was accepted; no product allowlist or phase scope was
  expanded.
- The successful path was simplified to return the exact admitted tuple before
  any further callable work. All other accepted implementation findings
  strengthened tests and structural guards rather than broadening production.

Quality Governor:
- Final audit confirmed one Phase 0 ownership slice, complete direct
  dependencies, an open prerequisite gate, the exact four-file allowlist, and
  strict separation from entrypoint, process, listener, state, READY, Uvicorn,
  SDK, storage, OTel, reload, shutdown, and UI behavior. Contract GO.
- Final implementation audit confirmed the exact four-file product allowlist,
  fixed public surface, close-once/reuse semantics, exception privacy, frozen
  dispatch, and full-tree AST boundary. Implementation GO.

## Verifier Evidence

- Command: focused child-adoption/runtime-config tests; `make test-phase0`;
  `make check`; `make gate-phase0`; candidate GitHub Actions matrix
- Result: passed
- Notes: the final focused suite passed 390 tests, `make test-phase0` passed
  2,196 tests, and the final `make gate-phase0` ran the complete `make check`
  path with 2,321 passing tests before the `phase0-sustained` gate passed on
  local macOS CPython 3.13.5. Ruff format/lint, strict mypy,
  `scripts/validate_agent_system.py`, `git diff --check`, the staged-file
  allowlist, and an external non-inheritable FD 3 restoration probe passed.
  Implementation candidate
  `b544819cc73e08b4ad8810f2b58222c32d5a57b5` passed
  [run 29170380435](https://github.com/alovwang-sys/FlowSight/actions/runs/29170380435)
  with jobs `86590502043` (macOS 3.12), `86590502044` (Ubuntu 3.12),
  `86590502047` (macOS 3.13), and `86590502053` (Ubuntu 3.13). Planned contract
  commit `4f53fa10c86658d99891ac094b3c99d9a7d9424d` passed
  [run 29169104247](https://github.com/alovwang-sys/FlowSight/actions/runs/29169104247)
  with jobs `86587218440` (macOS 3.13), `86587218452` (macOS 3.12),
  `86587218457` (Ubuntu 3.12), and `86587218476` (Ubuntu 3.13). This is
  retained as task-system evidence only, not descriptor-adoption or Phase 0
  product acceptance. FSQ-0001 did not recur in the candidate matrix.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record. This task
  must not relax, skip, or selectively retry that benchmark.
