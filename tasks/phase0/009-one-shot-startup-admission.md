# Task: Admit One Startup Outcome

## Task Metadata

```yaml
task_id: P0-009
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-006, P0-007, P0-008, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-009

## Phase

Phase 0

## Goal

After successful caller preflight and deadline setup, consume one parent-owned
`StartupReader` and return only an exact safe child-reported `StartupFailure`,
an exact `SidecarState` admitted through P0-008, or `None` when READY was not
proven.

## Context

- P0-006 provides one move-only startup reader whose canonical `receive()`
  validates one bounded signal, consumes handle ownership, and makes at most
  one descriptor-cleanup attempt without retrying an ambiguous close. READY
  remains a wake-up hint, while `StartupFailure` contains only one fixed safe
  code.
- P0-008 admits an already-decoded exact READY only after fresh
  `load -> authenticated health probe -> load` evidence and returns the exact
  second state. It deliberately does not own or consume a startup channel.
- This slice composes those two primitives without accepting a process handle.
  It therefore cannot inspect or clean up a child, transfer an owner lock,
  launch/retry, recover stale state, or decide election policy.
- The fixed public shape is `receive_startup_outcome(store, reader,
  timeout=0.5) -> SidecarState | StartupFailure | None`.
- A fixed `StartupAdmissionError` with one non-sensitive
  `STARTUP_ADMISSION_DEADLINE_FAILED` code reports ordinary failure while
  creating the outer deadline or its current pre-transfer receive budget. It is
  raised before transfer, so the caller still owns the reader. This task does
  not reuse a channel error for a failure that happened before channel receive.
- Exact top-level and timeout validation plus reader-handle validation happen
  while the caller still owns the reader. Deadline setup also precedes the
  ownership boundary. The logical move-in linearizes only when the sole
  canonical `StartupReader.receive()` invocation invalidates the public handle;
  P0-006 does so before its fallible transport work, then makes at most one
  descriptor-cleanup attempt without retrying an ambiguous close. Entering the
  method alone is not claimed as transfer.
- P0-009 does not broaden P0-006 to concurrent use, runtime replacement of its
  internal method graph, or arbitrary asynchronous interruption before P0-006
  invalidates the handle. A future caller must keep the call inside
  `with reader:` (or an equivalent active-exception-aware P0-006 cleanup path):
  the context closes a still caller-owned reader, is a no-op after canonical
  receive consumed it, and preserves the active exception with only P0-006's
  fixed cleanup note if cleanup itself is ambiguous. A naked exception-path
  `reader.close()` is not claimed to preserve the active exception.
- One outer monotonic success-admission deadline is shared with the channel and
  READY verifier. It prevents a late `SidecarState` success but is not a hard
  interruptible bound over P0-008's synchronous state loads. A decoded exact
  `StartupFailure` is negative evidence and remains returnable without a final
  success-deadline check.
- Exact P0-006 `StartupChannelError` values originating from canonical reader
  preflight or receive remain visible structured channel failures. Other
  ordinary collaborator/post-receive clock/verification failures collapse
  privately to `None`; `KeyboardInterrupt` and `SystemExit` preserve identity.
- Source of truth: MVP design section 4.2 and Phase 0, plus TRIAL-004 promotion
  requirements.

## Related Fact IDs

- FS-008

## Allowed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_admission.py`
- `tests/sidecar/test_startup_admission.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/startup_admission.py`
- `tests/sidecar/test_startup_admission.py`

## Forbidden

The following restrictions apply to production. Focused tests may open the
existing P0-006 anonymous channel, publish a P0-007 state as a private temporary
fixture, and use one bounded `127.0.0.1:0` health-server thread with observable
readiness and deterministic cleanup. They may not start a subprocess.

- Do not accept, inspect, poll, wait for, signal, terminate, kill, or reap a
  child/process handle, and do not construct a command, environment, `Popen`,
  `pass_fds`, session, or reaper.
- Do not open a startup channel, accept a `StartupWriter`, adopt an inherited
  descriptor, directly read/close/retry the reader descriptor, or add a second
  receive. P0-006 remains the sole channel framing, handle invalidation, and
  descriptor-cleanup implementation.
