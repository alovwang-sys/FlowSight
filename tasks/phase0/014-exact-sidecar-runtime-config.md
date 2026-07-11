# Task: Prepare Exact Sidecar Runtime Configuration

## Task Metadata

```yaml
task_id: P0-014
release: v1
task_type: implementation
status: planned
primary_phase: phase0
impacted_phases: []
depends_on: [P0-001, P0-003, P0-013, TRIAL-004]
requires_gates: [phase0-sustained]
opens_gates: []
scope_override: none
scope_override_approved_by: none
```

## Task ID

P0-014

## Phase

Phase 0

## Goal

Normalize one project root into an immutable, non-sensitive scalar sidecar
configuration before any child handoff or process launch.

## Context

- P0-013 ends with either one authenticated incumbent state or one move-only
  owner capability. A future launcher needs a separately reviewed scalar
  configuration before it can define a child protocol or consume that owner.
- The MVP selects platformdirs for the user runtime root. The same canonical
  project root must identify the project-scoped sidecar now and later bound
  Phase 2 source access; omitting it from the pre-handoff config would force a
  child-protocol revision.
- P0-001 remains the authority for canonical project-scoped runtime/state/lock/
  database paths. This task may construct one inert `StateStore` to normalize
  the platformdirs root, but it never ensures or touches that directory.
- The fixed surface is
  `prepare_sidecar_runtime_config(project_root, *, requested_port=None,
  startup_timeout=5.0) -> SidecarRuntimeConfig`.
- This task does not change `FlowSight`. A later SDK lifecycle task will map its
  existing `project_root` and `ui_port` inputs to this already-reviewed factory.
- Source of truth:
  - `docs/flowsight-mvp-design.md` sections 4.1, 4.2, 4.4, 7.4, and Phase 0
  - `spikes/sidecar_otel/RESULT.md` promotion requirements

## Related Fact IDs

- FS-001
- FS-008
- FS-023

## Allowed Files

