# Task: Freeze the Versioned Sidecar Child Bootstrap Arguments

## Task Metadata

```yaml
task_id: P0-016
release: v1
task_type: implementation
status: complete
primary_phase: phase0
impacted_phases: []
depends_on: [P0-004, P0-006, P0-014, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-016

## Phase

Phase 0

## Goal

Encode one exact prepared sidecar runtime configuration and the two future
inherited descriptor numbers into a canonical, bounded, versioned argv suffix,
then decode it in child context by re-deriving the trusted configuration without
starting a process or adopting any descriptor.

## Context

- P0-014 freezes the five exact scalar configuration fields and deliberately
  leaves their child wire protocol to the next task. P0-004 and P0-006 define
  the inherited owner-lock and startup-writer descriptor meanings; this task
  transfers only their integer identifiers, not their ownership.
- P0-015 proves an installed wheel supplies Uvicorn, but neither this task nor
  its tests import, configure, or start Uvicorn.
- The approved TRIAL-004 process path passes only the owner-lock and startup
  writer descriptors. The child sidecar itself later calls the P0-003 atomic
  loopback bind primitive, preserving the rule that only the sidecar owns the
  UI/API listener; no listener descriptor belongs in this protocol.
- The fixed public-internal surface is:

  ```python
  CHILD_BOOTSTRAP_SCHEMA_VERSION = 1

  class SidecarChildBootstrap:
      config: SidecarRuntimeConfig
      owner_lock_fd: int
      startup_writer_fd: int

  def encode_sidecar_child_bootstrap(
      config: SidecarRuntimeConfig,
      *,
      owner_lock_fd: int,
      startup_writer_fd: int,
  ) -> tuple[str, ...]: ...

  def decode_sidecar_child_bootstrap(
      arguments: tuple[str, ...],
  ) -> SidecarChildBootstrap: ...
  ```
- Every ordinary codec failure is exactly the built-in
  `ValueError("sidecar child bootstrap is invalid")`. Direct result
  construction is exactly
  `TypeError("SidecarChildBootstrap must be decoded")`; copy, deepcopy,
  pickle reduction, and state serialization fail exactly with
  `TypeError("SidecarChildBootstrap cannot be serialized")`; mutation fails
  exactly with `AttributeError("SidecarChildBootstrap is immutable")`. No
  custom public error type is added.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 2.5, 4.1, 4.2, 4.4, 7.4, and
    Phase 0
  - `spikes/sidecar_otel/RESULT.md` promotion requirements

## Related Fact IDs

- FS-001
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/child_bootstrap.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_runtime_config.py`
- `tests/sidecar/test_child_bootstrap.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `flowsight/sidecar/child_bootstrap.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_runtime_config.py`
- `tests/sidecar/test_child_bootstrap.py`

## Forbidden

- Do not add a CLI, `__main__`, `sys.argv`/`argparse` reader, complete Python
  command, environment mapping, launcher, `Popen`, fork, exec, `pass_fds`
  execution, process polling, signalling, termination, killing, waiting,
  reaping, or background thread.
- Do not accept, inspect, copy, close, detach, duplicate, set inheritable,
  `fstat`, or otherwise act on an `OwnerLock`, `StartupWriter`, socket, or live
  descriptor. The two exact integers are inert protocol scalars in this task.
- Do not bind or inherit a listener. Do not create, publish, load, remove, or
  repair `SidecarState`; construct or mutate a runtime directory; send or
  receive READY; instantiate FastAPI/Uvicorn; or decide incumbent/requested-port
  compatibility.
- Do not change `FlowSight`, SDK lifecycle, election, startup channel, existing
  sidecar primitives, SQLite/writer/queue/lease behavior, OTel, reload,
  shutdown, source scanning, UI, browser behavior, dependencies, or Makefile.
  The existing runtime-config test may change only its exact expected
  `flowsight.sidecar` exports and public-submodule set for this new codec.
- Do not import spike code. Apart from this one fixed bootstrap codec, do not
  add a second schema, alternate decoder, mapping/JSON form, general or
  alternate `SidecarRuntimeConfig` serializer, `to_wire`/`from_wire` method,
  mutable module state, callback, cache, or derived public configuration
  constructor.
- Do not put a capability token, final port, startup ID, PID, database path,
  state/lock path, raw exception, original argv object, or caller-controlled
  malformed value into the protocol result, repr, fixed error, log,
  stdout/stderr, callback, cache, or side result. Fixed-error paths must delete
  the input config/argv, path, descriptor, encoding, and caught-exception
  locals before the fixed raise so production codec traceback frames retain
  none of them. Caller-owned frames are outside this retention claim. The
  two canonical project/runtime paths are intentional internal handoff fields
  only and may never be echoed by errors or repr. An identity-preserved
  non-`Exception` `BaseException` may retain its caller-owned payload and
  traceback; production never inspects, formats, emits, or caches either.

## Acceptance Criteria

- [x] `CHILD_BOOTSTRAP_SCHEMA_VERSION` is the exact built-in integer `1`.
  The constant, `SidecarChildBootstrap`, `encode_sidecar_child_bootstrap`, and
  `decode_sidecar_child_bootstrap` are identical `flowsight.sidecar` exports,
  occur exactly once each in `__all__`, and have the fixed signatures above. No
  alternate constructor, decoder, command builder, environment builder, or
  descriptor-owner surface is added.
- [x] The canonical argv suffix is one exact built-in tuple containing exactly
  these eight exact built-in strings in order:
  `flowsight-sidecar-bootstrap-v1`, `project-root=<value>`,
  `runtime-root=<value>`, `project-id=<value>`,
  `requested-port=none|<canonical decimal>`,
  `startup-timeout=<canonical float.hex()>`,
  `owner-lock-fd=<canonical decimal>`, and
  `startup-writer-fd=<canonical decimal>`. The sum of each argument's exact
  `os.fsencode` byte length plus its terminating NUL is at most 9216 bytes.
  Production captures the canonical `os.fsencode` implementation at import;
  public attribute replacement cannot redirect it, while a private fault seam
  exists only to prove exact-byte failures and the 9216/9217 boundary.
- [x] Encoding accepts only an exact `SidecarRuntimeConfig`. It reads its five
  exact scalar fields without dynamic attribute dispatch, re-runs the canonical
  `prepare_sidecar_runtime_config` factory exactly once from the snapshotted
  config project root, requested port, and startup timeout, and requires all
  five re-derived fields to match field-by-field with built-in scalar
  operations, never config `__eq__`, before returning arguments. Its validation
  order is exact config type, exact descriptor types/ranges/distinctness, exact
  five slot names and scalar types, one canonical factory call, full five-field
  equality, then canonical encoding and byte admission. A forged, malformed,
  or changed config fails closed without returning partial arguments.
  Production captures the canonical factory at import; replacing the public
  package attribute cannot redirect it, while a private seam remains fault
  injection only.
- [x] `owner_lock_fd` and `startup_writer_fd` each accept only exact built-in
  integers in `3..2147483647` and must differ. `bool`, subclasses, coercible
  values, negative/stdio/overflow values, and equal descriptors fail before any
  later work. The codec performs no descriptor syscall and does not require the
  integers to identify currently open descriptors.
- [x] Decoding accepts only an exact built-in tuple of eight exact built-in
  strings. It rejects a list/subclass, missing/extra/reordered/duplicate fields,
  unknown version or key, NUL/control text, over-budget bytes, noncanonical
  decimal or float spellings, decimal leading signs/zeroes, invalid
  port/timeout, equal or out-of-range descriptors, and any malformed prefix
  without trusting a mapping or performing later configuration work. The
  decoder uses fixed positions and removes each exact field prefix once; it
  never uses a generic split, mapping, or key lookup that can reinterpret `=`
  inside a supported path.
- [x] The decoder treats transmitted `project-id` and `runtime-root` only as
  consistency proofs. It calls `prepare_sidecar_runtime_config` exactly once
  with the transmitted project root, requested port, and timeout only after the
  complete tuple/type/count/byte-budget/key/order, canonical port/float/FD,
  range, and descriptor-distinctness preflight has succeeded. It then requires
  both derived values and a canonical re-encoding of every field to equal the
  original tuple. Parent/child platformdirs disagreement, a missing or
  non-directory project root, altered canonical identity, derived-field
  disagreement, and noncanonical aliases fail closed. Replacing one directory
  with another at the same canonical path is not detectable by this lexical
  protocol and is not claimed.
- [x] `None`, port `0`, and explicit ports remain distinct. Ports `0`, `1`, and
  `65535`, independently fixed project identities, temporary absolute roots,
  symlink aliases at encoding time, paths containing `=`, spaces, multibyte
  characters and supported surrogateescape text, and representative timeout
  values have deterministic known-vector and exact round-trip tests. The codec
  retains exact `str`/`os.fsencode` identity and performs no Unicode, case, or
  filesystem-name normalization beyond P0-014 canonical preparation. Decimal
  and `float.hex` parsing does not accept alternate spellings that normalize to
  the same value.
- [x] `SidecarChildBootstrap` is an exact immutable slot-only result with only
  `config`, `owner_lock_fd`, and `startup_writer_fd`; direct construction is
  rejected, repr is exactly `<SidecarChildBootstrap>`, and the fixed
  copy/deepcopy/reduce/reduce-ex/getstate hooks and mutation are rejected with
  the exact fixed errors above; the object has no `__dict__`.
  This does not claim to stop a caller from reading public fields and creating
  an unrelated serializer. A successful
  result retains only the newly re-derived exact configuration and two exact
  integers, never the input tuple or an encoding/decoder callback.
- [x] Ordinary validation, encoding, parsing, path/configuration, and result
  construction failures become exactly
  `ValueError("sidecar child bootstrap is invalid")`. It is constructed and
  raised `from None` only after leaving an internal `except` suite and deleting
  every sensitive local; absent a caller-active exception its cause, context,
  notes, and production codec traceback frame locals contain no input
  config/argv, path, descriptor, encoding, or caught exception. A caller's
  already-active Python-managed context may remain, but formatted output
  suppresses it and production never reads or caches it. `KeyboardInterrupt`,
  `SystemExit`, and every other non-`Exception` `BaseException` preserve
  identity and stop all later work.
- [x] Success and every failure leave all caller-owned objects and descriptors
  untouched. Tests and static evidence prove there is no socket/subprocess,
  state mutation, startup channel I/O, descriptor adoption/close/dup/fstat/
  inheritable change, FastAPI/Uvicorn, SDK, storage, OTel, async/thread, time,
  logging/output, cache, callback, spike import, or network behavior.
- [x] Deterministic tests cover the complete public shape, canonical vectors,
  exact type/range/ordering matrices, byte budget boundaries, config
  re-derivation and provenance, hostile objects, non-retention, process-control
  at every dependency seam, fixed-error privacy, project/runtime filesystem
  target metadata unchanged, and AST allowlists capable of failing on alternate
  protocol or out-of-scope behavior. Decoder fault-injected exact-byte tests
  prove 9217 bytes fail before the configuration factory and exactly 9216 bytes
  reach the next lexical stage without claiming that a real filesystem permits
  a 9216-byte project path.
- [x] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on local CPython and the macOS/Linux x CPython 3.12/3.13 CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/sidecar/test_child_bootstrap.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
canonical child-bootstrap argument tests and all repository checks pass
```

