# Task: Admit One Configured Incumbent Port

## Task Metadata

```yaml
task_id: P0-019
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-010, P0-013, P0-014, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-019

## Phase

Phase 0

## Goal

Admit one exact already-verified incumbent state only when the configured port
policy permits attachment, returning that same state identity or one fixed
non-secret incompatibility error without creating launch authority or touching
runtime lifecycle.

## Context

- P0-010 returns one exact freshly verified incumbent `SidecarState`, but
  deliberately leaves requested-port compatibility to later attachment or
  election policy. P0-013 returns that exact state or an exact move-only
  `OwnerLock`, and likewise defers port policy. P0-014 freezes
  `requested_port`: `None`, dynamic `0`, or explicit `1..65535`.
- P0-018 requires configured incumbent ports to be admitted before a future
  parent launcher can attach. P0-019 is that port-policy-only prerequisite; it
  is parallel to P0-018 rather than a child-preparation dependency.
- The fixed public surface is:

  ```python
  def admit_configured_incumbent_port(
      config: SidecarRuntimeConfig,
      incumbent: SidecarState,
  ) -> SidecarState: ...
  ```
- Success means only: an exact incumbent port is compatible with the exact
  configured request. It does not prove health, project/store provenance,
  temporal freshness, owner-lock continuity, or attachment success. P0-010 and
  P0-013 own their canonical success postconditions; this task neither repeats
  nor weakens them.
- The accepted policy from TRIAL-004 is exact: `requested_port is None` or `0`
  accepts any admitted incumbent port; explicit `1..65535` accepts only the
  same incumbent port. Incompatibility is exactly the built-in
  `RuntimeError("configured incumbent port is incompatible")`. The message is
  the safe production manifestation of the spike's
  `EXPLICIT_PORT_MISMATCH`; it exposes neither requested nor actual port.
- Wrong top-level types fail exactly with
  `TypeError("config must be an exact SidecarRuntimeConfig")` and
  `TypeError("incumbent must be an exact SidecarState")`. An exact object with
  a missing or malformed admitted port slot fails through the same fixed
  incompatibility error rather than adding a public identity/provenance
  taxonomy.
- Production captures the canonical built-in slot getters for
  `SidecarRuntimeConfig.requested_port` and `SidecarState.port` at import. This
  avoids unknown property or public class-attribute dispatch. Private helpers
  may only delegate to those captured getters or synchronously fail before
  returning; forged successful slot values are outside the seam contract.
- An integrated `config -> StateStore -> wait_for_owner_election -> state |
  owner` wrapper is intentionally not this slice. It would add another
  move-only owner return-transfer window, prematurely choose how the total
  startup deadline is divided, and duplicate election failure/privacy work.
  The future launcher must establish one cooperative outer deadline before its
  first store/election/launch operation, own one store, call P0-013 once with
  the current positive remaining time, and give every later startup-admission
  stage only the newly computed positive remaining time. No stage may reset to
  the full `config.startup_timeout`. Every exact state success exit must pass
  through the captured P0-019 helper; an owner goes directly into the
  child-start path.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 4.2, 7.4, and Phase 0
  - `spikes/sidecar_otel/RESULT.md` and TRIAL-004 runtime evidence
  - P0-010, P0-013, P0-014, and P0-018 contracts

## Related Fact IDs

- FS-001
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/incumbent_port.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_incumbent_port.py`
- `tests/sidecar/test_runtime_config.py`

`flowsight/sidecar/__init__.py` may change only for the exact import and
`__all__` entry. `tests/sidecar/test_runtime_config.py` may change only for its
exact sidecar-export and public-submodule expectations.

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/incumbent_port.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_incumbent_port.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not accept a `StateStore`, `OwnerLock`, `None`, union outcome, timeout,
  deadline, callback, or caller-selected error policy. Do not construct a
  store; call discovery, health, election, or wait; read a clock; retry; sleep;
  or claim temporal/project/store/database provenance.
- Do not read any config field except `requested_port` or any state field
  except `port`. Do not read or expose a project ID, startup ID, PID, host,
  token, database path, runtime/project path, requested/actual port value, or
  full config/state repr. An identity-preserved non-`Exception` raised by a
  private getter seam may retain its preexisting payload and dependency
  traceback; production must clear its own config/state/port locals before
  propagation and must never inspect, format, log, emit, or cache that control.
- Do not acquire, inspect, transfer, close, or otherwise touch an `OwnerLock`.
  Do not create launch authority, interpret incompatibility as permission to
  elect/start/replace, or add an integrated state/owner resolution wrapper.