- Do not acquire/adopt/close an `OwnerLock`, elect, wait for contention,
  re-contend, transfer launch authority, recover/remove stale state, or treat a
  non-state result as permission for any such action.
- Do not bind or mutate a listener, start Uvicorn/ASGI, publish/remove state,
  perform an extra state load or health probe outside P0-008, or add polling,
  retry, backoff, loop, sleep, callback, or background work.
- Do not add SQLite, writer, queue, sender, lease, OTel, SDK lifecycle, reload,
  idle-stop, UI, browser, or trace behavior.
- Except for the existing fields on the exact verified `SidecarState` result,
  do not expose a token, path, PID, file descriptor, errno, raw channel frame,
  exception detail, or caller value in an error, repr, log, output, callback,
  separate result, or module cache.
- Do not import spike code or change the P0-001/P0-006/P0-008 contracts.

## Acceptance Criteria

- [ ] `receive_startup_outcome()` accepts only exact `StateStore` and
  `StartupReader` objects plus an exact built-in `int` or `float` timeout.
  Wrong top-level types and invalid/non-finite/non-positive/over-limit timeouts
  fail with fixed errors before reader field access, clock, receive, or verify.
- [ ] `receive_startup_outcome`, `StartupAdmissionError`, and
  `StartupAdmissionErrorCode` are identical exports from `flowsight.sidecar`
  and appear exactly once in its `__all__`. The function signature is exactly
  `(store: StateStore, reader: StartupReader, timeout: float = 0.5) ->
  SidecarState | StartupFailure | None`; the error constructor accepts one
  exact `code` argument and no dynamic detail.
- [ ] `StartupAdmissionErrorCode` contains only
  `STARTUP_ADMISSION_DEADLINE_FAILED`; `StartupAdmissionError` accepts only that
  exact code and exposes a fixed message/code with no caller value, cause, or
  context. No other new error category is introduced.
- [ ] Reader preflight uses the captured canonical `StartupReader.fileno`
  entrypoint, requires an exact built-in descriptor at least `3`, and emits only
  fixed safe errors. Exact P0-006 errors from this direct entrypoint preserve
  identity; an inexact/missing/below-`3` descriptor or any other ordinary
  malformed-reader failure raises exact context-free
  `ValueError("reader is invalid")`. A valid reader remains caller-owned and can
  complete a real later channel roundtrip after any preflight or deadline-setup
  failure.
- [ ] Production captures canonical unbound `StartupReader.fileno` and
  `StartupReader.receive` and canonical `verify_ready_startup` callables at
  import and invokes them only through separately patchable private helpers.
  This freezes the wrapper's direct dispatch only. The frozen `receive`
  implementation may still call its trusted P0-006 internal method graph
  dynamically; P0-009 neither freezes that graph nor tests runtime replacement
  of an internal method as a supported case.
- [ ] Each monotonic observation is an exact finite built-in `float`, never
  regresses from the prior observation, and cannot produce a non-finite or
  non-increasing deadline. A fixed context-free `StartupAdmissionError` raised
  during any pre-transfer deadline or remaining-budget observation leaves the
  reader caller-owned; process-control exceptions preserve identity at those
  same boundaries. Tests prove both initial and immediate pre-receive clock
  failures, rollback, expiry, and overflow with zero receive/verify work.
- [ ] One monotonic success-admission deadline starts before ownership transfer.
  A current positive remaining budget is passed to the sole receive; after an
  exact READY, a fresh positive non-regressed remainder is passed to P0-008 and
  a final positive non-regressed remainder is required before state success can
  return. Failure, expiry, or rollback after receive never starts extra work.
- [ ] The canonical reader receive is invoked at most once. Invocation entry
  alone is not claimed to have transferred ownership. On supported
  paths after P0-006 invalidates the public handle, READY, explicit failure,
  structured channel error, ordinary failure, or process-control interruption
  leaves that handle consumed. P0-006 makes at most one OS close attempt; an
  ambiguous close is never retried or misreported as proven physical closure.
  An exception before invalidation leaves caller cleanup responsible, and
  P0-009 never directly closes or retries a descriptor.
- [ ] An exact valid `StartupFailure` is independently reconstructed as a
  distinct equal copy and the exact received object is returned unchanged with
  zero READY verifications, state loads, or health probes. Forged, derived, or
  otherwise inexact failure values return `None` without exposing their fields.
