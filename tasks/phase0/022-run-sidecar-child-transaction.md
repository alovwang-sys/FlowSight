# Task: Run One Exact Sidecar Child Transaction

## Task Metadata

```yaml
task_id: P0-022
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-002, P0-003, P0-006, P0-007, P0-009, P0-011, P0-014, P0-016, P0-018, P0-020, P0-021, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-022

## Phase

Phase 0

## Goal

Consume one explicit canonical child-argument tuple and run exactly one
project-scoped sidecar child transaction: adopt its owner/startup-writer pair,
bind one loopback listener, create and publish one exact startup state, serve
that listener, emit one terminal READY or FAILURE startup outcome, and retire
state and owned resources in a fixed order on every claimed Python-managed
exit, without adding a parent launcher, executable entrypoint,
SQLite/event/telemetry storage, SDK, or UI behavior.

## Context

- P0-018 returns the exact re-derived `SidecarRuntimeConfig` plus the adopted
  move-only `OwnerLock` and `StartupWriter`, but deliberately stops before a
  listener, state, runtime, or READY signal. P0-020 serves an exact prebound
  listener and exposes one post-Uvicorn-start synchronous hook, but deliberately
  owns no child bootstrap, state publication, READY, or owner lifecycle.
  P0-022 is the smallest child-only join of those branches after P0-021 makes
  listener consumption unambiguous.
- P0-002 supplies the authenticated HTTP boundary exercised by the real child,
  and P0-016 supplies the exact canonical argument tuple used directly as
  child-entry evidence. Neither dependency adds parent launch behavior.
- The fixed blocking internal surface is:

  ```python
  def run_sidecar_child(arguments: tuple[str, ...]) -> None: ...
  ```
- Production receives an explicit exact built-in tuple and never reads ambient
  arguments or environment to discover it. A future executable entrypoint may
  construct `tuple(sys.argv[1:])` and call this function, but `sys.argv`,
  `__main__`, command construction, process creation, and exit-code policy are
  outside this task. Any non-exact tuple fails exactly with
  `TypeError("arguments must be an exact built-in tuple")` before dependency or
  ownership work.
- Before `prepare_sidecar_child` succeeds, P0-018 remains the sole authority for
  whether either inherited descriptor locator was untouched or retired. P0-022
  must not parse a rejected tuple, infer a descriptor, or guess cleanup. The
  successful exact three-tuple return transfers the adopted `OwnerLock` and
  `StartupWriter` terminally into this transaction; neither handle returns to
  the caller.
- After adoption, production reads only the exact config `runtime_root`,
  `project_id`, and `requested_port` slots through captured canonical slot
  getters. It constructs one exact P0-001 `StateStore`, calls P0-003 once with
  the configured port, calls P0-007 once with that store/listener, and publishes
  that same exact state while the adopted owner lock remains live.
- State publication precedes the required ownership-aware serving bridge. The
  sole exact startup hook closes over the exact state and startup writer and
  attempts one
  `StartupReady(startup_id=state.startup_id, sidecar_pid=state.pid,
  port=state.port)` only after locked Uvicorn has started. READY remains a wakeup
  hint: the parent must still use P0-009's fresh state/health/state admission.
- The startup outcome is logically one-shot across READY and FAILURE. Once any
  send attempt begins, no second message, fallback, or retry is permitted, but
  beginning `StartupWriter.send` is not treated as physical descriptor transfer
  or successful close. P0-022 retains cleanup responsibility for the writer and
  invokes its canonical `close` exactly once after every attempted or
  unattempted send; when `send` already consumed the endpoint that close is the
  reviewed idempotent no-op. If an ordinary failure occurs after successful
  adoption but before any READY attempt, the transaction makes at most one exact
  `StartupFailure(SIDECAR_STARTUP_FAILED)` send attempt after its owned
  listener/state cleanup attempts and before closing the writer and releasing
  the owner. A preparation failure has no trusted adopted writer and therefore
  sends nothing and performs no P0-022 writer cleanup.
- READY outcome state distinguishes `ready_attempted` from `ready_succeeded`;
  success requires the exact canonical send to return exact `None`. Every
  Python-managed non-control terminal path with no READY attempt, including an
  unexpected normal bridge return without hook execution, cleans up and makes
  its sole FAILURE attempt before ending in the fixed transaction
  `RuntimeError`. A failed READY attempt never falls back to FAILURE. Exact
  `None` from `run_sidecar_child` is possible only when READY succeeded, the
  bridge returned normally, state/writer/owner cleanup all succeeded, and every
  exact cleanup postcondition was satisfied.
- Immediately before the canonical state publication call, production records
  `publication_attempted` with no fallible operation between the marker and the
  call. `publication_succeeded` becomes true only when that exact call returns
  exact `None`. Because `StateStore.publish` may install state and then raise or
  return malformed evidence, every Python-returning path after the attempt calls
  `remove_if_owned` once even when publication did not report success.
- The child holds the exact owner lock throughout bind, publication, serving,
  state retirement, startup-writer retirement, and every other owned cleanup.
  On a claimed Python-managed exit, the network listener is retired first by
  its current owner, every attempted publication is then compare-removed by
  exact `startup_id`, the writer receives exactly one canonical close call, and
  the owner lock is closed last. No cleanup operation is retried after an
  ambiguous result.
- P0-021 resolves P0-020's pre/post-transfer process-control ambiguity with one
  consuming bridge. P0-022 owns the listener until it directly invokes
  `serve_owned_prebound_sidecar_app`, then terminally relinquishes it for every
  normal, ordinary-failure, and process-control outcome. P0-022 never infers
  ownership from error type, listener state, health, notes, or traceback and
  never performs a post-invocation listener close.
- Ordinary dependency or cleanup failures become one fixed non-secret child
  transaction error only after sensitive frames are gone. Synchronous
  `KeyboardInterrupt`, `SystemExit`, and custom non-`Exception`
  `BaseException` values are never converted to FAILURE and preserve object
  identity. Cleanup continues in the fixed remaining-resource order; a cleanup
  problem cannot replace the active control. P0-022 itself may add at most one
  fixed `sidecar child transaction cleanup failed` note. Already-reviewed fixed
  notes from P0-001, P0-003, P0-006, P0-011, P0-018's descriptor-adoption path,
  or P0-021 may coexist; these include `loopback listener cleanup failed` and
  `sidecar child descriptor adoption cleanup failed`. P0-022 neither
  deduplicates, removes, reorders, rewrites, nor interprets them. A cleanup
  control with no already-active control propagates unchanged after later owned
  cleanup attempts.
- P0-020 records a deliberate default-SIGTERM boundary: Uvicorn can restore and
  replay `SIG_DFL` after closing its listener, terminating the process before
  this outer Python frame resumes. P0-022 therefore promises state removal and
  Python handle cleanup only when control returns through Python. Default
  SIGTERM, SIGKILL, fatal signals, `os._exit`, OOM, and arbitrary asynchronous
  bytecode injection rely on OS descriptor release plus existing stale-state
  health/election recovery; they must not be reported as clean state removal.
- P0-019 is intentionally not a dependency. Its configured-incumbent admission
  dominates every state-return path in the future parent launcher; this task
  runs only the already-elected child path and creates no incumbent, attach, or
  launch decision.
- TRIAL-005's five user-approved Phase 4 scope reductions remain frozen and do
  not create a Phase 0 override. This task adds no `sys.monitoring`,
  `sys.settrace`, tracepoint function-shape support, or debugpy/coverage claim,
  and it does not import or promote the tracepoint spike.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 2.5, 4.2, 7.4, and Phase 0
  - `docs/agent-operating-system.md` Phase 0 gate and role model
  - `docs/agent-facts.tsv` FS-001, FS-007, FS-008, and FS-023
  - `spikes/sidecar_otel/RESULT.md` promotion requirements
  - `spikes/tracepoint_backend/RESULT.md` approved Phase 4 reductions
  - P0-001, P0-002, P0-003, P0-006, P0-007, P0-009, P0-011, P0-014, P0-016, P0-018, P0-020, and P0-021 contracts

## Dependency Boundary

P0-021 must be complete before P0-022 enters `in_progress`. P0-022 depends only
on its fixed ownership-aware surface:

```python
def serve_owned_prebound_sidecar_app(
    state: SidecarState,
    listener: socket.socket,
    *,
    on_started: Callable[[], None],
) -> None: ...
```

For exact top-level inputs, invocation terminally consumes the listener before
any fallible function-kind, compatibility, app, Config, Server, or run work.
Every Python-managed outcome after invocation receives exactly one bridge-owned
close attempt. P0-022 therefore owns and closes the listener only before that
call; once the direct call begins it performs no listener inspection, probe, or
cleanup on any outcome. P0-021 preserves P0-020's same-socket Uvicorn runtime,
post-start hook, fixed errors, process-control identity, privacy, and signal
boundary, so P0-022 neither copies nor weakens those contracts.

## Related Fact IDs

- FS-001
- FS-007
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/child_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_child_runtime.py`
- `tests/sidecar/test_runtime_config.py`

