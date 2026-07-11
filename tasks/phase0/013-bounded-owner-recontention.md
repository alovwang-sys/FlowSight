# Task: Retry Owner Election Within One Bounded Window

## Task Metadata

```yaml
task_id: P0-013
release: v1
task_type: implementation
status: complete
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
- Codex primary, acting as task owner, approved the post-activation wording
  clarification that only contention with positive remaining budget is admitted
  to wait, plus the explicit `make check` verification line. This changes no
  goal, allowed file, observable behavior, phase boundary, or scope override.
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

- [x] `wait_for_owner_election()` accepts only an exact `StateStore` and exact
  built-in `int` or `float` timeout. Wrong types and invalid/non-finite/
  non-positive/over-30-second values fail with P0-012's fixed preflight messages
  before clock, election, wait, or retry work.
- [x] `wait_for_owner_election` is an identical `flowsight.sidecar` export,
  occurs exactly once in `__all__`, and has the exact signature
  `(store: StateStore, timeout: float = 0.5) -> SidecarState | OwnerLock`.
  This task adds no public result wrapper, `None`, boolean, callback, error
  type, error code, or poll-cadence argument.
- [x] Production captures canonical `resolve_owner_election`, monotonic clock,
  and wait operation at import and calls them only through private test seams.
  Replacing public attributes later cannot change direct dispatch. Private seam
  replacement remains fault injection, not a provenance/security boundary.
- [x] After preflight, one exact finite non-regressing monotonic deadline bounds
  all retry admission and waiting. Before every election and wait there is a
  positive remaining budget; each election receives that current budget. An
  ordinary clock `Exception`, inexact/non-finite observation, rollback, overflow,
  insufficient elapsed time across a completed wait, or expiry raises exact
  `OWNER_ELECTION_DEADLINE_FAILED` with no later election/wait.
- [x] The first canonical P0-012 election runs immediately with no wait. An
  exact `SidecarState` or `OwnerLock` result is returned unchanged and ends the
  operation. The wrapper does not read fields, call `fileno`, close/release the
  owner, or make another clock/election/wait call after success.
- [x] Only an exact `OwnerLockError` carrying exact
  `OwnerLockErrorCode.OWNER_LOCK_HELD` enters the wait path. Every other exact
  P0-004 or P0-012 error preserves identity/code/message/cause/context with zero
  wait/retry; derived, malformed, or ordinary private-seam failure becomes the
  fixed `OWNER_ELECTION_FAILED`. Every non-`Exception` `BaseException` from the
  clock, election, or wait preserves identity and ends the operation with no
  later clock/election/wait.
- [x] Each contention admitted to waiting with a positive remaining budget
  performs one wait of exactly
  `min(0.025, current_remaining)` seconds through the frozen operation. The
  interval is always a positive built-in float. Exact `None` permits another
  retry only when the first post-wait observation proves elapsed monotonic time
  is finite and at least the requested interval, computed by checked
  subtraction, and the outer deadline remains positive. That same observation
  and remaining budget admit the next election without an intervening clock
  read. Ordinary/malformed wait failure becomes exact
  `OWNER_ELECTION_FAILED`, while process-control preserves identity.
- [x] Repeated contention cannot busy-loop or outlive the cooperative retry
  window: an early/no-op wait fails deadline admission even if the clock moved
  slightly, each iteration recomputes remaining time, and expiry reports exact
  deadline failure rather than the last contention object. Consumed contention
  is folded to a boolean outside its `except` suite; a later deadline/wait error
  carries no cause, context, note, callback, or retained reference to that
  contention object. A caller's already-active Python-managed `__context__` may
  remain, but fixed errors raised `from None` suppress it from formatted output.
  No path performs an extra wait/election after expiry.
- [x] Deterministic matrices cover immediate incumbent/winner, contention then
  incumbent/winner, repeated contention to expiry, every clock boundary,
  poll-capping, strict progress, exact error taxonomy/identity, malformed seam
  values, process-control, call order/budgets, no retained contention, and no
  retention after restoring seams. The clock matrix proves checked finite
  elapsed subtraction and zero redundant observation between a successful wait
  admission and its retry. One case proves an attempt admitted before the outer
  deadline may return an exact success after that deadline because P0-012 owns
  per-attempt success admission and the outer timeout is not a hard wall-clock
  interrupt. Tests use no sleep.
- [x] A real temporary-lock test starts with one canonical owner held, reaches
  an observable bounded wait, releases that owner without a sleep race, and
  proves the waiter returns one exact active owner that blocks another canonical
  acquire until caller close. A separate injected sequence proves state
  published during contention is returned unchanged.
- [x] Static evidence permits only validation, clock/deadline arithmetic,
  canonical P0-012 election, one fixed 25 ms wait, fixed errors, and the bounded
  retry loop. It rejects state/process/listener/channel/SQLite/runtime/SDK calls,
  owner inspection/cleanup/adoption, logging/output, callbacks, mutable state,
  comprehensions, async work, and caches without copying P0-012 internals.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/sidecar/test_startup_wait.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
bounded owner re-contention tests and all repository checks pass
```

## Risks

- Treating any negative discovery or malformed error as retry authority can
  create split brain. Only exact canonical lock contention may wait.
