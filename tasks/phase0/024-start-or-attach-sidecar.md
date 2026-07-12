# Task: Start or Attach One Project Sidecar

## Task Metadata

```yaml
task_id: P0-024
release: v1
task_type: implementation
status: planned
primary_phase: phase0
impacted_phases: []
depends_on: [P0-013, P0-014, P0-016, P0-019, P0-023, TRIAL-004]
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
- The owner path opens exactly one startup channel, encodes one canonical
  bootstrap with the exact owner/writer descriptors, and executes only the
  current interpreter as `-I -m flowsight.sidecar.child_entry` with that suffix.
  It uses `close_fds=True`, passes only the two reviewed descriptors, detaches
  standard input/output/error, starts a separate session, and performs no
  shell, command string, source import, environment rewrite, listener handoff,
  or parent SQLite/UI work. After a successful exec boundary is created, the
  parent retires its owner and writer handles exactly once; the child inherits
  and P0-017 adopts the matching pair. The parent retains only the reader until
  P0-009 consumes it.
- A child READY is still only a hint. P0-009 must fresh-load/probe/reload the
  published state before this function returns it. Child FAILURE, malformed or
  absent outcome, exhausted outer budget, launch failure, or invalid local
  evidence becomes one fixed non-secret parent startup error after owned
  parent resources have been handled. The one newly launched process is
  boundedly stopped/reaped on such a failed admission; success deliberately
  leaves the independently owned sidecar alive for reload reconnection.
- Process-control exceptions preserve identity. Once child ownership exists,
  the parent makes one bounded best-effort process/resource cleanup attempt
  without replacing the active control; it must not claim Python cleanup on
  fail-stop signals. Ordinary error paths expose neither project/runtime path,
  token, PID, port, descriptor, command, child output, raw exception, nor
  timeout in text, repr, logging, output, callback, cache, or retained frame.
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
- Do not make a second owner election, second startup channel, second child,
  listener descriptor handoff, retry loop, background worker, async task,
  mutable registry/cache, caller-selected command/environment/poll interval,
  or unbounded process wait.
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

- [ ] `start_or_attach_sidecar` is an identical `flowsight.sidecar` export,
  appears once in `__all__`, has exact signature
  `(config: SidecarRuntimeConfig) -> SidecarState`, and adds no root public API
  or public result/error/configuration type.
- [ ] Exact-config preflight safely admits only the scalar fields required by
  the reviewed primitives. A malformed exact object, ordinary dependency
  failure, invalid monotonic observation, or exhausted/rolled-back/non-finite
  deadline fails closed with one fixed parent error before any unauthorized
  later work; non-`Exception` control preserves identity.
- [ ] One finite, non-regressing outer deadline begins before store/election/
  launch work. It calls canonical P0-013 once with a positive current remainder
  and every following channel, launch, cleanup, and P0-009 call receives only a
  recomputed positive remainder. Deterministic clock/call-order tests prove no
  full-timeout reuse, extra election, stage after expiry, busy loop, or extra
  child.
- [ ] An exact P0-013 `SidecarState` takes the no-launch branch, reaches
  `admit_configured_incumbent_port` exactly once, and returns its exact state
  directly with no channel, command, process, or later work. Its explicit-port
  incompatibility is terminal and starts no child.
- [ ] An exact P0-013 `OwnerLock` is the only launch authority. The owner branch
  opens one reviewed channel, obtains the two exact live descriptors, creates
  the canonical P0-016 suffix, and launches exactly the isolated private entry
  with only those two descriptors. Parent/child descriptor ownership and close
  order are mechanically proved; all ordinary post-launch paths retire parent
  owner/writer/reader resources exactly once without closing child-owned
  descriptors.
- [ ] The child branch consumes one outcome through P0-009 under the remaining
  outer budget and returns only the exact verified `SidecarState`. READY/state
  mismatch, generic child FAILURE, malformed/absent channel evidence, failed
  preflight/launch/admission, and deadline expiry expose only the fixed parent
  error and cannot leak sensitive scalar or subprocess information.
- [ ] If a newly launched child cannot be admitted, the parent performs one
  bounded cleanup/reap sequence for that exact process. A cleanup failure is
  visible in the fixed startup failure (or as a fixed note on an active
  process-control exception) and never yields a false success; successful
  startup neither polls nor terminates the long-lived sidecar.
- [ ] Real isolated-process evidence, using a temporary project and an isolated
  per-test runtime root, proves initial launch reaches authenticated health,
  state identity is returned, a concurrent/second caller reconnects to the
  exact incumbent without a second child, an explicit mismatched port fails
  without launch, a pre-READY child failure is contained, and deterministic
  cleanup leaves no child or descriptor behind. Tests use observable bounded
  conditions rather than sleeps and clean up their child process/state.
- [ ] Static and behavioral tests freeze captured canonical dispatch, exact
  command shape, `-I`, `pass_fds`, detached stdio/session, one election/channel/
  child/admission, deadline propagation, error privacy, no shell or listener
  inheritance, no SDK/OTel/store-write/UI code, and no unreviewed mutable state
  or background work.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase
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

- Pending.

Adversarial Reviewer:

- Pending.

Fixer:

- Pending.

Verifier:

- Pending.

Quality Governor:

- Pending.

## Verifier Evidence

- Planned: record the focused parent-runtime/runtime-config result, Phase 0
  suite, repository check, sustained gate, candidate commit, and supported CI
  matrix before this task can become complete.

## Failure Queue Items

- None at planning time.