`flowsight/sidecar/__init__.py` may change only for the exact import and one
`__all__` entry. `tests/sidecar/test_runtime_config.py` may change only for its
exact sidecar-export and public-submodule expectations.

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/child_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_child_runtime.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not change P0-001/P0-002/P0-003/P0-006/P0-007/P0-009/P0-011/P0-014/
  P0-016/P0-018/P0-020/P0-021 implementations. Compose only their captured canonical
  surfaces; do not copy their validation, codecs, bind policy, state schema,
  HTTP app, Uvicorn Config, health admission, descriptor adoption, or cleanup
  state machines.
- Do not activate P0-022 before P0-021 is complete, bypass P0-021 by calling the
  older `serve_prebound_sidecar_app` directly, infer transfer from an exception
  type/traceback/note, probe the listener to guess whether close succeeded, or
  accept fail-stop process exit as a substitute for the claimed Python-managed
  listener cleanup.
- Do not change, depend on, or call P0-019. Do not add parent discovery, incumbent
  admission, election, attachment, one-overall-startup-deadline policy,
  launcher, command/environment construction, production `subprocess`,
  `Popen`, fork, exec, `pass_fds`, process polling, signaling,
  terminate/kill/wait/reap, or child-handle behavior. Bounded no-shell
  subprocesses and `pass_fds` are allowed only in the new test as real child
  evidence, never as a production launcher claim.
