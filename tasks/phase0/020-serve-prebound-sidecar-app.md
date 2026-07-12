# Task: Serve One Exact Prebound Sidecar App

## Task Metadata

```yaml
task_id: P0-020
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-002, P0-003, P0-007, P0-015, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-020

## Phase

Phase 0

## Goal

Run one canonical authenticated sidecar app synchronously on its exact
already-bound IPv4 loopback listener until the locked Uvicorn server stops,
and invoke one exact one-shot domain-neutral startup function only after the
server has started, without allocating another network listener or adding
entrypoint, launcher, state publication, READY, storage, SDK, or telemetry
behavior.

## Context

- P0-002 builds the private Host/token/Origin-enforcing FastAPI app. P0-003
  atomically returns the exact already-listening non-inheritable loopback socket
  that a later runtime must retain. P0-007 creates the exact state whose current
  process identity and port correspond to that listener. P0-015 locks Uvicorn
  0.51.0 as a wheel runtime dependency. No production module currently joins
  those four prerequisites into a serving runtime.
- The fixed blocking internal surface is:

  ```python
  def serve_prebound_sidecar_app(
      state: SidecarState,
      listener: socket.socket,
      *,
      on_started: Callable[[], None],
  ) -> None: ...
  ```
- This call is intended only for the future sidecar child main thread. It does
  not return a server, app, config, socket, shutdown handle, event, task, or
  thread. `on_started` must be an exact Python function. Production wraps it in
  one private one-shot async notifier supplied as Uvicorn's exact
  `callback_notify`; locked Uvicorn 0.51 first awaits it from `on_tick(0)` only
  after `Server.startup` has created the asyncio server and marked itself
  started. Exact coroutine, generator, and async-generator functions are
  rejected before transfer using captured canonical function-kind checks, so
  invoking the hook cannot manufacture an un-awaited coroutine merely because
  the function was declared `async def`. Repeated notify ticks become no-ops
  after the first exact-`None` callback result. A later child entrypoint may
  close over its exact startup
  writer to send READY from this linearization hook, but P0-020 itself imports,
  constructs, inspects, or emits no startup message and owns no state
  publication semantics.
- The startup function is synchronous and receives no state,
  listener, app, Config, Server, token, port, path, or runtime handle argument.
  Its ordinary failure or non-`None` result fails the serving attempt; its
  process-control exception preserves identity. A normal exact Python function
  returning an awaitable/generator violates the trusted internal caller
  contract; P0-020 never awaits, iterates, or invokes protocols on an unknown
  result. The hook must be bounded and nonblocking by trusted caller contract;
  Uvicorn's graceful-shutdown timeout does not bound arbitrary hook execution.
- Top-level type checks and compatibility preflight remain caller-owned. A wrong
  type or an exact but incompatible state/listener pair neither transfers nor
  closes the listener. After exact preflight proves the canonical
  current-process state and supplied socket agree, ownership remains with the
  caller until execution enters the owned serving helper's established outer
  cleanup guard. Entry into that guard is the sole transfer point. From that
  point, this call terminally consumes the listener and performs one captured
  canonical close attempt on every reviewed synchronous Python-managed exit;
  Uvicorn may already have closed it first.
- Preflight also proves the current thread has no running asyncio loop before
  `Server.run` can create its coroutine; otherwise it fails before transfer and
  cannot emit an un-awaited-coroutine warning. It checks only reviewed state
  `pid`, `host`, and `port` scalars plus canonical built-in
  socket operations. It requires the current main thread, current PID, exact
  loopback host/port agreement, a live AF_INET/SOCK_STREAM listener using
  protocol `0` or TCP, and a non-inheritable descriptor. Linux proves the
  listening state with `SO_ACCEPTCONN == 1`. Darwin, where that option is not
  available, uses the same exact `TCP_CONNECTION_INFO` listening-state probe
  accepted by P0-007. Other platforms remain outside the v1 process-lifecycle
  support matrix.
- Wrong top-level types fail exactly with
  `TypeError("state must be an exact SidecarState")` and
  `TypeError("listener must be an exact built-in socket.socket")`. An exact
  non-function startup hook fails exactly with
  `TypeError("on_started must be an exact Python function")`. An exact pair or
  execution context that fails compatibility preflight fails exactly with
  `ValueError("sidecar state and listener are incompatible")`. Every ordinary
  post-transfer app/config/server/run/cleanup failure becomes exactly
  `RuntimeError("prebound sidecar server failed")`. These fixed messages expose
  no state, PID, port, token, path, socket, or dependency detail.
- Uvicorn 0.51.0 must receive the listener only through captured
  `Server.run(sockets=[listener])`. `uvicorn.run`, `Config.bind_socket`, an
  omitted `sockets` argument, and `Config(fd=...)` can create, duplicate, or
  reinterpret listeners and are forbidden. The exact Config also freezes one
  worker, no reload, no proxy trust, no WebSocket stack, no app lifespan, no
  access/default logging, one bounded graceful-shutdown timeout, and the private
  one-shot startup notifier.
- Uvicorn closes supplied sockets during its normal shutdown, but Config load,
  startup, or main-loop failure can skip that shutdown. FlowSight therefore
  owns one final close attempt. If a process-control `BaseException` is active,
  cleanup cannot replace it; a cleanup problem may add only one fixed safe note.
  If cleanup itself raises a process-control exception without an already-active
  control, that cleanup control propagates unchanged.
- Uvicorn captures SIGINT/SIGTERM only on the main thread, restores the original
  handlers after graceful shutdown, and replays captured signals. A default
  SIGTERM can therefore terminate the process before FlowSight's Python frame
  regains control, after Uvicorn has closed its listener. Real-signal evidence
  proves bounded Uvicorn/OS teardown and signal replay; it does not claim
  arbitrary Python-finally cleanup for SIGKILL, fatal signals, OOM, or
  `os._exit`.
- The hook establishes only the required order “Uvicorn startup completed
  before notification” without adding a thread, probe, returned server handle,
  premature pre-start READY, or later reimplementation of Uvicorn startup. It
  is not atomic with SIGINT/SIGTERM: Uvicorn 0.51 checks `should_exit` only after
  awaiting `callback_notify`, so shutdown can be requested immediately before,
  during, or after the hook. READY remains a hint whose state/health/liveness is
  independently revalidated by P0-008/P0-009. The hook is the sole callback
  surface in this task and has no default or caller-selected error policy.
- P0-018 child preparation and P0-019 incumbent admission are parallel
  prerequisites for a later child/parent join, not dependencies of this pure
  serving primitive. That later task must still own one overall startup
  deadline, state publication/READY ordering, move-only owner transfer, spawn
  and reap behavior, and every incumbent success exit.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 4.2, 7.4, and Phase 0
  - `docs/agent-operating-system.md` Phase 0 gate
  - `spikes/sidecar_otel/RESULT.md` promotion requirements
  - P0-002, P0-003, P0-007, P0-015, and locked Uvicorn 0.51.0 source

## Related Fact IDs

- FS-001
- FS-007
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/server_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_server_runtime.py`
- `tests/sidecar/test_runtime_config.py`

