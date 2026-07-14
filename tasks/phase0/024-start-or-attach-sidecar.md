# Task: Start or Attach One Project Sidecar

## Task Metadata

```yaml
task_id: P0-024
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-013, P0-014, P0-016, P0-019, P0-023, P0-025, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-024

## Phase

Phase 0

## Goal

Compose the reviewed parent-side primitives into one bounded operation that
returns an already-healthy compatible project sidecar or starts exactly one
new sidecar child and admits its verified READY outcome. The operation is an
internal sidecar primitive; a subsequent SDK-lifecycle task alone maps
`FlowSight.init_app()` onto it.

## Child-Handoff Prerequisite

P0-025 is complete. Its private version-2 bootstrap supplies a close-only
parent-handoff read descriptor: a newly exec'd child cannot adopt resources,
publish state, bind its listener, or send READY until this parent task closes
the matching writer. P0-024 must establish the reaper's exclusive wait
ownership before that close releases the child. This corrects the prior
direct-child race without adding a public protocol, configuration field, or
alternate launch path.

## Context

- P0-013 is the sole canonical bounded election/re-contention policy. Its
  exact `SidecarState` result is an incumbent, while its move-only `OwnerLock`
  result is exclusive authority to launch one child.
- P0-019 must dominate every incumbent success exit: the launcher passes the
  exact P0-013 state directly to `admit_configured_incumbent_port` and returns
  only that exact successful result. An incompatible explicit configured port
  is terminal; it is never new launch authority.
- P0-014 supplies the immutable scalar configuration. P0-016 supplies the
  canonical child argv suffix, P0-023 supplies the private executable module,
  P0-006/P0-009 supply the one-shot channel and READY admission, and P0-022
  owns child-side listener/state/server lifecycle.
- A directly spawned long-lived P0-023 child cannot be dropped after READY: a
  focused `ResourceWarning=error` probe confirmed that CPython retains an
  un-awaited `Popen` in its private active list. TRIAL-004 established the
  narrowly sufficient Phase 0 exception: after successful exec and before the
  P0-025 writer release, one private daemon reaper thread takes sole blocking
  `wait()` ownership of that one child. It has no network, store, callback,
  queue, lifecycle, retry, or public API behavior and ends when the child
  exits. Before that transfer the launcher alone may terminate and reap. After
  transfer but before the handoff-writer close attempt, an admission failure
  may terminate the child and boundedly join the reaper, but it must not make a
  competing direct `wait()` call. After that close attempt it must not
  terminate the child. This single wait-only thread is the only permitted
  exception to the no-background-worker rule for this task.
- The fixed sidecar-internal surface is:

  ```python
  def start_or_attach_sidecar(config: SidecarRuntimeConfig) -> SidecarState: ...
  ```

  It is an identical `flowsight.sidecar` export so the later SDK task can use
  one reviewed primitive; it is not a new `flowsight` root public API and it
  adds no result wrapper, process handle, callback, configuration option, or
  alternate launcher.
- The operation owns exactly one cooperative monotonic startup budget taken
  from the prepared config. It establishes that outer deadline before its first
  store/election/launch operation, calls P0-013 exactly once with its current
  positive remainder, and gives every later launch/admission stage only a
  freshly recomputed positive remainder. No stage receives the original full
  timeout after elapsed work. As elsewhere in Phase 0, this is a cooperative
  bound, not an interruption of arbitrary synchronous OS work.
- The owner path opens exactly one startup channel and one close-only
  parent-handoff pipe, encodes one canonical version-2 bootstrap with the
  exact owner/writer/handoff-reader descriptors, and executes only the current
  interpreter as `-I -m flowsight.sidecar.child_entry` with that suffix. The
  parent uses `close_fds=True`, passes only those three reviewed descriptors,
  detaches standard input/output/error, starts a separate session, and
  performs no shell, command string, source import, environment rewrite,
  listener handoff, or parent SQLite/UI work. After a successful exec boundary
  is created, the parent retires its owner and writer handles exactly once;
  P0-023 inherits them and P0-017 adopts the matching pair. The parent retains
  the startup reader and handoff writer. Before it starts the reaper, it closes
  its inherited handoff-reader raw descriptor exactly once; it has already
  closed its owner and startup-writer handles exactly once. The startup reader
  remains owned only by the later `with reader:` admission boundary. The
  handoff writer is the sole move-before-close release capability: the parent
  transfers the one direct child to the private wait-only reaper before
  attempting to close that writer; EOF then releases P0-025's child gate.
- A child READY is still only a hint. P0-009 must fresh-load/probe/reload the
  published state before this function returns it. Child FAILURE, malformed or
  absent outcome, exhausted outer budget, launch failure, or invalid local
  evidence becomes one fixed non-secret parent startup error after owned
  parent resources have been handled. Before the parent attempts to close the
  handoff writer, it owns the unpublished child and may boundedly terminate/
  reap it on failure. That close attempt is the irreversible ownership commit:
  even a close fault may follow a physically delivered EOF. After that attempt
  the child may publish attachable state, so a local admission or close failure
  and process control return failure without terminating the child; the reaper
  remains its only wait owner and later callers may independently admit it.
- Ordinary failures are exactly `RuntimeError("sidecar parent startup failed")`
  with no cause, no context, and no notes unless pre-commit cleanup fails. The
  sole cleanup note is exactly `sidecar parent startup cleanup failed`; it may
  be attached once only to an active process-control exception or fixed ordinary
  failure. Process-control exceptions preserve identity. Before the
  handoff-close attempt the parent makes one bounded best-effort terminate-and-
  wait cleanup attempt without replacing the active control; it must not claim
  Python cleanup on fail-stop signals. After the attempt it must not terminate
  a potentially attachable child. The cooperative startup budget reserves one
  fixed, documented 250 ms terminal cleanup grace before that attempt so the
  unpublished failure path can always attempt `terminate()` followed by one
  bounded `wait()`, even when admission exhausts its allocated remainder.
  Ordinary error paths expose neither project/runtime path, token, PID, port,
  descriptor, command, child output, raw exception, nor timeout in text, repr,
  logging, output, callback, cache, or retained frame.
- This task intentionally does not touch `FlowSight`, FastAPI app mutation,
  OTel, sender queues, leases, reload hooks, SDK flush/shutdown, SQLite writes,
  UI opening, or sidecar idle-stop policy. Those remain separately scoped work.
- Source of truth: MVP design sections 2.2, 4.2, 4.4, and Phase 0; FS-001,
  FS-007, FS-008, FS-023; and the TRIAL-004 promotion requirements.

## Related Fact IDs

- FS-001
- FS-007
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/parent_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_parent_runtime.py`
- `tests/sidecar/test_runtime_config.py`