- Do not read `sys.argv`, add `__main__.py`, `argparse`, a console script,
  executable module, exit-code mapper, daemonization, PID file, signal handler,
  shutdown endpoint, background thread/task, async public variant, or returned
  runtime/process handle. Production must not call `os._exit` or install,
  replace, restore, or suppress process signal handlers.
- Do not open SQLite or construct/use `SQLiteWALWriter`; add a writer queue,
  fake or real event, ingest, producer lease, sender, OTel processor,
  instrumentation, trace, request context, completion state, SDK/`FlowSight`,
  `open_ui`, static UI, frontend, browser, code map, source API, or Phase 1+
  behavior. Do not add dependencies, packaging metadata, Makefile targets, or
  claim Phase 0 acceptance.
- Do not implement any tracepoint backend/API/UI, import either spike package,
  enable `sys.monitoring` or `sys.settrace`, widen the standard GIL-enabled
  CPython 3.12/3.13 macOS/Linux matrix, or claim real debugpy/coverage support.
  The five approved TRIAL-005 reductions remain unchanged and out of scope.
- Do not parse or index the child arguments, decode bootstrap fields again,
  infer descriptor numbers, duplicate/detach/serialize/copy either move-only
  handle, create another owner lock, return a resource bundle, or inspect raw
  handles after P0-018 rejects. A failed preparation cannot send FAILURE or
  perform guessed cleanup.
- Do not bind remote/non-loopback, create a socket directly, call `bind`,
  `listen`, `fromfd`, `dup`, `uvicorn.run`, or `Config.bind_socket`, or add an
  alternate/default listener. P0-003 creates the sole network listener and
  the required ownership-aware bridge consumes that exact identity while
  preserving P0-020 serving. Test-only control pipes and Uvicorn's internal
  wakeup socketpair are not alternate network listeners.
- Do not send READY before state publication or before P0-020 invokes the
  post-start hook. Do not send both READY and FAILURE, retry a send, send a
  second frame after any send attempt, accept a caller callback, or expose a
  startup message/result to the caller. Do not treat READY as health, treat a
  send attempt as physical descriptor transfer, skip the one canonical writer
  close after a send attempt, or close the writer more than once.
- Do not publish a copied/reconstructed state, remove state by path/unlink, or
  remove an unowned replacement. Only the exact generated state may be
  published and only `remove_if_owned(state.startup_id)` may retire it. Do not
  release the owner before listener/state/writer cleanup attempts finish.