`flowsight/sidecar/__init__.py` may change only for the exact import and one
`__all__` entry. `tests/sidecar/test_runtime_config.py` may change only for its
exact sidecar-export and public-submodule expectations.

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/server_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_server_runtime.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not add `__main__`, read ambient argv or command environment, build a
  command, or add production `subprocess`, `Popen`, fork, exec, `pass_fds`,
  process polling, signal sending, terminate/kill/reap, launcher, discovery,
  election, attachment, retry, deadline, health-client probe, or P0-019 calls.
  A bounded test-only child and cleanup/reap harness is allowed.
- Production must not construct, load, publish, remove, repair, rename, or
  serialize `StateStore` or `SidecarState`; acquire or inspect `OwnerLock`; open
  or use a startup channel; send READY/failure; decode/adopt child bootstrap
  resources; or touch SQLite, a writer, queue, event, filesystem path, or
  runtime file. The bounded test child may construct one inert temporary
  `StateStore`, real P0-003 listener, and P0-007 state solely as fixtures and
  encode one bounded state fixture through its private inherited control pipe;
  it still may not publish/load/remove state or create product runtime files.
- Do not add SDK/`FlowSight`, OTel, instrumentation, sender, producer lease,
  ingest, trace, code-map, browser, UI-build, `open_ui`, Phase 1 behavior,
  reload, multiple workers, WebSockets, TLS, proxy headers, remote/non-loopback
  binding, Windows lifecycle, free-threaded CPython, or spike imports.