`flowsight/sidecar/__init__.py` may change only for the exact import and one
`__all__` entry. `tests/sidecar/test_runtime_config.py` may change only for its
exact sidecar-export and public-submodule expectations.

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/parent_runtime.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_parent_runtime.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not change P0-001 through P0-023 implementations, reimplement their
  codecs/election/admission/child transaction, or bypass P0-019/P0-023 with an
  alternate state or child path.
- Do not modify `flowsight/sdk/`, `flowsight/__init__.py`, FastAPI apps,
  lifecycle hooks, OTel instrumentation, queues, sender/lease protocols,
  shutdown/idle policy, SQLite/store schema, UI/API routes, browser behavior,
  dependencies, packaging, Makefile, or spike code.
- Do not make a second owner election, second startup channel, second
  parent-handoff pipe, second long-lived sidecar, listener descriptor handoff,
  retry loop, async task,
  mutable registry/cache, caller-selected command/environment/poll interval,
  or unbounded parent wait. The sole permitted background thread starts only
  after successful exec and before parent-handoff EOF release, owns one direct
  P0-023 child, performs exactly one canonical blocking `wait()`, has no other
  side effect or API, and exits with that child. Before the handoff-writer
  close attempt the parent may terminate and boundedly join it on admission
  failure, but may not make a competing direct `wait()` call. After that
  attempt it must not terminate the child. It is not a general reaping or
  daemonization API and may not create a second server.