- Do not retry an ambiguous listener, state, writer, or owner cleanup; suppress
  or replace process control; turn process control into a FAILURE message; or
  claim Python finally/state removal under default SIGTERM, SIGKILL, fatal
  signal, `os._exit`, OOM, or arbitrary asynchronous interruption.
- The transaction may retain and pass the one exact `SidecarState`, construct
  its one exact `StateStore`, publish that exact state, and read only the exact
  config/state fields required by the fixed store/listener/READY/remove/bridge
  calls. Do not create an additional state copy, serialization, projection,
  cache, registry, callback egress, or retained side result. Do not inspect,
  format, print, log, or expose raw arguments, config/state repr,
  project/runtime/database paths, token, descriptor, dependency exception,
  errno, or dependency message. The exact P0-001 state publication, exact READY,
  and P0-002's authenticated health payload are the only reviewed
  state-identity egress; fixed errors and stdout/stderr remain non-secret.

## Acceptance Criteria

- [ ] `run_sidecar_child` is exported identically from `flowsight.sidecar`,
  occurs exactly once in `__all__`, has the fixed signature above, and is the
  only new production surface. It blocks through the server lifetime and
  returns exact `None` only after READY send success, normal ownership-aware
  bridge return, and successful claimed cleanup; it adds no public class, error
  enum, result, handle, callback, alternate function, async variant, or mutable
  registry/cache.
- [ ] Only an exact built-in tuple reaches P0-018. Wrong top-level type fails
  exactly with `TypeError("arguments must be an exact built-in tuple")` before
  any dependency or descriptor operation. The original exact tuple is passed once to one import-time-captured
  canonical `prepare_sidecar_child`; production never parses, indexes, copies,
  logs, serializes, or reconstructs it. Public package/module replacement cannot
  redirect the dependency.
- [ ] A P0-018 preparation ordinary failure performs no P0-022 descriptor,
  channel, lock, listener, state, filesystem, or cleanup work and becomes the
  fixed child-transaction error only after raw arguments and dependency errors
  are gone. P0-018 remains the sole authority for descriptor disposition. A
  preparation process-control exception preserves identity and ends the call
  with no later P0-022 work.
- [ ] One successful exact P0-018 result is admitted without iterable protocol
  dispatch and terminally transfers the exact config, owner, and writer into
  P0-022. Production reads exactly config `runtime_root`, `project_id`, and
  `requested_port` once each through captured canonical slot getters, constructs
  one exact canonical `StateStore`, and retains no bootstrap, descriptor scalar,
  raw argument field, project root, or startup timeout.
- [ ] While the adopted owner remains live, production calls captured P0-003
  exactly once with the exact configured `requested_port`, obtains the sole
  listener, calls captured P0-007 exactly once with the same exact store and
  listener, and calls the captured canonical `StateStore.publish` exactly once
  with that same exact state. `None`, dynamic `0`, and explicit port policy are
  inherited unchanged from P0-003; no fallback or compatibility policy is
  reimplemented.
- [ ] The transaction order is structurally fixed as successful adoption,
  store construction, listener bind, startup-state creation, state publication,
  then one completed ownership-aware bridge call. No server/app/READY work
  precedes successful publish. The same exact state and listener identities
  enter that bridge with one exact synchronous Python startup function; no
  alternate app, Config, socket, task, thread, or server path is reachable.
- [ ] The startup function constructs exactly one canonical `StartupReady` from
  the exact generated state's startup ID, PID, and actual bound port and calls
  the exact adopted writer's canonical `send` only after the bridge's preserved
  P0-020 post-start notifier invokes it. It receives no arguments, returns exact
  `None` only after a successful send, executes at most once, clears its own
  hook-local message references after its terminal attempt, and exposes no
  message or side result. The outer transaction retains the writer solely for
  its required one cleanup call. READY is not treated as health; real admission
  uses P0-009.
