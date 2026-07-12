# Task: Gate Child Publication on Parent Handoff

## Task Metadata

```yaml
task_id: P0-025
release: v1
task_type: implementation
status: in_progress
primary_phase: phase0
impacted_phases: []
depends_on: [P0-014, P0-016, P0-017, P0-018, P0-022, P0-023, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-025

## Phase

Phase 0

## Goal

Add one private, close-only parent-handoff descriptor to the child bootstrap so
the child cannot adopt resources, publish state, bind its listener, or send
READY until its launcher has completed its irreversible process-ownership
handoff. The later P0-024 parent task alone creates, inherits, and releases
that descriptor.

## Context

- P0-024 adversarial review found that a direct child can publish a healthy
  state before a parent-side reaper handoff completes. A concurrent loser can
  then attach to a child that the first caller later terminates after a local
  handoff failure. The ownership boundary must therefore be inside the child
  protocol, before P0-022 begins its state/listener transaction.
- P0-016's current eight-argument version-1 bootstrap cannot carry that
  authority. This task advances that private, unreleased schema to version 2
  with one exact `parent-handoff-fd` integer. It is not a public user setting,
  listener handoff, configuration field, capability token, or alternate launch
  path.
- The new descriptor is a one-direction close-only pipe. The child validates
  and consumes its exact read endpoint once, waits under the already-prepared
  startup timeout for EOF only, and then closes it. Any byte, invalid endpoint,
  early channel fault, timeout, ordinary error, or synchronous process control
  prevents all later adoption/state/listener/READY work. Parent writer creation
  and EOF release belong only to P0-024.
- P0-018/P0-022 retain ownership of the existing owner-lock and startup-writer
  pair. This task does not change their result shape, state transaction,
  listener/server lifecycle, READY protocol, or child entry's exact argv
  forwarding behavior.
- The fixed private bootstrap surface changes only as follows:

  ```python
  CHILD_BOOTSTRAP_SCHEMA_VERSION = 2

  class SidecarChildBootstrap:
      config: SidecarRuntimeConfig
      owner_lock_fd: int
      startup_writer_fd: int
      parent_handoff_fd: int

  def encode_sidecar_child_bootstrap(
      config: SidecarRuntimeConfig,
      *,
      owner_lock_fd: int,
      startup_writer_fd: int,
      parent_handoff_fd: int,
  ) -> tuple[str, ...]: ...
  ```

- Source of truth: MVP design sections 2.2, 4.2, 4.4, and Phase 0; FS-001,
  FS-007, FS-008, FS-023; P0-016/P0-017/P0-018/P0-022/P0-023; and the
  TRIAL-004 promotion requirements. TRIAL-004 proves the sidecar process
  model, not this new handoff protocol; this task must produce its own focused
  process evidence.

## Related Fact IDs

- FS-001
- FS-007
- FS-008
- FS-023

## Allowed Files

- `flowsight/sidecar/child_bootstrap.py`
- `flowsight/sidecar/child_preparation.py`
- `flowsight/sidecar/parent_handoff.py`
- `flowsight/sidecar/__init__.py`
- `tests/sidecar/test_child_bootstrap.py`
- `tests/sidecar/test_child_preparation.py`
- `tests/sidecar/test_parent_handoff.py`
- `tests/sidecar/test_runtime_config.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

`flowsight/sidecar/__init__.py` may change only for the revised exact
bootstrap exports and the private `parent_handoff` submodule expectation.
`tests/sidecar/test_runtime_config.py` may change only for those exact
sidecar-export and private-submodule expectations.

## Expected Changed Files