- Do not accept a project path, port, raw timeout, owner, reader/writer,
  command, descriptor, subprocess object, callback, or `None` as an alternate
  public input/result. Do not expose a child process handle or make callers
  responsible for its resource cleanup.
- Do not start a child for an incumbent mismatch, untrusted election result,
  deadline failure, malformed dependency result, or error. Do not attach to an
  unverified READY message or treat a failure message as state evidence.
- Do not print/log child output or raw failures; do not use shell execution,
  inherited listener descriptors, `preexec_fn`, arbitrary `cwd`, user-defined
  environment mappings, process polling loops, SIGKILL as normal control flow,
  or best-effort cleanup as a successful startup result.

## Acceptance Criteria

- [x] `start_or_attach_sidecar` is an identical `flowsight.sidecar` export,
  appears once in `__all__`, has exact signature
  `(config: SidecarRuntimeConfig) -> SidecarState`, and adds no root public API
  or public result/error/configuration type.
- [x] Exact-config preflight safely admits only the scalar fields required by
  the reviewed primitives. A malformed exact object, ordinary dependency
  failure, invalid monotonic observation, or exhausted/rolled-back/non-finite
  deadline fails closed with one fixed parent error before any unauthorized
  later work; non-`Exception` control preserves identity.
- [x] One finite, non-regressing outer deadline begins before store/election/
  launch work. It calls canonical P0-013 once with a positive current remainder
  and every following channel, launch, cleanup, and P0-009 call receives only a
  recomputed positive remainder. Deterministic clock/call-order tests prove no
  full-timeout reuse, extra election, stage after expiry, busy loop, or extra
  child. An exact P0-013 state or owner result still requires a fresh positive
  outer remainder before incumbent admission or owner-path work; insufficient
  time to reserve the fixed cleanup grace prevents launch.
- [x] An exact P0-013 `SidecarState` takes the no-launch branch, reaches
  `admit_configured_incumbent_port` exactly once, and returns its exact state
  directly with no channel, command, process, or later work. Its explicit-port
  incompatibility is terminal and starts no child.
- [x] An exact P0-013 `OwnerLock` is the only launch authority. The owner branch
  opens one reviewed channel and one close-only handoff pipe, obtains the three
  exact live child descriptors, creates the canonical P0-016 v2 suffix, and
  launches exactly one isolated direct P0-023 child with only those three
  descriptors. Parent/child descriptor ownership and close order are
  mechanically proved: before reaper start the parent retires owner,
  startup-writer, and handoff-reader exactly once; the startup reader is
  consumed only by `with reader:`; the handoff writer is moved before its sole
  irreversible close attempt. No path closes child-owned descriptors.
- [x] The child branch consumes one outcome through P0-009 under the remaining
  outer budget in exactly one `with reader:` ownership boundary and returns
  only the exact verified `SidecarState`. It never naked-closes or retries that
  reader. READY/state mismatch, generic child FAILURE, malformed/absent channel
  evidence, failed preflight/launch/admission, reader cleanup fault, and
  deadline expiry expose only the fixed parent error and cannot leak sensitive
  scalar or subprocess information. A failure before the handoff-writer close
  attempt terminates the unpublished child; a local or close failure after that
  attempt must not terminate the now-attachable child.
- [x] Before the parent releases the handoff writer, it transfers the newly
  launched child to exactly one private wait-only reaper. The child cannot
  publish state, bind, or send READY before that EOF release. If a newly
  unpublished child cannot reach the handoff-writer close attempt, the parent
  terminates then boundedly joins that reaper using the reserved fixed terminal
  cleanup grace;
  it never makes a competing direct child `wait()` after transfer. Before a
  failed transfer, the parent alone may terminate and wait. After the close
  attempt, a local admission or close failure does not terminate the child or
  reaper. A pre-commit cleanup failure is visible in the fixed startup failure
  (or as the one fixed note on an active process-control exception) and never
  yields a false success. On success, after verified state admission and reader
  retirement, the already-started private daemon reaper remains the canonical
  blocking wait owner; it neither polls nor terminates the healthy long-lived
  sidecar.