- [ ] Across the complete transaction there is at most one startup-channel send
  attempt. Beginning READY terminally consumes logical outcome authority whether
  the write succeeds, ordinarily fails, or raises process control; it does not
  assert physical descriptor transfer, and no FAILURE or retry follows. An
  ordinary post-adoption failure before any READY attempt selects exactly one canonical
  `StartupFailure(StartupFailureCode.SIDECAR_STARTUP_FAILED)` attempt only after
  listener/state cleanup attempts and before owner release. A process-control
  outcome never sends FAILURE. Any send failure is non-retryable and cannot
  expose raw detail. After every READY, FAILURE, or no-send path following
  successful adoption, production calls canonical `StartupWriter.close`
  exactly once; an endpoint already consumed by `send` makes that call a no-op.
- [ ] Production records separate `ready_attempted` and `ready_succeeded` state;
  only an exact `None` READY-send result sets success. Every Python-managed
  non-control terminal outcome with no READY attempt, including a normal bridge
  return without hook execution, first runs listener/state cleanup, makes its
  one eligible FAILURE attempt, closes writer/owner, and then raises the fixed
  transaction `RuntimeError`. A READY attempt that ordinarily fails or returns
  non-`None` never sends FAILURE and also ends in the fixed error after cleanup.
  Only READY success plus normal bridge return plus wholly successful cleanup
  can return exact `None`.
- [ ] Listener ownership is explicit in tests and implementation. It remains
  P0-022-owned through bind, state creation, and publication. Any ordinary or
  process-control outcome before the consuming bridge call causes exactly one
  child-owned canonical close attempt before state/writer/owner cleanup. At the
  bridge call boundary P0-022 terminally relinquishes the exact listener; for
  every normal, ordinary-failure, and process-control outcome after that call
  begins, the bridge alone performs its one close attempt and P0-022 performs
  none. Tests prove there is no exception-shape, `fileno`, health, note, or
  traceback inference and no pre/post-transfer second-close or leak gap.
- [ ] Immediately before exact `StateStore.publish(state)`, production sets
  `publication_attempted` with no fallible operation in the marker-to-call gap.
  `publication_succeeded` becomes true only if that call returns exact `None`;
  a non-`None` result is a primary ordinary failure. Every Python-returning path
  after the attempt calls captured canonical `remove_if_owned(startup_id)` once
  while the owner remains live. When publication succeeded, removal must return
  exact `True`; exact `False`, a non-exact bool, another result, or an exception
  is cleanup failure. When publication did not successfully return, exact
  `True` or `False` is accepted as cleanup evidence and the original primary
  ordinary/control outcome remains authoritative; a non-exact bool, another
  result, or exception is cleanup failure. Writer and owner cleanup still run
  after every removal outcome. Failure before `publication_attempted` performs
  no removal; direct unlink and replacement removal remain forbidden.
- [ ] Claimed Python-managed cleanup order is exactly: retire the listener once
  by P0-022 before the bridge or by the bridge after relinquishment,
  compare-remove published state, attempt the one eligible FAILURE send if
  selected, call canonical writer close exactly once regardless of send state,
  and close the owner lock last. Each owned cleanup is attempted at most once, no
  handle returns to the caller, and no ordinary or process-control failure skips
  later owned cleanup or releases owner authority early.
- [ ] Every ordinary store/bind/state/publish/server/startup-message/removal/
  cleanup failure becomes exactly
  `RuntimeError("sidecar child transaction failed")`, raised `from None` only
  after sensitive dependency/composition frames are gone. Without caller-active
  context, cause/context/notes are empty; caller context may remain only as
  suppressed context. Fixed error text, formatted traceback, production-frame
  locals, stdout/stderr, and logs contain no raw argument, path, token, startup
  ID, PID, port, descriptor, dependency error, or dependency message.
- [ ] Synchronous `KeyboardInterrupt`, `SystemExit`, and a custom direct
  `BaseException` from every captured post-adoption stage preserve object
  identity, payload, preexisting notes, and dependency traceback. Cleanup
  continues through the fixed remaining-resource order. A cleanup problem
  cannot replace the active control. P0-022 may add at most one exact
  `sidecar child transaction cleanup failed` note. Existing fixed safe notes
  independently added by P0-001, P0-003, P0-006, P0-011, P0-018's
  descriptor-adoption path, or P0-021 may coexist in dependency order; this
  includes `loopback listener cleanup failed` and
  `sidecar child descriptor adoption cleanup failed`. P0-022 does not
  deduplicate, remove, reorder, rewrite, or interpret them. A cleanup control
  with no prior control propagates unchanged after later cleanup. Caller-active
  ordinary/control contexts retain identity and notes across success, fixed
  failure, and control outcomes. Tests lock every permitted note string and
  reject raw cleanup detail.