- A wait that returns before its full requested interval can busy-loop under a
  fast-returning seam; full-interval monotonic progress fails closed.
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
- Codex primary implemented the fixed public bounded-wait composition in
  candidate `b107a15`: exact preflight, frozen canonical election/clock/wait
  dispatch, one 25 ms capped poll, checked full-interval progress, direct reuse
  of the post-wait remaining budget, and exact success/error transfer. The
  implementation adds no process, state mutation, descriptor handoff, runtime,
  SQLite, SDK, or telemetry behavior.
- Replacement candidate `ce76bcb` restricts wait/error propagation to explicit
  registered enum-member identities, so exact-type phantom `StrEnum` instances
  with canonical-looking or private values become fixed election failure.

Adversarial Reviewer:
- Reviewer 1: pre-implementation contract review found early-return busy-loop,
  process-control classification, and consumed-contention exception-chain gaps.
  After full-interval elapsed, exact `BaseException`, context-isolation, and
  honest outer-deadline evidence were added, final P0/P1/P2 = 0 and GO.
- Reviewer 2: implementability review verified exact-error bare re-raise,
  fixed-error creation outside `except`, checked elapsed subtraction, and direct
  reuse of the first post-wait budget. Final P0/P1/P2 = 0 and GO.
- Reviewer 3: production adversary verified malformed/missing error-code
  normalization, consumed-contention isolation, full-interval progress,
  decreasing budgets, process-control identity, frozen dispatch, and move-only
  owner transfer. Final P0/P1/P2 = 0 and GO.
- Reviewer 4: test adversary found a nested-function AST false-green, one
  literal `sleep(0)` evidence violation, and a missing exact-int timeout branch.
  After fixes, the suite proves every function is top-level, uses identity plus
  AST evidence without sleeping, and covers `31` plus a huge exact integer.
  Final P0/P1/P2 = 0 and GO.
- Reviewer 5: error adversary reproduced an exact-type phantom `StrEnum` member
  that was not a registered canonical error code. Candidate `b107a15` bare
  re-raised it and exposed arbitrary code text. Replacement `ce76bcb` uses
  complete registered-member identity allowlists; first-attempt and retry
  phantom matrices pass. Final P0/P1/P2 = 0 and GO.
- Reviewer 6: static adversary found the first identity evidence was
  alias/dataflow-blind and did not prove enum-member completeness. The final
  test binds every `error.code` read directly and uniquely to its inspected
  variable, restricts every use to exact identity checks, and freezes the full
  registered enum tuples. Final P0/P1/P2 = 0 and GO.

Fixer:
- Codex primary accepted all planned-contract and implementation findings,
  including safe missing-code handling, nested-helper rejection, no-sleep
  frozen dispatch evidence, exact-int timeout boundaries, phantom-member
  normalization, and alias-closed static evidence. None were deferred.

Quality Governor:
- Pre-implementation scope review confirmed one Phase 0 election-policy slice
  with no process/runtime/state/SDK expansion. Its two P2 wording/verification
  findings were task-owner approved and accepted immediately after activation;
  observable behavior was unchanged. Final review confirmed the replacement
  remains inside the same two product/test allowlist files, adds no dependency
  or scope override, and closes every recorded finding. P0/P1/P2 = 0 and GO.

## Verifier Evidence

- Command: focused startup-wait tests; `make test-phase0`; `make gate-phase0`
  (including full `make check`); pre-commit `make check-fast`; candidate GitHub
  Actions matrix
- Result: passed
- Notes: focused tests passed 135/135; `make test-phase0` passed 1,579 tests;
  embedded `make check` passed 1,704 tests plus formatting, lint, typing, and
  agent checks; the sustained Phase 0 gate passed on local CPython 3.13.5.
  Replacement candidate `ce76bcb289a379a907e5b91250448f9b953a7109` passed
  [run 29161101087](https://github.com/alovwang-sys/FlowSight/actions/runs/29161101087)
  on its first attempt with jobs `86566221981` (macOS 3.12), `86566221988`
  (Ubuntu 3.12), `86566222007` (Ubuntu 3.13), and `86566222039` (macOS 3.13).
  Intermediate candidate `b107a1590aed257076e1ed66a4e18a33b35bad37` passed
  [run 29160332404](https://github.com/alovwang-sys/FlowSight/actions/runs/29160332404)
  on its first attempt with jobs `86564216131` (Ubuntu 3.12), `86564216139`
  (Ubuntu 3.13), `86564216140` (macOS 3.13), and `86564216145` (macOS 3.12).
  Completion-control run
  [29160467690](https://github.com/alovwang-sys/FlowSight/actions/runs/29160467690)
  first failed only Ubuntu 3.13 after 1,666 passes when the TRIAL-004 real
  sidecar test observed removed state/dead health just before the owner lock was
  released; the authorized failed-job rerun passed as job `86565326621`, and
  attempt 2 completed successfully without a code or threshold change.
  The full check correctly retains the partial-scaffold limitation. This proves
  bounded re-contention policy only; it does not prove child launch/READY,
  descriptor handoff, state publication, requested-port policy, reload, SDK
  attachment, or complete Phase 0.

## Failure Queue Items

- none