- Do not load, publish, remove, repair, rename, reconstruct, or serialize state;
  bind a listener; open a startup channel; send/receive READY; create FastAPI or
  Uvicorn runtime; open SQLite; or add SDK, OTel, UI, browser, sender, lease,
  shutdown, reload, thread, async, subprocess, command, environment, or
  descriptor behavior.
- Do not return a boolean, `None`, copied state, tuple, enum, wrapper, callback,
  side result, or cache entry. Do not add a public error class/code or another
  success/failure surface.
- Do not call unknown object methods, properties, serializers, repr, equality,
  hashing, iterators, or conversion protocols. Do not add mutable module state,
  defaults, registry, cache, logging, or output.
- Do not change P0-010/P0-013/P0-014/P0-018 implementations, import spike code,
  change dependencies or the Makefile, or weaken the future launcher
  requirement that this helper dominate every incumbent success exit.

## Acceptance Criteria

- [x] `admit_configured_incumbent_port` is exported identically from
  `flowsight.sidecar`, occurs exactly once in `__all__`, has the fixed signature
  above, and is the only new production surface. It adds no public class,
  error taxonomy, result wrapper, constant, or alternate admission function.
- [x] Only exact `SidecarRuntimeConfig` and exact `SidecarState` inputs reach
  slot work. Wrong config type fails with the fixed config `TypeError` before
  inspecting the incumbent; wrong incumbent type fails with the fixed
  incumbent `TypeError` before either slot getter. Derived, duck, proxy, or
  coercible inputs cannot run attribute/property/protocol dispatch.
- [x] Production captures the two canonical built-in slot getters at import
  and calls each exactly once through its own private delegate. Public package,
  module, or class-slot replacement cannot redirect dispatch. Private delegates
  may call the captured getter or synchronously fail before returning; they may
  not forge a successful value.
- [x] The requested port must be exact `None` or an exact built-in `int` in
  `0..65535`; the incumbent port must be an exact built-in `int` in `1..65535`.
  Missing, subclassed, boolean, out-of-range, or malformed exact-object slots
  fail closed with the fixed incompatibility error and no later work.
- [x] `requested_port is None` and exact `0` each accept incumbent boundary
  ports `1` and `65535` plus representative `4040`. No default-port or bind
  policy changes either rule.
- [x] Every explicit exact port in `1..65535` succeeds only when it equals the
  incumbent port. Boundary matches `1 == 1` and `65535 == 65535` succeed;
  representative boundary and ordinary mismatches fail without retry,
  alternate result, or launch/election authority.
- [x] Every success returns the original exact `incumbent` object directly.
  The result retains no config, requested-port scalar, callback, wrapper,
  store, timeout, deadline, or extra state copy; no work occurs after the exact
  state return is selected.
- [x] Every exact-object slot failure and explicit mismatch becomes exactly
  `RuntimeError("configured incumbent port is incompatible")`, raised `from
  None` only after internal slot/comparison frames and sensitive locals are
  gone. Without caller-active context, cause/context/notes are empty. A
  caller-active Python-managed context may remain only as suppressed context.
  Fixed error text, formatted output, and P0-019 traceback locals contain no
  config/state identity, requested/actual port, token, PID, or path.
- [x] Ordinary private getter failure is not retained after restoring the seam;
  synchronous `KeyboardInterrupt`, `SystemExit`, and a custom non-`Exception`
  `BaseException` preserve object identity, payload, notes, and dependency
  traceback with no later getter. Before propagation, every P0-019 frame has
  deleted config/state/port locals; production does not inspect or format the
  control. Caller-active `ValueError` and `KeyboardInterrupt` retain
  identity/notes unchanged across success, fixed failure, and process control.
- [x] Tests prove no stdout/stderr/log output, filesystem change, state
  mutation/serialization, network, lock, clock, wait, process, thread, async,
  callback, cache, or runtime behavior. The incumbent's complete field values
  and the config remain unchanged across every result.
- [x] A positive full-tree AST allowlist freezes exact imports, immutable
  captured slot dispatch, marker/helper/public surfaces, signatures, calls,
  raises, handler ownership, direct identity return, and absence of unreviewed
  nested definitions or mutable state. It rejects StateStore/discovery/health/
  election/wait/OwnerLock, other config/state fields, ports in error text,
  state methods, serialization, listener/channel/READY/process/SDK/storage/
  OTel/UI behavior, dynamic calls, logging, output, and mutation.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass locally and on the macOS/Linux x CPython 3.12/3.13 CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest \
  tests/sidecar/test_incumbent_port.py \
  tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