- [ ] Deterministic unit/fault tests cover every dependency boundary, exact
  identities and call counts, config-slot admission, ownership transfer,
  publish-before-serve-before-READY order, READY/FAILURE mutual exclusion,
  logical send-attempt terminality plus unconditional one writer close,
  publication attempt/success plus the full exact removal-result matrix,
  no-READY normal bridge return, failed READY without FAILURE fallback,
  listener pre/post-transfer ownership, fixed cleanup/note ordering,
  ordinary/control arbitration, false cleanup success claims, no
  retention/output/log leakage, and absence of parent/process/SQLite/event/
  telemetry storage/SDK/OTel/UI/tracepoint behavior.
- [ ] A bounded no-shell test-only child receives one canonical P0-016 argument
  tuple and exactly the owner/startup-writer descriptors through `pass_fds`,
  calls production explicitly, and uses no production argv reader or launcher.
  The parent closes its writer/owner duplicates, consumes the real startup
  reader through P0-009, and proves READY matches the freshly published state,
  current child PID, actual listener port, and an authenticated health response.
  A separate contender remains blocked solely by the child owner until cleanup.
- [ ] Real-child ordinary failure modes before READY produce only the fixed
  FAILURE outcome or terminal channel EOF/error when the one send itself fails;
  they publish no enduring owned state, release the listener/writer/owner in
  order, allow a successor owner, emit no token/path/raw exception, and leave no
  child or process group. Tests use observable pipe/health/exit conditions with
  bounded deadlines and no sleeps.
- [ ] A real test-only custom SIGTERM handler exercises P0-020's restored-signal
  replay and normal Python return, then proves the listener is closed, exact
  state compare-removed, unused resources retired, owner released last, and
  `run_sidecar_child` returned exact `None`. A separate default-`SIG_DFL` mode
  proves bounded exact `-SIGTERM` exit, listener/OS descriptor release, dead
  authenticated health, re-acquirable owner, and stale-state recovery without
  asserting that the outer Python cleanup or state removal ran. Every child is
  reaped; TERM/KILL fallback is test cleanup only.
- [ ] A positive full-tree AST allowlist freezes exact imports, immutable
  captures/constants, fixed public/helper signatures, config-slot reads,
  dependency calls/counts, one nested exact startup function, transaction order,
  publication-attempt marker adjacency, one-shot outcome state, one writer close
  on every adopted path, separate READY attempt/success and publication
  attempt/success state, P0-021 listener relinquishment, exception handlers,
  cleanup/note order, fixed errors, and exact-`None` return. It rejects nested
  unreviewed behavior, dynamic imports/calls, argv/entrypoint/launcher/process
  production, direct socket bind/duplication, direct unlink, `SQLiteWALWriter`,
  event-storage writer, writer queue, SQLite/event/telemetry storage, SDK/OTel/
  UI/tracepoint code, mutable module state, logging, output, or cache. It
  positively permits and counts only the required `StartupWriter.send` and
  canonical `StartupWriter.close` calls.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass locally and on the macOS/Linux x CPython 3.12/3.13 CI matrix. The
  partial-scaffold disclaimer remains explicit; this child transaction alone is
  not parent launch, SDK attachment, SQLite ownership, bundled UI, or Phase 0
  product acceptance.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest \
  tests/sidecar/test_child_runtime.py \
  tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