- Do not accept a caller app, Config/Server factory, Uvicorn option, shutdown
  handle/event, timeout, worker count, log policy, or alternate listener. The
  exact required `on_started` Python function is the only callback; do not give
  it arguments, a default, READY/state semantics, repeated execution, a
  caller-selected failure policy, or another callback. Do not return or cache
  an app, Config, Server, socket, notifier, task, thread, result wrapper, or side
  result. Do not add a mutable module registry/cache/default, background
  thread/task, async public variant, new dependency, Makefile change, or Phase 0
  acceptance claim.
- FlowSight production must not call `socket.socket`, `bind`, `listen`,
  `fromfd`, `dup`,
  `Config.bind_socket`, or `uvicorn.run`. Do not call `Server.run` without the
  exact explicit one-element `sockets=[listener]` identity. Uvicorn's internal
  asyncio loop may create its internal wakeup socketpair and put that same
  supplied listener into nonblocking serving mode; it may not create, bind,
  duplicate, or substitute another network listener.
- Do not inspect, format, print, log, emit, or cache the token, database path,
  project/runtime path, state/listener repr, raw dependency error, or dependency
  message. Actual port reads are restricted to captured compatibility checks,
  exact Config construction, and P0-002's already-reviewed authenticated health
  payload. The port must not appear in a new response, fixed error, log, or
  stdout/stderr. Access logging and Uvicorn's default log config remain disabled.
- FlowSight must execute exactly one canonical close backstop after transfer and
  must not retry its own ambiguous backstop, detach, or duplicate the listener.
  Uvicorn may already have attempted or completed its separate internal close.
  Do not suppress a process-control exception or report physical closure after
  FlowSight's backstop itself fails. Do not change P0-002/P0-003/P0-007/P0-015/
  P0-018/P0-019 implementations.

## Acceptance Criteria

- [ ] `serve_prebound_sidecar_app` is exported identically from
  `flowsight.sidecar`, occurs exactly once in `__all__`, has the fixed signature
  above, and is the only new production surface. It adds no public class, error
  enum, result, handle, async variant, alternate server function, or callback
  beyond the exact required keyword-only `on_started` function.
- [ ] Wrong state type fails first with the fixed state `TypeError`
  without inspecting or closing the listener. Wrong listener type then fails
  with the fixed listener `TypeError` without reading state slots
  or calling app/Config/Server/close. Wrong startup-hook type fails next with its
  fixed `TypeError` before compatibility work. Exact coroutine, generator, and
  async-generator functions fail through the same type error using captured
  canonical function-kind checks, before invocation, transfer, cleanup, or
  un-awaited-object creation. Without caller-active context, each type error has
  empty cause/context/notes; caller-active context may remain only as suppressed
  context. Derived, duck, proxy, bound-method, callable-object, and coercible
  inputs cannot run attribute, property, equality, representation, callability,
  or scalar protocol dispatch.
- [ ] Exact-object preflight uses only captured canonical state slot getters,
  current-PID/main-thread/no-running-event-loop checks, and built-in socket
  operations. It reads only state `pid`, `host`, and `port`; every other field
  remains governed by P0-007's canonical-success contract and P0-002's app
  admission. It proves one live non-inheritable AF_INET/SOCK_STREAM
  protocol-0-or-TCP listener is already accepting on exact
  `127.0.0.1:state.port`; Linux and Darwin use their reviewed distinct listening
  probes. Closed, unbound, non-listening, UDP, IPv6, inheritable, non-loopback,
  wrong-PID, malformed identity-slot, port-mismatch, off-main-thread, and
  already-running-loop cases fail with the fixed compatibility `ValueError`
  before app/Config/Server/run. A real `asyncio.run` regression emits no
  un-awaited-coroutine warning.