- [ ] An exact `StartupReady` is independently reconstructed as a distinct equal
  copy before P0-008. Only a valid exact READY reaches the frozen canonical
  verifier, exactly once, using the exact received object and current positive
  remaining budget. Inexact, derived, forged, or unknown channel results return
  `None` with zero verifier calls, state loads, or health probes.
- [ ] Only an exact valid `SidecarState` returned by P0-008 can succeed. A
  separately reconstructed exact copy must be distinct and fully equal; the
  exact verifier result is returned only before the outer deadline. `None`,
  ordinary failure, derived/inexact/schema-invalid/nonfresh/unequal result, or
  late state returns `None`. Provenance is supplied by the frozen canonical
  P0-008 callable; reconstruction validates schema/value integrity but does not
  claim to prove collaborator provenance from values alone.
- [ ] Exact P0-006 `StartupChannelError` values originating only from the direct
  canonical fileno/receive stages preserve identity/code/message/cause/context
  and any existing fixed cleanup note. An identical or derived error injected
  by clock, message/state reconstruction, comparison, or READY verification is
  treated as an ordinary collaborator failure, never misreported as a channel
  failure.
- [ ] Other ordinary post-receive clock/collaborator failures return only
  `None` without logs, output, retained exception, secret detail, or another
  public error taxonomy. A normal return always follows the sole receive, so
  `None` never leaves the reader caller-owned.
- [ ] `KeyboardInterrupt` and `SystemExit` preserve identity at reader
  validation, deadline, receive, message reconstruction, READY verification,
  state reconstruction/comparison, and final admission. This wrapper adds no
  note; only P0-006's existing fixed channel-cleanup note may remain.
- [ ] One unpatched full READY composition runs public API -> real P0-006 pipe
  -> real P0-008 -> two real temporary state reads around one bounded real
  loopback health response, then proves exact state identity, reader invalidation,
  stable state metadata, dead helper thread, deterministic socket/writer cleanup,
  and no token output. A separate real failure-signal roundtrip returns exact
  safe failure evidence with zero verifier work.
- [ ] Deterministic unit matrices prove exact order/counts, outer deadline and
  ownership boundaries, structured-error provenance, and no hidden retry.
  Static AST evidence fixes exactly one canonical receive call site and one
  verifier call site; zero direct close/read/open/load/probe/publish/remove,
  statements or comprehension loops, mutable global/nonlocal cache writes,
  policy imports, or extra public functions prove no process/election/cleanup/
  state-mutation/retry authority entered production.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
pytest tests/sidecar/test_startup_admission.py
make test-phase0
make check
```

Expected result:

```text
one-shot startup admission tests and all repository checks pass
```

## Risks

- Folding an explicit child failure into `None` would discard an existing safe,
  structured negative result; treating either result as election or cleanup
  authority could create a split brain or kill an unrelated process.
- Losing reader ownership around the admission deadline can leak the pipe or
  close/retry a reused descriptor. Pre-transfer failures must remain exceptions,
  the future caller must use P0-006's active-exception-aware context cleanup,
  and P0-006 must stay the only handle invalidation/descriptor-cleanup
  implementation after transfer.
- Giving receive and READY verification separate full timeouts can admit a late
  startup; claiming a hard total timeout over synchronous state loads would
  promise behavior this slice cannot enforce.
- Pulling process polling/termination or owner-lock handoff into this wrapper
  would mix evidence admission with lifecycle policy and make cleanup races
  impossible to review independently.

## Reviewer Focus

- Can a derived/forged message, verifier result, late state, hidden retry, or
  second receive pass?
- Is the reader left with exactly one clear owner on every preflight, ordinary,
  structured-error, and process-control path without direct descriptor cleanup
  in this wrapper?
- Apart from the exact verified state result itself, can any token, path, PID,
  descriptor, errno, frame, raw exception, or caller callback reach an error,
  repr, output, log, cache, or side result?
- Did this remain only channel-to-READY admission with no process, lock,
  election, stale cleanup, state mutation, runtime, or SDK policy?

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
- Notes: proves one startup-channel outcome admission only, never child,
  election, cleanup, or launch authority

## Failure Queue Items

- none