- [x] Real isolated-process evidence, using a temporary project and an isolated
  per-test runtime root, proves initial launch reaches authenticated health,
  the child remains gated until reaper ownership is established, then is held
  by that wait-only reaper without a `ResourceWarning`,
  state identity is returned, concurrent callers reconnect to the exact
  incumbent without a second long-lived child, an explicit mismatched port
  fails without launch, a pre-READY child failure is synchronously contained,
  a post-EOF local admission failure does not terminate an attachable child,
  and deterministic pre-EOF cleanup leaves no child or descriptor behind.
  Tests use observable bounded conditions rather than sleeps and clean up their
  child process/state.
- [x] Static and behavioral tests freeze captured canonical dispatch, exact
  command shape, `-I`, `pass_fds`, detached stdio/session, one election/channel/
  child/admission, deadline propagation, error privacy, no shell or listener
  inheritance, no SDK/OTel/store-write/UI code, no unreviewed mutable state,
  and no background work other than the exact one-child wait-only reaper.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase
  0 gate pass locally and on the macOS/Linux × CPython 3.12/3.13 CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest \
  tests/sidecar/test_parent_runtime.py \
  tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
one bounded parent sidecar start-or-attach transaction passes all checks
without adding SDK, telemetry, storage, or UI behavior
```

## Risks

- A loser that starts instead of admitting the exact compatible incumbent can
  create split-brain UI/listener/SQLite ownership.
- Passing the full timeout to more than one stage can multiply startup latency
  or leave a parent waiting after an already-expired startup decision.
- Closing either parent descriptor before the exec boundary, or retaining it
  after a child failure, can respectively lose the lock/channel handoff or
  wedge later startup.
- A parent-side failure after child launch can leave a hidden live process;
  bounded cleanup must be explicit while successful children remain detached
  for reload reconnection.
- The only correct Phase 0 behavior here is sidecar process ownership. SDK
  registration, telemetry ownership, multi-worker lease rejection, and
  shutdown remain later, independently tested slices.

## Reviewer Focus

- Does every incumbent exit structurally pass through P0-019 and every owner
  path through exactly one P0-016/P0-023 child launch?
- Can any deadline boundary, malformed seam result, channel error, or cleanup
  condition create a second child, stale full timeout, false attachment, raw
  leak, zombie, or orphaned descriptor?
- Does success prove P0-009's fresh state/health/state admission rather than
  trusting the child's READY payload?
- Is the real process test isolated from user runtime state and deterministic
  in its child cleanup, while preserving macOS/Linux and CPython 3.12/3.13
  support?
- Did this stay out of the SDK/OTel/sender/lease/shutdown/UI scope reserved for
  later Phase 0/1 tasks?

## Role Outputs

Implementer:

- Commit `d9e0053` adds `start_or_attach_sidecar` to
  `flowsight/sidecar/parent_runtime.py` as a pure composition of the reviewed
  primitives: one preflight/deadline via `_prepare_election`, exactly one
  P0-013 call via `_elect_once`, incumbent exits only through
  `admit_configured_incumbent_port`, and an owner branch that opens one
  channel plus one handoff pipe, launches one canonical P0-023 child, retires
  owner/startup-writer/handoff-reader before reaper start, transfers the child
  to the single wait-only reaper before the sole irreversible handoff-writer
  close, and consumes one P0-009 outcome inside one `with reader:` boundary.
  The owner path reserves the fixed 250 ms terminal cleanup grace
  (`deadline - _CLEANUP_GRACE_SECONDS`) for every pre-commit stage budget so
  the unpublished failure path can always terminate and boundedly reap.
  `flowsight/sidecar/__init__.py` gained only the exact import and one
  `__all__` entry.
- Commit `effff6b` adds composition unit tests (deterministic clocks,
  call-order events, fixed-error shape, descriptor-closure tracking) and six
  real isolated-process acceptance tests driven through inherited
  `XDG_RUNTIME_DIR`/`HOME` isolation so the canonical `-I` child re-derives
  the same per-test runtime root.

Adversarial Reviewer:

- Reviewer 1: reviewed single-election/deadline, incumbent admission, child
  handoff, descriptor ownership, fixed-error privacy, and scope boundaries;
  all accepted findings were closed before the candidate push.
- Reviewer 2: the independent closeout review found the factory-construction
  and malformed-channel cleanup gaps, then re-reviewed the final fix diff and
  reported no remaining blocker, resource leak, API expansion, or phase creep.
- The closeout review found two pre-commit cleanup gaps before push: a raised
  `_NEW_CHILD_HANDOFF`/`_NEW_WAIT_ONLY_REAPER` factory could bypass child and
  gate retirement, and a malformed startup-channel result containing exact
  known endpoints could leave their descriptors outside the ordinary cleanup
  path. Both findings were accepted as P0-024 lifecycle defects and fixed in
  the allowed parent-runtime/test files before final verification.
- Verified every incumbent exit passes through `_admit_incumbent` →
  `admit_configured_incumbent_port` exactly once and returns the exact state;
  explicit-port incompatibility raises the fixed error with no channel or
  spawn work (frozen by launch-guard fakes).
- Verified single election, no full-timeout reuse (election gets 4.8 s and
  admission 3.95 s of a 5 s budget under the scripted clock), no stage after
  expiry, no retry loop, and no second child on any failure path.
- Walked every failure seam for commit-boundary correctness: before the
  handoff-writer close attempt the child is terminated and boundedly reaped
  (direct wait before transfer, reaper join after); after the close attempt
  (`_ChildHandoff.committed`) no path terminates the child, including
  admission faults, reader-exit faults, and close faults; process-control
  exceptions preserve identity and receive the single cleanup note only when
  cleanup actually failed.
- Checked descriptor lifecycles: each of startup reader/writer and handoff
  reader/writer is closed exactly once per flow (fstat-based unit assertions);
  the handoff writer is deliberately retained only when an unconfirmed child
  cleanup means the gate must stay held.
- Confirmed no SDK/OTel/store-write/UI code, no new public error/result type,
  and the module import surface is frozen by an AST test.
- Environmental notes (not diff findings): `tests/sidecar/test_state.py::
  test_load_rejects_equal_length_rewrite_and_atomic_path_replacement` fails
  deterministically on this sandbox's mounted filesystem (lstat-signature
  semantics) at baseline `2804db5` too, and `tests/sidecar/
  test_runtime_config.py::test_platformdirs_and_state_store_are_called_once_
  without_creating_runtime_paths` is intermittently flaky there for the same
  reason; both are untouched by this diff.

Fixer:

- Added a raw pre-commit containment path for child-handoff construction
  failure, routed reaper-construction failure through the existing
  `_ChildHandoff` cleanup boundary, and made a failed handoff-writer move close
  the still-owned gate only after the child is synchronously reaped. Added
  focused regression tests for both factories, the writer-move seam, fixed
  error/context shape, and descriptor retirement.
- Added fail-closed retirement of exact `StartupReader`/`StartupWriter`
  endpoints found inside a malformed tuple/list channel result, without
  admitting that malformed result or starting handoff/child work.
- Only review-driven adjustment applied during implementation: the handoff
  writer is moved into `_ChildHandoff` even when handle retirement raised a
  control exception, so a reaped pre-commit cleanup can still retire the gate
  descriptor instead of leaking it; post-commit unit tests switched to the
  fake reaper so `wait_calls` counts only parent-side waits.

Verifier:

- See Verifier Evidence below; commands were run exactly as recorded, from an
  equivalent CPython 3.12.3 Linux virtualenv because this session's sandbox
  cannot execute the repository's macOS `.venv`.

Quality Governor:

- Task stayed inside its four allowed product files plus this card; commits
  keep `in_progress`/`review` discipline with the evidence recorded as a
  control-plane-only change; no rule drift observed that requires doc updates.
  Final completion is supported by GitHub Actions run `29292851749`, which
  passed the macOS/Linux × CPython 3.12/3.13 matrix for candidate `853dc08`.

## Verifier Evidence

- Command: focused parent-runtime/runtime-config pytest; `make test-phase0`,
  `make check`, and `make gate-phase0` with the recorded CPython 3.13 editable
  venv; GitHub Actions `agent-checks` run `29292851749`.
- Result: passed
- Notes: focused tests passed 370 cases, Phase 0 passed 2 641 cases, repository
  checks passed 2 766 cases, the sustained gate passed, and Ubuntu/macOS ×
  CPython 3.12/3.13 all passed for candidate `853dc08`.
- Closeout environment: macOS x86_64, CPython 3.13.5. The repository `.venv`
  passed the focused suite but its isolated `flowsight.sidecar` import exceeded
  the unchanged 2-second P0-025 real-child fixture threshold, so final long
  targets used a temporary CPython 3.13.5 conda-based editable venv with the
  same pinned project/dev dependencies. No repository dependency or test
  timeout was changed.
- `.venv/bin/python -m pytest tests/sidecar/test_parent_runtime.py
  tests/sidecar/test_runtime_config.py`: 370 passed (23.99 s).
- New closeout regressions selected from `test_parent_runtime.py`: 4 passed;
  targeted Ruff and mypy checks passed.
- `make test-phase0 PYTHON=<temporary-venv>/bin/python`: 2 641 passed
  (118.53 s).
- `make check PYTHON=<temporary-venv>/bin/python`: agent-system validation,
  guard/formatter/staged-file/product-detection tests, Ruff format/lint, strict
  mypy (42 source files), and 2 766 Python tests passed (pytest 119.91 s).
- `make gate-phase0 PYTHON=<temporary-venv>/bin/python`: repeated the complete
  repository check with 2 766 tests passing (118.97 s), then reported
  `gate phase0-sustained passed`.
- Environment: sandboxed Linux (x86_64, glibc 2.35 host), CPython 3.12.3
  virtualenv with the repository's pinned dev dependencies; the repository's
  own macOS `.venv` is not executable here. Commands used
  `PYTHON=/tmp/fsvenv/bin/python` (the venv interpreter) where the Makefile
  default expects `.venv/bin/python`, and long targets were executed as their
  exact constituent commands because this environment kills any process after
  45 seconds.
- `python -m pytest tests/sidecar/test_parent_runtime.py
  tests/sidecar/test_runtime_config.py`: 366 passed (14.28 s).
- `make test-phase0` scope, run as its exact pytest constituents:
  `tests/test_sdk_skeleton.py tests/security/test_safe_summary.py
  tests/store/test_wal_writer.py` 97 passed;
  `tests/packaging/test_wheel_runtime_dependency.py` 13 passed;
  `tests/sidecar` 2 526 passed across chunks (637 + 522 + 754 + 26 + 222 +
  366 with one skip-free rerun), with the two pre-existing
  environment-dependent failures noted in the review recorded against
  baseline `2804db5` as well.
- `make check` scope: `ruff format --check` (71 files clean), `ruff check`
  (clean), `mypy` strict (42 source files, no issues), `make check-agent`
  constituents (`validate-agent-system` passed; `test-hooks` 158 deny/80
  allow passed; `test-formatter`, `test-allowlist`, `test-product-detection`,
  `test-validator` all OK), full pytest additionally covering
  `tests/spikes/test_tracepoint_backend.py` 36 passed and
  `tests/spikes/test_sidecar_otel_lifecycle.py` 89 passed (chunked).
- `make gate-phase0` validator: `scripts/validate_agent_system.py --gate
  phase0-sustained` → `gate phase0-sustained passed` (after the `make check`
  scope above).
- Pre-commit hook (staged-path boundary + snapshot `make check-fast`) passed
  for product commits `d9e0053`, `effff6b`, and closeout fix `853dc08`.
- GitHub Actions run `29292851749` passed for candidate `853dc08`: Ubuntu
  CPython 3.12/3.13 and macOS CPython 3.12/3.13 all completed the shared
  `make check` job successfully.

## Failure Queue Items

- None at planning time.