- [ ] Every ordinary incompatibility reconstructs its fixed error only after
  internal preflight frames and sensitive locals are gone. The listener remains
  caller-owned and unchanged from its input condition after failure. When the
  input was a valid open P0-003 listener and only state/PID/port/thread/loop
  compatibility failed, it remains open and usable; a closed, unbound, or other
  malformed input is not falsely claimed to become open or usable. A
  pre-transfer non-`Exception` preserves identity, payload, notes, and dependency
  traceback without cleanup or later work.
- [ ] Entry into one outer cleanup guard after successful preflight is the sole
  ownership-transfer boundary; no dependency call or mutable action occurs in
  the preflight-to-guard gap. Inside that guard the captured canonical app
  factory is called exactly once with the same state, and its result passes
  directly into one captured Config construction.
  Public package/module/class replacement cannot redirect dependencies;
  private seams may only delegate to their captured canonical dependency or
  synchronously fail before returning. The one explicit test-only exception is
  a malformed `Server.run` result seam used solely to prove non-`None` fail-closed
  behavior; canonical success/provenance remains covered by the real child.
- [ ] Config freezes exactly one safe local runtime: host `127.0.0.1`, the
  admitted state port, `uds=None`, `fd=None`, `loop="asyncio"`, `http="h11"`,
  `ws="none"`, `lifespan="off"`, `interface="asgi3"`, `env_file=None`,
  `reload=False`, `workers=1`, `proxy_headers=False`, an exact newly built empty
  `forwarded_allow_ips` list, `server_header=False`, `date_header=False`,
  `access_log=False`, `log_config=None`, `log_level="critical"`,
  `use_colors=False`, `factory=False`, `root_path=""`, backlog `128`,
  `limit_concurrency=128`, `limit_max_requests=None`,
  `limit_max_requests_jitter=0`, `timeout_keep_alive=5`,
  `timeout_graceful_shutdown=2`, `timeout_notify=30`,
  `callback_notify=<one exact private one-shot notifier>`,
  `headers=<one exact newly built empty list>`,
  `h11_max_incomplete_event_size=16384`, and `reset_contextvars=True`.
  Tests assert the complete reviewed keyword set and resulting load-bearing
  Config attributes. `WEB_CONCURRENCY`, forwarded-IP, reload, or logging
  environment cannot change these values. The external real-child deadline is
  exactly 10 seconds and cannot be derived from a looser implementation value.
- [ ] The private notifier contains the exact startup function and one one-shot
  state only; it is passed directly as Config's `callback_notify`. Locked
  Uvicorn invokes it first from `on_tick(0)` after successful startup. It calls
  the startup function at most once with no arguments, requires the exact
  `None` result, clears its function reference on success or failure, and makes
  every later notify an exact no-op. Ordinary/non-`None` hook failure follows
  the fixed runtime-error path; hook process control preserves identity. Tests
  cover exact built-in non-`None` malformed results only. A normal function
  returning an awaitable/generator violates the trusted caller contract and is
  never awaited, iterated, or otherwise inspected by P0-020. Notification proves
  only completed Uvicorn startup; it is not an atomic continued-liveness or
  no-shutdown guarantee, and later READY admission still requires P0-008/P0-009
  health revalidation.
- [ ] One captured canonical Server is constructed from that Config and its
  captured `run` is called exactly once with a newly built exact list of length
  one whose element is the same listener. No alternate bind/fd/socket path is
  reachable. A normal exact-`None` run result is the only success; any other
  return fails closed. The function blocks and exposes or retains no runtime
  handle or mutable side result.
- [ ] After transfer, FlowSight performs exactly one canonical listener-close
  attempt on every Python-managed outcome, including a normal return and every
  synchronous dependency failure. A successful close leaves the socket closed;
  Uvicorn closing first remains idempotent. An ordinary or ambiguous close
  failure becomes the same fixed runtime error without retry or false closure
  claim. Cleanup process control without an active control propagates unchanged.
- [ ] Ordinary app/Config/Server/run/result/cleanup failures become exactly
  `RuntimeError("prebound sidecar server failed")`, raised `from None` only
  after raw errors, state, listener, PID, port, app, config, and server locals
  are gone. Without caller-active context, cause/context/notes are empty; caller
  context may remain only as suppressed context. Fixed error text, formatted
  traceback, stdout/stderr, logs, and production-frame locals contain no
  sensitive identity/value, path, repr, raw exception, or dependency message.
