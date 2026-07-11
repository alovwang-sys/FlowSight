# Task: Claim the Project Owner-Lock Descriptor

## Task Metadata

```yaml
task_id: P0-004
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-003, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-004

## Phase

Phase 0

## Goal

Provide the project-scoped, long-lived owner-lock descriptor primitive that a
future launcher can acquire and an exec'd sidecar can strictly claim without an
unlock window.

## Context

- P0-001 established the descriptor-anchored project directory and reserved
  `sidecar-owner.lock`, but deliberately stopped before a long-lived owner lock.
- TRIAL-004 proved that `flock` ownership on the same open-file description can
  survive `fork`/`exec` and parent close while a child retains its inherited
  duplicate.
- This slice implements acquisition and inherited-descriptor claiming only.
  Production does not spawn a process or perform election orchestration here;
  a real `subprocess` + `pass_fds` test supplies continuity evidence.
- Source of truth: MVP design sections 4.2, 4.4, 7.4, and Phase 0.

## Related Fact IDs

- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/owner_lock.py`
- `flowsight/sidecar/state.py`
- `tests/sidecar/test_owner_lock.py`

The current task card and its verifier evidence are always writable control-plane records.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/owner_lock.py`
- `flowsight/sidecar/state.py`
- `tests/sidecar/test_owner_lock.py`

## Forbidden

- Do not start a child process, Uvicorn, listener, thread, queue, or SQLite
  connection in production code. Subprocesses are test-only continuity probes.
- Do not add election polling/retry, healthy-state checks, stale recovery,
  launcher orchestration, ready pipes, startup ACK/timeouts, or process stop.
- Do not publish/remove sidecar state or add producer leases, sender/ingest,
  OTel, trace, tracepoint, UI, or storage behavior.
- Do not expose `unlock`, `LOCK_UN`, detach, or a public inheritable toggle. A
  parent relinquishes its duplicate only with close after a future child-ready
  boundary.
- Do not unlink the persistent owner-lock file or import production behavior
  from `spikes/sidecar_otel`.

## Acceptance Criteria

- [ ] `OwnerLock.acquire()` accepts an exact `StateStore`, descriptor-anchors
  creation/opening of the fixed project owner-lock file, and requires a regular
  empty current-user `0600` file with one link and `O_RDWR` access.
- [ ] Acquisition is a single `LOCK_EX | LOCK_NB` attempt. Genuine contention
  returns fixed `OWNER_LOCK_HELD`; all other filesystem/flock failures return a
  separate fixed non-sensitive error and never repair or follow an unsafe file.
- [ ] A successful handle owns one non-inheritable descriptor, has a fixed safe
  repr, exposes only `fileno()`, and provides idempotent close/context-manager
  cleanup without ever calling `LOCK_UN` or unlinking the lock file.
- [ ] `OwnerLock.adopt_inherited()` consumes an exact built-in descriptor at
  least `3`, then verifies regular/current-user/`0600`/single-link/empty/
  `O_RDWR`, exact canonical lock inode and project directory, and restores
  non-inheritable status before returning.
- [ ] Adoption proves continuity rather than acquiring a new lock: an
  independently opened canonical probe must be blocked while reasserting
  `LOCK_EX | LOCK_NB` on the candidate succeeds. Unheld, shared-lock, separate
  open-file-description, wrong-inode, replaced-path, pipe/socket/directory,
  closed, and malformed descriptors fail closed and are consumed exactly once.
- [ ] A real exec + `pass_fds` test proves gapless continuity: after parent
  close, a third process remains blocked while the child holds the inherited
  descriptor; only the child's last close allows the same persistent inode to
  be acquired again. Independent exec contenders prove exactly one winner.
- [ ] Every allocated/consumed descriptor is closed at most once on failure.
  Close errors become fixed visible cleanup failures without retrying an
  ambiguous integer; `KeyboardInterrupt`/`SystemExit` propagate with only fixed
  cleanup notes where necessary.
- [ ] Public errors have exact fixed messages/codes and expose no path, file
  descriptor, inode, errno, raw OS text, cause, context, or sensitive note.
- [ ] Focused owner-lock tests, `make test-phase0`, and full repository checks
  pass on the supported OS/Python matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_owner_lock.py
make test-phase0
make check
```

Expected result:

```text
owner-lock descriptor and exec-continuity tests plus all repository checks pass
```

## Risks

- Calling `LOCK_UN` on the parent's duplicate would release the lock shared by
  the child open-file description and create a split-brain window.
- A successful `flock(fd)` alone does not prove the FD is the canonical project
  lock or that it was already held before exec.
- Default-fork tests can accidentally share a lock description and make two
  supposed contenders appear mutually exclusive; independent contenders must
  exec without inheriting the owner FD.
- Closing a descriptor and later retrying the same integer after a reported
  close error can close an unrelated reused FD.
- Owner-lock continuity alone is not election or a healthy singleton; state/
  health double-check and runtime readiness remain later tasks.

## Reviewer Focus

- Can any non-canonical, unheld, shared, separately opened, insecure, or swapped
  descriptor pass inherited claiming?
- Does acquisition distinguish contention from storage failure without leaking
  raw errors?
- Can cleanup unlock the child's shared open-file description, close an FD
  twice, or act on a reused integer?
- Do real exec tests prove both independent contention and gapless last-close
  handoff without sleeps?
- Does production remain a descriptor primitive with no hidden runtime or
  election orchestration?

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
- Notes: proves owner-lock descriptor continuity only, not a running singleton

## Failure Queue Items

- none