- `flowsight/sidecar/child_bootstrap.py`
- `flowsight/sidecar/child_preparation.py`
- `flowsight/sidecar/parent_handoff.py`
- `tests/sidecar/test_child_bootstrap.py`
- `tests/sidecar/test_child_preparation.py`
- `tests/sidecar/test_parent_handoff.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not implement P0-024 parent launch, election, `Popen`, reaper, process
  signalling, parent writer release, incumbent admission, or SDK lifecycle.
- Do not change P0-017 ownership adoption, P0-018/P0-022 child transaction,
  P0-023 argv forwarding, startup READY protocol, state/store schema,
  listener/server, FastAPI/Uvicorn, SQLite, OTel, sender, UI, dependencies,
  packaging, Makefile, or spike code.
- Do not add a listener descriptor, token, PID, port, startup ID, path,
  arbitrary payload, general IPC API, mapping/JSON protocol, environment
  fallback, command option, public configuration option, callback, registry,
  cache, thread, task, subprocess, polling loop, signal policy, or alternate
  bootstrap decoder.
- Do not make the handoff endpoint write-capable in the child. It accepts EOF
  only; a byte, EOF ambiguity, close fault, timeout, malformed descriptor, or
  process-control must fail closed before sidecar resource adoption.
- Do not emit raw descriptor, argv, project/runtime path, exception, timeout,
  or channel contents in errors, repr, logging, stdout/stderr, callback, cache,
  or retained fixed-error frames.

## Acceptance Criteria

- [ ] The private schema advances atomically to version 2 and a canonical
  nine-string tuple with exact final `parent-handoff-fd=<decimal>` field. All
  three descriptor fields are exact, distinct built-in integers in the existing
  supported range; version-1, missing, extra, reordered, duplicate, malformed,
  or noncanonical arguments fail under the existing fixed bootstrap error.
- [ ] The immutable exact bootstrap result has exactly four slots, no new
  constructor/serializer/public result type, and revised encode/decode exports
  occur exactly once in `flowsight.sidecar.__all__`.
- [ ] A new private child-only handoff primitive admits only the exact decoded
  bootstrap/config and its read endpoint, proves a read-only FIFO descriptor,
  waits once for EOF under the prepared timeout, retires that descriptor exactly
  once, and exposes only a fixed ordinary failure. It accepts no caller timeout,
  data, callback, path, or process object.
- [ ] `prepare_sidecar_child` invokes the captured handoff primitive exactly
  once after bootstrap/config admission and before P0-017 adoption. Handoff
  failure or control leaves the existing owner/writer locators untouched and
  reaches no later P0-017/P0-022 state/listener/READY work.
- [ ] Real isolated process evidence proves a held parent writer prevents any
  child state publication/listener/READY activity; closing that writer releases
  exactly one child transaction; malformed/data-bearing/closed/non-pipe
  endpoints and timeout fail before publication; and no case emits a raw
  descriptor, path, token, or exception.
- [ ] Static and behavioral tests freeze the version-2 tuple, exact captured
  dispatch, close-only child semantics, one bounded wait, descriptor close
  order, process-control identity, fixed-error frame privacy, and absence of
  parent launch/reaper/SDK/OTel/store-write/UI behavior.
- [ ] Focused tests, `make test-phase0`, `make check`, and `make gate-phase0`
  pass locally and on the macOS/Linux × CPython 3.12/3.13 CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest \
  tests/sidecar/test_child_bootstrap.py \
  tests/sidecar/test_child_preparation.py \
  tests/sidecar/test_parent_handoff.py \
  tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
the child cannot publish sidecar state before its exact parent handoff is released
```

## Risks

- A child that can publish before parent process ownership is irreversible can
  make a concurrent caller attach to a doomed state.
- Treating arbitrary pipe data as release would create an unreviewed IPC
  protocol and may expose bytes across the child boundary.
- Changing bootstrap fields without atomic version/canonical tests could let a
  parent and child disagree about inherited descriptor ownership.

## Reviewer Focus

- Is release unambiguously EOF-only, bounded, and complete before all P0-017/
  P0-022 resource work?
- Can a malformed or faulting handoff endpoint reach owner/writer adoption,
  state publication, listener bind, or READY?
- Did this revise only the unreleased child protocol and avoid parent/reaper,
  SDK, storage, and UI scope?

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

- Planned: record focused protocol/process evidence, Phase 0 suite, repository
  check, sustained gate, candidate commit, and supported CI matrix before this
  task can become complete.

## Failure Queue Items

- P0-024 review finding: direct-child ownership must linearize before child
  state becomes attachable; this task supplies that prerequisite.