- [ ] Synchronous `KeyboardInterrupt`, `SystemExit`, and a custom direct
  `BaseException` from every captured post-transfer dependency preserve object
  identity, payload, notes, and dependency traceback after one cleanup attempt,
  with no later collaborator. Cleanup failure cannot replace the active control
  and may add only one fixed cleanup note. Caller-active `ValueError` and
  `KeyboardInterrupt` retain identity/notes across success, fixed failure, and
  process-control outcomes; all P0-020 production frames scrub sensitive locals.
- [ ] Unit/fault tests prove ordering, captured provenance, exact Config kwargs,
  malicious-environment immunity, same-socket run identity, no second
  network-listener allocation/bind/duplication, main-thread/no-running-loop
  policy, coroutine/generator-function preflight with zero warning output,
  notifier post-start ordering/one-shot/reference clearing, expected post-transfer
  nonblocking serving mutation, no pre-transfer state/listener mutation or
  retention, no output/log leakage, transfer ownership, cleanup arbitration,
  raw-error collection, and exact success/failure/control identities. Asyncio's
  internal wakeup socketpair is permitted and cannot satisfy listener evidence.
- [ ] A bounded unpatched test-only child creates one real P0-003 listener and
  P0-007 state, sends bounded exact fixture data through a private inherited
  control pipe rather than argv/environment/stdout, forbids every later
  network-listener `socket.bind` through an audit hook, and calls production with
  an exact startup function that emits a fixed marker only when Uvicorn invokes
  it. The parent waits for that marker, then proves a complete authenticated
  health response on the exact fixture port/current child PID, wrong-token and
  wrong-Host rejection independently followed by valid health, no
  token/server/date/CORS/output leakage, and no alternate endpoint. The fixture
  pipe is the only test-only state encoding carveout and has an exact schema,
  byte bound, one write, and one read.
- [ ] Real child modes prove both Uvicorn signal contracts: a restored custom
  SIGTERM handler verifies the listener is already closed when replay occurs,
  emits a private `SIGTERM_REPLAYED` marker, and returns; a separate marker after
  the production call proves the function then returned exact `None`. The
  default-handler mode explicitly installs `SIG_DFL` before production, holds a
  stalled connection, and exits within the external 10-second deadline with
  exact `-SIGTERM`. Every path is reaped, cleanup fallback is TERM then KILL only
  on test failure, no child/process group survives, and the exact port can be
  rebound afterward. This is process/OS release evidence, not a claim of
  arbitrary Python-finally cleanup under fatal signals.
- [ ] A positive full-tree AST allowlist freezes exact imports, immutable
  dependency/socket/state captures, constants, helpers, signatures, Config
  keyword set, notifier structure, calls/counts/owners, transfer point, handlers,
  cleanup arbitration, and exact-`None` return. It rejects production socket
  construction/bind/listen/fromfd/dup,
  `uvicorn.run`, state/store/channel/owner/process/SDK/storage/OTel/UI/reload/
  multi-worker/WebSocket/proxy/logging/output/additional-callback/cache/background
  behavior, dynamic imports/calls, nested definitions, and mutable module state.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass locally and on the macOS/Linux x CPython 3.12/3.13 CI matrix. The
  partial-scaffold disclaimer remains explicit and is not Phase 0 acceptance.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest \
  tests/sidecar/test_server_runtime.py \
  tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