## Risks

- Trusting transmitted project/runtime identity rather than re-deriving it can
  make parent and child attach to different project-scoped stores.
- Permissive integer/float parsing can create multiple encodings for one
  semantic bootstrap and make process tests ambiguous.
- Descriptor numbers identify process-local table entries only. This codec
  cannot prove they are inherited, active, correctly typed, or still owned;
  child-side exact adoption and partial-adoption cleanup remain a later task.
- The paths are intentional internal argv values and may be visible to same-user
  local process inspection. No capability token or runtime secret is generated
  or transferred here; the future sidecar generates its token after bootstrap.
- The 9216-byte limit covers the declared maximum suffix fields only. It does
  not prove that an arbitrary future complete command plus environment fits a
  platform's `ARG_MAX`; that belongs to the launcher task.
- Successful re-derivation is a point-in-time lexical/configuration check, not a
  source-containment, inode-continuity, same-path replacement detector, or hard
  process-interrupt guarantee.

## Reviewer Focus

- Can malformed or noncanonical argv produce a successful result, different
  project identity, ambiguous port/timeout, or equal/wrong descriptor values?
- Can encode/decode trust a forged config or transmitted derived field instead
  of calling the canonical factory and comparing all five scalars?
- Can any failure/repr/retained object leak a path, descriptor, raw argv, or OS
  error, or invoke caller-controlled protocols?