- `pyproject.toml`
- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/runtime_config.py`
- `tests/sidecar/test_runtime_config.py`

The current task card and its verifier evidence are always writable
control-plane records; they do not expand the product-code allowlist above.

## Expected Changed Files

- `pyproject.toml`
- `flowsight/sidecar/__init__.py`
- `flowsight/sidecar/runtime_config.py`
- `tests/sidecar/test_runtime_config.py`

## Forbidden

- Do not change `FlowSight`, the SDK, examples, or any existing state/listener/
  election primitive. Do not scan the project, read source, import LibCST or a
  code-map module, or add a source-containment API.
- Do not create, chmod, read, publish, remove, repair, or retain a runtime
  directory, state file, lock, database, or other product filesystem object.
  Only strict canonical resolution of the caller's project-root directory and
  inert P0-001 `StateStore` path normalization are permitted.
- Do not inspect incumbent state, decide requested-port compatibility, bind a
  listener, elect/wait for an owner, adopt/close a descriptor, or manufacture a
  startup state.
- Do not define a wire format, environment or command, startup channel, child
  handoff, `pass_fds`, subprocess, process polling/cleanup, ASGI/Uvicorn
  runtime, or READY protocol.
- Do not add SQLite/writer/queue/lease/SDK/OTel/reload/shutdown/UI/browser
  behavior or import spike code. Apart from the exact platformdirs pin already
  selected by the MVP, do not add, remove, or change another dependency.
- Do not include or expose a token, database/state/lock path, PID, startup ID,
  final port, deadline, descriptor, process handle, raw exception, or caller
  path/value in repr, errors, logs, output, callbacks, caches, or side results.
- Do not add a default-port override, public mutable field, public constructor,
  `to_wire`/`from_wire`, serializer, callback, mutable module state, or cache.

## Acceptance Criteria

- [ ] `SidecarRuntimeConfig` is an exact frozen, slot-only value object with
  exactly `project_root: str`, `runtime_root: str`, `project_id: str`,
  `requested_port: int | None`, and `startup_timeout: float`. Direct
  construction always raises the fixed context-free
  `TypeError("SidecarRuntimeConfig must be prepared")`; repr is exactly
  `<SidecarRuntimeConfig>` and exposes no field.
- [ ] `SidecarRuntimeConfig` and `prepare_sidecar_runtime_config` are identical
  `flowsight.sidecar` exports and each occurs exactly once in `__all__`. The
  preparation function has the exact fixed signature and adds no alternate
  constructor, wire helper, public result wrapper, or extra field.
- [ ] `project_root` accepts only an exact built-in `str` or exact platform
  `Path`. It resolves strictly to an existing directory other than the
  filesystem root; relative, absolute, and symlink aliases of one directory
  produce the same exact canonical string and project ID. Empty/NUL text,
  missing paths, files, filesystem roots, derived/duck values, and ordinary
  resolution failures raise fixed context-free input errors without disclosing
  the path; process-control errors preserve identity.
- [ ] `project_id` is exactly
  `project-v1-<sha256(b"flowsight-project-v1\\0" + os.fsencode(canonical_root))>`
  with 64 lowercase hexadecimal digest characters. It contains no raw path and
  changes for distinct canonical roots.
- [ ] Production captures `platformdirs.user_runtime_path` at import and calls
  it exactly once as `user_runtime_path("flowsight", appauthor=False,
  ensure_exists=False)`. `pyproject.toml` has one exact unmarked runtime pin
  `platformdirs==4.10.0` and no duplicate dev pin. Public attribute replacement
  cannot change direct dispatch; a private seam remains fault injection only.
- [ ] The exact platformdirs `Path` is passed to one canonical P0-001
  `StateStore` construction with the derived project ID. The config stores that
  store's exact normalized absolute `runtime_root` as a nonempty, control-free,
  at-most-4096-character built-in string, never its project-specific
  `runtime_dir`. No directory is created and no store or `Path` identity is
  retained. Malformed/ordinary collaborator failure becomes fixed context-free
  `RuntimeError("sidecar runtime configuration failed")`.
- [ ] `requested_port` accepts only `None` or an exact built-in `int` in
  `0..65535`; `bool`, subclasses, coercible values, and out-of-range values fail
  with one fixed preflight error. `None`, `0`, and explicit ports remain
  distinct scalar values; this task performs no bind or compatibility policy.
- [ ] `startup_timeout` accepts only exact built-in `int`/`float` values that
  are finite, positive, and at most 30 seconds, then stores one exact `float`.
  It reads no clock and creates no deadline. Port/timeout failures happen
  before project-root resolution or platformdirs/runtime work.
- [ ] Success contains only independent exact scalar values, is value-equal for
  equal inputs, remains unchanged after caller filesystem aliases change, and
  retains no caller `Path`, platformdirs `Path`, `StateStore`, callback, or raw
  exception. Repr/errors/stdout/stderr expose no project or runtime path.
- [ ] Deterministic tests cover public shape, direct construction, frozen/slots,
  fixed repr/errors, root type/existence/directory/alias matrices, exact digest,
  platformdirs call/provenance/failure, no directory creation, normalized
  StateStore root versus runtime-dir confusion, complete port/timeout
  boundaries, exact float normalization, ordering/no-later-work, equality,
  non-retention, and stdout/stderr privacy.
- [ ] Static and fault-injection evidence proves production performs only exact
  preflight, strict project-root resolution, fixed project-ID hashing, the
  frozen platformdirs query, inert canonical StateStore normalization, scalar
  copying, and private construction. It rejects state mutation, source scan,
  listener/election/channel/process/wire/SDK/storage/runtime/logging/output/
  callback/cache/async/thread/spike behavior.
- [ ] Focused tests, `make test-phase0`, `make check`, and the sustained Phase 0
  gate pass on CPython 3.12/3.13 and the macOS/Linux CI matrix.

## No-Test Reason

N/A

## Verification

Run:

```sh
.venv/bin/python -m pytest tests/sidecar/test_runtime_config.py
make test-phase0
make check
make gate-phase0
```

Expected result:

```text
exact sidecar runtime-configuration tests and all repository checks pass
```

## Risks

- Distinct aliases of one project root must not create duplicate sidecars or
  source-sandbox identities.
- Passing `runtime_dir` instead of `runtime_root` to a child would make
  `StateStore` add a second `project-<digest>` layer and split ownership.
- Treating `bool`, a derived integer, `None`, or port `0` as interchangeable can
  silently change future listener policy.
- A repr, exception, retained input, or premature wire helper can leak a local
  path or freeze an unreviewed child protocol.
- This cooperative timeout is only a future launch admission duration; it is
  not a hard wall-clock process interrupt and this task reads no clock.

## Reviewer Focus

- Can path aliases, platformdirs failure, a forged collaborator, coercion, or
  malformed path create a different successful project/runtime identity?
- Does any output or retained object expose a path or caller-controlled value?
- Did the task stop before handoff, process, listener, election, state mutation,
  requested-port policy, SDK lifecycle, source scanning, and wire format?
- Are the five scalars sufficient for the next child-handoff task without
  inventing its versioned protocol here?

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
- Notes: planned task only; no implementation evidence yet.

## Failure Queue Items

- none