one exact adopted child publishes and serves one authenticated startup, emits
one terminal startup outcome, and retires state/resources in fixed order without
adding parent launch, SQLite/event/telemetry storage, SDK, UI, or tracepoint scope
```

## Risks

- Treating P0-018 decode success rather than its completed adoption as P0-022
  ownership could leak or double-close inherited locators. Conversely, parsing
  a failed tuple to recover descriptor numbers would touch untrusted resources.
- Sending READY before publish or before Uvicorn startup lets a parent admit a
  child that cannot yet satisfy authenticated health. Retrying READY as FAILURE
  can place two contradictory terminal outcomes on a one-shot channel.
- Bypassing P0-021 for the older P0-020 API recreates its same-shaped
  pre/post-transfer process-control ambiguity and makes exact listener cleanup
  impossible without a guess.
- Releasing the owner before listener shutdown, compare-removal, or startup
  writer retirement allows a successor to race a still-advertised or
  still-serving predecessor.
- Treating publish return as the only evidence of installation can leave an
  atomically installed state behind after a late publish failure; treating
  remove `False` identically after successful and unsuccessful publication can
  either hide cleanup loss or replace the original primary failure.
- A normal bridge return without a READY attempt is not child success. Returning
  `None` there would strand the parent until timeout; it must follow the sole
  FAILURE path and fixed error after cleanup.
- Default SIGTERM replay may terminate between P0-021 listener cleanup and
  P0-022 state cleanup. Tests and product claims must distinguish OS resource
  release/stale recovery from Python-managed clean removal.
- Folding SQLite writer startup into this task would add a background-thread and
  bounded-flush state machine to an already load-bearing process transaction.
  SQLite/event/telemetry storage ownership remains a later Phase 0 slice.

## Reviewer Focus

- Is the exact ownership boundary inherited from P0-018, with zero descriptor
  inference or P0-022 cleanup after preparation failure?
- Can READY be observed before exact state publication and real Uvicorn startup,
  or can any outcome produce two sends, a retry, or READY-to-FAILURE fallback?
- Does the completed bridge eliminate, rather than guess from, P0-020's
  pre/post-transfer control ambiguity, with exactly one owner and one close
  attempt on every claimed listener path?
- Is state compare-removed while the same owner remains live, with writer
  retirement before owner-last release, the exact publication success/removal
  matrix, and no ordinary failure skipping later cleanup?
- Can normal bridge return without READY, malformed READY success, or cleanup
  notes produce a false `None`, a second startup message, or raw detail?
- Do real signal tests distinguish custom-handler Python return from default
  SIGTERM fail-stop cleanup rather than falsely asserting finally semantics?
- Did the task remain child-only and avoid P0-019, parent deadline/launcher,
  argv/entrypoint, production process APIs, SQLite, UI, SDK/OTel, tracepoints,
  and changes to the five approved TRIAL-005 reductions?

## Role Outputs

Implementer:
- Implementation is intentionally pending. Planning fixes one blocking
  `run_sidecar_child(arguments)` surface and a child-only transaction from
  canonical adoption through owner-last cleanup, with no product file changed
  by the planning commit.

Adversarial Reviewer:
- Reviewer 1: planning analysis identified the P0-018 adoption return as the
  only safe P0-022 ownership admission and froze READY/FAILURE mutual exclusion,
  publish-before-start notification, publication-attempt cleanup, P0-021
  listener relinquishment, and compare-remove-before-owner-release as the
  primary adversarial targets. Actual-card review added separate
  `publication_attempted`/`publication_succeeded` state and the exact
  success-dependent removal-result matrix.
- Reviewer 2: planning analysis separated Python-managed cleanup from Uvicorn's
  default-SIGTERM replay, required both custom-handler normal-return and
  `SIG_DFL` fail-stop evidence, required one canonical writer close after every
  send attempt, and rejected any claim that send start proves physical FD
  transfer or stale state must be removed after default signal termination.
  Actual-card review also made no-READY normal bridge return a FAILURE-plus-fixed
  error path and froze dependency/P0-022 cleanup-note coexistence.

Fixer:
- Planning reconciliation accepts every actual-card finding: P0-002/P0-016 and
  P0-021 are explicit prerequisites; wrong arguments have one exact message;
  publication and READY each have attempted/succeeded state; every adopted
  writer is closed once; cleanup notes coexist without rewriting; exact state
  publication remains allowed while extra copy/cache/egress is forbidden; and
  P0-019 plus parent-launch policy remain deferred.

Quality Governor:
- Planning scope is one Phase 0 child transaction with exactly four allowed
  product/test files, `scope_override: none`, no new gate claim, and explicit
  separation from parent launch, argv/entrypoint, SQLite/event/telemetry
  storage, SDK, UI, and all v1 non-goals. Status must remain `planned` until
  P0-021 is complete.

## Verifier Evidence

- Command: `.venv/bin/python scripts/validate_agent_system.py`; `git diff --check`
- Result: passed
- Notes: planned task-record validation and whitespace checks passed;
  implementation and product verification have not started, and planned
  acceptance criteria remain unchecked by design.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record. This task
  must not relax, skip, or selectively retry that benchmark.