the exact prebound listener serves the private sidecar app and shuts down with
fixed ownership, privacy, and signal behavior without adding launcher, READY,
storage, SDK, or telemetry scope
```

## Risks

- Omitting `sockets=[listener]`, setting `fd`, or using `uvicorn.run` can bind or
  reinterpret a second socket and break the unique-port/state contract.
- Uvicorn defaults can silently enable multiple workers through
  `WEB_CONCURRENCY`, trust proxy headers, load WebSockets, add identifying
  headers, or install/access logs. Exact Config evidence is load-bearing.
- Uvicorn does not guarantee supplied-socket cleanup when load/start/main-loop
  fails before normal shutdown. FlowSight's one close backstop must cover those
  Python-managed exits without retrying an ambiguous close or replacing
  process control.
- `callback_notify` runs after startup but before Uvicorn checks `should_exit` on
  that tick. It cannot make READY atomic with shutdown or bound an arbitrary
  blocking caller hook; later admission must still use P0-008/P0-009 health
  verification, and the trusted child hook must remain synchronous and bounded.
- A TCP connect or control-pipe BOUND message does not prove the ASGI runtime is
  serving. The real test must complete authenticated HTTP and match the exact
  child state, PID, and port.
- Default SIGTERM termination proves Uvicorn/OS teardown, not that FlowSight's
  outer Python cleanup resumed. The custom-handler replay mode separately proves
  graceful shutdown, restored handler order, normal return, and cleanup seam.

## Reviewer Focus

- Can any wrong/malformed input trigger dynamic dispatch, consume the listener,
  or reach Uvicorn before exact compatibility succeeds?
- Can any Config default/environment path allocate another listener, enable
  reload/workers/proxy/WebSocket/logging behavior, or expose identifying data?
- Does every post-transfer Python-managed exit attempt cleanup once without
  retry, false closure claims, raw-error retention, or process-control masking?
- Does the startup function prove only post-start ordering, reject async/
  generator functions before transfer, execute once, clear its reference, and
  avoid claiming atomic continued liveness across signals?
- Do real child tests prove authenticated HTTP on the supplied port and exact
  signal replay/reap semantics instead of accepting connect-only or loose exit
  evidence?
- Did the task stop before entrypoint, state publication, READY, owner/store,
  launcher/deadline, SQLite, SDK, OTel, UI, reload, or Phase 0 acceptance?

## Role Outputs

Implementer:
- Implemented one synchronous exact prebound-socket Uvicorn serving boundary,
  one captured private Config/Server path, and one domain-neutral one-shot
  post-start function. The implementation remains inside the four allowed
  product/test files and adds no child entrypoint, READY/state publication,
  launcher, storage, SDK/OTel, UI, thread, task, or alternate listener path.

Adversarial Reviewer:
- Behavior reviewer froze exact HTTP, socket identity, signal replay, cleanup,
  privacy, hook, Config, context, and dependency-fault evidence. Final review
  after removing unauthorized malformed-close seams and adding unconfounded
  canonical-run/close-only evidence reported P0/P1/P2 = 0 and GO.
- Runtime reviewer verified locked Uvicorn Config/run/shutdown behavior,
  startup ordering, signal restore/replay, the main-thread/no-running-loop
  boundary, ownership transfer, exact physical close, and ordinary/control
  arbitration. Final review reported P0/P1/P2 = 0 and GO.
- Scope/evidence reviewer confirmed the exact four-file allowlist plus task-card
  record, the Phase 0 boundary, captured provenance, strict full-tree AST
  allowlist, and absence of unauthorized seams. Final review reported
  P0/P1/P2 = 0 and GO.

Fixer:
- Applied every accepted review finding: captured immutable state/socket/thread
  primitives and canonical dependencies; made preflight caller-owned and
  transfer occur only inside the outer cleanup guard; fixed exact Config,
  notifier, error/privacy, and cleanup-control arbitration; used the base C
  socket close descriptor; and strengthened real-child plus exact-AST evidence
  until all three independent reviews reached zero findings.

Quality Governor:
- P0/P1/P2 = 0, GO. Final audit confirmed one Phase 0 serving behavior, exact
  four-file allowlist plus task-card control record, one domain-neutral
  post-start ordering hook, later P0-008/P0-009 liveness verification, and
  explicit separation from child entrypoint, state/READY, launcher/election,
  storage, SDK/OTel, UI, and later-phase work.

## Verifier Evidence

- Command: focused Ruff/pytest/mypy checks; `make test-phase0`;
  `make gate-phase0`; `.venv/bin/python scripts/validate_agent_system.py`;
  `git diff --check`
- Result: passed; focused `441 passed`, Phase 0 `2415 passed`, full/gate
  `2540 passed`, sustained Phase 0 gate passed
- Notes: candidate implementation is locally ready for CI. Final independent
  runtime, behavior/test, and Phase 0 scope reviews each report P0/P1/P2 = 0 and
  GO after every accepted finding; macOS/Linux x CPython 3.12/3.13 candidate CI
  remains pending before task completion.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record. This task
  must not relax, skip, or selectively retry that benchmark.