configured incumbent port admission tests and all repository checks pass
without creating election, owner, launch, listener, or runtime behavior
```

## Risks

- Treating a port mismatch as negative discovery or new launch authority can
  create a second sidecar or attach against explicit user intent. The mismatch
  is terminal fixed failure for this decision.
- This helper intentionally cannot prove that the state came from the current
  store/election. Adding a store parameter would only compare values, not prove
  temporal provenance. The future launcher must pass the exact canonical
  P0-013 state immediately and structurally dominate every state success exit
  with this call.
- Wrapping P0-013 here would widen its move-only owner transfer gap and could
  spend a full election timeout before a later startup timeout. The future
  launcher, not this helper, owns the single outer deadline and owner-to-child
  transition.

## Reviewer Focus

- Can any inexact object or malformed slot reach dynamic dispatch or success?
- Can any requested/actual port, state/config identity, token, PID, or path
  escape through a fixed error, traceback local, output, callback, or cache?
- Can mismatch become `None`, boolean, state, owner, retry, cleanup, or launch
  authority instead of terminal fixed failure?
- Does production read only the two canonical port slots and return the exact
  state directly, with no store/election/timeout/owner/runtime behavior?
- Is the contract explicit that future launcher structure, rather than this
  pure helper, enforces call dominance and one overall startup deadline?

## Role Outputs

Implementer:
- Added the two-input pure port boundary with exact input checks, captured
  canonical slot getters, exact integer/range policy, fixed non-secret errors,
  process-control propagation with production-local cleanup, and direct
  incumbent identity return. Exported only the fixed public helper.
- Added focused behavioral, privacy, process-control, side-effect, public API,
  malformed-value, hostile-protocol, and positive full-tree AST tests.

Adversarial Reviewer:
- Reviewer 1: found no production defect but reported P1=3/P2=1
  test-survivable regressions: incumbent validation could collapse into
  mismatch, hostile scalar protocols were not constrained, control injection
  bypassed production reader frames, and one wrong-config privacy assertion
  omitted its incumbent sentinel. Final review after the accepted fixes
  reported P0/P1/P2 = 0 and GO.
- Reviewer 2: independently verified runtime privacy, exception identity,
  captured slot dispatch, malformed values, and exact identity return after the
  fixes; final P0/P1/P2 = 0 and GO.
- The Phase 0 scope reviewer confirmed the exact allowlist, AST policy, and
  absence of lifecycle or non-goal drift; final P0/P1/P2 = 0 and GO.

Fixer:
- Made malformed incumbent checks independent of mismatch, including equal
  boolean/int-subclass/float shapes; added hostile comparison/conversion/
  truthiness objects with zero-dispatch assertions; injected process control at
  captured getter constants so real reader frames are scrubbed; and tracked the
  incumbent sentinel on wrong-config preflight. Production code was unchanged
  by these accepted review fixes.

Quality Governor:
- P0/P1/P2 = 0, GO. Final audit confirmed the exact four-file product allowlist,
  task-card-only control-plane update, Phase 0 port-policy boundary, AST policy,
  and absence of store, election, owner, timeout, I/O, lifecycle, dependency,
  Makefile, or v1 non-goal drift.

## Verifier Evidence

- Command: focused pytest; `make test-phase0`; `make gate-phase0`; Ruff; mypy;
  agent-system, diff, and staged-file validation; candidate CI matrix
- Result: passed
- Notes: Focused tests passed 343 cases; `make test-phase0` passed 2273 cases;
  and `make gate-phase0` ran the full `make check` path with 2398 passing tests
  before `phase0-sustained` passed. The expected partial-scaffold notice
  remained and was not reported as Phase 0 acceptance. The staged-file guard
  admitted exactly the four indexed P0-019 product paths. Candidate commit
  `03ba07ec416005eaa0ed4038b51afa44b5ed0469` passed GitHub Actions run
  `29173530861` on Ubuntu and macOS with CPython 3.12/3.13; successful job IDs
  were `86598483300`, `86598483301`, `86598483303`, and `86598483318`. Final
  independent behavior, runtime/privacy, and Phase 0 scope reviews each
  reported P0/P1/P2 = 0 and GO.

## Failure Queue Items

- FSQ-0001 remains an unrelated Phase 4 benchmark-variance record. This task
  must not relax, skip, or selectively retry that benchmark.