- Did this stop before ownership transfer, listener, process, CLI, READY/state,
  Uvicorn, SDK lifecycle, storage, OTel, reload, and UI behavior?

## Role Outputs

Implementer:
- Added one side-effect-free Phase 0 codec for the canonical eight-argument
  schema, exact runtime-config re-derivation, inert descriptor scalars, bounded
  filesystem encoding, and an immutable decoded result. The public wrappers
  normalize every ordinary failure only after deleting all production-frame
  inputs and status locals; non-`Exception` process control retains identity.
- Added deterministic round trips, complete lexical/type/range matrices,
  byte-budget seams, factory/fsencode provenance checks, fixed-error privacy,
  object-release evidence, and an exact positive AST structure/call boundary.
  No launcher, CLI, descriptor ownership, listener, state, READY, Uvicorn, SDK,
  storage, telemetry, or UI behavior was added.

Adversarial Reviewer:
- Reviewer 1: found an impossible same-path replacement guarantee, an undefined
  error/export surface, missing legal `=`/Unicode path vectors, ambiguous
  retry wording, and the omitted hard-coded export-test allowlist. The revised
  card fixes each issue and the final review reported P0/P1/P2 = 0 and GO.
- Reviewer 2: found incomplete lexical-before-factory ordering, caller-context
  ambiguity, untested byte boundaries, mutable factory/fsencode dispatch, and
  a stale queue README. The revised card freezes order/provenance/errors,
  requires the 9216/9217 seam and Unicode evidence, updates the queue record,
  and received final P0/P1/P2 = 0 and GO.
