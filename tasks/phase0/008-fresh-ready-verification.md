# Task: Verify One Fresh READY Startup

## Task Metadata

```yaml
task_id: P0-008
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-005, P0-006, P0-007, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-008

## Phase

Phase 0

## Goal

Return the exact second freshly loaded and revalidated published `SidecarState`
only when an already-decoded READY hint matches a first trusted state load, one
authenticated health probe, and a second fully equal trusted state load.

## Context

- P0-006 defines READY as a typed wake-up hint, never proof of health or
  election authority. It requires a future consumer to compare fresh state and
  run the P0-005 authenticated health probe.
- P0-001 `StateStore.load()` intentionally folds missing, invalid, untrusted,
  and internal storage-read failure into `None`; P0-005 similarly returns
  `False` for ordinary non-health evidence. This wrapper cannot and must not
  invent a more detailed reason.
- P0-007 supplies the exact generated startup-state grammar; P0-008 tests are
  narrowly authorized to publish that state as a private temporary fixture for
  full-equality composition evidence.
- Production captures `StateStore.load` once at module import as a private
  unbound callable and invokes it through a separately patchable private helper.
  This bypasses an instance-injected `load` callback and a later replacement of
  `StateStore.load` while keeping deterministic fault injection local to this
  module's tests. P0-008 trusts the internal method graph already owned by
  P0-001; freezing that graph would require a separate P0-001 hardening task.
- The fixed public shape is `verify_ready_startup(store, ready, timeout=0.5) ->
  SidecarState | None`. `None` means only “this READY was not proven”; it never
  means “no sidecar exists” and never grants election, stale cleanup, retry,
  child cleanup, or process-launch authority.
- `timeout` is a success-admission deadline, not a hard interruptible wall-clock
  bound. Both state loads are synchronous filesystem calls and can return after
  the deadline; this primitive can only reject a late result. The one health
  probe receives the remaining positive budget and owns its existing bounded
  network deadline.
- Success proves only one observation window: equal trusted state snapshots
  bracket one authenticated health result matching READY. It does not prove
  inode continuity, exclude an ABA replacement, prove owner-lock continuity or
  child provenance, or promise health after return.
- Source of truth: MVP design section 4.2 and Phase 0, plus the TRIAL-004
  promotion requirements.

## Related Fact IDs

- FS-007
- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_verification.py`
- `tests/sidecar/test_startup_verification.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_verification.py`
- `tests/sidecar/test_startup_verification.py`

## Forbidden

The following restrictions apply to production. Focused tests may call the
existing `StateStore.publish()` only to create a private temporary fixture and
may use one bounded helper thread serving `127.0.0.1:0`, with observable
readiness, deadlines, and deterministic cleanup, to prove real composition.
That helper is test evidence, never lifecycle implementation.

- Do not read from or close a startup channel, accept `StartupFailure`, or add
  parent/child handle policy.
- Do not create, publish, repair, replace, remove, or poll state; at most two
  exact read-only `StateStore.load` calls are permitted: one before the probe
  and one additional call only after an exact `True`.
- Do not acquire/release an owner lock, elect, recover stale state, authorize
  attach/launch, or decide child cleanup.
- Do not perform a second health probe or add retry, polling, backoff, loop,
  sleep, callback, or background work.
- Do not add a separate HTTP client, listener, bind, Uvicorn/ASGI runtime,
  SQLite handle, thread, process, queue, sender, SDK, OTel, UI, or browser.
- Do not inspect PID liveness or spawn, poll, wait, signal, terminate, kill, or
  reap a process.
- Except for returning the verified state and P0-005's internal bearer use, do
  not place a token, path, PID, errno, raw exception, or detailed reason in an
  error, repr, log, output, callback, separate return value, or module cache.
- Do not import spike code or change the P0-001/P0-005/P0-006/P0-007 contracts.

## Acceptance Criteria

- [x] `verify_ready_startup()` accepts only exact `StateStore` and
  `StartupReady` objects plus an exact built-in `int` or `float` timeout. Wrong
  top-level types fail with fixed `TypeError` before field access, clock, load,
  or probe work.
- [x] Timeout is finite, positive, and at most the P0-005 maximum. Timeout and a
  freshly reconstructed READY are validated before clock/load/probe work;
  forged exact READY fields fail with a fixed context-free `ValueError` that
  contains no caller value.
- [x] One finite monotonic success-admission deadline starts before the first
  load. A positive remaining duration is required before the probe, before the
  second load, and after final comparison; no late result can succeed. The card
  and API do not claim to interrupt or hard-bound either synchronous load.
- [x] The canonical unbound `StateStore.load` implementation is invoked exactly
  once for `first_loaded`, without calling an instance-injected `load` callback.
  A separate exact `first_copy` reconstructed through `to_wire`/`from_wire`
  must be distinct from and fully equal to `first_loaded`, validating its values
  while the exact `first_loaded` object is retained for the probe. `None`,
  ordinary failure, an inexact result, or failed revalidation returns `None`.
- [x] READY must exactly match the first trusted state on startup ID, sidecar
  PID, and port. Any mismatch returns `None` with zero health probes and zero
  second loads.
- [x] The P0-005 health probe is invoked exactly once with the exact
  `first_loaded` object and the current positive remaining timeout. Only an
  exact `True` may continue;
  `False`, an inexact result, ordinary failure, or deadline exhaustion returns
  `None` without a second load.
