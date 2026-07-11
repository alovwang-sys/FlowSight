# Task: Verify One Fresh READY Startup

## Task Metadata

```yaml
task_id: P0-008
release: v1
task_type: implementation
status: in_progress
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
  This bypasses instance-injected callbacks and later class monkeypatches while
  keeping deterministic fault injection local to this module's tests.
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

- [ ] `verify_ready_startup()` accepts only exact `StateStore` and
  `StartupReady` objects plus an exact built-in `int` or `float` timeout. Wrong
  top-level types fail with fixed `TypeError` before field access, clock, load,
  or probe work.
- [ ] Timeout is finite, positive, and at most the P0-005 maximum. Timeout and a
  freshly reconstructed READY are validated before clock/load/probe work;
  forged exact READY fields fail with a fixed context-free `ValueError` that
  contains no caller value.
- [ ] One finite monotonic success-admission deadline starts before the first
  load. A positive remaining duration is required before the probe, before the
  second load, and after final comparison; no late result can succeed. The card
  and API do not claim to interrupt or hard-bound either synchronous load.
- [ ] The canonical unbound `StateStore.load` implementation is invoked exactly
  once for `first_loaded`, without calling an instance-injected `load` callback.
  A separate exact `first_copy` reconstructed through `to_wire`/`from_wire`
  must be distinct from and fully equal to `first_loaded`, validating its values
  while the exact `first_loaded` object is retained for the probe. `None`,
  ordinary failure, an inexact result, or failed revalidation returns `None`.
- [ ] READY must exactly match the first trusted state on startup ID, sidecar
  PID, and port. Any mismatch returns `None` with zero health probes and zero
  second loads.
- [ ] The P0-005 health probe is invoked exactly once with the exact
  `first_loaded` object and the current positive remaining timeout. Only an
  exact `True` may continue;
  `False`, an inexact result, ordinary failure, or deadline exhaustion returns
  `None` without a second load.
- [ ] After health succeeds within the admission deadline, the canonical load
  is invoked exactly once more to obtain `second_loaded`, which must not be the
  same object as `first_loaded`. A separate exact `second_copy` revalidates its
  values and must be distinct from and fully equal to `second_loaded`; missing,
  inexact, invalid, same-object, or ordinary failure evidence returns `None`.
- [ ] The two trusted states must be fully equal across every field, including
  token, project, startup ID, PID, port, database path, timestamp, host, and
  protocol/schema versions. A token-only or timestamp-only rotation fails even
  when every READY scalar still matches.
- [ ] Success returns the exact second loaded state, never the first or merely a
  reconstructed comparison copy. There is no third load, second probe, retained
  state/token cache, or hidden retry on any path.
- [ ] After caller preflight, every ordinary clock/load/revalidation/probe/
  comparison failure returns only `None`, with no log, output, repr, callback,
  retained exception, or secret detail. `KeyboardInterrupt` and `SystemExit`
  preserve identity at every stage; this wrapper adds no note, while an existing
  fixed cleanup note added inside P0-005 remains allowed.
- [ ] Real published-state and bounded loopback-health composition plus
  deterministic call-order/race matrices prove `load -> probe -> load`, exact
  early exits, full-state rotation rejection, and second-object return identity.
  The only result remains `SidecarState | None`; static scope evidence proves the
  module returns or grants no election, cleanup, or launch authority and
  imports/calls no such behavior. Caller policy remains outside this task.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
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
- Calling an injected instance method can execute unknown caller behavior;
  production must retain the canonical load callable.
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
- Notes: proves one fresh READY verification window only, never election or
  process authority

## Failure Queue Items

- none