- Three implementation reviewers initially found a singleton-input traceback
  retention bug plus false-green gaps in per-field comparisons, opaque/dynamic
  AST calls, nested definitions/module caches, captured factory provenance,
  all-position type/control coverage, nonempty byte-boundary evidence, and
  partial-candidate release. The wrappers now delete every failure local before
  the fixed raise; the tests exercise all five exact fields and all eight
  positions, prove candidate collection, and account for every one of the 111
  production calls plus exact recursive/top-level structure. All three final
  reviews reported P0/P1/P2 = 0 and GO.

Fixer:
- Codex primary accepted every contract finding, kept the simpler ordered
  eight-argument protocol instead of adding JSON parser ambiguity, and expanded
  no product/runtime scope.
- Codex primary accepted every implementation finding. The only production
  corrections were failure-frame local deletion and a semantically identical
  explicit `float.hex` call that makes the static call boundary complete; all
  other fixes strengthened behavioral and structural regression evidence.

Quality Governor:
- Independent review confirmed exactly one Phase 0 codec slice, direct
  dependencies only, an open prerequisite gate, the necessary four-file
  implementation/test allowlist, and strict separation from launcher, child
  adoption, listener, state, READY, Uvicorn, SDK, storage, OTel, and UI work.
  Final P0/P1/P2 = 0 and GO.
- The implementation diff remains inside the exact four-file allowlist plus
  this task card. Exact imports, globals, classes, functions, slot operations,
  and call counts reject alternate protocols, mutable caches, callbacks,
  descriptor/process operations, and hidden later-phase behavior.

## Verifier Evidence

- Command: focused child-bootstrap/runtime-config tests; `make test-phase0`;
  `make check`; `make gate-phase0`; candidate GitHub Actions matrix
- Result: passed
- Notes: the final focused suite passed 513 tests and `make test-phase0` passed
  2,105 tests. `make gate-phase0` ran the complete `make check` path with 2,230
  passing tests and then passed the `phase0-sustained` gate on local macOS
  CPython 3.13.5. Ruff format/lint,
  strict mypy, `scripts/validate_agent_system.py`, `git diff --check`, and JSONL
  parsing passed for the reviewed card and FSQ-0001 record. Implementation
  candidate `b961653a17f171da751edeceb353711cc1a30319` passed
  [run 29168092092](https://github.com/alovwang-sys/FlowSight/actions/runs/29168092092)
  with jobs `86584611628` (Ubuntu 3.12), `86584611629` (Ubuntu 3.13),
  `86584611634` (macOS 3.12), and `86584611635` (macOS 3.13). Planned
  contract commit `39af3fe215ed4b767f5c0049f7958594f8158e3d` passed
  [run 29166816747](https://github.com/alovwang-sys/FlowSight/actions/runs/29166816747)
  with jobs `86581228477` (macOS 3.13), `86581228478` (macOS 3.12),
  `86581228485` (Ubuntu 3.13), and `86581228492` (Ubuntu 3.12). This is
  retained as task-system evidence only, not codec or Phase 0 product
  acceptance. FSQ-0001 did not recur in the candidate matrix.

## Failure Queue Items

- FSQ-0001 is an unrelated Phase 4 benchmark-variance failure recorded from
  the P0-015 completion-only CI run. This task must not modify, skip, relax, or
  selectively rerun that benchmark to obtain green evidence; the single normal
  executions inside commands that collect it (`make check`,
  `make gate-phase0`, and CI) remain required, and any recurrence is recorded
  against the existing queue item.