- [x] After health succeeds within the admission deadline, the canonical load
  is invoked exactly once more to obtain `second_loaded`, which must not be the
  same object as `first_loaded`. A separate exact `second_copy` revalidates its
  values and must be distinct from and fully equal to `second_loaded`; missing,
  inexact, invalid, same-object, or ordinary failure evidence returns `None`.
- [x] The two trusted states must be fully equal across every field, including
  token, project, startup ID, PID, port, database path, timestamp, host, and
  protocol/schema versions. A token-only or timestamp-only rotation fails even
  when every READY scalar still matches.
- [x] Success returns the exact second loaded state, never the first or merely a
  reconstructed comparison copy. There is no third load, second probe, retained
  state/token cache, or hidden retry on any path.
- [x] After caller preflight, every ordinary clock/load/revalidation/probe/
  comparison failure returns only `None`, with no log, output, repr, callback,
  retained exception, or secret detail. `KeyboardInterrupt` and `SystemExit`
  preserve identity at every stage; this wrapper adds no note, while an existing
  fixed cleanup note added inside P0-005 remains allowed.
- [x] Real published-state and bounded loopback-health composition plus
  deterministic call-order/race matrices prove `load -> probe -> load`, exact
  early exits, full-state rotation rejection, and second-object return identity.
  The only result remains `SidecarState | None`; static scope evidence proves the
  module returns or grants no election, cleanup, or launch authority and
  imports/calls no such behavior. Caller policy remains outside this task.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_startup_verification.py
make test-phase0
make check
```

Expected result:

```text
fresh READY verification tests and all repository checks pass
```

## Risks

- Trusting READY without a fresh state match and authenticated probe can attach
  to stale or unrelated local state.
- Comparing only READY fields after health can miss token/database/start-time
  rotation during the observation window.
- Treating `None` as election permission can create a split brain; only a later
  owner-lock policy task may decide what happens after verification fails.
- Calling an injected instance `load` callback can execute unknown caller
  behavior; production must retain the canonical `StateStore.load` entrypoint.
- Claiming a hard total timeout over synchronous filesystem calls would create
  a guarantee this slice cannot enforce.

## Reviewer Focus

- Can mismatch, non-health, state rotation, same-object reuse, malformed
  collaborator output, or a late result pass?
- Can any early failure reach the network or a second load, or can any path
  retry/reorder the exact `load -> probe -> load` sequence?
- Can token, path, PID, errno, raw exception, or caller callback enter an error,
  repr, output, log, global cache, or retained exception?
- Did this remain one read-only verification window with no election, state
  mutation, channel ownership, process, runtime, or lifecycle policy?

## Role Outputs

Implementer:
- Added and exported one read-only `verify_ready_startup()` window with exact
  caller preflight, a monotonic success-admission deadline, a frozen canonical
  `StateStore.load` entrypoint, one P0-005 health probe, two independently
  revalidated state loads, full-state equality, and exact second-object return.

Adversarial Reviewer:
- Reviewer 1: narrowed an overbroad canonical-load claim to the exact frozen
  entrypoint boundary, found a READY-mismatch test that could falsely report
  zero probes, and confirmed the direct probe counter closes it. Final review
  reported P0=0/P1=0/P2=0.
- Reviewer 2: found false-positive gaps in timeout errors, READY attribute
  failures, revalidation collaborator results, forged-state ordering, and
  loopback setup cleanup. After fixes, final review reported P0=0/P1=0/P2=0.

Fixer:
- Applied every accepted finding. The 135-test matrix now fixes exact error
  types/messages, READY ordinary/process-control behavior, raw/derived/unequal
  revalidation rejection at both load positions, direct zero-probe mismatch
  evidence, full-field rotation, and deterministic whole-lifetime loopback
  cleanup. No finding was deferred.

Quality Governor:
- Candidate `84015ba` changes exactly the three allowlisted product/test files.
  Static and behavioral evidence keep the slice to one read-only verification
  window with no election, cleanup, launch, state mutation, channel ownership,
  retry, background work, or new authority result. The sustained gate and all
  four supported CI combinations are green.

## Verifier Evidence

- Command: focused startup-verification tests; `make test-phase0`;
  `make gate-phase0` (including full `make check`); pre-commit
  `make check-fast`; candidate GitHub Actions matrix
- Result: passed
- Notes: focused tests passed 135/135 on local CPython 3.13;
  `make test-phase0` passed 1,021 tests; the final full check passed 1,146 tests
  plus formatting, lint, typing, and agent checks, followed by the sustained
  Phase 0 gate. Candidate
  `84015ba1709e13f1db3f4e8e0b2ec512a84f3eac` passed
  [run 29150056354](https://github.com/alovwang-sys/FlowSight/actions/runs/29150056354):
  Ubuntu 3.12 job `86537998477`, Ubuntu 3.13 job `86537998482`, macOS 3.13 job
  `86537998483`, and macOS 3.12 job `86537998489`. The real composition test
  exercises P0-007 state generation/publication, P0-005 authenticated loopback
  health, and P0-001 fresh reads on Darwin and Linux. The full check correctly
  retains the partial-scaffold limitation. This evidence proves one fresh READY
  verification window only; it does not grant election, stale cleanup, launch,
  child ownership, or complete Phase 0 acceptance.

## Failure Queue Items

- none
